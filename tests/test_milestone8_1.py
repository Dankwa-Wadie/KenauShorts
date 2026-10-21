"""
tests/test_milestone8_1.py — Dedicated test suite for Stage 8 Milestone 8.1
Footage-First and Clean-Footage Rendering Modes.
"""

from __future__ import annotations

import copy
import dataclasses
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from core import render, style_presets
from core.agent import Candidate, Pick, _render_pick
from studio import server, store, worker


def _create_synthetic_video(path: Path, width: int = 1280, height: int = 720, duration: float = 1.0) -> Path:
    """Helper to generate a lightweight real mp4 file with ffmpeg for real rendering tests."""
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


class Milestone81RenderingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.work_dir = self.dir_path / "work"
        self.out_dir = self.dir_path / "out"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_presentation_mode_constants_and_validation(self):
        """1. Validate mode constants, schema, and error handling."""
        self.assertEqual(render.PRESENTATION_MODE_CARD, "card")
        self.assertEqual(render.PRESENTATION_MODE_FOOTAGE_CLEAN, "footage_clean")
        self.assertEqual(render.PRESENTATION_MODE_FOOTAGE_CAPTION, "footage_caption")
        self.assertEqual(
            render.VALID_PRESENTATION_MODES,
            {"card", "footage_clean", "footage_caption"},
        )
        self.assertEqual(render.DEFAULT_PRESENTATION_MODE, "card")

        fake_vid = self.work_dir / "fake.mp4"
        fake_out = self.out_dir / "fake_out.mp4"

        # Invalid presentation_mode string
        with self.assertRaises(ValueError) as ctx:
            render.render(fake_vid, "HEADLINE", fake_out, presentation_mode="invalid_mode")
        self.assertIn("Invalid presentation_mode: 'invalid_mode'", str(ctx.exception))

        # Invalid presentation_mode type
        with self.assertRaises(ValueError) as ctx:
            render.render(fake_vid, "HEADLINE", fake_out, presentation_mode=123)
        self.assertIn("expected string", str(ctx.exception))

        # Reserved presentation_mode footage_caption
        with self.assertRaises(NotImplementedError) as ctx:
            render.render(fake_vid, "HEADLINE", fake_out, presentation_mode="footage_caption")
        self.assertIn("reserved for a future milestone", str(ctx.exception))

    def test_footage_clean_produces_vertical_output_without_overlay(self):
        """2. Clean Footage produces 1080x1920 output and does not invoke card overlay composition."""
        source_vid = self.work_dir / "source.mp4"
        _create_synthetic_video(source_vid, width=1920, height=1080, duration=1.0)

        out_mp4 = self.out_dir / "clean_short.mp4"
        poster_png = self.out_dir / "clean_short.png"

        result = render.render(
            video=source_vid,
            headline="THIS SHOULD NOT BE RENDERED ON VIDEO",
            out=out_mp4,
            poster=poster_png,
            presentation_mode="footage_clean",
        )

        # Output video file exists and is valid
        self.assertTrue(out_mp4.is_file())
        self.assertTrue(poster_png.is_file())

        # Crucial check: NO overlay file was generated
        overlay_file = self.out_dir / "clean_short_overlay.png"
        self.assertFalse(overlay_file.exists(), "Clean footage mode must not generate an overlay file")
        self.assertIsNone(result.overlay, "Result.overlay must be None in footage_clean mode")
        self.assertEqual(result.presentation_mode, "footage_clean")
        self.assertEqual(result.video_box, (0, 0, 1080, 1920))

        # Verify output dimensions are strictly 1080x1920 (9:16 vertical)
        ratio = render.get_source_aspect_ratio(out_mp4)
        self.assertIsNotNone(ratio)
        self.assertAlmostEqual(ratio, 1080.0 / 1920.0, places=2)

    def test_card_mode_retains_existing_behavior(self):
        """3. Card mode generates overlay with avatar, headline, and borders."""
        source_vid = self.work_dir / "source_card.mp4"
        _create_synthetic_video(source_vid, width=1280, height=720, duration=1.0)

        out_mp4 = self.out_dir / "card_short.mp4"
        poster_png = self.out_dir / "card_short.png"

        result = render.render(
            video=source_vid,
            headline="SAMPLE HEADLINE",
            out=out_mp4,
            poster=poster_png,
            presentation_mode="card",
        )

        self.assertTrue(out_mp4.is_file())
        self.assertTrue(poster_png.is_file())

        overlay_file = self.out_dir / "card_short_overlay.png"
        self.assertTrue(overlay_file.is_file(), "Card mode must generate an overlay file")
        self.assertEqual(result.overlay, overlay_file)
        self.assertEqual(result.presentation_mode, "card")
        self.assertNotEqual(result.video_box, (0, 0, 1080, 1920))

    def test_default_mode_when_unspecified_is_card(self):
        """4. When presentation_mode is omitted, default is 'card' for backward compatibility."""
        source_vid = self.work_dir / "source_default.mp4"
        _create_synthetic_video(source_vid, width=640, height=360, duration=0.5)

        out_mp4 = self.out_dir / "default_short.mp4"
        result = render.render(
            video=source_vid,
            headline="DEFAULT MODE",
            out=out_mp4,
        )

        self.assertEqual(result.presentation_mode, "card")
        overlay_file = self.out_dir / "default_short_overlay.png"
        self.assertTrue(overlay_file.is_file())

    def test_layout_presentation_mode_config_override(self):
        """5. layout['presentation_mode'] in config cleanly sets default without explicit param."""
        source_vid = self.work_dir / "source_cfg.mp4"
        _create_synthetic_video(source_vid, width=640, height=360, duration=0.5)

        out_mp4 = self.out_dir / "cfg_short.mp4"
        cfg = {"layout": {"presentation_mode": "footage_clean"}}

        result = render.render(
            video=source_vid,
            headline="IGNORED HEADLINE",
            out=out_mp4,
            config=cfg,
        )

        self.assertEqual(result.presentation_mode, "footage_clean")
        self.assertIsNone(result.overlay)
        overlay_file = self.out_dir / "cfg_short_overlay.png"
        self.assertFalse(overlay_file.exists())

    def test_scaling_and_fit_strategies_in_composite(self):
        """6. Scaling strategies: cover (crop-to-fill) vs contain (aspect-fit with background)."""
        source_vid = self.work_dir / "source_fit.mp4"
        _create_synthetic_video(source_vid, width=1920, height=1080, duration=0.5)

        # A. Contain (aspect-fit 16:9 on 9:16)
        out_contain = self.out_dir / "fit_contain.mp4"
        render.composite(
            video=source_vid,
            overlay=None,
            box=None,
            layout={"canvas_width": 1080, "canvas_height": 1920, "footage_fit": "contain"},
            out=out_contain,
            presentation_mode="footage_clean",
        )
        self.assertTrue(out_contain.is_file())
        self.assertAlmostEqual(render.get_source_aspect_ratio(out_contain), 1080 / 1920, places=2)

        # B. Cover (crop-to-fill 16:9 on 9:16)
        out_cover = self.out_dir / "fit_cover.mp4"
        render.composite(
            video=source_vid,
            overlay=None,
            box=None,
            layout={"canvas_width": 1080, "canvas_height": 1920, "footage_fit": "cover"},
            out=out_cover,
            presentation_mode="footage_clean",
        )
        self.assertTrue(out_cover.is_file())
        self.assertAlmostEqual(render.get_source_aspect_ratio(out_cover), 1080 / 1920, places=2)

    def test_audio_handling_and_music_mixing_in_clean_mode(self):
        """7. Music mixing uses input 1 in clean mode and input 2 in card mode."""
        source_vid = self.work_dir / "source_audio.mp4"
        _create_synthetic_video(source_vid, width=640, height=360, duration=1.0)
        music_file = self.work_dir / "track.mp3"
        # Generate short audio track
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "sine=frequency=880:duration=1.0",
             "-c:a", "libmp3lame", str(music_file)],
            check=True,
        )

        # Clean mode with music
        out_clean = self.out_dir / "clean_music.mp4"
        dur_clean = render.composite(
            video=source_vid,
            overlay=None,
            box=None,
            layout={"canvas_width": 1080, "canvas_height": 1920},
            out=out_clean,
            music=music_file,
            presentation_mode="footage_clean",
        )
        self.assertTrue(out_clean.is_file())
        self.assertGreater(dur_clean, 0.0)
        self.assertTrue(render.has_audio_stream(out_clean))

        # Card mode with music
        out_card = self.out_dir / "card_music.mp4"
        overlay_file = self.out_dir / "card_music_overlay.png"
        box = render.build_overlay("TEST", render.Account(), render.DEFAULT_LAYOUT, overlay_file)
        dur_card = render.composite(
            video=source_vid,
            overlay=overlay_file,
            box=box,
            layout=render.DEFAULT_LAYOUT,
            out=out_card,
            music=music_file,
            presentation_mode="card",
        )
        self.assertTrue(out_card.is_file())
        self.assertGreater(dur_card, 0.0)
        self.assertTrue(render.has_audio_stream(out_card))

    @patch("core.agent.download_clip")
    @patch("core.agent.render")
    def test_automated_rendering_passes_selected_mode_through(self, mock_render, mock_download):
        """8. Automated _render_pick passes presentation_mode to render and registers it in store.draft."""
        mock_download.return_value = True
        mock_render.return_value = render.RenderResult(
            output=Path("out/test.mp4"),
            overlay=None,
            video_box=(0, 0, 1080, 1920),
            duration=15.0,
            presentation_mode="footage_clean",
        )

        cand = Candidate(kind="clip", key="yt_clean_1", title="Sample Title", url="https://youtube.com/watch?v=123", source="youtube")
        pick = Pick(
            candidate=cand,
            headline="NEWS HEADLINE",
            caption="Caption",
            title="Video Title",
            description="Video Description",
            presentation_mode="footage_clean",
        )

        config = {
            "layout": {"border_color": "#1D9BF0"},
            "posting": {"max_clip_seconds": 30},
        }

        with patch("core.agent.get_source_aspect_ratio", return_value=0.5625):
            res = _render_pick(pick, config, self.work_dir, self.out_dir, "test_stem")

        self.assertIsNotNone(res)
        self.assertTrue(mock_render.called)
        _, kwargs = mock_render.call_args
        self.assertEqual(kwargs.get("presentation_mode"), "footage_clean")
        self.assertEqual(pick.presentation_mode, "footage_clean")

    def test_manual_rerender_passes_mode_through_without_upload(self):
        """9. Worker manual re-render honors presentation_mode without initiating upload."""
        raw_vid = self.work_dir / "short_test_manual_raw.mp4"
        _create_synthetic_video(raw_vid, width=1280, height=720, duration=0.5)

        test_record = {
            "id": "short_test_manual",
            "video": str(self.out_dir / "short_test_manual.mp4"),
            "poster": str(self.out_dir / "short_test_manual.png"),
            "headline": "EXISTING HEADLINE",
            "title": "Short Title",
            "description": "Short Description",
            "status": "ready",
            "review_status": "unreviewed",
            "raw_video": str(raw_vid),
            "style_preset": "classic_blue",
            "presentation_mode": "footage_clean",
            "candidate": {"kind": "clip", "key": "test_key"},
        }

        # Write minimal config.example.json so worker can load config
        (self.dir_path / "config.example.json").write_text('{"posting": {}, "layout": {}}', encoding="utf-8")

        # Mock store and upload functions
        with patch.object(store, "ROOT", self.dir_path), \
             patch.object(store, "get", return_value=test_record), \
             patch.object(store, "put") as mock_put, \
             patch("core.agent.upload_to_youtube") as mock_upload:

            worker.work("render", "short_test_manual")

            # Verify upload was never called
            mock_upload.assert_not_called()

            # Find the final record put into store
            final_record = None
            for call_item in mock_put.call_args_list:
                table, key, rec = call_item[0]
                if rec.get("status") == "ready":
                    final_record = rec
                    break

            self.assertIsNotNone(final_record)
            self.assertEqual(final_record["status"], "ready")
            self.assertEqual(final_record["presentation_mode"], "footage_clean")
            # Verify new rendered file exists
            self.assertTrue(Path(final_record["video"]).is_file())
            # Verify no overlay file was created for the new render
            new_overlay = Path(final_record["video"]).parent / f"{Path(final_record['video']).stem}_overlay.png"
            self.assertFalse(new_overlay.exists(), "Re-render with footage_clean must not create overlay file")

    def test_api_video_presentation_mode_validation_and_mutation(self):
        """10. /api/video mutates presentation_mode and rejects invalid values."""
        test_record = {
            "id": "vid_api_test",
            "status": "ready",
            "title": "Title",
            "description": "Desc",
            "headline": "Headline",
            "style_preset": "classic_blue",
            "presentation_mode": "card",
        }

        handler = server.StudioHandler.__new__(server.StudioHandler)

        with patch.object(store, "get", return_value=test_record), \
             patch.object(store, "put") as mock_put:

            # Valid mutation to footage_clean
            res = handler.mutate("/api/video", {"id": "vid_api_test", "presentation_mode": "footage_clean"})
            self.assertEqual(res["presentation_mode"], "footage_clean")

            # Reset to card
            res = handler.mutate("/api/video", {"id": "vid_api_test", "presentation_mode": "card"})
            self.assertEqual(res["presentation_mode"], "card")

            # Empty string normalizes to card
            res = handler.mutate("/api/video", {"id": "vid_api_test", "presentation_mode": ""})
            self.assertEqual(res["presentation_mode"], "card")

            # Invalid string rejected with ValueError
            with self.assertRaises(ValueError) as ctx:
                handler.mutate("/api/video", {"id": "vid_api_test", "presentation_mode": "invalid_mode"})
            self.assertIn("Invalid presentation_mode", str(ctx.exception))

            # Non-string rejected with ValueError
            with self.assertRaises(ValueError) as ctx:
                handler.mutate("/api/video", {"id": "vid_api_test", "presentation_mode": 12345})
            self.assertIn("expected string", str(ctx.exception))

            # Reserved mode footage_caption rejected with ValueError
            with self.assertRaises(ValueError) as ctx:
                handler.mutate("/api/video", {"id": "vid_api_test", "presentation_mode": "footage_caption"})
            self.assertIn("reserved for a future milestone", str(ctx.exception))

    def test_metadata_preservation_in_clean_mode(self):
        """11. Clean render preserves title, description, tags, and candidate metadata without conflation."""
        source_vid = self.work_dir / "meta_test.mp4"
        _create_synthetic_video(source_vid, width=640, height=360, duration=0.5)

        out_mp4 = self.out_dir / "meta_short.mp4"
        poster_png = self.out_dir / "meta_short.png"

        cand_data = {
            "key": "meta_key_1",
            "source": "youtube",
            "url": "https://youtube.com/watch?v=meta123",
            "channel": "NASA",
            "licence": "public-domain",
        }

        config = {
            "posting": {
                "privacy": "unlisted",
                "tags": ["space", "nasa", "science"],
            },
            "layout": {
                "presentation_mode": "footage_clean",
            }
        }

        # Render clean video
        render.render(
            video=source_vid,
            headline="ON SCREEN HEADLINE (SUPPRESSED)",
            out=out_mp4,
            poster=poster_png,
            presentation_mode="footage_clean",
        )

        with patch.object(store, "put") as mock_put:
            rec = store.draft(
                video_stem="meta_short",
                video_path=out_mp4,
                poster_path=poster_png,
                headline="ON SCREEN HEADLINE (SUPPRESSED)",
                title="Exact YouTube Title (Untouched)",
                description="Exact YouTube Description with links and hashtags.",
                config=config,
                candidate_data=cand_data,
                presentation_mode="footage_clean",
            )

        # Assert metadata is strictly preserved
        self.assertEqual(rec["title"], "Exact YouTube Title (Untouched)")
        self.assertEqual(rec["description"], "Exact YouTube Description with links and hashtags.")
        self.assertEqual(rec["candidate"], cand_data)
        self.assertEqual(rec["presentation_mode"], "footage_clean")
        self.assertEqual(rec["headline"], "ON SCREEN HEADLINE (SUPPRESSED)")
        self.assertEqual(rec["privacy"], "unlisted")

    def test_temporary_file_cleanup_on_timeout_in_clean_mode(self):
        """12. If rendering times out in clean footage mode, partial output is unlinked and TimeoutError raised."""
        source_vid = self.work_dir / "timeout_src.mp4"
        _create_synthetic_video(source_vid, width=640, height=360, duration=0.5)
        out_mp4 = self.out_dir / "timeout_out.mp4"

        # Create a dummy partial file to prove it gets unlinked
        out_mp4.touch()
        self.assertTrue(out_mp4.is_file())

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 0.1)):
            with self.assertRaises(TimeoutError):
                render.composite(
                    video=source_vid,
                    overlay=None,
                    box=None,
                    layout={"canvas_width": 1080, "canvas_height": 1920},
                    out=out_mp4,
                    presentation_mode="footage_clean",
                    timeout=0.1,
                )

        self.assertFalse(out_mp4.exists(), "Partial file must be unlinked on timeout")


if __name__ == "__main__":
    unittest.main()
