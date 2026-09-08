"""
render.py — KenauShorts 1080x1920 Short-Form Card Renderer.

Composites a video clip + headline into a clean 9:16 portrait card format:
- Centered content block with account header (avatar, name, verified mark, handle)
- Monospace bold headline
- Source video clip in a rounded rectangle with custom border
- High-performance ffmpeg composition with hardware encoder detection (VideoToolbox / NVENC / QSV / libx264)
- Cross-platform font detection (Windows, macOS, Linux)
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageFont, ImageChops
except ImportError:
    sys.exit("Pillow is required: pip install Pillow")

LOG = logging.getLogger("kenaushorts.render")

DEFAULT_LAYOUT: dict[str, Any] = {
    "canvas_width": 1080,
    "canvas_height": 1920,
    "background": "#000000",
    "side_margin": 72,
    "video_margin": None,
    "vertical_bias": 0.40,
    "avatar_size": 96,
    "avatar_zoom": 1.25,
    "avatar_trim": True,
    "header_gap": 22,
    "name_size": 44,
    "handle_size": 36,
    "name_color": "#FFFFFF",
    "handle_color": "#8B98A5",
    "verified_color": "#1D9BF0",
    "verified_size": 40,
    "header_to_headline": 48,
    "headline_size": 76,
    "headline_color": "#FFFFFF",
    "headline_line_spacing": 14,
    "headline_wrap_chars": 0,
    "headline_uppercase": True,
    "headline_to_video": 52,
    "video_aspect": "16:9",
    "corner_radius": 40,
    "border_width": 6,
    "border_color": "#1D9BF0",
    "mono_font": "auto",
    "ui_font": "auto",
}

def _get_platform_font_candidates() -> tuple[list[tuple[str, int]], list[tuple[str, int]], list[tuple[str, int]]]:
    """Returns (mono_candidates, ui_bold_candidates, ui_regular_candidates) across OSes."""
    sys_name = platform.system()
    mono: list[tuple[str, int]] = []
    ui_bold: list[tuple[str, int]] = []
    ui_reg: list[tuple[str, int]] = []

    if sys_name == "Darwin":
        mono = [
            ("/System/Library/Fonts/Supplemental/Courier New Bold.ttf", 0),
            ("/Library/Fonts/Courier New Bold.ttf", 0),
            ("/System/Library/Fonts/Menlo.ttc", 1),
            ("/System/Library/Fonts/SFNSMono.ttf", 0),
        ]
        ui_bold = [
            ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
            ("/Library/Fonts/Arial Bold.ttf", 0),
            ("/System/Library/Fonts/HelveticaNeue.ttc", 1),
            ("/System/Library/Fonts/SFNS.ttf", 0),
        ]
        ui_reg = [
            ("/System/Library/Fonts/Supplemental/Arial.ttf", 0),
            ("/System/Library/Fonts/HelveticaNeue.ttc", 0),
            ("/System/Library/Fonts/SFNS.ttf", 0),
        ]
    elif sys_name == "Windows":
        win_dir = os.environ.get("WINDIR", r"C:\Windows")
        fonts_dir = Path(win_dir) / "Fonts"
        mono = [
            (str(fonts_dir / "consolab.ttf"), 0),
            (str(fonts_dir / "courbd.ttf"), 0),
            (str(fonts_dir / "consola.ttf"), 0),
        ]
        ui_bold = [
            (str(fonts_dir / "segoeuib.ttf"), 0),
            (str(fonts_dir / "arialbd.ttf"), 0),
        ]
        ui_reg = [
            (str(fonts_dir / "segoeui.ttf"), 0),
            (str(fonts_dir / "arial.ttf"), 0),
        ]

    # Universal Linux / Fallbacks
    mono.extend([
        ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 0),
        ("/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf", 0),
        ("/usr/share/fonts/truetype/freefont/FreeMonoBold.ttf", 0),
        ("/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf", 0),
    ])
    ui_bold.extend([
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 0),
        ("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", 0),
    ])
    ui_reg.extend([
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", 0),
        ("/usr/share/fonts/TTF/DejaVuSans.ttf", 0),
    ])

    return mono, ui_bold, ui_reg

MONO_CANDIDATES, UI_CANDIDATES, UI_REGULAR_CANDIDATES = _get_platform_font_candidates()
_RESOLVED_FONTS: dict[str, str] = {}

def load_font(
    candidates: list[tuple[str, int]],
    size: int,
    explicit: str = "",
    role: str = "",
) -> ImageFont.FreeTypeFont:
    """Load the first available font face, honoring font collections."""
    attempts: list[tuple[str, int]] = []
    if explicit and explicit != "auto":
        path, _, idx = explicit.partition("#")
        attempts.append((path, int(idx) if idx.isdigit() else 0))
    attempts += candidates

    for path, index in attempts:
        if not os.path.exists(path):
            continue
        try:
            font = ImageFont.truetype(path, size, index=index)
            if role:
                name = " ".join(str(n) for n in font.getname() if n)
                _RESOLVED_FONTS[role] = f"{name} ({path}#{index})"
            return font
        except OSError:
            continue

    if role:
        _RESOLVED_FONTS[role] = "Default bitmap font"
    return ImageFont.load_default()

def parse_aspect(spec: str) -> float:
    spec = str(spec).strip()
    if ":" in spec:
        w, h = spec.split(":", 1)
        return float(w) / float(h)
    return float(spec)

@dataclass
class Account:
    name: str = "KenauShorts"
    handle: str = "@kenaushorts"
    avatar: str = ""
    verified: bool = True

@dataclass
class RenderResult:
    output: Path
    overlay: Path
    video_box: tuple[int, int, int, int]
    duration: float
    poster: Path | None = None

def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]

def wrap_headline(
    text: str,
    font,
    max_width: int,
    draw: ImageDraw.ImageDraw,
    forced_chars: int = 0,
) -> list[str]:
    if forced_chars:
        return textwrap.wrap(text, width=forced_chars) or [""]
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if _text_size(draw, trial, font)[0] <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines

def draw_verified_badge(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, color: str) -> None:
    r = size / 2
    lobes = 8
    lobe_r = r * 0.30
    for i in range(lobes):
        angle = (2 * math.pi / lobes) * i
        lx = cx + math.cos(angle) * (r - lobe_r * 0.55)
        ly = cy + math.sin(angle) * (r - lobe_r * 0.55)
        draw.ellipse([lx - lobe_r, ly - lobe_r, lx + lobe_r, ly + lobe_r], fill=color)
    draw.ellipse([cx - r * 0.82, cy - r * 0.82, cx + r * 0.82, cy + r * 0.82], fill=color)

    w = max(2, int(size * 0.10))
    draw.line(
        [
            (cx - r * 0.34, cy + r * 0.03),
            (cx - r * 0.08, cy + r * 0.30),
            (cx + r * 0.38, cy - r * 0.28),
        ],
        fill="#FFFFFF",
        width=w,
        joint="curve",
    )

def trim_uniform_border(img: Image.Image, tolerance: int = 12) -> Image.Image:
    rgb = img.convert("RGB")
    w, h = rgb.size
    corners = [rgb.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    first = corners[0]
    if any(max(abs(a - b) for a, b in zip(first, c)) > tolerance for c in corners):
        return img
    bg = Image.new("RGB", rgb.size, first)
    diff = ImageChops.difference(rgb, bg).convert("L").point(lambda p: 255 if p > tolerance else 0)
    box = diff.getbbox()
    if not box:
        return img
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half = max(x1 - x0, y1 - y0) / 2 * 1.12
    half = max(half, 8)
    return img.crop((int(max(0, cx - half)), int(max(0, cy - half)), int(min(w, cx + half)), int(min(h, cy + half))))

def make_avatar(path: str, size: int, fallback_letter: str, trim: bool = True, zoom: float = 1.0) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)

    if path and os.path.exists(path):
        try:
            src = Image.open(path).convert("RGBA")
            if trim:
                src = trim_uniform_border(src)
            sw, sh = src.size
            side = min(sw, sh)
            side = max(8, int(side / max(zoom, 0.01)))
            src = src.crop((
                (sw - side) // 2,
                (sh - side) // 2,
                (sw - side) // 2 + side,
                (sh - side) // 2 + side,
            )).resize((size, size), Image.LANCZOS)
            img.paste(src, (0, 0))
            img.putalpha(mask)
            return img
        except OSError:
            pass

    # Clean fallback letter circle
    draw = ImageDraw.Draw(img)
    draw.ellipse([0, 0, size - 1, size - 1], fill="#1D9BF0")
    font = load_font(UI_CANDIDATES, int(size * 0.5))
    letter = (fallback_letter or "K")[0].upper()
    box = draw.textbbox((0, 0), letter, font=font)
    draw.text(
        ((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1]),
        letter,
        font=font,
        fill="#FFFFFF",
    )
    return img

def rounded_mask(w: int, h: int, radius: int, supersample: int = 4) -> Image.Image:
    big = Image.new("L", (w * supersample, h * supersample), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        [0, 0, w * supersample - 1, h * supersample - 1],
        radius=radius * supersample,
        fill=255,
    )
    return big.resize((w, h), Image.LANCZOS)

def build_overlay(
    headline: str,
    account: Account,
    layout: dict[str, Any],
    out_path: Path,
) -> tuple[int, int, int, int]:
    W = int(layout["canvas_width"])
    H = int(layout["canvas_height"])
    margin = int(layout["side_margin"])
    content_w = W - margin * 2

    canvas = Image.new("RGBA", (W, H), layout["background"])
    draw = ImageDraw.Draw(canvas)

    mono = load_font(MONO_CANDIDATES, int(layout["headline_size"]), layout.get("mono_font", ""), role="headline")
    name_font = load_font(UI_CANDIDATES, int(layout["name_size"]), layout.get("ui_font", ""), role="name")
    handle_font = load_font(UI_REGULAR_CANDIDATES, int(layout["handle_size"]), role="handle")

    avatar_size = int(layout["avatar_size"])
    header_h = max(avatar_size, int(layout["name_size"]) + int(layout["handle_size"]) + 12)

    text = headline.upper() if layout.get("headline_uppercase", True) else headline
    lines = wrap_headline(text, mono, content_w, draw, int(layout.get("headline_wrap_chars") or 0))
    line_h = int(layout["headline_size"]) + int(layout["headline_line_spacing"])
    headline_h = line_h * len(lines)

    border = int(layout["border_width"])
    aspect = parse_aspect(layout["video_aspect"])
    vid_margin = layout.get("video_margin")
    vid_margin = margin if vid_margin is None else int(vid_margin)
    frame_w = W - vid_margin * 2
    frame_h = int(round(frame_w / aspect))

    block_h = (
        header_h
        + int(layout["header_to_headline"])
        + headline_h
        + int(layout["headline_to_video"])
        + frame_h
    )
    bias = float(layout.get("vertical_bias", 0.40))
    top = max(0, int((H - block_h) * min(max(bias, 0.0), 1.0)))

    # Header
    y = top
    avatar = make_avatar(
        account.avatar,
        avatar_size,
        account.name,
        trim=bool(layout.get("avatar_trim", True)),
        zoom=float(layout.get("avatar_zoom", 1.0)),
    )
    canvas.alpha_composite(avatar, (margin, y + (header_h - avatar_size) // 2))

    text_x = margin + avatar_size + int(layout["header_gap"])
    name_y = y + (header_h - (int(layout["name_size"]) + int(layout["handle_size"]) + 12)) // 2
    draw.text((text_x, name_y), account.name, font=name_font, fill=layout["name_color"])

    if account.verified:
        name_w = _text_size(draw, account.name, name_font)[0]
        badge = int(layout["verified_size"])
        draw_verified_badge(
            draw,
            cx=text_x + name_w + 14 + badge // 2,
            cy=name_y + int(layout["name_size"]) * 0.55,
            size=badge,
            color=layout["verified_color"],
        )

    draw.text((text_x, name_y + int(layout["name_size"]) + 12), account.handle, font=handle_font, fill=layout["handle_color"])

    # Headline
    y = top + header_h + int(layout["header_to_headline"])
    for line in lines:
        draw.text((margin, y), line, font=mono, fill=layout["headline_color"])
        y += line_h

    # Video Frame Window
    frame_x = vid_margin
    frame_y = top + header_h + int(layout["header_to_headline"]) + headline_h + int(layout["headline_to_video"])
    radius = int(layout["corner_radius"])

    hole = rounded_mask(frame_w, frame_h, radius)
    canvas.paste((0, 0, 0, 0), (frame_x, frame_y), hole)
    draw.rounded_rectangle(
        [frame_x, frame_y, frame_x + frame_w - 1, frame_y + frame_h - 1],
        radius=radius,
        outline=layout["border_color"],
        width=border,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return frame_x, frame_y, frame_w, frame_h

_ENCODERS_CACHE: str | None = None

def has_encoder(name: str) -> bool:
    global _ENCODERS_CACHE
    if _ENCODERS_CACHE is None:
        try:
            _ENCODERS_CACHE = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, text=True, check=False,
            ).stdout
        except FileNotFoundError:
            _ENCODERS_CACHE = ""
    return name in (_ENCODERS_CACHE or "")

def video_encoder_args(config: dict[str, Any]) -> list[str]:
    posting = config.get("posting", {})
    choice = posting.get("encoder", "auto")
    bitrate = str(posting.get("video_bitrate", "6M"))
    preset = posting.get("x264_preset", "veryfast")
    crf = str(posting.get("crf", 20))

    if choice == "auto":
        if has_encoder("h264_videotoolbox"):
            return ["-c:v", "h264_videotoolbox", "-b:v", bitrate, "-profile:v", "high"]
        elif has_encoder("h264_nvenc"):
            return ["-c:v", "h264_nvenc", "-b:v", bitrate, "-preset", "p4"]
        elif has_encoder("h264_qsv"):
            return ["-c:v", "h264_qsv", "-b:v", bitrate]
        return ["-c:v", "libx264", "-preset", preset, "-crf", crf]

    if choice == "h264_videotoolbox" and has_encoder("h264_videotoolbox"):
        return ["-c:v", "h264_videotoolbox", "-b:v", bitrate, "-profile:v", "high"]
    if choice == "h264_nvenc" and has_encoder("h264_nvenc"):
        return ["-c:v", "h264_nvenc", "-b:v", bitrate]

    return ["-c:v", "libx264", "-preset", preset, "-crf", crf]

def probe_duration(video: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return float(out)
    except Exception:
        return 0.0

def composite(
    video: Path,
    overlay: Path,
    box: tuple[int, int, int, int],
    layout: dict[str, Any],
    out: Path,
    max_seconds: float | None = None,
    start_seconds: float = 0.0,
    poster: Path | None = None,
    config: dict[str, Any] | None = None,
    music: Path | None = None,
) -> float:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg was not found on your PATH. Please install ffmpeg.")

    W = int(layout["canvas_width"])
    H = int(layout["canvas_height"])
    x, y, w, h = box

    fchain = (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},"
        f"pad={W}:{H}:{x}:{y}:color={layout['background']},"
        f"setsar=1[stage];"
        f"[stage][1:v]overlay=0:0:format=auto,format=yuv420p[v]"
    )

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if start_seconds:
        cmd += ["-ss", str(start_seconds)]
    cmd += ["-i", str(video), "-loop", "1", "-i", str(overlay)]

    if max_seconds:
        cmd += ["-t", str(max_seconds)]

    cmd += ["-filter_complex", fchain, "-map", "[v]"]
    cmd += video_encoder_args(config or {})
    cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]

    LOG.info("Rendering short: %s", " ".join(cmd))
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=True)

    dur = probe_duration(out)
    if poster:
        poster.parent.mkdir(parents=True, exist_ok=True)
        mid = max(0.5, dur / 2)
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(mid), "-i", str(out), "-vframes", "1", "-q:v", "2", str(poster)],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    return dur

def render(
    video: Path,
    headline: str,
    out: Path,
    config: dict[str, Any] | None = None,
    account: Account | None = None,
    max_seconds: float | None = None,
    poster: Path | None = None,
) -> RenderResult:
    cfg = config or {}
    layout = {**DEFAULT_LAYOUT, **cfg.get("layout", {})}
    acct = account or Account(**{k: v for k, v in cfg.get("account", {}).items() if k in ("name", "handle", "avatar", "verified")})

    overlay_path = out.parent / f"{out.stem}_overlay.png"
    box = build_overlay(headline, acct, layout, overlay_path)
    dur = composite(video, overlay_path, box, layout, out, max_seconds=max_seconds, poster=poster, config=cfg)

    return RenderResult(output=out, overlay=overlay_path, video_box=box, duration=dur, poster=poster)
