"""
core/style_presets.py — Visual style presets for KenauShorts video cards.

Provides pre-defined style variations (border color, corner radius, video aspect ratio,
background crop anchors) that add automatic visual variety across rendered shorts.
"""

from __future__ import annotations

import copy
import random
from typing import Any

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
