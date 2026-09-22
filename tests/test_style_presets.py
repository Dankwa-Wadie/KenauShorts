"""
tests/test_style_presets.py — Tests for automatic style variety presets on video cards
and aspect-ratio matching.
"""

import copy
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.agent import Candidate, Pick, _render_pick
from core.style_presets import (
    STYLE_PRESETS,
    apply_style_preset,
    choose_style_preset,
    get_source_aspect_ratio,
    get_style_preset,
    match_style_preset_to_aspect,
    parse_aspect,
)
from studio import store


class StylePresetTests(unittest.TestCase):
    def test_style_presets_integrity(self):
        """Ensure presets are well-formed and stay within modest, visually safe bounds."""
        self.assertGreaterEqual(len(STYLE_PRESETS), 3)
        hex_re = re.compile(r"^#[0-9A-Fa-f]{6}$")

        preset_names = set()
        for preset in STYLE_PRESETS:
            name = preset["name"]
            self.assertNotIn(name, preset_names, f"Duplicate preset name: {name}")
            preset_names.add(name)

            self.assertTrue(preset.get("label"))
            self.assertTrue(preset.get("description"))

            layout = preset.get("layout", {})
            self.assertIn("border_color", layout)
            self.assertIn("corner_radius", layout)
            self.assertIn("video_aspect", layout)

            # Modest bounds verification
            self.assertTrue(hex_re.match(layout["border_color"]), f"Invalid hex color: {layout['border_color']}")
            self.assertTrue(20 <= layout["corner_radius"] <= 50, f"Corner radius out of bounds: {layout['corner_radius']}")

            aspect_val = parse_aspect(layout["video_aspect"])
            # Between 4:5 (0.8) and 16:9 (1.777...)
            self.assertTrue(0.75 <= aspect_val <= 1.80, f"Aspect ratio out of bounds: {layout['video_aspect']} ({aspect_val})")

            # Check lookup functions
            self.assertEqual(get_style_preset(name), preset)

        self.assertIsNone(get_style_preset("non_existent_preset"))
        chosen = choose_style_preset()
        self.assertIn(chosen, STYLE_PRESETS)

    def test_parse_aspect(self):
        """Verify parse_aspect converts ratios and decimals accurately."""
        self.assertAlmostEqual(parse_aspect("16:9"), 16.0 / 9.0)
        self.assertAlmostEqual(parse_aspect("4:3"), 4.0 / 3.0)
        self.assertAlmostEqual(parse_aspect("1:1"), 1.0)
        self.assertAlmostEqual(parse_aspect("4:5"), 0.8)
        self.assertAlmostEqual(parse_aspect(1.5), 1.5)
        self.assertAlmostEqual(parse_aspect(" 16:9 "), 16.0 / 9.0)

    def test_match_style_preset_to_aspect(self):
        """Verify that presets are matched to numerically closest source aspect ratios."""
        # 16:9 (1.778) and wider -> classic_blue
        self.assertEqual(match_style_preset_to_aspect(1.7778)["name"], "classic_blue")
        self.assertEqual(match_style_preset_to_aspect(2.35)["name"], "classic_blue")  # Cinemascope
        self.assertEqual(match_style_preset_to_aspect(1.6)["name"], "classic_blue")

        # 4:3 (1.333) -> warm_amber
        self.assertEqual(match_style_preset_to_aspect(1.3333)["name"], "warm_amber")
        self.assertEqual(match_style_preset_to_aspect(1.5)["name"], "warm_amber")  # 3:2 camera frame
        self.assertEqual(match_style_preset_to_aspect(1.2)["name"], "warm_amber")

        # 1:1 (1.0) -> emerald_compact
        self.assertEqual(match_style_preset_to_aspect(1.0)["name"], "emerald_compact")
        self.assertEqual(match_style_preset_to_aspect(1.05)["name"], "emerald_compact")
        self.assertEqual(match_style_preset_to_aspect(0.95)["name"], "emerald_compact")

        # 4:5 (0.8) and 9:16 (0.5625) -> cyber_violet
        self.assertEqual(match_style_preset_to_aspect(0.8)["name"], "cyber_violet")
        self.assertEqual(match_style_preset_to_aspect(0.5625)["name"], "cyber_violet")
        self.assertEqual(match_style_preset_to_aspect(0.7)["name"], "cyber_violet")

    def test_get_source_aspect_ratio_real_fixture(self):
        """Test reading aspect ratio from a real known video file in the repo (assets/default_mascot.mp4)."""
        fixture = Path(__file__).resolve().parent.parent / "assets" / "default_mascot.mp4"
        if fixture.exists():
            ratio = get_source_aspect_ratio(fixture)
            self.assertIsNotNone(ratio)
            # default_mascot is 1080x1920 (0.5625)
            self.assertAlmostEqual(ratio, 1080.0 / 1920.0, places=3)

    def test_get_source_aspect_ratio_mocked(self):
        """Test get_source_aspect_ratio across landscape, square, and portrait with ffprobe mocked."""
        dummy_file = Path(__file__)  # Use an existing file path so is_file() passes

        # Landscape 1920x1080
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="1920,1080\n")
            ratio = get_source_aspect_ratio(dummy_file)
            self.assertAlmostEqual(ratio, 16.0 / 9.0)

        # Portrait 1080x1920
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="1080,1920\n")
            ratio = get_source_aspect_ratio(dummy_file)
            self.assertAlmostEqual(ratio, 9.0 / 16.0)

        # Square 1080x1080
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="1080,1080\n")
            ratio = get_source_aspect_ratio(dummy_file)
            self.assertAlmostEqual(ratio, 1.0)

    def test_get_source_aspect_ratio_failures(self):
        """Test get_source_aspect_ratio error handling returns None gracefully."""
        # Non-existent file
        self.assertIsNone(get_source_aspect_ratio(Path("does_not_exist_xyz.mp4")))

        dummy_file = Path(__file__)

        # ffprobe CalledProcessError
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, ["ffprobe"])):
            self.assertIsNone(get_source_aspect_ratio(dummy_file))

        # ffprobe FileNotFoundError (e.g. ffprobe not installed)
        with patch("subprocess.run", side_effect=FileNotFoundError("ffprobe not found")):
            self.assertIsNone(get_source_aspect_ratio(dummy_file))

        # Empty output
        with patch("subprocess.run", return_value=MagicMock(stdout="")):
            self.assertIsNone(get_source_aspect_ratio(dummy_file))

        # Malformed output
        with patch("subprocess.run", return_value=MagicMock(stdout="invalid,data,extra")):
            self.assertIsNone(get_source_aspect_ratio(dummy_file))

        # Zero dimensions
        with patch("subprocess.run", return_value=MagicMock(stdout="0,0")):
            self.assertIsNone(get_source_aspect_ratio(dummy_file))

    def test_apply_style_preset_overrides_layout_without_mutating_base(self):
        """Test that apply_style_preset merges overrides over base config immutably."""
        base_config = {
            "account": {"name": "Test Account"},
            "layout": {
                "side_margin": 80,
                "border_color": "#FFFFFF",
                "corner_radius": 15,
                "video_aspect": "16:9",
            },
            "story_layout": {
                "card_radius": 20,
            },
        }
        orig_copy = copy.deepcopy(base_config)

        preset = get_style_preset("warm_amber")
        self.assertIsNotNone(preset)

        merged = apply_style_preset(base_config, preset)

        # Base config must not be mutated
        self.assertEqual(base_config, orig_copy)

        # Overrides applied
        self.assertEqual(merged["layout"]["border_color"], preset["layout"]["border_color"])
        self.assertEqual(merged["layout"]["corner_radius"], preset["layout"]["corner_radius"])
        self.assertEqual(merged["layout"]["video_aspect"], preset["layout"]["video_aspect"])

        # Preserved non-overridden fields
        self.assertEqual(merged["layout"]["side_margin"], 80)
        self.assertEqual(merged["account"]["name"], "Test Account")

    def test_render_pick_matches_source_aspect_ratio(self):
        """Test that _render_pick selects the closest style preset based on source aspect ratio."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir) / "work"
            out_dir = Path(tmp_dir) / "out"
            work_dir.mkdir()
            out_dir.mkdir()

            cases = [
                (1.7778, "classic_blue"),     # 16:9
                (1.3333, "warm_amber"),      # 4:3
                (1.0000, "emerald_compact"), # 1:1
                (0.5625, "cyber_violet"),    # 9:16 portrait
            ]

            for ratio, expected_preset in cases:
                candidate = Candidate(
                    kind="clip",
                    key=f"test_clip_{expected_preset}",
                    title="Test Clip",
                    url="https://example.com/video.mp4",
                    source="test",
                )
                pick = Pick(
                    candidate=candidate,
                    headline="TEST HEADLINE",
                    caption="Test caption",
                    title="Test Title",
                    description="Test Description",
                )
                base_config = {"layout": {}}

                with patch("core.agent.download_clip", return_value=True), \
                     patch("core.agent.get_source_aspect_ratio", return_value=ratio), \
                     patch("core.agent.render") as mock_render:
                    out_path = _render_pick(pick, base_config, work_dir, out_dir, f"stem_{expected_preset}")

                    self.assertIsNotNone(out_path)
                    self.assertEqual(pick.style_preset, expected_preset)

                    called_config = mock_render.call_args.kwargs["config"]
                    preset_cfg = get_style_preset(expected_preset)
                    self.assertEqual(called_config["layout"]["border_color"], preset_cfg["layout"]["border_color"])
                    self.assertEqual(called_config["layout"]["video_aspect"], preset_cfg["layout"]["video_aspect"])

    def test_render_pick_fallback_on_ffprobe_failure(self):
        """Test that _render_pick falls back to random selection if aspect ratio cannot be probed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir) / "work"
            out_dir = Path(tmp_dir) / "out"
            work_dir.mkdir()
            out_dir.mkdir()

            candidate = Candidate(
                kind="clip",
                key="test_clip_fallback",
                title="Test Clip Fallback",
                url="https://example.com/video.mp4",
                source="test",
            )
            pick = Pick(
                candidate=candidate,
                headline="TEST HEADLINE",
                caption="Test caption",
                title="Test Title",
                description="Test Description",
            )
            base_config = {"layout": {}}

            with patch("core.agent.download_clip", return_value=True), \
                 patch("core.agent.get_source_aspect_ratio", return_value=None), \
                 patch("core.agent.render") as mock_render:
                out_path = _render_pick(pick, base_config, work_dir, out_dir, "stem_fallback")

                self.assertIsNotNone(out_path)
                mock_render.assert_called_once()
                # Should have fallen back to a valid preset from STYLE_PRESETS
                self.assertIn(pick.style_preset, [p["name"] for p in STYLE_PRESETS])

    def test_render_pick_honors_preselected_preset(self):
        """Test that _render_pick uses pick.style_preset if already specified without probing aspect."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir) / "work"
            out_dir = Path(tmp_dir) / "out"
            work_dir.mkdir()
            out_dir.mkdir()

            candidate = Candidate(
                kind="clip",
                key="test_clip_preselected",
                title="Test Clip Preselected",
                url="https://example.com/video2.mp4",
                source="test",
            )
            pick = Pick(
                candidate=candidate,
                headline="TEST HEADLINE 2",
                caption="Test caption 2",
                title="Test Title 2",
                description="Test Description 2",
                style_preset="cyber_violet",
            )
            base_config = {"layout": {}}

            with patch("core.agent.download_clip", return_value=True), \
                 patch("core.agent.get_source_aspect_ratio") as mock_probe, \
                 patch("core.agent.render") as mock_render:
                out_path = _render_pick(pick, base_config, work_dir, out_dir, "stem_preselected")

                self.assertIsNotNone(out_path)
                # When preset is already set, get_source_aspect_ratio should not be called
                mock_probe.assert_not_called()
                self.assertEqual(pick.style_preset, "cyber_violet")

                called_config = mock_render.call_args.kwargs["config"]
                violet_preset = get_style_preset("cyber_violet")
                self.assertEqual(called_config["layout"]["border_color"], violet_preset["layout"]["border_color"])
                self.assertEqual(called_config["layout"]["video_aspect"], violet_preset["layout"]["video_aspect"])

    def test_store_draft_persists_style_preset(self):
        """Test that store.draft accepts and persists style_preset on the video record."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test_store.sqlite3"
            with patch.object(store, "DB", db_path), patch.object(store, "ROOT", Path(tmp_dir)):
                rec = store.draft(
                    video_stem="short_preset_test",
                    video_path=Path(tmp_dir) / "out.mp4",
                    poster_path=Path(tmp_dir) / "out.png",
                    headline="HEADLINE",
                    title="TITLE",
                    description="DESC",
                    config={"layout": {}},
                    style_preset="emerald_compact",
                )
                self.assertEqual(rec["style_preset"], "emerald_compact")

                saved = store.get("videos", "short_preset_test")
                self.assertIsNotNone(saved)
                self.assertEqual(saved.get("style_preset"), "emerald_compact")


if __name__ == "__main__":
    unittest.main()
