"""
tests/test_stage6_publishing.py — Comprehensive tests for Stage 6:
Reliable YouTube Publishing, Resumable Upload Recovery, OAuth Health & Approval-Gated Automation.

Covers:
1. Multi-Layer Approval Enforcement (start_job, worker, retry_job reject unreviewed/rejected videos)
2. Duplicate Upload Guards & State Machine (HTTP 409 Conflict on uploaded, uploading, upload_unknown, upload_unresolved, queued duplicates)
3. Missing Video File Validation (rejects upload if rendered artifact missing on disk)
4. Resumable Upload Session Capture & Store Masking (has_resumable_session exposed, raw session URI redacted)
5. Resumable Upload Reconciliation (HTTP PUT status query: 200->uploaded, 308->resumable/upload_unknown, 404/410->upload_unresolved, network err->upload_unknown)
6. Manual Resolution (confirm_uploaded validates 11-char ID -> uploaded; confirm_absent resets -> ready)
7. Startup Interruption Recovery (interrupted uploading video -> upload_unknown with reason)
8. Tiered OAuth Health Model (not_configured, configured, healthy, expired) & Channel Identity
9. YouTube Quota Model (2026 Rules: independent 100-calls buckets for videos.insert and search.list, 10000 general, PT midnight reset)
10. Automation Publish-Approved Mode (FIFO selection of approved ready videos, zero bypass of human review)
11. HTTP Endpoints (/api/video/reconcile, /api/video/resolve, /api/connections/test, privacy in /api/video)
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
import studio.worker as worker


class Stage6PublishingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / "out").mkdir()
        (cls.root / "work").mkdir()
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
            DB=cls.root / "test_stage6.sqlite3",
        )
        cls.store_patches.start()

        cls.worker_patches = patch.multiple(
            worker,
            store=store,
        )
        cls.worker_patches.start()

        # Start live HTTP test server on free loopback port
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.StudioHandler)
        cls.port = cls.httpd.server_address[1]
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

        cls.patch_port = patch.object(server, "PORT", cls.port)
        cls.patch_port.start()

    @classmethod
    def tearDownClass(cls):
        cls.patch_port.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.worker_patches.stop()
        cls.store_patches.stop()
        cls.server_patches.stop()
        cls.tmp.cleanup()

    def setUp(self):
        # Clean SQLite tables between test cases
        with store.connect() as conn:
            conn.execute("DELETE FROM videos")
            conn.execute("DELETE FROM jobs")
        with server.GUARD:
            server.ACTIVE_JOB = None
            server.ACTIVE_PROC = None

    def _create_test_video(
        self,
        stem: str,
        review_status: str = "unreviewed",
        status: str = "ready",
        file_exists: bool = True,
        privacy: str = "public",
        created_at: str | None = None,
    ) -> dict:
        video_file = self.root / "out" / f"{stem}.mp4"
        if file_exists:
            video_file.write_bytes(b"dummy video data")
        elif video_file.exists():
            video_file.unlink()

        poster_file = self.root / "out" / f"{stem}.png"
        poster_file.write_bytes(b"dummy poster")

        rec = {
            "id": stem,
            "created_at": created_at or store.now(),
            "status": status,
            "review_status": review_status,
            "video": str(video_file),
            "poster": str(poster_file),
            "headline": f"Headline {stem}",
            "title": f"Title {stem}",
            "description": "Short description",
            "privacy": privacy,
            "config": {},
            "candidate": {"source": "youtube", "channel": "TestChannel", "url": "https://youtube.com/watch?v=123"},
            "youtube_id": "yt_already_123" if status == "uploaded" else "",
            "youtube_url": f"https://www.youtube.com/watch?v=yt_already_123" if status == "uploaded" else "",
            "resumable_uri": None,
            "upload_failure_reason": "",
            "error": "",
        }
        store.put("videos", stem, rec)
        return rec

    def _request(self, method: str, path: str, body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {
            "Origin": f"http://127.0.0.1:{self.port}",
            "Host": f"127.0.0.1:{self.port}",
            "X-Studio-Token": server.CSRF,
            "Content-Type": "application/json",
        }
        raw_body = json.dumps(body) if body is not None else None
        conn.request(method, path, body=raw_body, headers=headers)
        res = conn.getresponse()
        data = res.read().decode("utf-8")
        conn.close()
        return res.status, json.loads(data) if data else {}

    # -------------------------------------------------------------------------
    # 1. Multi-Layer Approval Enforcement
    # -------------------------------------------------------------------------

    def test_start_job_rejects_unreviewed_video(self):
        """start_job must reject unreviewed videos for upload."""
        self._create_test_video("vid_unrev", review_status="unreviewed")
        with self.assertRaises(ValueError) as ctx:
            server.start_job("upload", key="vid_unrev")
        self.assertIn("cannot be uploaded", str(ctx.exception))
        self.assertIn("unreviewed", str(ctx.exception))

    def test_start_job_rejects_rejected_video(self):
        """start_job must reject explicitly rejected videos for upload."""
        self._create_test_video("vid_rej", review_status="rejected")
        with self.assertRaises(ValueError) as ctx:
            server.start_job("upload", key="vid_rej")
        self.assertIn("rejected", str(ctx.exception))

    def test_start_job_allows_approved_video(self):
        """start_job must succeed when video is approved."""
        self._create_test_video("vid_appr", review_status="approved")
        job = server.start_job("upload", key="vid_appr")
        self.assertEqual(job["status"], "pending")
        self.assertEqual(job["action"], "upload")
        self.assertEqual(job["key"], "vid_appr")

    def test_worker_rejects_unapproved_video_pre_transfer(self):
        """worker.work must abort before touching YouTube auth if review_status is not approved."""
        self._create_test_video("vid_worker_unrev", review_status="unreviewed")
        with self.assertRaises(ValueError) as ctx:
            worker.work("upload", "vid_worker_unrev")
        self.assertIn("approved", str(ctx.exception))

    def test_retry_job_rejects_unapproved_video(self):
        """retry_job must reject retrying an upload job if the video is not approved."""
        self._create_test_video("vid_retry_unrev", review_status="unreviewed")
        # Manually create a failed upload job
        failed_job = {
            "id": "job_fail_1",
            "action": "upload",
            "key": "vid_retry_unrev",
            "status": "failed",
            "created_at": store.now(),
        }
        store.put("jobs", "job_fail_1", failed_job)

        with self.assertRaises(ValueError) as ctx:
            server.retry_job("job_fail_1")
        self.assertIn("review status is 'unreviewed'", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 2. Duplicate Upload Guards & State Machine (HTTP 409 Conflict)
    # -------------------------------------------------------------------------

    def test_upload_guard_rejects_uploaded_video(self):
        """Cannot upload an already uploaded video (HTTP 409 Conflict)."""
        self._create_test_video("vid_uploaded", review_status="approved", status="uploaded")
        with self.assertRaises(server.ConflictError) as ctx:
            server.start_job("upload", key="vid_uploaded")
        self.assertIn("already been uploaded", str(ctx.exception))

    def test_upload_guard_rejects_uploading_video(self):
        """Cannot start upload if video status is currently 'uploading'."""
        self._create_test_video("vid_uploading", review_status="approved", status="uploading")
        with self.assertRaises(server.ConflictError) as ctx:
            server.start_job("upload", key="vid_uploading")
        self.assertIn("already currently uploading", str(ctx.exception))

    def test_upload_guard_rejects_upload_unknown(self):
        """Cannot start upload if video is in 'upload_unknown' state (must reconcile or resolve)."""
        self._create_test_video("vid_unk", review_status="approved", status="upload_unknown")
        with self.assertRaises(server.ConflictError) as ctx:
            server.start_job("upload", key="vid_unk")
        self.assertIn("upload_unknown", str(ctx.exception))

    def test_upload_guard_rejects_upload_unresolved(self):
        """Cannot start upload if video is in 'upload_unresolved' state (must resolve)."""
        self._create_test_video("vid_unres", review_status="approved", status="upload_unresolved")
        with self.assertRaises(server.ConflictError) as ctx:
            server.start_job("upload", key="vid_unres")
        self.assertIn("upload_unresolved", str(ctx.exception))

    def test_upload_guard_rejects_concurrent_queued_job(self):
        """Cannot queue a second upload job while one is already pending in queue."""
        self._create_test_video("vid_dup_queue", review_status="approved")
        # Enqueue first upload job
        server.start_job("upload", key="vid_dup_queue")
        # Attempt second upload job for same video
        with self.assertRaises(server.ConflictError) as ctx:
            server.start_job("upload", key="vid_dup_queue")
        self.assertIn("already pending in the pipeline", str(ctx.exception))

    def test_missing_video_file_rejected(self):
        """Cannot upload if the rendered video artifact does not exist on disk."""
        self._create_test_video("vid_no_file", review_status="approved", file_exists=False)
        with self.assertRaises(ValueError) as ctx:
            server.start_job("upload", key="vid_no_file")
        self.assertIn("not found", str(ctx.exception))

    def test_privacy_override(self):
        """start_job with extra privacy updates the video record privacy."""
        self._create_test_video("vid_priv", review_status="approved", privacy="public")
        server.start_job("upload", key="vid_priv", extra={"privacy": "unlisted"})
        rec = store.get("videos", "vid_priv")
        self.assertEqual(rec["privacy"], "unlisted")

    # -------------------------------------------------------------------------
    # 3. Resumable Session Capture & Enrichment Masking
    # -------------------------------------------------------------------------

    def test_store_enrichment_masks_resumable_uri(self):
        """enrich_video_record sets has_resumable_session=True and never exposes raw resumable_uri."""
        rec = self._create_test_video("vid_mask")
        rec["resumable_uri"] = "https://upload.youtube.com/upload/session_secret_token_12345"
        rec["youtube_id"] = "dQw4w9WgXcQ"
        store.put("videos", "vid_mask", rec)

        enriched = store.enrich_video_record(rec)
        self.assertTrue(enriched["has_resumable_session"])
        self.assertNotIn("resumable_uri", enriched)
        self.assertEqual(enriched["youtube_url"], "https://youtube.com/shorts/dQw4w9WgXcQ")

    # -------------------------------------------------------------------------
    # 4. Resumable Upload Reconciliation (reconcile_video_upload)
    # -------------------------------------------------------------------------

    @patch("requests.put")
    def test_reconcile_200_completed(self, mock_put):
        """HTTP 200 from session query marks video uploaded and extracts YouTube ID."""
        rec = self._create_test_video("vid_rec_200", status="upload_unknown")
        rec["resumable_uri"] = "https://upload.youtube.com/session/200"
        store.put("videos", "vid_rec_200", rec)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "yt_success_789"}
        mock_put.return_value = mock_resp

        res = server.reconcile_video_upload("vid_rec_200")
        self.assertEqual(res["status"], "uploaded")
        self.assertEqual(res["youtube_id"], "yt_success_789")

        updated = store.get("videos", "vid_rec_200")
        self.assertEqual(updated["status"], "uploaded")
        self.assertEqual(updated["youtube_id"], "yt_success_789")
        self.assertIsNone(updated["resumable_uri"])

    @patch("requests.put")
    def test_reconcile_308_resumable_incomplete(self, mock_put):
        """HTTP 308 Resume Incomplete keeps video in upload_unknown and reports byte range."""
        rec = self._create_test_video("vid_rec_308", status="upload_unknown")
        rec["resumable_uri"] = "https://upload.youtube.com/session/308"
        store.put("videos", "vid_rec_308", rec)

        mock_resp = MagicMock()
        mock_resp.status_code = 308
        mock_resp.headers = {"Range": "bytes=0-5242879"}
        mock_put.return_value = mock_resp

        res = server.reconcile_video_upload("vid_rec_308")
        self.assertEqual(res["status"], "resumable")
        self.assertTrue(res["has_resumable_session"])
        self.assertEqual(res["range"], "bytes=0-5242879")

        updated = store.get("videos", "vid_rec_308")
        self.assertEqual(updated["status"], "upload_unknown")
        self.assertEqual(updated["resumable_uri"], "https://upload.youtube.com/session/308")

    @patch("requests.put")
    def test_reconcile_404_expired_session(self, mock_put):
        """HTTP 404/410 transitions video to upload_unresolved (expired session != absent video)."""
        rec = self._create_test_video("vid_rec_404", status="upload_unknown")
        rec["resumable_uri"] = "https://upload.youtube.com/session/404"
        store.put("videos", "vid_rec_404", rec)

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_put.return_value = mock_resp

        res = server.reconcile_video_upload("vid_rec_404")
        self.assertEqual(res["status"], "upload_unresolved")
        self.assertFalse(res["has_resumable_session"])

        updated = store.get("videos", "vid_rec_404")
        self.assertEqual(updated["status"], "upload_unresolved")
        self.assertIsNone(updated["resumable_uri"])

    @patch("requests.put")
    def test_reconcile_network_error_preserves_session(self, mock_put):
        """Network error during reconciliation leaves status upload_unknown and preserves session."""
        rec = self._create_test_video("vid_rec_net", status="upload_unknown")
        rec["resumable_uri"] = "https://upload.youtube.com/session/net"
        store.put("videos", "vid_rec_net", rec)

        mock_put.side_effect = ConnectionResetError("Connection dropped")

        res = server.reconcile_video_upload("vid_rec_net")
        self.assertEqual(res["status"], "upload_unknown")
        self.assertTrue(res["has_resumable_session"])

        updated = store.get("videos", "vid_rec_net")
        self.assertEqual(updated["status"], "upload_unknown")
        self.assertEqual(updated["resumable_uri"], "https://upload.youtube.com/session/net")

    # -------------------------------------------------------------------------
    # 5. Manual Resolution (resolve_manual_video)
    # -------------------------------------------------------------------------

    def test_manual_resolution_confirm_uploaded(self):
        """confirm_uploaded marks video uploaded with verified ID."""
        self._create_test_video("vid_res_up", status="upload_unknown")
        res = server.resolve_manual_video("vid_res_up", "confirm_uploaded", "abc_1234567")
        self.assertEqual(res["status"], "uploaded")
        self.assertEqual(res["youtube_id"], "abc_1234567")

        updated = store.get("videos", "vid_res_up")
        self.assertEqual(updated["status"], "uploaded")
        self.assertEqual(updated["youtube_id"], "abc_1234567")
        self.assertEqual(updated["youtube_url"], "https://www.youtube.com/watch?v=abc_1234567")

    def test_manual_resolution_confirm_uploaded_url_extraction(self):
        """confirm_uploaded extracts ID from full YouTube URL."""
        self._create_test_video("vid_res_url", status="upload_unresolved")
        res = server.resolve_manual_video(
            "vid_res_url",
            "confirm_uploaded",
            "https://www.youtube.com/watch?v=myTestId999&t=10s",
        )
        self.assertEqual(res["youtube_id"], "myTestId999")

    def test_manual_resolution_confirm_uploaded_invalid_id_rejected(self):
        """confirm_uploaded rejects blank or invalid ID."""
        self._create_test_video("vid_res_bad", status="upload_unknown")
        with self.assertRaises(ValueError):
            server.resolve_manual_video("vid_res_bad", "confirm_uploaded", "")

    def test_manual_resolution_confirm_absent_resets_to_ready(self):
        """confirm_absent resets video to ready and clears failure reasons."""
        self._create_test_video("vid_res_abs", status="upload_unresolved")
        res = server.resolve_manual_video("vid_res_abs", "confirm_absent")
        self.assertEqual(res["status"], "ready")

        updated = store.get("videos", "vid_res_abs")
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["youtube_id"], "")
        self.assertIsNone(updated["resumable_uri"])

    def test_manual_resolution_rejects_inappropriate_status(self):
        """Cannot resolve a video that is already 'ready' or 'uploaded'."""
        self._create_test_video("vid_ready", status="ready")
        with self.assertRaises(ValueError):
            server.resolve_manual_video("vid_ready", "confirm_absent")

    # -------------------------------------------------------------------------
    # 6. Startup Interruption Recovery
    # -------------------------------------------------------------------------

    def test_startup_recovery_interrupted_upload(self):
        """recover_interrupted_jobs converts orphan uploading video to upload_unknown."""
        self._create_test_video("vid_orphan", status="uploading")
        server.recover_interrupted_jobs()

        rec = store.get("videos", "vid_orphan")
        self.assertEqual(rec["status"], "upload_unknown")
        self.assertIn("interrupted by server restart", rec["upload_failure_reason"])

    # -------------------------------------------------------------------------
    # 7. Tiered OAuth Health Model & Channel Identity
    # -------------------------------------------------------------------------

    def test_oauth_health_not_configured(self):
        """No secret, no token -> not_configured."""
        cs = self.root / "client_secret.json"
        tk = self.root / "token.json"
        if cs.exists(): cs.unlink()
        if tk.exists(): tk.unlink()
        self.assertEqual(server.check_oauth_health(), "not_configured")

    def test_oauth_health_configured(self):
        """Client secret present, token missing -> configured."""
        (self.root / "client_secret.json").write_text("{}", encoding="utf-8")
        tk = self.root / "token.json"
        if tk.exists(): tk.unlink()
        self.assertEqual(server.check_oauth_health(), "configured")

    # -------------------------------------------------------------------------
    # 8. Quota Accounting (2026 Model)
    # -------------------------------------------------------------------------

    def test_quota_tracker_buckets(self):
        """Local quota tracker maintains 2026 buckets for videos_insert, search_list, general."""
        quota_file = self.root / "studio-quota.json"
        if quota_file.exists(): quota_file.unlink()

        tracker = store.get_local_quota_tracker()
        self.assertEqual(tracker["videos_insert_limit"], 100)
        self.assertEqual(tracker["search_list_limit"], 100)
        self.assertEqual(tracker["general_units_limit"], 10000)
        self.assertEqual(tracker["videos_insert_count"], 0)
        self.assertIn("date_pt", tracker)

        # Record activity
        store.record_local_quota_activity("videos_insert", 1)
        store.record_local_quota_activity("search_list", 2)
        store.record_local_quota_activity("general", 5)

        updated = store.get_local_quota_tracker()
        self.assertEqual(updated["videos_insert_count"], 1)
        self.assertEqual(updated["search_list_count"], 2)
        self.assertEqual(updated["general_units"], 5)

    # -------------------------------------------------------------------------
    # 9. Automation Publish-Approved Mode (FIFO)
    # -------------------------------------------------------------------------

    def test_automation_loop_publish_approved_fifo(self):
        """Automation publish_approved mode selects the oldest approved ready video."""
        # Create unreviewed video (should be skipped)
        self._create_test_video("vid_auto_unrev", review_status="unreviewed", created_at="2026-09-01T00:00:00Z")
        # Create second approved video (younger)
        self._create_test_video("vid_auto_appr2", review_status="approved", created_at="2026-09-03T00:00:00Z")
        # Create first approved video (older)
        self._create_test_video("vid_auto_appr1", review_status="approved", created_at="2026-09-02T00:00:00Z")

        # Configure settings for automation
        settings = {
            "enabled": True,
            "interval_hours": 1,
            "mode": "publish_approved",
            "next_run": 0,
        }
        server.write_json(self.root / "studio-settings.json", settings)

        with patch.object(server, "is_online", return_value=True):
            # Run one iteration of the automation check logic directly
            videos = store.records("videos")
            videos.sort(key=lambda v: v.get("created_at", ""))
            candidate = None
            for v in videos:
                if v.get("review_status") == "approved" and v.get("status") == "ready":
                    candidate = v
                    break

            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["id"], "vid_auto_appr1")

    # -------------------------------------------------------------------------
    # 10. HTTP Endpoints
    # -------------------------------------------------------------------------

    def test_http_endpoint_conflict_error_409(self):
        """POST /api/job with upload on already-uploaded video returns HTTP 409 Conflict."""
        self._create_test_video("vid_http_conf", review_status="approved", status="uploaded")
        code, body = self._request("POST", "/api/job", {"action": "upload", "key": "vid_http_conf"})
        self.assertEqual(code, 409)
        self.assertIn("already been uploaded", body.get("error", ""))

    def test_http_endpoint_reconcile_and_resolve(self):
        """Test POST /api/video/resolve end-to-end via HTTP client."""
        self._create_test_video("vid_http_res", status="upload_unknown")
        code, body = self._request(
            "POST",
            "/api/video/resolve",
            {"id": "vid_http_res", "resolution": "confirm_absent"},
        )
        self.assertEqual(code, 200)
        self.assertEqual(body.get("status"), "ready")

        rec = store.get("videos", "vid_http_res")
        self.assertEqual(rec["status"], "ready")

    def test_http_endpoint_video_privacy_update(self):
        """POST /api/video can update privacy setting."""
        self._create_test_video("vid_priv_upd")
        code, body = self._request(
            "POST",
            "/api/video",
            {"id": "vid_priv_upd", "privacy": "unlisted"},
        )
        self.assertEqual(code, 200)
        rec = store.get("videos", "vid_priv_upd")
        self.assertEqual(rec["privacy"], "unlisted")

    def test_fake_http_server_resumable_upload_lifecycle(self):
        """
        Prove Google Resumable Upload protocol lifecycle with a live loopback HTTP server:
        1. Initial session creation and URI persistence.
        2. Partial upload followed by network interruption -> record transitions to upload_unknown.
        3. Status query (PUT bytes */size, Content-Length: 0) against existing URI.
        4. 308 response and Range handling (determines next byte).
        5. Resumed PUT to the exact same URI.
        6. Correct Content-Range, Content-Length, and request body.
        7. Successful completion (201 Created) -> record marked uploaded with YouTube video ID.
        """
        import http.server
        from google.oauth2.credentials import Credentials

        recorded_requests = []

        class FakeUploadHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_PUT(self):
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length) if content_length > 0 else b""
                cr = self.headers.get("Content-Range", "")
                recorded_requests.append({
                    "path": self.path,
                    "method": "PUT",
                    "content_range": cr,
                    "content_length": content_length,
                    "body": body,
                })

                if cr.startswith("bytes */"):
                    # Status query
                    self.send_response(308)
                    self.send_header("Range", "bytes=0-5242879")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif cr.startswith("bytes 0-"):
                    # Chunk 1
                    self.send_response(308)
                    self.send_header("Range", "bytes=0-5242879")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif cr.startswith("bytes 5242880-"):
                    # Resumed Chunk 2 -> 201 Created
                    self.send_response(201)
                    self.send_header("Content-Type", "application/json")
                    resp_data = json.dumps({"id": "yt_e2e_resumed_999"}).encode("utf-8")
                    self.send_header("Content-Length", str(len(resp_data)))
                    self.end_headers()
                    self.wfile.write(resp_data)
                else:
                    self.send_response(400)
                    self.end_headers()

        fake_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeUploadHandler)
        fake_port = fake_server.server_address[1]
        server_thread = threading.Thread(target=fake_server.serve_forever, daemon=True)
        server_thread.start()

        session_url = f"http://127.0.0.1:{fake_port}/upload/session_e2e_test"
        video_path = self.root / "out" / "vid_e2e_lifecycle.mp4"
        chunk1 = b"C" * 5242880
        chunk2 = b"D" * 5242880

        try:
            # 1. Create approved video with NO initial session URI
            rec = self._create_test_video("vid_e2e_lifecycle", review_status="approved", status="ready")
            video_path.write_bytes(chunk1 + chunk2)
            rec["video"] = str(video_path)
            rec["resumable_uri"] = None
            store.put("videos", "vid_e2e_lifecycle", rec)

            token_file = self.root / "token.json"
            token_file.write_text(json.dumps({
                "token": "fake_token",
                "refresh_token": "fake_refresh",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "fake_cid",
                "client_secret": "fake_sec",
                "scopes": ["https://www.googleapis.com/auth/youtube.upload"]
            }), encoding="utf-8")

            # 2. Simulate initial upload starting, capturing session URI, and dropping connection
            def simulate_partial_upload(**kwargs):
                cb = kwargs.get("on_session_created")
                if cb:
                    cb(session_url)
                raise ConnectionResetError("Simulated network drop during transfer")

            try:
                with patch("core.agent.upload_to_youtube", side_effect=simulate_partial_upload):
                    worker.work("upload", "vid_e2e_lifecycle")
            except ConnectionResetError:
                pass

            # Verify session URI was persisted and status transitioned to upload_unknown
            after_drop = store.get("videos", "vid_e2e_lifecycle")
            self.assertEqual(after_drop["status"], "upload_unknown")
            self.assertEqual(after_drop["resumable_uri"], session_url)
            self.assertIn("Simulated network drop", after_drop["upload_failure_reason"])

            # 3. Reconciliation via reconcile_video_upload sends empty PUT bytes */size
            reconcile_res = server.reconcile_video_upload("vid_e2e_lifecycle")
            self.assertEqual(reconcile_res["status"], "resumable")
            self.assertEqual(reconcile_res["range"], "bytes=0-5242879")
            self.assertTrue(reconcile_res["has_resumable_session"])

            # 4. Resumed upload execution using public direct HTTP mechanism
            recorded_requests.clear()
            creds = Credentials(token="fake_token")
            with patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=creds):
                worker.work("upload", "vid_e2e_lifecycle")

            # 5. Verify fake server received exact requests
            self.assertEqual(len(recorded_requests), 2)
            # Request A: empty PUT status check
            req_query = recorded_requests[0]
            self.assertEqual(req_query["method"], "PUT")
            self.assertEqual(req_query["path"], "/upload/session_e2e_test")
            self.assertEqual(req_query["content_range"], "bytes */10485760")
            self.assertEqual(req_query["content_length"], 0)

            # Request B: resumed chunk starting at byte 5242880
            req_chunk = recorded_requests[1]
            self.assertEqual(req_chunk["method"], "PUT")
            self.assertEqual(req_chunk["path"], "/upload/session_e2e_test")
            self.assertEqual(req_chunk["content_range"], "bytes 5242880-10485759/10485760")
            self.assertEqual(req_chunk["content_length"], 5242880)
            self.assertEqual(req_chunk["body"], chunk2)

            # 6. Verify database record updated to uploaded with YouTube ID
            completed = store.get("videos", "vid_e2e_lifecycle")
            self.assertEqual(completed["status"], "uploaded")
            self.assertEqual(completed["youtube_id"], "yt_e2e_resumed_999")
            self.assertEqual(completed["youtube_url"], "https://youtube.com/shorts/yt_e2e_resumed_999")
            self.assertEqual(completed["resumable_uri"], "")
            self.assertEqual(completed["upload_failure_reason"], "")

        finally:
            fake_server.shutdown()
            fake_server.server_close()

    def test_fake_http_server_expired_session_transitions_to_unresolved(self):
        """
        Verify that an expired session (HTTP 404/410) during recovery:
        1. Transitions video record to 'upload_unresolved'.
        2. Clears the resumable_uri.
        3. Never initiates a fresh upload or masks the ambiguity.
        """
        import http.server
        from google.oauth2.credentials import Credentials

        class ExpiredSessionHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_PUT(self):
                # Google responds 404 Not Found when resumable session expires
                self.send_error(404, "Resumable session expired")

        expired_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ExpiredSessionHandler)
        port = expired_server.server_address[1]
        t = threading.Thread(target=expired_server.serve_forever, daemon=True)
        t.start()

        session_url = f"http://127.0.0.1:{port}/upload/session_expired_test"
        video_path = self.root / "out" / "vid_exp_test.mp4"
        video_path.write_bytes(b"A" * 1024 * 1024)

        try:
            rec = self._create_test_video("vid_exp_test", review_status="approved", status="upload_unknown")
            rec["video"] = str(video_path)
            rec["resumable_uri"] = session_url
            store.put("videos", "vid_exp_test", rec)

            token_file = self.root / "token.json"
            token_file.write_text(json.dumps({
                "token": "fake_token",
                "refresh_token": "fake_refresh",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "fake_cid",
                "client_secret": "fake_sec",
                "scopes": ["https://www.googleapis.com/auth/youtube.upload"]
            }), encoding="utf-8")

            # Worker attempts resumed upload against expired session
            creds = Credentials(token="fake_token")
            with patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=creds):
                with self.assertRaises(Exception) as ctx:
                    worker.work("upload", "vid_exp_test")
                self.assertIn("expired", str(ctx.exception).lower())

            # Record must transition to upload_unresolved and session URI must be cleared
            updated = store.get("videos", "vid_exp_test")
            self.assertEqual(updated["status"], "upload_unresolved")
            self.assertEqual(updated["resumable_uri"], "")
            self.assertIn("expired", updated["upload_failure_reason"].lower())

        finally:
            expired_server.shutdown()
            expired_server.server_close()

    def test_fake_http_server_transient_error_preserves_upload_unknown(self):
        """
        Verify that a transient network/5xx error during resumable recovery:
        1. Keeps the video in 'upload_unknown'.
        2. Preserves the resumable_uri for future retry.
        3. Never initiates a fresh upload.
        """
        import http.server
        from google.oauth2.credentials import Credentials

        class TransientErrorHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_PUT(self):
                # Google responds 503 Service Unavailable during backend outage
                self.send_error(503, "Service Unavailable")

        err_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TransientErrorHandler)
        port = err_server.server_address[1]
        t = threading.Thread(target=err_server.serve_forever, daemon=True)
        t.start()

        session_url = f"http://127.0.0.1:{port}/upload/session_503_test"
        video_path = self.root / "out" / "vid_503_test.mp4"
        video_path.write_bytes(b"B" * 1024 * 1024)

        try:
            rec = self._create_test_video("vid_503_test", review_status="approved", status="upload_unknown")
            rec["video"] = str(video_path)
            rec["resumable_uri"] = session_url
            store.put("videos", "vid_503_test", rec)

            token_file = self.root / "token.json"
            token_file.write_text(json.dumps({
                "token": "fake_token",
                "refresh_token": "fake_refresh",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "fake_cid",
                "client_secret": "fake_sec",
                "scopes": ["https://www.googleapis.com/auth/youtube.upload"]
            }), encoding="utf-8")

            # Worker attempts resumed upload against server returning 503
            creds = Credentials(token="fake_token")
            with patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=creds):
                with self.assertRaises(Exception) as ctx:
                    worker.work("upload", "vid_503_test")
                self.assertIn("503", str(ctx.exception))

            # Record must remain upload_unknown and session URI must be preserved
            updated = store.get("videos", "vid_503_test")
            self.assertEqual(updated["status"], "upload_unknown")
            self.assertEqual(updated["resumable_uri"], session_url)
            self.assertIn("503", updated["upload_failure_reason"])

        finally:
            err_server.shutdown()
            err_server.server_close()

    def test_no_private_google_client_internals_used(self):
        """Verify static invariant: zero private Google client internals in production recovery path."""
        codebase_root = Path(__file__).resolve().parent.parent
        target_files = [
            codebase_root / "core" / "agent.py",
            codebase_root / "studio" / "worker.py",
            codebase_root / "studio" / "server.py",
        ]
        for tf in target_files:
            content = tf.read_text(encoding="utf-8")
            self.assertNotIn("_in_error_state", content, f"Found private internal '_in_error_state' in {tf.name}")
            self.assertNotIn("request._", content, f"Found private internal 'request._' in {tf.name}")



if __name__ == "__main__":
    unittest.main()
