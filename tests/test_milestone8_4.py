"""
tests/test_milestone8_4.py — Dedicated test suite for Stage 8 Milestone 8.4
Channel-Wide Presentation Defaults and Settings Persistence.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import render
from core.agent import Candidate, Pick, run_pipeline
from studio import server, store


class Milestone84SettingsAndDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.work_dir = self.dir_path / "work"
        self.out_dir = self.dir_path / "out"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        # Isolated config file
        self.config_path = self.dir_path / "config.json"
        self.base_config = {
            "account": {
                "name": "Test Channel",
                "handle": "@TestChannel",
                "avatar": "assets/default_avatar.png",
                "verified": True,
            },
            "layout": {
                "canvas_width": 1080,
                "canvas_height": 1920,
                "border_color": "#1D9BF0",
                "corner_radius": 40,
                "headline_size": 76,
                "presentation_mode": "card",
                "footage_fit": "auto",
            },
            "posting": {
                "max_clip_seconds": 30,
                "privacy": "private",
                "tags": ["test"],
            },
            "editorial": {
                "provider_order": ["gemini"],
                "max_candidates": 1,
            },
            "discovery": {
                "subreddits": ["technology"],
                "news_rss": ["https://feeds.test.com/rss"],
            },
            "story_layout": {
                "card_opacity": 0.85,
            },
        }
        self.config_path.write_text(json.dumps(self.base_config), encoding="utf-8")

        # Patch server paths and store
        self.orig_root = server.ROOT
        server.ROOT = self.dir_path

        self.orig_store_root = store.ROOT
        self.orig_store_db = store.DB
        store.ROOT = self.dir_path
        store.DB = self.dir_path / "studio.sqlite3"
        with store.connect():
            pass

    def tearDown(self):
        server.ROOT = self.orig_root
        store.ROOT = self.orig_store_root
        store.DB = self.orig_store_db
        self.temp_dir.cleanup()

    def test_validate_settings_config_accepts_valid_mode_and_fit(self):
        """1. validate_settings_config accepts supported presentation_mode and footage_fit."""
        patch_payload = {
            "layout": {
                "presentation_mode": "footage_clean",
                "footage_fit": "cover",
            }
        }
        updated = server.validate_settings_config(patch_payload)
        self.assertEqual(updated["layout"]["presentation_mode"], "footage_clean")
        self.assertEqual(updated["layout"]["footage_fit"], "cover")

        # Verify case insensitivity and whitespace trimming
        patch_whitespace = {
            "layout": {
                "presentation_mode": "  FOOTAGE_CLEAN  ",
                "footage_fit": "  CONTAIN  ",
            }
        }
        updated_ws = server.validate_settings_config(patch_whitespace)
        self.assertEqual(updated_ws["layout"]["presentation_mode"], "footage_clean")
        self.assertEqual(updated_ws["layout"]["footage_fit"], "contain")

        # Verify card mode and auto fit
        patch_card = {
            "layout": {
                "presentation_mode": "card",
                "footage_fit": "auto",
            }
        }
        updated_card = server.validate_settings_config(patch_card)
        self.assertEqual(updated_card["layout"]["presentation_mode"], "card")
        self.assertEqual(updated_card["layout"]["footage_fit"], "auto")

    def test_validate_settings_config_rejects_invalid_and_reserved_values(self):
        """2. validate_settings_config rejects reserved mode, invalid mode, invalid fit, and bad types."""
        # Reserved mode
        with self.assertRaises(ValueError) as ctx:
            server.validate_settings_config({"layout": {"presentation_mode": "footage_caption"}})
        self.assertIn("reserved", str(ctx.exception))

        # Invalid mode
        with self.assertRaises(ValueError) as ctx:
            server.validate_settings_config({"layout": {"presentation_mode": "invalid_mode"}})
        self.assertIn("Invalid presentation_mode", str(ctx.exception))

        # Non-string mode
        with self.assertRaises(ValueError) as ctx:
            server.validate_settings_config({"layout": {"presentation_mode": 123}})
        self.assertIn("expected string", str(ctx.exception))

        # Invalid fit
        with self.assertRaises(ValueError) as ctx:
            server.validate_settings_config({"layout": {"footage_fit": "stretch"}})
        self.assertIn("Invalid footage_fit", str(ctx.exception))

        # Non-string fit
        with self.assertRaises(ValueError) as ctx:
            server.validate_settings_config({"layout": {"footage_fit": ["cover"]}})
        self.assertIn("expected string", str(ctx.exception))

    def test_validate_settings_config_preserves_unrelated_settings_and_existing_fields(self):
        """3. Saving unrelated sections or layout attributes preserves existing presentation_mode and footage_fit."""
        # Set clean footage in config first
        initial_patch = {
            "layout": {
                "presentation_mode": "footage_clean",
                "footage_fit": "contain",
            }
        }
        saved_initial = server.validate_settings_config(initial_patch)
        server.save_config(saved_initial)

        # 3a: Update only account settings
        account_patch = {
            "account": {
                "name": "Updated Channel Name",
            }
        }
        updated_acct = server.validate_settings_config(account_patch)
        server.save_config(updated_acct)
        on_disk_acct = server.get_config()
        self.assertEqual(on_disk_acct["account"]["name"], "Updated Channel Name")
        self.assertEqual(on_disk_acct["layout"]["presentation_mode"], "footage_clean")
        self.assertEqual(on_disk_acct["layout"]["footage_fit"], "contain")

        # 3b: Update only layout border_color
        border_patch = {
            "layout": {
                "border_color": "#FF0000",
            }
        }
        updated_border = server.validate_settings_config(border_patch)
        server.save_config(updated_border)
        on_disk_border = server.get_config()
        self.assertEqual(on_disk_border["layout"]["border_color"], "#FF0000")
        self.assertEqual(on_disk_border["layout"]["presentation_mode"], "footage_clean")
        self.assertEqual(on_disk_border["layout"]["footage_fit"], "contain")
        self.assertEqual(on_disk_border["layout"]["canvas_width"], 1080)

    def test_settings_save_config_roundtrip_persistence(self):
        """4. Complete settings roundtrip persists presentation_mode and footage_fit to disk."""
        patch_payload = {
            "layout": {
                "border_color": "#00FF00",
                "corner_radius": 50,
                "headline_size": 80,
                "presentation_mode": "footage_clean",
                "footage_fit": "cover",
            }
        }
        validated = server.validate_settings_config(patch_payload)
        server.save_config(validated)

        loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["layout"]["presentation_mode"], "footage_clean")
        self.assertEqual(loaded["layout"]["footage_fit"], "cover")
        self.assertEqual(loaded["layout"]["border_color"], "#00FF00")
        self.assertEqual(loaded["layout"]["corner_radius"], 50)
        self.assertEqual(loaded["layout"]["headline_size"], 80)

    def test_automated_discovery_consumes_saved_channel_defaults(self):
        """5. Automated run_pipeline() consumes saved layout defaults without CLI overrides."""
        # Save channel defaults via server validation
        patch_payload = {
            "layout": {
                "presentation_mode": "footage_clean",
                "footage_fit": "cover",
            }
        }
        validated = server.validate_settings_config(patch_payload)
        server.save_config(validated)

        cand = Candidate(
            kind="clip",
            key="automated_discovery_candidate",
            title="Auto Discovery Candidate",
            url="https://youtube.com/watch?v=auto_test_123",
            source="youtube",
        )
        mock_pick = Pick(
            candidate=cand,
            headline="Auto Test Headline",
            caption="Auto Test Caption",
            title="Auto Test Title",
            description="Auto Test Description",
        )

        captured_render_kwargs = {}

        def mock_render(**kwargs):
            nonlocal captured_render_kwargs
            captured_render_kwargs = kwargs
            kwargs["out"].touch()
            if kwargs.get("poster"):
                kwargs["poster"].touch()
            return render.RenderResult(
                output=kwargs["out"],
                overlay=None,
                video_box=(0, 0, 1080, 1920),
                poster=kwargs.get("poster"),
                duration=5.0,
                presentation_mode=kwargs.get("presentation_mode", "card"),
                footage_fit=kwargs.get("footage_fit", "auto"),
            )

        def fake_download(url, out_path, max_seconds=35):
            Path(out_path).touch()
            return True

        with patch("core.agent.discover_all_candidates", return_value=[cand]), \
             patch("core.agent.pick_stories", return_value=[mock_pick]), \
             patch("core.agent.download_clip", side_effect=fake_download), \
             patch("core.agent.get_source_aspect_ratio", return_value=0.5625), \
             patch("core.agent.render", side_effect=mock_render), \
             patch("core.agent.ROOT", self.dir_path), \
             patch("core.agent.State"):
            # run_pipeline invoked with ZERO CLI flag overrides
            run_pipeline(self.config_path, dry_run=True)

        # 1. Renderer received saved channel defaults
        self.assertEqual(captured_render_kwargs.get("presentation_mode"), "footage_clean")
        self.assertEqual(captured_render_kwargs.get("footage_fit"), "cover")

        # 2. Pick mutated with resolved values
        self.assertEqual(mock_pick.presentation_mode, "footage_clean")
        self.assertEqual(mock_pick.footage_fit, "cover")

        # 3. Draft in SQLite store persisted resolved values
        videos = store.records("videos")
        self.assertEqual(len(videos), 1)
        video_rec = videos[0]
        self.assertEqual(video_rec.get("presentation_mode"), "footage_clean")
        self.assertEqual(video_rec.get("footage_fit"), "cover")
        self.assertEqual(video_rec.get("candidate", {}).get("key"), "automated_discovery_candidate")

    def test_backward_compatibility_missing_defaults(self):
        """6. Missing presentation_mode and footage_fit in config layout default to card and auto."""
        # Config without presentation_mode or footage_fit in layout
        legacy_config = {
            "clips": {"max_seconds": 30},
            "editorial": {"max_candidates": 1},
            "layout": {
                "border_color": "#1D9BF0",
                "corner_radius": 40,
                "headline_size": 76,
            },
        }
        legacy_config_file = self.dir_path / "legacy_config.json"
        legacy_config_file.write_text(json.dumps(legacy_config), encoding="utf-8")

        cand = Candidate(
            kind="clip",
            key="legacy_candidate",
            title="Legacy Candidate",
            url="https://youtube.com/watch?v=legacy_123",
            source="youtube",
        )
        mock_pick = Pick(
            candidate=cand,
            headline="Legacy Headline",
            caption="Legacy Caption",
            title="Legacy Title",
            description="Legacy Description",
        )

        captured_render_kwargs = {}

        def mock_render(**kwargs):
            nonlocal captured_render_kwargs
            captured_render_kwargs = kwargs
            kwargs["out"].touch()
            if kwargs.get("poster"):
                kwargs["poster"].touch()
            return render.RenderResult(
                output=kwargs["out"],
                overlay=None,
                video_box=(0, 0, 1080, 1920),
                poster=kwargs.get("poster"),
                duration=5.0,
                presentation_mode=kwargs.get("presentation_mode", "card"),
                footage_fit=kwargs.get("footage_fit", "auto"),
            )

        def fake_download(url, out_path, max_seconds=35):
            Path(out_path).touch()
            return True

        with patch("core.agent.discover_all_candidates", return_value=[cand]), \
             patch("core.agent.pick_stories", return_value=[mock_pick]), \
             patch("core.agent.download_clip", side_effect=fake_download), \
             patch("core.agent.get_source_aspect_ratio", return_value=1.777), \
             patch("core.agent.render", side_effect=mock_render), \
             patch("core.agent.ROOT", self.dir_path), \
             patch("core.agent.State"):
            run_pipeline(legacy_config_file, dry_run=True)

        self.assertEqual(captured_render_kwargs.get("presentation_mode"), "card")
        self.assertEqual(captured_render_kwargs.get("footage_fit"), "auto")

        videos = store.records("videos")
        matching = [v for v in videos if v.get("candidate", {}).get("key") == "legacy_candidate"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].get("presentation_mode"), "card")
        self.assertEqual(matching[0].get("footage_fit"), "auto")

    def test_channel_style_ui_contract_and_export(self):
        """7. Studio UI contains style mode/fit selectors and exports onStylePresentationModeChange."""
        app_js = Path("studio/web/app.js").read_text(encoding="utf-8")

        self.assertIn("function onStylePresentationModeChange", app_js)
        self.assertIn("style-presentation-mode", app_js)
        self.assertIn("style-footage-fit", app_js)
        self.assertIn("style-footage-fit-group", app_js)
        self.assertIn("onStylePresentationModeChange", app_js)

        # Node.js contract test for onStylePresentationModeChange export
        node_script = """
        const app = require('./studio/web/app.js');
        const assert = require('assert');
        assert.strictEqual(typeof app.onStylePresentationModeChange, 'function', 'onStylePresentationModeChange must be exported');
        console.log('OK');
        """
        proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node script failed: {proc.stderr}")
        self.assertIn("OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
