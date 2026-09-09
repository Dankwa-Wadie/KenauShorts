#!/usr/bin/env python3
"""
scripts/uninstall_service.py — Removes the KenauShorts always-on background service.

The counterpart to scripts/install_service.py: stops and deletes whichever
of these it created —
- Windows: the "KenauShortsStudio" Task Scheduler task
- macOS: the com.kenaushorts.studio LaunchAgent
- Linux: the kenaushorts.service systemd --user unit

This only removes the background service registration. It does not touch
your project folder, generated videos, or configuration — see
docs/UNINSTALLING.md for a full removal guide.
"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

def uninstall_macos() -> bool:
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.kenaushorts.studio.plist"
    if not plist_path.exists():
        print("ℹ️  No macOS background service found — nothing to remove.")
        return True

    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(plist_path)],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    plist_path.unlink(missing_ok=True)
    print(f"✓ Stopped and removed launchd agent: {plist_path}")
    return True

def uninstall_windows() -> bool:
    task_name = "KenauShortsStudio"
    check = subprocess.run(
        ["schtasks", "/query", "/tn", task_name],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if check.returncode != 0:
        print("ℹ️  No Windows background service found — nothing to remove.")
        return True

    res = subprocess.run(["schtasks", "/delete", "/tn", task_name, "/f"], check=False)
    if res.returncode == 0:
        print(f"✓ Removed Windows Task Scheduler task: {task_name}")
        return True
    print(f'⚠️  Failed to remove the task automatically. Try manually: schtasks /delete /tn "{task_name}" /f')
    return False

def uninstall_linux() -> bool:
    unit_path = Path.home() / ".config" / "systemd" / "user" / "kenaushorts.service"
    if not unit_path.exists():
        print("ℹ️  No Linux background service found — nothing to remove.")
        return True

    subprocess.run(["systemctl", "--user", "disable", "--now", "kenaushorts.service"], check=False)
    unit_path.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print(f"✓ Stopped and removed systemd service: {unit_path}")
    return True

def main() -> None:
    system = platform.system()
    print(f"Removing the KenauShorts background service for {system}...")

    if system == "Darwin":
        uninstall_macos()
    elif system == "Windows":
        uninstall_windows()
    elif system == "Linux":
        uninstall_linux()
    else:
        print(f"Unsupported operating system: {system}")
        return

    print()
    print("Background service removed. KenauShorts's files, config, and generated")
    print("videos are untouched — see docs/UNINSTALLING.md for a full removal guide.")

if __name__ == "__main__":
    main()
