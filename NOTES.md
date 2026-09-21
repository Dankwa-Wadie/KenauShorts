# KenauShorts — Project Notes

This file is for continuity across AI tools (Claude, ChatGPT, Antigravity) and
across sessions. Read this before starting work. Update it before switching
tools or ending a session — commit it along with your code changes.

**Rule: always `git pull` before starting, always commit + push before
switching tools or stopping.** This file is only trustworthy if it reflects
what's actually on `origin/main`.

---

## Current state (update this section each handoff)

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

