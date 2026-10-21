"""
tests/test_supervisor.py — Deterministic Acceptance Tests for Milestone 5
Tests independent health telemetry, bounded probe timeouts, stall semantics,
deduplication, auto-recovery, and server shutdown integration.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from studio import alerts, server, supervisor
from studio.supervisor import BoundedProbeRunner, Supervisor, probe_database, probe_disk_space


class TestSupervisor(unittest.TestCase):
    @contextlib.contextmanager
    def open_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_supervisor.db")
        alerts.reset_engine_for_testing()
        alerts.start_alert_engine(self.db_path)
        self.stop_event = threading.Event()
        server.ACTIVE_JOB = None
        server.QUEUE_WORKER_THREAD = None
        server.SCHEDULER_DIAGNOSTICS = {"state": "idle", "deferral_reason": ""}
        self.patch_settings = patch("studio.settings.load_settings", return_value={"enabled": False, "interval_hours": 3, "next_run": 0})
        self.patch_settings.start()

    def tearDown(self):
        if hasattr(self, "patch_settings"):
            self.patch_settings.stop()
        alerts.reset_engine_for_testing()
        server.ACTIVE_JOB = None
        server.QUEUE_WORKER_THREAD = None
        server.SCHEDULER_DIAGNOSTICS = {"state": "idle", "deferral_reason": ""}
        if hasattr(self, "sup") and self.sup:
            self.sup.stop(timeout=1.0)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -----------------------------------------------------------------------
    # 1. Bounded Database Probe Timeout
    # -----------------------------------------------------------------------
    def test_bounded_database_probe_timeout(self):
        """Verify that a slow or blocked database probe strictly times out within <= 2.0s."""
        runner = BoundedProbeRunner(max_timeout=0.3)
        block_event = threading.Event()

        def _blocking_query():
            block_event.wait(timeout=5.0)
            return {"ok": True}

        t0 = time.monotonic()
        ok, res, err = runner.run_probe(_blocking_query, timeout=0.3)
        elapsed = time.monotonic() - t0

        self.assertFalse(ok)
        self.assertIsNone(res)
        self.assertEqual(err, "probe_timed_out")
        self.assertLess(elapsed, 1.5, "Probe runner must not block beyond timeout")
        block_event.set()

    # -----------------------------------------------------------------------
    # 2. Bounded Guard Lock Contention
    # -----------------------------------------------------------------------
    def test_bounded_guard_contention(self):
        """Verify that supervisor active job probe does not hang when server.GUARD is held."""
        sup = Supervisor(db_path=self.db_path, stop_event=self.stop_event)
        hold_guard = threading.Event()
        guard_held = threading.Event()

        def _hold():
            with server.GUARD:
                guard_held.set()
                hold_guard.wait(timeout=5.0)

        th = threading.Thread(target=_hold, daemon=True)
        th.start()
        self.assertTrue(guard_held.wait(timeout=2.0))

        t0 = time.monotonic()
        job_info = sup._inspect_active_jobs(time.time())
        elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 1.5, "Lock acquisition must respect 0.5s timeout")
        self.assertIn(job_info["status"], ("idle", "running"))
        hold_guard.set()
        th.join(timeout=1.0)

    # -----------------------------------------------------------------------
    # 3. Disk Space Warning, Critical, and Verified Recovery
    # -----------------------------------------------------------------------
    def test_disk_space_warning_critical_and_recovery(self):
        """Verify disk space alerts fire at <1024MB and <512MB, and auto-resolve at >= 1200MB."""
        sup = Supervisor(
            db_path=self.db_path,
            stop_event=self.stop_event,
            disk_critical_mb=512.0,
            disk_low_mb=1024.0,
            disk_recovery_mb=1200.0,
        )

        with patch("shutil.disk_usage") as mock_usage:
            # Step A: 400MB free -> Critical Alert
            mock_usage.return_value = shutil._ntuple_diskusage(1000 * 1024 * 1024, 600 * 1024 * 1024, 400 * 1024 * 1024)
            sup.tick()
            alerts.wait_for_drain(timeout=2.0)
            with self.open_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT fingerprint, status, severity FROM alerts WHERE resolved_at IS NULL")
                rows = cur.fetchall()
            fps = {r[0]: r[2] for r in rows}
            self.assertIn("supervisor.storage.disk_critical", fps)
            self.assertEqual(fps["supervisor.storage.disk_critical"], "critical")
            self.assertNotIn("supervisor.storage.disk_low", fps)

            # Step B: 800MB free -> Warning Alert (Critical resolved)
            mock_usage.return_value = shutil._ntuple_diskusage(1000 * 1024 * 1024, 200 * 1024 * 1024, 800 * 1024 * 1024)
            sup.tick()
            alerts.wait_for_drain(timeout=2.0)
            with self.open_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT fingerprint, status, severity FROM alerts WHERE resolved_at IS NULL")
                rows = cur.fetchall()
            fps = {r[0]: r[2] for r in rows}
            self.assertIn("supervisor.storage.disk_low", fps)
            self.assertEqual(fps["supervisor.storage.disk_low"], "warning")
            self.assertNotIn("supervisor.storage.disk_critical", fps)

            # Step C: 1500MB free -> Fully Recovered
            mock_usage.return_value = shutil._ntuple_diskusage(2000 * 1024 * 1024, 500 * 1024 * 1024, 1500 * 1024 * 1024)
            sup.tick()
            alerts.wait_for_drain(timeout=2.0)
            with self.open_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT fingerprint, status FROM alerts WHERE resolved_at IS NULL")
                rows = cur.fetchall()
            self.assertEqual(len(rows), 0, "All storage alerts must be resolved upon space recovery")

    # -----------------------------------------------------------------------
    # 4. Scheduler Stall Detection and Quota Deferral Exclusion
    # -----------------------------------------------------------------------
    def test_scheduler_stall_detection_and_quota_deferral_exclusion(self):
        """Verify scheduler stall alert fires when overdue with no job, but suppresses during all legitimate quota/state deferrals."""
        sup = Supervisor(
            db_path=self.db_path,
            stop_event=self.stop_event,
            scheduler_stall_threshold=60.0,
        )

        now = time.time()
        # Mock overdue next_run and enabled=True
        mock_settings = {"enabled": True, "next_run": now - 150.0}

        with patch("studio.settings.load_settings", return_value=mock_settings):
            # Case A: Stalled (no deferral reason, no active job)
            server.SCHEDULER_DIAGNOSTICS["state"] = "waiting_for_schedule"
            server.SCHEDULER_DIAGNOSTICS["deferral_reason"] = ""
            server.ACTIVE_JOB = None
            sup.tick()
            alerts.wait_for_drain(timeout=2.0)

            with self.open_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT fingerprint FROM alerts WHERE resolved_at IS NULL")
                fps = [r[0] for r in cur.fetchall()]
                self.assertIn("supervisor.scheduler.stalled", fps)

            # Case B: Verify each legitimate quota/deferral state suppresses stall alert
            legitimate_cases = [
                ("waiting_for_quota", "google_quota_exhausted"),
                ("waiting_for_quota", "upload_quota_limit"),
                ("waiting_for_quota", "search_quota_limit"),
                ("waiting_for_candidate", "no_ready_videos"),
                ("waiting_for_candidate", "publish_waiting_for_candidate"),
                ("waiting_for_worker", "worker_busy"),
                ("waiting_for_network", "offline"),
                ("disabled", "disabled"),
            ]

            for state, reason in legitimate_cases:
                server.SCHEDULER_DIAGNOSTICS["state"] = state
                server.SCHEDULER_DIAGNOSTICS["deferral_reason"] = reason
                sup.tick()
                alerts.wait_for_drain(timeout=2.0)
                with self.open_db() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT fingerprint FROM alerts WHERE resolved_at IS NULL")
                    fps = [r[0] for r in cur.fetchall()]
                    self.assertNotIn("supervisor.scheduler.stalled", fps, f"Stall alert was not suppressed for ({state}, {reason})")

    # -----------------------------------------------------------------------
    # 5. Active Job Stall Detection and Progress Resumption
    # -----------------------------------------------------------------------
    def test_active_job_stall_detection_and_progress_resumption(self):
        """Verify active job progress stall fires alert at >300s and resolves when progress resumes."""
        sup = Supervisor(
            db_path=self.db_path,
            stop_event=self.stop_event,
            job_stall_threshold=300.0,
        )

        old_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=400)).isoformat()
        job_data = {
            "id": "job_stall_test",
            "status": "running",
            "stage": "Rendering video card",
            "stages": [
                {"stage": "Downloading media", "at": old_ts},
                {"stage": "Rendering video card", "at": old_ts},
            ],
            "pid": 99999,
        }

        # Put job in SQLite and memory
        with self.open_db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            conn.execute("INSERT OR REPLACE INTO jobs (id, data) VALUES (?, ?)", (job_data["id"], json.dumps(job_data)))

        with server.GUARD:
            server.ACTIVE_JOB = job_data

        # Tick 1: Stalled
        sup.tick()
        alerts.wait_for_drain(timeout=2.0)
        with self.open_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT fingerprint, message FROM alerts WHERE resolved_at IS NULL")
            rows = cur.fetchall()
            fps = [r[0] for r in rows]
            self.assertIn("supervisor.job.stall_job_stall_test", fps)
            self.assertIn("Rendering video card", rows[0][1])

        # Tick 2: Progress resumes with fresh stage
        fresh_ts = dt.datetime.now(dt.timezone.utc).isoformat()
        job_data["stages"].append({"stage": "Compositing overlay", "at": fresh_ts})
        job_data["stage"] = "Compositing overlay"
        with self.open_db() as conn:
            conn.execute("INSERT OR REPLACE INTO jobs (id, data) VALUES (?, ?)", (job_data["id"], json.dumps(job_data)))

        sup.tick()
        alerts.wait_for_drain(timeout=2.0)
        with self.open_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT fingerprint FROM alerts WHERE resolved_at IS NULL")
            fps = [r[0] for r in cur.fetchall()]
            self.assertNotIn("supervisor.job.stall_job_stall_test", fps, "Alert must be resolved on progress resumption")

        with server.GUARD:
            server.ACTIVE_JOB = None

    # -----------------------------------------------------------------------
    # 6. Active Job Terminal Transition Clears Stall
    # -----------------------------------------------------------------------
    def test_active_job_terminal_transition_clears_stall(self):
        """Verify that when a stalled job transitions to ANY terminal state, its stall alert is resolved."""
        terminal_states = ["completed", "failed", "cancelled", "interrupted", "idle"]

        for term_status in terminal_states:
            job_id = f"job_term_{term_status}"
            sup = Supervisor(db_path=self.db_path, stop_event=self.stop_event, job_stall_threshold=100.0)

            old_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=200)).isoformat()
            job_data = {
                "id": job_id,
                "status": "running",
                "stage": "Rendering",
                "stages": [{"stage": "Rendering", "at": old_ts}],
            }

            with self.open_db() as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
                conn.execute("INSERT OR REPLACE INTO jobs (id, data) VALUES (?, ?)", (job_data["id"], json.dumps(job_data)))

            with server.GUARD:
                server.ACTIVE_JOB = job_data

            sup.tick()
            alerts.wait_for_drain(timeout=2.0)
            with self.open_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT fingerprint FROM alerts WHERE resolved_at IS NULL")
                self.assertIn(f"supervisor.job.stall_{job_id}", [r[0] for r in cur.fetchall()])

            # Job reaches terminal state
            job_data["status"] = term_status
            with self.open_db() as conn:
                conn.execute("INSERT OR REPLACE INTO jobs (id, data) VALUES (?, ?)", (job_data["id"], json.dumps(job_data)))
            with server.GUARD:
                server.ACTIVE_JOB = None

            sup.tick()
            alerts.wait_for_drain(timeout=2.0)
            with self.open_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT fingerprint FROM alerts WHERE resolved_at IS NULL")
                self.assertNotIn(f"supervisor.job.stall_{job_id}", [r[0] for r in cur.fetchall()], f"Alert for terminal status {term_status} was not resolved")

    # -----------------------------------------------------------------------
    # 7. Missing and Corrupt Job Timestamps
    # -----------------------------------------------------------------------
    def test_missing_and_corrupt_job_timestamps(self):
        """Verify supervisor safely handles missing, empty, or corrupt timestamps without false alerts."""
        sup = Supervisor(db_path=self.db_path, stop_event=self.stop_event)

        corrupt_job = {
            "id": "job_corrupt",
            "status": "running",
            "stage": "Unknown",
            "stages": [{"stage": "Unknown", "at": "not-a-timestamp"}],
            "started_at": "invalid-iso",
        }

        with server.GUARD:
            server.ACTIVE_JOB = corrupt_job

        t0 = time.monotonic()
        telemetry = sup.tick()
        elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 1.0)
        self.assertFalse(telemetry["subsystems"]["active_job"]["stalled"])

        with self.open_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT fingerprint FROM alerts WHERE resolved_at IS NULL")
            fps = [r[0] for r in cur.fetchall()]
            self.assertNotIn("supervisor.job.stall_job_corrupt", fps)

        with server.GUARD:
            server.ACTIVE_JOB = None

    # -----------------------------------------------------------------------
    # 8. Queue Worker Dead with Pending Jobs
    # -----------------------------------------------------------------------
    def test_queue_worker_dead_with_pending_jobs(self):
        """Verify queue worker alert fires only when worker thread is dead AND jobs are pending."""
        sup = Supervisor(db_path=self.db_path, stop_event=self.stop_event)

        with self.open_db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            conn.execute("INSERT INTO jobs (id, data) VALUES ('j_pend', ?)", (json.dumps({"status": "pending"}),))

        # Worker is None (dead) and job is pending -> Alert fires
        server.QUEUE_WORKER_THREAD = None
        sup.tick()
        alerts.wait_for_drain(timeout=2.0)

        with self.open_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT fingerprint FROM alerts WHERE resolved_at IS NULL")
            fps = [r[0] for r in cur.fetchall()]
            self.assertIn("supervisor.queue.worker_dead", fps)

        # Worker becomes alive -> Alert auto-resolves
        alive_worker = threading.Thread(target=lambda: time.sleep(1.0), daemon=True)
        alive_worker.start()
        server.QUEUE_WORKER_THREAD = alive_worker

        sup.tick()
        alerts.wait_for_drain(timeout=2.0)
        with self.open_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT fingerprint FROM alerts WHERE resolved_at IS NULL")
            fps = [r[0] for r in cur.fetchall()]
            self.assertNotIn("supervisor.queue.worker_dead", fps)

        alive_worker.join(timeout=2.0)
        server.QUEUE_WORKER_THREAD = None

    # -----------------------------------------------------------------------
    # 9. Supervisor Tick Deduplication
    # -----------------------------------------------------------------------
    def test_supervisor_tick_deduplication(self):
        """Verify repeated ticks under persistent critical disk space result in exactly 1 active DB alert."""
        sup = Supervisor(db_path=self.db_path, stop_event=self.stop_event)

        with patch("shutil.disk_usage") as mock_usage:
            mock_usage.return_value = shutil._ntuple_diskusage(1000 * 1024 * 1024, 700 * 1024 * 1024, 300 * 1024 * 1024)
            # Execute 5 consecutive ticks
            for _ in range(5):
                sup.tick()
            alerts.wait_for_drain(timeout=2.0)

            with self.open_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT id, fingerprint, occurrence_count FROM alerts WHERE resolved_at IS NULL")
                rows = cur.fetchall()
                self.assertEqual(len(rows), 1, "Exactly one active alert row must exist (no duplicate incidents)")
                self.assertEqual(rows[0][1], "supervisor.storage.disk_critical")

    # -----------------------------------------------------------------------
    # 10. Supervisor Immediate Shutdown
    # -----------------------------------------------------------------------
    def test_supervisor_immediate_shutdown(self):
        """Verify supervisor thread sleeping on 15s interval exits immediately (<0.2s) when stopped."""
        sup = Supervisor(tick_interval=15.0, db_path=self.db_path)
        th = sup.start()
        self.assertTrue(th.is_alive())

        t0 = time.monotonic()
        clean = sup.stop(timeout=1.0)
        elapsed = time.monotonic() - t0

        self.assertTrue(clean, "Supervisor must report clean stop")
        self.assertFalse(th.is_alive())
        self.assertLess(elapsed, 0.3, "Supervisor must exit promptly without waiting for 15s tick")

    # -----------------------------------------------------------------------
    # 11. Shutdown Quiescence with Hung Supervisor
    # -----------------------------------------------------------------------
    def test_shutdown_quiescence_integration_hung_supervisor(self):
        """Verify stop_studio_application reports degraded shutdown if a worker thread hangs."""
        class MockServer:
            def __init__(self):
                self._lifecycle_lock = threading.Lock()
                self._active_handlers = set()
                self._in_flight_handlers = 0
            def stop_serving(self, total_timeout=1.0):
                return True, {"state": "stopped"}

        hung_event = threading.Event()
        hung_th = threading.Thread(target=lambda: hung_event.wait(timeout=5.0), name="HungWorker")
        hung_th.start()

        s = MockServer()
        app_clean, app_diag = server.stop_studio_application(
            server=s,
            alert_engine_stop_fn=alerts.stop_alert_engine,
            stop_event=threading.Event(),
            background_workers=[hung_th],
            total_timeout=0.2,
        )

        self.assertFalse(app_clean, "App clean must fail closed when a worker hangs")
        self.assertFalse(app_diag["producers_quiescent"], "Producers must not be quiescent")
        self.assertEqual(app_diag["live_workers_count"], 1)
        self.assertFalse(app_diag["alerts_diag"]["producers_quiescent"])
        self.assertFalse(app_diag["alerts_diag"]["accounting_complete"])

        hung_event.set()
        hung_th.join(timeout=1.0)

    # -----------------------------------------------------------------------
    # 12. Shutdown Quiescence Clean Integration
    # -----------------------------------------------------------------------
    def test_shutdown_quiescence_integration_clean(self):
        """Verify clean end-to-end shutdown when supervisor joins normally."""
        class MockServer:
            def __init__(self):
                self._lifecycle_lock = threading.Lock()
                self._active_handlers = set()
                self._in_flight_handlers = 0
            def stop_serving(self, total_timeout=1.0):
                return True, {"state": "stopped"}

        stop_ev = threading.Event()
        sup = Supervisor(tick_interval=15.0, db_path=self.db_path, stop_event=stop_ev)
        sup_th = sup.start()

        s = MockServer()
        app_clean, app_diag = server.stop_studio_application(
            server=s,
            alert_engine_stop_fn=alerts.stop_alert_engine,
            stop_event=stop_ev,
            background_workers=[sup_th],
            total_timeout=2.0,
        )

        self.assertTrue(app_clean, f"Clean shutdown expected: {app_diag}")
        self.assertTrue(app_diag["producers_quiescent"])
        self.assertEqual(app_diag["live_workers_count"], 0)
        self.assertTrue(app_diag["alerts_diag"]["accounting_complete"])
        self.assertFalse(sup_th.is_alive())

    # -----------------------------------------------------------------------
    # 13. Probe Runner Thread Accumulation Prevention
    # -----------------------------------------------------------------------
    def test_probe_runner_prevents_unbounded_thread_accumulation(self):
        """Verify that if a probe times out, the concurrency lock remains held until the worker finishes, preventing thread accumulation."""
        runner = BoundedProbeRunner(max_timeout=0.2)
        barrier = threading.Event()

        def _slow_probe():
            barrier.wait(timeout=2.0)
            return "slow_done"

        # First probe times out
        ok1, res1, err1 = runner.run_probe(_slow_probe, timeout=0.1)
        self.assertFalse(ok1)
        self.assertEqual(err1, "probe_timed_out")

        # Concurrency lock MUST remain held until the actual probe worker finishes!
        self.assertTrue(runner.is_probe_running())

        # Second probe submitted while first thread is still holding lock is immediately rejected
        ok2, res2, err2 = runner.run_probe(lambda: True, timeout=0.1)
        self.assertFalse(ok2)
        self.assertEqual(err2, "probe_worker_busy_previous_timed_out")

        # Release the slow probe worker
        barrier.set()

        # Wait briefly for worker thread to exit and release concurrency lock
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and runner.is_probe_running():
            time.sleep(0.01)
        self.assertFalse(runner.is_probe_running())

        # Third probe after release succeeds normally without rejection
        ok3, res3, err3 = runner.run_probe(lambda: "healthy", timeout=0.5)
        self.assertTrue(ok3)
        self.assertEqual(res3, "healthy")
        self.assertIsNone(err3)

    # -----------------------------------------------------------------------
    # 14. Probe Runner Sequential Immediate Handoff
    # -----------------------------------------------------------------------
    def test_bounded_probe_runner_immediate_sequential_handoff(self):
        """Verify sequential probes on the same runner complete back-to-back without busy rejection."""
        runner = BoundedProbeRunner(max_timeout=1.0)
        for i in range(10):
            probe_val = f"probe_result_{i}"
            ok, res, err = runner.run_probe(lambda val=probe_val: val, timeout=0.5)
            self.assertTrue(ok, f"Sequential probe #{i} failed with error: {err}")
            self.assertEqual(res, probe_val)
            self.assertIsNone(err)


if __name__ == "__main__":
    unittest.main()
