#!/usr/bin/env python3
"""
scripts/install_service.py — Cross-platform background service installer for KenauShorts.

Installs KenauShorts as an always-on background service that runs on user login:
- Windows: Windows Task Scheduler (logon trigger)
- macOS: launchd LaunchAgent (~/Library/LaunchAgents)
- Linux: systemd user service (~/.config/systemd/user)
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def install_macos() -> bool:
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / "com.kenaushorts.studio.plist"

    python = sys.executable
    server_script = str(ROOT / "studio" / "server.py")
    log_out = str(ROOT / "logs" / "service.log")
    log_err = str(ROOT / "logs" / "service.err.log")

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.kenaushorts.studio</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{server_script}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{ROOT}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_out}</string>
    <key>StandardErrorPath</key>
    <string>{log_err}</string>
</dict>
</plist>
"""
    plist_path.write_text(plist_content, encoding="utf-8")
    print(f"✓ Created launchd agent: {plist_path}")

    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(plist_path)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    res = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)], check=False)
    if res.returncode == 0:
        print("✓ KenauShorts macOS background service started!")
        return True
    else:
        print("⚠️  launchctl bootstrap returned non-zero. Try: launchctl load " + str(plist_path))
        return False

def install_windows() -> bool:
    task_name = "KenauShortsStudio"
    python = sys.executable
    # Use pythonw if available to prevent flashing a console window
    pythonw = Path(python).parent / "pythonw.exe"
    exe = str(pythonw if pythonw.exists() else python)
    server_script = str(ROOT / "studio" / "server.py")

    cmd = [
        "schtasks", "/create", "/f",
        "/tn", task_name,
        "/tr", f'"{exe}" "{server_script}"',
        "/sc", "onlogon",
        "/rl", "limited"
    ]
    res = subprocess.run(cmd, check=False)
    if res.returncode == 0:
        print(f"✓ Created Windows Task Scheduler task: {task_name}")
        print("✓ KenauShorts will now run automatically on logon.")
        return True
    else:
        print("⚠️  Failed to create task with schtasks. You can run scripts/start.py manually.")
        return False

def install_linux() -> bool:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "kenaushorts.service"

    python = sys.executable
    server_script = str(ROOT / "studio" / "server.py")

    content = f"""[Unit]
Description=KenauShorts Studio Always-On Background Daemon
After=network.target

[Service]
Type=simple
WorkingDirectory={ROOT}
ExecStart={python} {server_script}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""
    unit_path.write_text(content, encoding="utf-8")
    print(f"✓ Created systemd service: {unit_path}")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    res = subprocess.run(["systemctl", "--user", "enable", "--now", "kenaushorts.service"], check=False)
    if res.returncode == 0:
        print("✓ KenauShorts Linux background service enabled and started!")
        return True
    return False

def main() -> None:
    system = platform.system()
    print(f"Installing KenauShorts background service for {system}...")
    (ROOT / "logs").mkdir(exist_ok=True)

    if system == "Darwin":
        install_macos()
    elif system == "Windows":
        install_windows()
    elif system == "Linux":
        install_linux()
    else:
        print(f"Unsupported operating system: {system}")

if __name__ == "__main__":
    main()
