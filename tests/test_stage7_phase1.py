"""
Unit and integration tests for Stage 7 Phase 1:
- Server lock unblocking (network I/O runs outside GUARD lock without starving /api/status).
- Manual upload resolution state synchronization into state.json.
- Idempotent state.json mark_posted behavior.
- SQLite WAL mode, PRAGMA configuration, and store schema table validation.
"""
import http.client
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import studio.server as server
import studio.store as store
from core.state import State
from http.server import ThreadingHTTPServer


class Stage7Phase1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / "out").mkdir()
        (cls.root / "assets").mkdir()
        (cls.root / "logs").mkdir()
        cls.web_dir = cls.root / "studio" / "web"
        cls.web_dir.mkdir(parents=True)
        (cls.web_dir / "index.html").write_text("<html>ok</html>", encoding="utf-8")
        (cls.root / "config.json").write_text("{}", encoding="utf-8")

        cls.db_path = cls.root / "test_stage7.sqlite3"

        cls.server_patches = patch.multiple(
            server,
            ROOT=cls.root,
            WEB_DIR=cls.web_dir,
            LOGS_DIR=cls.root / "logs",
        )
        cls.server_patches.start()

        cls.store_patches = patch.multiple(
            store,
            ROOT=cls.root,
            DB=cls.db_path,
        )
        cls.store_patches.start()

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.StudioHandler)
        cls.port = cls.httpd.server_address[1]
        cls.port_patch = patch.object(server, "PORT", cls.port)
        cls.port_patch.start()

        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.server_thread.join(timeout=2)
        cls.port_patch.stop()
        cls.store_patches.stop()
        cls.server_patches.stop()
        cls.tmp.cleanup()

    def setUp(self):
        with store.connect() as conn:
            conn.execute("DELETE FROM videos")
            conn.execute("DELETE FROM jobs")
        with server.GUARD:
            server.ACTIVE_JOB = None
            server.ACTIVE_PROC = None
        server._RECONCILING_VIDEOS.clear()

        # Reset state.json
        state_p = self.root / "state.json"
        if state_p.exists():
            state_p.unlink()

    def _request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        h = {
            "Host": f"127.0.0.1:{self.port}",
            "Origin": f"http://127.0.0.1:{self.port}",
            "X-Studio-Token": server.CSRF,
            "Content-Type": "application/json",
        }
        if headers:
            h.update(headers)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, data, h)
        resp = conn.getresponse()
        resp_data = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(resp_data.decode("utf-8"))
        except Exception:
            return resp.status, resp_data

    # -------------------------------------------------------------------------
    # 1. Server Lock Unblocking (PERF-01)
    # -------------------------------------------------------------------------

    def test_youtube_connection_test_does_not_block_status(self):
        """Simulated slow YouTube connection test must not hold GUARD or block /api/status."""
        token_path = self.root / "token.json"
        token_path.write_text(json.dumps({
            "token": "fake_token",
            "refresh_token": "fake_refresh",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "cid",
            "client_secret": "cs",
            "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
        }), encoding="utf-8")

        test_started = threading.Event()
        test_finish = threading.Event()

        def slow_connection_test(*args, **kwargs):
            test_started.set()
            test_finish.wait(timeout=5)
            return {"status": "ok", "channel": "KenauTest", "channel_id": "UC123"}

        with patch.object(server, "test_youtube_connection", side_effect=slow_connection_test), \
             patch.object(server, "is_online", return_value=True):
            # Launch connection test in separate thread via HTTP
            def run_test():
                self._request("POST", "/api/connections/test", {})

            th = threading.Thread(target=run_test, daemon=True)
            th.start()

            # Wait until connection test starts running
            self.assertTrue(test_started.wait(timeout=3))

            # Concurrently request /api/status - this must return immediately (<1.0s) and not hang
            t0 = time.time()
            status_code, body = self._request("GET", "/api/status")
            elapsed = time.time() - t0

            self.assertEqual(status_code, 200)
            self.assertIn("online", body)
            self.assertLess(elapsed, 1.0, "GET /api/status was blocked by connections/test!")

            test_finish.set()
            th.join(timeout=2)

    def test_youtube_connection_test_busy_lock(self):
        """A second concurrent call to test_youtube_connection returns busy status."""
        (self.root / "client_secret.json").write_text("{}", encoding="utf-8")
        token_path = self.root / "token.json"
        token_path.write_text("{}", encoding="utf-8")

        # Artificially hold the connection test lock
        acquired = server._CONNECTION_TEST_LOCK.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            res = server.test_youtube_connection()
            self.assertEqual(res.get("status"), "busy")
            self.assertIn("already in progress", res.get("message", ""))
        finally:
            server._CONNECTION_TEST_LOCK.release()

    def test_reconcile_video_upload_does_not_block_status(self):
        """Simulated slow HTTP PUT in reconcile_video_upload must not hold GUARD or block /api/status."""
        vid_id = "vid_reconcile_unblock"
        video_record = {
            "id": vid_id,
            "status": "upload_unknown",
            "resumable_uri": "https://upload.youtube.com/my_upload_session",
            "title": "Unblock Test",
            "video": "out/test.mp4",
        }
        store.put("videos", vid_id, video_record)

        put_started = threading.Event()
        put_finish = threading.Event()

        def slow_requests_put(url, headers=None, timeout=None):
            put_started.set()
            put_finish.wait(timeout=5)
            mock_resp = MagicMock()
            mock_resp.status_code = 308
            mock_resp.headers = {"Range": "bytes 0-1000"}
            return mock_resp

        import requests
        with patch.object(requests, "put", side_effect=slow_requests_put), \
             patch.object(server, "is_online", return_value=True):
            def run_reconcile():
                self._request("POST", "/api/video/reconcile", {"id": vid_id})

            th = threading.Thread(target=run_reconcile, daemon=True)
            th.start()

            self.assertTrue(put_started.wait(timeout=3))

            # Concurrently request /api/status
            t0 = time.time()
            status_code, body = self._request("GET", "/api/status")
            elapsed = time.time() - t0

            self.assertEqual(status_code, 200)
            self.assertIn("online", body)
            self.assertLess(elapsed, 1.0, "GET /api/status was blocked by video/reconcile!")

            put_finish.set()
            th.join(timeout=2)


    def test_status_slow_probe_does_not_block_guard(self):
        """A slow network connectivity probe in is_online does not block unrelated guarded operations."""
        probe_started = threading.Event()
        probe_finish = threading.Event()

        def slow_probe():
            probe_started.set()
            probe_finish.wait(timeout=5)
            return True

        server._ONLINE_CACHE = {"status": True, "checked_at": 0.0}

        with patch.object(server, "probe_network_connectivity", side_effect=slow_probe):
            th = threading.Thread(target=lambda: self._request("GET", "/api/status"), daemon=True)
            th.start()

            self.assertTrue(probe_started.wait(timeout=3))

            # While probe is running outside GUARD, verify GUARD can be acquired immediately
            t0 = time.time()
            guard_acquired = False
            with server.GUARD:
                guard_acquired = True
            elapsed = time.time() - t0

            self.assertTrue(guard_acquired)
            self.assertLess(elapsed, 0.5, "GUARD was blocked by network probe!")

            probe_finish.set()
            th.join(timeout=2)

    def test_status_concurrent_requests_use_cached_probe(self):
        """Concurrent status requests do not trigger multiple simultaneous probes."""
        probe_count = 0
        probe_lock = threading.Lock()

        def counted_probe():
            nonlocal probe_count
            with probe_lock:
                probe_count += 1
            time.sleep(0.05)
            return True

        server._ONLINE_CACHE = {"status": True, "checked_at": 0.0}

        threads = []
        statuses = []

        def worker():
            code, body = self._request("GET", "/api/status")
            statuses.append((code, body.get("online")))

        with patch.object(server, "probe_network_connectivity", side_effect=counted_probe):
            for _ in range(5):
                t = threading.Thread(target=worker)
                threads.append(t)
                t.start()

            for t in threads:
                t.join(timeout=3)

        self.assertEqual(len(statuses), 5)
        for code, online in statuses:
            self.assertEqual(code, 200)
            self.assertTrue(online)
        self.assertLessEqual(probe_count, 2)

    def test_status_probe_exception_does_not_break_status(self):
        """Probe exceptions do not leave probe lock held or break /api/status response."""
        server._ONLINE_CACHE = {"status": False, "checked_at": 0.0}

        def failing_probe():
            raise OSError("Simulated socket error")

        with patch.object(server, "probe_network_connectivity", side_effect=failing_probe):
            code, body = self._request("GET", "/api/status")
            self.assertEqual(code, 200)
            self.assertIn("online", body)
            self.assertFalse(body["online"])

        # Probe lock must not be left held
        self.assertFalse(server._ONLINE_LOCK.locked())

    def test_reconcile_concurrent_same_video_rejected(self):
        """Concurrent reconciliation of the same video ID raises ConflictError (HTTP 409)."""
        vid_id = "vid_rec_conflict"
        video_record = {
            "id": vid_id,
            "status": "upload_unknown",
            "resumable_uri": "https://upload.youtube.com/conflict_session",
            "title": "Conflict Test",
        }
        store.put("videos", vid_id, video_record)

        # Mark vid_id as currently reconciling
        with server._RECONCILE_LOCK:
            server._RECONCILING_VIDEOS.add(vid_id)

        try:
            status_code, body = self._request("POST", "/api/video/reconcile", {"id": vid_id})
            self.assertEqual(status_code, 409)
            self.assertIn("Reconciliation already in progress", body.get("error", ""))
        finally:
            with server._RECONCILE_LOCK:
                server._RECONCILING_VIDEOS.discard(vid_id)

    def test_reconcile_video_upload_syncs_state_on_completion(self):
        """When reconcile_video_upload receives 200/201, it syncs candidate to state.json."""
        vid_id = "vid_rec_success"
        cand_key = "reddit_post_xyz123"
        video_record = {
            "id": vid_id,
            "candidate": {"key": cand_key},
            "status": "upload_unknown",
            "resumable_uri": "https://upload.youtube.com/success_session",
            "title": "Success Short",
            "headline": "A great headline",
            "video": "out/vid_rec_success.mp4",
        }
        store.put("videos", vid_id, video_record)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "yt_rec_done_999"}

        import requests
        with patch.object(requests, "put", return_value=mock_resp):
            status_code, body = self._request("POST", "/api/video/reconcile", {"id": vid_id})
            self.assertEqual(status_code, 200)
            self.assertEqual(body.get("status"), "uploaded")
            self.assertEqual(body.get("youtube_id"), "yt_rec_done_999")

        # Verify database record updated
        rec = store.get("videos", vid_id)
        self.assertEqual(rec["status"], "uploaded")
        self.assertEqual(rec["youtube_id"], "yt_rec_done_999")
        self.assertIsNone(rec["resumable_uri"])

        # Verify state.json was synchronized
        state = State(self.root / "state.json")
        posted = [p for p in state.posted if p.get("key") == cand_key]
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["youtube_id"], "yt_rec_done_999")
        self.assertIn(cand_key, state.seen)


    def test_reconcile_state_sync_failure_and_retry_recovery(self):
        """Simulate SQLite success but state.json failure, then verify retry repairs state.json without re-upload."""
        vid_id = "vid_rec_repair"
        cand_key = "reddit_post_repair_999"
        video_record = {
            "id": vid_id,
            "candidate": {"key": cand_key},
            "status": "upload_unknown",
            "resumable_uri": "https://upload.youtube.com/repair_session",
            "title": "Repair Test Video",
            "headline": "Repair Headline",
            "video": "out/test.mp4",
        }
        store.put("videos", vid_id, video_record)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "yt_repair_id_123"}

        import requests
        put_called = []
        def tracking_put(*args, **kwargs):
            put_called.append(True)
            return mock_resp

        # Step 1: Initial reconciliation where state.mark_posted fails
        with patch.object(requests, "put", side_effect=tracking_put), \
             patch("core.state.State.mark_posted", side_effect=IOError("Simulated disk error writing state.json")):
            with self.assertRaises(IOError):
                server.reconcile_video_upload(vid_id)

        # Confirm SQLite updated to uploaded with youtube_id
        db_rec = store.get("videos", vid_id)
        self.assertEqual(db_rec["status"], "uploaded")
        self.assertEqual(db_rec["youtube_id"], "yt_repair_id_123")

        # Confirm state.json does NOT contain candidate yet
        state_file = self.root / "state.json"
        state = State(state_file)
        self.assertEqual(len([p for p in state.posted if p.get("key") == cand_key]), 0)
        self.assertEqual(len(put_called), 1)

        # Step 2: Retry reconciliation. Must NOT call requests.put (no re-upload), must repair state.json
        put_called.clear()
        with patch.object(requests, "put", side_effect=tracking_put):
            retry_res = server.reconcile_video_upload(vid_id)

        self.assertEqual(retry_res["status"], "uploaded")
        self.assertEqual(retry_res["youtube_id"], "yt_repair_id_123")
        self.assertEqual(len(put_called), 0, "Retry must NOT perform network upload!")

        # Verify state.json is now repaired
        state = State(state_file)
        matching = [p for p in state.posted if p.get("key") == cand_key]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["youtube_id"], "yt_repair_id_123")
        self.assertIn(cand_key, state.seen)

        # Step 3: Repeated reconciliation retry is idempotent (no duplicate entries)
        repeat_res = server.reconcile_video_upload(vid_id)
        self.assertEqual(repeat_res["status"], "uploaded")
        state = State(state_file)
        self.assertEqual(len([p for p in state.posted if p.get("key") == cand_key]), 1)

        # Step 4: Conflicting YouTube ID on manual resolution is still rejected
        with self.assertRaises(server.ConflictError):
            server.resolve_manual_video(vid_id, "confirm_uploaded", "yt_conflicting_id")

    def test_startup_recovery_repairs_missing_state_json(self):
        """Simulate server restart when SQLite is uploaded but state.json was not synced."""
        vid_id = "vid_restart_heal"
        cand_key = "reddit_post_restart_888"
        video_record = {
            "id": vid_id,
            "candidate": {"key": cand_key},
            "status": "uploaded",
            "youtube_id": "yt_restart_888",
            "title": "Restart Healed Video",
            "headline": "Restart Headline",
            "video": "out/restart.mp4",
        }
        store.put("videos", vid_id, video_record)

        state_file = self.root / "state.json"
        state = State(state_file)
        self.assertNotIn(cand_key, [p.get("key") for p in state.posted])

        # Run startup recovery (simulating process restart)
        server.recover_interrupted_jobs()

        # Verify state.json was healed
        state = State(state_file)
        matching = [p for p in state.posted if p.get("key") == cand_key]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["youtube_id"], "yt_restart_888")
        self.assertIn(cand_key, state.seen)

    # -------------------------------------------------------------------------
    # 2. Manual Upload Resolution State Synchronization (DATA-01)
    # -------------------------------------------------------------------------

    def test_resolve_manual_confirm_uploaded_syncs_state(self):
        """Confirming upload manually writes to state.json and marks candidate posted."""
        vid_id = "vid_man_sync"
        cand_key = "reddit_thread_456"
        video_record = {
            "id": vid_id,
            "candidate": {"key": cand_key},
            "status": "upload_unknown",
            "title": "Manual Sync Test",
            "headline": "Manual Sync Headline",
            "video": "out/test.mp4",
        }
        store.put("videos", vid_id, video_record)

        res = server.resolve_manual_video(vid_id, "confirm_uploaded", "yt_manual_123")
        self.assertEqual(res["status"], "uploaded")
        self.assertEqual(res["youtube_id"], "yt_manual_123")

        # Verify state.json
        state = State(self.root / "state.json")
        posted = [p for p in state.posted if p.get("key") == cand_key]
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["youtube_id"], "yt_manual_123")
        self.assertEqual(posted[0]["title"], "Manual Sync Test")
        self.assertIn(cand_key, state.seen)

    def test_resolve_manual_confirm_uploaded_idempotent(self):
        """Re-confirming the same uploaded video with the same ID succeeds without state duplication."""
        vid_id = "vid_man_idem"
        cand_key = "reddit_thread_789"
        video_record = {
            "id": vid_id,
            "candidate": {"key": cand_key},
            "status": "upload_unknown",
            "title": "Idempotent Video",
            "video": "out/idem.mp4",
        }
        store.put("videos", vid_id, video_record)

        # First confirmation
        res1 = server.resolve_manual_video(vid_id, "confirm_uploaded", "yt_idem_001")
        self.assertEqual(res1["status"], "uploaded")

        # Second confirmation with same ID
        res2 = server.resolve_manual_video(vid_id, "confirm_uploaded", "yt_idem_001")
        self.assertEqual(res2["status"], "uploaded")
        self.assertIn("already confirmed", res2.get("message", ""))

        # Verify state.json does NOT contain duplicate posted entries
        state = State(self.root / "state.json")
        matching = [p for p in state.posted if p.get("key") == cand_key]
        self.assertEqual(len(matching), 1)

    def test_resolve_manual_confirm_uploaded_conflicting_id_rejected(self):
        """Attempting to re-confirm an uploaded video with a different ID raises ConflictError."""
        vid_id = "vid_man_conflict"
        video_record = {
            "id": vid_id,
            "status": "uploaded",
            "youtube_id": "yt_original_id",
            "title": "Conflict Video",
        }
        store.put("videos", vid_id, video_record)

        with self.assertRaises(server.ConflictError):
            server.resolve_manual_video(vid_id, "confirm_uploaded", "yt_different_id")

    def test_resolve_manual_rejects_inappropriate_statuses(self):
        """Manual resolution rejects videos in status 'ready', 'unreviewed', or other invalid states."""
        self.assertRaises(ValueError, server.resolve_manual_video, "nonexistent_vid", "confirm_uploaded", "yt123")

        store.put("videos", "vid_ready_state", {"id": "vid_ready_state", "status": "ready"})
        with self.assertRaises(ValueError):
            server.resolve_manual_video("vid_ready_state", "confirm_absent")

        with self.assertRaises(ValueError):
            server.resolve_manual_video("vid_ready_state", "confirm_uploaded", "yt123")

    # -------------------------------------------------------------------------
    # 3. SQLite Concurrency & Schema Cleanup (DATA-02)
    # -------------------------------------------------------------------------

    def test_sqlite_pragmas_wal_normal_busy_timeout(self):
        """store.connect() configures WAL mode, synchronous=NORMAL, and busy_timeout=15000."""
        with store.connect() as conn:
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            sync = conn.execute("PRAGMA synchronous;").fetchone()[0]
            timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]

            self.assertEqual(mode.lower(), "wal")
            self.assertEqual(sync, 1)  # 1 is NORMAL
            self.assertGreaterEqual(timeout, 15000)

    def test_sqlite_table_validation(self):
        """store CRUD methods reject unsupported tables with ValueError."""
        with self.assertRaises(ValueError):
            store.put("subreddits", "r_news", {"name": "news"})

        with self.assertRaises(ValueError):
            store.get("subreddits", "r_news")

        with self.assertRaises(ValueError):
            store.records("subreddits")

        with self.assertRaises(ValueError):
            store.delete("subreddits", "r_news")

        with self.assertRaises(ValueError):
            store.put("injected_table", "key1", {"x": 1})

    def test_sqlite_schema_does_not_create_subreddits_table(self):
        """connect() initializes videos and jobs tables, but does not create a subreddits table."""
        fresh_db = self.root / "fresh.sqlite3"
        with patch.object(store, "DB", fresh_db):
            with store.connect() as conn:
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()]
                self.assertIn("videos", tables)
                self.assertIn("jobs", tables)
                self.assertNotIn("subreddits", tables)

    # -------------------------------------------------------------------------
    # 4. State.mark_posted Idempotency
    # -------------------------------------------------------------------------

    def test_state_mark_posted_idempotent(self):
        """State.mark_posted updates metadata without creating duplicate entries in posted."""
        state_file = self.root / "state_test.json"
        state = State(state_file)

        entry = {
            "key": "post_dup_check",
            "youtube_id": "yt_dup_111",
            "title": "Dup Check 1",
            "headline": "H1",
            "video_path": "out/dup1.mp4",
        }
        state.mark_posted(entry)
        self.assertEqual(len(state.posted), 1)

        # Call again with updated headline and video_path
        entry_updated = {
            "key": "post_dup_check",
            "youtube_id": "yt_dup_111",
            "title": "Dup Check 1 (Updated)",
            "headline": "H1 (Updated)",
            "video_path": "out/dup1_v2.mp4",
        }
        state.mark_posted(entry_updated)
        self.assertEqual(len(state.posted), 1)
        self.assertEqual(state.posted[0]["title"], "Dup Check 1 (Updated)")
        self.assertIn("post_dup_check", state.seen)


if __name__ == "__main__":
    unittest.main()
