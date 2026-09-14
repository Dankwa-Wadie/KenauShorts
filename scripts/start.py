#!/usr/bin/env python3
"""
scripts/start.py — 1-Click launcher for KenauShorts on PC.

Starts the background Studio daemon and opens the web interface in your default browser.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

def main() -> None:
    print("=" * 60)
    print("           🎬 KenauShorts Studio Launcher")
    print("=" * 60)

    # 1. Check Python version
    if sys.version_info < (3, 9):
        print("❌ Python 3.9 or higher is required.")
        sys.exit(1)

    # 2. Check ffmpeg
    if not shutil.which("ffmpeg"):
        print("⚠️  ffmpeg not found on your system PATH!")
        print("   KenauShorts requires ffmpeg to render videos.")
        print("   - On Windows: winget install Gyan.FFmpeg or download from ffmpeg.org")
        print("   - On macOS: brew install ffmpeg")
        print("   - On Linux: sudo apt install ffmpeg")
        print("-" * 60)

    # 3. Create default folders
    (ROOT / "work").mkdir(exist_ok=True)
    (ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "logs").mkdir(exist_ok=True)

    port = int(os.environ.get("KENAU_STUDIO_PORT", "8766"))
    url = f"http://127.0.0.1:{port}/"

    print(f"🚀 Starting KenauShorts Engine at {url}...")
    print("💡 Press Ctrl+C to stop.\n")

    # Launch browser after a brief delay
    def open_browser():
        time.sleep(1.2)
        webbrowser.open(url)

    import threading
    threading.Thread(target=open_browser, daemon=True).start()

    from studio.server import run_server
    run_server(port)

if __name__ == "__main__":
    main()
