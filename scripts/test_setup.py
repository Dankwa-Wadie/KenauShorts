#!/usr/bin/env python3
"""
scripts/test_setup.py — Pre-flight diagnostic tool for KenauShorts.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def check_mark(ok: bool) -> str:
    return "✅ PASS" if ok else "❌ FAIL"

def main() -> None:
    print("=" * 60)
    print("         🔍 KenauShorts Pre-Flight Diagnostics")
    print("=" * 60)

    # 1. OS & Python
    py_ok = sys.version_info >= (3, 9)
    print(f"{check_mark(py_ok)} Python Version: {platform.python_version()} (>= 3.9 required)")

    # 2. ffmpeg & ffprobe
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    print(f"{check_mark(bool(ffmpeg_path))} ffmpeg: {ffmpeg_path or 'Not Found on PATH'}")
    print(f"{check_mark(bool(ffprobe_path))} ffprobe: {ffprobe_path or 'Not Found on PATH'}")

    # 3. Hardware acceleration check
    hw_encoder = "None (CPU libx264)"
    if ffmpeg_path:
        try:
            encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
            if "h264_videotoolbox" in encoders:
                hw_encoder = "Apple Silicon VideoToolbox (Fast hardware acceleration)"
            elif "h264_nvenc" in encoders:
                hw_encoder = "Nvidia NVENC (Fast hardware acceleration)"
            elif "h264_qsv" in encoders:
                hw_encoder = "Intel QuickSync (Hardware acceleration)"
        except Exception:
            pass
    print(f"ℹ️  Video Acceleration: {hw_encoder}")

    # 4. Pillow & Graphics
    try:
        from PIL import Image, ImageDraw, ImageFont
        print("✅ PASS Pillow: Installed and operational")
    except ImportError:
        print("❌ FAIL Pillow: Missing! Run pip install Pillow")

    # 4b. System fonts — a headless/minimal Linux install often has none at
    # all, which silently falls back to a tiny bitmap font on every card.
    try:
        sys.path.insert(0, str(ROOT))
        from core.render import MONO_CANDIDATES, UI_CANDIDATES, _RESOLVED_FONTS, load_font
        load_font(MONO_CANDIDATES, 76, role="preflight_mono")
        load_font(UI_CANDIDATES, 44, role="preflight_ui")
        mono_ok = "Default bitmap font" not in _RESOLVED_FONTS.get("preflight_mono", "")
        ui_ok = "Default bitmap font" not in _RESOLVED_FONTS.get("preflight_ui", "")
        print(f"{check_mark(mono_ok)} Headline font: {_RESOLVED_FONTS.get('preflight_mono', 'unknown')}")
        print(f"{check_mark(ui_ok)} UI font: {_RESOLVED_FONTS.get('preflight_ui', 'unknown')}")
        if not (mono_ok and ui_ok) and platform.system() == "Linux":
            print("   ℹ️  Install fonts-dejavu-core / fonts-liberation (see docs/LINUX_GUIDE.md)")
    except Exception as e:
        print(f"⚠️  Could not check system fonts: {e}")

    # 5. yt-dlp
    ytdlp_path = shutil.which("yt-dlp")
    print(f"{check_mark(bool(ytdlp_path))} yt-dlp: {ytdlp_path or 'Not Found on PATH (pip install yt-dlp)'}")

    print("=" * 60)
    if py_ok and ffmpeg_path:
        print("🎉 Your PC is ready to run KenauShorts!")
        print("   Start the studio: python3 scripts/start.py")
    else:
        print("⚠️  Please resolve the missing requirements above before running.")
    print("=" * 60)

if __name__ == "__main__":
    main()
