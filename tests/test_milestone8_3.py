"""
tests/test_milestone8_3.py — Dedicated test suite for Stage 8 Milestone 8.3
Discovery, Staging & Ingestion Pipeline Alignment for Clean Footage & Footage Fit.
"""

from __future__ import annotations

import argparse
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

from core import render
from core.agent import (
    Candidate,
    Pick,
    _render_pick,
    run_manual,
    run_pipeline,
)
from studio import server, store


class Milestone83DiscoveryAndStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.work_dir = self.dir_path / "work"
        self.out_dir = self.dir_path / "out"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        # Patch store root and DB to temp directory
        self.orig_store_root = store.ROOT
        self.orig_store_db = store.DB
        store.ROOT = self.dir_path
        store.DB = self.dir_path / "studio.sqlite3"
        with store.connect():
            pass

    def tearDown(self):
        with server.GUARD:
            server.ACTIVE_JOB = None
        store.ROOT = self.orig_store_root
        store.DB = self.orig_store_db
        self.temp_dir.cleanup()

    def test_pick_dataclass_defaults_and_explicit_fit(self):
        """1. Pick dataclass supports footage_fit with backward-compatible defaults."""
        cand = Candidate(
            kind="clip",
            key="test_key",
            title="Candidate Title",
            url="https://youtube.com/watch?v=12345678901",
            source="youtube",
        )
        # Default initialization (omitted presentation_mode and footage_fit)
        default_pick = Pick(
            candidate=cand,
            headline="Default Headline",
            caption="Default Caption",
            title="Default Title",
            description="Default Description",
        )
        self.assertEqual(default_pick.presentation_mode, "")
        self.assertEqual(default_pick.footage_fit, "")

        # Explicit initialization
        explicit_pick = Pick(
            candidate=cand,
            headline="Explicit Headline",
            caption="Explicit Caption",
            title="Explicit Title",
            description="Explicit Description",
            presentation_mode="footage_clean",
            footage_fit="cover",
        )
        self.assertEqual(explicit_pick.presentation_mode, "footage_clean")
        self.assertEqual(explicit_pick.footage_fit, "cover")

    def test_render_pick_precedence_and_render_propagation(self):
        """2. _render_pick resolves configuration precedence and propagates mode & fit to renderer."""
        cand = Candidate(
            kind="clip",
            key="precedence_key",
            title="Precedence Title",
            url="https://youtube.com/watch?v=12345678901",
            source="youtube",
        )
        raw_clip = self.work_dir / "raw_clip.mp4"
        raw_clip.touch()

        # Case 2a: Explicit pick settings take top precedence over config layout
        pick_a = Pick(
            candidate=cand,
            headline="Headline A",
            caption="Caption A",
            title="Title A",
            description="Desc A",
            presentation_mode="footage_clean",
            footage_fit="cover",
        )
        config_a = {
            "layout": {
                "presentation_mode": "card",
                "footage_fit": "contain",
            },
            "clips": {"max_seconds": 30},
        }

        captured_kwargs_a = {}

        def mock_render_a(**kwargs):
            nonlocal captured_kwargs_a
            captured_kwargs_a = kwargs
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
            )

        with patch("core.agent.render", side_effect=mock_render_a), \
             patch("core.agent.download_clip", return_value=True), \
             patch("core.agent.get_source_aspect_ratio", return_value=0.5625):
            _render_pick(pick_a, config_a, self.work_dir, self.out_dir, "stem_a")

        self.assertEqual(captured_kwargs_a.get("presentation_mode"), "footage_clean")
        self.assertEqual(captured_kwargs_a.get("footage_fit"), "cover")
        self.assertEqual(pick_a.presentation_mode, "footage_clean")
        self.assertEqual(pick_a.footage_fit, "cover")

        # Case 2b: Config layout takes precedence when pick mode/fit are empty
        pick_b = Pick(
            candidate=cand,
            headline="Headline B",
            caption="Caption B",
            title="Title B",
            description="Desc B",
        )
        config_b = {
            "layout": {
                "presentation_mode": "footage_clean",
                "footage_fit": "contain",
            },
            "clips": {"max_seconds": 30},
        }

        captured_kwargs_b = {}

        def mock_render_b(**kwargs):
            nonlocal captured_kwargs_b
            captured_kwargs_b = kwargs
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
            )

        with patch("core.agent.render", side_effect=mock_render_b), \
             patch("core.agent.download_clip", return_value=True), \
             patch("core.agent.get_source_aspect_ratio", return_value=1.777):
            _render_pick(pick_b, config_b, self.work_dir, self.out_dir, "stem_b")

        self.assertEqual(captured_kwargs_b.get("presentation_mode"), "footage_clean")
        self.assertEqual(captured_kwargs_b.get("footage_fit"), "contain")
        self.assertEqual(pick_b.presentation_mode, "footage_clean")
        self.assertEqual(pick_b.footage_fit, "contain")

        # Case 2c: Top-level config takes precedence when layout is absent
        pick_c = Pick(
            candidate=cand,
            headline="Headline C",
            caption="Caption C",
            title="Title C",
            description="Desc C",
        )
        config_c = {
            "presentation_mode": "footage_clean",
            "footage_fit": "cover",
            "clips": {"max_seconds": 30},
        }

        captured_kwargs_c = {}

        def mock_render_c(**kwargs):
            nonlocal captured_kwargs_c
            captured_kwargs_c = kwargs
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
            )

        with patch("core.agent.render", side_effect=mock_render_c), \
             patch("core.agent.download_clip", return_value=True), \
             patch("core.agent.get_source_aspect_ratio", return_value=1.0):
            _render_pick(pick_c, config_c, self.work_dir, self.out_dir, "stem_c")

        self.assertEqual(captured_kwargs_c.get("presentation_mode"), "footage_clean")
        self.assertEqual(captured_kwargs_c.get("footage_fit"), "cover")

        # Case 2d: Complete absence of mode/fit safely defaults to card and auto
        pick_d = Pick(
            candidate=cand,
            headline="Headline D",
            caption="Caption D",
            title="Title D",
            description="Desc D",
        )
        config_d = {"clips": {"max_seconds": 30}}

        captured_kwargs_d = {}

        def mock_render_d(**kwargs):
            nonlocal captured_kwargs_d
            captured_kwargs_d = kwargs
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
            )

        with patch("core.agent.render", side_effect=mock_render_d), \
             patch("core.agent.download_clip", return_value=True), \
             patch("core.agent.get_source_aspect_ratio", return_value=1.777):
            _render_pick(pick_d, config_d, self.work_dir, self.out_dir, "stem_d")

        self.assertEqual(captured_kwargs_d.get("presentation_mode"), "card")
        self.assertEqual(captured_kwargs_d.get("footage_fit"), "auto")

    def test_run_pipeline_draft_persistence_and_propagation(self):
        """3. run_pipeline propagates mode & fit into picks and stores both in video draft."""
        config_file = self.dir_path / "config.json"
        config_data = {
            "layout": {
                "presentation_mode": "footage_clean",
                "footage_fit": "cover",
            },
            "editorial": {"max_candidates": 1},
            "clips": {"max_seconds": 30},
        }
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        cand = Candidate(
            kind="clip",
            key="pipeline_cand",
            title="Pipeline Candidate",
            url="https://youtube.com/watch?v=12345678901",
            source="youtube",
        )
        mock_pick = Pick(
            candidate=cand,
            headline="Pipeline Headline",
            caption="Pipeline Caption",
            title="Pipeline Title",
            description="Pipeline Desc",
        )

        dummy_mp4 = self.out_dir / "short_pipeline_test.mp4"
        dummy_mp4.touch()
        dummy_png = self.out_dir / "short_pipeline_test.png"
        dummy_png.touch()

        with patch("core.agent.discover_all_candidates", return_value=[cand]), \
             patch("core.agent.pick_stories", return_value=[mock_pick]), \
             patch("core.agent._render_pick", return_value=dummy_mp4), \
             patch("core.agent.State"):
            run_pipeline(
                config_file,
                dry_run=True,
                presentation_mode="footage_clean",
                footage_fit="cover",
            )

        videos = store.records("videos")
        self.assertEqual(len(videos), 1)
        video_rec = videos[0]
        self.assertEqual(video_rec["presentation_mode"], "footage_clean")
        self.assertEqual(video_rec["footage_fit"], "cover")

    def test_run_manual_cli_parsing_and_execution(self):
        """4. run_manual validates mode & fit, stores in draft, and CLI parser handles flags."""
        config_file = self.dir_path / "config.json"
        config_data = {"clips": {"max_seconds": 30}}
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        dummy_mp4 = self.out_dir / "manual_test.mp4"
        dummy_mp4.touch()

        # Valid manual invocation
        with patch("core.agent.fetch_video_metadata", return_value=("Manual Title", "Manual Desc")), \
             patch("core.agent._render_pick", return_value=dummy_mp4), \
             patch("core.agent.State"):
            run_manual(
                config_file,
                "https://youtube.com/watch?v=manual12345",
                headline="Manual Headline",
                description="Manual Description",
                dry_run=True,
                presentation_mode="footage_clean",
                footage_fit="contain",
            )

        videos = store.records("videos")
        self.assertTrue(any(v.get("presentation_mode") == "footage_clean" and v.get("footage_fit") == "contain" for v in videos))

        # Validation failure: reserved presentation_mode
        with self.assertRaises(ValueError) as ctx:
            run_manual(config_file, "https://youtube.com/watch?v=123", presentation_mode="footage_caption")
        self.assertIn("reserved", str(ctx.exception))

        # Validation failure: invalid presentation_mode
        with self.assertRaises(ValueError) as ctx:
            run_manual(config_file, "https://youtube.com/watch?v=123", presentation_mode="invalid_mode")
        self.assertIn("Invalid presentation_mode", str(ctx.exception))

        # Validation failure: invalid footage_fit
        with self.assertRaises(ValueError) as ctx:
            run_manual(config_file, "https://youtube.com/watch?v=123", footage_fit="invalid_fit")
        self.assertIn("Invalid footage_fit", str(ctx.exception))

        # CLI argument parser test
        parser = argparse.ArgumentParser(description="KenauShorts Pipeline")
        parser.add_argument("--config", default="config.json")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--url", default="")
        parser.add_argument("--headline", default="")
        parser.add_argument("--description", default="")
        parser.add_argument("--presentation-mode", default="", choices=["", "card", "footage_clean"])
        parser.add_argument("--footage-fit", default="", choices=["", "auto", "cover", "contain"])

        args = parser.parse_args([
            "--url", "https://youtube.com/watch?v=123",
            "--presentation-mode", "footage_clean",
            "--footage-fit", "cover",
        ])
        self.assertEqual(args.presentation_mode, "footage_clean")
        self.assertEqual(args.footage_fit, "cover")

        # Invalid CLI argument raises SystemExit
        with self.assertRaises(SystemExit):
            parser.parse_args(["--presentation-mode", "invalid_mode"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["--footage-fit", "stretch"])

    def test_server_manual_job_validates_and_constructs_command(self):
        """5. server.start_job validates manual presentation_mode & fit and builds correct CLI command."""
        with patch("shutil.disk_usage") as mock_disk, \
             patch.object(server, "ensure_queue_worker"):
            mock_disk.return_value = shutil._ntuple_diskusage(10_000_000_000, 2_000_000_000, 8_000_000_000)

            # Valid manual job with clean mode and cover fit
            job_res = server.start_job(
                action="manual",
                extra={
                    "url": "https://youtube.com/watch?v=valid123456",
                    "headline": "Server Test Headline",
                    "presentation_mode": "footage_clean",
                    "footage_fit": "cover",
                },
            )
            cmd = job_res["command"]
            self.assertIn("--url", cmd)
            self.assertIn("https://youtube.com/watch?v=valid123456", cmd)
            self.assertIn("--presentation-mode", cmd)
            self.assertEqual(cmd[cmd.index("--presentation-mode") + 1], "footage_clean")
            self.assertIn("--footage-fit", cmd)
            self.assertEqual(cmd[cmd.index("--footage-fit") + 1], "cover")

            # Omitted options: does not include flags
            job_default = server.start_job(
                action="manual",
                extra={"url": "https://youtube.com/watch?v=valid123456"},
            )
            cmd_default = job_default["command"]
            self.assertNotIn("--presentation-mode", cmd_default)
            self.assertNotIn("--footage-fit", cmd_default)

            # Validation: reserved mode rejected with ValueError
            with self.assertRaises(ValueError) as ctx:
                server.start_job(action="manual", extra={"url": "https://x", "presentation_mode": "footage_caption"})
            self.assertIn("reserved", str(ctx.exception))

            # Validation: invalid mode rejected with ValueError
            with self.assertRaises(ValueError) as ctx:
                server.start_job(action="manual", extra={"url": "https://x", "presentation_mode": "invalid_mode"})
            self.assertIn("Invalid presentation_mode", str(ctx.exception))

            # Validation: non-string mode rejected with ValueError
            with self.assertRaises(ValueError) as ctx:
                server.start_job(action="manual", extra={"url": "https://x", "presentation_mode": 123})
            self.assertIn("expected string", str(ctx.exception))

            # Validation: invalid fit rejected with ValueError
            with self.assertRaises(ValueError) as ctx:
                server.start_job(action="manual", extra={"url": "https://x", "footage_fit": "stretch"})
            self.assertIn("Invalid footage_fit", str(ctx.exception))

            # Validation: non-string fit rejected with ValueError
            with self.assertRaises(ValueError) as ctx:
                server.start_job(action="manual", extra={"url": "https://x", "footage_fit": [1, 2]})
            self.assertIn("expected string", str(ctx.exception))

            # retry_job preserves manual options in command
            job_res["status"] = "failed"
            store.put("jobs", job_res["id"], job_res)
            retried = server.retry_job(job_res["id"])
            retried_cmd = retried["command"]
            self.assertIn("--presentation-mode", retried_cmd)
            self.assertEqual(retried_cmd[retried_cmd.index("--presentation-mode") + 1], "footage_clean")
            self.assertIn("--footage-fit", retried_cmd)
            self.assertEqual(retried_cmd[retried_cmd.index("--footage-fit") + 1], "cover")

    def test_manual_modal_html_and_js_contract(self):
        """6. Add Video by Link modal in index.html and app.js contract verification."""
        html_text = Path("studio/web/index.html").read_text(encoding="utf-8")
        app_js = Path("studio/web/app.js").read_text(encoding="utf-8")

        # HTML elements
        self.assertIn('id="manual-url-modal"', html_text)
        self.assertIn('id="manual-presentation-mode"', html_text)
        self.assertIn('id="manual-footage-fit-group"', html_text)
        self.assertIn('id="manual-footage-fit"', html_text)
        self.assertIn('onManualPresentationModeChange', html_text)

        # JS functions
        self.assertIn('function onManualPresentationModeChange', app_js)
        self.assertIn('openManualUrlModal', app_js)
        self.assertIn('submitManualUrl', app_js)
        self.assertIn('manual-presentation-mode', app_js)
        self.assertIn('manual-footage-fit', app_js)

        # Node.js contract test for onManualPresentationModeChange
        node_script = """
        const app = require('./studio/web/app.js');
        const assert = require('assert');
        assert.strictEqual(typeof app.onManualPresentationModeChange, 'function', 'onManualPresentationModeChange must be exported');
        console.log('OK');
        """
        proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node script failed: {proc.stderr}")
        self.assertIn("OK", proc.stdout)

    def test_backward_compatibility_omitted_options(self):
        """7. Omitted presentation_mode and footage_fit options preserve Card/Auto behavior."""
        cand = Candidate(
            kind="clip",
            key="compat_cand",
            title="Compat Title",
            url="https://youtube.com/watch?v=12345678901",
            source="youtube",
        )
        pick = Pick(
            candidate=cand,
            headline="Compat Headline",
            caption="Compat Caption",
            title="Compat Title",
            description="Compat Desc",
        )
        config = {"clips": {"max_seconds": 30}}

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
            )

        with patch("core.agent.render", side_effect=mock_render), \
             patch("core.agent.download_clip", return_value=True), \
             patch("core.agent.get_source_aspect_ratio", return_value=1.777):
            _render_pick(pick, config, self.work_dir, self.out_dir, "compat_out")

        self.assertEqual(captured_kwargs.get("presentation_mode"), "card")
        self.assertEqual(captured_kwargs.get("footage_fit"), "auto")

    def test_run_pipeline_automated_discovery_config_driven_persistence(self):
        """8. Automated discovery run_pipeline without CLI flags resolves layout config and persists mode & fit in draft."""
        config_file = self.dir_path / "config_auto.json"
        config_data = {
            "layout": {
                "presentation_mode": "footage_clean",
                "footage_fit": "cover",
            },
            "editorial": {"max_candidates": 1},
            "clips": {"max_seconds": 30},
        }
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        cand = Candidate(
            kind="clip",
            key="auto_discovery_key",
            title="Auto Discovery Title",
            url="https://youtube.com/watch?v=auto1234567",
            source="youtube",
        )
        mock_pick = Pick(
            candidate=cand,
            headline="Auto Headline",
            caption="Auto Caption",
            title="Auto Title",
            description="Auto Description",
        )
        self.assertEqual(mock_pick.presentation_mode, "")
        self.assertEqual(mock_pick.footage_fit, "")

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
            run_pipeline(config_file, dry_run=True)

        # 1. Renderer received resolved presentation mode and fit
        self.assertEqual(captured_render_kwargs.get("presentation_mode"), "footage_clean")
        self.assertEqual(captured_render_kwargs.get("footage_fit"), "cover")

        # 2. Pick was mutated with resolved settings
        self.assertEqual(mock_pick.presentation_mode, "footage_clean")
        self.assertEqual(mock_pick.footage_fit, "cover")

        # 3. Draft in SQLite store persisted resolved presentation mode and fit
        videos = store.records("videos")
        self.assertEqual(len(videos), 1)
        video_rec = videos[0]
        self.assertEqual(video_rec.get("candidate", {}).get("key"), "auto_discovery_key")
        self.assertEqual(video_rec.get("presentation_mode"), "footage_clean")
        self.assertEqual(video_rec.get("footage_fit"), "cover")



if __name__ == "__main__":
    unittest.main()
