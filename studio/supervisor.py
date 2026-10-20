"""
studio/supervisor.py — Independent Health Telemetry and Diagnostic Stall Alerts
Provides non-invasive health probes, bounded timeout execution, stall detection,
and automated alert lifecycle integration with studio.alerts.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from studio import alerts

LOG = logging.getLogger("kenaushorts.supervisor")
ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Constants & Defaults
# ---------------------------------------------------------------------------
DEFAULT_TICK_INTERVAL: float = 15.0
DEFAULT_JOB_STALL_THRESHOLD: float = 300.0  # 5 minutes without progress
DEFAULT_SCHEDULER_STALL_THRESHOLD: float = 120.0  # 2 minutes overdue
DEFAULT_DISK_CRITICAL_MB: float = 512.0
DEFAULT_DISK_LOW_MB: float = 1024.0
DEFAULT_DISK_RECOVERY_MB: float = 1200.0  # Hysteresis buffer
PROBE_TIMEOUT_SECONDS: float = 2.0
GUARD_ACQUIRE_TIMEOUT: float = 0.5

LEGITIMATE_SCHEDULER_STATES = frozenset({
    "waiting_for_quota",      # Preflight upload/search quota deferral
    "waiting_for_candidate",  # Publish mode waiting for candidate (Option B)
    "waiting_for_network",    # Offline / network disconnected
    "waiting_for_worker",     # Active job or queue busy
    "disabled",               # Scheduler disabled in studio-settings.json
})

LEGITIMATE_SCHEDULER_DEFERRALS = frozenset({
    "google_quota_exhausted",        # Google quota exhausted (circuit breaker)
    "upload_quota_limit",            # YouTube upload quota limit reached
    "search_quota_limit",            # YouTube search quota limit reached
    "no_ready_videos",               # Waiting for ready candidate in publish mode
    "publish_waiting_for_candidate", # Option B deferral reason alias
    "worker_busy",                   # Worker process active
    "offline",                       # Network unreachable
    "disabled",                      # Automation disabled
})

TERMINAL_JOB_STATUSES = frozenset({
    "completed",
    "failed",
    "cancelled",
    "idle",
    "interrupted",
})


# ---------------------------------------------------------------------------
# Bounded Probe Runner (Guarantees <= 2.0s and No Thread Accumulation)
# ---------------------------------------------------------------------------
class BoundedProbeRunner:
    """
    Executes probes with a hard timeout and strictly bounds thread accumulation
    by acquiring a concurrency lock that remains held until the actual probe worker
    finishes, refusing to spawn new probe threads if a previous probe is still running.
    """
    def __init__(self, max_timeout: float = PROBE_TIMEOUT_SECONDS):
        self.max_timeout = max_timeout
        self._concurrency_lock = threading.Lock()
        self._active_probe: Optional[threading.Thread] = None

    def is_probe_running(self) -> bool:
        """Returns True if a probe worker thread is currently running or holding the concurrency lock."""
        return self._concurrency_lock.locked()

    def run_probe(self, probe_fn: Callable[[], Any], timeout: Optional[float] = None) -> tuple[bool, Any, Optional[str]]:
        to = timeout if timeout is not None else self.max_timeout

        # Non-blocking attempt to acquire the concurrency lock.
        # If already held by an in-flight probe worker, immediately reject without spawning a thread.
        acquired = self._concurrency_lock.acquire(blocking=False)
        if not acquired:
            return False, None, "probe_worker_busy_previous_timed_out"

        result_holder: list[Any] = [None]
        error_holder: list[Optional[Exception]] = [None]
        done_event = threading.Event()

        def _target():
            try:
                try:
                    result_holder[0] = probe_fn()
                except Exception as e:
                    error_holder[0] = e
                finally:
                    done_event.set()
            finally:
                # The concurrency lock remains held until the actual probe worker finishes!
                self._concurrency_lock.release()

        try:
            th = threading.Thread(target=_target, name="SupervisorProbeWorker", daemon=True)
            self._active_probe = th
            th.start()
        except Exception as e:
            self._concurrency_lock.release()
            return False, None, f"thread_spawn_failed: {e}"

        if done_event.wait(timeout=to):
            if error_holder[0] is not None:
                return False, None, str(error_holder[0])
            return True, result_holder[0], None
        else:
            # Caller timed out.
            # Crucial: self._concurrency_lock remains held in background until _target finishes!
            return False, None, "probe_timed_out"


_GLOBAL_PROBE_RUNNER = BoundedProbeRunner(max_timeout=PROBE_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Individual Bounded Probes
# ---------------------------------------------------------------------------
def probe_database(
    db_path: Optional[Path | str] = None,
    runner: Optional[BoundedProbeRunner] = None,
) -> dict[str, Any]:
    """
    Executes a lightweight SQLite probe query (SELECT 1) bounded by busy_timeout
    and a hard wall-clock probe timeout.
    """
    p_runner = runner or _GLOBAL_PROBE_RUNNER
    if db_path is None:
        import studio.store as store
        target_path = store.DB
    else:
        target_path = Path(db_path)

    def _do_query():
        t0 = time.monotonic()
        conn = None
        try:
            conn = sqlite3.connect(target_path, timeout=1.5, check_same_thread=False)
            cur = conn.cursor()
            cur.execute("SELECT 1;")
            row = cur.fetchone()
            elapsed = time.monotonic() - t0
            return {"ok": bool(row and row[0] == 1), "latency_sec": round(elapsed, 4)}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    ok, res, err = p_runner.run_probe(_do_query, timeout=PROBE_TIMEOUT_SECONDS)
    if ok and res and res.get("ok"):
        return {"status": "healthy", "latency_sec": res.get("latency_sec", 0.0), "error": None}
    return {"status": "unresponsive", "latency_sec": None, "error": err or "unresponsive"}


def probe_disk_space(
    root_dir: Optional[Path | str] = None,
    runner: Optional[BoundedProbeRunner] = None,
) -> dict[str, Any]:
    """
    Probes filesystem disk usage on root_dir bounded by probe timeout.
    """
    p_runner = runner or _GLOBAL_PROBE_RUNNER
    target_dir = Path(root_dir) if root_dir else ROOT

    def _do_stat():
        usage = shutil.disk_usage(target_dir)
        if hasattr(usage, "free"):
            free_bytes = usage.free
            total_bytes = usage.total
        elif isinstance(usage, (tuple, list)) and len(usage) >= 3:
            total_bytes, free_bytes = usage[0], usage[2]
        else:
            free_bytes = getattr(usage, "free", 0)
            total_bytes = getattr(usage, "total", 0)
        free_mb = round(free_bytes / (1024 * 1024), 1)
        total_mb = round(total_bytes / (1024 * 1024), 1)
        return {"free_mb": free_mb, "total_mb": total_mb}

    ok, res, err = p_runner.run_probe(_do_stat, timeout=PROBE_TIMEOUT_SECONDS)
    if ok and res:
        free_mb = res["free_mb"]
        total_mb = res["total_mb"]
        if free_mb < DEFAULT_DISK_CRITICAL_MB:
            status = "critical"
        elif free_mb < DEFAULT_DISK_LOW_MB:
            status = "low"
        else:
            status = "healthy"
        return {"status": status, "free_mb": free_mb, "total_mb": total_mb, "error": None}
    return {"status": "unknown", "free_mb": 0.0, "total_mb": 0.0, "error": err or "probe_failed"}


def parse_timestamp_to_epoch(ts: Any) -> Optional[float]:
    """Safely converts an ISO timestamp string or float to Unix epoch seconds."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts) if ts > 0 else None
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        # Normalize trailing Z for Python 3.10 compatibility if needed
        clean = ts.strip().replace("Z", "+00:00")
        return dt.datetime.fromisoformat(clean).timestamp()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Independent Runtime Supervisor
# ---------------------------------------------------------------------------
class Supervisor:
    """
    Independent background supervisor coordinating runtime telemetry,
    health assessments, and diagnostic stall alerts.
    """
    def __init__(
        self,
        tick_interval: float = DEFAULT_TICK_INTERVAL,
        job_stall_threshold: float = DEFAULT_JOB_STALL_THRESHOLD,
        scheduler_stall_threshold: float = DEFAULT_SCHEDULER_STALL_THRESHOLD,
        disk_critical_mb: float = DEFAULT_DISK_CRITICAL_MB,
        disk_low_mb: float = DEFAULT_DISK_LOW_MB,
        disk_recovery_mb: float = DEFAULT_DISK_RECOVERY_MB,
        db_path: Optional[Path | str] = None,
        root_dir: Optional[Path | str] = None,
        stop_event: Optional[threading.Event] = None,
    ):
        self.tick_interval = tick_interval
        self.job_stall_threshold = job_stall_threshold
        self.scheduler_stall_threshold = scheduler_stall_threshold
        self.disk_critical_mb = disk_critical_mb
        self.disk_low_mb = disk_low_mb
        self.disk_recovery_mb = disk_recovery_mb
        self.db_path = db_path
        self.root_dir = root_dir

        self._stop_event = stop_event or threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._probe_runner = BoundedProbeRunner(max_timeout=PROBE_TIMEOUT_SECONDS)

        self._state_lock = threading.Lock()
        self._active_alerts: set[str] = set()
        self._latest_telemetry: dict[str, Any] = {}
        self._started_at = time.time()
        self._ticks_count = 0

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> threading.Thread:
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return self._thread
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, name="SupervisorThread", daemon=True)
            self._thread.start()
            return self._thread

    def stop(self, timeout: float = 2.0) -> bool:
        self._stop_event.set()
        th = self._thread
        if th and th.is_alive() and th != threading.current_thread():
            th.join(timeout=timeout)
            return not th.is_alive()
        return True

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as e:
                LOG.error("Unexpected error in supervisor tick: %s", e)

            # Wake up immediately when stop_event is signaled
            if self._stop_event.wait(timeout=self.tick_interval):
                break

    def tick(self) -> dict[str, Any]:
        """
        Executes a single inspection cycle: evaluates probes, detects stalls,
        emits/resolves alerts, and updates cached telemetry.
        """
        telemetry = self.collect_telemetry()
        self._evaluate_alerts(telemetry)
        with self._state_lock:
            self._ticks_count += 1
            self._latest_telemetry = telemetry
        return telemetry

    def get_latest_telemetry(self) -> dict[str, Any]:
        with self._state_lock:
            if self._latest_telemetry:
                return dict(self._latest_telemetry)
        return self.collect_telemetry()

    # -----------------------------------------------------------------------
    # Telemetry Collection (Pure Non-Invasive Probes)
    # -----------------------------------------------------------------------
    def collect_telemetry(self) -> dict[str, Any]:
        now_ts = time.time()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts))

        # 1. Database Probe
        db_res = probe_database(self.db_path, runner=self._probe_runner)

        # 2. Disk Probe
        disk_res = probe_disk_space(self.root_dir, runner=self._probe_runner)

        # 3. Thread & Process Inspection (Cooperative & Bounded)
        scheduler_info = self._inspect_scheduler(now_ts)
        queue_info = self._inspect_queue_worker()
        job_info = self._inspect_active_jobs(now_ts)
        alert_engine_info = self._inspect_alert_engine()

        # 4. Overall Health Grade
        # Unhealthy: critical disk, unresponsive db, dead queue with jobs, or stalled job
        # Degraded: low disk, stalled scheduler, alert engine backlog
        is_unhealthy = (
            disk_res.get("status") == "critical" or
            db_res.get("status") == "unresponsive" or
            queue_info.get("status") == "dead" or
            job_info.get("status") == "stalled"
        )
        is_degraded = (
            disk_res.get("status") == "low" or
            scheduler_info.get("status") == "stalled" or
            not alert_engine_info.get("drain_healthy", True)
        )

        overall_status = "unhealthy" if is_unhealthy else ("degraded" if is_degraded else "healthy")

        with self._state_lock:
            active_alerts_list = sorted(list(self._active_alerts))

        return {
            "status": overall_status,
            "timestamp": now_iso,
            "uptime_seconds": round(now_ts - self._started_at, 1),
            "subsystems": {
                "database": db_res,
                "storage": disk_res,
                "scheduler": scheduler_info,
                "queue_worker": queue_info,
                "active_job": job_info,
                "alerts": alert_engine_info,
            },
            "active_supervisor_alerts": active_alerts_list,
        }

    # -----------------------------------------------------------------------
    # Subsystem Inspectors
    # -----------------------------------------------------------------------
    def _inspect_scheduler(self, now_ts: float) -> dict[str, Any]:
        from studio import server
        import studio.settings as studio_settings

        # Non-blocking settings load
        try:
            settings_path = ROOT / "studio-settings.json"
            settings = studio_settings.load_settings(settings_path=settings_path)
            enabled = bool(settings.get("enabled", False))
            next_run = float(settings.get("next_run", 0))
        except Exception:
            enabled = False
            next_run = 0.0

        diag = getattr(server, "SCHEDULER_DIAGNOSTICS", {})
        sched_state = diag.get("state", "idle")
        deferral_reason = diag.get("deferral_reason", "")

        # Check thread liveliness
        # Look for thread in server globals or inspect active threads
        sched_thread_alive = False
        for th in threading.enumerate():
            if th.name == "AutomationLoop" or getattr(th, "_target", None) == server.automation_loop:
                sched_thread_alive = th.is_alive()
                break

        # Stall evaluation:
        # enabled, next_run overdue > 120s, no running job, and deferral reason / state not legitimate
        is_overdue = (next_run > 0) and ((now_ts - next_run) > self.scheduler_stall_threshold)
        is_legitimate_deferral = (
            deferral_reason in LEGITIMATE_SCHEDULER_DEFERRALS or
            sched_state in LEGITIMATE_SCHEDULER_STATES
        )

        active_job = getattr(server, "ACTIVE_JOB", None)
        has_running_job = bool(active_job and active_job.get("status") == "running")

        stalled = enabled and is_overdue and (not has_running_job) and (not is_legitimate_deferral)
        status = "disabled" if not enabled else ("stalled" if stalled else "healthy")

        return {
            "status": status,
            "thread_alive": sched_thread_alive,
            "scheduler_state": sched_state,
            "deferral_reason": deferral_reason,
            "enabled": enabled,
            "next_run": next_run,
            "overdue_seconds": round(max(0.0, now_ts - next_run), 1) if next_run > 0 else 0.0,
            "stalled": stalled,
        }

    def _inspect_queue_worker(self) -> dict[str, Any]:
        from studio import server
        import studio.store as store

        worker_th = getattr(server, "QUEUE_WORKER_THREAD", None)
        thread_alive = bool(worker_th and worker_th.is_alive())

        pending_count = 0
        target_db = Path(self.db_path) if self.db_path else store.DB
        try:
            if target_db.exists():
                conn = sqlite3.connect(f"file:{target_db}?mode=ro", uri=True, timeout=1.5)
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT data FROM jobs;")
                    for row in cur.fetchall():
                        try:
                            d = json.loads(row[0])
                            if d.get("status") == "pending":
                                pending_count += 1
                        except Exception:
                            pass
                finally:
                    conn.close()
            else:
                records = store.records("jobs")
                pending_count = sum(1 for j in records if j.get("status") == "pending")
        except Exception:
            pass

        if not thread_alive and pending_count > 0:
            status = "dead"
            stalled = True
        elif not thread_alive:
            status = "idle"
            stalled = False
        else:
            status = "healthy"
            stalled = False

        return {
            "status": status,
            "thread_alive": thread_alive,
            "pending_jobs_count": pending_count,
            "stalled": stalled,
        }

    def _inspect_active_jobs(self, now_ts: float) -> dict[str, Any]:
        from studio import server
        import studio.store as store

        # Attempt to inspect in-memory ACTIVE_JOB with bounded timeout
        active_job = None
        guard = getattr(server, "GUARD", None)
        if guard:
            acquired = guard.acquire(timeout=GUARD_ACQUIRE_TIMEOUT)
            if acquired:
                try:
                    active_job = getattr(server, "ACTIVE_JOB", None)
                finally:
                    guard.release()

        # Fallback to database if no memory job inspected
        if not active_job:
            target_db = Path(self.db_path) if self.db_path else store.DB
            try:
                if target_db.exists():
                    conn = sqlite3.connect(f"file:{target_db}?mode=ro", uri=True, timeout=1.5)
                    try:
                        cur = conn.cursor()
                        cur.execute("SELECT data FROM jobs;")
                        for row in cur.fetchall():
                            try:
                                d = json.loads(row[0])
                                if d.get("status") == "running":
                                    active_job = d
                                    break
                            except Exception:
                                pass
                    finally:
                        conn.close()
                else:
                    for j in store.records("jobs"):
                        if j.get("status") == "running":
                            active_job = j
                            break
            except Exception:
                pass

        if not active_job or active_job.get("status") != "running":
            return {
                "status": "idle",
                "job_id": None,
                "pid": None,
                "stage": None,
                "elapsed_without_progress_sec": 0.0,
                "stalled": False,
            }

        job_id = active_job.get("id", "unknown")
        pid = active_job.get("pid")
        stage = active_job.get("stage", "running")

        # Determine last progress timestamp
        stages = active_job.get("stages", [])
        last_progress_epoch: Optional[float] = None
        if isinstance(stages, list) and stages:
            last_entry = stages[-1]
            if isinstance(last_entry, dict):
                last_progress_epoch = parse_timestamp_to_epoch(last_entry.get("at"))

        if last_progress_epoch is None:
            last_progress_epoch = parse_timestamp_to_epoch(active_job.get("started_at"))

        if last_progress_epoch is None:
            # Missing or unparseable timestamps; avoid false stall alert
            return {
                "status": "running",
                "job_id": job_id,
                "pid": pid,
                "stage": stage,
                "elapsed_without_progress_sec": 0.0,
                "stalled": False,
                "unparseable_timestamp": True,
            }

        elapsed = max(0.0, now_ts - last_progress_epoch)
        stalled = elapsed > self.job_stall_threshold
        status = "stalled" if stalled else "running"

        return {
            "status": status,
            "job_id": job_id,
            "pid": pid,
            "stage": stage,
            "elapsed_without_progress_sec": round(elapsed, 1),
            "stalled": stalled,
        }

    def _inspect_alert_engine(self) -> dict[str, Any]:
        drain_healthy = getattr(alerts, "_DRAIN_THREAD_HEALTHY", True)
        queue_depth = 0
        q = getattr(alerts, "_ALERT_QUEUE", None)
        if q:
            queue_depth = q.qsize()
        engine_state = getattr(alerts, "_ENGINE_STATE", "unknown")

        return {
            "drain_healthy": drain_healthy,
            "queue_depth": queue_depth,
            "engine_state": engine_state,
        }

    # -----------------------------------------------------------------------
    # Alert Evaluation & Verified-Condition Auto-Recovery
    # -----------------------------------------------------------------------
    def _evaluate_alerts(self, telemetry: dict[str, Any]) -> None:
        subsystems = telemetry.get("subsystems", {})

        # A. Storage / Disk Alerts
        storage = subsystems.get("storage", {})
        free_mb = storage.get("free_mb", 0.0)
        disk_status = storage.get("status")

        fp_critical = "supervisor.storage.disk_critical"
        fp_low = "supervisor.storage.disk_low"

        if disk_status == "critical":
            self._fire(
                fp=fp_critical,
                reason="Critically low disk space (<512MB free)",
                message=f"Free disk space is critically low: {free_mb} MB available.",
                severity="critical",
                metadata={"free_mb": free_mb},
            )
            # Resolve low if active
            self._resolve(fp_low, "Superseded by critical disk alert")
        elif disk_status == "low":
            self._fire(
                fp=fp_low,
                reason="Low disk space (<1024MB free)",
                message=f"Free disk space is low: {free_mb} MB available.",
                severity="warning",
                metadata={"free_mb": free_mb},
            )
            self._resolve(fp_critical, "Free space increased above critical threshold")
        elif free_mb >= self.disk_recovery_mb:
            self._resolve(fp_critical, f"Disk space recovered to {free_mb} MB")
            self._resolve(fp_low, f"Disk space recovered to {free_mb} MB")

        # B. Database Unresponsive Alert
        db_sub = subsystems.get("database", {})
        fp_db = "supervisor.db.unresponsive"
        if db_sub.get("status") == "unresponsive":
            self._fire(
                fp=fp_db,
                reason="Database probe unresponsive or timed out",
                message=f"SQLite probe failed: {db_sub.get('error', 'timed out')}",
                severity="critical",
                metadata={"error": db_sub.get("error")},
            )
        else:
            self._resolve(fp_db, "Database probe responsive")

        # C. Scheduler Stalled Alert
        sched = subsystems.get("scheduler", {})
        fp_sched = "supervisor.scheduler.stalled"
        if sched.get("stalled"):
            self._fire(
                fp=fp_sched,
                reason="Automation scheduler stalled",
                message=f"Scheduler is overdue by {sched.get('overdue_seconds', 0)}s with no active job.",
                severity="warning",
                metadata={
                    "overdue_sec": sched.get("overdue_seconds"),
                    "deferral_reason": sched.get("deferral_reason"),
                },
            )
        else:
            self._resolve(fp_sched, "Scheduler active or legitimately deferred")

        # D. Queue Worker Dead Alert
        queue_w = subsystems.get("queue_worker", {})
        fp_queue = "supervisor.queue.worker_dead"
        if queue_w.get("stalled"):
            self._fire(
                fp=fp_queue,
                reason="Queue worker thread dead with pending jobs",
                message=f"Queue worker thread is dead while {queue_w.get('pending_jobs_count')} jobs are pending.",
                severity="critical",
                metadata={"pending_count": queue_w.get("pending_jobs_count")},
            )
        else:
            self._resolve(fp_queue, "Queue worker operational or queue empty")

        # E. Active Job Progress Stall Alert
        active_j = subsystems.get("active_job", {})
        job_id = active_j.get("job_id")
        fp_job = f"supervisor.job.stall_{job_id}" if job_id else None

        if active_j.get("stalled") and fp_job:
            self._fire(
                fp=fp_job,
                reason=f"Job {job_id} progress stalled",
                message=f"Job {job_id} in stage '{active_j.get('stage')}' has had no progress updates for {active_j.get('elapsed_without_progress_sec')}s.",
                severity="critical",
                metadata={
                    "job_id": job_id,
                    "pid": active_j.get("pid"),
                    "stage": active_j.get("stage"),
                    "elapsed_sec": active_j.get("elapsed_without_progress_sec"),
                },
            )
        else:
            # Active job is no longer stalled (progress resumed) or no running job
            if fp_job:
                self._resolve(fp_job, "Job progress resumed")

            # Collect active stall alert fingerprints from memory and database
            known_stall_fps = set()
            with self._state_lock:
                for a in self._active_alerts:
                    if a.startswith("supervisor.job.stall_"):
                        known_stall_fps.add(a)

            import studio.store as store
            target_db = Path(self.db_path) if self.db_path else store.DB
            try:
                if target_db.exists():
                    conn = sqlite3.connect(f"file:{target_db}?mode=ro", uri=True, timeout=1.5)
                    try:
                        cur = conn.cursor()
                        cur.execute("SELECT fingerprint FROM alerts WHERE fingerprint LIKE 'supervisor.job.stall_%' AND resolved_at IS NULL;")
                        for row in cur.fetchall():
                            known_stall_fps.add(row[0])
                    finally:
                        conn.close()
            except Exception:
                pass

            for a_fp in known_stall_fps:
                if not job_id or a_fp != fp_job:
                    j_id = a_fp[len("supervisor.job.stall_"):]
                    j_status = "completed"
                    try:
                        j_rec = store.get("jobs", j_id)
                        if j_rec and isinstance(j_rec, dict):
                            j_status = j_rec.get("status", "completed")
                    except Exception:
                        pass

                    if j_status in TERMINAL_JOB_STATUSES or j_status != "running":
                        self._resolve(a_fp, f"Job reached terminal state '{j_status}'")

    def _fire(self, fp: str, reason: str, message: str, severity: str = "warning", metadata: Optional[dict] = None) -> None:
        try:
            alerts.fire_alert(
                fingerprint=fp,
                component="supervisor",
                reason=reason,
                message=message,
                severity=severity,
                metadata=metadata or {},
            )
            with self._state_lock:
                self._active_alerts.add(fp)
        except Exception as e:
            LOG.warning("Supervisor could not fire alert %s: %s", fp, e)

    def _resolve(self, fp: str, reason: str) -> None:
        try:
            alerts.resolve_condition(
                fingerprint=fp,
                reason=reason,
                actor="supervisor",
            )
            with self._state_lock:
                self._active_alerts.discard(fp)
        except Exception as e:
            LOG.warning("Supervisor could not resolve alert %s: %s", fp, e)


# ---------------------------------------------------------------------------
# Global Supervisor Singleton Helpers
# ---------------------------------------------------------------------------
_GLOBAL_SUPERVISOR: Optional[Supervisor] = None
_SUPERVISOR_LOCK = threading.Lock()


def get_supervisor(tick_interval: float = DEFAULT_TICK_INTERVAL, **kwargs) -> Supervisor:
    global _GLOBAL_SUPERVISOR
    with _SUPERVISOR_LOCK:
        if _GLOBAL_SUPERVISOR is None:
            _GLOBAL_SUPERVISOR = Supervisor(tick_interval=tick_interval, **kwargs)
        return _GLOBAL_SUPERVISOR


def start_supervisor(tick_interval: float = DEFAULT_TICK_INTERVAL, **kwargs) -> Supervisor:
    sup = get_supervisor(tick_interval=tick_interval, **kwargs)
    sup.start()
    return sup


def stop_supervisor(timeout: float = 2.0) -> bool:
    global _GLOBAL_SUPERVISOR
    with _SUPERVISOR_LOCK:
        if _GLOBAL_SUPERVISOR:
            ok = _GLOBAL_SUPERVISOR.stop(timeout=timeout)
            return ok
        return True


def get_health_telemetry() -> dict[str, Any]:
    global _GLOBAL_SUPERVISOR
    with _SUPERVISOR_LOCK:
        sup = _GLOBAL_SUPERVISOR
    if sup:
        return sup.get_latest_telemetry()
    # Ad-hoc collection if supervisor is not started
    ad_hoc = Supervisor()
    return ad_hoc.collect_telemetry()
