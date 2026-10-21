"""
tests/test_frontend_integration.py — Integration tests for Studio Frontend & Health Telemetry.

Validates:
1. Static asset contents: index.html, style.css, and app.js contain all required
   telemetry markup, diagnostic modal, CSS state classes, and pure functions.
2. /api/health HTTP endpoint response structure, contract conformance, and schema.
3. Security redaction: no secret tokens, credentials, or sensitive host paths exposed.
4. Degraded and unhealthy state reflection via live HTTP responses.
5. Fail-closed error handling and DNS rebinding protection (Host header validation).
6. End-to-end server lifecycle and clean supervisor thread shutdown.
"""

import datetime as dt
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch

import studio.alerts as alerts
import studio.server as server
import studio.supervisor as supervisor


class TestFrontendStaticAssets(unittest.TestCase):
    """Verify frontend static files contain required telemetry widgets, modal, and styles."""

    @classmethod
    def setUpClass(cls):
        cls.web_dir = Path("studio/web")
        cls.index_html = (cls.web_dir / "index.html").read_text(encoding="utf-8")
        cls.style_css = (cls.web_dir / "style.css").read_text(encoding="utf-8")
        cls.app_js = (cls.web_dir / "app.js").read_text(encoding="utf-8")

    def test_index_html_contains_modal_and_host_info(self):
        """index.html must include the diagnostic modal dialog and clickable sidebar host-info."""
        self.assertIn('id="host-info"', self.index_html)
        self.assertIn('id="health-modal"', self.index_html)
        self.assertIn('id="health-modal-body"', self.index_html)
        self.assertIn('id="btn-close-health-modal"', self.index_html)
        self.assertIn('<dialog id="health-modal"', self.index_html)

    def test_style_css_contains_health_states(self):
        """style.css must include CSS rules for all required health indicator states."""
        required_classes = [
            ".status-indicator.degraded",
            ".status-indicator.unhealthy",
            ".status-indicator.stale",
            ".status-indicator.unavailable",
            ".health-pill",
            ".health-pill.healthy",
            ".health-pill.degraded",
            ".health-pill.unhealthy",
            ".health-pill.stale",
            ".health-pill.unavailable",
            ".health-pill.malformed",
            ".subsystem-grid",
            ".subsystem-card",
        ]
        for cls_name in required_classes:
            self.assertIn(cls_name, self.style_css, f"Missing CSS class: {cls_name}")

    def test_app_js_contains_health_telemetry_functions(self):
        """app.js must define telemetry polling, state evaluation, rendering, and modal handlers."""
        required_identifiers = [
            "fetchHealthTelemetry",
            "evaluateHealthState",
            "getHealthSummaryMessage",
            "renderHealthSubsystemCards",
            "openHealthModal",
            "closeHealthModal",
            "renderHealthModal",
            "triggerManualHealthRefresh",
            "updateOverviewHealthWidget",
            "pollStatusInFlight",
            "health-modal-refresh",
            "overview-health-refresh",
        ]
        for identifier in required_identifiers:
            self.assertIn(identifier, self.app_js, f"Missing function/identifier in app.js: {identifier}")


class TestHealthEndpointAndLifecycle(unittest.TestCase):
    """Live HTTP server tests for /api/health and frontend assets."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_studio.db")

        # Initialize SQLite DB
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        conn.commit()
        conn.close()

        alerts.reset_engine_for_testing()
        alerts.start_alert_engine(self.db_path)

        self.stop_event = threading.Event()
        self.sup = supervisor.Supervisor(
            tick_interval=3600.0,
            db_path=self.db_path,
            stop_event=self.stop_event,
        )
        self.sup_thread = self.sup.start()

        # Wire server and supervisor globals
        self.orig_port = server.PORT
        self.orig_sup = server.SUPERVISOR_INSTANCE
        self.orig_global_sup = supervisor._GLOBAL_SUPERVISOR

        server.SUPERVISOR_INSTANCE = self.sup
        supervisor._GLOBAL_SUPERVISOR = self.sup

        self.patch_settings = patch("studio.settings.load_settings", return_value={"enabled": False, "interval_hours": 3, "next_run": 0})
        self.patch_settings.start()

        # Bind test server on loopback with port 0
        self.httpd = server.StudioServer(("127.0.0.1", 0), server.StudioHandler)
        self.assigned_port = self.httpd.server_address[1]
        server.PORT = self.assigned_port

        self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.server_thread.start()
        self.assertTrue(self.httpd._loop_ready.wait(timeout=2.0))

    def tearDown(self):
        # Stop application
        server.stop_studio_application(
            server=self.httpd,
            alert_engine_stop_fn=alerts.stop_alert_engine,
            stop_event=self.stop_event,
            background_workers=[self.sup_thread],
            total_timeout=2.0,
        )
        self.server_thread.join(timeout=2.0)
        self.sup_thread.join(timeout=2.0)

        server.PORT = self.orig_port
        server.SUPERVISOR_INSTANCE = self.orig_sup
        supervisor._GLOBAL_SUPERVISOR = self.orig_global_sup
        if hasattr(self, "patch_settings"):
            self.patch_settings.stop()
        alerts.reset_engine_for_testing()

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _get(self, path: str, host: str = None) -> tuple[int, dict, bytes]:
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

    def test_api_health_contract_and_schema(self):
        """GET /api/health returns HTTP 200 with complete valid telemetry schema."""
        status_code, headers, body = self._get("/api/health")
        self.assertEqual(status_code, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))

        data = json.loads(body.decode("utf-8"))
        self.assertIn("status", data)
        self.assertIn(data["status"], ("healthy", "degraded", "unhealthy"))
        self.assertIn("timestamp", data)
        self.assertIn("uptime_seconds", data)
        self.assertGreaterEqual(data["uptime_seconds"], 0)
        self.assertIn("subsystems", data)
        self.assertIn("active_supervisor_alerts", data)
        self.assertIsInstance(data["active_supervisor_alerts"], list)

        sub = data["subsystems"]
        required_subsystems = ["database", "storage", "scheduler", "queue_worker", "active_job", "alerts"]
        for s in required_subsystems:
            self.assertIn(s, sub, f"Missing subsystem in telemetry: {s}")

        self.assertEqual(sub["database"]["status"], "healthy")
        self.assertIsInstance(sub["database"].get("latency_sec"), (float, int))
        self.assertIn(sub["storage"]["status"], ("healthy", "low", "critical"))
        self.assertIsInstance(sub["storage"].get("free_mb"), (int, float))
        self.assertFalse(sub["scheduler"]["stalled"])
        self.assertTrue(sub["alerts"]["drain_healthy"])

    def test_api_health_security_and_redaction(self):
        """Telemetery response must not leak secrets, bearer tokens, or sensitive credentials."""
        status_code, _, body = self._get("/api/health")
        self.assertEqual(status_code, 200)
        raw_text = body.decode("utf-8").lower()

        forbidden_keys = [
            "client_secret",
            "access_token",
            "refresh_token",
            "bearer",
            "api_key",
            "password",
            "private_key",
        ]
        for forbidden in forbidden_keys:
            self.assertNotIn(f'"{forbidden}"', raw_text, f"Potential credential leak of '{forbidden}' in /api/health")

    def test_api_health_reflects_degraded_storage(self):
        """When disk storage is low (e.g. 750MB free), /api/health reports degraded status."""
        with patch("shutil.disk_usage") as mock_usage:
            # 750MB free (<1GB is low space)
            mock_usage.return_value = shutil._ntuple_diskusage(1000 * 1024 * 1024, 250 * 1024 * 1024, 750 * 1024 * 1024)
            self.sup.tick()

            status_code, _, body = self._get("/api/health")
            self.assertEqual(status_code, 200)
            data = json.loads(body.decode("utf-8"))
            self.assertEqual(data["status"], "degraded")
            self.assertEqual(data["subsystems"]["storage"]["status"], "low")

    def test_api_health_reflects_unhealthy_critical_storage(self):
        """When disk storage is critical (<512MB free), /api/health reports unhealthy status and alert."""
        with patch("shutil.disk_usage") as mock_usage:
            # 300MB free (<512MB is critical)
            mock_usage.return_value = shutil._ntuple_diskusage(1000 * 1024 * 1024, 700 * 1024 * 1024, 300 * 1024 * 1024)
            self.sup.tick()
            alerts.wait_for_drain(timeout=2.0)

            status_code, _, body = self._get("/api/health")
            self.assertEqual(status_code, 200)
            data = json.loads(body.decode("utf-8"))
            self.assertEqual(data["status"], "unhealthy")
            self.assertEqual(data["subsystems"]["storage"]["status"], "critical")
            self.assertIn("supervisor.storage.disk_critical", data["active_supervisor_alerts"])

    def test_api_health_redacts_probe_exceptions_and_sensitive_paths(self):
        """Raw Python exceptions containing sensitive paths/tokens are strictly redacted to safe generic error codes."""
        fake_secret_token = "secret_bearer_token_xyz_998877"
        fake_secret_path = "/Users/admin_secret/.credentials/super_secret.jwt"

        def _exploding_query(*args, **kwargs):
            raise sqlite3.OperationalError(f"unable to open database file: {fake_secret_path} (auth={fake_secret_token})")

        with patch("studio.supervisor.sqlite3.connect", side_effect=_exploding_query):
            self.sup.tick()
            alerts.wait_for_drain(timeout=2.0)

            status_code, _, body = self._get("/api/health")
            self.assertEqual(status_code, 200)
            raw_response = body.decode("utf-8")

            # 1. Assert raw secrets/paths are NEVER exposed in the JSON response
            self.assertNotIn(fake_secret_token, raw_response, "Secret token leaked in /api/health!")
            self.assertNotIn(fake_secret_path, raw_response, "Private filesystem path leaked in /api/health!")

            data = json.loads(raw_response)
            self.assertEqual(data["status"], "unhealthy")
            self.assertEqual(data["subsystems"]["database"]["status"], "unresponsive")
            self.assertEqual(data["subsystems"]["database"]["error"], "error_OperationalError")

        # 2. Outside patch: assert active alert table message and metadata are also strictly redacted
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT fingerprint, message, metadata FROM alerts WHERE resolved_at IS NULL")
            rows = cur.fetchall()
            fps = [r[0] for r in rows]
            self.assertIn("supervisor.db.unresponsive", fps)
            alert_msg = [r[1] for r in rows if r[0] == "supervisor.db.unresponsive"][0]
            alert_meta = [r[2] for r in rows if r[0] == "supervisor.db.unresponsive"][0]
            self.assertNotIn(fake_secret_token, alert_msg)
            self.assertNotIn(fake_secret_path, alert_msg)
            self.assertNotIn(fake_secret_token, alert_meta)
            self.assertNotIn(fake_secret_path, alert_meta)

    def test_static_files_served_successfully(self):
        """Static assets index.html, style.css, and app.js are served with 200 OK."""
        for path in ("/", "/index.html", "/style.css", "/app.js"):
            code, headers, body = self._get(path)
            self.assertEqual(code, 200, f"Failed serving {path}")
            self.assertGreater(len(body), 0, f"Empty body for {path}")

    def test_dns_rebinding_rejected_with_403(self):
        """Requests with unauthorized Host header are rejected fail-closed with HTTP 403."""
        code, _, _ = self._get("/api/health", host="attacker.example.com")
        self.assertEqual(code, 403)


if __name__ == "__main__":
    unittest.main()
