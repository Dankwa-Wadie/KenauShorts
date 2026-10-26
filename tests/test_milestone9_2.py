"""
tests/test_milestone9_2.py — Dedicated test suite for Milestone 9.2:
Channel Defaults, Settings Governance, and Studio Controls for Card Visual Effects.

Covers:
- Settings validation, invalid values, and partial-update preservation in studio/server.py.
- API persistence and round-trip behavior for card_background_mode, ambient_dim, and card_shadow.
- Production StudioHandler.mutate execution for draft overrides.
- Worker rendering precedence in studio/worker.py:
  1. Explicit per-draft override (including explicit False)
  2. Live channel-level config["layout"] (bypassing stale creation snapshot)
  3. Existing legacy configuration location / style preset fallback
  4. Milestone 9.1 renderer default
- Clean-footage isolation and legacy-draft compatibility.
- UI contract testing for draft modal section toggling and style preview listeners.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import render
from studio import server, store, worker


class TestMilestone92SettingsValidation(unittest.TestCase):
    """Verifies validate_settings_config whitelist, type validation, and partial updates."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.orig_root = server.ROOT
        server.ROOT = self.tmp

        # Write base config.json
        self.cfg_file = self.tmp / "config.json"
        self.base_cfg = {
            "account": {"name": "Kenau", "handle": "@kenau", "verified": True},
            "layout": {
                "border_color": "#1D9BF0",
                "corner_radius": 40,
                "headline_size": 76,
                "presentation_mode": "card",
                "footage_fit": "auto",
                "card_background_mode": "solid",
                "ambient_dim": 0.45,
                "card_shadow": False,
            },
        }
        self.cfg_file.write_text(json.dumps(self.base_cfg, indent=2), encoding="utf-8")

    def tearDown(self):
        server.ROOT = self.orig_root
        self.td.cleanup()

    def test_valid_card_background_mode_accepted(self):
        for mode in ["solid", "blurred_ambient", "  SOLID  ", "  BLURRED_AMBIENT  "]:
            updated = server.validate_settings_config({"layout": {"card_background_mode": mode}})
            expected = mode.strip().lower()
            self.assertEqual(updated["layout"]["card_background_mode"], expected)

    def test_invalid_card_background_mode_rejected(self):
        for invalid in ["gradient", "video_blur", "", 123, True, None, []]:
            with self.assertRaises(ValueError):
                server.validate_settings_config({"layout": {"card_background_mode": invalid}})

    def test_valid_card_shadow_strict_boolean_accepted(self):
        updated_true = server.validate_settings_config({"layout": {"card_shadow": True}})
        self.assertTrue(updated_true["layout"]["card_shadow"])

        updated_false = server.validate_settings_config({"layout": {"card_shadow": False}})
        self.assertFalse(updated_false["layout"]["card_shadow"])

    def test_invalid_card_shadow_rejected(self):
        # Strict boolean requirement: strings and numbers must be rejected in validate_settings_config
        for invalid in ["true", "false", 1, 0, "yes", "no", None, []]:
            with self.assertRaises(ValueError):
                server.validate_settings_config({"layout": {"card_shadow": invalid}})

    def test_valid_ambient_dim_accepted(self):
        for val, expected in [(0.0, 0.0), (0.45, 0.45), (1.0, 1.0), (0, 0.0), (1, 1.0)]:
            updated = server.validate_settings_config({"layout": {"ambient_dim": val}})
            self.assertAlmostEqual(updated["layout"]["ambient_dim"], expected)
            self.assertIsInstance(updated["layout"]["ambient_dim"], float)

    def test_invalid_ambient_dim_rejected(self):
        for invalid in [-0.01, 1.01, -1, 2, "0.45", True, False, None, []]:
            with self.assertRaises(ValueError):
                server.validate_settings_config({"layout": {"ambient_dim": invalid}})

    def test_ambient_blur_validation(self):
        for valid in [1, 10, 50]:
            updated = server.validate_settings_config({"layout": {"ambient_blur": valid}})
            self.assertEqual(updated["layout"]["ambient_blur"], valid)

        for invalid in [0, 51, -5, "10", 3.14, True, False]:
            with self.assertRaises(ValueError):
                server.validate_settings_config({"layout": {"ambient_blur": invalid}})

    def test_shadow_parameter_validations(self):
        # shadow_blur
        self.assertEqual(server.validate_settings_config({"layout": {"shadow_blur": 24}})["layout"]["shadow_blur"], 24)
        with self.assertRaises(ValueError):
            server.validate_settings_config({"layout": {"shadow_blur": 0}})
        with self.assertRaises(ValueError):
            server.validate_settings_config({"layout": {"shadow_blur": 101}})

        # shadow_opacity
        self.assertAlmostEqual(server.validate_settings_config({"layout": {"shadow_opacity": 0.4}})["layout"]["shadow_opacity"], 0.4)
        with self.assertRaises(ValueError):
            server.validate_settings_config({"layout": {"shadow_opacity": -0.1}})
        with self.assertRaises(ValueError):
            server.validate_settings_config({"layout": {"shadow_opacity": 1.1}})
        with self.assertRaises(ValueError):
            server.validate_settings_config({"layout": {"shadow_opacity": True}})

        # shadow_offset_y
        self.assertEqual(server.validate_settings_config({"layout": {"shadow_offset_y": -20}})["layout"]["shadow_offset_y"], -20)
        self.assertEqual(server.validate_settings_config({"layout": {"shadow_offset_y": 50}})["layout"]["shadow_offset_y"], 50)
        with self.assertRaises(ValueError):
            server.validate_settings_config({"layout": {"shadow_offset_y": -51}})
        with self.assertRaises(ValueError):
            server.validate_settings_config({"layout": {"shadow_offset_y": 51}})

    def test_partial_settings_update_preserves_layout_visual_settings(self):
        # Update config with non-default visual settings
        self.base_cfg["layout"]["card_background_mode"] = "blurred_ambient"
        self.base_cfg["layout"]["ambient_dim"] = 0.60
        self.base_cfg["layout"]["card_shadow"] = True
        self.cfg_file.write_text(json.dumps(self.base_cfg, indent=2), encoding="utf-8")

        # Now execute partial update touching only account
        partial_patch = {"account": {"name": "New Channel Name"}}
        updated = server.validate_settings_config(partial_patch)

        # Confirm account was updated AND layout visual settings were preserved
        self.assertEqual(updated["account"]["name"], "New Channel Name")
        self.assertEqual(updated["layout"]["card_background_mode"], "blurred_ambient")
        self.assertEqual(updated["layout"]["ambient_dim"], 0.60)
        self.assertTrue(updated["layout"]["card_shadow"])


class TestMilestone92VideoMutationAndDraftOverrides(unittest.TestCase):
    """Verifies POST /api/video mutation, explicit inheritance representations, and explicit False."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.orig_store_root = store.ROOT
        self.orig_store_db = store.DB
        self.orig_server_root = server.ROOT

        store.ROOT = self.tmp
        store.DB = self.tmp / "studio.sqlite3"
        server.ROOT = self.tmp

        with store.connect():
            pass

    def tearDown(self):
        store.ROOT = self.orig_store_root
        store.DB = self.orig_store_db
        server.ROOT = self.orig_server_root
        self.td.cleanup()

    def _create_mock_handler(self, payload: dict) -> dict:
        """Invokes POST /api/video handling logic directly via production StudioHandler.mutate."""
        vid_id = payload.get("id")
        if vid_id and not store.get("videos", vid_id):
            store.put("videos", vid_id, {
                "id": vid_id,
                "title": "Initial Title",
                "headline": "Initial Headline",
                "status": "ready",
                "config": {"layout": {}},
            })

        handler = server.StudioHandler.__new__(server.StudioHandler)
        handler.headers = {"Host": f"127.0.0.1:{server.PORT}"}
        handler.mutate("/api/video", payload)
        return store.get("videos", vid_id) if vid_id else {}

    def test_explicit_card_background_mode_override_persists(self):
        rec = self._create_mock_handler({"id": "v1", "card_background_mode": "blurred_ambient"})
        self.assertEqual(rec["card_background_mode"], "blurred_ambient")
        self.assertEqual(rec["config"]["layout"]["card_background_mode"], "blurred_ambient")

    def test_resetting_card_background_mode_to_none_clears_override(self):
        # Set first
        self._create_mock_handler({"id": "v1", "card_background_mode": "blurred_ambient"})
        # Reset with None
        rec = self._create_mock_handler({"id": "v1", "card_background_mode": None})
        self.assertIsNone(rec["card_background_mode"])
        self.assertNotIn("card_background_mode", rec["config"]["layout"])

        # Reset with empty string
        self._create_mock_handler({"id": "v1", "card_background_mode": "solid"})
        rec2 = self._create_mock_handler({"id": "v1", "card_background_mode": ""})
        self.assertIsNone(rec2["card_background_mode"])
        self.assertNotIn("card_background_mode", rec2["config"]["layout"])

    def test_explicit_card_shadow_true_persists(self):
        rec = self._create_mock_handler({"id": "v1", "card_shadow": True})
        self.assertIs(rec["card_shadow"], True)
        self.assertIs(rec["config"]["layout"]["card_shadow"], True)

    def test_explicit_card_shadow_false_persists_as_boolean_false(self):
        # Critical test: False must NOT be coerced to None or dropped
        rec = self._create_mock_handler({"id": "v1", "card_shadow": False})
        self.assertIs(rec["card_shadow"], False)
        self.assertIsNotNone(rec["card_shadow"])
        self.assertIs(rec["config"]["layout"]["card_shadow"], False)

    def test_resetting_card_shadow_to_none_clears_override(self):
        # Set to True first
        self._create_mock_handler({"id": "v1", "card_shadow": True})
        # Reset with None
        rec = self._create_mock_handler({"id": "v1", "card_shadow": None})
        self.assertIsNone(rec["card_shadow"])
        self.assertNotIn("card_shadow", rec["config"]["layout"])

        # Reset with string 'inherit'
        self._create_mock_handler({"id": "v1", "card_shadow": True})
        rec2 = self._create_mock_handler({"id": "v1", "card_shadow": "inherit"})
        self.assertIsNone(rec2["card_shadow"])

    def test_string_boolean_variants_handled_properly(self):
        rec_true = self._create_mock_handler({"id": "v1", "card_shadow": "true"})
        self.assertIs(rec_true["card_shadow"], True)

        rec_false = self._create_mock_handler({"id": "v1", "card_shadow": "false"})
        self.assertIs(rec_false["card_shadow"], False)

    def test_invalid_card_background_mode_in_mutation_rejected(self):
        with self.assertRaises(ValueError):
            self._create_mock_handler({"id": "v1", "card_background_mode": "invalid_mode"})

    def test_invalid_card_shadow_in_mutation_rejected(self):
        with self.assertRaises(ValueError):
            self._create_mock_handler({"id": "v1", "card_shadow": "invalid_shadow"})

    def test_missing_video_id_raises_value_error(self):
        with self.assertRaises(ValueError):
            self._create_mock_handler({"card_shadow": True})


class TestMilestone92WorkerPrecedence(unittest.TestCase):
    """Verifies studio/worker.py configuration precedence for card_background_mode and card_shadow."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.work_dir = self.tmp / "work"
        self.out_dir = self.tmp / "out"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self.orig_store_root = store.ROOT
        self.orig_store_db = store.DB
        store.ROOT = self.tmp
        store.DB = self.tmp / "studio.sqlite3"

        with store.connect():
            pass

        # Create dummy raw source file
        self.raw_clip = self.work_dir / "vid1_raw.mp4"
        self.raw_clip.write_bytes(b"dummy mp4 raw footage")

    def tearDown(self):
        store.ROOT = self.orig_store_root
        store.DB = self.orig_store_db
        self.td.cleanup()

    def _run_worker_test(self, record_data: dict, channel_config: dict) -> dict:
        """Executes worker.work('render', 'vid1') with mock render.render and returns kwargs."""
        vid_id = "vid1"
        rec = {
            "id": vid_id,
            "status": "ready",
            "headline": "Test Headline",
            "candidate": {"source": "youtube", "key": "test_key", "kind": "clip"},
            **record_data,
        }
        store.put("videos", vid_id, rec)
        (self.tmp / "config.json").write_text(json.dumps(channel_config), encoding="utf-8")

        captured_kwargs = {}

        def mock_render(**kwargs):
            nonlocal captured_kwargs
            captured_kwargs = kwargs
            kwargs["out"].touch()
            if kwargs.get("poster"):
                kwargs["poster"].touch()
            return render.RenderResult(
                output=kwargs["out"],
                overlay=None,
                video_box=(0, 0, 1080, 1920),
                poster=kwargs.get("poster"),
                duration=1.0,
                presentation_mode=kwargs.get("presentation_mode", "card"),
                footage_fit=kwargs.get("footage_fit", "auto"),
                card_background_mode=kwargs.get("card_background_mode", "solid"),
                card_shadow=kwargs.get("card_shadow", False),
            )

        with patch("studio.worker.render.render", side_effect=mock_render), \
             patch("studio.worker.store", store):
            worker.work("render", vid_id)

        return captured_kwargs

    def test_explicit_false_shadow_overrides_channel_true(self):
        """Case 1: Explicit draft override False overrides channel-wide True."""
        record_data = {"card_shadow": False}
        channel_config = {"layout": {"card_shadow": True}}
        captured = self._run_worker_test(record_data, channel_config)
        self.assertIs(captured.get("card_shadow"), False)

    def test_explicit_true_shadow_overrides_channel_false(self):
        """Case 2: Explicit draft override True overrides channel-wide False."""
        record_data = {"card_shadow": True}
        channel_config = {"layout": {"card_shadow": False}}
        captured = self._run_worker_test(record_data, channel_config)
        self.assertIs(captured.get("card_shadow"), True)

    def test_absent_shadow_override_inherits_channel_default(self):
        """Case 3: Absent draft override inherits channel-wide True."""
        record_data = {"card_shadow": None}
        channel_config = {"layout": {"card_shadow": True}}
        captured = self._run_worker_test(record_data, channel_config)
        self.assertIs(captured.get("card_shadow"), True)

    def test_absent_draft_and_channel_shadow_defaults_to_false(self):
        """Case 4: Neither draft nor channel specifies shadow -> defaults to False."""
        record_data = {}  # completely absent
        channel_config = {"layout": {}}
        captured = self._run_worker_test(record_data, channel_config)
        self.assertIs(captured.get("card_shadow"), False)

    def test_explicit_ambient_background_overrides_channel_solid(self):
        """Case 5: Explicit draft blurred_ambient overrides channel solid."""
        record_data = {"card_background_mode": "blurred_ambient"}
        channel_config = {"layout": {"card_background_mode": "solid"}}
        captured = self._run_worker_test(record_data, channel_config)
        self.assertEqual(captured.get("card_background_mode"), "blurred_ambient")

    def test_absent_background_override_inherits_channel_ambient(self):
        """Case 6: Absent draft override inherits channel blurred_ambient."""
        record_data = {"card_background_mode": None}
        channel_config = {"layout": {"card_background_mode": "blurred_ambient"}}
        captured = self._run_worker_test(record_data, channel_config)
        self.assertEqual(captured.get("card_background_mode"), "blurred_ambient")

    def test_absent_draft_and_channel_background_defaults_to_solid(self):
        """Case 7: Neither specifies card_background_mode -> defaults to solid."""
        record_data = {}
        channel_config = {"layout": {}}
        captured = self._run_worker_test(record_data, channel_config)
        self.assertEqual(captured.get("card_background_mode"), "solid")

    def test_draft_snapshot_false_channel_true_inherits_true(self):
        """Behavioral test: Draft creation snapshot had card_shadow=False, but draft inherits (None).
        When channel config is changed to True, next render MUST resolve True (bypassing stale snapshot)."""
        record_data = {
            "card_shadow": None,
            "config": {"layout": {"card_shadow": False}},  # Stale draft snapshot
        }
        channel_config = {"layout": {"card_shadow": True}}  # Live channel setting
        captured = self._run_worker_test(record_data, channel_config)
        self.assertIs(captured.get("card_shadow"), True)

    def test_draft_snapshot_true_channel_false_inherits_false(self):
        """Behavioral test: Draft creation snapshot had card_shadow=True, but draft inherits (None).
        When channel config is changed to False, next render MUST resolve False (bypassing stale snapshot)."""
        record_data = {
            "card_shadow": None,
            "config": {"layout": {"card_shadow": True}},  # Stale draft snapshot
        }
        channel_config = {"layout": {"card_shadow": False}}  # Live channel setting
        captured = self._run_worker_test(record_data, channel_config)
        self.assertIs(captured.get("card_shadow"), False)

    def test_explicit_draft_false_overrides_channel_true_despite_snapshot(self):
        """Behavioral test: Draft has explicit card_shadow=False.
        Even if snapshot was True and channel is True, render MUST resolve False."""
        record_data = {
            "card_shadow": False,
            "config": {"layout": {"card_shadow": True}},
        }
        channel_config = {"layout": {"card_shadow": True}}
        captured = self._run_worker_test(record_data, channel_config)
        self.assertIs(captured.get("card_shadow"), False)

    def test_explicit_draft_true_overrides_channel_false_despite_snapshot(self):
        """Behavioral test: Draft has explicit card_shadow=True.
        Even if snapshot was False and channel is False, render MUST resolve True."""
        record_data = {
            "card_shadow": True,
            "config": {"layout": {"card_shadow": False}},
        }
        channel_config = {"layout": {"card_shadow": False}}
        captured = self._run_worker_test(record_data, channel_config)
        self.assertIs(captured.get("card_shadow"), True)

    def test_draft_snapshot_solid_channel_ambient_inherits_ambient(self):
        """Behavioral test: Draft creation snapshot had card_background_mode='solid', but draft inherits (None).
        When channel config is changed to 'blurred_ambient', next render MUST resolve 'blurred_ambient'."""
        record_data = {
            "card_background_mode": None,
            "config": {"layout": {"card_background_mode": "solid"}},
        }
        channel_config = {"layout": {"card_background_mode": "blurred_ambient"}}
        captured = self._run_worker_test(record_data, channel_config)
        self.assertEqual(captured.get("card_background_mode"), "blurred_ambient")

    def test_draft_snapshot_ambient_channel_solid_inherits_solid(self):
        """Behavioral test: Draft creation snapshot had card_background_mode='blurred_ambient', but draft inherits (None).
        When channel config is changed to 'solid', next render MUST resolve 'solid'."""
        record_data = {
            "card_background_mode": None,
            "config": {"layout": {"card_background_mode": "blurred_ambient"}},
        }
        channel_config = {"layout": {"card_background_mode": "solid"}}
        captured = self._run_worker_test(record_data, channel_config)
        self.assertEqual(captured.get("card_background_mode"), "solid")

    def test_reset_to_inheritance_then_channel_changes_uses_new_channel_setting(self):
        """Behavioral test: Draft had explicit override, is reset to None, then channel setting changes."""
        # 1. Explicit override True overrides channel False
        rec1 = {
            "card_shadow": True,
            "config": {"layout": {"card_shadow": False}},
        }
        cap1 = self._run_worker_test(rec1, {"layout": {"card_shadow": False}})
        self.assertIs(cap1.get("card_shadow"), True)

        # 2. Reset draft override to None; channel is True -> resolves True
        rec2 = {
            "card_shadow": None,
            "config": {"layout": {"card_shadow": False}},
        }
        cap2 = self._run_worker_test(rec2, {"layout": {"card_shadow": True}})
        self.assertIs(cap2.get("card_shadow"), True)

        # 3. Channel changes to False -> resolves False
        cap3 = self._run_worker_test(rec2, {"layout": {"card_shadow": False}})
        self.assertIs(cap3.get("card_shadow"), False)

    def test_clean_footage_mode_isolates_and_preserves_clean_rendering(self):
        """Case 8: presentation_mode == 'footage_clean' passes mode cleanly."""
        record_data = {
            "presentation_mode": "footage_clean",
            "card_background_mode": "blurred_ambient",
            "card_shadow": True,
        }
        channel_config = {"layout": {"presentation_mode": "footage_clean"}}
        captured = self._run_worker_test(record_data, channel_config)
        self.assertEqual(captured.get("presentation_mode"), "footage_clean")

    def test_legacy_draft_compatibility(self):
        """Case 9: Legacy draft without any Stage 9 fields re-renders safely."""
        record_data = {}  # legacy
        channel_config = {"layout": {"border_color": "#1D9BF0"}}
        captured = self._run_worker_test(record_data, channel_config)
        self.assertEqual(captured.get("presentation_mode"), "card")
        self.assertEqual(captured.get("card_background_mode"), "solid")
        self.assertIs(captured.get("card_shadow"), False)


class TestMilestone92UIContracts(unittest.TestCase):
    """Verifies that Node.js UI contracts pass for modal toggling, listener registration, and exports."""

    def test_node_health_ui_suite_passes(self):
        res = subprocess.run(["node", "tests/test_health_ui.js"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"test_health_ui.js failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("All 13 UI health unit tests passed successfully!", res.stdout)

    def test_node_exports_and_ui_behaviors(self):
        node_script = """
        const app = require('./studio/web/app.js');

        // Verify required exports
        const requiredFuncs = [
          'onPresentationModeChange',
          'onManualPresentationModeChange',
          'onStylePresentationModeChange',
          'onStyleCardBgModeChange',
          'setupStylePreview',
          'updateStylePreview'
        ];
        for (const fn of requiredFuncs) {
          if (typeof app[fn] !== 'function') {
            console.error('Missing export: ' + fn);
            process.exit(1);
          }
        }

        // Mock DOM environment
        global.document = {
          getElementById: (id) => {
            if (!global._elements[id]) {
              global._elements[id] = {
                id: id,
                style: {},
                value: '',
                listeners: {},
                addEventListener: function(event, fn) {
                  if (!this.listeners[event]) this.listeners[event] = [];
                  this.listeners[event].push(fn);
                  global._listenerCount++;
                }
              };
            }
            return global._elements[id];
          }
        };

        global._elements = {};
        global._listenerCount = 0;

        // Test 1: onPresentationModeChange toggles edit-visual-effects-section
        const ve = document.getElementById('edit-visual-effects-section');
        app.onPresentationModeChange('footage_clean');
        if (ve.style.display !== 'none') {
          console.error('Expected footage_clean to hide visual effects, got: ' + ve.style.display);
          process.exit(1);
        }
        app.onPresentationModeChange('card');
        if (ve.style.display !== 'block') {
          console.error('Expected card to show visual effects, got: ' + ve.style.display);
          process.exit(1);
        }

        // Test 2: onManualPresentationModeChange does NOT alter edit-visual-effects-section
        ve.style.display = 'untouched';
        app.onManualPresentationModeChange('footage_clean');
        if (ve.style.display !== 'untouched') {
          console.error('onManualPresentationModeChange touched edit-visual-effects-section!');
          process.exit(1);
        }

        // Test 3: repeated bg mode changes do not accumulate event listeners
        global._elements = {};
        global._listenerCount = 0;
        app.setupStylePreview();
        const initialListeners = global._listenerCount;
        if (initialListeners !== 12) {
          console.error('Expected 12 initial listeners, got: ' + initialListeners);
          process.exit(1);
        }

        for (let i = 0; i < 5; i++) {
          app.onStyleCardBgModeChange(i % 2 === 0 ? 'blurred_ambient' : 'solid');
        }
        if (global._listenerCount !== initialListeners) {
          console.error('Listeners multiplied: ' + global._listenerCount + ' vs ' + initialListeners);
          process.exit(1);
        }

        console.log('UI CONTRACTS OK');
        """
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node UI contracts failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("UI CONTRACTS OK", res.stdout)


if __name__ == "__main__":
    unittest.main()
