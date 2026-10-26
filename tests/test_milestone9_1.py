"""
tests/test_milestone9_1.py — Focused tests for Milestone 9.1:
Ambient Background and Shadow Engine.

Covers:
- Default settings preserve existing behavior.
- Solid mode retains existing background behavior.
- Blurred ambient produces valid 1080x1920 output with downscaled blur filtergraph.
- Shadow enabled and disabled behavior in Pillow overlay.
- Clean-footage output remains completely unaffected by card-effect settings.
- Invalid configuration values are rejected with ValueError.
- Existing source audio and video timing are preserved.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from PIL import Image

from core.render import (
    Account,
    CARD_BACKGROUND_MODE_BLURRED_AMBIENT,
    CARD_BACKGROUND_MODE_SOLID,
    DEFAULT_AMBIENT_BLUR,
    DEFAULT_AMBIENT_DIM,
    DEFAULT_CARD_BACKGROUND_MODE,
    DEFAULT_CARD_SHADOW,
    DEFAULT_LAYOUT,
    DEFAULT_SHADOW_BLUR,
    DEFAULT_SHADOW_OFFSET_Y,
    DEFAULT_SHADOW_OPACITY,
    PRESENTATION_MODE_CARD,
    PRESENTATION_MODE_FOOTAGE_CLEAN,
    VALID_CARD_BACKGROUND_MODES,
    build_overlay,
    composite,
    has_audio_stream,
    probe_duration,
    render,
)

FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _run_cmd(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class TestCardBackgroundDefaultsAndValidation(unittest.TestCase):
    """Verifies default layout constants and fail-closed validation of settings."""

    def test_default_layout_includes_ambient_and_shadow_defaults(self):
        self.assertEqual(DEFAULT_LAYOUT["card_background_mode"], "solid")
        self.assertFalse(DEFAULT_LAYOUT["card_shadow"])
        self.assertEqual(DEFAULT_LAYOUT["ambient_dim"], 0.45)
        self.assertEqual(DEFAULT_LAYOUT["ambient_blur"], 10)
        self.assertEqual(DEFAULT_LAYOUT["shadow_blur"], 24)
        self.assertEqual(DEFAULT_LAYOUT["shadow_opacity"], 0.40)
        self.assertEqual(DEFAULT_LAYOUT["shadow_offset_y"], 8)

    def test_valid_card_background_modes_constants(self):
        self.assertIn("solid", VALID_CARD_BACKGROUND_MODES)
        self.assertIn("blurred_ambient", VALID_CARD_BACKGROUND_MODES)
        self.assertEqual(CARD_BACKGROUND_MODE_SOLID, "solid")
        self.assertEqual(CARD_BACKGROUND_MODE_BLURRED_AMBIENT, "blurred_ambient")

    def test_invalid_card_background_mode_rejected_in_build_overlay(self):
        with tempfile.TemporaryDirectory() as td:
            out_png = Path(td) / "test_overlay.png"
            acct = Account(name="Test", handle="@test")

            for invalid in ["gradient", "video_blur", "random", "", 123, None]:
                with self.assertRaises(ValueError):
                    build_overlay(
                        headline="Headline",
                        account=acct,
                        layout={**DEFAULT_LAYOUT, "card_background_mode": invalid},
                        out_path=out_png,
                    )

    def test_invalid_ambient_dim_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            dummy_mp4 = Path(td) / "dummy.mp4"
            dummy_mp4.touch()
            out_mp4 = Path(td) / "out.mp4"

            for invalid in [-0.1, 1.5, "0.5", True, None]:
                with self.assertRaises(ValueError):
                    composite(
                        video=dummy_mp4,
                        overlay=dummy_mp4,
                        box=(0, 0, 1080, 1920),
                        layout={**DEFAULT_LAYOUT, "card_background_mode": "blurred_ambient", "ambient_dim": invalid},
                        out=out_mp4,
                    )

    def test_invalid_ambient_blur_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            dummy_mp4 = Path(td) / "dummy.mp4"
            dummy_mp4.touch()
            out_mp4 = Path(td) / "out.mp4"

            for invalid in [0, 51, -5, "10", 3.14, True]:
                with self.assertRaises(ValueError):
                    composite(
                        video=dummy_mp4,
                        overlay=dummy_mp4,
                        box=(0, 0, 1080, 1920),
                        layout={**DEFAULT_LAYOUT, "card_background_mode": "blurred_ambient", "ambient_blur": invalid},
                        out=out_mp4,
                    )

    def test_invalid_card_shadow_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            out_png = Path(td) / "test_overlay.png"
            acct = Account(name="Test", handle="@test")

            for invalid in ["maybe", "soft", 123, []]:
                with self.assertRaises(ValueError):
                    build_overlay(
                        headline="Headline",
                        account=acct,
                        layout={**DEFAULT_LAYOUT, "card_shadow": invalid},
                        out_path=out_png,
                    )

    def test_invalid_shadow_parameters_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            out_png = Path(td) / "test_overlay.png"
            acct = Account(name="Test", handle="@test")

            # shadow_blur out of bounds
            with self.assertRaises(ValueError):
                build_overlay("H", acct, {**DEFAULT_LAYOUT, "card_shadow": True, "shadow_blur": 0}, out_png)
            with self.assertRaises(ValueError):
                build_overlay("H", acct, {**DEFAULT_LAYOUT, "card_shadow": True, "shadow_blur": 105}, out_png)

            # shadow_opacity out of bounds
            with self.assertRaises(ValueError):
                build_overlay("H", acct, {**DEFAULT_LAYOUT, "card_shadow": True, "shadow_opacity": -0.1}, out_png)
            with self.assertRaises(ValueError):
                build_overlay("H", acct, {**DEFAULT_LAYOUT, "card_shadow": True, "shadow_opacity": 1.2}, out_png)

            # shadow_offset_y out of bounds
            with self.assertRaises(ValueError):
                build_overlay("H", acct, {**DEFAULT_LAYOUT, "card_shadow": True, "shadow_offset_y": 60}, out_png)


class TestPillowOverlayShadowAndTransparency(unittest.TestCase):
    """Verifies overlay transparency and shadow rendering in Pillow."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.acct = Account(name="KenauShorts", handle="@kenaushorts")

    def tearDown(self):
        self.td.cleanup()

    def test_solid_mode_overlay_has_opaque_background(self):
        out_png = self.tmp / "solid_overlay.png"
        box = build_overlay(
            headline="Solid Mode Test",
            account=self.acct,
            layout={**DEFAULT_LAYOUT, "card_background_mode": "solid", "background": "#000000"},
            out_path=out_png,
        )
        im = Image.open(out_png)
        self.assertEqual(im.size, (1080, 1920))

        # Top corner margin should be opaque black
        pixel = im.getpixel((10, 10))
        self.assertEqual(pixel[3], 255)  # Alpha is 255 (opaque)
        self.assertEqual(pixel[:3], (0, 0, 0))

        # Inside the video cutout window should be transparent (alpha 0)
        fx, fy, fw, fh = box
        cutout_pixel = im.getpixel((fx + fw // 2, fy + fh // 2))
        self.assertEqual(cutout_pixel[3], 0)

    def test_blurred_ambient_overlay_has_transparent_background(self):
        out_png = self.tmp / "ambient_overlay.png"
        box = build_overlay(
            headline="Ambient Mode Test",
            account=self.acct,
            layout={**DEFAULT_LAYOUT, "card_background_mode": "blurred_ambient"},
            out_path=out_png,
        )
        im = Image.open(out_png)
        self.assertEqual(im.size, (1080, 1920))

        # Top corner margin should be transparent to let ambient video show through
        pixel = im.getpixel((10, 10))
        self.assertEqual(pixel[3], 0)  # Alpha is 0 (transparent)

        # Inside the video cutout window should also be transparent
        fx, fy, fw, fh = box
        cutout_pixel = im.getpixel((fx + fw // 2, fy + fh // 2))
        self.assertEqual(cutout_pixel[3], 0)

    def test_card_shadow_disabled_by_default(self):
        out_png = self.tmp / "no_shadow.png"
        box = build_overlay(
            headline="No Shadow Test",
            account=self.acct,
            layout={**DEFAULT_LAYOUT, "card_background_mode": "blurred_ambient", "card_shadow": False},
            out_path=out_png,
        )
        im = Image.open(out_png)
        fx, fy, fw, fh = box

        # 15px directly below the video frame cutout (outside the frame) should have alpha 0
        below_frame = im.getpixel((fx + fw // 2, fy + fh + 15))
        self.assertEqual(below_frame[3], 0)

    def test_card_shadow_enabled_draws_soft_gaussian_shadow(self):
        out_png = self.tmp / "with_shadow.png"
        box = build_overlay(
            headline="Shadow Enabled Test",
            account=self.acct,
            layout={
                **DEFAULT_LAYOUT,
                "card_background_mode": "blurred_ambient",
                "card_shadow": True,
                "shadow_opacity": 0.40,
                "shadow_blur": 24,
                "shadow_offset_y": 8,
            },
            out_path=out_png,
        )
        im = Image.open(out_png)
        fx, fy, fw, fh = box

        # 15px directly below the video frame should have a soft dark shadow (alpha > 0 and RGB black)
        below_frame = im.getpixel((fx + fw // 2, fy + fh + 15))
        self.assertGreater(below_frame[3], 0)
        self.assertEqual(below_frame[:3], (0, 0, 0))

        # Inside the video window cutout, alpha must remain strictly 0
        cutout_pixel = im.getpixel((fx + fw // 2, fy + fh // 2))
        self.assertEqual(cutout_pixel[3], 0)

    def test_card_shadow_string_variants_accepted(self):
        out_png = self.tmp / "str_shadow.png"
        for val in ["true", "1", "yes", "on"]:
            box = build_overlay(
                "H",
                self.acct,
                {**DEFAULT_LAYOUT, "card_background_mode": "blurred_ambient", "card_shadow": val},
                out_png,
            )
            im = Image.open(out_png)
            fx, fy, fw, fh = box
            self.assertGreater(im.getpixel((fx + fw // 2, fy + fh + 15))[3], 0)


@unittest.skipUnless(FFMPEG_AVAILABLE, "ffmpeg/ffprobe not on PATH")
class TestFFmpegFiltergraphAndRendering(unittest.TestCase):
    """End-to-end rendering tests using local test footage."""

    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp_dir.name)

        # Generate 2-second 16:9 test clip with audio
        cls.test_clip = cls.dir / "test_src.mp4"
        _run_cmd([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(cls.test_clip),
        ])

    @classmethod
    def tearDownClass(cls):
        cls.tmp_dir.cleanup()

    def test_solid_mode_render_preserves_existing_behavior(self):
        out_mp4 = self.dir / "out_solid.mp4"
        res = render(
            video=self.test_clip,
            headline="Solid Background Test",
            out=out_mp4,
            config={"layout": {"card_background_mode": "solid"}},
            max_seconds=1.5,
        )
        self.assertTrue(out_mp4.is_file())
        self.assertAlmostEqual(res.duration, 1.5, delta=0.2)
        self.assertEqual(res.presentation_mode, "card")
        self.assertEqual(res.card_background_mode, "solid")
        self.assertFalse(res.card_shadow)

    def test_blurred_ambient_render_produces_valid_output(self):
        out_mp4 = self.dir / "out_ambient.mp4"
        res = render(
            video=self.test_clip,
            headline="Blurred Ambient Test",
            out=out_mp4,
            config={"layout": {
                "card_background_mode": "blurred_ambient",
                "ambient_dim": 0.40,
                "ambient_blur": 10,
            }},
            max_seconds=1.5,
        )
        self.assertTrue(out_mp4.is_file())
        self.assertAlmostEqual(res.duration, 1.5, delta=0.2)
        self.assertEqual(res.presentation_mode, "card")
        self.assertEqual(res.card_background_mode, "blurred_ambient")
        self.assertTrue(has_audio_stream(out_mp4))

    def test_blurred_ambient_with_shadow_enabled(self):
        out_mp4 = self.dir / "out_ambient_shadow.mp4"
        res = render(
            video=self.test_clip,
            headline="Ambient With Shadow Test",
            out=out_mp4,
            config={"layout": {
                "card_background_mode": "blurred_ambient",
                "card_shadow": True,
                "ambient_dim": 0.50,
            }},
            max_seconds=1.0,
        )
        self.assertTrue(out_mp4.is_file())
        self.assertTrue(res.card_shadow)
        self.assertEqual(res.card_background_mode, "blurred_ambient")

    def test_clean_footage_mode_unaffected_by_card_visual_settings(self):
        out_mp4 = self.dir / "out_clean.mp4"
        res = render(
            video=self.test_clip,
            headline="Clean Footage Should Ignore Card Effects",
            out=out_mp4,
            presentation_mode="footage_clean",
            card_background_mode="blurred_ambient",
            card_shadow=True,
            max_seconds=1.0,
        )
        self.assertTrue(out_mp4.is_file())
        self.assertIsNone(res.overlay)
        self.assertEqual(res.presentation_mode, "footage_clean")
        self.assertEqual(res.video_box, (0, 0, 1080, 1920))

    def test_render_direct_kwargs_override_config(self):
        out_mp4 = self.dir / "out_override.mp4"
        res = render(
            video=self.test_clip,
            headline="Direct Kwargs Test",
            out=out_mp4,
            config={"layout": {"card_background_mode": "solid", "card_shadow": False}},
            card_background_mode="blurred_ambient",
            card_shadow=True,
            ambient_dim=0.35,
            max_seconds=1.0,
        )
        self.assertEqual(res.card_background_mode, "blurred_ambient")
        self.assertTrue(res.card_shadow)

    def test_rendering_performance_comparison(self):
        """Measures render duration on local clip: solid vs blurred ambient."""
        out_solid = self.dir / "perf_solid.mp4"
        t0 = time.time()
        render(
            video=self.test_clip,
            headline="Performance Solid",
            out=out_solid,
            config={"layout": {"card_background_mode": "solid"}},
            max_seconds=2.0,
        )
        solid_time = time.time() - t0

        out_ambient = self.dir / "perf_ambient.mp4"
        t0 = time.time()
        render(
            video=self.test_clip,
            headline="Performance Ambient",
            out=out_ambient,
            config={"layout": {"card_background_mode": "blurred_ambient", "card_shadow": True}},
            max_seconds=2.0,
        )
        ambient_time = time.time() - t0

        # Assert both finished in reasonable time (well under 300s timeout, typically 1-3s)
        self.assertLess(solid_time, 15.0)
        self.assertLess(ambient_time, 20.0)
        print(f"\n[Milestone 9.1 Performance Benchmark] Solid: {solid_time:.3f}s | Blurred Ambient + Shadow: {ambient_time:.3f}s")


if __name__ == "__main__":
    unittest.main()
