"""
tests/test_stage4_pipeline.py — Comprehensive tests for Stage 4: Pipeline & Job Management:
1. Persistent Job Execution Metadata (started_at, finished_at, duration_seconds)
2. Pipeline Stage Timeline Progression & Capping (dedup, cap at 50)
3. Structured Failure Diagnostics (4-tier priority, secret redaction)
4. Persistent Job History API (/api/jobs filtering, pagination, log truncation)
5. Retry of Eligible Terminal Jobs (linkage, immutability, command reconstruction, rejection of active/completed)
6. Queue Management Visibility & Cancellation by ID
7. Render Job Progress Reporting (worker emit_progress and emit_summary)
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from http.server import ThreadingHTTPServer

import studio.server as server
import studio.store as store


class Stage4PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / "out").mkdir()
        (cls.root / "assets").mkdir()
        cls.logs_dir = cls.root / "logs"
        cls.logs_dir.mkdir()
        (cls.root / "config.json").write_text("{}", encoding="utf-8")
        web_dir = cls.root / "studio" / "web"
        web_dir.mkdir(parents=True)
        (web_dir / "index.html").write_text("<html>ok</html>", encoding="utf-8")

        cls.server_patches = patch.multiple(
            server,
            ROOT=cls.root,
            WEB_DIR=web_dir,
            LOGS_DIR=cls.logs_dir,
        )
        cls.server_patches.start()

        cls.store_patches = patch.multiple(
            store,
            ROOT=cls.root,
            DB=cls.root / "test_stage4.sqlite3",
        )
        cls.store_patches.start()

        with store.connect():
            pass

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.StudioHandler)
        cls.port = cls.httpd.server_address[1]
        cls.port_patch = patch.object(server, "PORT", cls.port)
        cls.port_patch.start()

        cls.http_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.http_thread.start()

        server.ensure_queue_worker()

    @classmethod
    def tearDownClass(cls):
        server.STOP_EVENT.set()
        server.QUEUE_EVENT.set()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.http_thread.join()
        cls.port_patch.stop()
        cls.store_patches.stop()
        cls.server_patches.stop()
        cls.tmp.cleanup()

    def setUp(self):
        with server.GUARD:
            server.ACTIVE_JOB = None
            if server.ACTIVE_PROC and server.ACTIVE_PROC.poll() is None:
                try:
                    server.ACTIVE_PROC.kill()
                except Exception:
                    pass
            server.ACTIVE_PROC = None

        if store.DB.exists():
            with store.connect() as db:
                db.execute("DELETE FROM jobs")

    def tearDown(self):
        with server.GUARD:
            if server.ACTIVE_PROC and server.ACTIVE_PROC.poll() is None:
                try:
                    server.ACTIVE_PROC.kill()
                except Exception:
                    pass
            server.ACTIVE_JOB = None
            server.ACTIVE_PROC = None

    def request(self, method: str, path: str, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        req_headers = {"Host": f"127.0.0.1:{self.port}"}
        if headers:
            req_headers.update(headers)
        conn.request(method, path, body, req_headers)
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
        conn.close()
        return status, data

    def post_json(self, path: str, payload: dict):
        return self.request(
            "POST",
            path,
            body=json.dumps(payload),
            headers={
                "Origin": f"http://127.0.0.1:{self.port}",
                "X-Studio-Token": server.CSRF,
                "Content-Type": "application/json",
            },
        )

    # ----------------------------------------------------------------------
    # 1. Persistent Job Execution Metadata
    # ----------------------------------------------------------------------

    def test_compute_duration_seconds(self):
        """Test execution duration calculation helper with various inputs."""
        # 10.5 seconds difference
        t1 = "2026-09-25T12:00:00.000000"
        t2 = "2026-09-25T12:00:10.500000"
        self.assertEqual(server.compute_duration_seconds(t1, t2), 10.5)

        # None input returns None
        self.assertIsNone(server.compute_duration_seconds(None, t2))
        self.assertIsNone(server.compute_duration_seconds(t1, None))

        # Invalid format returns None
        self.assertIsNone(server.compute_duration_seconds("invalid", t2))

        # End earlier than start returns 0.0
        self.assertEqual(server.compute_duration_seconds(t2, t1), 0.0)

    def test_job_initial_metadata(self):
        """Job created by start_job has initial timing and empty error."""
        with patch.object(server, "ensure_queue_worker"):
            job = server.start_job("preview")
            self.assertIsNotNone(job["created_at"])
            self.assertIsNone(job["started_at"])
            self.assertIsNone(job["finished_at"])
            self.assertIsNone(job["duration_seconds"])
            self.assertEqual(job["error"], "")
            self.assertEqual(len(job["stages"]), 1)
            self.assertEqual(job["stages"][0]["stage"], "Queued")

    # ----------------------------------------------------------------------
    # 2. Pipeline Stage Timeline Progression & Capping
    # ----------------------------------------------------------------------

    def test_append_job_stage_deduplication(self):
        """Consecutive identical stage updates update timestamp instead of creating duplicate."""
        job = {"stages": [{"stage": "Queued", "at": "2026-09-25T10:00:00"}]}
        server.append_job_stage(job, "Downloading...", "2026-09-25T10:01:00")
        self.assertEqual(len(job["stages"]), 2)
        self.assertEqual(job["stages"][-1]["stage"], "Downloading...")
        self.assertEqual(job["stages"][-1]["at"], "2026-09-25T10:01:00")

        # Append same stage name again
        server.append_job_stage(job, "Downloading...", "2026-09-25T10:01:05")
        self.assertEqual(len(job["stages"]), 2)
        self.assertEqual(job["stages"][-1]["at"], "2026-09-25T10:01:05")

    def test_append_job_stage_capping_at_50(self):
        """Stage timeline is capped at 50 events."""
        job = {"stages": []}
        for i in range(60):
            server.append_job_stage(job, f"Stage {i}", f"2026-09-25T10:00:{i:02d}")
        self.assertEqual(len(job["stages"]), 50)
        self.assertEqual(job["stages"][-1]["stage"], "Stage 59")
        self.assertEqual(job["stages"][0]["stage"], "Stage 10")

    # ----------------------------------------------------------------------
    # 3. Structured Failure Diagnostics & Secret Redaction
    # ----------------------------------------------------------------------

    def test_extract_diagnostic_error_summary_message(self):
        """Priority 1: KENAU_SUMMARY message extraction."""
        log = (
            "Some normal engine logs...\n"
            'KENAU_SUMMARY: {"status": "failed", "message": "No CC-BY candidates found matching score threshold."}\n'
            "End of run.\n"
        )
        diag = server.extract_diagnostic_error(log, 1)
        self.assertEqual(diag, "No CC-BY candidates found matching score threshold.")

    def test_extract_diagnostic_error_exception_traceback(self):
        """Priority 2: Traceback exception line extraction."""
        log = (
            "Starting agent...\n"
            "Traceback (most recent call last):\n"
            '  File "core/agent.py", line 42, in <module>\n'
            "ValueError: Free disk space below 512 MB\n"
        )
        diag = server.extract_diagnostic_error(log, 1)
        self.assertEqual(diag, "ValueError: Free disk space below 512 MB")

    def test_extract_diagnostic_error_nonzero_exit(self):
        """Priority 3: Non-zero exit diagnostic if no exception or summary found."""
        log = "Process terminated abruptly by system supervisor.\n"
        diag = server.extract_diagnostic_error(log, 42)
        self.assertEqual(diag, "Process exited with code 42")

    def test_extract_diagnostic_secret_redaction(self):
        """Secrets like Google/OpenAI keys or tokens are redacted in error messages."""
        log = (
            "Traceback (most recent call last):\n"
            "RuntimeError: Failed request using AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P and Bearer ya29.a0AfH6SMDI...\n"
        )
        diag = server.extract_diagnostic_error(log, 1)
        self.assertNotIn("AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P", diag)
        self.assertIn("[REDACTED_KEY]", diag)
        self.assertNotIn("ya29.a0AfH6SMDI", diag)
        self.assertIn("[REDACTED_TOKEN]", diag)

    # ----------------------------------------------------------------------
    # 4. Persistent Job History API (GET /api/jobs)
    # ----------------------------------------------------------------------

    def test_api_jobs_history_list_and_filter(self):
        """GET /api/jobs returns ordered lightweight jobs with status filtering."""
        # Insert 3 jobs directly
        j1 = {"id": "job1", "status": "completed", "action": "render", "created_at": "2026-09-25T10:00:00", "log": "x" * 1000}
        j2 = {"id": "job2", "status": "failed", "action": "run", "created_at": "2026-09-25T10:01:00", "log": "short log"}
        j3 = {"id": "job3", "status": "cancelled", "action": "preview", "created_at": "2026-09-25T10:02:00", "log": ""}

        store.put("jobs", "job1", j1)
        store.put("jobs", "job2", j2)
        store.put("jobs", "job3", j3)

        # GET /api/jobs (all)
        code, body = self.request("GET", "/api/jobs?limit=10")
        self.assertEqual(code, 200)
        jobs = json.loads(body)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(jobs[0]["id"], "job3")  # Reverse rowid order
        # Verify log truncation
        job1_res = next(j for j in jobs if j["id"] == "job1")
        self.assertEqual(len(job1_res["log"]), 500)

        # Filter by failed
        code, body = self.request("GET", "/api/jobs?status=failed")
        self.assertEqual(code, 200)
        failed_jobs = json.loads(body)
        self.assertEqual(len(failed_jobs), 1)
        self.assertEqual(failed_jobs[0]["id"], "job2")

        # Invalid status filter
        code, body = self.request("GET", "/api/jobs?status=nonexistent")
        self.assertEqual(code, 400)
        self.assertIn("Invalid status filter", json.loads(body)["error"])

    # ----------------------------------------------------------------------
    # 5. Retry of Eligible Terminal Jobs
    # ----------------------------------------------------------------------

    def test_retry_failed_job_success(self):
        """Retrying a failed job enqueues a new job, links them, and preserves history."""
        failed_job = {
            "id": "failed123",
            "action": "preview",
            "command": [sys.executable, "-u", "-m", "core.agent", "--dry-run"],
            "key": "",
            "extra": {},
            "status": "failed",
            "stage": "Failed with exit code 1",
            "stages": [{"stage": "Queued", "at": "2026-09-25T10:00:00"}, {"stage": "Failed", "at": "2026-09-25T10:00:05"}],
            "created_at": "2026-09-25T10:00:00",
            "started_at": "2026-09-25T10:00:01",
            "finished_at": "2026-09-25T10:00:05",
            "duration_seconds": 4.0,
            "error": "Simulated failure",
            "log": "Simulated error log",
        }
        store.put("jobs", "failed123", failed_job)

        with patch.object(server, "ensure_queue_worker"):
            code, body = self.post_json("/api/job/retry", {"id": "failed123"})
            self.assertEqual(code, 200)
            res = json.loads(body)
            new_id = res["id"]
            self.assertNotEqual(new_id, "failed123")
            self.assertEqual(res["retry_of"], "failed123")
            self.assertEqual(res["status"], "pending")
            self.assertEqual(res["action"], "preview")

            # Check original job has retried_by link and remains unchanged in status
            orig = store.get("jobs", "failed123")
            self.assertEqual(orig["status"], "failed")
            self.assertEqual(orig["retried_by"], new_id)
            self.assertEqual(orig["duration_seconds"], 4.0)

    def test_retry_ineligible_jobs_rejected(self):
        """Retrying running, pending, completed, or nonexistent jobs raises error."""
        # 1. Nonexistent
        code, body = self.post_json("/api/job/retry", {"id": "ghost_job"})
        self.assertEqual(code, 400)
        self.assertIn("not found", json.loads(body)["error"].lower())

        # 2. Completed job
        store.put("jobs", "done_job", {"id": "done_job", "status": "completed", "action": "run"})
        code, body = self.post_json("/api/job/retry", {"id": "done_job"})
        self.assertEqual(code, 400)
        self.assertIn("already succeeded", json.loads(body)["error"].lower())

        # 3. Active running job
        store.put("jobs", "running_job", {"id": "running_job", "status": "running", "action": "run"})
        code, body = self.post_json("/api/job/retry", {"id": "running_job"})
        self.assertEqual(code, 400)
        self.assertIn("must be failed", json.loads(body)["error"].lower())

    def test_retry_duplicate_active_prevented(self):
        """Retrying a job whose retry is already pending or running is rejected."""
        store.put("jobs", "parent_job", {"id": "parent_job", "status": "failed", "action": "preview", "retried_by": "child_job"})
        store.put("jobs", "child_job", {"id": "child_job", "status": "pending", "action": "preview", "retry_of": "parent_job"})

        code, body = self.post_json("/api/job/retry", {"id": "parent_job"})
        self.assertEqual(code, 400)
        self.assertIn("already queued or running", json.loads(body)["error"].lower())

    # ----------------------------------------------------------------------
    # 6. Queue Management Visibility & Cancellation by ID
    # ----------------------------------------------------------------------

    def test_cancel_pending_job_by_id(self):
        """Cancelling a queued pending job by its ID leaves other jobs intact."""
        with server.GUARD:
            server.ACTIVE_JOB = {"id": "busy_dummy", "status": "running"}
        try:
            j1 = server.start_job("preview")
            j2 = server.start_job("preview")
            j3 = server.start_job("preview")

            # Cancel j2 specifically
            code, body = self.post_json("/api/job/cancel", {"id": j2["id"]})
            self.assertEqual(code, 200)
            res = json.loads(body)
            self.assertEqual(res["status"], "cancelled")
            self.assertEqual(res["id"], j2["id"])

            # Verify j2 is cancelled in store
            record_j2 = store.get("jobs", j2["id"])
            self.assertEqual(record_j2["status"], "cancelled")
            self.assertIn("Cancelled", record_j2["stage"])

            # Verify j1 and j3 are still pending
            self.assertEqual(store.get("jobs", j1["id"])["status"], "pending")
            self.assertEqual(store.get("jobs", j3["id"])["status"], "pending")
        finally:
            with server.GUARD:
                server.ACTIVE_JOB = None

    # ----------------------------------------------------------------------
    # 7. Worker Render/Upload Progress Reporting
    # ----------------------------------------------------------------------

    def test_worker_progress_reporting(self):
        """Worker emits progress and summary stages during render action."""
        import studio.worker as worker

        mock_draft = {
            "id": "draft_test_1",
            "title": "Test Title",
            "headline": "Test Headline",
            "channel_title": "Test Channel",
            "source_clip": "test_clip.mp4",
            "style_preset": "classic_blue",
            "status": "ready",
        }

        mock_rendered = {
            "video": "out/test_rendered.mp4",
            "poster": "out/test_poster.jpg",
            "aspect": "9:16",
        }

        with patch("studio.store.get", return_value=mock_draft), \
             patch("studio.store.put") as mock_put, \
             patch("core.agent.emit_progress") as mock_progress, \
             patch("core.agent.emit_summary") as mock_summary, \
             patch("core.render.render", return_value=mock_rendered), \
             patch("studio.worker.render.render", return_value=mock_rendered), \
             patch("pathlib.Path.is_file", return_value=True):

            worker.work("render", "draft_test_1")

            # Check that emit_progress was called with meaningful stages
            progress_stages = [call[0][0] for call in mock_progress.call_args_list]
            self.assertIn("Preparing render", progress_stages)
            self.assertTrue(any("Applying style preset" in s for s in progress_stages))
            self.assertIn("Compositing video card", progress_stages)

            # Check summary was emitted
            mock_summary.assert_called_once()
            summary_arg = mock_summary.call_args[0][0]
            self.assertEqual(summary_arg["status"], "completed")
            self.assertEqual(summary_arg["message"], "Render completed successfully")
            self.assertTrue(summary_arg["video"].endswith(".mp4"))


if __name__ == "__main__":
    unittest.main()
