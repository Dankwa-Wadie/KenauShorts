"""
tests/test_milestone8_2.py — Dedicated test suite for Stage 8 Milestone 8.2
Studio UI & Workflow Modernization for Clean Footage & Footage Fit.
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
from unittest.mock import MagicMock, call, patch

from core import render, style_presets
from studio import server, store, worker


def _create_synthetic_video(path: Path, width: int = 1280, height: int = 720, duration: float = 1.0) -> Path:
    """Helper to generate a lightweight real mp4 file with ffmpeg for real rendering tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=green:s={width}x{height}:d={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ]
    subprocess.run(cmd, check=True)
    return path


class Milestone82FootageFitAndUITests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.work_dir = self.dir_path / "work"
        self.out_dir = self.dir_path / "out"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        # Write minimal config.example.json in temp dir for worker
        sample_config = {
            "layout": {"canvas_width": 1080, "canvas_height": 1920},
            "clips": {"max_seconds": 30},
        }
        (self.dir_path / "config.example.json").write_text(json.dumps(sample_config), encoding="utf-8")

        # Patch store root and DB to temp directory
        self.orig_store_root = store.ROOT
        self.orig_store_db = store.DB
        store.ROOT = self.dir_path
        store.DB = self.dir_path / "studio.sqlite3"
        with store.connect():
            pass

    def tearDown(self):
        store.ROOT = self.orig_store_root
        store.DB = self.orig_store_db
        self.temp_dir.cleanup()

    def test_footage_fit_constants_and_validation(self):
        """1. Validate footage_fit constants, schema, and error handling in core/render.py."""
        self.assertEqual(render.FOOTAGE_FIT_AUTO, "auto")
        self.assertEqual(render.FOOTAGE_FIT_COVER, "cover")
        self.assertEqual(render.FOOTAGE_FIT_CONTAIN, "contain")
        self.assertEqual(
            render.VALID_FOOTAGE_FIT,
            {"auto", "cover", "contain"},
        )
        self.assertEqual(render.DEFAULT_FOOTAGE_FIT, "auto")

        fake_vid = self.work_dir / "fake.mp4"
        fake_out = self.out_dir / "fake_out.mp4"

        # Invalid footage_fit string
        with self.assertRaises(ValueError) as ctx:
            render.render(fake_vid, "HEADLINE", fake_out, footage_fit="invalid_fit")
        self.assertIn("Invalid footage_fit: 'invalid_fit'", str(ctx.exception))

        # Invalid footage_fit type
        with self.assertRaises(ValueError) as ctx:
            render.render(fake_vid, "HEADLINE", fake_out, footage_fit=123)
        self.assertIn("expected string", str(ctx.exception))

        # Reserved presentation mode still rejected
        with self.assertRaises(NotImplementedError) as ctx:
            render.render(fake_vid, "HEADLINE", fake_out, presentation_mode="footage_caption")
        self.assertIn("reserved for a future milestone", str(ctx.exception))

    def test_render_result_records_footage_fit(self):
        """2. render() returns RenderResult with accurate footage_fit."""
        source_vid = self.work_dir / "source.mp4"
        _create_synthetic_video(source_vid, width=640, height=360, duration=0.5)
        out_mp4 = self.out_dir / "out_clean_cover.mp4"
        poster_png = self.out_dir / "out_clean_cover.png"

        result = render.render(
            video=source_vid,
            headline="TEST",
            out=out_mp4,
            poster=poster_png,
            presentation_mode="footage_clean",
            footage_fit="cover",
        )
        self.assertEqual(result.presentation_mode, "footage_clean")
        self.assertEqual(result.footage_fit, "cover")
        self.assertTrue(out_mp4.is_file())

    def test_store_draft_and_enrichment_preserve_footage_fit(self):
        """3. store.draft() and store.enrich_video_record() properly handle footage_fit."""
        vid_path = self.out_dir / "test_vid.mp4"
        poster_path = self.out_dir / "test_poster.png"
        vid_path.touch()
        poster_path.touch()

        # Explicit footage_fit
        rec1 = store.draft(
            video_stem="rec1",
            video_path=vid_path,
            poster_path=poster_path,
            headline="Rec 1 Headline",
            title="Rec 1 Title",
            description="Rec 1 Desc",
            config={},
            presentation_mode="footage_clean",
            footage_fit="contain",
        )
        self.assertEqual(rec1["presentation_mode"], "footage_clean")
        self.assertEqual(rec1["footage_fit"], "contain")

        # Default footage_fit fallback to "auto"
        rec2 = store.draft(
            video_stem="rec2",
            video_path=vid_path,
            poster_path=poster_path,
            headline="Rec 2 Headline",
            title="Rec 2 Title",
            description="Rec 2 Desc",
            config={},
            presentation_mode="card",
        )
        self.assertEqual(rec2["presentation_mode"], "card")
        self.assertEqual(rec2["footage_fit"], "auto")

        # Raw record enrichment defaults
        raw_rec = {"id": "raw_1", "status": "ready"}
        enriched = store.enrich_video_record(raw_rec)
        self.assertEqual(enriched["presentation_mode"], "card")
        self.assertEqual(enriched["footage_fit"], "auto")

    def test_server_api_video_mutation_validates_fit_and_mode(self):
        """4. POST /api/video validates presentation_mode and footage_fit, rejecting invalid values."""
        test_record = {
            "id": "vid_api_test",
            "status": "ready",
            "title": "Title",
            "description": "Desc",
            "headline": "Headline",
            "style_preset": "classic_blue",
            "presentation_mode": "card",
            "footage_fit": "auto",
            "config": {"layout": {"presentation_mode": "card", "footage_fit": "auto"}},
        }

        handler = server.StudioHandler.__new__(server.StudioHandler)

        with patch.object(store, "get", return_value=test_record), \
             patch.object(store, "put") as mock_put:

            # Valid mutation to footage_clean with cover fit
            res = handler.mutate("/api/video", {"id": "vid_api_test", "presentation_mode": "footage_clean", "footage_fit": "cover"})
            self.assertEqual(res["presentation_mode"], "footage_clean")
            self.assertEqual(res["footage_fit"], "cover")
            self.assertEqual(res["config"]["layout"]["presentation_mode"], "footage_clean")
            self.assertEqual(res["config"]["layout"]["footage_fit"], "cover")

            # Valid mutation to contain fit
            res = handler.mutate("/api/video", {"id": "vid_api_test", "footage_fit": "contain"})
            self.assertEqual(res["footage_fit"], "contain")
            self.assertEqual(res["config"]["layout"]["footage_fit"], "contain")

            # Empty string normalizes to auto
            res = handler.mutate("/api/video", {"id": "vid_api_test", "footage_fit": ""})
            self.assertEqual(res["footage_fit"], "auto")

            # None normalizes to auto
            res = handler.mutate("/api/video", {"id": "vid_api_test", "footage_fit": None})
            self.assertEqual(res["footage_fit"], "auto")

            # Invalid footage_fit rejected with ValueError
            with self.assertRaises(ValueError) as ctx:
                handler.mutate("/api/video", {"id": "vid_api_test", "footage_fit": "stretch"})
            self.assertIn("Invalid footage_fit", str(ctx.exception))

            # Non-string footage_fit rejected with ValueError
            with self.assertRaises(ValueError) as ctx:
                handler.mutate("/api/video", {"id": "vid_api_test", "footage_fit": 9999})
            self.assertIn("expected string", str(ctx.exception))

            # Reserved presentation_mode footage_caption rejected with ValueError
            with self.assertRaises(ValueError) as ctx:
                handler.mutate("/api/video", {"id": "vid_api_test", "presentation_mode": "footage_caption"})
            self.assertIn("reserved", str(ctx.exception))

            # Invalid presentation_mode rejected with ValueError
            with self.assertRaises(ValueError) as ctx:
                handler.mutate("/api/video", {"id": "vid_api_test", "presentation_mode": "invalid_mode"})
            self.assertIn("Invalid presentation_mode", str(ctx.exception))

    def test_worker_re_rendering_resolves_and_preserves_fit(self):
        """5. studio.worker.work('render', key) passes presentation_mode and footage_fit to render()."""
        raw_clip = self.dir_path / "work" / "vid_key_raw.mp4"
        _create_synthetic_video(raw_clip, width=640, height=360, duration=0.5)

        # Create record in store with clean mode and contain fit
        record = {
            "id": "vid_key",
            "status": "ready",
            "headline": "Worker Re-render Test",
            "title": "Worker Test Title",
            "description": "Worker Test Desc",
            "presentation_mode": "footage_clean",
            "footage_fit": "contain",
            "candidate": {"source": "youtube", "key": "test_src"},
            "config": {
                "layout": {
                    "presentation_mode": "footage_clean",
                    "footage_fit": "contain",
                }
            },
        }
        store.put("videos", "vid_key", record)

        captured_kwargs = {}

        def mock_render(**kwargs):
            nonlocal captured_kwargs
            captured_kwargs = kwargs
            # Create the out files
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
            worker.work("render", "vid_key")

        self.assertEqual(captured_kwargs.get("presentation_mode"), "footage_clean")
        self.assertEqual(captured_kwargs.get("footage_fit"), "contain")

        updated_rec = store.get("videos", "vid_key")
        self.assertEqual(updated_rec["presentation_mode"], "footage_clean")
        self.assertEqual(updated_rec["footage_fit"], "contain")
        self.assertEqual(updated_rec["status"], "ready")

    def test_frontend_guidance_text_generation(self):
        """7. Verify preview guidance logic for all modes and fit options via Node.js."""
        node_script = """
        const { getPreviewGuidanceText } = require('./studio/web/app.js');
        const assert = require('assert');

        const cleanCover = getPreviewGuidanceText('footage_clean', 'cover');
        assert(cleanCover.includes('scales and center-crops'), 'clean cover text mismatch');
        assert(cleanCover.includes('cropped on the left and right'), 'clean cover crop warning mismatch');

        const cleanContain = getPreviewGuidanceText('footage_clean', 'contain');
        assert(cleanContain.includes('black letterbox bars'), 'clean contain text mismatch');
        assert(cleanContain.includes('No content is cropped'), 'clean contain no-crop guarantee mismatch');

        const cleanAuto = getPreviewGuidanceText('footage_clean', 'auto');
        assert(cleanAuto.includes('Adaptive fit'), 'clean auto text mismatch');
        assert(cleanAuto.includes('letterboxed with black padding'), 'clean auto letterbox mismatch');

        const cardText = getPreviewGuidanceText('card', 'auto');
        assert(cardText.includes('classic 9:16 card'), 'card text mismatch');
        assert(cardText.includes('headline'), 'card headline mismatch');

        console.log('OK');
        """
        proc = subprocess.run(
            ["node", "-e", node_script],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, f"Node script failed: {proc.stderr}")
        self.assertIn("OK", proc.stdout)

    def test_frontend_ui_elements_and_css_contract(self):
        """8. Verify frontend HTML/CSS contract: mode selectors, guidance container, and badges exist."""
        app_js = Path("studio/web/app.js").read_text(encoding="utf-8")
        style_css = Path("studio/web/style.css").read_text(encoding="utf-8")

        # HTML elements in app.js
        self.assertIn('id="edit-presentation-mode"', app_js)
        self.assertIn('id="edit-footage-fit"', app_js)
        self.assertIn('id="footage-fit-group"', app_js)
        self.assertIn('id="preview-mode-banner"', app_js)
        self.assertIn('id="btn-rerender-draft"', app_js)
        self.assertIn('badge-mode-clean', app_js)
        self.assertIn('badge-mode-card', app_js)

        # CSS classes in style.css
        self.assertIn('.badge-mode-clean', style_css)
        self.assertIn('.badge-mode-card', style_css)
        self.assertIn('.preview-mode-guidance', style_css)
        self.assertIn('.preview-mode-guidance.clean', style_css)
        self.assertIn('.preview-mode-guidance.card', style_css)
        self.assertIn('.preview-mode-guidance-title', style_css)
        self.assertIn('.preview-mode-guidance-text', style_css)


if __name__ == "__main__":
    unittest.main()
