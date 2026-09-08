"""
render_story.py — Story card compositor for KenauShorts.

Renders multi-line text stories, quotes, and image galleries over a looping background/ambient video.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from core.render import (
    Account,
    UI_CANDIDATES,
    UI_REGULAR_CANDIDATES,
    draw_verified_badge,
    has_audio_stream,
    load_font,
    make_avatar,
    probe_duration,
    rounded_mask,
    video_encoder_args,
    wrap_headline,
)

LOG = logging.getLogger("kenaushorts.render_story")

DEFAULT_STORY_LAYOUT: dict[str, Any] = {
    "canvas_width": 1080,
    "canvas_height": 1920,
    "background": "#000000",
    "card_width": 936,
    "card_padding": 48,
    "card_radius": 36,
    "card_background": "#16181C",
    "card_opacity": 0.90,
    "background_dim": 0.35,
    "background_zoom": 1.0,
    "vertical_bias": 0.42,
    "avatar_size": 84,
    "name_size": 40,
    "handle_size": 32,
    "name_color": "#FFFFFF",
    "handle_color": "#8B98A5",
    "verified_color": "#1D9BF0",
    "verified_size": 36,
    "story_size": 42,
    "story_color": "#FFFFFF",
    "commentary_size": 36,
    "commentary_color": "#D0D5DD",
}

def build_story_overlay(
    headline: str,
    commentary: str,
    images: list[Path],
    account: Account,
    layout: dict[str, Any],
    out_path: Path,
) -> Path:
    W = int(layout["canvas_width"])
    H = int(layout["canvas_height"])
    card_w = int(layout["card_width"])
    pad = int(layout["card_padding"])
    radius = int(layout["card_radius"])

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # Scrim
    dim = float(layout.get("background_dim", 0.35))
    if dim > 0:
        alpha = int(255 * min(max(dim, 0.0), 1.0))
        scrim = Image.new("RGBA", (W, H), (0, 0, 0, alpha))
        canvas.alpha_composite(scrim)

    name_font = load_font(UI_CANDIDATES, int(layout["name_size"]), role="story_name")
    handle_font = load_font(UI_REGULAR_CANDIDATES, int(layout["handle_size"]), role="story_handle")
    story_font = load_font(UI_CANDIDATES, int(layout["story_size"]), role="story_text")
    comment_font = load_font(UI_REGULAR_CANDIDATES, int(layout["commentary_size"]), role="comment_text")

    inner_w = card_w - pad * 2
    story_lines = wrap_headline(headline, story_font, inner_w, draw)
    story_h = len(story_lines) * int(layout["story_size"] * 1.35)

    comment_lines = wrap_headline(commentary, comment_font, inner_w - 32, draw) if commentary else []
    comment_h = len(comment_lines) * int(layout["commentary_size"] * 1.3) + 24 if comment_lines else 0

    header_h = max(int(layout["avatar_size"]), int(layout["name_size"]) + int(layout["handle_size"]) + 8)

    card_h = pad + header_h + 32 + story_h + (comment_h + 24 if comment_h else 0) + pad
    bias = float(layout.get("vertical_bias", 0.42))
    card_x = (W - card_w) // 2
    card_y = max(0, int((H - card_h) * bias))

    # Draw card background
    card_layer = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
    card_mask = rounded_mask(card_w, card_h, radius)
    opacity = int(255 * float(layout.get("card_opacity", 0.90)))
    bg_fill = (22, 24, 28, opacity)
    ImageDraw.Draw(card_layer).rounded_rectangle([0, 0, card_w - 1, card_h - 1], radius=radius, fill=bg_fill)
    canvas.alpha_composite(card_layer, (card_x, card_y))

    # Draw Header inside card
    avatar = make_avatar(account.avatar, int(layout["avatar_size"]), account.name)
    canvas.alpha_composite(avatar, (card_x + pad, card_y + pad))

    tx = card_x + pad + int(layout["avatar_size"]) + 20
    ty = card_y + pad + 6
    draw.text((tx, ty), account.name, font=name_font, fill=layout["name_color"])
    if account.verified:
        bw = draw.textbbox((0, 0), account.name, font=name_font)[2]
        badge_sz = int(layout["verified_size"])
        draw_verified_badge(draw, tx + bw + 14 + badge_sz // 2, ty + badge_sz // 2 + 4, badge_sz, layout["verified_color"])
    draw.text((tx, ty + int(layout["name_size"]) + 6), account.handle, font=handle_font, fill=layout["handle_color"])

    # Draw Story
    sy = card_y + pad + header_h + 32
    for line in story_lines:
        draw.text((card_x + pad, sy), line, font=story_font, fill=layout["story_color"])
        sy += int(layout["story_size"] * 1.35)

    # Draw Commentary Quote Box if present
    if comment_lines:
        sy += 16
        draw.line([card_x + pad, sy, card_x + pad, sy + comment_h], fill=layout["verified_color"], width=6)
        cy = sy + 6
        for line in comment_lines:
            draw.text((card_x + pad + 24, cy), line, font=comment_font, fill=layout["commentary_color"])
            cy += int(layout["commentary_size"] * 1.3)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return out_path

def render_story(
    headline: str,
    commentary: str,
    images: list[Path],
    mascot: Path,
    out: Path,
    poster: Path | None = None,
    config: dict[str, Any] | None = None,
    duration: float = 25.0,
    music: Path | None = None,
) -> float:
    cfg = config or {}
    layout = {**DEFAULT_STORY_LAYOUT, **cfg.get("story_layout", {})}
    acct = Account(**{k: v for k, v in cfg.get("account", {}).items() if k in ("name", "handle", "avatar", "verified")})

    overlay = out.parent / f"{out.stem}_story_overlay.png"
    build_story_overlay(headline, commentary, images, acct, layout, overlay)

    W = int(layout["canvas_width"])
    H = int(layout["canvas_height"])

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-stream_loop", "-1", "-i", str(mascot),
        "-i", str(overlay),
        "-t", str(duration),
        "-filter_complex", f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[bg];[bg][1:v]overlay=0:0[v]",
        "-map", "[v]",
    ]
    cmd += video_encoder_args(cfg)
    cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]

    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=True)

    if poster:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "1.0", "-i", str(out), "-vframes", "1", "-q:v", "2", str(poster)],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    return duration
