"""
tests/test_stage7_phase3.py — Stage 7 Phase 3 Tests:
1. Operational Telemetry & Quota Governance:
   - Thread-safe local quota tracker with Pacific Time midnight reset & DST handling.
   - Pre-flight quota guard capping YouTube search at 100 calls/day.
   - Non-search discovery sources continue when search quota exhausted.
   - Quota tracking at request boundaries and 403 quotaExceeded handling.
2. Discovery Diagnostics & Run Telemetry:
   - Detailed candidate yield per source (search, channels, Reddit).
   - Duplicate candidate tracking and seen candidate filtering.
   - Licensing and duration rejection categorization.
   - Evidence-based idle diagnostic explanations.
   - Telemetry emitted in KENAU_SUMMARY.
3. Executive Operational Command Center:
   - Non-blocking, sanitized OAuth health reporting (no secrets).
   - Extended /api/status with quota_tracker, oauth_health, incidents, and latest_discovery.
   - Incident remediation actions (reconcile, resolve, retry) maintain state integrity.
"""
from __future__ import annotations

import datetime as dt
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import agent, render
from core.state import State
import studio.server as server
import studio.store as store


class Stage7Phase3Tests(unittest.TestCase):
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

        cls.db_path = cls.root / "test_stage7_p3.sqlite3"

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

        # Start live test server
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.StudioHandler)
        cls.port = cls.httpd.server_address[1]
        cls.port_patch = patch.object(server, "PORT", cls.port)
        cls.port_patch.start()
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        server.STOP_EVENT.set()
        server.QUEUE_EVENT.set()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.server_thread.join()
        cls.port_patch.stop()
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

        # Reset local quota tracker file
        quota_file = self.root / "studio-quota.json"
        if quota_file.exists():
            try:
                quota_file.unlink()
            except Exception:
                pass

        # Reset token and client secret files
        for f in (self.root / "token.json", self.root / "client_secret.json", self.root / "studio-channel.json"):
            if f.exists():
                try:
                    f.unlink()
                except Exception:
                    pass

    def tearDown(self):
        with server.GUARD:
            if server.ACTIVE_PROC and server.ACTIVE_PROC.poll() is None:
                try:
                    server.ACTIVE_PROC.kill()
                except Exception:
                    pass
            server.ACTIVE_JOB = None
            server.ACTIVE_PROC = None

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if headers:
            h.update(headers)
        raw_body = None
        if body is not None:
            raw_body = json.dumps(body).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        conn.request(method, path, raw_body, h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        try:
            parsed = json.loads(data.decode("utf-8"))
        except Exception:
            parsed = data
        return resp.status, parsed

    # =========================================================================
    # Task 1: Quota Tracking, Pacific Time Calculation & Reset
    # =========================================================================

    def test_pacific_time_dst_calculation(self):
        """Test Pacific Time calculation for both PST (standard) and PDT (daylight)."""
        # Summer date (PDT: UTC-7) - July 15, 2026 12:00:00 UTC
        utc_summer = dt.datetime(2026, 7, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
        pt_summer = store.get_pacific_now(utc_summer)
        self.assertEqual(pt_summer.hour, 5)  # 12 - 7 = 5 AM
        self.assertEqual(store.get_pacific_date(utc_summer), "2026-07-15")

        # Winter date (PST: UTC-8) - January 15, 2026 12:00:00 UTC
        utc_winter = dt.datetime(2026, 1, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
        pt_winter = store.get_pacific_now(utc_winter)
        self.assertEqual(pt_winter.hour, 4)  # 12 - 8 = 4 AM
        self.assertEqual(store.get_pacific_date(utc_winter), "2026-01-15")

        # Border case: 2026-07-15 06:30:00 UTC is 2026-07-14 23:30:00 PDT
        utc_border = dt.datetime(2026, 7, 15, 6, 30, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(store.get_pacific_date(utc_border), "2026-07-14")

    def test_pacific_reset_info(self):
        """Test reset info returns seconds remaining until midnight PT."""
        utc_now = dt.datetime(2026, 7, 15, 20, 0, 0, tzinfo=dt.timezone.utc)
        # 20:00 UTC in PDT (UTC-7) is 13:00 PDT (1:00 PM).
        # Hours until midnight PDT: 11 hours = 39600 seconds.
        reset_info = store.get_pacific_reset_info(utc_now)
        self.assertEqual(reset_info["seconds_remaining"], 11 * 3600)
        self.assertTrue(reset_info["reset_at_iso"].startswith("2026-07-16T00:00:00"))
        self.assertEqual(reset_info["is_dst"], True)

    def test_quota_tracker_initial_and_thread_safe_recording(self):
        """Test recording quota activity increments counters correctly and is thread safe."""
        tracker = store.get_local_quota_tracker()
        self.assertEqual(tracker["search_list_count"], 0)
        self.assertEqual(tracker["videos_insert_count"], 0)
        self.assertEqual(tracker["general_units"], 0)
        self.assertFalse(tracker["search_limit_reached"])
        self.assertIn("reset_at", tracker)
        self.assertIn("disclaimer", tracker)

        # Record activity across multiple concurrent threads
        def worker(action, count):
            for _ in range(10):
                store.record_local_quota_activity(action, count)

        threads = [
            threading.Thread(target=worker, args=("search_list", 1)),
            threading.Thread(target=worker, args=("search_list", 1)),
            threading.Thread(target=worker, args=("videos_insert", 1)),
            threading.Thread(target=worker, args=("general", 5)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final_tracker = store.get_local_quota_tracker()
        self.assertEqual(final_tracker["search_list_count"], 20)
        self.assertEqual(final_tracker["videos_insert_count"], 10)
        self.assertEqual(final_tracker["general_units"], 50)
        self.assertFalse(final_tracker["search_limit_reached"])

    def test_quota_tracker_reset_on_new_pacific_day(self):
        """Test tracker resets counts to 0 when date_pt rolls over to a new day."""
        # Seed tracker with day 1 data
        store.record_local_quota_activity("search_list", 95)
        tracker = store.get_local_quota_tracker()
        self.assertEqual(tracker["search_list_count"], 95)

        # Mock Pacific date to next day
        tomorrow = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).strftime("%Y-%m-%d")
        with patch("studio.store.get_pacific_date", return_value=tomorrow):
            new_tracker = store.get_local_quota_tracker()
            self.assertEqual(new_tracker["date_pt"], tomorrow)
            self.assertEqual(new_tracker["search_list_count"], 0)
            self.assertEqual(new_tracker["videos_insert_count"], 0)
            self.assertEqual(new_tracker["general_units"], 0)
            self.assertFalse(new_tracker["search_limit_reached"])

    def test_search_limit_reached_flag(self):
        """Test search_limit_reached flag is set when search_list_count >= 100."""
        store.record_local_quota_activity("search_list", 100)
        tracker = store.get_local_quota_tracker()
        self.assertEqual(tracker["search_list_count"], 100)
        self.assertTrue(tracker["search_limit_reached"])

    # =========================================================================
    # Task 2: Discovery Diagnostics, Pre-Flight Guard & Telemetry
    # =========================================================================

    def test_youtube_discovery_preflight_blocks_when_quota_exhausted(self):
        """YouTube search is skipped when search quota is reached, recording stats."""
        # Exhaust search quota
        store.record_local_quota_activity("search_list", 100)
        stats = {
            "sources": {},
            "rejected_by_reason": {},
        }

        with patch("requests.get") as mock_get:
            candidates = agent.discover_youtube(["test query"], api_key="dummy_api_key", stats=stats)
            mock_get.assert_not_called()
            self.assertEqual(candidates, [])
            self.assertTrue(stats.get("search_quota_blocked"))
            self.assertEqual(stats["sources"]["youtube_search"]["skipped_quota"], 1)

    def test_youtube_discovery_increments_quota_per_query(self):
        """YouTube search increments quota tracker once per search.list query."""
        initial = store.get_local_quota_tracker()["search_list_count"]
        stats = {
            "sources": {},
            "rejected_by_reason": {},
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "items": [
                {
                    "id": {"videoId": "test_v1"},
                    "snippet": {
                        "title": "Creative Commons Video 1",
                        "description": "A great short video",
                        "channelTitle": "TestChannel",
                        "publishedAt": "2026-07-15T10:00:00Z",
                    }
                }
            ]
        }

        with patch("requests.get", return_value=mock_resp):
            candidates = agent.discover_youtube(["query1", "query2"], api_key="test_api_key", stats=stats)
            self.assertEqual(len(candidates), 2)
            after = store.get_local_quota_tracker()["search_list_count"]
            self.assertEqual(after - initial, 2)
            self.assertEqual(stats["sources"]["youtube_search"]["attempted"], 2)

    def test_youtube_discovery_403_quota_exceeded_handling(self):
        """YouTube 403 quotaExceeded is caught, sets limit reached, and records stat."""
        stats = {
            "sources": {},
            "rejected_by_reason": {},
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = '{"error": {"errors": [{"reason": "quotaExceeded"}]}}'

        with patch("requests.get", return_value=mock_resp):
            candidates = agent.discover_youtube(["query1"], api_key="test_api_key", stats=stats)
            self.assertEqual(candidates, [])
            self.assertTrue(stats.get("search_quota_blocked"))

    def test_non_search_sources_continue_when_search_quota_exhausted(self):
        """Channels and Reddit sources still produce candidates even when search quota is reached."""
        store.record_local_quota_activity("search_list", 100)
        stats = {
            "sources": {},
            "duplicates_across_sources": 0,
            "unique_candidates": 0,
            "seen_candidates": 0,
            "yield": 0,
            "rejected_by_reason": {},
        }

        channel_cand = agent.Candidate(
            kind="clip",
            key="cand_chan_1",
            title="Channel Video",
            url="https://youtube.com/watch?v=cand_chan_1",
            source="youtube_channel",
            channel="TestChan",
            licence="unknown",
            text="Channel description",
        )
        reddit_cand = agent.Candidate(
            kind="story",
            key="cand_red_1",
            title="Reddit Video",
            url="https://v.redd.it/test1",
            source="reddit:test",
            channel="test_sub",
            licence="standard",
            text="Reddit text",
        )

        mock_state = MagicMock()
        mock_state.is_seen.return_value = False

        with patch("core.agent.discover_youtube", return_value=[]):
            with patch("core.agent.discover_youtube_rss", return_value=[channel_cand]):
                with patch("core.agent.discover_reddit", return_value=[reddit_cand]):
                    with patch("core.agent.verify_youtube_licenses", side_effect=lambda cands, *args, **kwargs: cands):
                        candidates = agent.discover_all_candidates(
                            config={"discovery": {"youtube_channels": ["UC123"], "subreddits": ["test"]}},
                            state=mock_state,
                            stats=stats,
                        )
                        self.assertEqual(len(candidates), 2)
                        self.assertEqual(stats["sources"]["youtube_channels"]["candidates"], 1)
                        self.assertEqual(stats["sources"]["reddit"]["candidates"], 1)
                        self.assertEqual(stats["eligible_candidates"], 2)

    def test_discover_all_candidates_duplicate_and_seen_tracking(self):
        """Test cross-source deduplication and seen filtering in discovery stats."""
        stats = {
            "sources": {},
            "duplicates_within_run": 0,
            "unique_candidates": 0,
            "seen_candidates": 0,
            "eligible_candidates": 0,
            "rejected_by_reason": {},
        }

        # cand1 from search, cand1 again from channel (duplicate), cand2 seen already
        cand1 = agent.Candidate(
            kind="clip", key="k1", title="V1", url="https://youtube.com/watch?v=k1",
            source="youtube_search", channel="C1", licence="cc", text="T1",
        )
        cand1_dup = agent.Candidate(
            kind="clip", key="k1", title="V1 dup", url="https://youtube.com/watch?v=k1",
            source="youtube_channel", channel="C1", licence="cc", text="T1 dup",
        )
        cand2 = agent.Candidate(
            kind="story", key="k2", title="V2", url="https://reddit.com/r/test",
            source="reddit:test", channel="sub", licence="standard", text="T2",
        )

        mock_state = MagicMock()
        mock_state.is_seen.side_effect = lambda cid: cid == "k2"  # k2 is already seen

        with patch("core.agent.discover_youtube", return_value=[cand1]):
            with patch("core.agent.discover_youtube_rss", return_value=[cand1_dup]):
                with patch("core.agent.discover_reddit", return_value=[cand2]):
                    with patch("core.agent.verify_youtube_licenses", side_effect=lambda cands, *args, **kwargs: cands):
                        res = agent.discover_all_candidates(
                            config={"discovery": {}},
                            state=mock_state,
                            stats=stats,
                        )
                        # cand1 kept; cand1_dup is duplicate across sources; cand2 is seen
                        self.assertEqual(len(res), 1)
                        self.assertEqual(res[0].key, "k1")
                        self.assertEqual(stats["duplicates_within_run"], 1)
                        self.assertEqual(stats["rejected_by_reason"]["already_seen"], 1)
                        self.assertEqual(stats["unique_candidates"], 2)
                        self.assertEqual(stats["eligible_candidates"], 1)

    def test_evidence_based_idle_explanation(self):
        """Test idle diagnostic explanation generation for various telemetry outcomes."""
        # 1. Quota exceeded with 0 total raw candidates
        stats_quota = {
            "search_quota_blocked": True,
            "total_discovered_raw": 0,
            "unique_candidates": 0,
            "rejected_by_reason": {},
        }
        exp = agent.generate_idle_explanation(stats_quota)
        self.assertIn("YouTube search quota limit reached", exp)

        # 2. All candidates already seen
        stats_seen = {
            "total_discovered_raw": 5,
            "unique_candidates": 5,
            "rejected_by_reason": {"already_seen": 5},
        }
        exp = agent.generate_idle_explanation(stats_seen)
        self.assertIn("previously seen", exp)

        # 3. Licensing / duration rejections
        stats_rej = {
            "total_discovered_raw": 4,
            "unique_candidates": 4,
            "rejected_by_reason": {"all rights reserved": 3, "longer than 90s": 1},
        }
        exp = agent.generate_idle_explanation(stats_rej)
        self.assertIn("rejected", exp)
        self.assertIn("all rights reserved", exp)

        # 4. Zero results found across all configured sources
        stats_zero = {
            "total_discovered_raw": 0,
            "unique_candidates": 0,
            "rejected_by_reason": {},
        }
        exp = agent.generate_idle_explanation(stats_zero)
        self.assertIn("No candidates returned", exp)

    # =========================================================================
    # Task 3: OAuth Health & Server Status Telemetry API
    # =========================================================================

    def test_oauth_health_non_blocking_and_sanitized(self):
        """Test OAuth health check returns structured dict without leaking any secrets."""
        # Case 1: Neither client_secret nor token exists
        health = server.get_oauth_status_details()
        self.assertFalse(health["healthy"])
        self.assertTrue(health["needs_attention"])
        self.assertEqual(health["status"], "not_configured")

        # Verify no credentials leaked
        for forbidden in ["token", "refresh_token", "client_secret", "client_id"]:
            self.assertNotIn(forbidden, health)

        # Case 2: Token present with dummy data and cached channel info
        channel_file = self.root / "studio-channel.json"
        channel_file.write_text(json.dumps({
            "channel_id": "UC123456",
            "channel_title": "Kenau Channel",
            "channel_custom_url": "@kenau",
        }), encoding="utf-8")

        dummy_token = self.root / "token.json"
        dummy_token.write_text(json.dumps({
            "token": "SECRET_ACCESS_TOKEN_XYZ",
            "refresh_token": "SECRET_REFRESH_TOKEN_XYZ",
            "client_id": "SECRET_CLIENT_ID_XYZ",
            "client_secret": "SECRET_CLIENT_SECRET_XYZ",
            "expiry": "2099-01-01T00:00:00Z"
        }), encoding="utf-8")

        with patch("studio.server.check_oauth_health", return_value="healthy"):
            health = server.get_oauth_status_details()
            self.assertTrue(health["healthy"])
            self.assertFalse(health["needs_attention"])
            self.assertEqual(health["channel_title"], "Kenau Channel")
            self.assertEqual(health["channel_id"], "UC123456")
            # Secrets MUST NOT be exposed
            for forbidden in ["SECRET_ACCESS_TOKEN_XYZ", "SECRET_REFRESH_TOKEN_XYZ", "SECRET_CLIENT_ID_XYZ", "SECRET_CLIENT_SECRET_XYZ"]:
                self.assertNotIn(forbidden, str(health))

    def test_server_status_endpoint_extended_payload(self):
        """Test GET /api/status returns legacy fields plus quota_tracker, oauth_health, incidents, and latest_discovery."""
        # Insert test video records representing incidents using store.put
        store.put("videos", "v_unknown_1", {
            "id": "v_unknown_1",
            "title": "Ambiguous Upload Video",
            "status": "upload_unknown",
            "source": "youtube_search",
            "duration": 45,
        })
        store.put("videos", "v_unresolved_1", {
            "id": "v_unresolved_1",
            "title": "Unresolved Video",
            "status": "upload_unresolved",
            "source": "youtube_channel",
            "duration": 30,
        })
        store.put("videos", "v_render_fail_1", {
            "id": "v_render_fail_1",
            "title": "Failed Render Video",
            "status": "render_failed",
            "source": "reddit",
            "duration": 50,
        })
        store.put("videos", "v_uploaded_1", {
            "id": "v_uploaded_1",
            "title": "Normal Uploaded Video",
            "status": "uploaded",
            "source": "youtube_search",
            "duration": 60,
        })

        # Insert a job with discovery stats in summary
        summary_data = {
            "outcome": "idle",
            "explanation": "YouTube search quota limit reached; 0 candidates from other sources",
            "discovery_stats": {
                "search_quota_blocked": True,
                "sources": {"youtube_search": {"attempted": 0, "skipped_quota": 1}},
                "yield": 0,
                "rejected_by_reason": {},
            }
        }
        store.put("jobs", "job_test_diag", {
            "id": "job_test_diag",
            "action": "run",
            "kind": "run",
            "status": "completed",
            "created_at": "2026-07-15 12:00:00",
            "finished_at": "2026-07-15 12:05:00",
            "summary": summary_data,
        })

        status_code, payload = self.request("GET", "/api/status")
        self.assertEqual(status_code, 200)

        # Legacy fields verified
        self.assertIn("csrf", payload)
        self.assertIn("online", payload)
        self.assertIn("free_space_mb", payload)
        self.assertIn("automation", payload)

        # Phase 3 extended fields verified
        self.assertIn("quota_tracker", payload)
        self.assertIn("oauth_health", payload)
        self.assertIn("incidents", payload)
        self.assertIn("latest_discovery", payload)

        # Check incidents content
        incidents = payload["incidents"]
        self.assertEqual(len(incidents), 3)
        incident_ids = [i["id"] for i in incidents]
        self.assertIn("v_unknown_1", incident_ids)
        self.assertIn("v_unresolved_1", incident_ids)
        self.assertIn("v_render_fail_1", incident_ids)
        self.assertNotIn("v_uploaded_1", incident_ids)

        # Check latest discovery content
        self.assertTrue(payload["latest_discovery"].get("search_quota_blocked"))

    def test_incident_remediation_endpoints(self):
        """Test the endpoints triggered by incident remediation action buttons in Overview."""
        store.put("videos", "v_test_resolve", {
            "id": "v_test_resolve",
            "title": "Resolve Me Video",
            "status": "upload_unresolved",
            "source": "youtube_search",
            "duration": 40,
        })

        # Test POST /api/video/resolve to mark it uploaded or failed
        code, resp = self.request("POST", "/api/video/resolve", body={
            "id": "v_test_resolve",
            "resolution": "confirm_absent",
        }, headers={"X-Studio-Token": server.CSRF, "Origin": f"http://127.0.0.1:{self.port}"})

        self.assertEqual(code, 200)
        self.assertEqual(resp.get("status"), "ready")

        # Verify video status in SQLite is now ready
        updated = store.get("videos", "v_test_resolve")
        self.assertEqual(updated["status"], "ready")

    def test_incident_remediation_reconcile_endpoint(self):
        """Test POST /api/video/reconcile endpoint."""
        store.put("videos", "v_test_reconcile", {
            "id": "v_test_reconcile",
            "title": "Reconcile Me Video",
            "status": "upload_unknown",
            "resumable_uri": "https://upload.youtube.com/upload/mock_session",
            "video": "out/test.mp4",
        })

        with patch("studio.server.reconcile_video_upload", return_value={"status": "upload_unknown", "message": "Incomplete"}):
            code, resp = self.request("POST", "/api/video/reconcile", body={
                "id": "v_test_reconcile",
            }, headers={"X-Studio-Token": server.CSRF, "Origin": f"http://127.0.0.1:{self.port}"})
            self.assertEqual(code, 200)
            self.assertEqual(resp.get("status"), "upload_unknown")

    def test_license_check_records_general_units_and_rejects_reasons(self):
        """Test verify_youtube_licenses records general units and tracks rejection reasons."""
        initial_units = store.get_local_quota_tracker()["general_units"]
        stats = {"rejected_by_reason": {}}

        cand1 = agent.Candidate(kind="clip", key="yt_12345678901", title="V1", url="https://youtube.com/watch?v=12345678901", source="youtube_search")
        cand2 = agent.Candidate(kind="clip", key="yt_12345678902", title="V2", url="https://youtube.com/watch?v=12345678902", source="youtube_search")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "items": [
                {
                    "id": "12345678901",
                    "status": {"privacyStatus": "public", "license": "creativeCommon"},
                    "contentDetails": {"duration": "PT20S"},
                    "snippet": {"liveBroadcastContent": "none"},
                },
                {
                    "id": "12345678902",
                    "status": {"privacyStatus": "private", "license": "creativeCommon"},
                    "contentDetails": {"duration": "PT20S"},
                    "snippet": {"liveBroadcastContent": "none"},
                }
            ]
        }

        with patch("requests.get", return_value=mock_resp):
            kept = agent.verify_youtube_licenses([cand1, cand2], api_key="test_api_key", max_seconds=35, stats=stats)
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0].key, "yt_12345678901")

            # Check general units incremented by 1 (1 batch of 50)
            after_units = store.get_local_quota_tracker()["general_units"]
            self.assertEqual(after_units - initial_units, 1)

            # Check rejection reasons tracked
            self.assertEqual(stats["rejected_by_reason"].get("not public"), 1)

    def test_pipeline_summary_emits_discovery_stats(self):
        """Test run_pipeline emits discovery_stats in KENAU_SUMMARY on idle."""
        mock_stdout = io.StringIO()
        cfg_file = self.root / "config.json"
        cfg_file.write_text("{}", encoding="utf-8")
        with patch("sys.stdout", mock_stdout):
            with patch("core.agent.discover_all_candidates", return_value=[]):
                with patch("core.agent.generate_idle_explanation", return_value="Simulated idle explanation"):
                    agent.run_pipeline(cfg_file)

        output = mock_stdout.getvalue()
        self.assertIn("KENAU_SUMMARY ", output)
        summary_line = [line for line in output.splitlines() if line.startswith("KENAU_SUMMARY ")][0]
        summary_obj = json.loads(summary_line[14:])
        self.assertEqual(summary_obj.get("status"), "idle")
        self.assertIn("discovery_stats", summary_obj)
        self.assertIn("message", summary_obj)
        self.assertEqual(summary_obj["message"], "Simulated idle explanation")

    def test_ui_assets_structure_and_contracts(self):
        """Verify UI contracts in app.js and style.css for Command Center."""
        app_js = (Path(__file__).resolve().parent.parent / "studio" / "web" / "app.js").read_text(encoding="utf-8")
        style_css = (Path(__file__).resolve().parent.parent / "studio" / "web" / "style.css").read_text(encoding="utf-8")

        # Command Center elements in app.js
        self.assertIn("loadOverview", app_js)
        self.assertIn("incident-list", app_js)
        self.assertIn("incident-card", app_js)
        self.assertIn("startOverviewCountdown", app_js)
        self.assertIn("clearOverviewCountdown", app_js)
        self.assertIn("reconcileIncident", app_js)
        self.assertIn("resolveIncident", app_js)
        self.assertIn("retryRenderIncident", app_js)
        self.assertIn("overview-countdown-val", app_js)

        # Style classes in style.css
        self.assertIn(".command-grid", style_css)
        self.assertIn(".alert-banner", style_css)
        self.assertIn(".meter-track", style_css)
        self.assertIn(".meter-fill", style_css)
        self.assertIn(".incident-card", style_css)
    def test_incident_resolution_contract_and_state_guards(self):
        """Test incident resolution API with exact frontend payloads, duplicate prevention, and state guards."""
        headers = {"X-Studio-Token": server.CSRF, "Origin": f"http://127.0.0.1:{self.port}"}

        # 1. confirm_uploaded using exact frontend payload { id, action, youtube_id } with YouTube URL
        store.put("videos", "v_resolve_upload_1", {
            "id": "v_resolve_upload_1",
            "title": "Resolve Upload Video",
            "status": "upload_unknown",
            "source": "youtube_search",
            "duration": 45,
            "candidate": {"key": "yt_cand_1"},
        })

        code, resp = self.request("POST", "/api/video/resolve", body={
            "id": "v_resolve_upload_1",
            "action": "confirm_uploaded",
            "youtube_id": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        }, headers=headers)
        self.assertEqual(code, 200)
        self.assertEqual(resp.get("status"), "uploaded")
        self.assertEqual(resp.get("youtube_id"), "dQw4w9WgXcQ")

        # Verify state in SQLite & state.json
        rec = store.get("videos", "v_resolve_upload_1")
        self.assertEqual(rec["status"], "uploaded")
        self.assertEqual(rec["youtube_id"], "dQw4w9WgXcQ")
        state = State(self.root / "state.json")
        self.assertTrue(any(p.get("key") == "yt_cand_1" for p in state.posted))

        # Verify no upload job was enqueued or created in the pipeline
        jobs = store.records("jobs")
        self.assertFalse(any(j.get("action") == "upload" for j in jobs))

        # 2. Idempotent re-confirmation with same ID succeeds
        code, resp = self.request("POST", "/api/video/resolve", body={
            "id": "v_resolve_upload_1",
            "action": "confirm_uploaded",
            "youtube_id": "dQw4w9WgXcQ",
        }, headers=headers)
        self.assertEqual(code, 200)
        self.assertEqual(resp.get("status"), "uploaded")

        # 3. Conflicting re-confirmation with different ID fails with 409 Conflict
        code, resp = self.request("POST", "/api/video/resolve", body={
            "id": "v_resolve_upload_1",
            "action": "confirm_uploaded",
            "youtube_id": "another_id_123",
        }, headers=headers)
        self.assertEqual(code, 409)

        # 4. Attempting to mark already uploaded video absent fails with 409 Conflict
        code, resp = self.request("POST", "/api/video/resolve", body={
            "id": "v_resolve_upload_1",
            "action": "confirm_absent",
            "confirmed": True,
        }, headers=headers)
        self.assertEqual(code, 409)

        # 5. confirm_absent on upload_unknown with explicit declination fails with 400
        store.put("videos", "v_resolve_unknown_1", {
            "id": "v_resolve_unknown_1",
            "title": "Unknown Video",
            "status": "upload_unknown",
            "source": "youtube_search",
        })
        # Explicit declination: confirmed=False -> 400
        code, resp = self.request("POST", "/api/video/resolve", body={
            "id": "v_resolve_unknown_1",
            "action": "confirm_absent",
            "confirmed": False,
        }, headers=headers)
        self.assertEqual(code, 400)
        self.assertIn("Confirmation was explicitly declined", resp.get("error", ""))

        # With confirmation: action=confirm_absent -> 200 and reset to ready
        code, resp = self.request("POST", "/api/video/resolve", body={
            "id": "v_resolve_unknown_1",
            "action": "confirm_absent",
            "confirmed": True,
        }, headers=headers)
        self.assertEqual(code, 200)
        self.assertEqual(resp.get("status"), "ready")
        self.assertEqual(store.get("videos", "v_resolve_unknown_1")["status"], "ready")

        # 6. Resolving a video currently uploading is rejected with 409
        store.put("videos", "v_active_uploading", {
            "id": "v_active_uploading",
            "title": "Uploading Video",
            "status": "uploading",
        })
        code, resp = self.request("POST", "/api/video/resolve", body={
            "id": "v_active_uploading",
            "action": "confirm_absent",
            "confirmed": True,
        }, headers=headers)
        self.assertEqual(code, 409)

        # 7. confirm_uploaded with blank/invalid ID is rejected with 400
        store.put("videos", "v_invalid_id", {
            "id": "v_invalid_id",
            "status": "upload_unresolved",
        })
        code, resp = self.request("POST", "/api/video/resolve", body={
            "id": "v_invalid_id",
            "action": "confirm_uploaded",
            "youtube_id": "   ",
        }, headers=headers)
        self.assertEqual(code, 400)

    def test_persistent_quota_circuit_breaker_and_rollover(self):
        """Test persistent Google quota exhaustion circuit breaker and automatic PT midnight reset."""
        # 1. Trigger quotaExceeded on search
        mock_resp_403 = MagicMock()
        mock_resp_403.status_code = 403
        mock_resp_403.text = "The request cannot be completed because you have exceeded your quotaExceeded."

        stats = {}
        with patch("requests.get", return_value=mock_resp_403):
            res = agent.discover_youtube(["query1", "query2"], api_key="dummy_key", stats=stats)
            self.assertEqual(len(res), 0)

        # Verify circuit breaker is tripped and persisted in studio-quota.json
        tracker = store.get_local_quota_tracker()
        self.assertTrue(tracker["google_quota_exhausted"])
        self.assertTrue(tracker["search_limit_reached"])

        # 2. Subsequent discovery run skips search immediately in pre-flight without HTTP call
        with patch("requests.get") as mock_get:
            stats2 = {}
            res2 = agent.discover_youtube(["query3"], api_key="dummy_key", stats=stats2)
            self.assertEqual(len(res2), 0)
            mock_get.assert_not_called()
            self.assertTrue(stats2.get("search_quota_blocked"))
            self.assertTrue(stats2.get("google_quota_exhausted"))

        # 3. Simulate day rollover to tomorrow Pacific Time
        today_pt = store.get_pacific_date()
        tomorrow_utc = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1, hours=2)
        tomorrow_pt = store.get_pacific_date(tomorrow_utc)
        self.assertNotEqual(today_pt, tomorrow_pt)

        # On tomorrow, tracker resets google_quota_exhausted to False and search_limit_reached to False
        tomorrow_tracker = store.get_local_quota_tracker(now_utc=tomorrow_utc)
        self.assertFalse(tomorrow_tracker["google_quota_exhausted"])
        self.assertFalse(tomorrow_tracker["search_limit_reached"])
        self.assertEqual(tomorrow_tracker["search_list_count"], 0)

    def test_concurrent_quota_reservation_race_prevention(self):
        """Test atomic check_and_reserve_quota prevents concurrent admission beyond search limit."""
        quota_path = self.root / "studio-quota.json"
        today_pt = store.get_pacific_date()
        quota_path.write_text(json.dumps({
            "date_pt": today_pt,
            "search_list_count": 0,
            "search_list_limit": 5,
        }), encoding="utf-8")

        success_count = [0]
        failed_count = [0]
        lock = threading.Lock()

        def try_reserve():
            allowed, _ = store.check_and_reserve_quota("search_list", 1)
            with lock:
                if allowed:
                    success_count[0] += 1
                else:
                    failed_count[0] += 1

        threads = [threading.Thread(target=try_reserve) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(success_count[0], 5)
        self.assertEqual(failed_count[0], 7)
        final_tracker = store.get_local_quota_tracker()
        self.assertEqual(final_tracker["search_list_count"], 5)
        self.assertTrue(final_tracker["search_limit_reached"])

    def test_worker_upload_quota_accounting_at_start(self):
        """Test that worker records videos_insert at actual upload start, but not for resumable uploads."""
        initial_count = store.get_local_quota_tracker()["videos_insert_count"]

        # 1. Fresh upload (no resumable_uri)
        video_dummy = self.root / "out" / "test_upload.mp4"
        video_dummy.write_bytes(b"dummy mp4 data")

        store.put("videos", "v_worker_upload_test", {
            "id": "v_worker_upload_test",
            "title": "Worker Upload Test",
            "status": "ready",
            "video": "out/test_upload.mp4",
        })

        import studio.worker as worker_module

        # Mock agent.upload_to_youtube to succeed
        with patch("core.agent.upload_to_youtube", return_value="yt_uploaded_id_1"):
            worker_module.work("upload", "v_worker_upload_test")

        after_count = store.get_local_quota_tracker()["videos_insert_count"]
        self.assertEqual(after_count - initial_count, 1)

        # 2. Resumable upload (resumable_uri present) -> should NOT increment videos_insert
        store.put("videos", "v_worker_resumable_test", {
            "id": "v_worker_resumable_test",
            "title": "Worker Resumable Test",
            "status": "ready",
            "video": "out/test_upload.mp4",
            "resumable_uri": "https://upload.youtube.com/session/123",
        })

        with patch("core.agent.upload_to_youtube", return_value="yt_uploaded_id_2"):
            worker_module.work("upload", "v_worker_resumable_test")

        after_resumable_count = store.get_local_quota_tracker()["videos_insert_count"]
        self.assertEqual(after_resumable_count, after_count)

    def test_discovery_diagnostics_distinguish_failures_from_empty(self):
        """Test that generate_idle_explanation distinguishes upstream source failures from valid empty results."""
        # Case A: Valid 0 candidates (no failures)
        stats_empty = {
            "total_discovered_raw": 0,
            "unique_candidates": 0,
            "sources": {
                "youtube_search": {"attempted": 2, "failures": 0, "candidates": 0},
                "youtube_channels": {"configured": 1, "failures": 0, "candidates": 0},
            },
            "rejected_by_reason": {},
        }
        exp_empty = agent.generate_idle_explanation(stats_empty)
        self.assertIn("No candidates returned", exp_empty)

        # Case B: Source query failures occurred
        stats_failed = {
            "total_discovered_raw": 0,
            "unique_candidates": 0,
            "sources": {
                "youtube_search": {"attempted": 2, "failures": 2, "candidates": 0},
            },
            "rejected_by_reason": {},
        }
        exp_failed = agent.generate_idle_explanation(stats_failed)
        self.assertIn("Discovery queries failed due to network or upstream API errors (2 failure(s))", exp_failed)

    def test_quota_persistence_resilience_and_corrupt_recovery(self):
        """Test recovery from corrupt or malformed studio-quota.json file."""
        quota_path = self.root / "studio-quota.json"

        # 1. Completely corrupt non-JSON file
        quota_path.write_text("{corrupt-invalid-json", encoding="utf-8")
        tracker = store.get_local_quota_tracker()
        self.assertEqual(tracker["videos_insert_count"], 0)
        self.assertEqual(tracker["search_list_count"], 0)
        self.assertFalse(tracker["google_quota_exhausted"])

        # 2. Corrupt field values (strings, negative numbers)
        quota_path.write_text(json.dumps({
            "date_pt": store.get_pacific_date(),
            "videos_insert_count": "invalid",
            "search_list_count": -50,
            "google_quota_exhausted": True,
        }), encoding="utf-8")

        tracker2 = store.get_local_quota_tracker()
        self.assertEqual(tracker2["videos_insert_count"], 0)
        self.assertEqual(tracker2["search_list_count"], 0)
        self.assertTrue(tracker2["google_quota_exhausted"])


if __name__ == "__main__":
    unittest.main()

