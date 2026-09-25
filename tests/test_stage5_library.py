"""
tests/test_stage5_library.py — Comprehensive tests for Stage 5: Video Library & Review:
1. Editorial Review State (unreviewed, approved, rejected, strict validation, backward compatibility)
2. Library API & Artifact Health (GET /api/videos, GET /api/video, dynamic health checks)
3. Job-to-Video Matching & Lineage (exact key, summary metadata, target file stem fallback)
4. Deletion Safety & Concurrency Guard (HTTP 409 on active target jobs, out/ containment, raw clip preservation)
5. Review & Re-render Interaction (preserves on queue, resets on successful new render, preserves on failed render)
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


class Stage5LibraryTests(unittest.TestCase):
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
            DB=cls.root / "test_stage5.sqlite3",
        )
        cls.store_patches.start()

        cls.worker_patches = patch.multiple(
            worker,
            store=store,
        )
        cls.worker_patches.start()

        with store.connect():
            pass

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
        cls.port_patch.stop()
        cls.server_patches.stop()
        cls.store_patches.stop()
        cls.worker_patches.stop()
        cls.tmp.cleanup()

    def setUp(self):
        with store.connect() as db:
            db.execute("DELETE FROM videos")
            db.execute("DELETE FROM jobs")

    def _req(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {
            "Host": f"127.0.0.1:{self.port}",
            "Origin": f"http://127.0.0.1:{self.port}",
            "X-Studio-Token": server.CSRF,
            "Content-Type": "application/json",
        }
        raw_body = json.dumps(body) if body is not None else None
        conn.request(method, path, body=raw_body, headers=headers)
        res = conn.getresponse()
        raw = res.read().decode("utf-8")
        data = json.loads(raw) if raw else {}
        conn.close()
        return res.status, data

    # --------------------------------------------------------------------------
    # 1. Review State Tests
    # --------------------------------------------------------------------------

    def test_store_draft_defaults_to_unreviewed(self):
        """Verify store.draft creates records with review_status: 'unreviewed' by default."""
        v_path = self.root / "out" / "short_test_rev1.mp4"
        p_path = self.root / "out" / "short_test_rev1.png"
        rec = store.draft(
            video_stem="short_test_rev1",
            video_path=v_path,
            poster_path=p_path,
            headline="TEST HEADLINE",
            title="Test Title",
            description="Test Desc",
            config={},
        )
        self.assertEqual(rec.get("review_status"), "unreviewed")
        persisted = store.get("videos", "short_test_rev1")
        self.assertEqual(persisted.get("review_status"), "unreviewed")

    def test_enrich_video_record_defaults_missing_review_status(self):
        """Verify backward compatibility: records lacking review_status get 'unreviewed' dynamically."""
        raw_rec = {
            "id": "short_legacy_rev",
            "video": str(self.root / "out" / "short_legacy_rev.mp4"),
            "status": "ready",
            "title": "Legacy Video",
        }
        store.put("videos", "short_legacy_rev", raw_rec)

        # GET /api/video should return review_status: unreviewed
        status, data = self._req("GET", "/api/video?id=short_legacy_rev")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("review_status"), "unreviewed")

    def test_uploaded_video_is_not_automatically_approved(self):
        """Verify mandatory rule: status == 'uploaded' does NOT mean review_status is 'approved'."""
        uploaded_rec = {
            "id": "short_uploaded_test",
            "video": str(self.root / "out" / "short_uploaded_test.mp4"),
            "status": "uploaded",
            "youtube_id": "yt_12345",
        }
        store.put("videos", "short_uploaded_test", uploaded_rec)

        status, data = self._req("GET", "/api/video?id=short_uploaded_test")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("status"), "uploaded")
        self.assertEqual(data.get("review_status"), "unreviewed")

    def test_api_video_updates_review_status_valid(self):
        """Test updating review_status through POST /api/video persists in SQLite."""
        store.put("videos", "vid_rev_mut", {
            "id": "vid_rev_mut",
            "status": "ready",
            "review_status": "unreviewed",
            "title": "Mutable Review Title",
        })

        # Set to approved
        status, data = self._req("POST", "/api/video", {"id": "vid_rev_mut", "review_status": "approved"})
        self.assertEqual(status, 200)
        self.assertEqual(data.get("review_status"), "approved")

        persisted = store.get("videos", "vid_rev_mut")
        self.assertEqual(persisted.get("review_status"), "approved")

        # Set to rejected
        status, data = self._req("POST", "/api/video", {"id": "vid_rev_mut", "review_status": "rejected"})
        self.assertEqual(status, 200)
        self.assertEqual(data.get("review_status"), "rejected")

        # Set back to unreviewed
        status, data = self._req("POST", "/api/video", {"id": "vid_rev_mut", "review_status": "unreviewed"})
        self.assertEqual(status, 200)
        self.assertEqual(data.get("review_status"), "unreviewed")

    def test_api_video_rejects_invalid_review_status(self):
        """Test POST /api/video rejects invalid review_status values with HTTP 400."""
        store.put("videos", "vid_rev_invalid", {
            "id": "vid_rev_invalid",
            "status": "ready",
            "review_status": "unreviewed",
        })

        for invalid_val in ["published", "pending", "ACCEPTED", 123, None, ""]:
            status, data = self._req("POST", "/api/video", {"id": "vid_rev_invalid", "review_status": invalid_val})
            self.assertEqual(status, 400, f"Expected 400 for review_status={invalid_val}")
            self.assertIn("error", data)

    # --------------------------------------------------------------------------
    # 2. Library Listing & Artifact Health Tests
    # --------------------------------------------------------------------------

    def test_artifact_health_detection(self):
        """Verify GET /api/videos dynamically reports video_exists, poster_exists, and raw_exists."""
        v_file = self.root / "out" / "vid_health_intact.mp4"
        p_file = self.root / "out" / "vid_health_intact.png"
        raw_file = self.root / "work" / "vid_health_intact_raw.mp4"

        v_file.write_bytes(b"dummy mp4 content" * 1000)
        p_file.write_bytes(b"dummy png content")
        raw_file.write_bytes(b"dummy raw content")

        store.put("videos", "vid_health_intact", {
            "id": "vid_health_intact",
            "video": str(v_file),
            "poster": str(p_file),
            "raw_video": str(raw_file),
            "status": "ready",
        })

        # Missing files video
        store.put("videos", "vid_health_missing", {
            "id": "vid_health_missing",
            "video": str(self.root / "out" / "nonexistent.mp4"),
            "poster": str(self.root / "out" / "nonexistent.png"),
            "raw_video": str(self.root / "work" / "nonexistent_raw.mp4"),
            "status": "ready",
        })

        status, data = self._req("GET", "/api/videos")
        self.assertEqual(status, 200)

        by_id = {v["id"]: v for v in data}
        intact = by_id["vid_health_intact"]
        self.assertTrue(intact["video_exists"])
        self.assertTrue(intact["poster_exists"])
        self.assertTrue(intact["raw_exists"])
        self.assertGreater(intact["file_size_mb"], 0.0)

        missing = by_id["vid_health_missing"]
        self.assertFalse(missing["video_exists"])
        self.assertFalse(missing["poster_exists"])
        self.assertFalse(missing["raw_exists"])
        self.assertEqual(missing["file_size_mb"], 0.0)

    def test_single_video_detail_includes_source_evidence(self):
        """Verify GET /api/video returns technical source and candidate licensing metadata."""
        store.put("videos", "vid_source_ev", {
            "id": "vid_source_ev",
            "status": "ready",
            "candidate": {
                "source": "youtube",
                "channel": "NASA",
                "url": "https://www.youtube.com/watch?v=mock123",
                "licence": "public-domain",
                "key": "yt_mock123",
                "published_at": 1788892386.0,
            },
        })

        status, data = self._req("GET", "/api/video?id=vid_source_ev")
        self.assertEqual(status, 200)
        cand = data.get("candidate", {})
        self.assertEqual(cand.get("channel"), "NASA")
        self.assertEqual(cand.get("source"), "youtube")
        self.assertEqual(cand.get("licence"), "public-domain")
        self.assertEqual(cand.get("url"), "https://www.youtube.com/watch?v=mock123")

    # --------------------------------------------------------------------------
    # 3. Job-to-Video Matching & Lineage Tests
    # --------------------------------------------------------------------------

    def test_job_matches_video_deterministic_priority(self):
        """Verify job_matches_video deterministic matching rules."""
        # 1. Exact key match for worker job
        j1 = {"id": "j1", "action": "render", "key": "short_test_100"}
        self.assertTrue(server.job_matches_video(j1, "short_test_100"))
        self.assertFalse(server.job_matches_video(j1, "short_other_200"))

        # 2. Explicit summary video_id match
        j2 = {"id": "j2", "action": "preview", "key": "", "summary": {"video_id": "short_test_100"}}
        self.assertTrue(server.job_matches_video(j2, "short_test_100"))
        self.assertFalse(server.job_matches_video(j2, "short_other_200"))

        # 3. Validated target_file stem match
        j3 = {"id": "j3", "action": "run", "key": "", "target_file": str(self.root / "out" / "short_test_100.mp4")}
        self.assertTrue(server.job_matches_video(j3, "short_test_100"))
        self.assertFalse(server.job_matches_video(j3, "short_other_200"))

        # 4. Unrelated job excluded
        j4 = {"id": "j4", "action": "preview", "key": None, "summary": None, "target_file": None}
        self.assertFalse(server.job_matches_video(j4, "short_test_100"))

    def test_get_latest_job_for_video_in_api(self):
        """Verify GET /api/video?id=... resolves latest associated Stage 4 job."""
        vid_id = "short_job_link"
        store.put("videos", vid_id, {"id": vid_id, "status": "ready"})

        # Record older job and newer job
        store.put("jobs", "older_job", {
            "id": "older_job",
            "action": "preview",
            "key": "",
            "summary": {"video_id": vid_id},
            "status": "completed",
            "duration_seconds": 12.5,
            "created_at": "2026-09-20T10:00:00Z",
        })
        store.put("jobs", "newer_job", {
            "id": "newer_job",
            "action": "render",
            "key": vid_id,
            "status": "failed",
            "stage": "Failed: Compositing error",
            "error": "ffmpeg exited with status 1",
            "duration_seconds": 3.2,
            "created_at": "2026-09-20T11:00:00Z",
        })

        status, data = self._req("GET", f"/api/video?id={vid_id}")
        self.assertEqual(status, 200)
        latest = data.get("latest_job")
        self.assertIsNotNone(latest)
        self.assertEqual(latest["id"], "newer_job")
        self.assertEqual(latest["status"], "failed")
        self.assertEqual(latest["duration_seconds"], 3.2)
        self.assertIn("ffmpeg exited with status 1", latest["error"])

    # --------------------------------------------------------------------------
    # 4. Deletion Safety Tests
    # --------------------------------------------------------------------------

    def test_delete_video_succeeds_when_no_active_job(self):
        """Verify video deletion unlinks out/ files and removes record from SQLite."""
        v_file = self.root / "out" / "short_delete_me.mp4"
        p_file = self.root / "out" / "short_delete_me.png"
        raw_file = self.root / "work" / "short_delete_me_raw.mp4"

        v_file.write_bytes(b"dummy")
        p_file.write_bytes(b"dummy")
        raw_file.write_bytes(b"dummy")

        store.put("videos", "short_delete_me", {
            "id": "short_delete_me",
            "video": str(v_file),
            "poster": str(p_file),
            "raw_video": str(raw_file),
            "status": "ready",
        })

        status, data = self._req("POST", "/api/video", {"id": "short_delete_me", "action": "delete"})
        self.assertEqual(status, 200)
        self.assertEqual(data.get("status"), "deleted")

        # Record deleted from SQLite
        self.assertIsNone(store.get("videos", "short_delete_me"))

        # out/ files unlinked
        self.assertFalse(v_file.exists())
        self.assertFalse(p_file.exists())

        # work/ raw clip preserved!
        self.assertTrue(raw_file.exists())

    def test_delete_video_blocked_when_job_active(self):
        """Verify video deletion returns HTTP 409 Conflict if a pending or running job targets it."""
        vid_id = "short_busy_del"
        store.put("videos", vid_id, {
            "id": vid_id,
            "status": "ready",
            "video": str(self.root / "out" / f"{vid_id}.mp4"),
        })

        # Add active pending job targeting vid_id
        store.put("jobs", "active_job_1", {
            "id": "active_job_1",
            "action": "render",
            "key": vid_id,
            "status": "running",
        })

        status, data = self._req("POST", "/api/video", {"id": vid_id, "action": "delete"})
        self.assertEqual(status, 409)
        self.assertIn("error", data)
        self.assertIn("currently running", data["error"])

        # Video record still intact in SQLite
        self.assertIsNotNone(store.get("videos", vid_id))

    def test_delete_video_enforces_out_containment(self):
        """Verify delete action cannot unlink files outside out/ directory."""
        outside_file = self.root / "assets" / "precious.mp4"
        outside_file.write_bytes(b"precious asset")

        store.put("videos", "short_escape_del", {
            "id": "short_escape_del",
            "video": str(outside_file),
            "status": "ready",
        })

        status, data = self._req("POST", "/api/video", {"id": "short_escape_del", "action": "delete"})
        self.assertEqual(status, 200)
        # Outside file must remain alive
        self.assertTrue(outside_file.exists())

    # --------------------------------------------------------------------------
    # 5. Review & Re-render Interaction Tests
    # --------------------------------------------------------------------------

    def test_review_status_not_reset_when_job_merely_queued(self):
        """Verify queueing a render job does not reset review_status before completion."""
        vid_id = "short_approved_render"
        store.put("videos", vid_id, {
            "id": vid_id,
            "status": "ready",
            "review_status": "approved",
            "title": "Approved Title",
        })

        # Queue render job
        job_res = server.start_job("render", vid_id)
        self.assertEqual(job_res["status"], "pending")

        # Video record review_status must still be approved
        rec = store.get("videos", vid_id)
        self.assertEqual(rec["review_status"], "approved")

    def test_successful_render_resets_review_status_to_unreviewed(self):
        """Verify worker successful render resets review_status to 'unreviewed' on new artifact."""
        key = "short_render_reset"
        raw_clip = self.root / "work" / f"{key}_raw.mp4"
        raw_clip.write_bytes(b"dummy raw")

        store.put("videos", key, {
            "id": key,
            "status": "ready",
            "review_status": "approved",
            "headline": "TEST HEADLINE",
            "title": "Approved Title",
            "video": str(self.root / "out" / f"{key}.mp4"),
            "poster": str(self.root / "out" / f"{key}.png"),
            "candidate": {"kind": "clip", "key": "k1"},
            "style_preset": "classic_blue",
        })

        with patch("studio.worker.render.render") as mock_render:
            mock_render.return_value = None
            worker.work("render", key)

        rec = store.get("videos", key)
        self.assertEqual(rec["status"], "ready")
        self.assertEqual(rec["review_status"], "unreviewed")

    def test_failed_render_preserves_previous_review_status(self):
        """Verify worker failed render preserves existing review_status."""
        key = "short_render_fail"
        raw_clip = self.root / "work" / f"{key}_raw.mp4"
        raw_clip.write_bytes(b"dummy raw")

        store.put("videos", key, {
            "id": key,
            "status": "ready",
            "review_status": "approved",
            "headline": "TEST HEADLINE",
            "video": str(self.root / "out" / f"{key}.mp4"),
            "candidate": {"kind": "clip"},
            "style_preset": "classic_blue",
        })

        with patch("studio.worker.render.render", side_effect=RuntimeError("Encoder crash")):
            with self.assertRaises(RuntimeError):
                worker.work("render", key)

        rec = store.get("videos", key)
        self.assertEqual(rec["status"], "render_failed")
        # Review status must remain approved, not unreviewed or rejected
        self.assertEqual(rec["review_status"], "approved")


if __name__ == "__main__":
    unittest.main()
