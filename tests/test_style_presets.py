"""
tests/test_style_presets.py — Tests for automatic style variety presets on video cards.
"""

import copy
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.agent import Candidate, Pick, _render_pick
from core.render import parse_aspect
from core.style_presets import (
    STYLE_PRESETS,
    apply_style_preset,
    choose_style_preset,
    get_style_preset,
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

    def test_render_pick_selects_and_applies_random_preset_to_clip(self):
        """Test that _render_pick chooses a preset, applies its layout, and sets pick.style_preset."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir) / "work"
            out_dir = Path(tmp_dir) / "out"
            work_dir.mkdir()
            out_dir.mkdir()

            candidate = Candidate(
                kind="clip",
                key="test_clip_1",
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
            base_config = {
                "layout": {
                    "border_color": "#000000",
                    "corner_radius": 10,
                    "video_aspect": "16:9",
                }
            }

            with patch("core.agent.download_clip", return_value=True), \
                 patch("core.agent.render") as mock_render:
                out_path = _render_pick(pick, base_config, work_dir, out_dir, "stem_1")

                self.assertIsNotNone(out_path)
                mock_render.assert_called_once()

                # Pick should now have a valid style_preset recorded
                self.assertIn(pick.style_preset, [p["name"] for p in STYLE_PRESETS])

                # The config passed to render() should have that preset's layout applied
                called_config = mock_render.call_args.kwargs["config"]
                applied_preset = get_style_preset(pick.style_preset)
                self.assertEqual(called_config["layout"]["border_color"], applied_preset["layout"]["border_color"])
                self.assertEqual(called_config["layout"]["corner_radius"], applied_preset["layout"]["corner_radius"])
                self.assertEqual(called_config["layout"]["video_aspect"], applied_preset["layout"]["video_aspect"])

    def test_render_pick_honors_preselected_preset(self):
        """Test that _render_pick uses pick.style_preset if already specified."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir) / "work"
            out_dir = Path(tmp_dir) / "out"
            work_dir.mkdir()
            out_dir.mkdir()

            candidate = Candidate(
                kind="clip",
                key="test_clip_2",
                title="Test Clip 2",
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
                 patch("core.agent.render") as mock_render:
                out_path = _render_pick(pick, base_config, work_dir, out_dir, "stem_2")

                self.assertIsNotNone(out_path)
                self.assertEqual(pick.style_preset, "cyber_violet")

                called_config = mock_render.call_args.kwargs["config"]
                violet_preset = get_style_preset("cyber_violet")
                self.assertEqual(called_config["layout"]["border_color"], violet_preset["layout"]["border_color"])
                self.assertEqual(called_config["layout"]["corner_radius"], violet_preset["layout"]["corner_radius"])
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
