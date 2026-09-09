# 🎬 KenauShorts

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)]()
[![FFmpeg Required](https://img.shields.io/badge/ffmpeg-required-orange.svg)](https://ffmpeg.org/)

**KenauShorts** is an open-source, always-on automated short-form video creation and publishing studio designed to run locally on your PC.

It continuously discovers trending stories across **user-defined Subreddits**, **YouTube channels**, and **RSS feeds**, uses LLMs (Google Gemini, Anthropic Claude, or OpenAI) to select viral stories and craft punchy headlines, composites high-production 1080×1920 portrait cards using hardware-accelerated FFmpeg, and uploads them automatically to YouTube Shorts.

```
┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐
│    DISCOVERY    │  ──>  │  AI EDITORIAL   │  ──>  │  1080x1920 CARD │  ──>  │ YOUTUBE SHORTS  │
│ User Subreddits │       │ Gemini / Claude │       │  RENDER ENGINE  │       │  AUTO-PUBLISH   │
│ YouTube & RSS   │       │  OpenAI Models  │       │ Hardware FFmpeg │       │ OAuth Approval  │
└─────────────────┘       └─────────────────┘       └─────────────────┘       └─────────────────┘
```

---

## ✨ Features

- **🌐 Runs 100% Locally on Your PC**: No external SaaS fees or cloud storage requirements. Your API keys, channel secrets, and generated videos remain exclusively on your computer.
- **⚡ Always-On Background Daemon**: Seamlessly runs on startup across **Windows** (Task Scheduler), **macOS** (launchd), and **Linux** (systemd) with sleep prevention (`caffeinate` / Windows execution states).
- **🛸 Subreddit Scraper Manager**: Easily add, categorize, test, and remove any subreddits for viral research directly from the Studio interface, complete with minimum upvote filtering and real-time test scrapes.
- **🧠 Multi-Model AI Reasoning**:
  - **Google Gemini** (Recommended — generous free tier, fast response)
  - **Anthropic Claude** (Claude 3.5 Sonnet)
  - **OpenAI** (GPT-4o / GPT-4o-mini)
- **🎨 1080×1920 Card Compositor**:
  - High-framerate portrait rendering (9:16 Shorts/Reels/TikTok).
  - Clean Twitter/X-style card layout: circular avatar, verified badge, handle, bold monospace headline, and rounded video window.
  - Native hardware acceleration support (`h264_videotoolbox` on Apple Silicon, `h264_nvenc` on NVIDIA, `h264_qsv` on Intel, and `libx264`).
- **🖥️ Local Web Studio UI**:
  - First-Run Onboarding Wizard for new users.
  - Interactive Review Queue: Watch rendered drafts in an embedded HTML5 video player, edit titles/descriptions, re-render, or approve with one click.
  - Live execution log terminal streaming straight from the Python engine.

---

## 🚀 Quickstart

### 1. Prerequisites

- **Python 3.9+**
- **FFmpeg** installed and accessible on your system PATH:
  - **Windows**: `winget install Gyan.FFmpeg`
  - **macOS**: `brew install ffmpeg`
  - **Linux**: `sudo apt install ffmpeg`

Verify your system:
```bash
ffmpeg -version
```

### 2. Clone and Setup

```bash
git clone https://github.com/yourusername/KenauShorts.git
cd KenauShorts

# Create a virtual environment
python3 -m venv .venv

# Activate the virtual environment
# On macOS / Linux:
source .venv/bin/activate
# On Windows (PowerShell):
# .venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

### 3. Run Pre-Flight Diagnostics

```bash
python3 scripts/test_setup.py
```

### 4. Launch KenauShorts Studio

```bash
python3 scripts/start.py
```

Your browser will automatically open to `http://127.0.0.1:8766/`. The **First-Run Setup Wizard** will guide you through entering your channel name, choosing your AI provider, and selecting your starting subreddits.

### 5. Authorize YouTube Uploads

The wizard sets up AI editorial and discovery, but **publishing to YouTube is a separate, one-time step** — and it's easy to confuse with the AI key setup above because Google has two look-alike products involved:

| | Used for | Where you get it | How you provide it |
|---|---|---|---|
| Gemini API key | AI editorial (picking stories, writing headlines) | [Google **AI Studio**](https://aistudio.google.com/app/apikey) | Paste it in the Connections tab |
| YouTube OAuth client | Uploading the rendered video | Google **Cloud Console** (a different product) | Download `client_secret.json`, then click *Connect* once in the Studio |

Uploading a video requires your explicit consent, so Google doesn't allow it via a pasted API key at all, regardless of which product issued it — this is a Google API restriction, not a limitation of this app. Follow **[docs/YOUTUBE_API_SETUP.md](docs/YOUTUBE_API_SETUP.md)** to create the OAuth client, then in the Studio's **Connections** tab click **Connect / Authorize YouTube**. This opens a real Google sign-in window in your browser — that's expected, not an error. Approve access once and it's saved to `token.json` for every future run.

---

## 🤖 Configuring Always-On Background Service

To let KenauShorts run quietly in the background without needing a terminal open:

```bash
python3 scripts/install_service.py
```

- **Windows**: Creates a Windows Task Scheduler task that launches the engine on login.
- **macOS**: Registers a `launchd` service in `~/Library/LaunchAgents`.
- **Linux**: Enables a `systemd --user` background unit.

To remove it again without touching anything else:

```bash
python3 scripts/uninstall_service.py
```

For a full reset or permanent removal (deleting generated data, revoking API keys and YouTube access, removing the project itself), see **[docs/UNINSTALLING.md](docs/UNINSTALLING.md)**.

---

## 📂 Project Structure

```
KenauShorts/
├── core/
│   ├── agent.py               # Discovery, AI editorial curation, and pipeline runner
│   ├── render.py              # 1080x1920 card compositor with cross-platform fonts
│   ├── render_story.py        # Multi-line story card variant
│   ├── scrape_reddit.py       # Resilient Reddit scraper with rate-limiting backoff
│   ├── youtube_auth.py        # YouTube Desktop OAuth helper
│   ├── state.py               # Deduplication store (prevents duplicate shorts)
│   └── power.py               # Cross-platform sleep inhibitor (Win/Mac/Linux)
├── studio/
│   ├── server.py              # Zero-dependency local REST API & automation server
│   ├── store.py               # SQLite database & cross-platform file locking
│   ├── worker.py              # Background worker for isolated render and upload jobs
│   └── web/                   # Modern dark-mode Web Studio interface
│       ├── index.html
│       ├── style.css
│       └── app.js
├── scripts/
│   ├── start.py               # 1-Click launcher
│   ├── install_service.py     # Background daemon installer (Win/Mac/Linux)
│   ├── uninstall_service.py   # Background daemon remover (Win/Mac/Linux)
│   └── test_setup.py          # Pre-flight diagnostic tool
├── tests/                     # Unit tests (stdlib unittest, no extra dependency)
├── docs/                      # In-depth setup and API guides
└── config.example.json        # Template configuration
```

### Running the test suite

```bash
python3 -m unittest discover -s tests
```

Render tests are skipped automatically if `ffmpeg`/`ffprobe` aren't on `PATH`; everything else runs with no network access and no API keys.

---

## 🔒 Privacy & Security

- **Strict .gitignore**: Your `.env`, `client_secret.json`, `token.json`, `studio-secrets.json`, SQLite databases, and output video folders are permanently excluded from Git commits.
- **Local Loopback Only**: The Studio server binds strictly to `127.0.0.1:8766`.
- **Masked Keys**: Sensitive API tokens are never exposed in log outputs or web requests.

---

## 📖 Documentation

- [Getting Started Guide](docs/GETTING_STARTED.md)
- [Windows Setup Guide](docs/WINDOWS_GUIDE.md)
- [macOS Setup Guide](docs/MACOS_GUIDE.md)
- [Linux Setup Guide](docs/LINUX_GUIDE.md)
- [YouTube Data API & OAuth Setup](docs/YOUTUBE_API_SETUP.md)
- [Uninstalling / Resetting](docs/UNINSTALLING.md)

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
