# Windows Setup Guide for KenauShorts

This guide walks through configuring KenauShorts on Windows 10 or Windows 11.

---

## 1. Installing FFmpeg on Windows

FFmpeg is essential for rendering 1080×1920 video cards.

### Recommended: Via Windows Package Manager (winget)

Open **PowerShell** or **Command Prompt** as Administrator and run:
```powershell
winget install Gyan.FFmpeg
```
Restart your PowerShell terminal and verify:
```powershell
ffmpeg -version
```

### Alternative: Manual Download
1. Download the release build from [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/).
2. Extract the archive (e.g. to `C:\ffmpeg`).
3. Add `C:\ffmpeg\bin` to your system's `PATH` environment variable.

---

## 2. Python Setup on Windows

1. Install Python 3.10+ from the official [Python Website](https://www.python.org/) or Microsoft Store.
2. During installation, make sure to check the box:
   **☑ Add python.exe to PATH**

---

## 3. Running KenauShorts

In PowerShell or Windows Terminal:
```powershell
cd KenauShorts
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts\start.py
```

---

## 4. Always-On Background Service on Windows

To run KenauShorts automatically whenever you log into Windows:
```powershell
python scripts\install_service.py
```
This registers a scheduled task in the Windows Task Scheduler under `KenauShortsStudio` that starts silently in the background on logon.

To stop or remove the task:
```powershell
schtasks /delete /tn "KenauShortsStudio" /f
```
