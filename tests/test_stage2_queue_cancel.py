"""
tests/test_stage2_queue_cancel.py — Tests for Stage 2 Process Reliability,
Cancellation, and Persistent Serial Queue in KenauShorts.
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
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer

import studio.server as server
import studio.store as store


class Stage2ProcessAndQueueTests(unittest.TestCase):
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
        (web_dir / "index.html").write_text("<html>ok</html>")

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
            DB=cls.root / "test_stage2.sqlite3",
        )
        cls.store_patches.start()

        # Initialize tables
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
        # Clear jobs table and active state between test cases
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
    # 1. Process Primitives: Liveness, Validation & Tree Termination
    # ----------------------------------------------------------------------

    def test_process_liveness_checks(self):
        current_pid = os.getpid()
        self.assertTrue(server.is_process_alive(current_pid))
        self.assertFalse(server.is_process_alive(-1))
        self.assertFalse(server.is_process_alive(0))
        self.assertFalse(server.is_process_alive(9999999))

    def test_is_python_process_identifies_python(self):
        current_pid = os.getpid()
        self.assertTrue(server.is_python_process(current_pid))
        self.assertFalse(server.is_python_process(9999999))

    def test_terminate_process_tree_kills_child_process(self):
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(15)"],
            cwd=self.root,
        )
        self.assertTrue(server.is_process_alive(p.pid))
        terminated = server.terminate_process_tree(p.pid)
        try:
            p.wait(timeout=2)
        except Exception:
            pass
        self.assertTrue(terminated)
        self.assertFalse(server.is_process_alive(p.pid))

    def test_terminate_process_tree_refuses_own_pid_and_invalid(self):
        self.assertFalse(server.terminate_process_tree(os.getpid()))
        self.assertFalse(server.terminate_process_tree(-1))
        self.assertFalse(server.terminate_process_tree(0))

    # ----------------------------------------------------------------------
    # 2. FIFO Serial Queue Execution (Ordering & Non-Overlapping)
    # ----------------------------------------------------------------------

    def test_serial_fifo_queue_processes_jobs_sequentially(self):
        # We start two quick jobs that log output
        job1_cmd = [sys.executable, "-u", "-c", "import time; print('START 1'); time.sleep(0.3); print('END 1')"]
        job2_cmd = [sys.executable, "-u", "-c", "import time; print('START 2'); time.sleep(0.3); print('END 2')"]

        with patch.object(server, "start_job", side_effect=server.start_job):
            # Enqueue Job 1
            j1 = {
                "id": "j1_fifo",
                "action": "preview",
                "command": job1_cmd,
                "status": "pending",
                "stage": "Queued",
                "created_at": "2026-09-21T07:00:00.000000+00:00",
                "automatic": False,
                "log": "",
            }
            # Enqueue Job 2 with a slightly later timestamp
            j2 = {
                "id": "j2_fifo",
                "action": "preview",
                "command": job2_cmd,
                "status": "pending",
                "stage": "Queued",
                "created_at": "2026-09-21T07:00:01.000000+00:00",
                "automatic": False,
                "log": "",
            }

            store.put("jobs", j1["id"], j1)
            store.put("jobs", j2["id"], j2)
            server.QUEUE_EVENT.set()

            # Wait for both jobs to finish (up to 5s)
            start_wait = time.time()
            while time.time() - start_wait < 5.0:
                rec1 = store.get("jobs", "j1_fifo")
                rec2 = store.get("jobs", "j2_fifo")
                if (rec1 and rec1.get("status") == "completed" and
                        rec2 and rec2.get("status") == "completed"):
                    break
                time.sleep(0.1)

            rec1 = store.get("jobs", "j1_fifo")
            rec2 = store.get("jobs", "j2_fifo")
            self.assertEqual(rec1.get("status"), "completed")
            self.assertEqual(rec2.get("status"), "completed")
            self.assertIn("START 1", rec1.get("log", ""))
            self.assertIn("START 2", rec2.get("log", ""))

    def test_api_queue_and_status_reporting(self):
        # Enqueue dummy pending jobs directly into store
        store.put("jobs", "p1", {
            "id": "p1", "action": "preview", "status": "pending",
            "stage": "Queued", "created_at": "2026-09-21T07:01:00+00:00",
        })
        store.put("jobs", "p2", {
            "id": "p2", "action": "preview", "status": "pending",
            "stage": "Queued", "created_at": "2026-09-21T07:01:05+00:00",
        })

        # Test GET /api/queue
        status, data = self.request("GET", "/api/queue")
        self.assertEqual(status, 200)
        body = json.loads(data)
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["pending"][0]["id"], "p1")
        self.assertEqual(body["pending"][1]["id"], "p2")

        # Test GET /api/status includes queue_count
        status, data = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        body = json.loads(data)
        self.assertEqual(body["queue_count"], 2)
        self.assertEqual(body["queued_jobs"], ["p1", "p2"])

    # ----------------------------------------------------------------------
    # 3. Active Job Cancellation & Process Termination
    # ----------------------------------------------------------------------

    def test_cancel_active_running_job(self):
        # Spawn a long sleep job
        long_cmd = [sys.executable, "-u", "-c", "import time; print('RUNNING'); time.sleep(30)"]
        job_id = "job_to_cancel_active"
        job = {
            "id": job_id,
            "action": "preview",
            "command": long_cmd,
            "status": "pending",
            "stage": "Queued",
            "created_at": store.now(),
            "automatic": False,
            "log": "",
        }
        store.put("jobs", job_id, job)
        server.QUEUE_EVENT.set()

        # Wait until the job starts running and gets a PID
        pid = None
        for _ in range(50):
            with server.GUARD:
                if server.ACTIVE_JOB and server.ACTIVE_JOB["id"] == job_id and server.ACTIVE_JOB.get("pid"):
                    pid = server.ACTIVE_JOB["pid"]
                    break
            time.sleep(0.1)

        self.assertIsNotNone(pid, "Job did not start in time")
        self.assertTrue(server.is_process_alive(pid))

        # Cancel via POST /api/job/cancel
        status, data = self.post_json("/api/job/cancel", {"id": job_id})
        self.assertEqual(status, 200)
        resp_data = json.loads(data)
        self.assertEqual(resp_data["status"], "cancelled")

        # Verify the process was terminated
        time.sleep(0.3)
        self.assertFalse(server.is_process_alive(pid))

        # Check job in store
        rec = store.get("jobs", job_id)
        self.assertEqual(rec["status"], "cancelled")
        self.assertIn("Cancelled", rec["stage"])

        # Check ACTIVE_JOB is cleared
        with server.GUARD:
            self.assertIsNone(server.ACTIVE_JOB)

    def test_cancel_idle_job_returns_idle_message(self):
        status, data = self.post_json("/api/job/cancel", {})
        self.assertEqual(status, 200)
        body = json.loads(data)
        self.assertEqual(body["status"], "idle")

    # ----------------------------------------------------------------------
    # 4. Pending Job Cancellation in Queue
    # ----------------------------------------------------------------------

    def test_cancel_pending_job_in_queue_skips_execution(self):
        # Put a blocker job running and a pending job in store
        blocker_cmd = [sys.executable, "-u", "-c", "import time; time.sleep(0.5)"]
        blocker_id = "blocker_job"
        store.put("jobs", blocker_id, {
            "id": blocker_id, "action": "preview", "command": blocker_cmd,
            "status": "pending", "stage": "Queued", "created_at": "2026-09-21T07:10:00+00:00",
        })

        pending_id = "pending_job_to_cancel"
        flag_file = self.root / "pending_executed.txt"
        pending_cmd = [sys.executable, "-u", "-c", f"from pathlib import Path; Path(r'{flag_file}').write_text('ran')"]
        store.put("jobs", pending_id, {
            "id": pending_id, "action": "preview", "command": pending_cmd,
            "status": "pending", "stage": "Queued", "created_at": "2026-09-21T07:10:05+00:00",
        })

        # Cancel the pending job BEFORE the blocker finishes
        status, data = self.post_json("/api/job/cancel", {"id": pending_id})
        self.assertEqual(status, 200)
        body = json.loads(data)
        self.assertEqual(body["status"], "cancelled")

        # Confirm store reflects cancelled
        rec = store.get("jobs", pending_id)
        self.assertEqual(rec["status"], "cancelled")
        self.assertEqual(rec["stage"], "Cancelled from queue")

        # Wake worker so blocker runs and finishes
        server.QUEUE_EVENT.set()
        time.sleep(1.0)

        # Confirm pending job was NOT executed
        self.assertFalse(flag_file.exists(), "Cancelled pending job should not have executed!")

    # ----------------------------------------------------------------------
    # 5. Output Artifact Cleanup on Cancellation
    # ----------------------------------------------------------------------

    def test_cleanup_job_artifacts_on_cancel(self):
        fake_video = self.root / "out" / "short_test_cancel.mp4"
        fake_video.write_bytes(b"partial render data")
        self.assertTrue(fake_video.is_file())

        job = {
            "id": "job_artifact_cleanup",
            "status": "cancelled",
            "target_file": str(fake_video),
        }
        server.cleanup_job_artifacts(job)
        self.assertFalse(fake_video.is_file(), "Cancelled video artifact should be deleted")

    # ----------------------------------------------------------------------
    # 6. Server Restart Recovery
    # ----------------------------------------------------------------------

    def test_recover_interrupted_jobs_kills_orphan_and_updates_status(self):
        # Spawn an orphan process simulating a killed server
        orphan = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            cwd=self.root,
        )
        self.assertTrue(server.is_process_alive(orphan.pid))

        interrupted_id = "job_interrupted_crash"
        store.put("jobs", interrupted_id, {
            "id": interrupted_id,
            "action": "run",
            "status": "running",
            "stage": "Rendering video card",
            "pid": orphan.pid,
            "created_at": store.now(),
        })

        # Pending job that should NOT be marked interrupted
        surviving_pending_id = "job_surviving_pending"
        store.put("jobs", surviving_pending_id, {
            "id": surviving_pending_id,
            "action": "preview",
            "status": "pending",
            "stage": "Queued",
            "created_at": store.now(),
        })

        # Run restart recovery
        server.recover_interrupted_jobs()
        try:
            orphan.wait(timeout=2)
        except Exception:
            pass

        # The orphan process should be terminated
        self.assertFalse(server.is_process_alive(orphan.pid))

        # The running job should be marked interrupted
        rec = store.get("jobs", interrupted_id)
        self.assertEqual(rec["status"], "interrupted")
        self.assertIn("Interrupted", rec["stage"])

        # The pending job should still be pending
        pending_rec = store.get("jobs", surviving_pending_id)
        self.assertEqual(pending_rec["status"], "pending")


if __name__ == "__main__":
    unittest.main()
