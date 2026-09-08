# macOS Setup Guide for KenauShorts

This guide walks through configuring KenauShorts on macOS (Apple Silicon or Intel).

---

## 1. Installing FFmpeg via Homebrew

```bash
brew install ffmpeg
ffmpeg -version
```

KenauShorts detects Apple Silicon's hardware media engine (`h264_videotoolbox`) automatically, enabling ultra-fast 1080×1920 video renders with minimal CPU usage and low battery drain.

---

## 2. Python Setup

```bash
cd KenauShorts
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 3. Launching Studio

```bash
python3 scripts/start.py
```

---

## 4. Always-On Background Daemon on macOS

To run KenauShorts continuously in the background at login without keeping a Terminal window open:

```bash
python3 scripts/install_service.py
```

This creates and loads a LaunchAgent at `~/Library/LaunchAgents/com.kenaushorts.studio.plist`.

### Managing the Background Service:
- **View Logs**:
  ```bash
  tail -f logs/service.log
  ```
- **Restart Service**:
  ```bash
  launchctl kickstart -k "gui/$(id -u)/com.kenaushorts.studio"
  ```
- **Stop / Unload Service**:
  ```bash
  launchctl bootout "gui/$(id -u)/com.kenaushorts.studio"
  ```
