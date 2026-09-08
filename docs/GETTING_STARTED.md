# Getting Started with KenauShorts

Welcome to **KenauShorts**! This guide takes you from an empty folder to producing your first automated short-form video in under 5 minutes.

---

## 1. System Requirements

- **Operating System**: Windows 10/11, macOS 12+, or Ubuntu/Debian Linux
- **Python**: 3.9 or newer
- **FFmpeg**: Required for media decoding, cropping, and rendering

To test if FFmpeg is installed, run:
```bash
ffmpeg -version
```
If you get an error that `ffmpeg` was not found, see:
- [Windows Guide](WINDOWS_GUIDE.md)
- [macOS Guide](MACOS_GUIDE.md)

---

## 2. Installation

1. Clone the repository and enter the directory:
   ```bash
   git clone https://github.com/yourusername/KenauShorts.git
   cd KenauShorts
   ```

2. Create a clean Python virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate    # On Windows: .venv\Scripts\activate
   ```

3. Install requirements:
   ```bash
   pip install -r requirements.txt
   ```

4. Run the pre-flight checker:
   ```bash
   python3 scripts/test_setup.py
   ```

---

## 3. Starting the Studio

Start the engine:
```bash
python3 scripts/start.py
```
This starts the local loopback server on `http://127.0.0.1:8766/` and opens your browser.

---

## 4. The First-Run Setup Wizard

When you open the web interface for the first time:
1. **Channel Identity**: Enter your Channel Name (e.g. *Daily Pulse*) and Handle (e.g. *@DailyPulse*).
2. **AI Provider**:
   - We recommend **Google Gemini** for new creators because Google AI Studio offers a free tier without requiring a paid subscription.
   - Paste your API key from [aistudio.google.com](https://aistudio.google.com/app/apikey).
3. **Subreddits**:
   - Check the boxes for the niches you want to target (e.g., Tech, AI, Gaming, Space).
   - Click **Complete Setup & Launch**.

---

## 5. Generating Your First Draft

In the top bar of the Studio:
- Click **+ Create Preview Draft**.
- Switch to the **Live Logs** tab to watch the engine in action:
  1. It discovers the newest posts across your chosen subreddits and YouTube channels.
  2. The AI picks the most viral story and writes an all-caps headline.
  3. The video clip is downloaded and composited into a 1080×1920 portrait card.
- Once complete, head over to **Drafts & Library** to watch the video!
