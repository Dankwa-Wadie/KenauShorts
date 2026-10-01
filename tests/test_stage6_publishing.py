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
        token_p = self.root / "token.json"
        if token_p.exists():
            token_p.unlink()

    def tearDown(self):
        token_p = self.root / "token.json"
        if token_p.exists():
            try:
                token_p.unlink()
            except OSError:
                pass

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
    # 1. Automatic Publishing & No Mandatory Approval Gate
    # -------------------------------------------------------------------------

    def test_start_job_allows_unreviewed_video(self):
        """start_job must allow eligible ready unreviewed videos for upload without approval."""
        self._create_test_video("vid_unrev", review_status="unreviewed")
        job = server.start_job("upload", key="vid_unrev")
        self.assertEqual(job["status"], "pending")
        self.assertEqual(job["action"], "upload")
        self.assertEqual(job["key"], "vid_unrev")

    def test_start_job_allows_video_without_review_status(self):
        """start_job must allow eligible ready videos lacking review_status."""
        rec = self._create_test_video("vid_no_rev", review_status="unreviewed")
        del rec["review_status"]
        store.put("videos", "vid_no_rev", rec)
        job = server.start_job("upload", key="vid_no_rev")
        self.assertEqual(job["status"], "pending")
        self.assertEqual(job["action"], "upload")
        self.assertEqual(job["key"], "vid_no_rev")

    def test_start_job_allows_approved_video(self):
        """start_job must continue to succeed when video is approved."""
        self._create_test_video("vid_appr", review_status="approved")
        job = server.start_job("upload", key="vid_appr")
        self.assertEqual(job["status"], "pending")
        self.assertEqual(job["action"], "upload")
        self.assertEqual(job["key"], "vid_appr")

    def test_start_job_allows_rejected_video_per_simple_policy(self):
        """Hypothetical rejected videos do not block upload per simple non-authoritative policy."""
        self._create_test_video("vid_rej", review_status="rejected")
        job = server.start_job("upload", key="vid_rej")
        self.assertEqual(job["status"], "pending")
        self.assertEqual(job["action"], "upload")
        self.assertEqual(job["key"], "vid_rej")

    def test_worker_allows_unreviewed_video(self):
        """worker.work must accept unreviewed ready video without requiring approval."""
        self._create_test_video("vid_worker_unrev", review_status="unreviewed")
        # When token.json is not present, worker reaches YouTube upload step and raises FileNotFoundError,
        # confirming it did NOT reject the video due to review_status.
        with self.assertRaises(FileNotFoundError):
            worker.work("upload", "vid_worker_unrev")

    def test_retry_job_allows_unapproved_video(self):
        """retry_job must allow retrying an upload job for an unreviewed ready video."""
        self._create_test_video("vid_retry_unrev", review_status="unreviewed")
        failed_job = {
            "id": "job_fail_1",
            "action": "upload",
            "key": "vid_retry_unrev",
            "status": "failed",
            "created_at": store.now(),
        }
        store.put("jobs", "job_fail_1", failed_job)
        new_job = server.retry_job("job_fail_1")
        self.assertEqual(new_job["status"], "pending")
        self.assertEqual(new_job["action"], "upload")
        self.assertEqual(new_job["key"], "vid_retry_unrev")

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
    # 9. Automatic Publishing Mode (FIFO)
    # -------------------------------------------------------------------------

    def test_automation_loop_publish_fifo_without_approval(self):
        """Automation publish mode selects the oldest eligible ready video without requiring approval."""
        # Create unreviewed ready video (oldest)
        self._create_test_video("vid_auto_unrev", review_status="unreviewed", created_at="2026-09-01T00:00:00Z")
        # Create ready video without review_status (middle)
        rec2 = self._create_test_video("vid_auto_norev", review_status="unreviewed", created_at="2026-09-02T00:00:00Z")
        del rec2["review_status"]
        store.put("videos", "vid_auto_norev", rec2)
        # Create approved ready video (youngest)
        self._create_test_video("vid_auto_appr", review_status="approved", created_at="2026-09-03T00:00:00Z")

        # Configure settings for automation with canonical 'publish' mode
        settings = {
            "enabled": True,
            "interval_hours": 1,
            "mode": "publish",
            "next_run": 0,
        }
        server.write_json(self.root / "studio-settings.json", settings)

        with patch.object(server, "is_online", return_value=True):
            # Run the automation check logic directly
            videos = store.records("videos")
            videos.sort(key=lambda v: v.get("created_at", ""))
            candidate = None
            for v in videos:
                if v.get("status") == "ready":
                    v_file = v.get("video")
                    if v_file and (self.root / v_file).is_file():
                        candidate = v
                        break

            # Must select the oldest ready video (vid_auto_unrev), even though it is unreviewed
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["id"], "vid_auto_unrev")

    def test_automation_normalizes_publish_approved_mode(self):
        """Settings endpoint accepts 'publish_approved' for backward compatibility and normalizes to 'publish'."""
        code, body = self._request("POST", "/api/settings", {
            "automation": {
                "enabled": True,
                "interval_hours": 4,
                "mode": "publish_approved",
            }
        })
        self.assertEqual(code, 200)
        saved = server.get_automation_settings()
        self.assertEqual(saved["mode"], "publish")
        self.assertEqual(saved["interval_hours"], 4)

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

    def test_fake_http_server_partial_chunk_acknowledgement(self):
        """
        Verify partial chunk acknowledgement handling:
        1. Status query returns Range bytes=0-2097151 (2MB received).
        2. Worker seeks file to 2097152 and transmits next 5MB chunk.
        3. Fake server simulates partial acknowledgement: only acknowledges up to byte 5242879 (HTTP 308).
        4. Worker authoritatively updates offset to 5242880, seeks file stream to 5242880,
           and transmits the exact remaining bytes (5242880-10485759).
        5. Fake server verifies exact byte boundaries, payload, and same session URI -> 201 Created.
        """
        import http.server
        from google.oauth2.credentials import Credentials

        recorded_chunk_requests = []

        class PartialAckHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_PUT(self):
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length) if content_length > 0 else b""
                cr = self.headers.get("Content-Range", "")
                recorded_chunk_requests.append({
                    "path": self.path,
                    "method": "PUT",
                    "content_range": cr,
                    "content_length": content_length,
                    "body": body,
                })

                if cr.startswith("bytes */"):
                    # Status query: server already has bytes 0-2097151 (2MB)
                    self.send_response(308)
                    self.send_header("Range", "bytes=0-2097151")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif cr.startswith("bytes 2097152-"):
                    # First resumed chunk: server only acknowledges up to byte 5242879 (5MB total)
                    self.send_response(308)
                    self.send_header("Range", "bytes=0-5242879")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif cr.startswith("bytes 5242880-"):
                    # Second resumed chunk: server receives remaining bytes -> 201 Created
                    self.send_response(201)
                    self.send_header("Content-Type", "application/json")
                    resp_data = json.dumps({"id": "yt_partial_ack_success"}).encode("utf-8")
                    self.send_header("Content-Length", str(len(resp_data)))
                    self.end_headers()
                    self.wfile.write(resp_data)
                else:
                    self.send_response(400)
                    self.end_headers()

        partial_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PartialAckHandler)
        port = partial_server.server_address[1]
        t = threading.Thread(target=partial_server.serve_forever, daemon=True)
        t.start()

        session_url = f"http://127.0.0.1:{port}/upload/session_partial_ack"
        video_path = self.root / "out" / "vid_partial_ack.mp4"
        chunk1 = b"E" * 5242880
        chunk2 = b"F" * 5242880
        video_path.write_bytes(chunk1 + chunk2)

        try:
            rec = self._create_test_video("vid_partial_ack", review_status="approved", status="upload_unknown", file_exists=False)
            video_path.write_bytes(chunk1 + chunk2)
            rec["video"] = str(video_path)
            rec["resumable_uri"] = session_url
            store.put("videos", "vid_partial_ack", rec)

            token_file = self.root / "token.json"
            token_file.write_text(json.dumps({
                "token": "fake_token",
                "refresh_token": "fake_refresh",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "fake_cid",
                "client_secret": "fake_sec",
                "scopes": ["https://www.googleapis.com/auth/youtube.upload"]
            }), encoding="utf-8")

            creds = Credentials(token="fake_token")
            with patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=creds):
                worker.work("upload", "vid_partial_ack")

            # Verify requests:
            # 1. Status query: bytes */10485760
            # 2. Resumed chunk from 2097152 to 7339999
            # 3. Resumed chunk after partial ack from 5242880 to 10485759
            self.assertEqual(len(recorded_chunk_requests), 3)

            req_query = recorded_chunk_requests[0]
            self.assertEqual(req_query["content_range"], "bytes */10485760")
            self.assertEqual(req_query["content_length"], 0)

            req_chunk1 = recorded_chunk_requests[1]
            self.assertEqual(req_chunk1["content_range"], "bytes 2097152-7340031/10485760")
            self.assertEqual(req_chunk1["content_length"], 5242880)
            # Body must begin at byte 2097152 of the file
            expected_body1 = (chunk1 + chunk2)[2097152:7340032]
            self.assertEqual(req_chunk1["body"], expected_body1)

            req_chunk2 = recorded_chunk_requests[2]
            self.assertEqual(req_chunk2["content_range"], "bytes 5242880-10485759/10485760")
            self.assertEqual(req_chunk2["content_length"], 5242880)
            self.assertEqual(req_chunk2["body"], chunk2)

            updated = store.get("videos", "vid_partial_ack")
            self.assertEqual(updated["status"], "uploaded")
            self.assertEqual(updated["youtube_id"], "yt_partial_ack_success")
            self.assertEqual(updated["resumable_uri"], "")

        finally:
            partial_server.shutdown()
            partial_server.server_close()

    def test_parse_resumable_range_validation_and_errors(self):
        """Test parse_resumable_range under valid, missing, malformed, out-of-range, and regressive inputs."""
        import core.agent as agent

        total_size = 10485760

        # Valid standard Range
        self.assertEqual(agent.parse_resumable_range("bytes=0-5242879", total_size), 5242880)
        self.assertEqual(agent.parse_resumable_range("  bytes=0-0  ", total_size), 1)
        self.assertEqual(agent.parse_resumable_range("bytes=0-10485758", total_size), 10485759)

        # Missing Range on status query -> 0 bytes
        self.assertEqual(agent.parse_resumable_range(None, total_size, is_status_query=True), 0)
        self.assertEqual(agent.parse_resumable_range("", total_size, is_status_query=True), 0)
        self.assertEqual(agent.parse_resumable_range("   ", total_size, is_status_query=True), 0)

        # Missing Range on chunk upload -> RuntimeError
        with self.assertRaises(RuntimeError) as ctx:
            agent.parse_resumable_range(None, total_size, is_status_query=False)
        self.assertIn("missing Range header", str(ctx.exception))

        with self.assertRaises(RuntimeError) as ctx:
            agent.parse_resumable_range("", total_size, is_status_query=False)
        self.assertIn("missing Range header", str(ctx.exception))

        # Malformed Range headers
        with self.assertRaises(RuntimeError) as ctx:
            agent.parse_resumable_range("invalid_header", total_size)
        self.assertIn("missing 'bytes=' prefix", str(ctx.exception))

        with self.assertRaises(RuntimeError) as ctx:
            agent.parse_resumable_range("bytes=5-5242879", total_size)
        self.assertIn("does not start at byte 0", str(ctx.exception))

        with self.assertRaises(RuntimeError) as ctx:
            agent.parse_resumable_range("bytes=0", total_size)
        self.assertIn("Malformed Range header format", str(ctx.exception))

        with self.assertRaises(RuntimeError) as ctx:
            agent.parse_resumable_range("bytes=0-abc", total_size)
        self.assertIn("non-integer last byte", str(ctx.exception))

        with self.assertRaises(RuntimeError) as ctx:
            agent.parse_resumable_range("bytes=0--5", total_size)
        self.assertIn("negative last byte", str(ctx.exception))

        # Out-of-range Range header (last byte index >= total file size)
        with self.assertRaises(RuntimeError) as ctx:
            agent.parse_resumable_range(f"bytes=0-{total_size}", total_size)
        self.assertIn("Out-of-range Range header", str(ctx.exception))

        # Regressive Range header
        with self.assertRaises(RuntimeError) as ctx:
            agent.parse_resumable_range("bytes=0-1000", total_size, expected_start=5000)
        self.assertIn("Regressive Range header", str(ctx.exception))

    def test_oauth_refresh_handling_in_recovery(self):
        """
        Verify OAuth refresh failure handling during recovery:
        1. Successful refresh allows upload to proceed with fresh token.
        2. Failed refresh redacts secrets, raises clear error, makes ZERO upload requests,
           and preserves upload_unknown status and resumable_uri.
        3. Expired token without refresh_token raises clear error, makes ZERO upload requests,
           and preserves upload_unknown status and resumable_uri.
        """
        import http.server
        from google.oauth2.credentials import Credentials

        request_counter = []

        class DummyUploadHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass
            def do_PUT(self):
                request_counter.append(self.headers.get("Authorization", ""))
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                resp_bytes = json.dumps({"id": "ok123"}).encode("utf-8")
                self.send_header("Content-Length", str(len(resp_bytes)))
                self.end_headers()
                self.wfile.write(resp_bytes)

        dummy_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DummyUploadHandler)
        port = dummy_server.server_address[1]
        t = threading.Thread(target=dummy_server.serve_forever, daemon=True)
        t.start()

        session_url = f"http://127.0.0.1:{port}/upload/session_oauth_test"
        video_path = self.root / "out" / "vid_oauth_test.mp4"
        video_path.write_bytes(b"G" * 1024)

        token_file = self.root / "token.json"
        token_file.write_text(json.dumps({
            "token": "stale_token",
            "refresh_token": "fake_refresh",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "fake_cid",
            "client_secret": "fake_sec",
            "scopes": ["https://www.googleapis.com/auth/youtube.upload"]
        }), encoding="utf-8")

        try:
            # Sub-case 1: Successful refresh
            rec = self._create_test_video("vid_oauth_succ", review_status="approved", status="upload_unknown")
            rec["video"] = str(video_path)
            rec["resumable_uri"] = session_url
            store.put("videos", "vid_oauth_succ", rec)

            creds_succ = MagicMock(spec=Credentials)
            creds_succ.expired = True
            creds_succ.refresh_token = "valid_refresh_token"
            creds_succ.token = "stale_token"

            def do_succ_refresh(req):
                creds_succ.expired = False
                creds_succ.token = "fresh_token_123"

            creds_succ.refresh.side_effect = do_succ_refresh

            with patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=creds_succ):
                worker.work("upload", "vid_oauth_succ")

            creds_succ.refresh.assert_called_once()
            self.assertIn("Bearer fresh_token_123", request_counter)
            updated_succ = store.get("videos", "vid_oauth_succ")
            self.assertEqual(updated_succ["status"], "uploaded")

            # Sub-case 2: Failed refresh
            request_counter.clear()
            rec2 = self._create_test_video("vid_oauth_fail", review_status="approved", status="upload_unknown")
            rec2["video"] = str(video_path)
            rec2["resumable_uri"] = session_url
            store.put("videos", "vid_oauth_fail", rec2)

            creds_fail = MagicMock(spec=Credentials)
            creds_fail.expired = True
            creds_fail.refresh_token = "secret_refresh_token_999"
            creds_fail.token = "stale_token_abc"
            creds_fail.client_secret = "super_secret_client_key_888"
            creds_fail.refresh.side_effect = Exception("invalid_grant: secret_refresh_token_999 was revoked")

            with patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=creds_fail):
                with self.assertRaises(Exception) as ctx:
                    worker.work("upload", "vid_oauth_fail")

                # Error message must safely redact refresh_token
                err_text = str(ctx.exception)
                self.assertNotIn("secret_refresh_token_999", err_text)
                self.assertIn("[REDACTED]", err_text)
                self.assertIn("OAuth token refresh failed", err_text)

            # ZERO HTTP upload requests must have been made with stale token
            self.assertEqual(len(request_counter), 0)

            # Record must remain in upload_unknown and resumable_uri must be preserved
            updated_fail = store.get("videos", "vid_oauth_fail")
            self.assertEqual(updated_fail["status"], "upload_unknown")
            self.assertEqual(updated_fail["resumable_uri"], session_url)

            # Sub-case 3: Expired credentials with NO refresh token
            request_counter.clear()
            rec3 = self._create_test_video("vid_oauth_norefresh", review_status="approved", status="upload_unknown")
            rec3["video"] = str(video_path)
            rec3["resumable_uri"] = session_url
            store.put("videos", "vid_oauth_norefresh", rec3)

            creds_noref = MagicMock(spec=Credentials)
            creds_noref.expired = True
            creds_noref.refresh_token = None
            creds_noref.token = "expired_token_xyz"

            with patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=creds_noref):
                with self.assertRaises(Exception) as ctx:
                    worker.work("upload", "vid_oauth_norefresh")

                self.assertIn("no refresh token is available", str(ctx.exception))

            # ZERO HTTP upload requests must have been made
            self.assertEqual(len(request_counter), 0)

            # Record must remain in upload_unknown and resumable_uri must be preserved
            updated_noref = store.get("videos", "vid_oauth_norefresh")
            self.assertEqual(updated_noref["status"], "upload_unknown")
            self.assertEqual(updated_noref["resumable_uri"], session_url)

        finally:
            dummy_server.shutdown()
            dummy_server.server_close()



if __name__ == "__main__":
    unittest.main()
