# KenauShorts — Project Notes

This file is for continuity across AI tools (Claude, ChatGPT, Antigravity) and
across sessions. Read this before starting work. Update it before switching
tools or ending a session — commit it along with your code changes.

**Rule: always `git pull` before starting, always commit + push before
switching tools or stopping.** This file is only trustworthy if it reflects
what's actually on `origin/main`.

---

## Current state (update this section each handoff)

- **Stage 2 Completed (Process Reliability, Cancellation & Persistent Serial Queue)**:
  - Robust process lifecycle management: tracking `proc.pid` in `ACTIVE_PROC` and SQLite `jobs` table.
  - Win32 process liveness (`WaitForSingleObject` / `OpenProcess`) & `taskkill /F /T` process tree termination.
  - Explicit manual job cancellation (`POST /api/job/cancel` and Cancel button in UI) with process termination, artifact cleanup, and queue unblocking.
  - Single-worker FIFO queue using existing SQLite `jobs` table (no external queue servers). Jobs transition cleanly through `pending` -> `running` -> `completed` / `failed` / `cancelled`.
  - Server restart recovery: `recover_interrupted_jobs()` reconciles orphaned running jobs to `interrupted`, kills lingering child processes, and preserves pending jobs for sequential processing.
  - Frontend UI: cancel button in topbar actions, queue depth indicator (`badge-mode`), and queue position notifications.
  - Test suite: `tests/test_stage2_queue_cancel.py` verified with 11/11 tests passing (45/45 studio & integration tests passing).
- Pipeline: working end-to-end. Discovery (YouTube channels + search queries
  + Reddit videos only, no text/story sources) → Gemini editorial → ffmpeg
  render → optional upload.
- Automation: currently posting directly but **private** (not public yet).
  Interval: [FILL IN — e.g. every 2 hours].
- Active experiment: testing whether Marvel Rivals gaming content
  outperforms other topics (one video already got the channel's highest
  view count). Current `youtube_queries` (5, kept deliberately small for
  YouTube search quota reasons — see below):
  - gaming highlights
  - Marvel Rivals highlight
  - trending video game clip
  - gaming fail moment
  - funny fails
  - AI and space queries were dropped — observed to underperform.
- Known content-quality issue being watched: occasional non-English source
  video content. Added `relevanceLanguage: "en"` to the YouTube search call
  as a mitigation (soft bias, not a hard filter) plus an editorial-prompt
  rule that headlines must always be written in English. Not a complete fix
  — keep an eye on this.

## Hard constraints — don't relearn these the hard way

- **YouTube Data API `search.list` quota**: as of June 2026, this is capped
  at roughly **100 calls/day**, separate from the main 10,000-unit pool.
  One pipeline run = one `search.list` call per configured `youtube_query`.
  **Query count directly limits how often automation can safely run.**
  Formula: safe runs/day ≈ 100 ÷ (number of queries). Check this before
  changing either the query list or the automation interval — changing one
  without the other breaks this math.
- **OAuth token expiry**: the Google Cloud app is in "Testing" mode (not
  verified), so the YouTube upload refresh token expires after **~7 days**.
  This is expected, not a bug — reconnect via the Studio's Connections page
  when uploads start failing with `invalid_grant`. Decided not to pursue
  full Google verification (privacy policy + demo video + review) since
  this is a single-user local tool.
- **Copyright**: this project intentionally sticks to public-domain
  (NASA/NOAA/Library of Congress) and Creative-Commons-licensed
  (`videoLicense=creativeCommon`, `youtube_cc_only`) video sources only.
  Explicitly decided against a copyrighted-show-clips niche (e.g. Family
  Guy, The Mentalist) due to Content ID / infringement risk. Don't add
  discovery sources that pull raw footage from licensed TV/movies/games —
  CC-licensed *creator commentary about* a copyrighted game (e.g. Marvel
  Rivals gameplay highlights from a creator) is fine; ripped game
  cutscenes/assets are not.
- **Secrets**: never hardcode API keys anywhere. `.env`, `client_secret.json`,
  `token.json`, `studio-secrets.json`, `studio-settings.json`,
  `studio.sqlite3`, `state.json`, `config.json` are all gitignored — keep it
  that way. Verify with `git status --ignored` if unsure.

## Recurring bug pattern — check this before deep debugging

A stale background Python process or a leftover `.pipeline.lock` file
(usually from force-closing a terminal mid-job) causes misleading errors:
"a job is currently running" with no real job active, or an env var
appearing to not be set even though it clearly is in the current shell.
**Fix**: `Get-Process python* | Stop-Process -Force`, delete
`.pipeline.lock` if present, restart the Studio server fresh in one window.
This has recurred multiple times — check this FIRST before assuming a new
bug.

## Architecture quick reference

- `core/agent.py` — discovery, editorial (Gemini call), render orchestration,
  `run_pipeline()` (normal loop) and `run_manual()` (paste-a-URL mode).
- `core/render.py` — ffmpeg card compositing (video + headline + avatar).
  Already supports background music mixing via `composite()`'s `music`
  param (ducking, fade-out, looping) — just needs a real file; see below.
- `core/state.py` — `state.json` wrapper (`seen`, `failed`, `posted` history).
- `studio/server.py` — Studio's HTTP API (all `/api/*` routes), job dispatch.
- `studio/store.py` — SQLite layer for video records + job records.
- `studio/web/app.js` + `index.html` + `style.css` — Studio frontend.

## In progress / paused

- **Background music**: wiring is done (`config.json` → `audio.track` →
  `_render_pick()` → `render()`), just needs actual instrumental track
  file(s) placed in `assets/music/`. Recommended source: pixabay.com/music
  (free, no attribution required). Not yet resumed.
- **Card visual effects**: requested but not yet scoped — needs a concrete
  idea (zoom/pan? transition? vignette?) before implementation starts.
- **Bulk upload**: considered, deliberately not built — uploading is
  irreversible/public-facing and costs real quota, unlike bulk delete.

## Multi-AI workflow

Rotating between Claude, ChatGPT, and Antigravity. Sequential, not
simultaneous — one tool works until a natural stopping point, commits +
pushes everything (including WIP), then the next tool pulls fresh and
continues. Update the "Current state" section above before every handoff so
the next tool isn't rediscovering context from scratch.
