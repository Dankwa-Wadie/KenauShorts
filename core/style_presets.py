"""
core/style_presets.py — Visual style presets for KenauShorts video cards.

Provides pre-defined style variations (border color, corner radius, video aspect ratio,
background crop anchors) that add automatic visual variety across rendered shorts.
"""

from __future__ import annotations

import copy
import logging
import random
import subprocess
from pathlib import Path
from typing import Any

LOG = logging.getLogger("kenaushorts.style_presets")

STYLE_PRESETS: list[dict[str, Any]] = [
    {
        "name": "classic_blue",
        "label": "Classic Blue",
        "description": "Standard 16:9 landscape video frame with signature electric blue border and smooth rounded corners.",
        "layout": {
            "border_color": "#1D9BF0",
            "corner_radius": 40,
            "video_aspect": "16:9",
        },
        "story_layout": {
            "card_radius": 36,
            "background_zoom": 1.0,
            "background_anchor_x": 0.5,
            "background_anchor_y": 0.5,
        },
    },
    {
        "name": "warm_amber",
        "label": "Warm Amber",
        "description": "4:3 frame with warm amber/gold border, tighter corners, and slight upward crop bias.",
        "layout": {
            "border_color": "#F59E0B",
            "corner_radius": 28,
            "video_aspect": "4:3",
            "background_anchor_y": 0.45,
        },
        "story_layout": {
            "card_radius": 28,
            "background_zoom": 1.05,
            "background_anchor_x": 0.5,
            "background_anchor_y": 0.45,
        },
    },
    {
        "name": "emerald_compact",
        "label": "Emerald Compact",
        "description": "1:1 square video frame with emerald green border and prominent rounded corners.",
        "layout": {
            "border_color": "#10B981",
            "corner_radius": 48,
            "video_aspect": "1:1",
        },
        "story_layout": {
            "card_radius": 44,
            "background_zoom": 1.0,
            "background_anchor_x": 0.5,
            "background_anchor_y": 0.5,
        },
    },
    {
        "name": "cyber_violet",
        "label": "Cyber Violet",
        "description": "4:5 portrait-biased frame with cyber violet border, subtle zoom, and sleek 32px corners.",
        "layout": {
            "border_color": "#8B5CF6",
            "corner_radius": 32,
            "video_aspect": "4:5",
            "background_zoom": 1.05,
        },
        "story_layout": {
            "card_radius": 32,
            "background_zoom": 1.1,
            "background_anchor_x": 0.5,
            "background_anchor_y": 0.5,
        },
    },
]

PRESETS_BY_NAME: dict[str, dict[str, Any]] = {p["name"]: p for p in STYLE_PRESETS}


def get_style_preset(name: str) -> dict[str, Any] | None:
    """Retrieve a style preset by its name identifier."""
    return PRESETS_BY_NAME.get(name)


def choose_style_preset() -> dict[str, Any]:
    """Randomly select one style preset."""
    return random.choice(STYLE_PRESETS)


def apply_style_preset(config: dict[str, Any], preset: dict[str, Any]) -> dict[str, Any]:
    """
    Merge preset layout and story_layout overrides over the base config.
    Returns a deep copy of config with overrides applied without mutating the original.
    """
    cfg = copy.deepcopy(config)
    if "layout" in preset:
        cfg_layout = cfg.setdefault("layout", {})
        for k, v in preset["layout"].items():
            cfg_layout[k] = v
    if "story_layout" in preset:
        cfg_story = cfg.setdefault("story_layout", {})
        for k, v in preset["story_layout"].items():
            cfg_story[k] = v
    return cfg


def parse_aspect(spec: str | float) -> float:
    """Parse aspect ratio specification (e.g. '16:9' or 1.777) to float."""
    if isinstance(spec, (int, float)):
        return float(spec)
    spec = str(spec).strip()
    if ":" in spec:
        w, h = spec.split(":", 1)
        return float(w) / float(h)
    return float(spec)


def get_source_aspect_ratio(video: Path | str) -> float | None:
    """
    Read the source video's actual width and height via ffprobe,
    returning width/height as a float. Returns None on failure or if file is missing.
    """
    try:
        video_path = Path(video)
        if not video_path.is_file():
            return None

        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        if not out:
            return None

        first_line = out.splitlines()[0].strip()
        parts = first_line.split(",")
        if len(parts) >= 2:
            w = float(parts[0].strip())
            h = float(parts[1].strip())
            if w > 0 and h > 0:
                return w / h
    except Exception as err:
        LOG.warning("ffprobe failed to determine aspect ratio for %s: %s", video, err)
        return None
    return None


def match_style_preset_to_aspect(aspect_ratio: float) -> dict[str, Any]:
    """
    Return the style preset whose video_aspect is numerically closest
    to the given source aspect ratio.
    """
    if not STYLE_PRESETS:
        raise ValueError("STYLE_PRESETS cannot be empty")

    def _diff(preset: dict[str, Any]) -> float:
        spec = preset.get("layout", {}).get("video_aspect", "16:9")
        try:
            val = parse_aspect(spec)
        except Exception:
            val = 16.0 / 9.0
        return abs(val - aspect_ratio)

    return min(STYLE_PRESETS, key=_diff)

