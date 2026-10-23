# KenauShorts — Project Notes

This file is for continuity across AI tools (Claude, ChatGPT, Antigravity) and
across sessions. Read this before starting work. Update it before switching
tools or ending a session — commit it along with your code changes.

**Rule: always `git pull` before starting, always commit + push before
switching tools or stopping.** This file is only trustworthy if it reflects
what's actually on `origin/main`.

---

## Current state (update this section each handoff)

- **Stage 8 Closed with Documented Caveat (Milestones 8.1–8.5 Complete)**:
  - **Status & Scope**: Stage 8 (Clean-Footage Rendering, Presentation Modes, Ingestion Propagation, Channel Defaults, and Re-render Integrity) is formally closed across Milestones 8.1–8.5. No Milestone 8.6 is approved or required by current evidence.
  - **Verified Targeted Tests**: 160 tests across 7 targeted test suites (`test_milestone8_*.py`, `test_stage5_library.py`, `test_stage2_queue_cancel.py`, `test_server_lifecycle.py` + `test_alerts.py`, `test_operational_smoke.py` + `test_supervisor.py` + `test_frontend_integration.py`, `test_agent_discovery.py` + `test_pipeline_integration.py`, and `test_health_ui.js`) confirmed passing with exit code 0 at HEAD (`c31b367`).
  - **Documented Test-Evidence Caveats**:
    - The historical 413-test full-discovery result belongs to Milestone 8.1 at commit `76e48cb`; it is not evidence of a clean full-suite run at current HEAD (which contains ~452 discoverable tests).
    - The queue-cancellation test (`tests/test_stage2_queue_cancel.py`) passes 11/11 in standalone runs, but its intermittent 3.0s timeout during monolithic Windows single-process test discovery (`unittest discover`) remains unconfirmed as resolved under cumulative process load.
    - A full monolithic test-discovery pass at the current HEAD has not been verified.

- **Stage 8 Milestone 8.5 Completed & Verified (Re-render Pipeline Integrity, Raw-Source Guards, and Library Presentation Filtering)**:
  - **Worker Presentation Settings Precedence (`studio/worker.py`)**:
    - Realigned background worker re-render configuration precedence:
      `record field` > `config["layout"][field]` > `config[field]` > `render_config["layout"][field]` > `render_config[field]` > Default (`"card"` / `"auto"`).
    - Applied consistently to both `presentation_mode` and `footage_fit`.
    - Eliminates the silent bug where presets with hardcoded `"presentation_mode": "card"` in layout overrode the channel-wide default (`config["layout"]["presentation_mode"] = "footage_clean"`) for legacy/unmigrated records.
    - Proved that explicit settings on the record continue to override channel defaults.
  - **Fail-Closed Raw Source Verification (`studio/worker.py`)**:
    - Replaced silent fallback to `record["video"]` with strict fail-closed validation: checks `work/{key}_raw.mp4`, edit lineage `work/{orig_key}_raw.mp4`, and `record.get("raw_video")` (relative or absolute).
    - If no valid raw source exists on disk, raises descriptive `ValueError("Cannot re-render video '{key}': original raw source video not found on disk. Re-rendering from an already-composited output is disabled to prevent video corruption.")`.
    - Strictly prevents feeding already-composited 9:16 card outputs into the renderer, eliminating corrupted center-cropped avatar/headline artifacts and nested card compounding.
    - Preserves worker exception handling: marks video `status="render_failed"` with error details and emits failure summary.
  - **Server Preflight Re-render Validation (`studio/server.py`)**:
    - Added preflight checks in `start_job()` for `action == "render"` before spawning subprocesses or inserting job records into SQLite:
      - Validates video ID is non-empty.
      - Confirms video record exists in SQLite.
      - Utilizes repository's established `store.enrich_video_record(record)["raw_exists"]` check to confirm original raw source file exists on disk.
      - Rejects invalid requests with HTTP 400 Bad Request (`ValueError`).
      - Strictly prevents creating orphaned jobs or falsely reporting successful scheduling.
  - **Library Presentation Mode Filtering & Modal UI Guard (`studio/web/app.js`)**:
    - Added Presentation Mode filter dropdown (`#library-filter-mode`) to Library filter bar:
      - `All Modes` (`all`)
      - `Clean Footage` (`footage_clean`)
      - `Card Mode` (`card`)
    - Added `libraryFilterMode` state variable, change listener, and integrated into `resetLibraryFilters()`.
    - Integrated seamlessly with existing status, review, source, style preset, and media health filters.
    - In `renderLibraryGrid()`, checks `matchesLibraryMode(v, libraryFilterMode)` where legacy records without `presentation_mode` evaluate to `"card"`.
    - In draft review modal (`openVideoModal`), if `v.raw_exists === false`, disables `#btn-rerender-draft` with `disabled style="opacity: 0.5; cursor: not-allowed;"` and tooltip `"Cannot re-render: Original raw source video is missing from disk"`, with defensive guard in `reRenderDraft()` to prevent issuing requests.
    - Exported `matchesLibraryMode`, `getLibraryFilterMode`, `setLibraryFilterMode`, and `resetLibraryFilters` for automated Node.js contract testing.
  - **Deterministic Test Suites & Verification Evidence**:
    - Focused Milestone 8.5 suite (`tests/test_milestone8_5.py`): 13/13 passed (1.95s).
    - Combined Stage 8 regression suite (`test_milestone8_1.py`, `test_milestone8_2.py`, `test_milestone8_3.py`, `test_milestone8_4.py`, `test_milestone8_5.py`): 47/47 passed (14.53s).
    - Stage 5 Library suite (`tests/test_stage5_library.py`): 15/15 passed (1.10s) with verified raw source fixture for queue test.
    - Process queue and cancellation suite (`tests/test_stage2_queue_cancel.py`): 11/11 passed (6.82s).
    - Server lifecycle and alert engine suites (`test_server_lifecycle.py`, `test_alerts.py`): 27/27 passed (4.81s).
    - Operational smoke, supervisor daemon, and frontend integration suites (`test_operational_smoke.py`, `test_supervisor.py`, `test_frontend_integration.py`): 26/26 passed (7.87s).
    - Existing discovery and pipeline integration suites (`test_agent_discovery.py`, `test_pipeline_integration.py`): 21/21 passed (0.35s).
    - Node.js UI health tests (`tests/test_health_ui.js`): 13/13 passed.
    - Bytecode compilation: `python -m compileall core studio scripts tests` clean (0 errors).
    - Formatting check: `git diff --check` clean (0 errors).
    - Protected invariants: `studio-settings.json` completely untouched (`"interval_hours": 3`), `studio/supervisor.py` untouched (0 diff against HEAD), `core/agent.py` untouched (0 diff against HEAD).
    - Live YouTube requests/uploads: 0 calls made.

- **Stage 8 Milestone 8.4 Completed & Verified (Channel-Wide Presentation Defaults and Settings Persistence)**:
  - **Settings Whitelist & Validation Governance (`studio/server.py`)**:
    - Added `presentation_mode` and `footage_fit` recognition and validation to `validate_settings_config()` under `layout`.
    - Enforces fail-closed validation: accepts only `SUPPORTED_PRESENTATION_MODES` (`"card"`, `"footage_clean"`) and `VALID_FOOTAGE_FIT` (`"auto"`, `"cover"`, `"contain"`).
    - Strictly rejects reserved `"footage_caption"`, invalid choices, and non-string types with HTTP 400 Bad Request (`ValueError`).
    - Eliminates the silent configuration stripping bug where saving unrelated settings or channel branding would wipe `presentation_mode` and `footage_fit` from `config.json`.
    - Preserves existing layout fields (`border_color`, `corner_radius`, `canvas_width`, etc.) during partial settings updates.
  - **Channel & Style UI (`studio/web/app.js`)**:
    - Extended Channel & Style page (`loadStyle`) with Default Presentation Mode (`#style-presentation-mode`) and Default Footage Fit (`#style-footage-fit` in `#style-footage-fit-group`).
    - Loaded current values from `layout.presentation_mode` and `layout.footage_fit`, defaulting to Card and Auto only when genuinely absent.
    - Added `onStylePresentationModeChange()` to toggle `#style-footage-fit-group` visibility without mutating the selected fit value.
    - Updated `saveStyleSettings()` to package `presentation_mode` and `footage_fit` inside `cfg.layout` for persistence via `POST /api/settings`.
    - Exported `onStylePresentationModeChange` for Node.js contract test compatibility.
  - **Schema Documentation (`config.example.json`)**:
    - Documented `"presentation_mode": "card"` and `"footage_fit": "auto"` under `"layout"`.
  - **Deterministic Test Suites & Verification Evidence**:
    - Focused Milestone 8.4 suite (`tests/test_milestone8_4.py`): 7/7 passed (0.46s).
    - Combined regression suite (`test_milestone8_1.py`, `test_milestone8_2.py`, `test_milestone8_3.py`, `test_milestone8_4.py`, `test_operational_smoke.py`, `test_supervisor.py`, `test_frontend_integration.py`): 60/60 passed (26.4s).
    - Existing discovery and pipeline integration suites (`test_agent_discovery.py`, `test_pipeline_integration.py`): 21/21 passed (0.58s).
    - Node.js UI health tests: 13/13 passed (`node tests/test_health_ui.js`).
    - Bytecode compilation: `python -m compileall core studio scripts tests` clean (0 errors).
    - Formatting check: `git diff --check` clean (0 errors).
    - Protected invariants: `studio-settings.json` completely untouched (`"interval_hours": 3`), `studio/supervisor.py` untouched (0 diff).
    - Live YouTube requests/uploads: 0 calls made.

- **Stage 8 Milestone 8.3 Completed & Verified (Discovery, Staging & Ingestion Pipeline Alignment for Clean Footage & Footage Fit)**:
  - **Automated & Manual Ingestion Alignment (`core/agent.py`)**:
    - Added `footage_fit: str = ""` to `Pick` dataclass with backward-compatible default.
    - Updated `_render_pick()` with unambiguous configuration precedence rule:
      `pick.field` > `config["layout"][field]` > `config[field]` > `render_config["layout"][field]` > Default (`"card"` / `"auto"`).
      Guarantees that an explicit operator or config selection is never overridden by preset defaults.
    - Direct renderer argument passing with defensive signature inspection for mock backward-compatibility.
    - Persists resolved `presentation_mode` and `footage_fit` in `store.draft()` across both `run_pipeline()` and `run_manual()`.
    - Added CLI flags `--presentation-mode` (choices `["", "card", "footage_clean"]`) and `--footage-fit` (choices `["", "auto", "cover", "contain"]`) to `core.agent` with fail-closed validation.
  - **Server Validation & Job Construction (`studio/server.py`)**:
    - `POST /api/job` with action `"manual"` extracts and validates `presentation_mode` and `footage_fit`.
    - Rejects reserved `"footage_caption"`, invalid modes, invalid fits, and non-string types with HTTP 400 Bad Request before launching worker subprocesses.
    - Assembles `--presentation-mode` and `--footage-fit` CLI arguments for `core.agent`.
    - Preserves options across job retries (`retry_job`).
  - **Studio Ingestion Modal UI (`studio/web/index.html`, `studio/web/app.js`)**:
    - Added Presentation Mode selector (`#manual-presentation-mode`) and Footage Fit selector (`#manual-footage-fit` in `#manual-footage-fit-group`) to `<dialog id="manual-url-modal">`.
    - `openManualUrlModal()` resets selectors to documented defaults (`card`, `auto`).
    - `onManualPresentationModeChange()` toggles footage fit selector visibility when clean footage is selected.
    - `submitManualUrl()` includes validated `presentation_mode` and `footage_fit` in `POST /api/job` payload.
  - **Deterministic Test Suites & Verification Evidence**:
    - Focused Milestone 8.3 suite (`tests/test_milestone8_3.py`): 8/8 passed (0.5s).
    - Combined regression suite (`test_milestone8_1.py`, `test_milestone8_2.py`, `test_milestone8_3.py`, `test_operational_smoke.py`, `test_supervisor.py`, `test_frontend_integration.py`): 53/53 passed (22.3s).
    - Existing discovery and pipeline integration suites (`test_agent_discovery.py`, `test_pipeline_integration.py`): 21/21 passed (0.5s).
    - Node.js UI health tests: 13/13 passed (`node tests/test_health_ui.js`).
    - Bytecode compilation: `python -m compileall core studio scripts tests` clean (0 errors).
    - Formatting check: `git diff --check` clean (0 errors).
    - Protected invariants: `studio-settings.json` completely untouched (`"interval_hours": 3`), `studio/supervisor.py` untouched (0 diff).
    - Live YouTube requests/uploads: 0 calls made.

- **Stage 8 Milestone 8.2 Completed & Verified (Studio UI & Workflow Modernization for Clean Footage & Footage Fit)**:
  - **Presentation Mode & Footage Fit Controls (`studio/web/app.js`, `studio/web/style.css`)**:
    - Video modal (`openVideoModal`):
      - Added Presentation Mode selector (`#edit-presentation-mode`) allowing operators to toggle between Card (`card`) and Clean Footage (`footage_clean`).
      - Added Footage Fit selector (`#edit-footage-fit` in `#footage-fit-group`), exposed dynamically when Clean Footage is active, supporting Auto (`auto`), Cover (`cover`), and Contain (`contain`).
      - Dynamic Re-render Action button (`#btn-rerender-draft`): dynamically labels action as `Re-render Clean Footage` versus `Re-render Card` based on chosen presentation mode.
    - Representative 9:16 Preview Guidance (`#preview-mode-banner`):
      - Displays contextual explanations directly beneath the video player explaining scaling and cropping behavior across modes and fit options:
        - Clean Footage (Cover): explains that video fills the entire 9:16 frame with center-cropping on left and right for landscape content.
        - Clean Footage (Contain): explains that full video fits inside the 9:16 frame with black letterbox bars and zero cropping.
        - Clean Footage (Auto): explains adaptive behavior (vertical content covers frame; landscape content contains with black padding).
        - Card: explains classic branded 9:16 layout with bold headline, style borders, and avatar branding.
    - Library Grid Presentation Mode Badges:
      - Displayed on video cards in library grid (`.badge-mode-clean` and `.badge-mode-card`) for instant visual distinction between clean footage shorts and card shorts.
  - **API Validation & State Synchronization (`studio/server.py`)**:
    - `POST /api/video`: validates `presentation_mode` (`"card"`, `"footage_clean"`, rejecting reserved `"footage_caption"` and unsupported modes with 400 Bad Request) and `footage_fit` (`"auto"`, `"cover"`, `"contain"`, rejecting invalid/non-string values with 400 Bad Request).
    - Synchronizes top-level record fields (`record["presentation_mode"]`, `record["footage_fit"]`) and nested layout configuration (`record["config"]["layout"]`).
  - **Persistence & Worker Re-rendering (`studio/store.py`, `studio/worker.py`)**:
    - `studio/store.py`: `store.draft()` accepts and records `footage_fit`; `enrich_video_record()` supplies backward-compatible default `"auto"` for unmigrated records.
    - `studio/worker.py`: worker `"render"` action resolves both `presentation_mode` and `footage_fit` from SQLite record or configuration, passes both to `render.render()`, and persists both upon render completion.
    - `core/agent.py`: Reverted to exact HEAD (Milestone 8.1 committed state) to keep Milestone 8.2 strictly isolated to Studio UI and worker re-rendering workflows without touching automated discovery pipeline logic.
  - **Deterministic Test Suites & Verification Evidence**:
    - Focused Milestone 8.2 suite (`tests/test_milestone8_2.py`): 7/7 passed (2.1s) after Option A test scope alignment (removed pick dataclass propagation test to keep `core/agent.py` untouched at HEAD).
    - Combined regression suites (`test_milestone8_1.py`, `test_milestone8_2.py`, `test_operational_smoke.py`, `test_supervisor.py`, `test_frontend_integration.py`): 45/45 passed (21.1s).
    - Node.js UI health tests: 13/13 passed (`node tests/test_health_ui.js`).
    - Python bytecode compilation: `python -m compileall core studio scripts tests` clean (0 errors).
    - Formatting check: `git diff --check` clean (0 errors).
    - Invariant verification: `core/agent.py`, `studio/supervisor.py`, and `studio-settings.json` have 0 diff against HEAD; `studio-settings.json` verified with `"interval_hours": 3`.
    - Live YouTube requests/uploads: 0 calls made.


- **Stage 8 Milestone 8.1 Completed & Verified (Footage-First and Clean-Footage Rendering Modes in `76e48cb53264465e8cde9399c1ed47c5a203c8a2`)**:
  - **Core Rendering Foundation (`core/render.py`)**:
    - Defined presentation mode constants: `PRESENTATION_MODE_CARD = "card"`, `PRESENTATION_MODE_FOOTAGE_CLEAN = "footage_clean"`, `PRESENTATION_MODE_FOOTAGE_CAPTION = "footage_caption"`, `VALID_PRESENTATION_MODES = ("card", "footage_clean", "footage_caption")`, `DEFAULT_PRESENTATION_MODE = "card"`.
    - Added `presentation_mode` and `footage_fit` fields to `DEFAULT_LAYOUT`.
    - Clean footage rendering filterchain: generates full-bleed 9:16 vertical Shorts video (1080x1920) without burned-in text overlays, channel badges, avatars, headlines, or branding borders. Skips `build_overlay` when in `footage_clean` mode.
    - Source-aware scaling and cropping (`footage_fit`):
      - `"cover"`: Scales and crops source center to fill 1080x1920 without distortion.
      - `"contain"`: Scales source to fit within 1080x1920 with letterbox/pillarbox padding and black background.
      - `"auto"`: Evaluates source aspect ratio via `ffprobe`; applies `cover` for vertical/portrait sources ($\le 1.0$) and `contain` for landscape/widescreen sources ($> 1.0$) to avoid aggressive cropping of horizontal content.
    - Audio and music preservation: Preserves original video audio stream and mixes optional background music with volume normalization and ducking filters.
    - Updated `RenderResult`: includes `overlay: Path | None` (`None` for clean footage) and `presentation_mode: str`.
    - Reserved `footage_caption` mode: raises `NotImplementedError` if invoked, establishing architecture hook for future captioning milestone.
  - **Style Presets Propagation (`core/style_presets.py`)**:
    - Added `"presentation_mode": "card"` to layout configurations across all 5 built-in presets (`classic_blue`, `neon_cyan`, `warm_amber`, `minimal_mono`, `retro_purple`).
    - Updated `apply_style_preset()` to propagate top-level `presentation_mode` configuration into merged layouts.
  - **Automated Discovery Pipeline Integration (`core/agent.py`)**:
    - Added `presentation_mode: str = ""` to `Pick` dataclass.
    - Automated rendering in `_render_pick()`: resolves presentation mode from pick, layout, or config; emits stage-specific progress events (`"Rendering clean footage"` vs `"Rendering video card"`); passes mode to `render.render()`; and records mode in `store.draft()`.
    - Safe render invocation: inspects `render` signature to ensure backward compatibility with test harnesses and legacy mocks.
  - **Database Persistence & Studio Worker Integration (`studio/store.py`, `studio/worker.py`, `studio/server.py`)**:
    - `studio/store.py`: `draft()` accepts `presentation_mode`; `enrich_video_record()` supplies backward-compatible default `"card"` for unmigrated records.
    - `studio/worker.py`: manual worker action `"render"` resolves `presentation_mode` from video record or job config and invokes `render.render()`.
    - `studio/server.py`: `/api/video` mutation endpoint validates and persists `presentation_mode`, rejecting invalid values with 400 Bad Request.
  - **Unsupported & Reserved Mode Rejection (`core/render.py`, `studio/server.py`)**:
    - Partitioned mode definitions into `SUPPORTED_PRESENTATION_MODES = {"card", "footage_clean"}` and `RESERVED_PRESENTATION_MODES = {"footage_caption"}`.
    - `core/render.render()` immediately raises `NotImplementedError` if `footage_caption` or any reserved mode is requested.
    - `/api/video` mutation endpoint strictly rejects `footage_caption` and any non-supported modes with HTTP 400 Bad Request (`ValueError`), preventing unsupported modes from being saved or scheduled for rendering.
  - **Hermetic Test Isolation (`tests/test_supervisor.py`, `tests/test_frontend_integration.py`, `tests/test_operational_smoke.py`)**:
    - Preserved production `studio/supervisor.py` completely untouched at its exact base-commit state.
    - Isolated settings purely within test harnesses (`setUp()` / `_setup_safety_barriers()`), preventing wall-clock expiration of `studio-settings.json` on disk from causing false-positive scheduler stall alerts.
  - **Deterministic Test Suites & Verification Evidence**:
    - Focused Milestone 8.1 suite (`tests/test_milestone8_1.py`): 12/12 passed (11.4s).
    - Full repository discovery suite (`python -X utf8 -m unittest discover tests`): 413/413 passed (74.2s, 1 skipped on Windows symlink privileges, 0 failures, 0 errors).
    - Node.js UI health tests: 13/13 passed (`node tests/test_health_ui.js`).
    - Smoke, supervisor, and frontend regression suites: 38/38 passed (18.6s).
    - Codebase compilation: `python -m compileall core studio scripts tests` clean (0 errors).
    - Formatting check: `git diff --check` clean (0 warnings).
    - Protected configuration: `studio-settings.json` completely untouched (`"interval_hours": 3`).
    - Implemented in commit `76e48cb53264465e8cde9399c1ed47c5a203c8a2`.


- **Operational Smoke Test Coverage & Probe Concurrency Race Fix Completed & Verified (`a8682c48c7014831b20249ecc033f8d8c17572c1`)**:
  - **Sequential-Probe Concurrency Fix (`studio/supervisor.py`)**:
    - Identified a race in `BoundedProbeRunner.run_probe()`: `done_event.set()` fired in the inner `finally` block before the worker thread executed outer `finally: self._concurrency_lock.release()`.
    - Added `th.join(timeout=0.5)` when `done_event.wait(timeout=to)` is True. Guarantees the completed worker thread fully terminates and releases the concurrency lock before `run_probe()` returns to callers executing sequential probes (`Supervisor.tick()` calling `probe_database()` then `probe_disk_space()`).
    - Strictly preserves single-worker bounds and timeout behavior: timed-out probes (`done_event.wait()` times out) bypass `join` and continue holding `_concurrency_lock` in the background, rejecting new threads with `"probe_worker_busy_previous_timed_out"`.
  - **Sequential Probe Regression Test (`tests/test_supervisor.py`)**:
    - Added `test_bounded_probe_runner_immediate_sequential_handoff`: 10 immediate sequential probes back-to-back on a single `BoundedProbeRunner` with zero sleeps, proving zero busy rejections.
  - **Controlled Non-Publishing Operational Smoke Test (`tests/test_operational_smoke.py`)**:
    - Pre-execution action inventory and isolation verification against all publish and network entry points.
    - Outbound API & upload blackhole protection: `unittest.mock.patch` poison-pills on `core.agent.upload_to_youtube`, `core.agent.discover_youtube`, `studio.server.test_youtube_connection`, and `studio.server.reconcile_video_upload` raising `RuntimeError`.
    - Network socket isolation: `studio.server.is_online` patched to return `True` without external network packets; loopback-only HTTP client.
    - Database & settings isolation: dedicated ephemeral SQLite DB (`smoke_studio.sqlite3`) and isolated settings file (`isolated-settings.json`).
    - Live loopback HTTP server binding on ephemeral port (`StudioServer` + `StudioHandler`).
    - Validates static asset delivery (`/`, `/style.css`, `/app.js`), operational endpoints (`/api/status`, `/api/health`, `/api/alerts`, `/api/queue`, `/api/retention/audit`), supervisor tick cycle, and graceful shutdown (`server.stop_studio_application()`).
    - Genuine `unittest` lifecycle cleanup verification: `test_operational_smoke_teardown_on_induced_failure` executes `NestedFailingSmokeTest` via `test_case.run(test_result)`, proving that `unittest`'s `doCleanups()` joins server and supervisor threads and closes listening socket even upon assertion failure.
  - **Deterministic Test Suites & Verification Evidence**:
    - Full repository discovery suite (`python -X utf8 -m unittest discover tests`): 401/401 passed (53.3s, 1 skipped on Windows symlinks, 0 failures, 0 errors).
    - Node.js UI health tests: 13/13 passed (`node tests/test_health_ui.js`).
    - Regression suites (`tests.test_supervisor`, `tests.test_alerts`, `tests.test_server_lifecycle`, `tests.test_frontend_integration`): 51/51 passed (10.8s).
    - Codebase compilation: `python -m compileall core studio scripts tests` clean.
    - Formatting check: `git diff --check` clean.
    - Protected configuration: `studio-settings.json` completely untouched (`"interval_hours": 3`).
    - Synchronized with `origin/main` at `a8682c48c7014831b20249ecc033f8d8c17572c1`.

- **Stage 7 Phase 4 Milestone 6 Completed & Verified (Frontend Integration and End-to-End Verification)**:
  - **Health Telemetry UI Integration (`studio/web/app.js`, `studio/web/index.html`, `studio/web/style.css`)**:
    - Dual polling coordination: Replaced standalone status polling with concurrent `Promise.all([fetch('/api/status'), fetchHealthTelemetry()])` bounded by a 2.5s `AbortController` signal, preventing duplicate intervals or unbounded timer pileups.
    - Six authoritative UI health states with distinct styling and fail-closed transitions:
      1. `healthy` (`.status-indicator.online`, badge `Healthy`, green): All subsystems operational, probes responsive, no active diagnostic alerts.
      2. `degraded` (`.status-indicator.degraded`, badge `Degraded`, amber): Non-critical warnings (e.g. low disk space <1GB, scheduler deferred or pending resolution).
      3. `unhealthy` (`.status-indicator.unhealthy`, badge `Critical / Unhealthy`, red): Critical subsystem failure (e.g. critical disk space <512MB, database probe unresponsive, queue worker dead with pending jobs, active job progress stalled >300s, alert engine drain worker failure).
      4. `unavailable` (`.status-indicator.unavailable`, badge `Unavailable`, muted red/gray): Host daemon unreachable, network drop, HTTP 5xx, or request timeout (>2.5s). Never falsely displays a healthy status.
      5. `stale` (`.status-indicator.stale`, badge `Stale`, amber/orange): Telemetry data older than 45 seconds (`dataAgeSec > 45`), cleanly alerting operators to frozen background threads or client clock skew.
      6. `malformed` (`.health-pill.malformed`, red): Rejects invalid JSON or unexpected schemas.
    - Non-invasive Diagnostic Modal (`<dialog id="health-modal">`):
      - Interactive detailed breakdown of all 6 subsystems: SQLite Database (with probe latency ms), Disk Storage (free GB/MB), Automation Scheduler (state & deferral reasons), Queue Worker (thread status & pending count), Active Job (stage, duration, stall status), and Alert Engine (drain worker status & queue depth).
      - Live listing of all active supervisor stall alerts by fingerprint.
      - "↻ Refresh Telemetry" button (`id="health-modal-refresh"`) for on-demand telemetry retrieval without waiting for background tick.
      - Clickable sidebar host info `#host-info` opens modal with keyboard (ESC) and backdrop click dismissal.
    - Overview Tab Telemetry Widget (`#overview-health-widget`):
      - Replaced static storage panel with dynamic live subsystem cards grid, telemetry freshness display, and "↻ Refresh Telemetry" button (`id="overview-health-refresh"`).
    - Security & Redaction:
      - Strictly sanitized rendering using `escapeHtml()` across all subsystem parameters (stage, reason, error) preventing HTML injection / XSS.
      - Telemetry endpoint `/api/health` and UI render no API keys, OAuth tokens, secrets, or sensitive filesystem paths.
  - **Backend & Supervisor Enhancements (`studio/supervisor.py`, `studio/server.py`)**:
    - Synchronized `tick()` and `get_latest_telemetry()` with `self._active_alerts` to ensure `active_supervisor_alerts` is immediately fresh on every tick.
    - Enhanced `get_health_telemetry()` to inspect `server.SUPERVISOR_INSTANCE` if `_GLOBAL_SUPERVISOR` is not set, preventing redundant ad-hoc probes.
    - `ensure_supervisor()` registers `SUPERVISOR_INSTANCE` into `supervisor._GLOBAL_SUPERVISOR`.
    - Implemented `sanitize_probe_error(err)` to sanitize raw exception strings, filesystem paths, and credentials into generic safe error codes across probe collectors and active alert rows.
    - Protected `DOMContentLoaded` and `beforeunload` in `studio/web/app.js` with `typeof document !== 'undefined'` and exported functions for automated Node.js testing.
  - **Deterministic Test Suites & Comprehensive Coverage**:
    - `tests/test_health_ui.js` (13 tests, Node.js): Unit test suite covering `evaluateHealthState` (all 6 states: null/undefined, explicit unavailable, malformed, stale >45s, healthy, degraded, unhealthy), `getHealthSummaryMessage` (subsystem breakdowns), `renderHealthSubsystemCards` with XSS sanitization, critical+stale precedence, fresh recovery, and `pollStatusInFlight` guard lifecycle / independent status processing.
    - `tests/test_frontend_integration.py` (10 tests, Python unittest): End-to-end integration suite verifying static asset delivery (`/index.html`, `/style.css`, `/app.js`), modal markup and CSS rules, `/api/health` JSON schema conformance, security credential redaction check, sensitive path/token exception redaction check, degraded storage reflection, unhealthy critical storage reflection and alert listing, and fail-closed DNS rebinding rejection (HTTP 403 on invalid Host).
    - Combined Milestone Regression (`tests.test_alerts`, `tests.test_server_lifecycle`, `tests.test_supervisor`, `tests.test_frontend_integration`): 50/50 passed (10.3s).
    - Full repository discovery suite (`python -X utf8 -m unittest discover tests`): 398/398 passed (63.2s, 1 skipped on Windows symlink privileges, 0 failures, 0 errors).
    - Codebase compilation: `python -m compileall core studio scripts tests` 0 errors.
    - Formatting check: `git diff --check` 0 warnings/errors.
    - Protected configuration: `studio-settings.json` completely untouched (`"interval_hours": 3`).
  - **Stage 7 Phase 4 Completed**: All 6 milestones (Milestones 1-6) fully complete and verified.

- **Stage 7 Phase 4 Milestone 5 Completed & Verified (Independent Health Telemetry and Diagnostic Stall Alerts in `studio/supervisor.py`)**:
  - **Bounded Probe Runner & Resource Leak Prevention (`studio/supervisor.py`)**:
    - `BoundedProbeRunner`: Thread-safe bounded execution ($\le 2.0\text{s}$) with single-probe concurrency lock refusing new thread spawning while a probe is active; the concurrency lock remains held until the probe worker completes, not merely until caller timeout expires, strictly guaranteeing zero thread accumulation under persistent probe timeouts, hung I/O, or SQLite lock contention.
    - `probe_database(db_path, runner)`: Lightweight `SELECT 1` query probe with `sqlite3.connect(..., timeout=1.5)` bounded by 2.0s runner timeout. Handles connection errors, query timeouts, and schema accessibility, with connection cleanup strictly in `finally: conn.close()`.
    - `probe_disk_space(root_dir, runner)`: Filesystem free space probe supporting namedtuples, tuples, and dicts. Classifies disk health into `critical` (< 512 MB), `low` (< 1024 MB), and `healthy`.
  - **Authoritative Stall Semantics & Background Supervisor Daemon**:
    - `Supervisor`: Background daemon thread executing on an approved 15-second tick interval.
    - Non-blocking `GUARD.acquire(timeout=0.5)` for in-memory active job inspection, falling back safely to SQLite state if locked.
    - Active job progress stall detection: Inspects running jobs (`status == "running"`) and detects stalls when elapsed time since the latest progress stage or start time exceeds the 300-second stall threshold (`time.time() - last_progress > 300.0`). All authoritative terminal states (`completed`, `failed`, `cancelled`, `interrupted`, `idle`) resolve active job-stall alerts.
    - Scheduler stall detection: Detects dead or stalled scheduler loop when next scheduled run is overdue beyond acceptable grace period, while strictly excluding all legitimate scheduler deferral states (`waiting_for_quota`, `waiting_for_candidate`, `waiting_for_worker`, `waiting_for_network`, `disabled`) and deferral reasons (`google_quota_exhausted`, `upload_quota_limit`, `search_quota_limit`, `no_ready_videos`, `publish_waiting_for_candidate`, `worker_busy`, `offline`, `disabled`).
    - Dead queue worker detection: Detects queue thread failure while queued jobs remain pending.
  - **Idempotent Alert Creation & Auto-Recovery**:
    - Enqueues diagnostic alerts through the Milestone 4 alert engine (`studio/alerts.py`) utilizing partial unique index `idx_alerts_active` via fingerprinting (`db_probe_failed`, `disk_space_low`, `disk_space_critical`, `scheduler_stalled`, `queue_worker_dead`, `job_progress_stalled:{job_id}`).
    - Deterministic condition auto-recovery: Automatically detects when a condition recovers (e.g., job progress resumes, disk space frees, database connectivity restores, job reaches terminal state) and resolves active alerts via `alerts.resolve_condition()`, advancing recovery markers.
  - **Studio Server Lifecycle Coordination (`studio/server.py`)**:
    - Added `SUPERVISOR_THREAD` and `SUPERVISOR_INSTANCE` globals, `ensure_supervisor()`.
    - Added `GET /api/health` HTTP endpoint delivering live telemetry (`status`, `db_healthy`, `disk_healthy`, `scheduler_healthy`, `queue_healthy`, `active_job`, `stalled_jobs`, `alerts`).
    - Registered `SUPERVISOR_THREAD` in server shutdown lifecycle (`workers` in `stop_studio_application()`), ensuring supervisor is joined before producer quiescence checks and alert intake closure.
  - **Alert Engine Hardening (`studio/alerts.py`)**:
    - Added `wait_for_drain(timeout: float = 2.0) -> bool` to await `_ALERT_QUEUE` and `_CURRENT_EVENT_BEING_PROCESSED`.
    - Hardened `reset_engine_for_testing()` to signal `_SHUTDOWN_EVENT.set()` and join `_DRAIN_THREAD` before clearing queue and state, eliminating thread leakage between unit tests.
  - **Deterministic Test Suites & Comprehensive Coverage**:
    - `tests/test_supervisor.py` (13 tests): Database probe success (`SELECT 1`), database probe failure/timeout, disk space health classification, zero thread accumulation and concurrency lock retention under probe timeout, scheduler stall detection with all legitimate quota/state exclusions, dead queue worker detection, active job progress stall detection (>300s since last stage), active job progress resumed -> auto-resolution of stall alert, all terminal job states (`completed`, `failed`, `cancelled`, `interrupted`, `idle`) resolve stall alerts, alert deduplication on repeated supervisor ticks, orderly shutdown responsiveness within bounded timeout, health telemetry payload schema and consistency, and HTTP `GET /api/health` integration via `StudioHandler`.
    - Test Suite Pass Evidence:
      - `tests/test_supervisor.py`: 13/13 passed (4.1s, 0 ResourceWarnings).
      - Lifecycle + Alerts + Supervisor combined suite: 40/40 passed (8.7s, 0 ResourceWarnings).
      - Full repository test discovery suite: 388/388 passed (61.4s, 1 skipped on Windows symlink privileges, 0 failures, 0 errors).
  - **Remaining Stage 7 Phase 4 Milestones**:
    - Milestone 6: Frontend integration and end-to-end verification.

- **Stage 7 Phase 4 Milestone 4 Completed & Verified (Operational Alert Lifecycle, Transition Matrix, Drain Worker & Server Shutdown Coordination)**:
  - **Operational Alert Engine & Persistence (`studio/alerts.py`)**:
    - Complete alert lifecycle with severity ranking (`critical`, `warning`, `info`), incident deduplication via unique partial index `idx_alerts_active` (`WHERE resolved_at IS NULL`), and transitions (`active` -> `acknowledged` -> `resolved`).
    - Explicit transaction and migration atomicity (`init_alert_schema`): uses transaction savepoints (`SAVEPOINT init_alert_schema`) to protect schema creation, defensive legacy column migrations (all 21 columns and default values), duplicate active incident resolution, and partial unique index creation.
    - Idempotent table and index creation, safe on existing SQLite connections whether or not an outer transaction is active.
    - Deterministic recovery marker tracking (`alert_recovery_markers` table) tracking high-water marks and recovery states for active alerts.
  - **Fail-Stop Drain Worker & In-Memory Enqueue Queue**:
    - Dedicated drain thread (`_DRAIN_THREAD`) with unbounded thread-safe FIFO queue (`_ENQUEUE_QUEUE`) consuming enqueue requests and persisting alerts/events to SQLite via dedicated database connection (`_DEDICATED_CONN`).
    - Dedicated database connection lifecycle: `_DEDICATED_CONN` is never closed while `_DRAIN_THREAD` is alive, even if shutdown times out, preventing `ProgrammingError: Cannot operate on a closed database`.
    - Fail-stop crash containment: In the event of a drain loop exception, the worker captures the diagnostic crash state, sets `_DRAIN_CRASH_EVENT`, and stops cleanly without corrupting transaction state.
  - **Operator-Transition Shutdown Barrier & Admission Protocol**:
    - Race-safe transition admission: `execute_operator_transition()` checks `_ENGINE_STATE == "running"` under `_ENQUEUE_LOCK` and tracks in-flight operations with `_ACTIVE_OPERATOR_TRANSITIONS`.
    - `get_active_operator_transitions()` inspects in-flight transitions under `_ENQUEUE_LOCK`.
    - New operator transitions are rejected with `RuntimeError("Alert intake is closed")` once shutdown begins (`close_alert_intake()`).
    - Shutdown coordination (`stop_studio_application` in `studio/server.py` and `stop_alert_engine` in `studio/alerts.py`) checks `active_operator_transitions == 0` before declaring `producers_quiescent`.
    - `accounting_complete` and `clean` shutdown fail closed (`False`) whenever any operator transition or handler remains active or unrecorded.
  - **Studio Server Lifecycle Coordination (`studio/server.py`)**:
    - Unified orderly shutdown sequence in `stop_studio_application()`:
      1. Closes server socket and stops HTTP request intake (`StudioServer.stop_serving()`).
      2. Waits for in-flight HTTP request handlers (`live_handlers`) to finish.
      3. Signals automation and supervisor background loops to stop and joins worker threads (`live_workers`).
      4. Closes alert intake via `close_alert_intake()` under `_ENQUEUE_LOCK`.
      5. Evaluates producer quiescence: `producers_quiescent = (len(live_handlers) == 0 and in_flight_count == 0 and len(live_workers) == 0 and active_transitions == 0)`.
      6. Stops alert drain engine (`stop_alert_engine()`), draining remaining events and verifying accounting against high-water marks.
      7. Returns comprehensive diagnostic payload including `clean`, `producers_quiescent`, `accounting_complete`, `live_handlers`, `live_workers`, and `active_operator_transitions`.
  - **Deterministic Test Suites & Comprehensive Coverage**:
    - `tests/test_alerts.py` (14 tests): Full schema migration, all 21 column definitions and defaults, partial unique index `idx_alerts_active`, duplicate resolution, recovery marker schema, transition matrix (`active` -> `acknowledged` -> `resolved`), stale acknowledgement rejections, recovery accounting, and crash-containment isolation.
    - `tests/test_server_lifecycle.py` (13 tests): Orderly application shutdown, socket closure and connection refusal, active request handler drains, stuck-handler timeout diagnostics, producer quiescence barriers, deterministic operator-transition barriers (`test_active_operator_transition_blocks_quiescence_and_accounting`), and drain accounting completeness.
    - Stage 7 Regression: 175 tests passed (1 skip on Windows symlink privilege).
    - Full repository test discovery suite: 375 tests passed (1 skip on Windows symlink privilege, 0 failures, 0 errors in 60.9s).
  - **Remaining Stage 7 Phase 4 Milestones**:
    - Milestone 5: Independent health telemetry and diagnostic stall alerts (`studio/supervisor.py`).
    - Milestone 6: Frontend integration and end-to-end verification.

- **Stage 7 Phase 4 Milestone 3 Completed & Verified (Quota-Aware Scheduling, Bounded Jitter & Pacific-Time Rollover Resumption)**:
  - **Decoupled Job Dispatch from Execution Completion (`studio/server.py`)**:
    - Decoupled `next_run` advancement and failure count resets from initial job dispatch. Successful dispatch via `start_job()` merely transitions the scheduler to track the running job.
    - Added `handle_automatic_job_completion(job, finished_at)` hooked into worker process termination in `run_job_process()`.
    - Genuine pipeline/upload success resets `consecutive_failures = 0` and advances `next_run = finished_at + interval_seconds + calculate_jitter(interval_seconds)`.
    - Normal discovery runs yielding 0 candidates (genuine idle) reset `consecutive_failures = 0` and advance `next_run` normally with bounded jitter.
  - **Bounded Exponential Backoff on Pipeline Failures (`studio/settings.py`)**:
    - `calculate_backoff_delay(consecutive_failures)`: Implements bounded exponential backoff `delay = min(3600.0, 60.0 * (2 ** (consecutive_failures - 1)))`.
    - Jitter is bounded to `±10%` of delay (minimum `±1.0s`, clamped to `[60.0, 3600.0]`).
    - Terminal job failures, crashes, and pipeline errors increment `consecutive_failures` and schedule the next attempt with backoff delay instead of normal recurrence.
  - **Option B for Publish-Mode Waiting**:
    - In `publish` mode, when no ready candidate is found, `next_run` is preserved in place without forward schedule drift or synthetic retry delays.
    - Scheduler state transitions to `waiting_for_candidate` with `deferral_reason: "publish_waiting_for_candidate"`.
    - Reactive wakeup via `AUTOMATION_EVENT.set()` wakes the scheduler immediately when a newly rendered video transitions to `ready`, avoiding polling latency.
  - **Quota Governance & Rollover Resumption**:
    - Worker-authoritative quota reservation: The worker subprocess remains authoritative for YouTube upload quota checking and reservations (`videos_insert`).
    - Search vs. upload quota decoupling: Search quota exhaustion (`search_list` limit reached or Google circuit breaker tripped) does not block eligible ready videos from being uploaded.
    - Non-YouTube discovery preservation: When YouTube search quota is exhausted, discovery continues if non-YouTube sources (RSS feeds or Reddit) are configured.
    - Timezone-aware Pacific-Time rollover: `studio/store.py:get_pacific_next_rollover(now_utc)` computes the exact epoch and ISO string of the next 00:00:00 Pacific Time midnight, accounting for dynamic DST transitions (PST/PDT) and year boundaries.
    - Rollover jitter: `calculate_rollover_jitter()` introduces `0` to `+900s` (+0 to +15 minutes) jitter to prevent thundering-herd API requests at midnight rollover.
    - Scheduler preflight checks detect quota exhaustion and set diagnostics (`state: "quota_suspended"`, `deferral_reason: "quota_exhausted_search"` or `"quota_exhausted_upload"`).
  - **Diagnostics & Status Integration (`studio/server.py`)**:
    - `SCHEDULER_DIAGNOSTICS` exposed via `/api/status` with `scheduler_state`, `deferral_reason`, and `effective_due_time`.
    - `calculate_effective_due_time(settings)` determines the actual due timestamp considering quota suspension rollover and backoff.
  - **Harness & Phase 2 Test Synchronization (`tests/test_stage7_phase2.py`)**:
    - Resolved reproducible scheduler hang in `test_automation_loop_advances_interval_when_candidate_queued`. Replaced obsolete `time.sleep` monkeypatching with deterministic `AUTOMATION_EVENT.wait` interception and clean `try...finally: STOP_EVENT.clear()` cleanup.
    - Aligned Phase 2 assertions with approved Milestone 3 contract (decoupled dispatch, completion-time interval advancement, and Option B waiting without schedule drift).
  - **Scheduler Event Synchronization & Deterministic Wakeups (`studio/server.py`)**:
    - Eliminated lost-wakeup window by relocating `AUTOMATION_EVENT.clear()` inside the `with GUARD:` critical section in `automation_loop()`.
    - Proved locking invariants: all state mutations and event signals (`run_job_process()`, `handle_automatic_job_completion()`, `resolve_incident()`, and `/api/settings`) execute under `with GUARD:`.
    - Added immediate event signaling (`AUTOMATION_EVENT.set()`) when worker completes (`ACTIVE_JOB = None`), when videos resolve to `ready`, and when automation configuration updates via `/api/settings`.
  - **Defensive Completion-Handling Hardening (`studio/server.py`)**:
    - Added `completion_handled` lifecycle guard in `run_job_process()`: ensures that an unexpected exception inside `handle_automatic_job_completion()` on the normal path cannot escape into the outer exception handler, preventing successful jobs from being corrupted into failed jobs or executing completion handling twice.
    - Added early-cancellation recurrence handling: when an automatic job is cancelled immediately after process creation, completion handling is invoked before early return, preventing the scheduler from remaining indefinitely due or repeatedly re-dispatching.
  - **In-Memory Scheduling Persistence Recovery & Self-Healing Reconciliation (`studio/server.py`)**:
    - Added guarded in-memory scheduling override state (`_IN_MEMORY_NEXT_RUN`, `_IN_MEMORY_CONSECUTIVE_FAILURES`) protected under `with GUARD:`.
    - Upload success persistence failure: If `studio_settings.update_settings({"next_run": ...})` fails while multiple ready videos exist, the calculated recurrence interval (3h + jitter) is held in memory, strictly preventing immediate back-to-back dispatch of subsequent ready videos.
    - Consecutive failure backoff escalation: If settings persistence fails during repeated pipeline errors, failure counts and exponential backoff delays escalate in memory ($60\text{s} \to 120\text{s} \to 240\text{s}$) rather than spinning against stale on-disk timestamps every 15-second polling cycle.
    - Publish-staging discovery wakeup: `AUTOMATION_EVENT.set()` is executed in a `finally:` block so that newly staged ready candidates are immediately dispatched for upload even if persisting `consecutive_failures: 0` to disk raises an exception.
    - Self-healing reconciliation: On each scheduler iteration, if in-memory overrides are present, the scheduler opportunistically persists them to disk once transient filelock or disk errors clear, resetting in-memory overrides to `None` upon successful write.
  - **Test Suite**:
    - `tests/test_stage7_phase4_scheduling.py`: 41/41 passed (3.5s), including 6 in-memory persistence-recovery regression tests (covering upload success persistence failure, publish-staging failure, consecutive failure backoff escalation, self-healing reconciliation, Pacific rollover with a future in-memory deadline, and `/api/settings` mutation override preservation/clearing), 5 deterministic synchronization regression tests, and 3 focused defensive completion-handling and early-cancellation tests.
    - Full repository test discovery suite: 348 passed and 1 skipped out of 349 discovered tests (43.9s, 0 failures, 0 errors, 1 skipped on Windows symlink privileges).
  - **Remaining Stage 7 Phase 4 Milestones**:
    - Milestone 5: Independent health telemetry and diagnostic stall alerts (`studio/supervisor.py`).
    - Milestone 6: Frontend integration and end-to-end verification.

- **Stage 7 Phase 4 Milestone 2 Completed & Verified (Retention Reference Engine & Dry-Run Audit API)**:
  - **Safe Retention Reference Engine (`studio/retention.py`)**:
    - Strictly read-only, non-destructive audit engine for candidate raw downloads in `work/`. Real deletion pathways are NOT implemented. No files are deleted, moved, renamed, truncated, or modified.
    - Uploaded output MP4s and poster PNGs are permanent and never deleted.
    - Verified raw-download naming convention: `work/short_{epoch}_{key}_raw.mp4`.
    - Strict acquisition epoch assessment: File age is computed directly from the acquisition epoch embedded in the filename (`now - epoch`). Never falls back to Windows `ctime`, filesystem `mtime`, or `videos.created_at`.
    - Enforces 7-day retention threshold (604,800s): Files `>= 604,800s` are `age_eligible`; files `< 604,800s` are `too_young`. Validates epoch bounds (`1_000_000_000` to `2_500_000_000`) and rejects clock-skewed future timestamps (`> now + 300s`) with `invalid_timestamp`.
  - **Discovery Rules & Windows Path Safety**:
    - Discovers `.mp4` candidate files ending in `_raw.mp4` directly inside `ROOT / "work"`. Subdirectory scanning is rejected (`not a direct child`).
    - Traversal prevention: Verifies resolved file path is strictly contained within the approved `work/` directory (`relative_to` validation).
    - Windows reparse point and symlink rejection: Rejects symlinks and directory junctions via `os.path.islink()` and Windows `FILE_ATTRIBUTE_REPARSE_POINT` (0x0400). Rejects Windows Alternate Data Streams (`:`). Case-insensitive Windows path comparisons normalized via `os.path.normcase()`.
  - **Conservative Reference Detection & Fail-Closed State Inspection**:
    - Inspects SQLite tables (`videos`, `jobs`), queue state, re-render lineage (`_edit_`), and `state.json` under read-only transactions.
    - References tracked:
      - SQLite video records matching canonical raw path `f"{id}_raw.mp4"` or explicit `raw_video` field.
      - Re-render source lineage: video records with `_edit_` in `id` preserve the original `{orig_key}_raw.mp4` as re-render source.
      - Active and pending jobs in SQLite (`jobs` with status `pending` or `running`).
      - In-progress, unresolved, or pending retry workflows (`ready`, `rendering`, `uploading`, `upload_unknown`, `upload_unresolved`, `failed`, `render_failed`).
      - Host execution state: active pipeline lock (`.pipeline.lock`).
    - Fail-closed behavior: Any database error, unreadable SQLite file, or corrupt `state.json` fails closed with `unknown_reference` without assuming files are unreferenced.
  - **Deterministic Safety Classification Precedence**:
    - Precedence: (1) `inspection_error` -> (2) `invalid_path` / `invalid_metadata` -> (3) `unknown_reference` -> (4) `unknown_age` / `invalid_timestamp` -> (5) `referenced` -> (6) `too_young` -> (7) `age_eligible_unreferenced`.
    - Only files confirmed unreferenced across all successfully inspected state sources and `>= 7 days` old are classified as `age_eligible_unreferenced` (retention review candidates).
  - **Dry-Run Audit API (`GET` and `POST` `/api/retention/audit` & `/api/retention`)**:
    - Exposed on local Studio server in `studio/server.py`.
    - Returns structured JSON with `"mode": "dry_run"`, `"deletion_occurred": false`, `"deletion_enabled": false`, summary metrics (file counts, estimated reclaimable bytes/MB), and sanitized per-file diagnostics with zero internal absolute path leakage.
  - **Test Suite**:
    - `tests/test_stage7_phase4_retention.py`: 35 tests passed (1 skip on Windows symlink privilege, while mocked reparse detection passed). Covers discovery, path traversal, ADS rejection, age thresholds, future timestamp rejection, reference detection across drafts, active/queued jobs, re-render lineage, corrupt DB/state fail-closed, safety precedence, dry-run guarantees, and server integration.
    - Full repository test discovery suite: 307/307 passed (56.8s, 0 failures, 0 errors, 1 skipped).
  - **Remaining Stage 7 Phase 4 Milestones**:
    - Milestone 3: Quota-aware scheduling, jitter, and PT rollover resumption.
    - Milestone 4: Operational alert lifecycle and SQLite partial index (`studio/alerts.py`).
    - Milestone 5: Independent health telemetry and diagnostic stall alerts (`studio/supervisor.py`).
    - Milestone 6: Frontend integration and end-to-end verification.
- **Stage 7 Phase 4 Milestone 1 Completed & Verified (Configuration Integrity, Cross-Process Locking & Atomic Persistence)**:
  - **Standalone Settings Module (`studio/settings.py`)**:
    - Centralized access to `studio-settings.json` with typed exceptions (`SettingsError`, `SettingsLockError`, `ConfigurationCorruptError`).
    - Cross-process OS file locking on `studio-settings.lock` combining `threading.RLock()` with `msvcrt.locking` on Windows (`fcntl.flock` on POSIX) with a 5.0s bounded timeout and fail-closed semantics.
    - Thread-local reentrancy tracking (`_LOCAL = threading.local()`) enabling nested `load_settings()` and `update_settings()` operations within the same thread without file-handle self-deadlocks.
    - Strict global lock hierarchy adherence: `GUARD (Level 1) -> studio-settings.lock (Level 2) -> studio-quota.lock (Level 3) -> SQLite WAL (Level 4)`. Locks are never held across long I/O, network calls, rendering, or subprocess execution.
  - **Schema Validation & Unknown-Field Preservation**:
    - `validate_settings_schema()` enforces strict types: `enabled` (bool), `interval_hours` (strictly finite numeric 1-168, non-bool, rejects `NaN`/`inf`), `mode` ("preview", "publish", with "publish_approved" normalized to "publish"), `auto_discovery_publish_opt_in` (bool), `next_run` (strictly finite non-negative numeric), `consecutive_failures` (non-negative int), and quota suspension fields.
    - Aligned `DEFAULT_SETTINGS` and `DEFAULT_AUTOMATION` strictly to the approved Milestone 1 scope (`{"enabled": False, "interval_hours": 5, "mode": "preview", "next_run": 0, "auto_discovery_publish_opt_in": False}`), leaving existing configured disk settings intact.
    - Unknown and custom configuration fields (e.g. `custom_debug_flag`, arbitrary metadata) are preserved verbatim across all read-modify-write operations without loss or mutation.
    - Missing settings files cleanly initialize with backward-compatible defaults; unreadable or corrupt JSON fails closed with `ConfigurationCorruptError` without overwriting valid data.
  - **Atomic Read-Modify-Write Persistence**:
    - `update_settings()` reads existing settings under lock, applies partial updates, validates the merged document, serializes to a unique `.tmp` file in the same directory with `os.fsync()`, and atomically replaces via `os.replace()`. Cleans up temporary files on write failure.
  - **Server Integration & Error Sanitization (`studio/server.py`)**:
    - Migrated `get_automation_settings()` to delegate to `studio.settings.load_settings()`.
    - Migrated `automation_loop()` to persist `next_run` via `studio.settings.update_settings()`.
    - Migrated `/api/settings` POST handler to persist automation updates via `studio.settings.update_settings()`.
    - Sanitized `/api/settings` error handling: `ConfigurationCorruptError` and `SettingsLockError` are logged to server error log and return clean client error responses without leaking internal filesystem paths.
  - **Test Suite**:
    - `tests/test_stage7_phase4_settings.py`: 30/30 passed (8.7s) covering schema validation, bool-type rejection, non-finite number rejection (`NaN`, `inf`, `-inf`), defaults schema scope, unknown-field preservation, lock reentrancy in the same thread, 10-thread concurrency, 5-subprocess concurrency under `msvcrt.locking`, lock timeouts, abrupt process termination (`kill`) OS handle release, atomic-write failure recovery, server integration, and API error message sanitization.
    - Full repository test discovery suite: 272/272 passed (46.3s, 0 failures, 0 errors).
  - **Remaining Stage 7 Phase 4 Milestones**:
    - Milestone 2: Retention reference engine and dry-run audit API (`studio/retention.py`).
    - Milestone 3: Quota-aware scheduling, jitter, and PT rollover resumption.
    - Milestone 4: Operational alert lifecycle and SQLite partial index (`studio/alerts.py`).
    - Milestone 5: Independent health telemetry and diagnostic stall alerts (`studio/supervisor.py`).
    - Milestone 6: Frontend integration and end-to-end verification.
- **Stage 7 Phase 3 Completed, Fully Corrected & Verified (Command Center, Quota Governance & Discovery Diagnostics)**:
  - **Operational Telemetry & Quota Governance**:
    - Multi-process safe and thread-safe quota coordination: `_quota_lock_guard()` in `studio/store.py` coordinates reads and writes to `studio-quota.json` across concurrent worker subprocesses, server threads, and CLI commands using OS-level file locking (`msvcrt.locking` on Windows, `fcntl.flock` on Unix) on `studio-quota.lock`.
    - Fail-closed quota admission: `discover_youtube` in `core/agent.py` fails closed whenever quota tracking raises an exception or cannot reliably verify limits, preventing runaway unmetered API calls.
    - Persistent quota exhaustion circuit breaker (`google_quota_exhausted: True`) tripped on HTTP 403 `quotaExceeded` for search or video checks; prevents repetitive outbound API retries and automatically resets at Pacific Time midnight rollover.
    - Strict `videos_insert` enforcement: fresh uploads in `studio/worker.py` and `core/agent.py` atomically check and reserve quota before transfer begins; if limit is reached or quota tracker fails, the upload is postponed and the video safely remains `ready` without being falsely marked `failed`. Resumed sessions do not re-consume quota.
    - Timezone-aware midnight Pacific Time (PT) daily reset (`get_pacific_now`, `get_pacific_date`, `get_pacific_reset_info`) with dynamic DST and year-boundary calculation, atomic `.tmp` persistence, and automatic recovery from corrupted JSON files.
    - Non-blocking, sanitized OAuth health diagnostics (`get_oauth_status_details()`) reporting channel metadata and status without exposing sensitive tokens or client credentials.
  - **Incident Resolution API Contract & Strict Video ID Validation**:
    - Unified `POST /api/video/resolve` to support both `action` and `resolution` parameters (`confirm_uploaded`, `confirm_absent`, and `mark_failed` alias).
    - Strict 11-character YouTube video ID validation (`^[A-Za-z0-9_-]{11}$`) rejecting IDs that are too short, too long, empty, or contain invalid characters or whitespace.
    - State guards preventing conflicting re-confirmations (409 Conflict), rejecting `confirm_absent` on already-uploaded videos, and rejecting resolution on currently `uploading` videos.
    - Zero upload re-triggering during manual incident resolution.
  - **Discovery Diagnostics & Run Telemetry**:
    - Standardized telemetry keys (`total_discovered_raw`, `duplicates_within_run`, `unique_candidates`, `seen_candidates`, `rejected_by_reason`, `eligible_candidates`).
    - Evidence-based idle diagnostic explanations (`generate_idle_explanation()`) distinguishing tracker failures, Google quota exhaustion, daily quota limits, upstream query/network errors, and valid zero-candidate yields.
  - **Hardware & Cross-Platform Defect Resolutions**:
    - In `core/render.py`, `has_encoder()` probes hardware encoder initialization (`_WORKING_ENCODERS`) rather than relying solely on `ffmpeg -encoders` compilation flags, preventing crashes on Windows hosts without NVIDIA CUDA drivers by safely falling back to `libx264`.
    - In `scripts/uninstall_service.py`, `uninstall_macos()` uses `getattr(os, "getuid", lambda: 501)()` to prevent `AttributeError` on Windows while preserving macOS behavior.
  - **Executive Operational Command Center (Overview UI)**:
    - Cleaned duplicate HTML markup, panel headers, and disk space cards.
    - Added high-visibility OAuth alert banners when credentials need attention.
    - Operational KPI cards (Ready, Uploaded, In Queue, Total Videos) with dynamic layout.
    - Automation Schedule widget with dynamic local real-time countdown timer (`startOverviewCountdown`, `clearOverviewCountdown`) with `beforeunload` teardown.
    - System Storage Health indicator with color-coded safety thresholds (<512 MB critical, <1 GB low, healthy).
    - Visual Daily Quota Meters for search queries (100 cap), video uploads, and general units with midnight PT reset timer.
    - Actionable Incident Remediation cards with 1-click buttons: Reconcile / Confirm / Fail for `upload_unknown` and `upload_unresolved`, and Retry Render for `render_failed`.
    - Latest Discovery Diagnostics card displaying source yield and rejection breakdowns.
  - **Test Suite**:
    - `tests/test_stage7_phase3.py`: 27/27 passed (1.9s).
    - Stage 6 & 7 Combined Regression Suite: 103/103 passed (17.7s).
    - Full discovery suite (`python -m unittest discover tests`): 238/238 passed (35.5s), 0 failures, 0 errors!
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
### Stage 7 Phase 3 Corrective Fix: Review Findings & Targeted Corrections

#### Implementation Date
2026-10-02

#### Addressed Review Areas & Technical Solutions

1. **Incident Resolution API Contract & State Transition Guards**:
   - **Unified Contract**: `POST /api/video/resolve` in `studio/server.py` now uniformly accepts either `action` (used by frontend `resolveIncident`) or `resolution` (used by tests/CLI), and honors `confirmed: true` / `confirm: true`.
   - **Supported Actions**: Canonical actions `confirm_uploaded` and `confirm_absent` are supported, alongside backward-compatible alias `mark_failed` (which maps safely to `confirm_absent`).
   - **YouTube Video ID Extraction & Validation**: When `confirm_uploaded` is called, `youtube_id` or `youtube_url` is parsed to extract clean 11-character alphanumeric IDs (handling standard `watch?v=`, `youtu.be/`, `/shorts/`, or raw IDs). Invalid or empty IDs return HTTP 400 Bad Request. Regex validation `^[A-Za-z0-9_-]+$` prevents malformed strings.
   - **State Transition Guards**:
     - Videos currently in `uploading` cannot be resolved manually (returns HTTP 409 Conflict).
     - Videos already marked `uploaded` reject conflicting YouTube IDs (HTTP 409 Conflict), but permit idempotent re-confirmation if the ID matches.
     - `confirm_absent` on already-uploaded videos is rejected with HTTP 409 Conflict.
     - Manual incident resolution does not trigger an outbound upload or queue an upload job.
   - **Frontend Synchronization**: `studio/web/app.js` sends `{ id, action: canonicalAction, resolution: canonicalAction, confirmed: true }` and passes `youtube_id` when resolving `upload_unresolved` items.

2. **Persistent Quota Exhaustion Circuit Breaker**:
   - **Circuit Breaker Flag**: In `studio/store.py`, `studio-quota.json` persists `google_quota_exhausted: True` alongside `date_pt` and `reason`.
   - **Trip Mechanism**: Tripped on HTTP 403 `quotaExceeded` during YouTube search (`core/agent.py:discover_youtube`) or license validation (`core/agent.py:verify_youtube_licenses`) via `store.mark_google_quota_exhausted()`.
   - **Pre-Flight Protection**: Subsequent discovery runs detect `google_quota_exhausted` in pre-flight checks and immediately bypass all search queries without issuing any outbound network requests to the YouTube Data API.
   - **Date Rollover**: The circuit breaker automatically resets (`google_quota_exhausted: False`) when the Pacific Time date rolls over past midnight PT.

3. **Atomic Quota Admission & Request-Start Accounting**:
   - **Atomic Check & Reserve**: Introduced `store.check_and_reserve_quota(action_type, units=1, now_utc=None)` protected under `_QUOTA_LOCK`. Callers atomically check limits and increment counters in a single critical section, preventing multi-threaded or concurrent pipeline runs from racing past daily limits.
   - **Request-Start Upload Accounting**:
     - In `studio/worker.py` and `core/agent.py:upload_to_youtube`, upload quota (`videos_insert`, 1600 units equivalent) is accounted at upload initiation before HTTP transfer begins.
     - Delayed post-sync accounting was eliminated to avoid missing failed initial attempts.
     - Resumed upload sessions (`resumable_uri` present) bypass re-recording to avoid double-charging quota for network retries.

4. **Discovery Diagnostics Telemetry Standardization**:
   - **Standardized Metric Names**: In `core/agent.py`, candidate tracking dictionary keys are standardized: `total_discovered_raw`, `duplicates_within_run`, `unique_candidates`, `seen_candidates`, `rejected_by_reason`, and `eligible_candidates`.
   - **Failure Distinction**: `generate_idle_explanation(stats)` distinguishes true empty candidate yields from complete upstream source failures. If network errors or API failures occurred across configured sources, it explicitly reports `"Discovery queries failed due to network or upstream API errors (X failure(s))"` instead of claiming no content matched criteria.

5. **Timezone & Persistence Resilience**:
   - **Pacific Time Calculation**: `get_pacific_reset_info()` dynamically calculates next midnight PT by finding the start of the next calendar day in Pacific Time and computing the exact epoch difference, resilient to Daylight Saving Time boundaries and year rollover.
   - **Atomic File Persistence**: `_save_quota_data()` writes serialized JSON to a temporary file (`.tmp`) and uses atomic `os.replace` to prevent corrupted files during sudden power loss or process interruption.
   - **Auto-Recovery**: `_load_and_sanitize_quota_data()` safely catches corrupted, truncated, or invalid JSON, as well as negative or invalid counter types, automatically resetting to a clean, valid quota schema.

6. **Incident & Frontend Safety**:
   - **Event Listener Teardown**: In `studio/web/app.js`, a `beforeunload` listener clears active intervals and countdown timers (`overviewCountdownTimer`, `statusInterval`).
   - **CSS Contract Verification**: Verified styles for `.stat-card`, `.stat-card:hover`, `.video-card:hover`, and incident button states in `studio/web/style.css`.

### Stage 7 Phase 3 Final Corrections & Verification

#### Implementation Date
2026-10-03

#### Addressed Areas & Architectural Solutions

1. **Fail-Closed Quota Admission (`core/agent.py`)**:
   - **Pre-Flight Exception Guard**: If `get_local_quota_tracker()` raises an exception (e.g. disk failure or missing state), `discover_youtube` immediately fails closed, logs `LOG.error()`, emits `emit_progress("YouTube quota tracker unavailable — skipping search")`, sets `stats["quota_tracker_failed"] = True`, and skips all search queries without issuing outbound requests.
   - **Per-Query Exception Guard**: If `check_and_reserve_quota("search_list", 1)` raises an exception, the search loop logs the failure, marks `stats["quota_tracker_failed"] = True`, and aborts search queries immediately.
   - **Evidence-Based Idle Explanation**: `generate_idle_explanation()` distinctly reports:
     - `"YouTube quota tracking failed; search queries aborted to fail closed"` when tracker errors occur.
     - `"YouTube search quota exhausted by upstream API; 0 candidates from other sources"` when the Google 403 breaker is active.
     - `"YouTube search quota limit reached; 0 candidates from other sources"` when the local daily query cap is reached.
     - `"Discovery queries failed due to network or upstream API errors (X failure(s))"` when upstream network/HTTP errors occur.
     - `"No candidates returned by configured discovery queries or channels"` for valid empty candidate yields.

2. **Strict `videos_insert` Quota Limit Enforcement (`studio/worker.py` & `core/agent.py`)**:
   - **Pre-Upload Atomic Check & Reserve**: Before initiating a fresh upload, `studio/worker.py` invokes `store.check_and_reserve_quota("videos_insert", 1)` under multi-process lock. If the limit is reached or quota checking fails, `QuotaBlockedError` is raised.
   - **Safe State Preservation**: If an upload is blocked by quota governance before transfer starts, the video record is safely reset to `status="ready"` with an informative message. The video is **not** falsely marked `failed`.
   - **Resumable Session Preservation**: Resuming an existing upload session (`resumable_uri` present) bypasses quota reservation, avoiding duplicate charges for chunk retries.
   - **Upload Failure Accounting**: Because the YouTube Data API consumes upload quota upon initiation of video creation, quota units reserved at initiation are intentionally retained if a network failure occurs during chunk transfer; the video transitions to `upload_unknown` for reconciliation.

3. **Strict 11-Character YouTube Video ID Validation (`studio/server.py`)**:
   - In `resolve_manual_video()` and `POST /api/video/resolve`, IDs are strictly validated against `^[A-Za-z0-9_-]{11}$`.
   - Accepts raw 11-character IDs or standard YouTube URLs (`watch?v=`, `youtu.be/`, `/shorts/`) from which the 11-character ID is extracted.
   - Rejects IDs that are too short (<11), too long (>11), empty, whitespace, or contain invalid punctuation/characters with `400 Bad Request`.
   - Maintains protected state guards: rejects resolving active `uploading` videos (409 Conflict) and already-uploaded videos on `confirm_absent` (409 Conflict).

4. **Multi-Process Quota Safety (`studio/store.py`)**:
   - **Single-Server vs Multi-Process Architecture**: KenauShorts enforces single-server execution via `server_lock()` on `.server.lock`. However, worker jobs and discovery runs execute as independent child processes (`studio.worker`, `core.agent`), and operators can execute CLI commands concurrently.
   - **OS-Level File Coordination**: Implemented `_quota_lock_guard()` combining thread-safe `threading.RLock()` with cross-process file locking on `studio-quota.lock` (`msvcrt.locking` on Windows, `fcntl.flock` on POSIX).
   - **Atomic File Persistence**: Data is written via `.tmp` and atomic `os.replace`, guaranteeing crash resilience and preventing race conditions or lost quota updates across processes.

5. **Hardware & Cross-Platform Defect Resolutions**:
   - **Hardware Encoder Probe (`core/render.py`)**: `has_encoder()` previously relied solely on `ffmpeg -encoders` output (which lists all compiled encoders, including `h264_nvenc`). On Windows systems lacking NVIDIA CUDA hardware/drivers (`nvcuda.dll`), this caused ffmpeg crashes. `has_encoder()` now probes whether hardware encoders can actually initialize (`_WORKING_ENCODERS`), falling back cleanly to `libx264`.
   - **Cross-Platform Service Uninstall (`scripts/uninstall_service.py`)**: `uninstall_macos()` used `os.getuid()`, which does not exist on Windows. Updated to `getattr(os, "getuid", lambda: 501)()` to prevent `AttributeError` when running tests or scripts on Windows.

---

## Stage 7 Phase 3 Quota Safety Corrections

### Implementation Date
2026-10-03

### Baseline Commit
`d571c27` (`test(stage7): harden test_timed_out_video_retry_without_server_restart against queue worker race`)

### Addressed Vulnerabilities & Architectural Improvements

1. **Fail-Closed Multi-Process Lock Guard (`studio/store.py`)**:
   - `_quota_lock_guard()` now raises `QuotaLockError` if OS-level file locking on `studio-quota.lock` fails or times out after 5.0 seconds. It no longer proceeds under only the in-process thread lock.
   - OS lock acquisition status is tracked via `acquired_os_lock`; lock release is attempted only when acquisition succeeded, and the file descriptor is always closed in `finally`.
   - Callers (`discover_youtube`, worker uploads) fail closed on `QuotaLockError`, preventing uncontrolled outbound API requests.

2. **Fail-Closed Quota Persistence (`studio/store.py`)**:
   - `_save_quota_data()` propagates `QuotaPersistenceError` if temporary file creation or atomic `replace()` fails.
   - Reservations are never treated as successful unless safely persisted to disk.
   - Discovery and worker uploads catch persistence exceptions and fail closed without making outbound requests. Blocked videos remain in `status="ready"` with summary `blocked`.

3. **Safe Handling of Corrupt Quota Files (`studio/store.py`)**:
   - `_load_and_sanitize_quota_data()` distinguishes between missing quota files and corrupt/unreadable files:
     - Missing file: Initializes a fresh default tracker for today (`today_pt`).
     - Malformed JSON, unreadable file, non-dict root, missing `date_pt`, or invalid field data types (e.g. non-numeric strings, negative counts, booleans in integer fields): Raises `QuotaCorruptError` to fail closed instead of silently resetting counts to zero.
     - Legitimate rollover: If `date_pt != today_pt` on a valid file, preserves configured limits and resets counts to 0.

4. **Quota Recovery Procedure**:
   - In the event of disk corruption or `QuotaCorruptError`:
     1. Review the Google Developer Console for the project's authoritative quota consumption for the current Pacific Time day.
     2. Stop background workers and the Studio server (`python -m studio.server --stop` or terminate process).
     3. If manual recovery is desired, create/repair `studio-quota.json` with valid JSON:
        ```json
        {
          "date_pt": "YYYY-MM-DD",
          "videos_insert_count": <ACTUAL_UPLOADS_TODAY>,
          "search_list_count": <ACTUAL_SEARCHES_TODAY>,
          "general_units": 0,
          "videos_insert_limit": 100,
          "search_list_limit": 100,
          "general_units_limit": 10000,
          "google_quota_exhausted": false,
          "google_quota_exhausted_reason": "",
          "google_quota_exhausted_at": "",
          "disclaimer": "Local estimate only. Google Developer Console is the authoritative source of truth."
        }
        ```
     4. Alternatively, if no uploads or searches have been performed today, delete `studio-quota.json`; the system will automatically re-initialize default safe state.
     5. Restart the Studio server.

5. **Exactly-Once Upload Reservation (`studio/worker.py` & `core/agent.py`)**:
   - Fresh uploads invoke `check_and_reserve_quota("videos_insert", 1)` exactly once before upload initiation.
   - Resumable continuations (`resumable_uri` present) bypass reservation to prevent double-charging.
   - Reconciliations (`confirm_uploaded`, `confirm_absent`) do not invoke `videos_insert`.
   - Replaced error-string matching with typed exceptions (`QuotaBlockedError`, `UploadSessionExpiredError`).

6. **Strict Video ID Validation & URL Extraction (`studio/server.py`)**:
   - `extract_youtube_video_id()` extracts canonical 11-char IDs from `watch?v=`, `youtu.be/`, `/shorts/`, stripping whitespace, query parameters, and fragments.
   - In strict mode (`strict_id=True`), strictly validates full string against `^[A-Za-z0-9_-]{11}$`, rejecting short, long, whitespace, or invalid character inputs with HTTP 400.

7. **Deterministic Phase 2 Retry Test (`tests/test_stage7_phase2.py`)**:
   - `test_timed_out_video_retry_without_server_restart` uses `threading.Event` synchronization inside patched `run_job_process` to confirm the retried job is picked up and transitioned to `completed` without timing-dependent sleeps.

### Test Evidence
- **Stage 7 Phase 3 Suite** (`tests/test_stage7_phase3.py`): **31/31 passed** (2.4s).
- **Stage 7 Phase 2 Suite** (`tests/test_stage7_phase2.py`): **20/20 passed** (8.0s).
- **Stage 7 Phase 1 Suite** (`tests/test_stage7_phase1.py`): **18/18 passed** (2.3s).
- **Stage 6 Publishing Suite** (`tests/test_stage6_publishing.py`): **38/38 passed** (6.2s).
- **Combined Targeted Regression Suite**: **107/107 passed** (16.2s).
- **Full Discovery Suite** (`python -X utf8 -m unittest discover tests`):
  - **242 tests total: 242 passed, 0 failures, 0 errors** (45.5s)!
- **Compilation & Formatting Checks**:
  - `python -m compileall core studio scripts tests`: 0 errors.
  - `git diff --check`: 0 warnings/errors.

### Remaining Limitations
- Local quota counters remain estimates; Google Developer Console is the authoritative source of truth.
- Stage 7 Phase 4 (Milestones 1-6) + Operational Smoke Test & Probe Concurrency Race Fix completed & verified on origin/main (a8682c48c7014831b20249ecc033f8d8c17572c1).
