"""
tests/test_stage7_phase2.py — Stage 7 Phase 2 Tests:
1. Subprocess lifecycle, watchdog, timeout, termination, and artifact cleanup.
2. Worker interruption recovery (idempotency, process cleanup, video state transitions).
3. Queue pre-flight validation and FIFO ordering resilience.
4. Automation queue resilience (no 5-hour stall on empty candidates, FIFO selection, approval-free publishing).
5. Log streaming and SQLite write throttling.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import agent, render
from core.state import State
import studio.server as server
import studio.store as store


class Stage7Phase2Tests(unittest.TestCase):
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
        (cls.root / "config.json").write_text("{}", encoding="utf-8")

        cls.db_path = cls.root / "test_stage7_p2.sqlite3"

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
            DB=cls.db_path,
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
        server.STOP_EVENT.clear()
        server.QUEUE_EVENT.clear()
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
                db.execute("DELETE FROM videos")

    def tearDown(self):
        with server.GUARD:
            if server.ACTIVE_PROC and server.ACTIVE_PROC.poll() is None:
                try:
                    server.ACTIVE_PROC.kill()
                except Exception:
                    pass
            server.ACTIVE_JOB = None
            server.ACTIVE_PROC = None

    # =========================================================================
    # Task 1: Subprocess Lifecycle & Watchdog
    # =========================================================================

    def test_render_composite_timeout_raises_and_cleans_output(self):
        """render.composite() unlinks partial output file and raises TimeoutError when ffmpeg times out."""
        dummy_out = self.root / "out" / "timed_out_test.mp4"
        dummy_out.write_bytes(b"partial video content")
        self.assertTrue(dummy_out.exists())

        def fake_run(cmd, *args, **kwargs):
            if "-i" in cmd:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 1.0))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(TimeoutError) as cm:
                render.composite(
                    video=self.root / "dummy.mp4",
                    overlay=self.root / "overlay.png",
                    box=(0, 0, 1080, 1920),
                    layout={"canvas_width": 1080, "canvas_height": 1920, "background": "#000000"},
                    out=dummy_out,
                    timeout=0.1,
                )
            self.assertIn("timed out", str(cm.exception).lower())
            self.assertFalse(dummy_out.exists(), "Partial output file must be cleaned up on timeout")

    def test_render_composite_calledprocesserror_cleans_output(self):
        """render.composite() unlinks partial output file on CalledProcessError."""
        dummy_out = self.root / "out" / "error_test.mp4"
        dummy_out.write_bytes(b"corrupt frames")
        self.assertTrue(dummy_out.exists())

        def fake_run(cmd, *args, **kwargs):
            if "-i" in cmd:
                raise subprocess.CalledProcessError(returncode=1, cmd=cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(subprocess.CalledProcessError):
                render.composite(
                    video=self.root / "dummy.mp4",
                    overlay=self.root / "overlay.png",
                    box=(0, 0, 1080, 1920),
                    layout={"canvas_width": 1080, "canvas_height": 1920, "background": "#000000"},
                    out=dummy_out,
                )
            self.assertFalse(dummy_out.exists(), "Partial output file must be cleaned up on error")

    def test_download_clip_cleans_partial_files_on_timeout(self):
        """agent.download_clip() cleans up partial .mp4 and .part files when yt-dlp times out."""
        out_mp4 = self.root / "work" / "test_clip.mp4"
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        out_mp4.write_bytes(b"half downloaded")
        out_part = self.root / "work" / "test_clip.part"
        out_part.write_bytes(b"part buffer")

        def fake_run(cmd, *args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 90))

        with patch("subprocess.run", side_effect=fake_run):
            res = agent.download_clip("https://example.com/watch?v=123", out_mp4)
            self.assertFalse(res)
            self.assertFalse(out_mp4.exists(), "Partial .mp4 should be unlinked on timeout")
            self.assertFalse(out_part.exists(), "Partial .part should be unlinked on timeout")

    def test_job_watchdog_timeout_terminates_process_and_marks_failed(self):
        """run_job_process() terminates hung process via watchdog and records clear failure diagnostic."""
        job_id = "job_watchdog_test"
        dummy_artifact = self.root / "out" / f"{job_id}.mp4"
        dummy_artifact.write_bytes(b"partial render")

        job = {
            "id": job_id,
            "action": "render",
            "status": "running",
            "stage": "Rendering",
            "stages": [{"stage": "Rendering", "at": store.now()}],
            "target_file": str(dummy_artifact),
            "created_at": store.now(),
            "started_at": store.now(),
            "timeout": 0.4,  # 400ms timeout for fast deterministic test
        }
        store.put("jobs", job_id, job)

        # Launch a python command that sleeps 10 seconds
        code = "import time, sys; sys.stdout.write('Running\\n'); sys.stdout.flush(); time.sleep(10)"
        cmd = [sys.executable, "-c", code]

        server.run_job_process(job, cmd)

        updated = store.get("jobs", job_id)
        self.assertEqual(updated["status"], "failed")
        self.assertIn("timed out", updated["stage"].lower())
        self.assertIn("exceeded timeout limit", updated["error"].lower())
        self.assertFalse(dummy_artifact.exists(), "Job artifact must be cleaned up on timeout")

    def test_job_exception_terminates_process_tree(self):
        """run_job_process() terminates process tree and cleans up if an unexpected exception occurs."""
        job_id = "job_exc_test"
        job = {
            "id": job_id,
            "action": "preview",
            "status": "running",
            "stage": "Starting",
            "stages": [{"stage": "Starting", "at": store.now()}],
            "created_at": store.now(),
            "started_at": store.now(),
        }
        store.put("jobs", job_id, job)

        killed = []
        orig_terminate = server.terminate_process_tree
        def mock_term(pid, expected_created_at=None):
            killed.append(pid)
            return orig_terminate(pid, expected_created_at)

        class FailingStdout:
            def __init__(self, real):
                self._real = real
            def __iter__(self):
                yield "started\n"
                raise RuntimeError("Simulated broken pipe in reader")
            def __getattr__(self, name):
                return getattr(self._real, name)

        proc_ref = []
        orig_popen = subprocess.Popen
        def popen_with_failing_stdout(*args, **kwargs):
            p = orig_popen(*args, **kwargs)
            proc_ref.append(p)
            p.stdout = FailingStdout(p.stdout)
            return p

        try:
            with patch.object(server, "terminate_process_tree", side_effect=mock_term), \
                 patch("subprocess.Popen", side_effect=popen_with_failing_stdout):
                cmd = [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(10)",
                ]
                server.run_job_process(job, cmd)
        finally:
            if proc_ref and proc_ref[0].poll() is None:
                proc_ref[0].kill()
                proc_ref[0].wait()

        self.assertGreater(len(killed), 0, "terminate_process_tree should be called on unhandled exception")
        updated = store.get("jobs", job_id)
        self.assertEqual(updated["status"], "failed")

    # =========================================================================
    # Task 2: Worker Interruption & Job Recovery
    # =========================================================================

    def test_recover_interrupted_jobs_handles_running_jobs_and_videos(self):
        """recover_interrupted_jobs() marks running jobs interrupted, cleans artifacts, and updates video states."""
        job_id = "job_running_interrupted"
        artifact = self.root / "out" / f"{job_id}.mp4"
        artifact.write_bytes(b"orphan artifact")

        job = {
            "id": job_id,
            "action": "render",
            "status": "running",
            "stage": "Rendering",
            "stages": [{"stage": "Rendering", "at": store.now()}],
            "target_file": str(artifact),
            "created_at": store.now(),
            "started_at": store.now(),
        }
        store.put("jobs", job_id, job)

        vid_uploading = {
            "id": "vid_upl_int",
            "status": "uploading",
            "resumable_uri": "https://upload.youtube.com/my_session",
            "video": "out/test1.mp4",
        }
        vid_rendering = {
            "id": "vid_rnd_int",
            "status": "rendering",
            "video": "out/test2.mp4",
        }
        store.put("videos", vid_uploading["id"], vid_uploading)
        store.put("videos", vid_rendering["id"], vid_rendering)

        server.recover_interrupted_jobs()

        # Job must be interrupted and artifact cleaned up
        updated_job = store.get("jobs", job_id)
        self.assertEqual(updated_job["status"], "interrupted")
        self.assertIn("interrupted by server restart", updated_job["stage"].lower())
        self.assertFalse(artifact.exists(), "Orphan artifact should be deleted")

        # Videos must be recovered without losing resumable session
        up_rec = store.get("videos", vid_uploading["id"])
        self.assertEqual(up_rec["status"], "upload_unknown")
        self.assertEqual(up_rec["resumable_uri"], "https://upload.youtube.com/my_session")

        rnd_rec = store.get("videos", vid_rendering["id"])
        self.assertEqual(rnd_rec["status"], "render_failed")

    def test_recover_interrupted_jobs_is_idempotent(self):
        """Calling recover_interrupted_jobs() multiple times causes no duplicate stages or errors."""
        job_id = "job_idempotent_int"
        job = {
            "id": job_id,
            "action": "render",
            "status": "running",
            "stage": "Starting",
            "stages": [{"stage": "Starting", "at": store.now()}],
            "created_at": store.now(),
            "started_at": store.now(),
        }
        store.put("jobs", job_id, job)

        server.recover_interrupted_jobs()
        first_rec = store.get("jobs", job_id)
        self.assertEqual(first_rec["status"], "interrupted")

        # Second call
        server.recover_interrupted_jobs()
        second_rec = store.get("jobs", job_id)
        self.assertEqual(second_rec["status"], "interrupted")
        self.assertEqual(len(second_rec["stages"]), len(first_rec["stages"]))

    # =========================================================================
    # Task 3: Queue Pre-flight Validation & FIFO Ordering
    # =========================================================================

    def test_queue_worker_preflight_rejects_already_uploaded_video(self):
        """queue_worker_loop() pre-flight validation prevents executing upload on already uploaded video."""
        vid_id = "vid_already_done"
        store.put("videos", vid_id, {
            "id": vid_id,
            "status": "uploaded",
            "youtube_id": "yt_existing_999",
            "video": "out/done.mp4",
        })

        job_id = "job_upload_redundant"
        job = {
            "id": job_id,
            "action": "upload",
            "key": vid_id,
            "status": "pending",
            "created_at": "2026-10-01T10:00:00Z",
            "command": [sys.executable, "-c", "import sys; sys.exit(0)"],
        }
        store.put("jobs", job_id, job)

        # Trigger single pass of queue worker logic
        with patch.object(server, "run_job_process") as mock_run:
            # Wake worker loop or simulate queue pass
            with server.GUARD:
                candidate = server.get_next_pending_job()
                self.assertIsNotNone(candidate)
                self.assertEqual(candidate["id"], job_id)

            # Let queue_worker_loop process one step
            th = threading.Thread(target=server.queue_worker_loop, daemon=True)
            th.start()
            server.QUEUE_EVENT.set()

            # Wait up to 1 second for pre-flight to process
            for _ in range(20):
                j = store.get("jobs", job_id)
                if j and j.get("status") != "pending":
                    break
                time.sleep(0.05)

            server.STOP_EVENT.set()
            server.QUEUE_EVENT.set()
            th.join(timeout=1)
            server.STOP_EVENT.clear()

            self.assertEqual(mock_run.call_count, 0, "Subprocess must NOT be executed for already uploaded video")
            j = store.get("jobs", job_id)
            self.assertEqual(j["status"], "failed")
            self.assertIn("already been uploaded", j["error"])

    def test_queue_worker_preflight_rejects_ambiguous_video(self):
        """queue_worker_loop() pre-flight rejects upload of video in upload_unknown state without resume."""
        vid_id = "vid_ambig_test"
        store.put("videos", vid_id, {
            "id": vid_id,
            "status": "upload_unknown",
            "resumable_uri": "https://upload.youtube.com/test",
            "video": "out/ambig.mp4",
        })

        job_id = "job_ambig_pending"
        job = {
            "id": job_id,
            "action": "upload",
            "key": vid_id,
            "status": "pending",
            "created_at": store.now(),
            "command": [sys.executable, "-c", "import sys; sys.exit(0)"],
        }
        store.put("jobs", job_id, job)

        with patch.object(server, "run_job_process") as mock_run:
            th = threading.Thread(target=server.queue_worker_loop, daemon=True)
            th.start()
            server.QUEUE_EVENT.set()

            for _ in range(20):
                j = store.get("jobs", job_id)
                if j and j.get("status") != "pending":
                    break
                time.sleep(0.05)

            server.STOP_EVENT.set()
            server.QUEUE_EVENT.set()
            th.join(timeout=1)
            server.STOP_EVENT.clear()

            self.assertEqual(mock_run.call_count, 0)
            j = store.get("jobs", job_id)
            self.assertEqual(j["status"], "failed")
            self.assertIn("upload_unknown", j["error"])

    def test_queue_worker_fifo_ordering_with_transient_failure(self):
        """Failing job in queue does not block subsequent eligible jobs in FIFO order."""
        # Job 1: Ineligible video (missing from db)
        job1_id = "job_fifo_1_bad"
        store.put("jobs", job1_id, {
            "id": job1_id,
            "action": "upload",
            "key": "nonexistent_vid",
            "status": "pending",
            "created_at": "2026-10-01T01:00:00Z",
        })

        # Job 2: Eligible video
        vid2_file = self.root / "out" / "vid2.mp4"
        vid2_file.write_bytes(b"ready video")
        vid2_id = "vid2_ready"
        store.put("videos", vid2_id, {
            "id": vid2_id,
            "status": "ready",
            "video": str(vid2_file.relative_to(self.root)),
            "title": "Valid Short",
        })
        job2_id = "job_fifo_2_good"
        store.put("jobs", job2_id, {
            "id": job2_id,
            "action": "upload",
            "key": vid2_id,
            "status": "pending",
            "created_at": "2026-10-01T02:00:00Z",
        })

        executed = []
        def fake_run(j, cmd):
            executed.append(j["id"])
            j["status"] = "completed"
            store.put("jobs", j["id"], j)

        with patch.object(server, "run_job_process", side_effect=fake_run):
            th = threading.Thread(target=server.queue_worker_loop, daemon=True)
            th.start()
            server.QUEUE_EVENT.set()

            for _ in range(30):
                j1 = store.get("jobs", job1_id)
                j2 = store.get("jobs", job2_id)
                if j1 and j1.get("status") == "failed" and j2 and j2.get("status") == "completed":
                    break
                time.sleep(0.05)

            server.STOP_EVENT.set()
            server.QUEUE_EVENT.set()
            th.join(timeout=1)
            server.STOP_EVENT.clear()

            j1 = store.get("jobs", job1_id)
            j2 = store.get("jobs", job2_id)
            self.assertEqual(j1["status"], "failed")
            self.assertEqual(j2["status"], "completed")
            self.assertEqual(executed, [job2_id])

    # =========================================================================
    # Task 4: Automation Loop Resilience
    # =========================================================================

    def test_automation_loop_no_candidates_schedules_short_retry_not_interval(self):
        """When no eligible ready videos exist, automation loop sets next_run to 60s, avoiding 5-hour stall."""
        settings_file = self.root / "studio-settings.json"
        now_ts = 1000000.0
        settings = {
            "enabled": True,
            "mode": "publish",
            "interval_hours": 5.0,
            "next_run": now_ts - 10,
        }
        settings_file.write_text(json.dumps(settings), encoding="utf-8")

        server.STOP_EVENT.clear()
        # Mock is_online, time, and empty videos
        with patch("time.time", return_value=now_ts), \
             patch("time.sleep", side_effect=lambda s: server.STOP_EVENT.set()), \
             patch.object(store, "busy", return_value=False), \
             patch.object(server, "is_online", return_value=True):
            server.automation_loop()
            server.STOP_EVENT.clear()

        updated_settings = json.loads(settings_file.read_text(encoding="utf-8"))
        # Must be now_ts + 60.0 (short backoff), NOT now_ts + 5 * 3600
        self.assertEqual(updated_settings["next_run"], now_ts + 60.0)

    def test_automation_loop_advances_interval_when_candidate_queued(self):
        """When an eligible candidate is queued, automation loop advances next_run by interval_hours."""
        vid_file = self.root / "out" / "ready_short.mp4"
        vid_file.write_bytes(b"ready video")
        vid_id = "vid_auto_ready"
        store.put("videos", vid_id, {
            "id": vid_id,
            "status": "ready",
            "review_status": "unreviewed",
            "video": str(vid_file.relative_to(self.root)),
            "title": "Auto Publish Short",
            "created_at": "2026-10-01T05:00:00Z",
        })

        settings_file = self.root / "studio-settings.json"
        now_ts = 2000000.0
        settings = {
            "enabled": True,
            "mode": "publish",
            "interval_hours": 4.0,
            "next_run": now_ts - 10,
        }
        settings_file.write_text(json.dumps(settings), encoding="utf-8")

        queued_jobs = []
        def fake_start(action, key="", automatic=False, extra=None):
            queued_jobs.append((action, key))
            return {"status": "pending"}

        server.STOP_EVENT.clear()
        with patch("time.time", return_value=now_ts), \
             patch("time.sleep", side_effect=lambda s: server.STOP_EVENT.set()), \
             patch.object(store, "busy", return_value=False), \
             patch.object(server, "is_online", return_value=True), \
             patch.object(server, "start_job", side_effect=fake_start):
            server.automation_loop()
            server.STOP_EVENT.clear()

        self.assertEqual(len(queued_jobs), 1)
        self.assertEqual(queued_jobs[0], ("upload", vid_id))

        updated_settings = json.loads(settings_file.read_text(encoding="utf-8"))
        self.assertEqual(updated_settings["next_run"], now_ts + 4.0 * 3600)

    def test_automation_loop_skips_failed_video_picks_next_ready_fifo(self):
        """Automation loop skips failed/unknown videos and picks the oldest ready video."""
        # Video 1: Failed
        store.put("videos", "v1_failed", {
            "id": "v1_failed",
            "status": "failed",
            "created_at": "2026-10-01T01:00:00Z",
        })
        # Video 2: Ready
        v2_file = self.root / "out" / "v2.mp4"
        v2_file.write_bytes(b"ready v2")
        store.put("videos", "v2_ready", {
            "id": "v2_ready",
            "status": "ready",
            "video": str(v2_file.relative_to(self.root)),
            "created_at": "2026-10-01T02:00:00Z",
        })
        # Video 3: Ready
        v3_file = self.root / "out" / "v3.mp4"
        v3_file.write_bytes(b"ready v3")
        store.put("videos", "v3_ready", {
            "id": "v3_ready",
            "status": "ready",
            "video": str(v3_file.relative_to(self.root)),
            "created_at": "2026-10-01T03:00:00Z",
        })

        settings_file = self.root / "studio-settings.json"
        now_ts = 3000000.0
        settings = {
            "enabled": True,
            "mode": "publish",
            "interval_hours": 3.0,
            "next_run": now_ts - 5,
        }
        settings_file.write_text(json.dumps(settings), encoding="utf-8")

        queued_keys = []
        def fake_start(action, key="", automatic=False, extra=None):
            queued_keys.append(key)
            return {"status": "pending"}

        server.STOP_EVENT.clear()
        with patch("time.time", return_value=now_ts), \
             patch("time.sleep", side_effect=lambda s: server.STOP_EVENT.set()), \
             patch.object(store, "busy", return_value=False), \
             patch.object(server, "is_online", return_value=True), \
             patch.object(server, "start_job", side_effect=fake_start):
            server.automation_loop()
            server.STOP_EVENT.clear()

        self.assertEqual(queued_keys, ["v2_ready"])

    # =========================================================================
    # Task 5: Log Streaming & Write Overhead Reduction
    # =========================================================================

    def test_run_job_process_throttles_sqlite_writes_for_high_frequency_logs(self):
        """run_job_process() throttles regular log SQLite updates while persisting every line to .log file."""
        job_id = "job_throttle_test"
        job = {
            "id": job_id,
            "action": "preview",
            "status": "running",
            "created_at": store.now(),
            "started_at": store.now(),
        }
        store.put("jobs", job_id, job)

        # Subprocess prints 50 regular lines in quick succession
        code = "import sys\nfor i in range(50):\n    sys.stdout.write(f'Debug line {i}\\n')\nsys.stdout.flush()\n"
        cmd = [sys.executable, "-c", code]

        put_count = 0
        orig_put = store.put
        def counting_put(table, key, data):
            nonlocal put_count
            if table == "jobs" and key == job_id:
                put_count += 1
            return orig_put(table, key, data)

        with patch.object(store, "put", side_effect=counting_put):
            server.run_job_process(job, cmd)

        # 50 lines would have been 50+ writes without throttling.
        # With 1.0s throttling on regular lines + start/end, total writes must be <= 5!
        self.assertLessEqual(put_count, 5, f"Expected throttled writes <= 5, got {put_count}")

        # Verify all 50 lines are intact in log file
        log_file = self.logs_dir / f"{job_id}.log"
        self.assertTrue(log_file.exists())
        log_text = log_file.read_text(encoding="utf-8")
        for i in range(50):
            self.assertIn(f"Debug line {i}", log_text)

    def test_run_job_process_persists_progress_and_summary_immediately(self):
        """KENAU_PROGRESS and KENAU_SUMMARY lines trigger immediate SQLite persistence."""
        job_id = "job_immediate_progress"
        job = {
            "id": job_id,
            "action": "render",
            "status": "running",
            "created_at": store.now(),
            "started_at": store.now(),
        }
        store.put("jobs", job_id, job)

        code = (
            "import sys, time\n"
            "sys.stdout.write('KENAU_PROGRESS {\"stage\": \"Testing Step 1\"}\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(0.05)\n"
            "sys.stdout.write('KENAU_SUMMARY {\"status\": \"completed\", \"message\": \"Done!\"}\\n')\n"
            "sys.stdout.flush()\n"
        )
        cmd = [sys.executable, "-c", code]

        server.run_job_process(job, cmd)

        updated = store.get("jobs", job_id)
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(updated["summary"]["message"], "Done!")
        stages = [s["stage"] for s in updated["stages"]]
        self.assertIn("Testing Step 1", stages)

    # =========================================================================
    # Task 6: Live Video-State Recovery on Job Timeout
    # =========================================================================

    def test_render_timeout_transitions_video_to_render_failed(self):
        """When a render job times out, associated video transitions from rendering to render_failed."""
        vid_id = "vid_to_render_failed"
        store.put("videos", vid_id, {
            "id": vid_id,
            "status": "rendering",
            "video": "out/test_render_to.mp4",
            "headline": "Timeout Render Headline",
        })

        job_id = "job_to_render"
        job = {
            "id": job_id,
            "action": "render",
            "key": vid_id,
            "status": "running",
            "stage": "Rendering",
            "stages": [{"stage": "Rendering", "at": store.now()}],
            "created_at": store.now(),
            "started_at": store.now(),
            "timeout": 0.3,
        }
        store.put("jobs", job_id, job)

        cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
        server.run_job_process(job, cmd)

        updated_job = store.get("jobs", job_id)
        self.assertEqual(updated_job["status"], "failed")
        self.assertIn("timed out", updated_job["stage"].lower())

        video = store.get("videos", vid_id)
        self.assertEqual(video["status"], "render_failed")
        self.assertIn("timed out", video.get("error", "").lower())

    def test_upload_timeout_transitions_video_to_upload_unknown_and_preserves_metadata(self):
        """When an upload job times out, associated video transitions from uploading to upload_unknown and preserves session."""
        vid_id = "vid_to_upload_unknown"
        resumable_uri = "https://upload.youtube.com/my_active_session_456"
        store.put("videos", vid_id, {
            "id": vid_id,
            "status": "uploading",
            "title": "Timeout Upload Test",
            "video": "out/ready_to_upload.mp4",
            "resumable_uri": resumable_uri,
        })

        job_id = "job_to_upload"
        job = {
            "id": job_id,
            "action": "upload",
            "key": vid_id,
            "status": "running",
            "stage": "Uploading to YouTube",
            "stages": [{"stage": "Uploading to YouTube", "at": store.now()}],
            "created_at": store.now(),
            "started_at": store.now(),
            "timeout": 0.3,
        }
        store.put("jobs", job_id, job)

        cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
        server.run_job_process(job, cmd)

        updated_job = store.get("jobs", job_id)
        self.assertEqual(updated_job["status"], "failed")

        video = store.get("videos", vid_id)
        self.assertEqual(video["status"], "upload_unknown")
        self.assertEqual(video["resumable_uri"], resumable_uri, "resumable_uri must be preserved")
        self.assertEqual(video["title"], "Timeout Upload Test", "metadata must be preserved")
        self.assertIn("timed out", video.get("upload_failure_reason", "").lower())

    def test_timeout_does_not_overwrite_uploaded_video(self):
        """Job timeout must NOT overwrite a video that is already marked uploaded."""
        vid_id = "vid_already_uploaded_guard"
        store.put("videos", vid_id, {
            "id": vid_id,
            "status": "uploaded",
            "youtube_id": "yt_success_777",
            "youtube_url": "https://youtube.com/shorts/yt_success_777",
            "video": "out/done.mp4",
        })

        job_id = "job_to_uploaded_guard"
        job = {
            "id": job_id,
            "action": "upload",
            "key": vid_id,
            "status": "running",
            "created_at": store.now(),
            "started_at": store.now(),
            "timeout": 0.3,
        }
        store.put("jobs", job_id, job)

        cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
        server.run_job_process(job, cmd)

        video = store.get("videos", vid_id)
        self.assertEqual(video["status"], "uploaded", "uploaded status must NEVER be overwritten by timeout")
        self.assertEqual(video["youtube_id"], "yt_success_777")

    def test_repeated_timeout_handling_is_safe_and_idempotent(self):
        """Calling _handle_job_timeout_video_state repeatedly is idempotent and does not corrupt state."""
        vid_id = "vid_idempotent_to"
        store.put("videos", vid_id, {
            "id": vid_id,
            "status": "uploading",
            "resumable_uri": "https://upload.youtube.com/session_idem",
        })
        job = {"action": "upload", "key": vid_id}

        server._handle_job_timeout_video_state(job, 900.0)
        v1 = store.get("videos", vid_id)
        self.assertEqual(v1["status"], "upload_unknown")

        # Second invocation
        server._handle_job_timeout_video_state(job, 900.0)
        v2 = store.get("videos", vid_id)
        self.assertEqual(v2["status"], "upload_unknown")
        self.assertEqual(v2["resumable_uri"], "https://upload.youtube.com/session_idem")

    def test_timed_out_video_retry_without_server_restart(self):
        """A video whose job timed out can be retried through safe workflows without restarting server."""
        vid_file = self.root / "out" / "render_retry_test.mp4"
        vid_file.write_bytes(b"rendered video bytes")
        vid_id = "vid_retry_without_restart"
        store.put("videos", vid_id, {
            "id": vid_id,
            "status": "render_failed",
            "video": str(vid_file.relative_to(self.root)),
            "headline": "Headline to Retry",
            "title": "Title to Retry",
        })

        job_id = "job_timed_out_source"
        store.put("jobs", job_id, {
            "id": job_id,
            "action": "render",
            "key": vid_id,
            "status": "failed",
            "error": "Process exceeded timeout limit",
            "created_at": store.now(),
            "started_at": store.now(),
            "finished_at": store.now(),
        })

        # Operator can retry the job immediately without restarting server
        res = server.retry_job(job_id)
        self.assertIn("id", res)
        self.assertEqual(res["status"], "pending")
        self.assertEqual(res["retry_of"], job_id)

        new_job = store.get("jobs", res["id"])
        self.assertIsNotNone(new_job)
        self.assertIn(new_job["status"], ("pending", "running"))


if __name__ == "__main__":
    unittest.main()
