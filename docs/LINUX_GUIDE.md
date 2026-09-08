# Linux Setup Guide for KenauShorts

This guide walks through configuring KenauShorts on Ubuntu/Debian, Fedora, and Arch-based distributions, on both a desktop and a headless server.

---

## 1. Installing FFmpeg on Linux

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install ffmpeg

# Fedora
sudo dnf install ffmpeg

# Arch / Manjaro
sudo pacman -S ffmpeg
```

Verify it's on `PATH`:
```bash
ffmpeg -version
```

KenauShorts auto-detects hardware encoders at render time: Nvidia's `h264_nvenc` if the proprietary driver is installed, or Intel's `h264_qsv` on supported integrated graphics. It falls back to software `libx264` with no configuration needed either way.

---

## 2. System Fonts

The headline text and story cards render with Pillow, using whatever TrueType fonts are installed system-wide — but a minimal server install (no desktop environment) often ships with **none at all**, which silently falls back to a tiny bitmap font instead of the intended bold monospace/UI faces. Install the fallback font packages KenauShorts looks for:

```bash
# Debian / Ubuntu
sudo apt install fonts-dejavu-core fonts-liberation

# Fedora
sudo dnf install dejavu-sans-mono-fonts dejavu-sans-fonts liberation-mono-fonts liberation-sans-fonts

# Arch / Manjaro
sudo pacman -S ttf-dejavu ttf-liberation
```

Run `python3 scripts/test_setup.py` after installing — it reports whether a real font was found or whether cards would still render with the bitmap fallback.

---

## 3. Python Setup

```bash
cd KenauShorts
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Python 3.9+ is required; check with `python3 --version`.

---

## 4. Launching Studio

```bash
python3 scripts/start.py
```

On a desktop with a browser installed, this opens the Studio UI automatically. On a headless server there's no browser to open — the command still starts the server and prints the URL; open `http://<server-ip>:8766/` from another machine on the same network, or `ssh -L 8766:localhost:8766 user@server` and browse to `http://localhost:8766/` locally.

---

## 5. Always-On Background Service on Linux

```bash
python3 scripts/install_service.py
```

This creates and enables a `systemd --user` unit at `~/.config/systemd/user/kenaushorts.service`, so the Studio starts automatically and restarts itself if it crashes.

### Managing the Background Service

- **Check status**:
  ```bash
  systemctl --user status kenaushorts.service
  ```
- **View logs**:
  ```bash
  journalctl --user -u kenaushorts.service -f
  ```
- **Restart**:
  ```bash
  systemctl --user restart kenaushorts.service
  ```
- **Stop / disable**:
  ```bash
  systemctl --user disable --now kenaushorts.service
  ```

A user service normally stops when you log out. To keep it running on a headless server across logouts and reboots, enable lingering once:
```bash
sudo loginctl enable-linger "$USER"
```

---

## 6. YouTube OAuth on a Headless Server

`core/youtube_auth.py`'s one-time authorization opens a browser and waits for Google's redirect on `localhost` — which only works if that browser is running on the same machine as the server. On a headless box, either:

- run the SSH port-forward from step 4 (`ssh -L 8766:localhost:8766 ...`) and complete the "Connect YouTube" flow from your local browser through that tunnel, since the redirect still lands on `localhost` from the server's own point of view, or
- run `python3 -m core.youtube_auth` once on a desktop machine with the same `client_secret.json`, then copy the resulting `token.json` to the server.

---

## 7. Sleep Prevention

KenauShorts prevents the system from sleeping mid-render using `systemd-inhibit`, which ships with systemd and covers every distro in this guide. On a non-systemd distro (Alpine, some minimal container bases), this step is skipped silently and rendering still completes — it just won't stop the machine from suspending if something else on the system requests it.
