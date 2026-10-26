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
    from PIL import Image, ImageDraw, ImageFont, ImageChops, ImageFilter
except ImportError:
    sys.exit("Pillow is required: pip install Pillow")

from core.style_presets import (
    STYLE_PRESETS,
    apply_style_preset,
    choose_style_preset,
    get_source_aspect_ratio,
    get_style_preset,
    match_style_preset_to_aspect,
)

LOG = logging.getLogger("kenaushorts.render")

PRESENTATION_MODE_CARD = "card"
PRESENTATION_MODE_FOOTAGE_CLEAN = "footage_clean"
PRESENTATION_MODE_FOOTAGE_CAPTION = "footage_caption"

SUPPORTED_PRESENTATION_MODES = {
    PRESENTATION_MODE_CARD,
    PRESENTATION_MODE_FOOTAGE_CLEAN,
}

RESERVED_PRESENTATION_MODES = {
    PRESENTATION_MODE_FOOTAGE_CAPTION,
}

VALID_PRESENTATION_MODES = SUPPORTED_PRESENTATION_MODES | RESERVED_PRESENTATION_MODES

DEFAULT_PRESENTATION_MODE = PRESENTATION_MODE_CARD

FOOTAGE_FIT_AUTO = "auto"
FOOTAGE_FIT_COVER = "cover"
FOOTAGE_FIT_CONTAIN = "contain"

VALID_FOOTAGE_FIT = {
    FOOTAGE_FIT_AUTO,
    FOOTAGE_FIT_COVER,
    FOOTAGE_FIT_CONTAIN,
}

DEFAULT_FOOTAGE_FIT = FOOTAGE_FIT_AUTO

CARD_BACKGROUND_MODE_SOLID = "solid"
CARD_BACKGROUND_MODE_BLURRED_AMBIENT = "blurred_ambient"

VALID_CARD_BACKGROUND_MODES = {
    CARD_BACKGROUND_MODE_SOLID,
    CARD_BACKGROUND_MODE_BLURRED_AMBIENT,
}

DEFAULT_CARD_BACKGROUND_MODE = CARD_BACKGROUND_MODE_SOLID
DEFAULT_AMBIENT_DIM = 0.45
DEFAULT_AMBIENT_BLUR = 10
DEFAULT_CARD_SHADOW = False
DEFAULT_SHADOW_BLUR = 24
DEFAULT_SHADOW_OPACITY = 0.40
DEFAULT_SHADOW_OFFSET_Y = 8

DEFAULT_LAYOUT: dict[str, Any] = {
    "canvas_width": 1080,
    "canvas_height": 1920,
    "background": "#000000",
    "presentation_mode": DEFAULT_PRESENTATION_MODE,
    "footage_fit": DEFAULT_FOOTAGE_FIT,
    "card_background_mode": DEFAULT_CARD_BACKGROUND_MODE,
    "ambient_dim": DEFAULT_AMBIENT_DIM,
    "ambient_blur": DEFAULT_AMBIENT_BLUR,
    "card_shadow": DEFAULT_CARD_SHADOW,
    "shadow_blur": DEFAULT_SHADOW_BLUR,
    "shadow_opacity": DEFAULT_SHADOW_OPACITY,
    "shadow_offset_y": DEFAULT_SHADOW_OFFSET_Y,
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
    overlay: Path | None
    video_box: tuple[int, int, int, int]
    duration: float
    poster: Path | None = None
    presentation_mode: str = DEFAULT_PRESENTATION_MODE
    footage_fit: str = DEFAULT_FOOTAGE_FIT
    card_background_mode: str = DEFAULT_CARD_BACKGROUND_MODE
    card_shadow: bool = DEFAULT_CARD_SHADOW

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

    raw_bg_mode = layout.get("card_background_mode", DEFAULT_CARD_BACKGROUND_MODE)
    if not isinstance(raw_bg_mode, str):
        raise ValueError(f"Invalid card_background_mode: expected string, got {type(raw_bg_mode).__name__}")
    card_bg_mode = raw_bg_mode.strip().lower()
    if card_bg_mode not in VALID_CARD_BACKGROUND_MODES:
        raise ValueError(f"Invalid card_background_mode: '{raw_bg_mode}'. Must be one of {sorted(VALID_CARD_BACKGROUND_MODES)}")

    if card_bg_mode == CARD_BACKGROUND_MODE_BLURRED_AMBIENT:
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    else:
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

    raw_shadow = layout.get("card_shadow", DEFAULT_CARD_SHADOW)
    if isinstance(raw_shadow, str):
        lowered = raw_shadow.strip().lower()
        if lowered in ("true", "1", "yes", "on", "enable", "enabled"):
            use_shadow = True
        elif lowered in ("false", "0", "no", "off", "disable", "disabled", "none", ""):
            use_shadow = False
        else:
            raise ValueError(f"Invalid card_shadow: expected boolean, got '{raw_shadow}'")
    elif isinstance(raw_shadow, bool):
        use_shadow = raw_shadow
    elif raw_shadow is None:
        use_shadow = False
    else:
        raise ValueError(f"Invalid card_shadow: expected boolean, got {type(raw_shadow).__name__}")

    if use_shadow:
        raw_sblur = layout.get("shadow_blur", DEFAULT_SHADOW_BLUR)
        if not isinstance(raw_sblur, int) or isinstance(raw_sblur, bool) or not (1 <= raw_sblur <= 100):
            raise ValueError(f"Invalid shadow_blur: expected integer between 1 and 100, got {raw_sblur}")
        shadow_blur = int(raw_sblur)

        raw_sopacity = layout.get("shadow_opacity", DEFAULT_SHADOW_OPACITY)
        if not isinstance(raw_sopacity, (int, float)) or isinstance(raw_sopacity, bool) or not (0.0 <= raw_sopacity <= 1.0):
            raise ValueError(f"Invalid shadow_opacity: expected float between 0.0 and 1.0, got {raw_sopacity}")
        shadow_opacity = float(raw_sopacity)

        raw_soffset_y = layout.get("shadow_offset_y", DEFAULT_SHADOW_OFFSET_Y)
        if not isinstance(raw_soffset_y, int) or isinstance(raw_soffset_y, bool) or not (-50 <= raw_soffset_y <= 50):
            raise ValueError(f"Invalid shadow_offset_y: expected integer between -50 and 50, got {raw_soffset_y}")
        shadow_offset_y = int(raw_soffset_y)

        shadow_mask = Image.new("L", (W, H), 0)
        draw_m = ImageDraw.Draw(shadow_mask)
        draw_m.rounded_rectangle(
            [frame_x, frame_y + shadow_offset_y, frame_x + frame_w - 1, frame_y + frame_h - 1 + shadow_offset_y],
            radius=radius,
            fill=int(255 * shadow_opacity),
        )
        shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(radius=shadow_blur))
        shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        shadow_layer.paste((0, 0, 0, 255), (0, 0), shadow_mask)
        canvas.alpha_composite(shadow_layer)

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

RENDER_TIMEOUT: float = 300.0  # 5 minutes max for short video composite
POSTER_TIMEOUT: float = 30.0
PROBE_TIMEOUT: float = 15.0
ENCODER_TIMEOUT: float = 10.0

_ENCODERS_CACHE: str | None = None
_WORKING_ENCODERS: dict[str, bool] = {}

def has_encoder(name: str) -> bool:
    global _ENCODERS_CACHE
    if _ENCODERS_CACHE is None:
        try:
            _ENCODERS_CACHE = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, text=True, check=False, timeout=ENCODER_TIMEOUT,
            ).stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            _ENCODERS_CACHE = ""

    if name not in (_ENCODERS_CACHE or ""):
        return False

    # For hardware-accelerated encoders, probe if driver/hardware can actually initialize
    if name in ("h264_nvenc", "h264_videotoolbox", "h264_qsv", "hevc_nvenc"):
        if name not in _WORKING_ENCODERS:
            try:
                probe = subprocess.run(
                    ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                     "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.04",
                     "-c:v", name, "-f", "null", "-"],
                    capture_output=True, check=False, timeout=ENCODER_TIMEOUT,
                )
                _WORKING_ENCODERS[name] = (probe.returncode == 0)
            except Exception:
                _WORKING_ENCODERS[name] = False
        return _WORKING_ENCODERS[name]

    return True

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
            capture_output=True, text=True, check=True, timeout=PROBE_TIMEOUT,
        ).stdout.strip()
        return float(out)
    except Exception:
        return 0.0

def has_audio_stream(video: Path) -> bool:
    """Whether the source has an audio track — decides how music is mixed."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True, timeout=PROBE_TIMEOUT,
        ).stdout.strip()
        return bool(out)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False

def composite(
    video: Path,
    overlay: Path | None,
    box: tuple[int, int, int, int] | None,
    layout: dict[str, Any],
    out: Path,
    max_seconds: float | None = None,
    start_seconds: float = 0.0,
    poster: Path | None = None,
    config: dict[str, Any] | None = None,
    music: Path | None = None,
    loop_video: bool = False,
    timeout: float = RENDER_TIMEOUT,
    presentation_mode: str | None = None,
) -> float:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg was not found on your PATH. Please install ffmpeg.")

    W = int(layout.get("canvas_width", 1080))
    H = int(layout.get("canvas_height", 1920))

    mode = presentation_mode or layout.get("presentation_mode") or ((config or {}).get("presentation_mode")) or DEFAULT_PRESENTATION_MODE
    if not isinstance(mode, str):
        raise ValueError(f"Invalid presentation_mode: expected string, got {type(mode).__name__}")
    mode = mode.strip().lower()
    if mode not in VALID_PRESENTATION_MODES:
        raise ValueError(f"Invalid presentation_mode: '{mode}'. Must be one of {sorted(VALID_PRESENTATION_MODES)}")

    is_clean = (mode == PRESENTATION_MODE_FOOTAGE_CLEAN) or (overlay is None)

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if start_seconds:
        cmd += ["-ss", str(start_seconds)]
    if loop_video:
        cmd += ["-stream_loop", "-1"]
    cmd += ["-i", str(video)]

    if is_clean:
        music_input_idx = 1
        footage_fit = str(layout.get("footage_fit", DEFAULT_FOOTAGE_FIT)).strip().lower()
        if footage_fit not in VALID_FOOTAGE_FIT:
            footage_fit = DEFAULT_FOOTAGE_FIT

        use_cover = False
        if footage_fit == "cover":
            use_cover = True
        elif footage_fit == "contain":
            use_cover = False
        else:
            bg_zoom = float(layout.get("background_zoom", 1.0))
            src_aspect = get_source_aspect_ratio(video)
            if bg_zoom > 1.0:
                use_cover = True
            elif src_aspect is not None and src_aspect <= 0.75:
                use_cover = True
            else:
                use_cover = False

        if use_cover:
            zoom = max(1.0, float(layout.get("background_zoom", 1.0)))
            ax = min(max(float(layout.get("background_anchor_x", 0.5)), 0.0), 1.0)
            ay = min(max(float(layout.get("background_anchor_y", 0.5)), 0.0), 1.0)
            sw, sh = int(W * zoom), int(H * zoom)
            fchain = (
                f"[0:v]scale={sw}:{sh}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H}:(in_w-out_w)*{ax}:(in_h-out_h)*{ay},"
                f"setsar=1,format=yuv420p[v]"
            )
        else:
            bg_color = layout.get("background", "#000000")
            fchain = (
                f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={bg_color},"
                f"setsar=1,format=yuv420p[v]"
            )
    else:
        cmd += ["-loop", "1", "-i", str(overlay)]
        music_input_idx = 2
        x, y, w, h = box if box else (0, 0, W, H)

        zoom = max(1.0, float(layout.get("background_zoom", 1.0)))
        ax = min(max(float(layout.get("background_anchor_x", 0.5)), 0.0), 1.0)
        ay = min(max(float(layout.get("background_anchor_y", 0.5)), 0.0), 1.0)
        sw, sh = int(w * zoom), int(h * zoom)

        raw_bg_mode = layout.get("card_background_mode", DEFAULT_CARD_BACKGROUND_MODE)
        if not isinstance(raw_bg_mode, str):
            raise ValueError(f"Invalid card_background_mode: expected string, got {type(raw_bg_mode).__name__}")
        card_bg_mode = raw_bg_mode.strip().lower()
        if card_bg_mode not in VALID_CARD_BACKGROUND_MODES:
            raise ValueError(f"Invalid card_background_mode: '{raw_bg_mode}'. Must be one of {sorted(VALID_CARD_BACKGROUND_MODES)}")

        if card_bg_mode == CARD_BACKGROUND_MODE_BLURRED_AMBIENT:
            raw_dim = layout.get("ambient_dim", DEFAULT_AMBIENT_DIM)
            if not isinstance(raw_dim, (int, float)) or isinstance(raw_dim, bool) or not (0.0 <= raw_dim <= 1.0):
                raise ValueError(f"Invalid ambient_dim: expected float between 0.0 and 1.0, got {raw_dim}")
            ambient_dim = float(raw_dim)

            raw_blur = layout.get("ambient_blur", DEFAULT_AMBIENT_BLUR)
            if not isinstance(raw_blur, int) or isinstance(raw_blur, bool) or not (1 <= raw_blur <= 50):
                raise ValueError(f"Invalid ambient_blur: expected integer between 1 and 50, got {raw_blur}")
            ambient_blur = int(raw_blur)

            dim_filter = f",drawbox=x=0:y=0:w={W}:h={H}:color=black@{ambient_dim:.2f}:t=fill" if ambient_dim > 0 else ""
            fchain = (
                f"[0:v]split=2[bg_src][fg_src];"
                f"[bg_src]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
                f"boxblur={ambient_blur}:3,scale={W}:{H}:flags=bilinear{dim_filter}[bg];"
                f"[fg_src]scale={sw}:{sh}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h}:(in_w-out_w)*{ax}:(in_h-out_h)*{ay},setsar=1[fg];"
                f"[bg][fg]overlay={x}:{y}:format=auto[stage];"
                f"[stage][1:v]overlay=0:0:format=auto,format=yuv420p[v]"
            )
        else:
            fchain = (
                f"[0:v]scale={sw}:{sh}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h}:(in_w-out_w)*{ax}:(in_h-out_h)*{ay},"
                f"pad={W}:{H}:{x}:{y}:color={layout['background']},"
                f"setsar=1[stage];"
                f"[stage][1:v]overlay=0:0:format=auto,format=yuv420p[v]"
            )

    # Music is input when present
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]

    if max_seconds:
        cmd += ["-t", str(max_seconds)]

    audio_cfg = (config or {}).get("audio", {})
    music_vol = float(audio_cfg.get("music_volume", 0.35))
    src_vol = float(audio_cfg.get("source_audio_volume", 1.0))
    fade = float(audio_cfg.get("fade_out_seconds", 1.5))
    src_has_audio = has_audio_stream(video)

    amap: list[str] = []
    if music:
        dur = max_seconds or probe_duration(video) or 30.0
        fade_start = max(0.0, float(dur) - fade)
        if src_has_audio:
            # Duck the music under the original audio rather than replacing it.
            fchain += (
                f";[0:a]volume={src_vol}[a0]"
                f";[{music_input_idx}:a]volume={music_vol},afade=t=out:st={fade_start:.2f}:d={fade}[a1]"
                f";[a0][a1]amix=inputs=2:duration=first:dropout_transition=0,"
                f"aresample=48000[a]"
            )
        else:
            fchain += (
                f";[{music_input_idx}:a]volume={music_vol},afade=t=out:st={fade_start:.2f}:d={fade},"
                f"aresample=48000[a]"
            )
        amap = ["-map", "[a]"]
    elif src_has_audio:
        amap = ["-map", "0:a"]

    cmd += ["-filter_complex", fchain, "-map", "[v]"] + amap
    cmd += video_encoder_args(config or {})
    cmd += [
        "-pix_fmt", "yuv420p",
        "-r", str((config or {}).get("posting", {}).get("fps", 30)),
    ]
    if amap:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
    cmd += ["-movflags", "+faststart", "-shortest", str(out)]

    LOG.info("Rendering short: %s", " ".join(cmd))
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(cmd, check=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        LOG.error("Rendering ffmpeg process timed out after %.1fs: %s", timeout, e)
        out.unlink(missing_ok=True)
        raise TimeoutError(f"Rendering ffmpeg process timed out after {timeout}s") from e
    except subprocess.CalledProcessError:
        out.unlink(missing_ok=True)
        raise

    dur = probe_duration(out)
    if poster:
        poster.parent.mkdir(parents=True, exist_ok=True)
        mid = max(0.5, dur / 2)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(mid), "-i", str(out), "-vframes", "1", "-q:v", "2", str(poster)],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=POSTER_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            LOG.warning("Poster generation timed out after %.1fs", POSTER_TIMEOUT)
            poster.unlink(missing_ok=True)

    return dur

def render(
    video: Path,
    headline: str,
    out: Path,
    config: dict[str, Any] | None = None,
    account: Account | None = None,
    max_seconds: float | None = None,
    poster: Path | None = None,
    music: Path | None = None,
    timeout: float = RENDER_TIMEOUT,
    presentation_mode: str | None = None,
    footage_fit: str | None = None,
    card_background_mode: str | None = None,
    card_shadow: bool | None = None,
    ambient_dim: float | None = None,
) -> RenderResult:
    cfg = config or {}
    layout = {**DEFAULT_LAYOUT, **cfg.get("layout", {})}
    acct = account or Account(**{k: v for k, v in cfg.get("account", {}).items() if k in ("name", "handle", "avatar", "verified")})

    mode = presentation_mode or layout.get("presentation_mode") or cfg.get("presentation_mode") or DEFAULT_PRESENTATION_MODE
    if not isinstance(mode, str):
        raise ValueError(f"Invalid presentation_mode: expected string, got {type(mode).__name__}")
    mode = mode.strip().lower()

    if mode not in VALID_PRESENTATION_MODES:
        raise ValueError(f"Invalid presentation_mode: '{mode}'. Must be one of {sorted(VALID_PRESENTATION_MODES)}")

    if mode == PRESENTATION_MODE_FOOTAGE_CAPTION:
        raise NotImplementedError("Presentation mode 'footage_caption' is reserved for a future milestone.")

    fit = footage_fit or layout.get("footage_fit") or cfg.get("footage_fit") or DEFAULT_FOOTAGE_FIT
    if not isinstance(fit, str):
        raise ValueError(f"Invalid footage_fit: expected string, got {type(fit).__name__}")
    fit = fit.strip().lower()
    if fit not in VALID_FOOTAGE_FIT:
        raise ValueError(f"Invalid footage_fit: '{fit}'. Must be one of {sorted(VALID_FOOTAGE_FIT)}")
    layout["footage_fit"] = fit

    if card_background_mode is not None:
        if not isinstance(card_background_mode, str):
            raise ValueError(f"Invalid card_background_mode: expected string, got {type(card_background_mode).__name__}")
        mode_val = card_background_mode.strip().lower()
        if mode_val not in VALID_CARD_BACKGROUND_MODES:
            raise ValueError(f"Invalid card_background_mode: '{card_background_mode}'. Must be one of {sorted(VALID_CARD_BACKGROUND_MODES)}")
        layout["card_background_mode"] = mode_val

    if card_shadow is not None:
        if isinstance(card_shadow, str):
            lowered = card_shadow.strip().lower()
            if lowered in ("true", "1", "yes", "on", "enable", "enabled"):
                shadow_val = True
            elif lowered in ("false", "0", "no", "off", "disable", "disabled", "none", ""):
                shadow_val = False
            else:
                raise ValueError(f"Invalid card_shadow: expected boolean, got '{card_shadow}'")
        elif isinstance(card_shadow, bool):
            shadow_val = card_shadow
        else:
            raise ValueError(f"Invalid card_shadow: expected boolean, got {type(card_shadow).__name__}")
        layout["card_shadow"] = shadow_val

    if ambient_dim is not None:
        if not isinstance(ambient_dim, (int, float)) or isinstance(ambient_dim, bool) or not (0.0 <= ambient_dim <= 1.0):
            raise ValueError(f"Invalid ambient_dim: expected float between 0.0 and 1.0, got {ambient_dim}")
        layout["ambient_dim"] = float(ambient_dim)

    if mode == PRESENTATION_MODE_FOOTAGE_CLEAN:
        W = int(layout.get("canvas_width", 1080))
        H = int(layout.get("canvas_height", 1920))
        box = (0, 0, W, H)
        dur = composite(
            video=video,
            overlay=None,
            box=box,
            layout=layout,
            out=out,
            max_seconds=max_seconds,
            poster=poster,
            config=cfg,
            music=music,
            timeout=timeout,
            presentation_mode=mode,
        )
        return RenderResult(
            output=out,
            overlay=None,
            video_box=box,
            duration=dur,
            poster=poster,
            presentation_mode=mode,
            footage_fit=fit,
            card_background_mode=layout.get("card_background_mode", DEFAULT_CARD_BACKGROUND_MODE),
            card_shadow=bool(layout.get("card_shadow", DEFAULT_CARD_SHADOW)),
        )

    # Card mode
    overlay_path = out.parent / f"{out.stem}_overlay.png"
    box = build_overlay(headline, acct, layout, overlay_path)
    dur = composite(
        video=video,
        overlay=overlay_path,
        box=box,
        layout=layout,
        out=out,
        max_seconds=max_seconds,
        poster=poster,
        config=cfg,
        music=music,
        timeout=timeout,
        presentation_mode=mode,
    )

    return RenderResult(
        output=out,
        overlay=overlay_path,
        video_box=box,
        duration=dur,
        poster=poster,
        presentation_mode=mode,
        footage_fit=fit,
        card_background_mode=layout.get("card_background_mode", DEFAULT_CARD_BACKGROUND_MODE),
        card_shadow=bool(layout.get("card_shadow", DEFAULT_CARD_SHADOW)),
    )
