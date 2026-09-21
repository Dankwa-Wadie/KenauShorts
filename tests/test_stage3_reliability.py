"""
tests/test_stage3_reliability.py — Tests for Stage 3 Reliability Hardening:
1. Multi-Instance Server Collision Prevention (.server.lock and allow_reuse_address=False)
2. False Success Prevention on Zero Picks / Discovery Failure (summary status handling)
3. Cancellation vs Completion Race Window Mitigation (atomic GUARD status transition)
4. PID Recycling Protection (GetProcessTimes validation in terminate_process_tree and restart recovery)
"""

import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch, MagicMock

import studio.server as server
import studio.store as store


class Stage3ReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / "out").mkdir()
        (cls.root / "assets").mkdir()
        cls.logs_dir = cls.root / "logs"
        cls.logs_dir.mkdir()
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
            DB=cls.root / "test_stage3.sqlite3",
        )
        cls.store_patches.start()

        with store.connect():
            pass

    @classmethod
    def tearDownClass(cls):
        server.STOP_EVENT.set()
        server.QUEUE_EVENT.set()
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

    # ----------------------------------------------------------------------
    # 1. Multi-Instance Server Collision Prevention
    # ----------------------------------------------------------------------

    def test_server_lock_mutual_exclusion(self):
        """Test that server_lock prevents a second server instance from entering."""
        with server.server_lock():
            # Second attempt must raise RuntimeError
            with self.assertRaises(RuntimeError) as cm:
                with server.server_lock():
                    pass
            self.assertIn("already running", str(cm.exception).lower())

        # Once released, locking should succeed again
        with server.server_lock():
            pass

    def test_studio_server_rejects_address_reuse(self):
        """Test that StudioServer has allow_reuse_address=False and rejects duplicate bind."""
        self.assertFalse(server.StudioServer.allow_reuse_address)
        s1 = server.StudioServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = s1.server_address[1]
        try:
            with self.assertRaises(OSError):
                server.StudioServer(("127.0.0.1", port), BaseHTTPRequestHandler)
        finally:
            s1.server_close()

    def test_run_server_does_not_recover_jobs_on_collision(self):
        """Test that run_server aborts before recover_interrupted_jobs if lock fails."""
        with patch.object(server, "recover_interrupted_jobs") as mock_recover:
            with server.server_lock():
                # Server is already locked; run_server must catch RuntimeError and exit cleanly
                server.run_server(port=8799)
                mock_recover.assert_not_called()

    # ----------------------------------------------------------------------
    # 2. False Success Prevention on Zero Picks / Discovery Failure
    # ----------------------------------------------------------------------

    def test_run_job_process_failed_summary_marks_failed(self):
        """Test that KENAU_SUMMARY {'status': 'failed'} with exit code 0 sets job to failed."""
        script = (
            "import json\n"
            "print('KENAU_PROGRESS ' + json.dumps({'stage': 'Finding stories'}))\n"
            "print('KENAU_SUMMARY ' + json.dumps({'status': 'failed', 'message': 'No candidate passed the editorial filter.'}))\n"
        )
        job = {
            "id": "test_job_failed_summary",
            "action": "preview",
            "status": "running",
            "stage": "Starting",
            "created_at": store.now(),
        }
        store.put("jobs", job["id"], job)
        command = [sys.executable, "-c", script]

        server.run_job_process(job, command)

        res = store.get("jobs", job["id"])
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["stage"], "Failed: No candidate passed the editorial filter.")

    def test_run_job_process_idle_summary_marks_idle(self):
        """Test that KENAU_SUMMARY {'status': 'idle'} with exit code 0 sets job to idle."""
        script = (
            "import json\n"
            "print('KENAU_PROGRESS ' + json.dumps({'stage': 'Scraping'}))\n"
            "print('KENAU_SUMMARY ' + json.dumps({'status': 'idle', 'message': 'No new candidates to process.'}))\n"
        )
        job = {
            "id": "test_job_idle_summary",
            "action": "preview",
            "status": "running",
            "stage": "Starting",
            "created_at": store.now(),
        }
        store.put("jobs", job["id"], job)
        command = [sys.executable, "-c", script]

        server.run_job_process(job, command)

        res = store.get("jobs", job["id"])
        self.assertEqual(res["status"], "idle")
        self.assertEqual(res["stage"], "Idle: No new candidates to process.")

    def test_run_job_process_completed_summary_marks_completed(self):
        """Test that KENAU_SUMMARY {'status': 'completed'} sets job to completed."""
        script = (
            "import json\n"
            "print('KENAU_PROGRESS ' + json.dumps({'stage': 'Rendering'}))\n"
            "print('KENAU_SUMMARY ' + json.dumps({'status': 'completed', 'video': 'out/video.mp4'}))\n"
        )
        job = {
            "id": "test_job_completed_summary",
            "action": "preview",
            "status": "running",
            "stage": "Starting",
            "created_at": store.now(),
        }
        store.put("jobs", job["id"], job)
        command = [sys.executable, "-c", script]

        server.run_job_process(job, command)

        res = store.get("jobs", job["id"])
        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["stage"], "Completed")
        self.assertEqual(res.get("target_file"), "out/video.mp4")

    def test_run_job_process_nonzero_exit_marks_failed(self):
        """Test that an unhandled crash (exit 1) without summary sets job to failed."""
        script = "import sys; sys.exit(1)"
        job = {
            "id": "test_job_crash",
            "action": "preview",
            "status": "running",
            "stage": "Starting",
            "created_at": store.now(),
        }
        store.put("jobs", job["id"], job)
        command = [sys.executable, "-c", script]

        server.run_job_process(job, command)

        res = store.get("jobs", job["id"])
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["stage"], "Failed")

    # ----------------------------------------------------------------------
    # 3. Cancellation vs Completion Race Window Mitigation
    # ----------------------------------------------------------------------

    def test_cancellation_during_job_run_preserves_cancelled(self):
        """Test that cancelling a job while running persists cancelled status."""
        script = (
            "import time\n"
            "time.sleep(0.5)\n"
        )
        job = {
            "id": "test_cancel_race",
            "action": "preview",
            "status": "running",
            "stage": "Starting",
            "created_at": store.now(),
        }
        store.put("jobs", job["id"], job)
        command = [sys.executable, "-c", script]

        t = threading.Thread(target=server.run_job_process, args=(job, command))
        t.start()

        # Wait until process has spawned and registered pid
        for _ in range(50):
            with server.GUARD:
                if server.ACTIVE_PROC and job.get("pid"):
                    break
            time.sleep(0.05)

        cancel_res = server.cancel_job(job["id"])
        self.assertEqual(cancel_res["status"], "cancelled")

        t.join(timeout=3)
        res = store.get("jobs", job["id"])
        self.assertEqual(res["status"], "cancelled")

    def test_cancel_on_completed_or_idle_job_returns_existing_status(self):
        """Test that calling cancel_job on already completed or idle job does not alter status."""
        completed_job = {
            "id": "job_already_completed",
            "status": "completed",
            "stage": "Completed",
            "created_at": store.now(),
        }
        store.put("jobs", completed_job["id"], completed_job)
        res_comp = server.cancel_job("job_already_completed")
        self.assertEqual(res_comp["status"], "completed")
        self.assertIn("already completed", res_comp["message"].lower())

        idle_job = {
            "id": "job_already_idle",
            "status": "idle",
            "stage": "Idle",
            "created_at": store.now(),
        }
        store.put("jobs", idle_job["id"], idle_job)
        res_idle = server.cancel_job("job_already_idle")
        self.assertEqual(res_idle["status"], "idle")
        self.assertIn("already idle", res_idle["message"].lower())

    # ----------------------------------------------------------------------
    # 4. PID Recycling Protection
    # ----------------------------------------------------------------------

    def test_get_process_creation_time_returns_valid_timestamp(self):
        """Test that get_process_creation_time returns a positive timestamp for alive process."""
        ts = server.get_process_creation_time(os.getpid())
        if sys.platform == "win32":
            self.assertIsInstance(ts, int)
            self.assertGreater(ts, 0)
        self.assertIsNone(server.get_process_creation_time(9999999))
        self.assertIsNone(server.get_process_creation_time(-1))

    def test_terminate_process_tree_refuses_when_creation_time_mismatched(self):
        """Test that terminate_process_tree refuses to terminate if creation time does not match."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            real_ts = server.get_process_creation_time(proc.pid)
            if real_ts is not None:
                # Provide mismatched creation time
                fake_ts = real_ts + 100000000
                killed = server.terminate_process_tree(proc.pid, expected_created_at=fake_ts)
                self.assertFalse(killed)
                # Process must still be alive
                self.assertIsNone(proc.poll())

                # Now provide matching creation time
                killed_real = server.terminate_process_tree(proc.pid, expected_created_at=real_ts)
                self.assertTrue(killed_real)
                self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_recover_interrupted_jobs_with_mismatched_creation_time_leaves_process_alive(self):
        """Test that restart recovery does not terminate a recycled PID."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            real_ts = server.get_process_creation_time(proc.pid)
            fake_ts = (real_ts or 1000) + 123456789

            interrupted_id = "job_recycled_pid"
            store.put("jobs", interrupted_id, {
                "id": interrupted_id,
                "status": "running",
                "pid": proc.pid,
                "pid_created_at": fake_ts,
                "created_at": store.now(),
            })

            server.recover_interrupted_jobs()

            # Job status in store should be reconciled to interrupted
            rec = store.get("jobs", interrupted_id)
            self.assertEqual(rec["status"], "interrupted")

            # But the process must NOT have been killed because timestamps did not match!
            if sys.platform == "win32":
                self.assertIsNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
