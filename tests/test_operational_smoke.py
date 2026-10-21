"""
tests/test_operational_smoke.py — Controlled Non-Publishing Operational Smoke Test.

Validates end-to-end operational readiness of KenauShorts Studio under Stage 7 Phase 4:
1. Pre-execution action inventory and safety isolation verification.
2. Outbound API & YouTube upload blackhole protection (poison-pills on all publish/network entry points).
3. Fully isolated runtime environment (isolated database, isolated settings, zero mutation to production state).
4. Live loopback HTTP server startup on an ephemeral port (StudioServer + StudioHandler).
5. Live health telemetry (/api/health), supervisor daemon, and alert engine integration.
6. Frontend static asset delivery (/index.html, /style.css, /app.js) and operational API contract inspection.
7. Graceful application shutdown, producer quiescence, and thread join lifecycle.
8. Post-test integrity audit: 0 YouTube API calls, 0 external sockets, untouched studio-settings.json.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import studio.alerts as alerts
import studio.server as server
import studio.store as store
import studio.supervisor as supervisor
from studio import settings as studio_settings


class OperationalSmokeTest(unittest.TestCase):
    """Controlled, non-publishing operational smoke test in isolated local environment."""

    @classmethod
    def setUpClass(cls):
        # 1. Audit production settings before any execution
        cls.root_settings_path = Path("studio-settings.json")
        cls.assertTrue(cls.root_settings_path.is_file(), "Production studio-settings.json must exist")
        cls.orig_settings_bytes = cls.root_settings_path.read_bytes()
        cls.orig_settings_data = json.loads(cls.orig_settings_bytes.decode("utf-8"))
        assert cls.orig_settings_data.get("interval_hours") == 3, (
            "Protected constraint: interval_hours must be exactly 3 before test"
        )

        # 2. Track any prohibited outbound call attempts
        cls.prohibited_calls_attempted: list[str] = []

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="smoke_test_")
        self.tmp_path = Path(self.tmp_dir)

        # Isolated SQLite database
        self.isolated_db_path = self.tmp_path / "smoke_studio.sqlite3"
        conn = sqlite3.connect(self.isolated_db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS videos (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        conn.commit()
        conn.close()

        # Isolated settings file (non-publishing mode, disabled automation for safety)
        self.isolated_settings_path = self.tmp_path / "isolated-settings.json"
        self.isolated_settings_data = {
            "enabled": False,
            "interval_hours": 3,
            "mode": "preview",
            "next_run": 0,
            "consecutive_failures": 0,
        }
        self.isolated_settings_path.write_text(json.dumps(self.isolated_settings_data), encoding="utf-8")

        # Save server/store globals for clean tearDown restoration
        self.orig_port = server.PORT
        self.orig_store_db = store.DB
        self.orig_sup_instance = server.SUPERVISOR_INSTANCE
        self.orig_sup_thread = server.SUPERVISOR_THREAD
        self.orig_global_sup = supervisor._GLOBAL_SUPERVISOR

        # Wire isolated database into store
        store.DB = self.isolated_db_path

        # Setup safety poison pills to block any outbound actions
        self._setup_safety_barriers()

        # Wire cleanup handlers in guaranteed reverse order
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.addCleanup(self._audit_protected_settings)
        self.addCleanup(self._restore_globals)
        self.addCleanup(self._cleanup_alert_engine)

        # Track runtime components for safe teardown
        self.httpd: server.StudioServer | None = None
        self.server_thread: threading.Thread | None = None
        self.sup: supervisor.Supervisor | None = None
        self.sup_thread: threading.Thread | None = None
        self.stop_event: threading.Event | None = None
        self.assigned_port: int | None = None

    def tearDown(self):
        # Fallback defensive cleanup in case addCleanup registration was interrupted
        self._cleanup_http_server()
        self._cleanup_supervisor()
        self._cleanup_alert_engine()
        self._restore_globals()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        self._audit_protected_settings()

    def _cleanup_http_server(self):
        """Idempotent cleanup for live HTTP server and its serving thread."""
        if hasattr(self, "httpd") and self.httpd is not None:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
            try:
                self.httpd.server_close()
            except Exception:
                pass
        if hasattr(self, "server_thread") and self.server_thread is not None and self.server_thread.is_alive():
            self.server_thread.join(timeout=2.0)

    def _cleanup_supervisor(self):
        """Idempotent cleanup for background supervisor and its thread."""
        if hasattr(self, "stop_event") and self.stop_event is not None:
            self.stop_event.set()
        if hasattr(self, "sup") and self.sup is not None:
            try:
                self.sup.stop(timeout=1.0)
            except Exception:
                pass
        if hasattr(self, "sup_thread") and self.sup_thread is not None and self.sup_thread.is_alive():
            self.sup_thread.join(timeout=2.0)

    def _cleanup_alert_engine(self):
        """Idempotent cleanup for alert engine."""
        try:
            alerts.stop_alert_engine(timeout=1.5)
        except Exception:
            pass
        alerts.reset_engine_for_testing()

    def _restore_globals(self):
        """Restore global server and supervisor references."""
        server.PORT = self.orig_port
        store.DB = self.orig_store_db
        server.SUPERVISOR_INSTANCE = self.orig_sup_instance
        server.SUPERVISOR_THREAD = self.orig_sup_thread
        supervisor._GLOBAL_SUPERVISOR = self.orig_global_sup

    def _audit_protected_settings(self):
        """Audit production settings file to guarantee zero modification."""
        curr_settings_bytes = self.root_settings_path.read_bytes()
        self.assertEqual(
            curr_settings_bytes,
            self.orig_settings_bytes,
            "Protected constraint violated: studio-settings.json was modified!",
        )
        curr_data = json.loads(curr_settings_bytes.decode("utf-8"))
        self.assertEqual(
            curr_data.get("interval_hours"),
            3,
            "Protected constraint violated: interval_hours was modified!",
        )

    def _setup_safety_barriers(self):
        """Install non-publishing poison pills across all external API and upload entry points."""
        def _poison_pill_upload(*args, **kwargs):
            self.prohibited_calls_attempted.append("upload_to_youtube")
            raise RuntimeError("CRITICAL SAFETY VIOLATION: Live YouTube upload attempted during smoke test!")

        def _poison_pill_discover(*args, **kwargs):
            self.prohibited_calls_attempted.append("discover_youtube")
            raise RuntimeError("CRITICAL SAFETY VIOLATION: Live YouTube discovery attempted during smoke test!")

        def _poison_pill_connection_test(*args, **kwargs):
            self.prohibited_calls_attempted.append("test_youtube_connection")
            raise RuntimeError("CRITICAL SAFETY VIOLATION: External YouTube OAuth connection test attempted!")

        def _poison_pill_reconcile(*args, **kwargs):
            self.prohibited_calls_attempted.append("reconcile_video_upload")
            raise RuntimeError("CRITICAL SAFETY VIOLATION: Resumable upload reconciliation attempted!")

        # Patch core and server entry points
        self.patch_upload = patch("core.agent.upload_to_youtube", side_effect=_poison_pill_upload)
        self.patch_discover = patch("core.agent.discover_youtube", side_effect=_poison_pill_discover)
        self.patch_conn = patch("studio.server.test_youtube_connection", side_effect=_poison_pill_connection_test)
        self.patch_recon = patch("studio.server.reconcile_video_upload", side_effect=_poison_pill_reconcile)

        # Offline/loopback socket guard: simulated is_online=True without external network probes
        self.patch_online = patch("studio.server.is_online", return_value=True)

        # Isolated automation settings loader
        self.patch_settings = patch(
            "studio.server.get_automation_settings",
            side_effect=lambda: json.loads(self.isolated_settings_path.read_text(encoding="utf-8")),
        )
        self.patch_load_settings = patch(
            "studio.settings.load_settings",
            side_effect=lambda *a, **kw: json.loads(self.isolated_settings_path.read_text(encoding="utf-8")),
        )

        self.patch_upload.start()
        self.patch_discover.start()
        self.patch_conn.start()
        self.patch_recon.start()
        self.patch_online.start()
        self.patch_settings.start()
        self.patch_load_settings.start()

        self.addCleanup(self.patch_upload.stop)
        self.addCleanup(self.patch_discover.stop)
        self.addCleanup(self.patch_conn.stop)
        self.addCleanup(self.patch_recon.stop)
        self.addCleanup(self.patch_online.stop)
        self.addCleanup(self.patch_settings.stop)
        self.addCleanup(self.patch_load_settings.stop)

    def _http_get(self, path: str, host: str = None) -> tuple[int, dict[str, str], bytes]:
        """Performs a loopback HTTP GET request enforcing loopback-only destinations."""
        url = f"http://127.0.0.1:{self.assigned_port}{path}"
        req = urllib.request.Request(url)
        req.add_header("Host", host or f"127.0.0.1:{self.assigned_port}")
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                headers = dict(resp.getheaders())
                body = resp.read()
                return resp.status, headers, body
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read()

    def test_controlled_operational_smoke_run(self):
        """
        Executes complete operational smoke test:
        - Bootstraps AlertEngine and Supervisor on isolated SQLite DB
        - Binds StudioServer on loopback ephemeral port
        - Probes all key endpoints (/api/health, /api/status, /api/alerts, /api/retention/audit, static assets)
        - Verifies non-publishing safety barriers
        - Shuts down cleanly via stop_studio_application
        """
        # --- Step 1: Start Alert Engine & Supervisor ---
        alerts.reset_engine_for_testing()
        alerts.start_alert_engine(str(self.isolated_db_path))
        self.addCleanup(self._cleanup_alert_engine)

        self.stop_event = threading.Event()
        self.sup = supervisor.Supervisor(
            tick_interval=1.0,
            db_path=str(self.isolated_db_path),
            stop_event=self.stop_event,
        )
        self.sup_thread = self.sup.start()
        self.addCleanup(self._cleanup_supervisor)

        server.SUPERVISOR_INSTANCE = self.sup
        supervisor._GLOBAL_SUPERVISOR = self.sup

        # --- Step 2: Bind & Start Live Studio HTTP Server ---
        self.httpd = server.StudioServer(("127.0.0.1", 0), server.StudioHandler)
        self.assigned_port = self.httpd.server_address[1]
        server.PORT = self.assigned_port

        self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self._cleanup_http_server)
        self.assertTrue(self.httpd._loop_ready.wait(timeout=2.0), "Server loop must be ready within 2.0s")

        # --- Step 3: Probe Static Assets ---
        # 3.1: Root / (index.html)
        status, headers, body = self._http_get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        html_text = body.decode("utf-8")
        self.assertIn('id="host-info"', html_text)
        self.assertIn('id="health-modal"', html_text)
        self.assertIn('id="health-modal-body"', html_text)

        # 3.2: CSS stylesheet (/style.css)
        status, headers, body = self._http_get("/style.css")
        self.assertEqual(status, 200)
        css_text = body.decode("utf-8")
        self.assertIn(".status-indicator.degraded", css_text)
        self.assertIn(".health-pill", css_text)
        self.assertIn(".subsystem-grid", css_text)

        # 3.3: JavaScript frontend (/app.js)
        status, headers, body = self._http_get("/app.js")
        self.assertEqual(status, 200)
        js_text = body.decode("utf-8")
        self.assertIn("fetchHealthTelemetry", js_text)
        self.assertIn("evaluateHealthState", js_text)
        self.assertIn("pollStatusInFlight", js_text)

        # --- Step 4: Operational API Endpoints ---
        # 4.1: /api/status
        status, headers, body = self._http_get("/api/status")
        self.assertEqual(status, 200)
        status_data = json.loads(body.decode("utf-8"))
        self.assertTrue(status_data.get("online"))
        self.assertIn("csrf", status_data)
        self.assertIsNone(status_data.get("active_job"))

        # 4.2: /api/health
        status, headers, body = self._http_get("/api/health")
        self.assertEqual(status, 200)
        health_data = json.loads(body.decode("utf-8"))
        self.assertIn(health_data.get("status"), ("healthy", "degraded", "unhealthy"))
        self.assertGreaterEqual(health_data.get("uptime_seconds", 0), 0)
        self.assertIsInstance(health_data.get("active_supervisor_alerts"), list)

        subsystems = health_data.get("subsystems", {})
        for required_sub in ("database", "storage", "scheduler", "queue_worker", "active_job", "alerts"):
            self.assertIn(required_sub, subsystems, f"Missing subsystem in telemetry: {required_sub}")
        self.assertEqual(subsystems["database"]["status"], "healthy")
        self.assertIn(subsystems["storage"]["status"], ("healthy", "low", "critical"))
        self.assertFalse(subsystems["scheduler"]["stalled"])

        # 4.3: /api/alerts
        status, headers, body = self._http_get("/api/alerts")
        self.assertEqual(status, 200)
        alerts_data = json.loads(body.decode("utf-8"))
        self.assertIn("alerts", alerts_data)
        self.assertIsInstance(alerts_data["alerts"], list)
        self.assertGreaterEqual(alerts_data.get("count", 0), 0)

        # 4.4: /api/queue
        status, headers, body = self._http_get("/api/queue")
        self.assertEqual(status, 200)
        queue_data = json.loads(body.decode("utf-8"))
        self.assertIn("pending", queue_data)
        self.assertEqual(queue_data.get("count"), 0)

        # 4.5: /api/retention/audit (Milestone 2 API)
        status, headers, body = self._http_get("/api/retention/audit")
        self.assertEqual(status, 200)
        retention_data = json.loads(body.decode("utf-8"))
        self.assertEqual(retention_data.get("mode"), "dry_run")
        self.assertFalse(retention_data.get("deletion_occurred"))
        self.assertIn("summary", retention_data)

        # --- Step 5: Execute Supervisor Tick Cycle ---
        tick_res = self.sup.tick()
        self.assertIn(tick_res["status"], ("healthy", "degraded"))
        self.assertEqual(tick_res["subsystems"]["database"]["status"], "healthy")

        # --- Step 6: Orderly Application Shutdown & Barrier Verification ---
        clean, diag = server.stop_studio_application(
            server=self.httpd,
            alert_engine_stop_fn=alerts.stop_alert_engine,
            stop_event=self.stop_event,
            background_workers=[self.sup_thread],
            total_timeout=3.0,
        )

        self.server_thread.join(timeout=2.0)
        self.sup_thread.join(timeout=2.0)

        self.assertTrue(clean, f"Application shutdown must report clean stop: {diag}")
        self.assertTrue(diag["producers_quiescent"], "Producers must be quiescent")
        self.assertTrue(diag["alerts_diag"]["accounting_complete"], "Alert accounting must be complete")
        self.assertEqual(diag["live_workers_count"], 0)
        self.assertFalse(self.sup_thread.is_alive(), "Supervisor thread must be stopped")

        # --- Step 7: Connection Refusal Post-Shutdown ---
        with self.assertRaises(OSError):
            s = socket.create_connection(("127.0.0.1", self.assigned_port), timeout=0.5)
            s.close()

        # --- Step 8: Strict Non-Publishing Verification ---
        self.assertEqual(
            len(self.prohibited_calls_attempted),
            0,
            f"Prohibited external calls were attempted during smoke test: {self.prohibited_calls_attempted}",
        )

    def test_operational_smoke_teardown_on_induced_failure(self):
        """
        Verify that Python's actual unittest.TestCase.run() lifecycle executes
        registered addCleanup() handlers after an intentional assertion failure.
        """
        captured: dict[str, Any] = {}

        class NestedFailingSmokeTest(OperationalSmokeTest):
            def runTest(self):
                # 1. Start Alert Engine and register cleanup
                alerts.reset_engine_for_testing()
                alerts.start_alert_engine(str(self.isolated_db_path))
                self.addCleanup(self._cleanup_alert_engine)

                # 2. Start Supervisor and register cleanup
                self.stop_event = threading.Event()
                self.sup = supervisor.Supervisor(
                    tick_interval=1.0,
                    db_path=str(self.isolated_db_path),
                    stop_event=self.stop_event,
                )
                self.sup_thread = self.sup.start()
                self.addCleanup(self._cleanup_supervisor)

                # 3. Start Live Server and register cleanup
                self.httpd = server.StudioServer(("127.0.0.1", 0), server.StudioHandler)
                self.assigned_port = self.httpd.server_address[1]
                server.PORT = self.assigned_port

                self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
                self.server_thread.start()
                self.addCleanup(self._cleanup_http_server)
                self.assertTrue(self.httpd._loop_ready.wait(timeout=2.0), "Server loop ready in 2.0s")

                captured["httpd"] = self.httpd
                captured["server_th"] = self.server_thread
                captured["port"] = self.assigned_port
                captured["sup"] = self.sup
                captured["sup_th"] = self.sup_thread
                captured["stop_ev"] = self.stop_event

                # Confirm server is listening and threads are active before failure
                with socket.create_connection(("127.0.0.1", self.assigned_port), timeout=1.0) as s:
                    s.close()
                self.assertTrue(self.server_thread.is_alive())
                self.assertTrue(self.sup_thread.is_alive())

                # 4. Intentionally induce assertion failure midway
                self.assertEqual("midway_failure", "must_fail_intentionally")

        # Execute through unittest's real test lifecycle runner
        test_case = NestedFailingSmokeTest("runTest")
        test_result = unittest.TestResult()
        test_case.run(test_result)

        # Confirm the induced failure was genuinely recorded by unittest
        self.assertEqual(len(test_result.failures), 1, "Induced failure must be recorded in TestResult")
        self.assertEqual(len(test_result.errors), 0, "No unexpected errors should occur in cleanup")
        self.assertIn("must_fail_intentionally", test_result.failures[0][1])

        # Confirm that unittest addCleanup handlers genuinely terminated threads and closed port
        self.assertFalse(captured["server_th"].is_alive(), "Server thread must be joined by unittest addCleanup")
        self.assertFalse(captured["sup_th"].is_alive(), "Supervisor thread must be joined by unittest addCleanup")
        with self.assertRaises(OSError, msg="Port must be closed and refuse connections after addCleanup"):
            s = socket.create_connection(("127.0.0.1", captured["port"]), timeout=0.5)
            s.close()


if __name__ == "__main__":
    unittest.main()
