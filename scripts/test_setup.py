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
