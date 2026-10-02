# KenauShorts — Project Notes

This file is for continuity across AI tools (Claude, ChatGPT, Antigravity) and
across sessions. Read this before starting work. Update it before switching
tools or ending a session — commit it along with your code changes.

**Rule: always `git pull` before starting, always commit + push before
switching tools or stopping.** This file is only trustworthy if it reflects
what's actually on `origin/main`.

---

## Current state (update this section each handoff)

- **Stage 7 Phase 3 Completed (Command Center, Quota Governance & Discovery Diagnostics)**:
  - **Operational Telemetry & Quota Governance**:
    - Thread-safe local tracking of daily YouTube API quota (`search_list_count`, `videos_insert_count`, `general_units`) in `studio-quota.json` protected by `_QUOTA_LOCK`.
    - Timezone-aware midnight Pacific Time (PT) daily reset (`get_pacific_now`, `get_pacific_date`, `get_pacific_reset_info`) with robust US Daylight Saving Time (DST) fallback on Windows without requiring optional tzdata packages.
    - Pre-flight quota guard capping YouTube search at 100 calls/day (`search_list_limit`); gracefully skips searches when exhausted while allowing non-search discovery sources (channels, Reddit) to continue producing candidates.
    - Non-blocking, sanitized OAuth health diagnostics (`get_oauth_status_details()`) reporting channel metadata and status without exposing sensitive tokens or client credentials.
  - **Discovery Diagnostics & Run Telemetry**:
    - Structured per-run telemetry (`discovery_stats`) tracking configured queries, channels, subreddits, attempted vs skipped searches, raw discovered candidates, cross-source duplicates, unique candidates, seen filtering, and specific rejection breakdown (`rejected_by_reason`).
    - Evidence-based idle diagnostic explanations (`generate_idle_explanation()`) emitted in `KENAU_SUMMARY` providing clear operator explanations for zero-candidate runs.
  - **Executive Operational Command Center (Overview UI)**:
    - Cleaned duplicate HTML markup, panel headers, and disk space cards.
    - Added high-visibility OAuth alert banners when credentials need attention.
    - Operational KPI cards (Ready, Uploaded, In Queue, Total Videos) with dynamic layout.
    - Automation Schedule widget with dynamic local real-time countdown timer (`startOverviewCountdown`, `clearOverviewCountdown`).
    - System Storage Health indicator with color-coded safety thresholds (<512 MB critical, <1 GB low, healthy).
    - Visual Daily Quota Meters for search queries (100 cap), video uploads, and general units with midnight PT reset timer.
    - Actionable Incident Remediation cards with 1-click buttons: Reconcile / Confirm / Fail for `upload_unknown` and `upload_unresolved`, and Retry Render for `render_failed`.
    - Latest Discovery Diagnostics card displaying source yield and rejection breakdowns.
  - **Test Suite**:
    - `tests/test_stage7_phase3.py`: 18/18 passed (1.0s).
    - `tests/test_stage7_phase2.py`: 20/20 passed (9.0s).
    - `tests/test_stage7_phase1.py`: 18/18 passed (1.8s).
    - `tests/test_stage6_publishing.py`: 38/38 passed (6.3s).
    - Full discovery suite: 229 tests total (222 passed, 0 failures, 7 known environment limitations: 6 NVENC, 1 macOS).
- **Stage 7 Phase 2 Completed (Worker Lifecycle, Watchdogs & Automation Reliability)**:
  - **Recoverable SQLite & `state.json` Synchronization (Fix 1)**:
    - In `studio/server.py`, `reconcile_video_upload()` detects if the SQLite video record already has `status == "uploaded"`. When already uploaded, it bypasses outbound HTTP requests (no duplicate uploads or spurious session queries) and immediately invokes `_sync_candidate_posted(video, yt_id)` to ensure candidate state in `state.json` is synchronized with `state.posted` and `state.seen`.
    - Preserves existing `youtube_id` and rejects conflicting IDs with `ConflictError` (HTTP 409).
    - Failed candidate synchronization raises an exception rather than returning success, allowing callers to retry safely. Subsequent retries are idempotent and do not duplicate entries in `state.json`.
    - Server startup recovery in `recover_interrupted_jobs()` automatically heals existing `uploaded` videos that have missing candidate records in `state.json`.
  - **Decoupled Network Probing from Global Server Lock (Fix 2)**:
    - In `studio/server.py`, `StudioHandler.do_GET()` for `/api/status` performs network connectivity checking outside `with GUARD:`, ensuring slow DNS/socket probes never block `GUARD` or Studio operations.
    - Implemented `is_online(cached: bool = True)` with a 10-second TTL cache (`_ONLINE_CACHE`, `_ONLINE_LOCK`). Cache hits return instantly. Cache misses attempt non-blocking lock acquisition; concurrent callers receive cached status rather than blocking.
    - Robust exception handling in `probe_network_connectivity()` catches socket errors, logs warnings, safely releases locks, and returns `online: False`.
  - **Test Suite**:
    - `tests/test_stage7_phase1.py`: 18/18 passed (1.7s) covering both Phase 1 and the corrective fixes.
    - `tests/test_stage6_publishing.py`: 38/38 passed (11.0s).
    - `tests/test_studio_server.py`: 11/11 passed (3.9s).
    - `tests/test_studio_store.py`: 9/9 passed (0.5s).
    - Full discovery suite: 191 tests total (184 passed, 0 failures, 7 known environment-specific errors: 6 NVENC, 1 macOS).
- **Automatic Publishing Workflow Restored (Removal of Mandatory Approval Gate)**:
  - **Direct Automated Publishing**: Transitioned from manual approval-gated publishing to a fully automated publishing workflow: Discover → Check eligibility → Process/render → Validate readiness → Automatically queue → Upload to YouTube → Confirm outcome. Videos become eligible for automatic publishing upon becoming `ready` with a valid rendered file on disk without requiring manual human approval.
  - **Canonical Automation Mode (`publish`)**: `publish` is established as the canonical automatic publishing mode in backend, frontend, and settings. The legacy `publish_approved` mode is supported as a backward-compatible alias and automatically normalized to `publish` by `/api/settings`. In `publish` mode, the scheduler selects eligible ready videos in FIFO order (oldest first) and queues them for YouTube upload.
  - **Role of `review_status`**: `review_status` is decoupled from publishing authorization and maintained purely as informational/legacy editorial metadata. Videos with `unreviewed`, missing, or `approved` review statuses are all eligible for publishing. Exactly 0 videos in the database were marked `rejected` when this change was implemented (86 total videos: 58 uploaded, 18 ready, 8 failed, 2 render_failed; review statuses: 8 unreviewed, 78 None); per simple policy, hypothetical rejected records are non-blocking for publishing eligibility.
  - **Preservation of All Stage 6 Safety & Reliability Safeguards**:
    - Real eligibility checks preserved: valid rendered artifact on disk, duration constraints, metadata validity, Creative Commons/licensing validation, and live/unended content exclusion.
    - Publishing state machine & duplicate guards preserved (HTTP 409 Conflict): blocks already uploaded videos, actively uploading videos, concurrent duplicate queued jobs, and ambiguous states (`upload_unknown`, `upload_unresolved`).
    - Resumable upload recovery preserved: direct authenticated HTTP PUTs, 5MB chunking, same-session URI recovery, authoritative Range header validation (`parse_resumable_range`), and explicit file stream seeking (`f.seek(current_offset)`) handling partial chunk acknowledgements with zero private Google client internals.
    - OAuth refresh failure handling preserved: pre-transfer expiration checks, secret redaction (`[REDACTED]`), zero requests with stale tokens, and preservation of `upload_unknown` without starting duplicate fresh uploads.
    - Ambiguity distinction preserved: 404/410 upload server session expiration transitions to `upload_unresolved`, while network/5xx/OAuth errors preserve `resumable_uri` in `upload_unknown`.
    - Security preserved: CSRF protection, host-rebinding protection, input validation, secret redaction.
  - **Studio UI Updates**:
    - Video modal: Removed disabled state on "Upload to YouTube" button that previously blocked unreviewed videos; eligible ready videos can be uploaded immediately.
    - Editorial dropdown: Clarified as informational rating notes (`✓ Approved`) with explanatory helper noting that eligible ready videos publish automatically.
    - Automation settings: Updated mode selector to "Automatic Publishing (Upload eligible ready videos FIFO)" pointing to canonical `publish` mode.
  - **Test Suite**:
    - `tests/test_stage6_publishing.py`: 38/38 passed (6.5s) covering automatic queueing of unreviewed/missing review_status videos, FIFO automation loop candidate selection without approval, normalization of `publish_approved` mode, partial chunk acknowledgement stream seeking, Range validation, OAuth refresh failure handling with secret redaction, expired sessions (404), 5xx ambiguity preservation, duplicate guards (409), and static verification of zero private client internals.
    - `tests/test_stage5_library.py`: 15/15 passed (0.9s).
    - `tests/test_stage4_pipeline.py`: 14/14 passed (0.8s).
    - `tests/test_stage3_reliability.py`: 12/12 passed (4.7s).
    - `tests/test_stage2_queue_cancel.py`: 11/11 passed (5.7s).
    - `tests/test_style_presets.py`: 14/14 passed (0.2s).
    - Full discovery suite: 173 tests total (160 passed, 0 failures, 13 known environment-dependent errors: 6 NVENC, 1 macOS, 6 Windows terminal cp1252 print).
  - **Live Runtime Verification**: Verified live against server at `http://127.0.0.1:8766/`:
    1. Verified `GET /api/connections` serves tiered OAuth status (`healthy`), channel identity, and local 2026 quota tracker.
    2. Verified `POST /api/connections/test` tests OAuth credentials, refreshes tokens, and queries channel identity.
    3. Verified `POST /api/video/reconcile` and `POST /api/video/resolve` handle ambiguous states with CSRF protection.
- **Stage 5 Completed (Video Library & Review)**:
  - **Editorial Review Status & Separation of Concerns**: Added explicit review state (`unreviewed`, `approved`, `rejected`) strictly decoupled from processing status (`ready`, `rendering`, `uploaded`, `failed`). Existing records default to `unreviewed` (uploaded status does NOT imply approved). Persisted in SQLite `videos` table via `POST /api/video`. Strict validation returns HTTP 400 on invalid values.
  - **Re-render Review State Semantics**: Genuinely re-rendering a card with new style/content resets `review_status` to `"unreviewed"` only upon successful completion. Failed renders preserve the previous review decision.
  - **Library Toolbar with Multi-Faceted Filters & Instant Search**: Added responsive search across title, headline, channel, source, URL, and video ID. Added faceted filters for processing status (`ready`, `uploaded`, `failed`, `rendering`), review status (`unreviewed`, `approved`, `rejected`), source platform (`youtube`, `reddit`, `manual`), style presets (`classic_blue`, `warm_amber`, `emerald_compact`, `cyber_violet`, `unset`), and media health (`intact`, `missing`). Supports sorting by newest, oldest, title, status.
  - **Artifact Health & Missing File Handling**: Dynamic health inspection returns `video_exists`, `poster_exists`, `raw_exists`, and `file_size_mb`. UI displays warning badges on cards and a dedicated missing-file placeholder in the modal instead of a broken/black player. Raw footage in `work/` is inspected to inform whether re-rendering is possible.
  - **Comprehensive Video Review Modal**: Organizes Preview, Editable Metadata, Editorial Review Decision (`Unreviewed`, `Approved`, `Rejected`), Technical Source & Licensing Evidence (platform, channel, clickable source URL, candidate key, objective license indicator: Public Domain, Creative Commons detected, Standard, Manual), and Stage 4 Pipeline Activity (latest job status, duration, failure diagnostic box, 1-click retry, link to full job log).
  - **Job-to-Video Matching & Lineage**: Implemented `job_matches_video()` with deterministic precedence (action key match -> summary metadata `video_id` -> target file stem fallback) and `get_latest_job_for_video()`. Excludes unrelated jobs and handles retries cleanly.
  - **Deletion Concurrency & Safety**: `POST /api/video` with `action: "delete"` checks active jobs under `GUARD` and raises HTTP 409 Conflict if an active render/upload job targets the video. Enforces `out/` containment and preserves `work/*_raw.mp4`.
  - **Test Suite**:
    - `tests/test_stage5_library.py`: 15/15 passed (0.9s).
    - `tests/test_stage4_pipeline.py`: 14/14 passed (0.8s).
    - `tests/test_stage3_reliability.py`: 12/12 passed (4.7s).
    - `tests/test_stage2_queue_cancel.py`: 11/11 passed (6.0s).
    - `tests/test_style_presets.py`: 14/14 passed (0.9s).
    - Full discovery suite: 135 tests total, 128 passed, 0 failures, 7 known environment-specific errors (6 NVENC, 1 macOS).
  - **Runtime Verification**: Verified live against server at `http://127.0.0.1:8766/`:
    1. Loaded all 79 videos with enriched health and review status.
    2. Verified uploaded videos are unreviewed by default.
    3. Mutated review status to approved, verified SQLite persistence, reset to unreviewed.
    4. Verified invalid review status rejected with HTTP 400.
    5. Verified active job deletion blocked with HTTP 409 Conflict.
    6. Verified missing video file flagged with `video_exists: False`.
  - **Next Stage**: Stage 6 (Publishing & Automation Management).
- **Stage 4 Completed (Pipeline & Job Management)**:
  - **Persistent Job Execution Metadata**: SQLite `jobs` records now track `started_at` (populated at `pending -> running` transition), `finished_at` (populated on all terminal outcomes: completed, failed, cancelled, interrupted), and execution `duration_seconds` (`round(finished - started, 1)` strictly execution time, distinct from queue wait time).
  - **Pipeline Stage Timeline**: Capped at 50 events (`job["stages"]`) with consecutive stage deduplication. Every state transition, worker step, and agent step records timestamped progress.
  - **Structured Failure Diagnostics**: 4-tier fallback extraction (`KENAU_SUMMARY` message -> exception/error keyword -> non-zero exit code -> log tail). Automatic secret redaction for API keys (Google `AIza...`, OpenAI `sk-...`), bearer tokens (`Bearer ...`), and sensitive query parameters.
  - **Persistent Job History API**: Added `GET /api/jobs` supporting `status` filtering (`all`, `pending`, `running`, `completed`, `failed`, `cancelled`, `idle`, `interrupted`) and `limit` parameter (1 to 200). Serves lightweight records with logs capped at 500 characters, while `/api/job?id=` provides full logs.
  - **Retry of Eligible Terminal Jobs**: `POST /api/job/retry` under `GUARD` reconstructs commands from semantic inputs (`action`, `key`, `extra`) using current `sys.executable`. Links `new_job["retry_of"]` and `source_job["retried_by"]`. Rejects active (`pending`, `running`) or already succeeded (`completed`, `idle`) jobs. Rejects duplicate active retries. Preserves immutable history on original records.
  - **Queue Management Visibility & Cancellation by ID**: `POST /api/job/cancel` accepts `{ "id": job_id }` to cancel specific pending or running jobs. Immediate process termination, artifact cleanup, queue unblocking, and queue event signalling.
  - **Worker Progress Reporting**: `studio/worker.py` emits `agent.emit_progress()` stages (`Preparing render`, `Applying style preset`, `Compositing video card`, `Finalizing render`) and `agent.emit_summary()` structured payloads for render and upload actions.
  - **Pipeline & Queue Studio UI**: Added first-class "Pipeline & Queue" tab (`data-page="queue"`), real-time active execution card, queued tasks list with position badges and individual cancel buttons, execution history table with filter tabs (`All`, `Completed`, `Failed`, `Cancelled`), and comprehensive Job Detail modal (`#job-detail-modal`) with stage progression stepper, diagnostic failure box, retry actions, and direct log viewer link.
  - **Test Suite**:
    - `tests/test_stage4_pipeline.py`: 14/14 tests passed (0.7s).
    - `tests/test_stage3_reliability.py`: 12/12 tests passed (4.8s).
    - `tests/test_stage2_queue_cancel.py`: 11/11 tests passed (6.1s).
    - `tests/test_style_presets.py`: 14/14 tests passed (0.2s).
    - Full test suite: 120 tests total, 112 passed, 0 failures, 8 known environment-specific errors/skips (6 NVENC, 1 macOS, 1 flaky socket timing).
  - **Live Runtime Verification**: Verified against running server at `http://127.0.0.1:8766/`:
    1. Normal job: Real YouTube download and ffmpeg composite rendered (`out/short_1790361134_yt_Ba_vdCp.mp4`) with `status: completed`, `duration_seconds: 158.7s`, 7 stages.
    2. Multiple queued jobs: Enqueued `j1` and `j2`, verified positions, cancelled `j2` from queue.
    3. Active cancellation: Cancelled running job, verified `status: cancelled`, `stage: Cancelled by user`, `duration_seconds: 1.4s`.
    4. Controlled failure: Triggered failing worker job, verified `status: failed`, clean diagnostic `ValueError: Video nonexistent_draft_999 not found`.
    5. Retry: Retried cancelled job, verified new job created with `retry_of` and `retried_by` linkage.
    6. Persistent job history API: Retrieved `/api/jobs` with pagination and status filters.
    7. Server-lock mutual exclusion: Confirmed second instance rejected cleanly with `.server.lock` (`[Errno 13] Permission denied`).
- **Manual Style Preset Override in Studio UI (Completed)**:
  - Backend validation: Updated `/api/video` POST handler in `studio/server.py` to validate `style_preset` against known presets in `STYLE_PRESETS` or empty string (`""` / `None` for Auto), rejecting invalid preset names or non-string values with a clear error.
  - Re-rendering with preset: Updated `studio/worker.py`'s `"render"` action to read `record.get("style_preset")`. When a preset is set on the record, it applies that preset directly (skipping aspect probing). If unset/empty (`""`), it performs aspect-ratio matching (or fallback) automatically. Also preserves the original `raw_video` reference for future re-renders.
  - Frontend UI: Added a "Card Style" dropdown to `openVideoModal()` in `studio/web/app.js` populated with the 4 presets (`classic_blue`, `warm_amber`, `emerald_compact`, `cyber_violet`) plus an "Auto (match video shape)" option, defaulting to the video's current `style_preset`. Updated `saveVideoEdits()` to include `style_preset`, which is saved prior to re-rendering in `reRenderDraft()`.
  - Tests: Added unit tests in `tests/test_style_presets.py` covering `/api/video` validation and `worker.py` preset handling (14/14 passed). Full suite: 106 tests total, 99 passed, 0 failures, 7 known environment errors.
  - Manual verification: Verified against live server using `short_1790296060_yt_ctZOWjE`, changing preset from `cyber_violet` (4:5) to `warm_amber` (4:3), saving and re-rendering via persistent job queue, and verifying output video and poster adopted the amber border (`#F59E0B`) and 4:3 frame.
- **Aspect-Ratio Matched Style Presets for Rendered Cards (Completed)**:
  - Replaced random style preset selection in `_render_pick()` (`core/agent.py`) with intelligent aspect-ratio matching based on the source video's actual dimensions:
    - `get_source_aspect_ratio(video_path)` uses `ffprobe` to determine width/height and returns $w/h$ as a float. Handled gracefully with fallback on ffprobe failure or missing file.
    - `match_style_preset_to_aspect(aspect_ratio)` selects the preset whose `video_aspect` is numerically closest:
      - Wide / landscape (>= 1.55, e.g. 16:9) -> `classic_blue` (16:9)
      - Medium landscape (1.17 to 1.55, e.g. 4:3, 3:2) -> `warm_amber` (4:3)
      - Square (0.90 to 1.17, e.g. 1:1) -> `emerald_compact` (1:1)
      - Tall / portrait (< 0.90, e.g. 4:5, 9:16) -> `cyber_violet` (4:5)
    - Fallback: if ffprobe fails or aspect ratio cannot be determined, logs a warning and falls back to random selection (`choose_style_preset()`) as a safety net.
    - Overrides: honors pre-set `pick.style_preset` if already specified.
  - Tests: `tests/test_style_presets.py` expanded to 11 tests (all 11 passed). Full test suite: 103 tests total, 96 passed, 0 failures, 7 known environment errors.
  - Real verification: Rendered real source clips for landscape (1920x1080 -> `classic_blue`), portrait (1080x1920 -> `cyber_violet`), and square (1080x1080 -> `emerald_compact`).
- **Style Variety Presets for Rendered Cards (Completed)**:
  - Defined 4 pre-defined visual style presets in `core/style_presets.py` (exported via `core/render.py`):
    - `classic_blue`: 16:9 aspect, border `#1D9BF0`, corner radius 40
    - `warm_amber`: 4:3 aspect, border `#F59E0B`, corner radius 28, background_anchor_y 0.45
    - `emerald_compact`: 1:1 aspect, border `#10B981`, corner radius 48
    - `cyber_violet`: 4:5 aspect, border `#8B5CF6`, corner radius 32, background_zoom 1.05
  - Record persistence: `store.draft()` records `"style_preset": "<name>"` on the video document in the `videos` table.
- **Stage 3 Completed (Reliability Hardening)**:
  - Addressed all 4 audit findings from the Stage 2 Post-Implementation Audit:
    1. **Multi-Instance Server Collision Prevention**: Subclassed `ThreadingHTTPServer` as `StudioServer(allow_reuse_address=False)` and introduced non-blocking directory file locking (`.server.lock` via `msvcrt.locking` on Windows / `fcntl.flock` on POSIX). In `run_server()`, socket binding and server locking occur *before* running `recover_interrupted_jobs()`, so duplicate server launches fail immediately without killing running jobs or mutating records. Added `.server.lock` to `.gitignore`.
    2. **False Success on Zero Picks / Discovery Failure**: In `run_job_process()`, now inspects `job.get("summary", {}).get("status")` before evaluating process exit code. Emitted summaries with `"failed"` or `"idle"` set terminal statuses to `"failed"` and `"idle"` respectively with descriptive stages (e.g. `Failed: No candidate passed the editorial filter.`, `Idle: No new candidates to process.`), preventing empty runs from being marked `"completed"`.
    3. **Cancellation vs Completion Race Window Elimination**: In `run_job_process()`, the post-process status inspection, artifact cleanup, and `store.put("jobs", ...)` are synchronized under `with GUARD:`. Late cancellations cannot be overwritten by late completion writes.
    4. **PID Recycling Protection Without psutil**: Added `get_process_creation_time()` using Windows `kernel32.GetProcessTimes` via standard library `ctypes.wintypes.FILETIME` (100-nanosecond precision). Recorded `job["pid_created_at"]` at process spawn. In `terminate_process_tree()` and `recover_interrupted_jobs()`, verifies that active process creation times match `pid_created_at` before issuing `taskkill`, preventing termination of recycled PIDs.
  - Test suites:
    - `tests/test_stage3_reliability.py`: 12/12 passed (4.8s).
    - `tests/test_stage2_queue_cancel.py`: 11/11 passed (6.1s).
  - Maintained zero build step, pure standard library, no external queue/process dependencies (`psutil`, Redis, Celery, etc.).
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

---

## Stage 2 Post-Implementation Audit

### Audit Date
2026-09-21

### Audited Commit
`40f865c` (`feat(stage2): process reliability, job cancellation, and persistent serial queue`)

### Audit Scope
Process lifecycle, PID tracking, cancellation, persistent FIFO queue, restart recovery, multi-instance behavior, race conditions, artifact cleanup, false-success paths, and Stage 2 security.

### Test Results
- **Dedicated Stage 2 Tests** (`tests/test_stage2_queue_cancel.py`):
  - 11/11 passed (6.2s). Zero errors, zero failures, zero warnings.
- **Full Discovery Test Suite** (`python -m unittest discover -s tests -p "test_*.py"`):
  - 80 tests total: 72 passed, 0 failed, 8 errors, 0 skipped.
  - Error breakdown:
    - 6 errors in `test_render.py`: environment-specific missing Nvidia CUDA / NVENC driver (`[h264_nvenc] Cannot load nvcuda.dll`) on this Windows test host.
    - 1 error in `test_uninstall_service.py`: macOS-specific test running on Windows attempting `os.getuid()`.
    - 1 error in `test_studio_server.py`: loopback socket abort (`WinError 10053`) during high-frequency discovery run (passes 11/11 in isolation).

### Verification Results

| Area | Status | Evidence / Finding |
|---|---|---|
| PID tracking | **VERIFIED** | Child PID recorded under `GUARD` into `job["pid"]` and persisted to SQLite immediately upon `Popen` creation. |
| Process identity | **PARTIALLY VERIFIED / RISK** | `is_python_process()` identifies `python.exe` and `ffmpeg.exe` via `tasklist`. However, if Windows recycles a PID for an unrelated external Python process, the validator cannot differentiate it from KenauShorts. |
| Process termination | **VERIFIED** | `taskkill /F /T /PID` terminates real process trees. Safety guards explicitly refuse `os.getpid()`, negative PIDs, and PID 0. |
| Active cancellation | **VERIFIED** | Real child process terminated via `taskkill`, partial artifacts unlinked, `status: "cancelled"` recorded, and worker queue unblocked. |
| Pending cancellation | **VERIFIED** | Queued job marked `status: "cancelled"` in SQLite; queue worker skips execution when reached. |
| FIFO queue | **VERIFIED** | Verified with real subprocesses logging execution order: strictly sequential A -> B -> C without overlapping. |
| Queue persistence | **VERIFIED** | Jobs backed by SQLite `jobs` table; pending jobs survive server restarts and execute in chronological FIFO order. |
| Restart recovery | **VERIFIED** | `recover_interrupted_jobs()` reconciles running jobs to `interrupted`, terminates orphan processes, and preserves pending jobs. |
| Multi-instance safety | **RISK IDENTIFIED** | Concurrency=1 is guaranteed ONLY within one server process. Because Python's `ThreadingHTTPServer` sets `SO_REUSEADDR = True` by default on Windows, multiple servers can bind to port 8766 simultaneously. Furthermore, Server 2's startup recovery will terminate Server 1's active child process. |
| Race conditions | **RISK IDENTIFIED** | In `run_job_process()`, if a cancel request arrives in the narrow window after child process exit 0 but before the `finally` block `store.put()`, `job["status"] = "completed"` can overwrite `"cancelled"`. |
| Artifact cleanup | **VERIFIED** | `cleanup_job_artifacts()` enforces `p.resolve().is_relative_to(out_dir)` and `p.is_file()`, preventing arbitrary file deletion or directory unlinking. |
| False-success prevention | **RISK IDENTIFIED** | `server.py` evaluates completion solely via exit code (`code == 0`). In `core/agent.py`, when discovery or editorial produces zero picks, it outputs `KENAU_SUMMARY {"status": "failed"}` but executes `return` (exit code 0), causing `server.py` to record the job as `"completed"`. |
| Security | **VERIFIED** | Subprocess calls use argument lists with `shell=False` (no command injection). CSRF/Origin enforcement and path containment are preserved. |

### Manual Verification
- **Test 1 (Sequential FIFO)**: Verified execution sequence `['START A', 'END A', 'START B', 'END B', 'START C', 'END C']`.
- **Test 2 (Cancellation)**: Verified active process tree terminated, SQLite status updated to `cancelled`, and `ACTIVE_JOB` cleared.
- **Test 3 (Controlled failure)**: Exit code 1 correctly records `failed`. Exit code 0 with failure summary records `completed` (demonstrating the false-success edge case).
- **Test 4 (Restart recovery)**: Orphan child process killed, job status updated to `interrupted`, pending job preserved.
- **Test 5 (Duplicate server startup)**: Two `ThreadingHTTPServer` instances bound to port 8766 simultaneously on Windows without collision error due to `SO_REUSEADDR`.
- **Test 6 (Duplicate start operations)**: Separate UUIDs generated; second job enqueued at position 1.

### Findings

#### VERIFIED
- Single-worker FIFO queue operates correctly within a single server instance.
- Subprocess tree termination (`taskkill /F /T`) reliably cleans up child processes on Windows.
- Active and pending job cancellation behaves as designed.
- Artifact cleanup safely restricts unlinking to files inside `out/`.

#### PARTIALLY VERIFIED / RISKS
1. **Multi-Instance Server Collision (Medium Severity)**: Multiple `server.py` instances can bind the same port simultaneously on Windows due to default `SO_REUSEADDR`. An accidental second instance launch will kill the first instance's active job during `recover_interrupted_jobs()`.
2. **False Success on Zero Picks (Medium Severity)**: `core/agent.py` exits with code 0 on editorial failure, causing `server.py` to report `completed` instead of `failed`.
3. **Completion vs Cancel Race Window (Low Severity)**: Late cancellation arriving immediately after process exit 0 can be overwritten by `"completed"` in the `finally` block.
4. **PID Recycling Blindspot (Low Severity)**: `is_python_process()` relies solely on process name `python.exe` from `tasklist`, which does not verify process start time or command arguments.

### Stage 3 Considerations
1. In `studio/server.py`: Set `ThreadingHTTPServer.allow_reuse_address = False` (or acquire a named Windows mutex / file lock on startup) before running `recover_interrupted_jobs()` so duplicate server instances fail immediately without terminating active jobs.
2. In `studio/server.py`: Inspect `job.get("summary", {}).get("status")` in addition to exit code `code == 0` when setting final job status to prevent false success.
3. In `run_job_process()`: Acquire `GUARD` and verify that the job was not marked `"cancelled"` before writing the final completion record in the `finally` block.
4. In `core/agent.py`: Consider exiting with `sys.exit(1)` when all candidates or editorial picks fail.

### Audit Conclusion
Stage 2 meets all single-instance operational requirements: process tracking, process tree cancellation, persistent FIFO queueing, and restart recovery are fully functional and supported by real runtime tests. The four architectural edge cases documented above are recorded for remediation in subsequent stages.

---

## Stage 3 Implementation — Reliability Hardening (Verified)

### Remediated Audit Findings

| Finding | Severity | Resolution Implemented | Verification Method | Result |
|---|---|---|---|---|
| **1. Multi-Instance Server Collision** | Medium | Subclassed `ThreadingHTTPServer` to `StudioServer(allow_reuse_address=False)`. Added non-blocking directory lock `server_lock()` on `.server.lock`. In `run_server()`, locking and port binding occur prior to `recover_interrupted_jobs()`. Added `.server.lock` to `.gitignore`. | `test_server_lock_mutual_exclusion`<br>`test_studio_server_rejects_address_reuse`<br>`test_run_server_does_not_recover_jobs_on_collision` | **VERIFIED** (Duplicate server fails immediately, active jobs untouched) |
| **2. False Success on Zero Picks** | Medium | In `run_job_process()`, inspects `job.get("summary", {}).get("status")` before exit code fallback. Handles `"failed"` -> `"failed"` and `"idle"` -> `"idle"` with descriptive stage messages. | `test_run_job_process_failed_summary_marks_failed`<br>`test_run_job_process_idle_summary_marks_idle`<br>`test_run_job_process_completed_summary_marks_completed`<br>`test_run_job_process_nonzero_exit_marks_failed` | **VERIFIED** (Zero candidates mark `idle`, editorial rejections mark `failed`) |
| **3. Completion vs Cancel Race Window** | Low | Enclosed final status evaluation, cancellation check, artifact cleanup, and `store.put("jobs", ...)` in `run_job_process()` within `with GUARD:`. Atomic transition prevents interleaved cancel writes from being overwritten. | `test_cancellation_during_job_run_preserves_cancelled`<br>`test_cancel_on_completed_or_idle_job_returns_existing_status` | **VERIFIED** (Cancelled state preserved; terminal status cannot be regressed) |
| **4. PID Recycling Blindspot** | Low | Implemented `get_process_creation_time()` using standard library `ctypes` (`kernel32.GetProcessTimes` with 100ns `FILETIME` resolution). Tracked `job["pid_created_at"]` at process spawn. In `terminate_process_tree()` and `recover_interrupted_jobs()`, process creation time is matched before `taskkill`. | `test_get_process_creation_time_returns_valid_timestamp`<br>`test_terminate_process_tree_refuses_when_creation_time_mismatched`<br>`test_recover_interrupted_jobs_with_mismatched_creation_time_leaves_process_alive` | **VERIFIED** (Recycled PIDs safely ignored, matching PIDs terminated) |

### Test Evidence
- **Stage 3 Test Suite** (`tests/test_stage3_reliability.py`): 12/12 passed (4.8s).
- **Stage 2 Test Suite** (`tests/test_stage2_queue_cancel.py`): 11/11 passed (6.1s).
- **Total Dedicated Reliability Tests**: 23/23 passing.

---

## Manual Style Preset Override + Stage 3 Reliability Regression Audit

### Audit Date
2026-09-25

### Audited Commit
`f5c29ed` (`feat: add manual style preset override to video review modal`)

### Working Tree Pre-Audit Condition (Critical Finding)
- Prior to the start of this audit, `core/agent.py` was found modified on disk (uncommitted, timestamp `2026-09-25 08:43:26`).
- **Diff Analysis**: Lines 207-224 inside `discover_rss` were corrupted with an incomplete, unindented YouTube snippet containing undefined variables (`vid`, `sn`, `q`).
- **Status & Risk**: Per audit rules, this pre-existing change was NOT committed and NOT included in the audited commit `f5c29ed`. It represents an uncommitted regression risk in the working tree for RSS discovery if left uncorrected by subsequent maintenance.

### Audit Scope
1. **Manual Style Preset Override**: Modal review UI (`studio/web/app.js`), backend `/api/video` validation and persistence (`studio/server.py`), and worker re-rendering / aspect fallback (`studio/worker.py`).
2. **Stage 3 Reliability Hardening Regression Check**: Multi-instance collision prevention (`.server.lock` and `StudioServer`), false-success prevention (inspecting `job["summary"]["status"]`), cancellation vs completion race prevention under `GUARD`, and PID recycling protection (`GetProcessTimes`).
3. **Automated & Manual Verification**: Automated unit test suites, full test discovery, and live runtime testing against the active server.

### Test Results

- **Targeted Test Suites**:
  - `tests/test_style_presets.py`: 14/14 passed (1.0s).
  - `tests/test_stage3_reliability.py`: 12/12 passed (5.1s).
  - `tests/test_stage2_queue_cancel.py`: 11/11 passed (6.7s).
- **Full Discovery Test Suite** (`$env:PYTHONUTF8='1'; python -m unittest discover -s tests -p "test_*.py"`):
  - 106 tests total: 99 passed, 0 failures, 7 errors, 0 skipped.
  - 6 errors in `test_render.py`: Expected host limitation (missing NVIDIA CUDA/NVENC hardware encoder on local Windows environment: `[h264_nvenc] Cannot load nvcuda.dll`). Normal rendering correctly defaults to `libx264`.
  - 1 error in `test_uninstall_service.py`: Expected platform limitation (macOS-specific test calling `os.getuid()` running on Windows).
  - Flakiness Note: `test_stage2_queue_cancel.py`'s `test_api_queue_and_status_reporting` passes reliably in isolation, but can exhibit a race condition in full suite runs if the active background queue worker picks up a dummy test job before `GET /api/queue` completes.

### Verification Results

| Component | Status | Evidence / Verification Details |
|---|---|---|
| **Modal UI (`app.js`)** | **VERIFIED** | Dropdown `#edit-style-preset` populated with Auto option and all 4 presets. `saveVideoEdits()` captures and sends `style_preset`. `reRenderDraft()` saves edits before enqueuing render job. |
| **Backend Validation (`server.py`)** | **VERIFIED** | `/api/video` POST handler runs under `GUARD`, rejects unknown preset names (e.g. `'neon_punk'`) and non-string types with HTTP 400. Accurately persists valid presets and empty string to SQLite `videos` table. |
| **Worker Re-rendering (`worker.py`)** | **VERIFIED** | Worker `"render"` action respects `record.get("style_preset")`. When set, applies preset directly; when empty (`""`), automatically probes aspect ratio via `ffprobe` and matches preset. Preserves `raw_video` reference across repeated re-renders. |
| **Multi-Instance Server Guard** | **VERIFIED** | Real-world manual launch of a second server instance `python -m studio.server` while instance 1 is running was rejected immediately with `[ERROR] Cannot start KenauShorts Studio: Another KenauShorts server instance is already running (locked .server.lock): [Errno 13] Permission denied`. First instance unaffected. |
| **False-Success Prevention** | **VERIFIED** | `run_job_process()` evaluates `summary.get("status")` before evaluating process exit code. Unit tests verify `failed` and `idle` summaries set terminal statuses to `failed` and `idle`. |
| **Cancel vs Completion Race** | **VERIFIED** | Terminal status inspection, artifact unlinking, and SQLite update are atomic under `with GUARD:`. Verified in `test_cancellation_during_job_run_preserves_cancelled`. |
| **PID Recycling Protection** | **VERIFIED** | Process creation timestamp recorded at spawn with 100ns precision via Windows `kernel32.GetProcessTimes`. Process tree termination refuses termination if timestamp does not match. |

### Manual Live Verification
- **Test A (Inspection)**: Queried `short_1790296060_yt_ctZOWjE` via `GET /api/video`. Confirmed `style_preset: "cyber_violet"`.
- **Test B (Validation Rejection)**: POSTed invalid preset `"neon_punk"` -> rejected HTTP 400. POSTed integer `12345` -> rejected HTTP 400.
- **Test C (Mutation & Persistence)**: POSTed `"warm_amber"` -> returned HTTP 200, persisted in database.
- **Test D (Real Re-render)**: Enqueued render job via `POST /api/job`. Monitored through `running` to `completed`. Verified new video file `short_1790296060_yt_ctZOWjE_edit_c1748b.mp4` generated (6,096,508 bytes) and `raw_video` path preserved.
- **Test E (Auto Mode & Chain Re-render)**: POSTed `""` (Auto mode) and re-rendered. Job completed successfully, generating `short_1790296060_yt_ctZOWjE_edit_00d01e.mp4` (7,842,513 bytes) with `raw_video` maintained across multiple re-renders.
- **Test F (Multi-instance Collision)**: Executed `python -m studio.server` in a secondary process. Exited immediately with `.server.lock` permission denied error. Primary server remained alive and responsive.

---

## Stage 7 Phase 1: State Consistency, Server Lock Unblocking & SQLite Reliability

### Implementation Date
2026-10-01

### Baseline Commit
`46473dd` (`fix(stage6): remove approval gate and restore automatic publishing`)

### Implemented Scope & Architectural Changes

1. **Server Lock Unblocking (PERF-01)**:
   - **Problem**: Outbound network I/O operations (`test_youtube_connection()`, `reconcile_video_upload()`, and `fetch_subreddit_posts()`) were executing while holding the server-wide `GUARD` lock. A slow external API call or network timeout froze Studio and blocked all unrelated endpoints such as `GET /api/status`.
   - **Resolution**:
     - Removed coarse `with GUARD:` in `StudioHandler.do_POST()`. Local in-memory state mutations inside `StudioHandler.mutate()` (`/api/video`, `/api/connections`, `/api/subreddits`, `/api/youtube_channels`, `/api/youtube_queries`, `/api/settings`, `/api/wizard`) are individually scoped under `with GUARD:`.
     - In `test_youtube_connection()`, eliminated `GUARD` acquisition. Added `_CONNECTION_TEST_LOCK = threading.Lock()` with non-blocking acquisition (`_CONNECTION_TEST_LOCK.acquire(blocking=False)`), returning `{"status": "busy", "message": "A connection test is already in progress."}` if already running.
     - In `reconcile_video_upload()`, fine-grained the locking: `GUARD` is acquired only to read the video record, HTTP PUT to Google resumable upload URI is executed outside `GUARD`, and `GUARD` is re-acquired to persist the updated record and synchronize `state.json`.
     - Added process-level per-video lock set `_RECONCILING_VIDEOS: set[str]` protected by `_RECONCILE_LOCK = threading.Lock()` to prevent duplicate concurrent reconciliations on the same video, raising HTTP 409 `ConflictError`.
     - `/api/subreddits/test` (`fetch_subreddit_posts`) executes network calls completely outside `GUARD`.

2. **Manual Resolution State Synchronization (DATA-01)**:
   - **Problem**: Confirming an ambiguous upload as successful via `resolve_manual_video()` with `confirm_uploaded` updated the SQLite database record to `uploaded` but did not synchronize `state.json`. This left candidate posts unmarked in `state.posted` and `state.seen`, allowing automated discovery loops to re-discover and re-process duplicate videos.
   - **Resolution**:
     - Added helper `_sync_candidate_posted(record, youtube_id)` in `studio/server.py` invoked both upon `confirm_uploaded` manual resolution and upon successful HTTP 200/201 reconciliation.
     - Synchronizes candidate key (`record["candidate"]["key"]` fallback to `record["key"]` or `id`), title, headline, video path, confirmed `youtube_id`, and timestamp into `state.json` via `State.mark_posted()`.
     - Ensured idempotency in `resolve_manual_video()`: if an already-`uploaded` video is re-confirmed with the same `youtube_id`, it re-syncs `state.json` and returns success without error. Attempting to re-confirm with a conflicting `youtube_id` raises `ConflictError` (HTTP 409).
     - Enhanced `core/state.py` `State.mark_posted(entry)` to be fully idempotent: updates existing entry fields in-place and refreshes `self.seen[key]` without appending duplicate records to `self.posted`.

3. **SQLite Concurrency & Schema Cleanup (DATA-02)**:
   - **Problem**: SQLite was operating in default rollback journal mode with default busy timeout, risking contention. Additionally, `connect()` in `studio/store.py` executed `CREATE TABLE IF NOT EXISTS subreddits`, an unused and inconsistent table schema (all subreddits are stored in `config.json`).
   - **Resolution**:
     - In `studio/store.py` `connect()`, configured:
       - `PRAGMA journal_mode=WAL;` (Write-Ahead Logging for high-concurrency readers & writers).
       - `PRAGMA synchronous=NORMAL;` (safe durability with reduced disk sync overhead).
       - `PRAGMA busy_timeout=15000;` (15s wait to gracefully handle transaction concurrency).
     - Removed `CREATE TABLE IF NOT EXISTS subreddits` from `connect()`.
     - Added strict table validation in `store.put()`, `store.get()`, `store.records()`, and `store.delete()`: raises `ValueError(f"Unsupported table: {table}")` if table is not in `("videos", "jobs")`.

### Test Evidence
- **Stage 7 Phase 1 Suite** (`tests/test_stage7_phase1.py`): 13/13 passed (2.1s).
  - Tests non-blocking `/api/status` during slow YouTube connection test.
  - Tests busy status on concurrent YouTube connection test.
  - Tests non-blocking `/api/status` during slow upload reconciliation.
  - Tests HTTP 409 ConflictError on concurrent duplicate video reconciliation.
  - Tests `state.json` candidate synchronization on reconciliation completion.
  - Tests manual `confirm_uploaded` synchronization to `state.json`.
  - Tests idempotency and conflict prevention in `resolve_manual_video()`.
  - Tests rejection of inappropriate video statuses.
  - Tests SQLite WAL, synchronous=NORMAL, and busy_timeout pragmas.
  - Tests table validation across all store CRUD operations.
  - Tests absence of `subreddits` table creation on clean databases.
  - Tests idempotent `State.mark_posted()`.
- **Targeted Publishing & Server Test Suites**:
  - `tests/test_stage6_publishing.py`: 38/38 passed (6.9s).
  - `tests/test_studio_server.py`: 11/11 passed (3.8s).
  - `tests/test_studio_store.py`: 9/9 passed (0.5s).
- **Combined Targeted Test Run**: 71/71 passed (17.2s).
- **Full Discovery Test Suite** (`$env:PYTHONUTF8='1'; python -m unittest discover tests`):
  - 186 tests total: 179 passed, 0 failures, 7 known environment-specific errors (6 NVENC on non-CUDA host, 1 macOS test calling `os.getuid()` on Windows).

---

## Stage 7 Phase 1 Corrective Fix: State Sync Recovery & Connectivity Unblocking

### Implementation Date
2026-10-01

### Baseline Commit
`691ea35` (`feat(stage7): state consistency, server lock unblocking and sqlite wal`)

### Addressed Issues & Architectural Changes

1. **Recoverable SQLite and state.json Synchronization (Fix 1)**:
   - **Problem**: In commit `691ea35`, `reconcile_video_upload()` could persist `status="uploaded"` in SQLite but fail during `_sync_candidate_posted()` due to transient I/O errors. On subsequent retry, the function returned early because SQLite already recorded `uploaded`, leaving `state.json` permanently out-of-sync and risking duplicate candidate discovery.
   - **Resolution**:
     - Updated `reconcile_video_upload()`: if the video is already marked `uploaded`, it bypasses any outbound HTTP PUT/query and invokes `_sync_candidate_posted(video, yt_id)`.
     - Preserves the existing `youtube_id` and rejects conflicting IDs with `ConflictError` (HTTP 409).
     - Exceptions during synchronization are logged and re-raised so callers know synchronization is incomplete.
     - Thanks to idempotent `State.mark_posted()`, retries update existing candidate metadata in-place and refresh `seen` without duplicate entries.
     - Updated `recover_interrupted_jobs()` on server startup to scan all videos in SQLite: any video marked `uploaded` with a valid `youtube_id` is passed through `_sync_candidate_posted()`, automatically healing missing `state.json` entries upon process restart.

2. **Decoupled Network Probing from /api/status Lock (Fix 2)**:
   - **Problem**: In `StudioHandler.do_GET()`, `is_online()` performed a raw blocking socket connection while holding `with GUARD:`. Under slow DNS resolution or poor network connectivity, every `/api/status` request froze the server-wide `GUARD` lock for several seconds, blocking all worker job updates and client queries.
   - **Resolution**:
     - Separated `is_online()` execution from `GUARD`: `/api/status` now evaluates connectivity before acquiring `with GUARD:` strictly for in-memory status reads.
     - Introduced an in-memory cache: `_ONLINE_CACHE: dict[str, Any] = {"status": True, "checked_at": 0.0}` with `_ONLINE_LOCK = threading.Lock()` and a 10.0-second TTL (`ONLINE_CACHE_TTL = 10.0`).
     - `is_online(cached: bool = True)` checks cache freshness. If older than 10 seconds, it uses non-blocking lock acquisition (`_ONLINE_LOCK.acquire(blocking=False)`). If another thread is actively probing, callers immediately receive the cached status rather than stacking probes or stalling.
     - Extracted raw socket connection into `probe_network_connectivity() -> bool`, which catches all socket and OS errors safely, logs a warning, and returns `False`.
     - Freshness trade-off: Network status changes may take up to 10 seconds to reflect in `/api/status`, but `/api/status` and `GUARD` remain completely unblocked and responsive during network degradation or DNS delays.
     - Timing-sensitive tests mock `server.is_online` so pure lock contention can be benchmarked deterministically.

### Test Evidence
- **Stage 7 Phase 1 Suite** (`tests/test_stage7_phase1.py`): 18/18 passed (1.7s).
  - 13 Phase 1 baseline tests.
  - `test_reconcile_state_sync_failure_and_retry_recovery`: Verifies disk failure during state sync leaves SQLite as `uploaded`, retry repairs `state.json` with zero network calls, repeated retry does not duplicate entries, and conflicting IDs raise `ConflictError`.
  - `test_startup_recovery_repairs_missing_state_json`: Verifies `recover_interrupted_jobs()` heals missing `state.json` records on startup.
  - `test_status_slow_probe_does_not_block_guard`: Verifies 5-second slow network probe outside `GUARD` allows concurrent thread to acquire `GUARD` in <0.5s.
  - `test_status_concurrent_requests_use_cached_probe`: Verifies 5 concurrent requests return 200 OK without triggering simultaneous probes.
  - `test_status_probe_exception_does_not_break_status`: Verifies probe socket errors log warning, release probe lock, and return 200 OK with `online: False`.
- **Targeted Test Suites**:
  - `tests/test_stage6_publishing.py`: 38/38 passed (11.0s).
  - `tests/test_studio_server.py`: 11/11 passed (3.9s).
  - `tests/test_studio_store.py`: 9/9 passed (0.5s).
- **Full Discovery Test Suite** (`$env:PYTHONUTF8='1'; python -m unittest discover tests`):
  - 191 tests total: 184 passed, 0 failures, 7 known environment-specific errors (6 NVENC on non-CUDA Windows host, 1 macOS test calling `os.getuid()` on Windows).

---

## Stage 7 Phase 2: Worker Lifecycle & Automation Reliability

### Implementation Date
2026-10-01

### Baseline Commit
`dfb7004` (`fix(stage7): resolve state sync race and decouple network probe from guard`)

### Implemented Objectives & Architectural Improvements

1. **Subprocess Lifecycle & Watchdog Management**:
   - **Command-Level Timeouts**:
     - `core/render.py`: Configured explicit timeouts for media processing tools: `RENDER_TIMEOUT = 300.0` (5 minutes), `POSTER_TIMEOUT = 30.0`, `PROBE_TIMEOUT = 15.0`, `ENCODER_TIMEOUT = 10.0`. `composite()` and `render()` accept an optional `timeout: float` parameter. In case of timeout or non-zero exit, any partial output artifact is unlinked immediately.
     - `core/style_presets.py`: Configured `timeout=15.0` in `get_source_aspect_ratio()`.
     - `core/agent.py`: Hardened `download_clip()` with explicit handling for `subprocess.TimeoutExpired` and general errors, unlinking partial `.mp4`, `.part`, and `.ytdl` files upon failure.
   - **Job-Level Process Watchdog**:
     - `studio/server.py`: Introduced `DEFAULT_JOB_TIMEOUTS: dict[str, float] = {"upload": 900.0, "render": 600.0, "preview": 300.0, "run": 300.0, "youtube_connect": 180.0, "manual": 300.0}`.
     - Added an asynchronous watchdog (`threading.Timer(job_timeout, on_timeout)`) in `run_job_process()`. If execution exceeds the allotted duration, the watchdog terminates the active process tree via `terminate_process_tree(proc.pid, expected_created_at=proc_created_at)`, shuts down standard streams, cleans up partial artifacts, and records terminal status `failed` with stage `"Failed: Timed out after Xs"`.
     - Extended `is_python_process()` to recognize helper subprocess binaries including `yt-dlp` and `ffprobe`.
     - Ensured `terminate_process_tree()` is called in the `except Exception` handler if an unexpected exception occurs while the subprocess is still running.

2. **Worker Interruption & Queue Pre-Flight Validation**:
   - **Startup Interruption Recovery**:
     - Enhanced `recover_interrupted_jobs()`: whenever an active job is marked interrupted upon server startup, `cleanup_job_artifacts(job)` unlinks partial video or thumbnail files associated with the job before terminating any orphaned process tree.
   - **Pre-Flight Queue Validation**:
     - Hardened `queue_worker_loop()` before process spawning:
       - For `upload` jobs: validates that the video record exists in SQLite, has not already been uploaded (`status == "uploaded"`), is not locked in an unresolved/ambiguous state without an active resumable session, and that the underlying rendered video file actually exists on disk.
       - For `render` jobs: validates that the target record exists in SQLite.
       - Invalid jobs fail immediately under `GUARD` with a clear explanatory stage and error string (e.g., `"Video 'xyz' has already been uploaded (abc123)"` or `"Rendered video file for 'xyz' not found on disk"`), preventing the worker from spawning doomed worker subprocesses.

3. **Automation Queue Resilience**:
   - **Elimination of Pipeline Stalls**:
     - In `automation_loop()`, fixed the schedule update logic: previously, `settings["next_run"]` was immediately advanced by `interval_hours` (e.g. 5 hours) before checking for candidate readiness. If no ready candidates were available or if starting a job failed, the automation loop went dormant for 5 hours.
     - New behavior: when in publish mode with no ready videos available (or if queuing fails), `settings["next_run"]` is scheduled with a resilient 60-second backoff (`now_ts + 60.0`). The full `interval_hours` advancement is applied exclusively after a candidate job has been successfully queued or started.
     - FIFO candidate selection: candidate selection iterates through eligible ready videos ordered by `created_at`, safely skipping failed, errored, or non-ready videos without hanging the pipeline or requiring manual human intervention.

4. **Log Streaming & SQLite Write Overhead Reduction**:
   - **Append-Mode Log Streaming**:
     - Refactored `run_job_process()` to eliminate $O(N^2)$ rewrite overhead: instead of rewriting the entire log file to disk on every single output line, opened `{job_id}.log` in append mode (`open(..., "a", encoding="utf-8")`) and flushed each stdout line sequentially.
   - **Throttled SQLite Progress Updates**:
     - High-frequency stdout lines (e.g., ffmpeg frame progress or encoder dumps) are throttled so that SQLite updates occur at most once per 1.0 second (`last_db_write`).
     - State-change transitions (`KENAU_PROGRESS` and `KENAU_SUMMARY`) immediately bypass the throttle to update SQLite without delay.

### Test Evidence
- **Stage 7 Phase 2 Suite** (`tests/test_stage7_phase2.py`): 15/15 passed (5.0s).
  - `test_has_encoder_timeout_handled_gracefully`: Verifies timeout handling in `has_encoder`.
  - `test_probe_duration_and_audio_stream_timeouts`: Verifies timeout handling in ffprobe helpers.
  - `test_render_composite_timeout_unlinks_partial_out_and_raises`: Verifies ffmpeg timeouts unlink partial outputs and raise `TimeoutError`.
  - `test_download_clip_timeout_and_error_cleans_up_partial_files`: Verifies partial `.mp4`, `.part`, `.ytdl` files are cleaned up on download timeout or failure.
  - `test_run_job_process_watchdog_kills_hung_process_and_cleans_up`: Verifies watchdog termination of hung jobs, artifact cleanup, and stage recording.
  - `test_recover_interrupted_jobs_cleans_up_artifacts`: Verifies startup recovery cleans up partial artifacts.
  - `test_queue_worker_rejects_missing_video_preflight`: Verifies upload job for non-existent video fails pre-flight.
  - `test_queue_worker_rejects_already_uploaded_video_preflight`: Verifies upload job for already uploaded video fails pre-flight.
  - `test_queue_worker_rejects_missing_file_on_disk_preflight`: Verifies upload job for missing file on disk fails pre-flight.
  - `test_queue_worker_rejects_ambiguous_video_without_resume`: Verifies upload job for ambiguous state without resume fails pre-flight.
  - `test_queue_worker_executes_valid_job_after_preflight_rejection`: Verifies queue processor advances cleanly to valid jobs after rejecting invalid jobs.
  - `test_automation_loop_no_candidates_schedules_short_retry_not_interval`: Verifies 60s backoff when no candidates are ready.
  - `test_automation_loop_advances_interval_when_candidate_queued`: Verifies interval advancement when a job is queued.
  - `test_automation_loop_skips_failed_video_picks_next_ready_fifo`: Verifies FIFO ordering and skipping of failed videos.
  - `test_run_job_process_throttles_sqlite_writes_for_high_frequency_logs`: Verifies throttling of SQLite progress updates while preserving append-mode file logging.
- **Full Discovery Suite** (`python -X utf8 -m unittest discover tests`):
  - 206 tests total: 199 passed, 0 failures, 7 known environment-specific errors (6 NVENC on non-CUDA host, 1 macOS `os.getuid()` test on Windows).

---

## Stage 7 Phase 2 Corrective Patch: Live Video-State Recovery on Job Timeout

### Implementation Date
2026-10-01

### Baseline Commit
`926ce32` (`feat(stage7): improve worker lifecycle and automation reliability`)

### Addressed Issue & Architectural Improvements

1. **Live Video-State Recovery on Subprocess Timeout**:
   - **Problem**: In commit `926ce32`, when an upload or render job timed out and was killed by the watchdog in `run_job_process()`, the job record was marked `failed`, but the associated record in the SQLite `videos` table remained in `uploading` or `rendering`. This prevented immediate operator retry or reconciliation during continuous server execution until the server was restarted.
   - **Resolution**:
     - Introduced `_handle_job_timeout_video_state(job, timeout_seconds)` in `studio/server.py`.
     - Executed under `with GUARD:` immediately upon confirmed timeout in `run_job_process()` (both normal post-wait branch and exception handler).
     - **Render timeout**: transitions video from `rendering` to `render_failed` with error message `"Render process timed out after Xs."`.
     - **Upload timeout**: transitions video from `uploading` to `upload_unknown` with failure reason `"Upload process timed out after Xs. Resumable session or manual reconciliation required."`, while preserving existing `resumable_uri`, `video`, `title`, and candidate metadata intact.
     - **Terminal state protection**: strictly checks current video status and refuses to overwrite valid terminal or completed states (`uploaded`, `ready`, `failed`, `render_failed`, `upload_unknown`, `upload_unresolved`).
     - **Safe retry workflow**: operator can immediately re-render a `render_failed` video or reconcile/resolve an `upload_unknown` video without server restart.
     - **Duplicate publication guards preserved**: `automation_loop()` continues to strictly pick `ready` videos; `start_job()` and `queue_worker_loop()` continue to block duplicate or uncertain uploads.

### Test Evidence
- **Stage 7 Phase 2 Suite** (`tests/test_stage7_phase2.py`): 20/20 passed (8.3s).
  - 15 Phase 2 baseline tests.
  - `test_render_timeout_transitions_video_to_render_failed`: Verifies video transitions from `rendering` to `render_failed` on render timeout.
  - `test_upload_timeout_transitions_video_to_upload_unknown_and_preserves_metadata`: Verifies video transitions from `uploading` to `upload_unknown` and preserves `resumable_uri` and title on upload timeout.
  - `test_timeout_does_not_overwrite_uploaded_video`: Verifies timeout does not overwrite a video already marked `uploaded`.
  - `test_repeated_timeout_handling_is_safe_and_idempotent`: Verifies repeated calls to timeout recovery are idempotent and non-destructive.
  - `test_timed_out_video_retry_without_server_restart`: Verifies timed-out render jobs can be retried immediately through `retry_job()` without restarting the server.
- **Targeted Regression Suites**:
  - `tests/test_stage6_publishing.py`: 38/38 passed (6.6s).
  - `tests/test_stage7_phase1.py`: 18/18 passed (1.5s).
  - `tests/test_stage4_pipeline.py`: 14/14 passed (1.1s).
  - `tests/test_stage3_reliability.py`: 12/12 passed (5.3s).
  - `tests/test_stage2_queue_cancel.py`: 11/11 passed (6.3s).
  - `tests/test_studio_server.py`: 11/11 passed (0.8s).
  - `tests/test_studio_store.py`: 9/9 passed (0.6s).
- **Full Discovery Suite** (`python -X utf8 -m unittest discover tests`):
  - 211 tests total: 204 passed, 0 failures, 7 known environment-specific errors (6 NVENC on non-CUDA Windows host, 1 macOS `os.getuid()` test on Windows).

---

## Stage 7 Phase 3: Executive Operational Command Center, Quota Governance & Discovery Diagnostics

### Implementation Date
2026-10-02

### Baseline Commit
`d28c4ce` (`fix(stage7): recover video state on subprocess timeout`)

### Addressed Areas & Architectural Enhancements

1. **Operational Telemetry & YouTube Quota Governance**:
   - **Thread-Safe Local Activity Tracking**:
     - `studio/store.py` introduces `_QUOTA_LOCK = threading.Lock()` guarding reads and writes to `studio-quota.json`.
     - Preserves all existing keys (`videos_insert_count`, `search_list_count`, `general_units`, `videos_insert_limit`, `search_list_limit`, `general_units_limit`, `date_pt`, `disclaimer`).
     - Adds `search_limit_reached` boolean flag (`search_list_count >= search_list_limit`).
   - **Timezone-Aware Pacific Time Midnight Reset**:
     - YouTube Data API resets daily quotas at midnight US Pacific Time (PT).
     - Implemented `get_pacific_now(now_utc)`: attempts standard library `zoneinfo.ZoneInfo("America/Los_Angeles")`, falling back gracefully to exact US Daylight Saving Time calculation (PDT UTC-7 from 2nd Sunday in March to 1st Sunday in November, PST UTC-8 otherwise) without requiring external packages on Windows.
     - Implemented `get_pacific_date(now_utc)` and `get_pacific_reset_info(now_utc)` returning next midnight PT epoch timestamp (`reset_at`), ISO string (`reset_at_iso`), and exact seconds remaining (`seconds_remaining`).
     - On date roll-over in Pacific Time, counters automatically reset to zero for the new day.
   - **Discovery Pre-Flight Quota Guard**:
     - In `core/agent.py`'s `discover_youtube()`: inspects `search_list_count` against `search_list_limit` (100) before issuing outbound requests. If limit reached, skips search queries gracefully without crashing or stopping the pipeline.
     - Re-evaluates quota remaining before each individual query.
     - Records quota usage at the request boundary (`store.record_local_quota_activity("search_list", 1)`).
     - Handles HTTP 403 `quotaExceeded` by immediately setting search limit reached and recording diagnostic stats.
     - Non-search discovery sources (`discover_youtube_rss`, `discover_reddit`) continue normal operation even when search quota is exhausted, ensuring the pipeline remains productive.
   - **License Check Quota Tracking**:
     - In `core/agent.py`'s `verify_youtube_licenses()`: records `general` quota units at batch boundaries (1 unit per 50 video IDs) and tallies rejection reasons (`not public`, `all rights reserved`, `longer than Xs`, etc.).
   - **Proactive OAuth Health Reporting**:
     - `studio/server.py` implements `get_oauth_status_details()` returning non-blocking structured health status (`status`, `healthy`, `needs_attention`, `message`, `channel_title`, `channel_id`, `channel_custom_url`).
     - Strictly sanitized: never leaks access tokens, refresh tokens, client IDs, or client secrets.
     - Handled missing token/secret configurations gracefully with operator guidance.

2. **Discovery Diagnostics & Run Telemetry**:
   - **Structured Discovery Telemetry**:
     - `core/agent.py` passes `stats` dictionary through `discover_all_candidates`, `discover_youtube`, `verify_youtube_licenses`, and editorial curation.
     - Tracks configured queries, channels, and subreddits; attempted searches vs skipped searches; raw discovered candidates per source; cross-source duplicates (`duplicates_within_run`); unique candidates; seen candidate filtering (`already_seen`); and eligible candidate yield (`eligible_candidates`).
   - **Evidence-Based Idle Explanations**:
     - Implemented `generate_idle_explanation(stats)` in `core/agent.py` providing specific, actionable explanations when discovery produces zero candidates:
       - Search quota limit reached with zero candidates from other sources.
       - Configured sources returned zero items.
       - All discovered candidates were previously processed.
       - Discovered candidates were rejected by licensing or duration filters (with specific breakdown).
   - **Summary Telemetry Emission**:
     - `core/agent.py` emits `discovery_stats` in structured `KENAU_SUMMARY` on `completed`, `idle`, and `failed` pipeline outcomes.

3. **Executive Operational Command Center (Studio Overview UI)**:
   - **Markup & Layout Cleanup**:
     - In `studio/web/app.js` and `studio/web/style.css`, eliminated duplicate card markup, redundant panel headers, and repeated Free Disk Space cards.
     - Structured layout using clean CSS Grid (`.command-grid`).
   - **High-Visibility OAuth Alert Banners**:
     - Prominent banner rendered at top of Overview whenever OAuth status requires attention (`not_configured`, `configured`, `expired`, `invalid`, `error`) with direct navigation link to Connections tab.
   - **Operational KPI Cards**:
     - Metric cards for Ready videos, Uploaded videos, In Queue count, and Total Library videos.
   - **Live Automation Schedule & Countdown Timer**:
     - Displays automation mode (FIFO publishing vs preview drafts), next run timestamp, and dynamic real-time JavaScript countdown timer (`startOverviewCountdown`, `clearOverviewCountdown`) with proper page lifecycle teardown.
   - **System Storage Health Meter**:
     - Explicit color-coded storage safety thresholds: Critical (<512 MB, danger), Low Space (<1 GB, warning), and Healthy (>1 GB, success) with visual capacity badge.
   - **Visual Daily Quota Meters**:
     - Clean progress meters (`.meter-track`, `.meter-fill`) for YouTube Search (100 daily query cap), Video Uploads (100 cap), and General API Units (10,000 cap), along with dynamic countdown to Pacific Time midnight reset.
   - **Actionable Incident Remediation Cards**:
     - Automatically surfaces videos in abnormal states (`upload_unknown`, `upload_unresolved`, `render_failed`).
     - Provides safe 1-click remediation workflows directly in Overview:
       - `upload_unknown`: ⚡ Reconcile Upload (queries Google Resumable Upload protocol), ✓ Confirm, ✕ Fail.
       - `upload_unresolved`: ✓ Confirm Uploaded (prompts for YouTube Video ID), ✕ Mark Failed.
       - `render_failed`: ↺ Retry Render (re-enqueues render job immediately without server restart).
     - State transitions reuse existing backend endpoints (`POST /api/video/reconcile`, `POST /api/video/resolve`, `POST /api/job`) ensuring full validation, duplicate publication protection, and process tracking.
   - **Latest Discovery Diagnostics Card**:
     - Summarizes the most recent discovery run outcome, candidates yielded per source, duplicate counts, and rejected candidates with specific category tags (`.diag-tag`).

### Test Evidence
- **Stage 7 Phase 3 Suite** (`tests/test_stage7_phase3.py`): 18/18 passed (1.0s).
  - Quota Pacific time calculation and DST transitions (PST vs PDT).
  - Pacific reset timestamp, ISO string, and seconds remaining.
  - Quota tracker initialization and thread-safe concurrent recording.
  - Quota tracker midnight reset on Pacific day rollover.
  - Search limit reached flag enforcement.
  - Discovery pre-flight guard skips search when quota exhausted.
  - Request-boundary quota tracking per query.
  - 403 quotaExceeded handling and limit enforcement.
  - Non-search sources continue when search quota exhausted.
  - Cross-source candidate deduplication and seen tracking.
  - Evidence-based idle diagnostic explanation generation.
  - Non-blocking sanitized OAuth health check without secret leakage.
  - Extended `/api/status` payload verification (incidents, quota, oauth_health, latest_discovery).
  - Incident remediation resolve endpoint (`POST /api/video/resolve`).
  - Incident remediation reconcile endpoint (`POST /api/video/reconcile`).
  - License check general units tracking and rejection categorization.
  - Pipeline summary emission of `discovery_stats`.
  - UI assets structure and CSS classes contract verification.
- **Stage 7 Phase 1 & 2 Regressions**:
  - `tests/test_stage7_phase1.py`: 18/18 passed (1.8s).
  - `tests/test_stage7_phase2.py`: 20/20 passed (9.0s).
  - `tests/test_stage6_publishing.py`: 38/38 passed (6.3s).
- **Full Discovery Suite** (`python -X utf8 -m unittest discover tests`):
  - 229 tests total: 222 passed, 0 failures, 7 known environment-specific errors (6 NVENC on non-CUDA Windows host, 1 macOS `os.getuid()` test on Windows).
- **Compilation & Formatting Checks**:
  - `python -m compileall core studio scripts tests`: 0 errors.
  - `git diff --check`: 0 warnings/errors.
