"""
tests/test_milestone8_5.py — Dedicated test suite for Stage 8 Milestone 8.5
Re-render Pipeline Integrity, Raw-Source Guards, and Library Presentation Filtering.
"""

from __future__ import annotations

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


def _create_synthetic_video(path: Path, width: int = 1280, height: int = 720, duration: float = 1.0) -> Path:
    """Helper to generate a lightweight dummy mp4 file for testing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=blue:s={width}x{height}:d={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ]
    subprocess.run(cmd, check=True)
    return path


class Milestone85ReRenderAndLibraryFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.work_dir = self.dir_path / "work"
        self.out_dir = self.dir_path / "out"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        # Write base config.json in temp dir
        self.config_data = {
            "account": {
                "name": "Test Channel",
                "handle": "@TestChannel",
            },
            "layout": {
                "canvas_width": 1080,
                "canvas_height": 1920,
                "presentation_mode": "footage_clean",
                "footage_fit": "cover",
            },
            "clips": {"max_seconds": 30},
        }
        (self.dir_path / "config.json").write_text(json.dumps(self.config_data), encoding="utf-8")
        (self.dir_path / "config.example.json").write_text(json.dumps(self.config_data), encoding="utf-8")

        # Patch store and server paths to isolated temp directory
        self.orig_store_root = store.ROOT
        self.orig_store_db = store.DB
        self.orig_server_root = server.ROOT
        store.ROOT = self.dir_path
        store.DB = self.dir_path / "studio.sqlite3"
        server.ROOT = self.dir_path

        with store.connect():
            pass

    def tearDown(self):
        store.ROOT = self.orig_store_root
        store.DB = self.orig_store_db
        server.ROOT = self.orig_server_root
        try:
            self.temp_dir.cleanup()
        except OSError:
            pass

    # -------------------------------------------------------------------------
    # 1. Legacy record inherits config.layout.presentation_mode
    # -------------------------------------------------------------------------
    def test_legacy_record_inherits_config_layout_presentation_mode(self):
        """1. A legacy record without presentation_mode inherits config.layout.presentation_mode."""
        raw_file = self.work_dir / "vid_legacy_raw.mp4"
        raw_file.write_bytes(b"dummy_video_bytes")

        record = {
            "id": "vid_legacy",
            "status": "ready",
            "headline": "Legacy Short",
            "title": "Legacy Title",
            "candidate": {"source": "youtube", "key": "legacy_key"},
            # Notice presentation_mode is completely absent
        }
        store.put("videos", "vid_legacy", record)

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
                duration=0.5,
                presentation_mode=kwargs.get("presentation_mode", "card"),
                footage_fit=kwargs.get("footage_fit", "auto"),
            )

        with patch("studio.worker.render.render", side_effect=mock_render), \
             patch("studio.worker.store", store):
            worker.work("render", "vid_legacy")

        self.assertEqual(captured_kwargs.get("presentation_mode"), "footage_clean")
        updated = store.get("videos", "vid_legacy")
        self.assertEqual(updated["presentation_mode"], "footage_clean")

    # -------------------------------------------------------------------------
    # 2. Explicit record setting overrides channel-wide default
    # -------------------------------------------------------------------------
    def test_explicit_record_setting_overrides_channel_wide_default(self):
        """2. An explicit record setting overrides the channel-wide default."""
        raw_file = self.work_dir / "vid_card_raw.mp4"
        raw_file.write_bytes(b"dummy_video_bytes")

        # config has footage_clean, but record explicitly chooses card
        record = {
            "id": "vid_card",
            "status": "ready",
            "headline": "Card Short",
            "title": "Card Title",
            "presentation_mode": "card",
            "candidate": {"source": "youtube", "key": "card_key"},
        }
        store.put("videos", "vid_card", record)

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
                duration=0.5,
                presentation_mode=kwargs.get("presentation_mode", "card"),
                footage_fit=kwargs.get("footage_fit", "auto"),
            )

        with patch("studio.worker.render.render", side_effect=mock_render), \
             patch("studio.worker.store", store):
            worker.work("render", "vid_card")

        self.assertEqual(captured_kwargs.get("presentation_mode"), "card")
        updated = store.get("videos", "vid_card")
        self.assertEqual(updated["presentation_mode"], "card")

    # -------------------------------------------------------------------------
    # 3. footage_fit follows the same precedence rules
    # -------------------------------------------------------------------------
    def test_footage_fit_precedence_rules(self):
        """3. footage_fit follows record -> config.layout -> default precedence."""
        raw_legacy = self.work_dir / "vid_fit_legacy_raw.mp4"
        raw_legacy.write_bytes(b"dummy_video_bytes")
        raw_explicit = self.work_dir / "vid_fit_explicit_raw.mp4"
        raw_explicit.write_bytes(b"dummy_video_bytes")

        # Case A: Legacy record inherits config.layout.footage_fit ("cover")
        record_legacy = {
            "id": "vid_fit_legacy",
            "status": "ready",
            "headline": "Fit Test",
            "title": "Fit Title",
            "candidate": {"source": "youtube", "key": "fit_key"},
        }
        store.put("videos", "vid_fit_legacy", record_legacy)

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
                duration=0.5,
                presentation_mode=kwargs.get("presentation_mode", "card"),
                footage_fit=kwargs.get("footage_fit", "auto"),
            )

        with patch("studio.worker.render.render", side_effect=mock_render), \
             patch("studio.worker.store", store):
            worker.work("render", "vid_fit_legacy")

        self.assertEqual(captured_kwargs.get("footage_fit"), "cover")

        # Case B: Explicit record footage_fit ("contain") overrides config ("cover")
        record_explicit = {
            "id": "vid_fit_explicit",
            "status": "ready",
            "headline": "Fit Explicit",
            "title": "Fit Explicit",
            "footage_fit": "contain",
            "candidate": {"source": "youtube", "key": "fit_key"},
        }
        store.put("videos", "vid_fit_explicit", record_explicit)

        with patch("studio.worker.render.render", side_effect=mock_render), \
             patch("studio.worker.store", store):
            worker.work("render", "vid_fit_explicit")

        self.assertEqual(captured_kwargs.get("footage_fit"), "contain")

    # -------------------------------------------------------------------------
    # 4. Missing raw source causes a clear failure
    # -------------------------------------------------------------------------
    def test_missing_raw_source_fails_closed(self):
        """4. Missing raw source causes worker to fail closed with descriptive ValueError."""
        record = {
            "id": "vid_missing_raw",
            "status": "ready",
            "headline": "Missing Raw",
            "title": "Missing Raw Title",
            "candidate": {"source": "youtube", "key": "missing_src"},
        }
        store.put("videos", "vid_missing_raw", record)

        with patch("studio.worker.store", store):
            with self.assertRaises(ValueError) as ctx:
                worker.work("render", "vid_missing_raw")

            self.assertIn("original raw source video not found on disk", str(ctx.exception))
            self.assertIn("vid_missing_raw", str(ctx.exception))

        updated = store.get("videos", "vid_missing_raw")
        self.assertEqual(updated["status"], "render_failed")
        self.assertIn("original raw source video not found on disk", updated["error"])

    # -------------------------------------------------------------------------
    # 5. Rendered composite is never used as substitute for missing raw footage
    # -------------------------------------------------------------------------
    def test_rendered_composite_never_used_as_raw_substitute(self):
        """5. An existing rendered composite is never used as a substitute for missing raw footage."""
        composite_mp4 = self.out_dir / "short_vid_comp.mp4"
        composite_mp4.write_bytes(b"already_composited_mp4_bytes")

        record = {
            "id": "vid_comp",
            "status": "ready",
            "headline": "Comp Short",
            "title": "Comp Title",
            "video": str(composite_mp4),
            "presentation_mode": "footage_clean",
            "candidate": {"source": "youtube", "key": "comp_key"},
        }
        store.put("videos", "vid_comp", record)

        render_mock = MagicMock()
        with patch("studio.worker.render.render", render_mock), \
             patch("studio.worker.store", store):
            with self.assertRaises(ValueError) as ctx:
                worker.work("render", "vid_comp")

            self.assertIn("Re-rendering from an already-composited output is disabled", str(ctx.exception))
            render_mock.assert_not_called()

    # -------------------------------------------------------------------------
    # 6. Legitimate raw-source path formats and edit lineage work
    # -------------------------------------------------------------------------
    def test_legitimate_raw_source_path_formats_work(self):
        """6. Legitimate raw-source path formats continue to work, including edit lineage."""
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
                duration=0.5,
                presentation_mode="card",
                footage_fit="auto",
            )

        # Path 6A: Direct work/{key}_raw.mp4
        raw_direct = self.work_dir / "short_direct_raw.mp4"
        raw_direct.write_bytes(b"raw_direct")
        rec_direct = {
            "id": "short_direct",
            "status": "ready",
            "headline": "Direct",
            "title": "Direct",
            "candidate": {"source": "youtube", "key": "k1"},
        }
        store.put("videos", "short_direct", rec_direct)
        with patch("studio.worker.render.render", side_effect=mock_render), patch("studio.worker.store", store):
            worker.work("render", "short_direct")
        self.assertEqual(captured_kwargs["video"], raw_direct)

        # Path 6B: Edit lineage key ({orig_key}_edit_{uuid}) matches work/{orig_key}_raw.mp4
        orig_raw = self.work_dir / "short_orig_raw.mp4"
        orig_raw.write_bytes(b"raw_orig")
        rec_edit = {
            "id": "short_orig_edit_abc123",
            "status": "ready",
            "headline": "Edit Short",
            "title": "Edit Short",
            "candidate": {"source": "youtube", "key": "k2"},
        }
        store.put("videos", "short_orig_edit_abc123", rec_edit)
        with patch("studio.worker.render.render", side_effect=mock_render), patch("studio.worker.store", store):
            worker.work("render", "short_orig_edit_abc123")
        self.assertEqual(captured_kwargs["video"], orig_raw)

        # Path 6C: Explicit record["raw_video"] relative or absolute path
        custom_raw = self.dir_path / "custom" / "custom_raw.mp4"
        custom_raw.parent.mkdir(parents=True, exist_ok=True)
        custom_raw.write_bytes(b"custom_raw")
        rec_custom = {
            "id": "short_custom",
            "status": "ready",
            "headline": "Custom",
            "title": "Custom",
            "raw_video": str(custom_raw),
            "candidate": {"source": "youtube", "key": "k3"},
        }
        store.put("videos", "short_custom", rec_custom)
        with patch("studio.worker.render.render", side_effect=mock_render), patch("studio.worker.store", store):
            worker.work("render", "short_custom")
        self.assertEqual(captured_kwargs["video"], custom_raw.resolve())

    # -------------------------------------------------------------------------
    # 7. Server preflight rejects missing source before launching worker
    # -------------------------------------------------------------------------
    def test_server_preflight_rejects_missing_source_before_worker(self):
        """7. Server preflight rejects missing raw source before launching worker subprocess."""
        record = {
            "id": "vid_preflight_missing",
            "status": "ready",
            "headline": "Preflight Missing",
            "title": "Preflight Missing",
            "candidate": {"source": "youtube", "key": "src_key"},
        }
        store.put("videos", "vid_preflight_missing", record)

        with patch("studio.server.store", store):
            with self.assertRaises(ValueError) as ctx:
                server.start_job("render", "vid_preflight_missing")

            self.assertIn("original raw source video not found on disk", str(ctx.exception))
            jobs = store.records("jobs")
            self.assertEqual(len(jobs), 0, "No job should be queued when preflight fails")

    # -------------------------------------------------------------------------
    # 8. Server preflight accepts valid source and schedules job
    # -------------------------------------------------------------------------
    def test_server_preflight_accepts_valid_source_and_schedules_job(self):
        """8. Server preflight accepts a valid source and preserves successful job scheduling."""
        raw_file = self.work_dir / "vid_preflight_ok_raw.mp4"
        raw_file.write_bytes(b"valid_raw")

        record = {
            "id": "vid_preflight_ok",
            "status": "ready",
            "headline": "Preflight OK",
            "title": "Preflight OK",
            "candidate": {"source": "youtube", "key": "src_key"},
        }
        store.put("videos", "vid_preflight_ok", record)

        with patch("studio.server.store", store), \
             patch("studio.server.subprocess.Popen") as mock_popen, \
             patch("studio.server.threading.Thread"):
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            res = server.start_job("render", "vid_preflight_ok")
            self.assertIn(res["status"], ("pending", "immediate"))
            self.assertIn("id", res)

            job = store.get("jobs", res["id"])
            self.assertIsNotNone(job)
            self.assertEqual(job["action"], "render")
            self.assertEqual(job["key"], "vid_preflight_ok")

    # -------------------------------------------------------------------------
    # 9. Rejected requests do not create orphaned or falsely successful jobs
    # -------------------------------------------------------------------------
    def test_rejected_requests_do_not_create_orphaned_jobs(self):
        """9. Rejected requests do not create orphaned or falsely successful jobs."""
        with patch("studio.server.store", store):
            # Test empty key
            with self.assertRaises(ValueError):
                server.start_job("render", "")

            # Test non-existent video
            with self.assertRaises(ValueError):
                server.start_job("render", "non_existent_key")

            jobs = store.records("jobs")
            self.assertEqual(len(jobs), 0, "Rejected render requests must never create orphaned jobs")

    # -------------------------------------------------------------------------
    # 10. Library filter works for All Modes, Clean Footage, and Card Mode
    # -------------------------------------------------------------------------
    def test_library_filter_contract_all_clean_card(self):
        """10. Node.js contract test: matchesLibraryMode filters correctly for all modes."""
        node_script = """
        const { matchesLibraryMode } = require('./studio/web/app.js');
        const assert = require('assert');

        const cleanVid = { id: 'v1', presentation_mode: 'footage_clean' };
        const cardVid = { id: 'v2', presentation_mode: 'card' };

        // All Modes
        assert.strictEqual(matchesLibraryMode(cleanVid, 'all'), true, 'cleanVid in all');
        assert.strictEqual(matchesLibraryMode(cardVid, 'all'), true, 'cardVid in all');

        // Clean Footage filter
        assert.strictEqual(matchesLibraryMode(cleanVid, 'footage_clean'), true, 'cleanVid in footage_clean');
        assert.strictEqual(matchesLibraryMode(cardVid, 'footage_clean'), false, 'cardVid not in footage_clean');

        // Card Mode filter
        assert.strictEqual(matchesLibraryMode(cardVid, 'card'), true, 'cardVid in card');
        assert.strictEqual(matchesLibraryMode(cleanVid, 'card'), false, 'cleanVid not in card');

        console.log('OK');
        """
        proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node script failed: {proc.stderr}")
        self.assertIn("OK", proc.stdout)

    # -------------------------------------------------------------------------
    # 11. Legacy records without a presentation mode appear in Card Mode filter
    # -------------------------------------------------------------------------
    def test_legacy_records_appear_in_card_mode_filter(self):
        """11. Node.js contract test: Legacy records with absent/empty presentation mode match Card Mode."""
        node_script = """
        const { matchesLibraryMode } = require('./studio/web/app.js');
        const assert = require('assert');

        const legacyAbsent = { id: 'v_legacy_1' };
        const legacyEmpty = { id: 'v_legacy_2', presentation_mode: '' };
        const legacyNull = { id: 'v_legacy_3', presentation_mode: null };

        // Should match Card Mode
        assert.strictEqual(matchesLibraryMode(legacyAbsent, 'card'), true, 'legacyAbsent in card');
        assert.strictEqual(matchesLibraryMode(legacyEmpty, 'card'), true, 'legacyEmpty in card');
        assert.strictEqual(matchesLibraryMode(legacyNull, 'card'), true, 'legacyNull in card');

        // Should NOT match Clean Footage
        assert.strictEqual(matchesLibraryMode(legacyAbsent, 'footage_clean'), false, 'legacyAbsent not clean');
        assert.strictEqual(matchesLibraryMode(legacyEmpty, 'footage_clean'), false, 'legacyEmpty not clean');
        assert.strictEqual(matchesLibraryMode(legacyNull, 'footage_clean'), false, 'legacyNull not clean');

        // Should match All Modes
        assert.strictEqual(matchesLibraryMode(legacyAbsent, 'all'), true, 'legacyAbsent in all');
        assert.strictEqual(matchesLibraryMode(legacyEmpty, 'all'), true, 'legacyEmpty in all');

        console.log('OK');
        """
        proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node script failed: {proc.stderr}")
        self.assertIn("OK", proc.stdout)

    # -------------------------------------------------------------------------
    # 12. Resetting filters restores defaults and preserves existing filters
    # -------------------------------------------------------------------------
    def test_reset_filters_restores_defaults(self):
        """12. Node.js contract test: resetLibraryFilters resets libraryFilterMode to 'all'."""
        node_script = """
        const app = require('./studio/web/app.js');
        const assert = require('assert');

        // Mock document elements for resetLibraryFilters and renderLibraryGrid
        global.document = {
          getElementById: (id) => ({
            value: 'custom_value',
            style: {},
            innerHTML: '',
            addEventListener: () => {},
            querySelector: () => null,
            querySelectorAll: () => [],
          })
        };

        app.setLibraryFilterMode('footage_clean');
        assert.strictEqual(app.getLibraryFilterMode(), 'footage_clean', 'setup mode filter');

        app.resetLibraryFilters();
        assert.strictEqual(app.getLibraryFilterMode(), 'all', 'reset restores all');

        console.log('OK');
        """
        proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node script failed: {proc.stderr}")
        self.assertIn("OK", proc.stdout)

    # -------------------------------------------------------------------------
    # 13. Modal disables re-rendering when raw footage is missing
    # -------------------------------------------------------------------------
    def test_modal_disables_rerender_when_raw_is_missing(self):
        """13. Verify app.js contains the markup guard and disabled click prevention."""
        app_js = Path("studio/web/app.js").read_text(encoding="utf-8")

        # 1. Markup guard in openVideoModal: disabled attribute when raw_exists is false
        self.assertIn("${v.raw_exists === false ?", app_js)
        self.assertIn("Cannot re-render: Original raw source video is missing from disk", app_js)
        self.assertIn("disabled style=\"opacity: 0.5; cursor: not-allowed;\"", app_js)

        # 2. Defensive guard in reRenderDraft: checks button disabled state
        self.assertIn("if (rerenderBtn && rerenderBtn.disabled)", app_js)


if __name__ == "__main__":
    unittest.main()
