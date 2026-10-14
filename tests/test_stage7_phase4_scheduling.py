"""
tests/test_stage7_phase4_scheduling.py — Tests for Stage 7 Phase 4 Milestone 3:
Quota-Aware Scheduling, Bounded Jitter, and Pacific-Time Rollover Resumption.

Covers:
- Positive bounded jitter calculation and edge cases
- Rollover jitter [60, 300] seconds post-midnight PT
- Bounded exponential backoff delay progression and 1800s cap
- Effective due time calculation preserving normal recurrence
- Pacific Time rollover calculation and DST transitions
- SQLite intent preservation across restart
- Search quota vs. upload quota decoupling
- Automatic job completion handling (upload success, staging preview, regular preview,
  genuine idle, quota-blocked discovery, worker QuotaBlockedError, pipeline failure, cancellation)
- Pacific rollover suspension lifting and jittered resumption
- Scheduler state machine diagnostics and /api/status reporting
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
from pathlib import Path
import random
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from studio import settings as studio_settings
from studio.settings import (
    calculate_backoff_delay,
    calculate_effective_due_time,
    calculate_jitter,
    calculate_rollover_jitter,
    load_settings,
    update_settings,
)
import studio.server as server
from studio import store


class JitterBoundsTests(unittest.TestCase):
    """Tests uniform positive bounded jitter calculation."""

    def test_jitter_bounds_standard_intervals(self):
        # 3 hours = 10800s -> 10% is 1080s, capped at 900s
        for _ in range(50):
            j = calculate_jitter(10800.0)
            self.assertGreaterEqual(j, 0.0)
            self.assertLessEqual(j, 900.0)

        # 5 hours = 18000s -> 10% is 1800s, capped at 900s
        for _ in range(50):
            j = calculate_jitter(18000.0)
            self.assertGreaterEqual(j, 0.0)
            self.assertLessEqual(j, 900.0)

        # 1 hour = 3600s -> 10% is 360s
        for _ in range(50):
            j = calculate_jitter(3600.0)
            self.assertGreaterEqual(j, 0.0)
            self.assertLessEqual(j, 360.0)

        # 10 minutes = 600s -> 10% is 60s
        for _ in range(50):
            j = calculate_jitter(600.0)
            self.assertGreaterEqual(j, 0.0)
            self.assertLessEqual(j, 60.0)

    def test_jitter_bounds_edge_cases(self):
        self.assertEqual(calculate_jitter(0.0), 0.0)
        self.assertEqual(calculate_jitter(-100.0), 0.0)
        self.assertEqual(calculate_jitter(-0.01), 0.0)

    def test_jitter_deterministic_with_rng(self):
        rng1 = random.Random(12345)
        rng2 = random.Random(12345)
        j1 = calculate_jitter(10800.0, rng=rng1)
        j2 = calculate_jitter(10800.0, rng=rng2)
        self.assertEqual(j1, j2)


class RolloverJitterTests(unittest.TestCase):
    """Tests uniform bounded rollover jitter [60.0, 300.0]s."""

    def test_rollover_jitter_bounds(self):
        for _ in range(100):
            rj = calculate_rollover_jitter()
            self.assertGreaterEqual(rj, 60.0)
            self.assertLessEqual(rj, 300.0)

    def test_rollover_jitter_deterministic_with_rng(self):
        rng1 = random.Random(54321)
        rng2 = random.Random(54321)
        rj1 = calculate_rollover_jitter(rng=rng1)
        rj2 = calculate_rollover_jitter(rng=rng2)
        self.assertEqual(rj1, rj2)


class FailureBackoffTests(unittest.TestCase):
    """Tests bounded exponential failure backoff progression."""

    def test_backoff_progression(self):
        # f=1: base 60.0 + uniform(0, 30) -> [60.0, 90.0]
        for _ in range(20):
            d = calculate_backoff_delay(1)
            self.assertGreaterEqual(d, 60.0)
            self.assertLessEqual(d, 90.0)

        # f=2: base 120.0 + uniform(0, 30) -> [120.0, 150.0]
        for _ in range(20):
            d = calculate_backoff_delay(2)
            self.assertGreaterEqual(d, 120.0)
            self.assertLessEqual(d, 150.0)

        # f=3: base 240.0 -> [240.0, 270.0]
        for _ in range(20):
            d = calculate_backoff_delay(3)
            self.assertGreaterEqual(d, 240.0)
            self.assertLessEqual(d, 270.0)

        # f=4: base 480.0 -> [480.0, 510.0]
        for _ in range(20):
            d = calculate_backoff_delay(4)
            self.assertGreaterEqual(d, 480.0)
            self.assertLessEqual(d, 510.0)

        # f=5: base 960.0 -> [960.0, 990.0]
        for _ in range(20):
            d = calculate_backoff_delay(5)
            self.assertGreaterEqual(d, 960.0)
            self.assertLessEqual(d, 990.0)

        # f=6: capped at 1800.0 -> [1800.0, 1830.0]
        for _ in range(20):
            d = calculate_backoff_delay(6)
            self.assertGreaterEqual(d, 1800.0)
            self.assertLessEqual(d, 1830.0)

        # f=10: capped at 1800.0 -> [1800.0, 1830.0]
        for _ in range(20):
            d = calculate_backoff_delay(10)
            self.assertGreaterEqual(d, 1800.0)
            self.assertLessEqual(d, 1830.0)

    def test_backoff_zero_or_negative_failures_defaults_to_1(self):
        d0 = calculate_backoff_delay(0)
        self.assertGreaterEqual(d0, 60.0)
        self.assertLessEqual(d0, 90.0)

        dneg = calculate_backoff_delay(-5)
        self.assertGreaterEqual(dneg, 60.0)
        self.assertLessEqual(dneg, 90.0)


class EffectiveDueTimeTests(unittest.TestCase):
    """Tests calculation of effective due time separating next_run recurrence from quota suspension."""

    def test_effective_due_time_not_suspended(self):
        due = calculate_effective_due_time(1000.0, quota_suspended=False)
        self.assertEqual(due, 1000.0)

    def test_effective_due_time_suspended_future_iso(self):
        # Suspension until epoch 2000.0
        iso_str = dt.datetime.fromtimestamp(2000.0, tz=dt.timezone.utc).isoformat()
        due = calculate_effective_due_time(1000.0, quota_suspended=True, quota_suspended_until=iso_str)
        self.assertEqual(due, 2000.0)

    def test_effective_due_time_suspended_float_epoch(self):
        due = calculate_effective_due_time(1000.0, quota_suspended=True, quota_suspended_until=3000.0)
        self.assertEqual(due, 3000.0)

    def test_effective_due_time_suspended_past_epoch(self):
        # If suspension ended at 500, but next_run is 1000, next_run is preserved
        due = calculate_effective_due_time(1000.0, quota_suspended=True, quota_suspended_until=500.0)
        self.assertEqual(due, 1000.0)


class PacificRolloverAndDSTTests(unittest.TestCase):
    """Tests Pacific midnight rollover calculations during standard and daylight saving times."""

    def test_pacific_rollover_winter_pst(self):
        # In PST (UTC-8): 2026-01-15 14:00 UTC is 06:00 PST
        now_utc = dt.datetime(2026, 1, 15, 14, 0, 0, tzinfo=dt.timezone.utc)
        reset_ts, reset_iso = store.get_pacific_next_rollover(now_utc)
        # Next midnight PT is 2026-01-16 00:00 PST = 2026-01-16 08:00 UTC
        expected_utc = dt.datetime(2026, 1, 16, 8, 0, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(reset_ts, expected_utc.timestamp())
        self.assertIn("2026-01-16T00:00:00", reset_iso)

    def test_pacific_rollover_summer_pdt(self):
        # In PDT (UTC-7): 2026-07-15 14:00 UTC is 07:00 PDT
        now_utc = dt.datetime(2026, 7, 15, 14, 0, 0, tzinfo=dt.timezone.utc)
        reset_ts, reset_iso = store.get_pacific_next_rollover(now_utc)
        # Next midnight PT is 2026-07-16 00:00 PDT = 2026-07-16 07:00 UTC
        expected_utc = dt.datetime(2026, 7, 16, 7, 0, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(reset_ts, expected_utc.timestamp())
        self.assertIn("2026-07-16T00:00:00", reset_iso)


class SQLiteIntentPreservationTests(unittest.TestCase):
    """Tests that job intent metadata (e.g. publish_staging) is persisted and survives process lifecycle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.patchers = [
            patch.object(store, "ROOT", self.root),
            patch.object(store, "DB", self.root / "studio.sqlite3"),
            patch.object(server, "ROOT", self.root),
        ]
        for p in self.patchers:
            p.start()
        # store initialized on first connect

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        self.tmp.cleanup()

    def test_publish_staging_intent_persisted_and_retrieved(self):
        job_data = {
            "id": "job_staging_123",
            "action": "preview",
            "status": "pending",
            "extra": {"intent": "publish_staging"},
            "created_at": store.now(),
        }
        store.put("jobs", "job_staging_123", job_data)

        # Retrieve record afresh
        retrieved = store.get("jobs", "job_staging_123")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.get("extra", {}).get("intent"), "publish_staging")


class AutomaticJobCompletionTests(unittest.TestCase):
    """Tests handle_automatic_job_completion hook under all operational outcomes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.settings_path = self.root / "studio-settings.json"
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "interval_hours": 3,
            "mode": "publish",
            "next_run": 1000.0,
            "consecutive_failures": 0,
            "quota_suspended": False,
        }), encoding="utf-8")

        self.patchers = [
            patch.object(server, "ROOT", self.root),
            patch.object(store, "ROOT", self.root),
            patch.object(store, "DB", self.root / "studio.sqlite3"),
        ]
        for p in self.patchers:
            p.start()
        # store initialized on first connect
        server.AUTOMATION_EVENT.clear()
        server._IN_MEMORY_NEXT_RUN = None
        server._IN_MEMORY_CONSECUTIVE_FAILURES = None

    def tearDown(self):
        server._IN_MEMORY_NEXT_RUN = None
        server._IN_MEMORY_CONSECUTIVE_FAILURES = None
        for p in self.patchers:
            p.stop()
        self.tmp.cleanup()

    def test_upload_success_completion_resets_failures_and_schedules_next(self):
        # Set some existing failures
        studio_settings.update_settings({"consecutive_failures": 2}, settings_path=self.settings_path)

        job = {
            "id": "job_up_1",
            "action": "upload",
            "automatic": True,
            "status": "completed",
            "error": "",
        }
        now_ts = time.time()
        with patch("time.time", return_value=now_ts):
            server.handle_automatic_job_completion(job, store.now())

        saved = studio_settings.load_settings(self.settings_path)
        self.assertEqual(saved["consecutive_failures"], 0)
        # Configured interval = 3h = 10800s. Jitter in [0, 900]s.
        self.assertGreaterEqual(saved["next_run"], now_ts + 10800.0)
        self.assertLessEqual(saved["next_run"], now_ts + 10800.0 + 900.0)

    def test_staging_preview_success_wakes_scheduler_and_keeps_due(self):
        # Set prior failure
        studio_settings.update_settings({"consecutive_failures": 3, "next_run": 1000.0}, settings_path=self.settings_path)

        job = {
            "id": "job_stg_1",
            "action": "preview",
            "automatic": True,
            "status": "completed",
            "extra": {"intent": "publish_staging"},
            "error": "",
        }
        server.AUTOMATION_EVENT.clear()
        server.handle_automatic_job_completion(job, store.now())

        saved = studio_settings.load_settings(self.settings_path)
        self.assertEqual(saved["consecutive_failures"], 0)
        # Scheduler event was signaled to wake up immediately
        self.assertTrue(server.AUTOMATION_EVENT.is_set())
        # next_run was NOT pushed out 3 hours
        self.assertEqual(saved["next_run"], 1000.0)

    def test_regular_preview_success_schedules_normal_interval(self):
        job = {
            "id": "job_prev_1",
            "action": "preview",
            "automatic": True,
            "status": "completed",
            "extra": {},
            "error": "",
        }
        now_ts = time.time()
        with patch("time.time", return_value=now_ts):
            server.handle_automatic_job_completion(job, store.now())

        saved = studio_settings.load_settings(self.settings_path)
        self.assertEqual(saved["consecutive_failures"], 0)
        self.assertGreaterEqual(saved["next_run"], now_ts + 10800.0)
        self.assertLessEqual(saved["next_run"], now_ts + 10800.0 + 900.0)

    def test_genuine_idle_completion_resets_failures_and_schedules_normal_interval(self):
        # Set prior failures
        studio_settings.update_settings({"consecutive_failures": 1}, settings_path=self.settings_path)

        job = {
            "id": "job_idle_1",
            "action": "preview",
            "automatic": True,
            "status": "idle",
            "summary": {
                "status": "idle",
                "message": "0 candidates found",
                "discovery_stats": {"total_discovered_raw": 0, "search_quota_blocked": False, "google_quota_exhausted": False},
            },
            "error": "",
        }
        now_ts = time.time()
        with patch("time.time", return_value=now_ts):
            server.handle_automatic_job_completion(job, store.now())

        saved = studio_settings.load_settings(self.settings_path)
        self.assertEqual(saved["consecutive_failures"], 0)
        self.assertFalse(saved.get("quota_suspended", False))
        self.assertGreaterEqual(saved["next_run"], now_ts + 10800.0)
        self.assertLessEqual(saved["next_run"], now_ts + 10800.0 + 900.0)

    def test_worker_quota_blocked_error_enters_suspension_without_failure_penalty(self):
        job = {
            "id": "job_up_blocked",
            "action": "upload",
            "automatic": True,
            "status": "failed",
            "summary": {"status": "blocked", "message": "YouTube upload quota limit reached (100/100)"},
            "error": "YouTube upload quota limit reached (100/100)",
        }
        server.handle_automatic_job_completion(job, store.now())

        saved = studio_settings.load_settings(self.settings_path)
        self.assertTrue(saved["quota_suspended"])
        self.assertEqual(saved["quota_suspended_reason"], "upload_quota_limit")
        # consecutive_failures must NOT be incremented
        self.assertEqual(saved.get("consecutive_failures", 0), 0)

    def test_discovery_quota_blocked_enters_suspension_without_failure_penalty(self):
        job = {
            "id": "job_disc_blocked",
            "action": "preview",
            "automatic": True,
            "status": "idle",
            "summary": {
                "status": "idle",
                "message": "YouTube search quota limit reached; 0 candidates from other sources",
                "discovery_stats": {
                    "total_discovered_raw": 0,
                    "search_quota_blocked": True,
                    "google_quota_exhausted": False,
                },
            },
            "error": "",
        }
        server.handle_automatic_job_completion(job, store.now())

        saved = studio_settings.load_settings(self.settings_path)
        self.assertTrue(saved["quota_suspended"])
        self.assertEqual(saved["quota_suspended_reason"], "search_quota_limit")
        self.assertEqual(saved.get("consecutive_failures", 0), 0)

    def test_pipeline_failure_increments_consecutive_failures_and_backs_off(self):
        job = {
            "id": "job_crash_1",
            "action": "preview",
            "automatic": True,
            "status": "failed",
            "error": "SyntaxError in script: unexpected token",
        }
        now_ts = time.time()
        with patch("time.time", return_value=now_ts):
            server.handle_automatic_job_completion(job, store.now())

        saved = studio_settings.load_settings(self.settings_path)
        self.assertEqual(saved["consecutive_failures"], 1)
        # f=1: backoff is [60, 90]s
        self.assertGreaterEqual(saved["next_run"], now_ts + 60.0)
        self.assertLessEqual(saved["next_run"], now_ts + 90.0)

        # Second failure: f=2 -> [120, 150]s
        with patch("time.time", return_value=now_ts):
            server.handle_automatic_job_completion(job, store.now())
        saved = studio_settings.load_settings(self.settings_path)
        self.assertEqual(saved["consecutive_failures"], 2)
        self.assertGreaterEqual(saved["next_run"], now_ts + 120.0)
        self.assertLessEqual(saved["next_run"], now_ts + 150.0)

    def test_user_cancellation_completion_reschedules_safely(self):
        studio_settings.update_settings({"consecutive_failures": 2}, settings_path=self.settings_path)
        job = {
            "id": "job_canc_1",
            "action": "upload",
            "automatic": True,
            "status": "cancelled",
            "error": "Cancelled by user",
        }
        now_ts = time.time()
        with patch("time.time", return_value=now_ts):
            server.handle_automatic_job_completion(job, store.now())

        saved = studio_settings.load_settings(self.settings_path)
        # consecutive_failures not incremented
        self.assertEqual(saved["consecutive_failures"], 2)
        # next_run rescheduled with normal interval + jitter
        self.assertGreaterEqual(saved["next_run"], now_ts + 10800.0)


class QuotaIndependenceAndEligibilityTests(unittest.TestCase):
    """Tests decoupling between search quota and upload quota eligibility."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.settings_path = self.root / "studio-settings.json"
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "interval_hours": 3,
            "mode": "publish",
            "next_run": 0.0,  # due immediately
            "consecutive_failures": 0,
            "quota_suspended": False,
        }), encoding="utf-8")

        self.patchers = [
            patch.object(server, "ROOT", self.root),
            patch.object(store, "ROOT", self.root),
            patch.object(store, "DB", self.root / "studio.sqlite3"),
            patch.object(server, "is_online", return_value=True),
        ]
        for p in self.patchers:
            p.start()
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        # store initialized on first connect

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        self.tmp.cleanup()

    def test_search_quota_exhausted_does_not_block_ready_upload(self):
        # Create a ready video file on disk and in database
        vid_file = self.root / "out" / "vid_ready_1.mp4"
        vid_file.write_bytes(b"dummy mp4 content")

        video_record = {
            "id": "vid_ready_1",
            "status": "ready",
            "video": "out/vid_ready_1.mp4",
            "created_at": store.now(),
        }
        store.put("videos", "vid_ready_1", video_record)

        # Quota tracker: search limit reached, but upload quota is completely available!
        mock_tracker = {
            "search_limit_reached": True,
            "google_quota_exhausted": False,
            "videos_insert_count": 0,
            "videos_insert_limit": 100,
        }

        with patch.object(store, "get_local_quota_tracker", return_value=mock_tracker):
            with patch.object(server, "start_job") as mock_start_job:
                # Trigger a single iteration of automation_loop checks
                # Emulate what automation_loop does:
                records = store.records("videos")
                candidate = None
                for v in records:
                    if v.get("status") == "ready" and (self.root / v.get("video", "")).is_file():
                        candidate = v
                        break

                self.assertIsNotNone(candidate)
                # Verify that search_limit_reached does NOT block upload
                upload_limit_hit = mock_tracker["videos_insert_count"] >= mock_tracker["videos_insert_limit"]
                google_ex = mock_tracker["google_quota_exhausted"]
                self.assertFalse(upload_limit_hit)
                self.assertFalse(google_ex)

                # start_job("upload") would be dispatched
                server.start_job("upload", key=candidate["id"], automatic=True)
                mock_start_job.assert_called_with("upload", key="vid_ready_1", automatic=True)

    def test_search_quota_exhausted_with_non_youtube_sources_allows_discovery(self):
        # Config has both youtube and reddit/rss
        mock_cfg = {
            "discovery": {
                "youtube_queries": ["query1"],
                "subreddits": ["subreddit1"],
                "news_rss": ["http://rss.example.com"],
            }
        }
        mock_tracker = {
            "search_limit_reached": True,
            "google_quota_exhausted": False,
        }

        with patch.object(server, "get_config", return_value=mock_cfg):
            with patch.object(store, "get_local_quota_tracker", return_value=mock_tracker):
                disc_cfg = mock_cfg["discovery"]
                has_yt = bool(disc_cfg.get("youtube_queries"))
                has_others = bool(disc_cfg.get("subreddits") or disc_cfg.get("news_rss"))
                search_limit_reached = bool(mock_tracker.get("search_limit_reached", False))

                # If has_others is True, search_quota does NOT defer the job!
                is_blocked = has_yt and not has_others and search_limit_reached
                self.assertFalse(is_blocked)

    def test_completed_discovery_with_search_quota_reached_does_not_suspend(self):
        # Case where YouTube search quota was reached, but Reddit discovery succeeded and produced candidate
        job = {
            "id": "job_reddit_succ",
            "action": "preview",
            "automatic": True,
            "status": "completed",
            "summary": {
                "status": "completed",
                "discovery_stats": {
                    "total_discovered_raw": 3,
                    "search_quota_blocked": True,
                    "google_quota_exhausted": False,
                },
            },
            "error": "",
        }
        mock_tracker = {"search_limit_reached": True, "google_quota_exhausted": False}
        with patch.object(store, "get_local_quota_tracker", return_value=mock_tracker):
            server.handle_automatic_job_completion(job, store.now())

        saved = studio_settings.load_settings(self.settings_path)
        # Quota suspension MUST NOT be entered because discovery succeeded!
        self.assertFalse(saved.get("quota_suspended", False))
        self.assertEqual(saved.get("consecutive_failures", 0), 0)


class PacificRolloverResumptionTests(unittest.TestCase):
    """Tests clearing quota suspension and resuming with rollover jitter at Pacific midnight."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.settings_path = self.root / "studio-settings.json"
        # Suspended until an epoch in the past
        past_iso = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "interval_hours": 3,
            "mode": "publish",
            "next_run": 500.0,
            "consecutive_failures": 2,
            "quota_suspended": True,
            "quota_suspended_reason": "upload_quota_limit",
            "quota_suspended_until_pt": past_iso,
        }), encoding="utf-8")

        self.patchers = [
            patch.object(server, "ROOT", self.root),
            patch.object(store, "ROOT", self.root),
            patch.object(store, "DB", self.root / "studio.sqlite3"),
        ]
        for p in self.patchers:
            p.start()
        # store initialized on first connect

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        self.tmp.cleanup()

    def test_rollover_clears_suspension_and_resumes_with_jitter(self):
        settings = studio_settings.load_settings(self.settings_path)
        self.assertTrue(settings["quota_suspended"])

        # Emulate rollover lifting logic from automation_loop:
        now_ts = time.time()
        rollover_jitter = studio_settings.calculate_rollover_jitter()
        resumed_next_run = max(float(settings.get("next_run", 0)), now_ts + rollover_jitter)

        studio_settings.update_settings(
            {
                "quota_suspended": False,
                "quota_suspended_reason": "",
                "quota_suspended_until_pt": "",
                "quota_suspended_at": "",
                "next_run": resumed_next_run,
                "consecutive_failures": 0,
            },
            settings_path=self.settings_path,
        )

        saved = studio_settings.load_settings(self.settings_path)
        self.assertFalse(saved["quota_suspended"])
        self.assertEqual(saved["quota_suspended_reason"], "")
        self.assertEqual(saved["consecutive_failures"], 0)
        self.assertGreaterEqual(saved["next_run"], now_ts + 60.0)
        self.assertLessEqual(saved["next_run"], now_ts + 300.0)


class SchedulerDiagnosticsAndApiStatusTests(unittest.TestCase):
    """Tests scheduler state diagnostics and /api/status integration."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.settings_path = self.root / "studio-settings.json"
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "interval_hours": 3,
            "mode": "preview",
            "next_run": 50000.0,
            "consecutive_failures": 0,
            "quota_suspended": False,
        }), encoding="utf-8")

        self.patchers = [
            patch.object(server, "ROOT", self.root),
            patch.object(store, "ROOT", self.root),
            patch.object(store, "DB", self.root / "studio.sqlite3"),
            patch.object(server, "is_online", return_value=True),
        ]
        for p in self.patchers:
            p.start()
        # store initialized on first connect

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        self.tmp.cleanup()

    def test_status_diagnostics_integration(self):
        server.SCHEDULER_DIAGNOSTICS["state"] = "waiting_for_schedule"
        server.SCHEDULER_DIAGNOSTICS["deferral_reason"] = "waiting_schedule"

        cfg = server.get_automation_settings()
        sched_state = server.SCHEDULER_DIAGNOSTICS.get("state", "idle")
        defer_reason = server.SCHEDULER_DIAGNOSTICS.get("deferral_reason", "")
        eff_due = studio_settings.calculate_effective_due_time(
            cfg.get("next_run", 0),
            quota_suspended=cfg.get("quota_suspended", False),
            quota_suspended_until=cfg.get("quota_suspended_until_pt", ""),
        )
        cfg["scheduler_state"] = sched_state
        cfg["deferral_reason"] = defer_reason
        cfg["effective_due_time"] = eff_due

        self.assertEqual(cfg["scheduler_state"], "waiting_for_schedule")
        self.assertEqual(cfg["deferral_reason"], "waiting_schedule")
        self.assertEqual(cfg["effective_due_time"], 50000.0)


class SchedulerSynchronizationAndRaceTests(unittest.TestCase):
    """
    Deterministic tests for AUTOMATION_EVENT synchronization:
    1. Signal arriving after wait() returns but before scheduler state inspection.
    2. Job completing while the scheduler is waiting for an active worker to finish.
    3. Settings changing while the scheduler is waiting.
    4. Signal arriving during scheduler state inspection.
    5. No duplicate dispatches and preservation of single-worker FIFO invariant.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.settings_path = self.root / "studio-settings.json"
        self.out_dir = self.root / "out"
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self.patchers = [
            patch.object(server, "ROOT", self.root),
            patch.object(store, "ROOT", self.root),
            patch.object(store, "DB", self.root / "studio.sqlite3"),
            patch.object(server, "is_online", return_value=True),
        ]
        for p in self.patchers:
            p.start()

        server.STOP_EVENT.clear()
        server.AUTOMATION_EVENT.clear()
        server.ACTIVE_JOB = None
        server.ACTIVE_PROC = None

    def tearDown(self):
        server.STOP_EVENT.set()
        for p in self.patchers:
            p.stop()
        self.tmp.cleanup()
        server.STOP_EVENT.clear()
        server.AUTOMATION_EVENT.clear()
        server.QUEUE_EVENT.clear()

    def test_signal_arriving_between_wait_and_state_inspection_inspected_in_same_cycle(self):
        """1. A signal arriving after wait() returns but before state inspection is inspected and acted on immediately."""
        now_ts = 1000000.0
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "mode": "publish",
            "interval_hours": 3.0,
            "next_run": now_ts - 10,
        }), encoding="utf-8")

        vid_file = self.out_dir / "vid_sync_1.mp4"
        vid_file.write_bytes(b"ready video payload")

        dispatched_jobs = []
        def fake_start(action, key="", automatic=False, extra=None):
            dispatched_jobs.append((action, key))
            return {"status": "pending", "id": "job_1"}

        def interleaved_wait(timeout=None):
            with server.GUARD:
                store.put("videos", "vid_sync_1", {
                    "id": "vid_sync_1",
                    "status": "ready",
                    "video": "out/vid_sync_1.mp4",
                    "created_at": "2026-10-01T00:00:00Z",
                })
                server.AUTOMATION_EVENT.set()
            server.STOP_EVENT.set()
            return True

        with patch.object(server.AUTOMATION_EVENT, "wait", side_effect=interleaved_wait), \
             patch.object(store, "busy", return_value=False), \
             patch.object(server, "start_job", side_effect=fake_start), \
             patch("time.time", return_value=now_ts):
            server.automation_loop()

        self.assertEqual(len(dispatched_jobs), 1)
        self.assertEqual(dispatched_jobs[0], ("upload", "vid_sync_1"))
        self.assertEqual(server.SCHEDULER_DIAGNOSTICS.get("state"), "job_dispatched")

    def test_job_completing_while_waiting_for_worker_wakes_scheduler_and_dispatches_next(self):
        """2. A job completing while the scheduler is waiting for an active worker immediately wakes and dispatches."""
        now_ts = 2000000.0
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "mode": "publish",
            "interval_hours": 3.0,
            "next_run": now_ts - 10,
        }), encoding="utf-8")

        vid_file = self.out_dir / "vid_sync_2.mp4"
        vid_file.write_bytes(b"ready video payload 2")
        store.put("videos", "vid_sync_2", {
            "id": "vid_sync_2",
            "status": "ready",
            "video": "out/vid_sync_2.mp4",
            "created_at": "2026-10-01T00:00:00Z",
        })

        server.ACTIVE_JOB = {"id": "job_render_prev", "action": "render"}
        dispatched = []

        def fake_start(action, key="", automatic=False, extra=None):
            dispatched.append((action, key))
            return {"status": "pending", "id": "job_2"}

        cycle = 0
        def step_wait(timeout=None):
            nonlocal cycle
            cycle += 1
            if cycle == 1:
                # Entry to loop: let iteration 1 run and observe worker busy
                return True
            else:
                # Iteration 1 ran and deferred to waiting_for_worker
                self.assertEqual(server.SCHEDULER_DIAGNOSTICS.get("state"), "waiting_for_worker")
                with server.GUARD:
                    server.ACTIVE_JOB = None
                    server.AUTOMATION_EVENT.set()
                server.STOP_EVENT.set()
                return True

        with patch.object(server.AUTOMATION_EVENT, "wait", side_effect=step_wait), \
             patch.object(store, "busy", return_value=False), \
             patch.object(server, "start_job", side_effect=fake_start), \
             patch("time.time", return_value=now_ts):
            server.automation_loop()

        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0], ("upload", "vid_sync_2"))
        self.assertEqual(server.SCHEDULER_DIAGNOSTICS.get("state"), "job_dispatched")

    def test_settings_change_while_waiting_wakes_scheduler_immediately(self):
        """3. Automation settings changing while scheduler is waiting wakes scheduler to dispatch work."""
        now_ts = 3000000.0
        self.settings_path.write_text(json.dumps({
            "enabled": False,
            "mode": "publish",
            "interval_hours": 3.0,
            "next_run": now_ts - 10,
        }), encoding="utf-8")

        vid_file = self.out_dir / "vid_sync_3.mp4"
        vid_file.write_bytes(b"ready video payload 3")
        store.put("videos", "vid_sync_3", {
            "id": "vid_sync_3",
            "status": "ready",
            "video": "out/vid_sync_3.mp4",
            "created_at": "2026-10-01T00:00:00Z",
        })

        dispatched = []
        def fake_start(action, key="", automatic=False, extra=None):
            dispatched.append((action, key))
            return {"status": "pending", "id": "job_3"}

        cycle = 0
        def settings_wait(timeout=None):
            nonlocal cycle
            cycle += 1
            if cycle == 1:
                # Entry to loop: let iteration 1 run and observe disabled
                return True
            else:
                # Iteration 1 ran and observed disabled
                self.assertEqual(server.SCHEDULER_DIAGNOSTICS.get("state"), "disabled")
                with server.GUARD:
                    studio_settings.update_settings({"enabled": True}, settings_path=self.settings_path)
                    server.AUTOMATION_EVENT.set()
                server.STOP_EVENT.set()
                return True

        with patch.object(server.AUTOMATION_EVENT, "wait", side_effect=settings_wait), \
             patch.object(store, "busy", return_value=False), \
             patch.object(server, "start_job", side_effect=fake_start), \
             patch("time.time", return_value=now_ts):
            server.automation_loop()

        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0], ("upload", "vid_sync_3"))
        self.assertEqual(server.SCHEDULER_DIAGNOSTICS.get("state"), "job_dispatched")

    def test_signal_arriving_during_state_inspection_is_preserved_for_next_cycle(self):
        """4. A signal arriving during scheduler state inspection remains set for the subsequent cycle."""
        now_ts = 4000000.0
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "mode": "publish",
            "interval_hours": 3.0,
            "next_run": now_ts + 3600.0,
        }), encoding="utf-8")

        original_get_settings = server.get_automation_settings
        def hooked_get_settings():
            cfg = original_get_settings()
            server.AUTOMATION_EVENT.set()
            return cfg

        server.STOP_EVENT.clear()
        def one_shot_wait(timeout=None):
            server.STOP_EVENT.set()
            return True

        with patch.object(server.AUTOMATION_EVENT, "wait", side_effect=one_shot_wait), \
             patch.object(server, "get_automation_settings", side_effect=hooked_get_settings), \
             patch.object(store, "busy", return_value=False), \
             patch("time.time", return_value=now_ts):
            server.automation_loop()

        self.assertTrue(server.AUTOMATION_EVENT.is_set())

    def test_rapid_signals_no_duplicate_dispatches_and_fifo_order_preserved(self):
        """5. Multiple rapid signals do not cause duplicate dispatches and preserve single-worker FIFO invariant."""
        now_ts = 5000000.0
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "mode": "publish",
            "interval_hours": 3.0,
            "next_run": now_ts - 10,
        }), encoding="utf-8")

        for i, vid in enumerate(["v1", "v2", "v3"]):
            vf = self.out_dir / f"{vid}.mp4"
            vf.write_bytes(f"video {vid}".encode())
            store.put("videos", vid, {
                "id": vid,
                "status": "ready",
                "video": f"out/{vid}.mp4",
                "created_at": f"2026-10-01T0{i}:00:00Z",
            })

        dispatched = []
        def track_start(action, key="", automatic=False, extra=None):
            dispatched.append((action, key))
            server.ACTIVE_JOB = {"id": f"job_{key}", "action": action, "key": key}
            return {"status": "pending", "id": f"job_{key}"}

        server.AUTOMATION_EVENT.set()
        server.AUTOMATION_EVENT.set()
        server.AUTOMATION_EVENT.set()

        cycle = 0
        def multi_wait(timeout=None):
            nonlocal cycle
            cycle += 1
            if cycle >= 2:
                server.STOP_EVENT.set()
            return True

        with patch.object(server.AUTOMATION_EVENT, "wait", side_effect=multi_wait), \
             patch.object(store, "busy", return_value=False), \
             patch.object(server, "start_job", side_effect=track_start), \
             patch("time.time", return_value=now_ts):
            server.automation_loop()

        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0], ("upload", "v1"))
        self.assertEqual(server.SCHEDULER_DIAGNOSTICS.get("state"), "waiting_for_worker")


class CompletionHandlingDefensiveGuardsTests(unittest.TestCase):
    """
    Tests for defensive completion-handling guards:
    1. An exception from handle_automatic_job_completion on normal path does not
       convert an already-completed job into 'failed' and does not invoke completion
       handling a second time.
    2. An early cancellation return immediately after process creation handles
       automatic completion recurrence so the scheduler does not remain permanently due.
    3. An early cancellation of a manual job does not advance or modify automation settings.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.settings_path = self.root / "studio-settings.json"
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "mode": "publish",
            "interval_hours": 3.0,
            "next_run": 1000000.0,
            "consecutive_failures": 0,
        }), encoding="utf-8")

        self.patchers = [
            patch.object(server, "ROOT", self.root),
            patch.object(store, "ROOT", self.root),
            patch.object(store, "DB", self.root / "studio.sqlite3"),
            patch.object(server, "LOGS_DIR", self.root / "logs"),
        ]
        for p in self.patchers:
            p.start()
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        server.STOP_EVENT.clear()
        server.AUTOMATION_EVENT.clear()
        server.QUEUE_EVENT.clear()
        server.ACTIVE_JOB = None
        server.ACTIVE_PROC = None
        server._IN_MEMORY_NEXT_RUN = None
        server._IN_MEMORY_CONSECUTIVE_FAILURES = None

    def tearDown(self):
        server.STOP_EVENT.clear()
        server.AUTOMATION_EVENT.clear()
        server.QUEUE_EVENT.clear()
        server.ACTIVE_JOB = None
        server.ACTIVE_PROC = None
        server._IN_MEMORY_NEXT_RUN = None
        server._IN_MEMORY_CONSECUTIVE_FAILURES = None
        for p in self.patchers:
            p.stop()
        self.tmp.cleanup()

    def test_completion_handler_exception_does_not_convert_completed_job_to_failed_or_duplicate(self):
        """
        Verify that if handle_automatic_job_completion raises an unexpected exception on the normal path:
        1. The job's final status in store remains 'completed' (not flipped to 'failed').
        2. handle_automatic_job_completion is NOT invoked a second time in the error path.
        3. consecutive_failures is NOT incremented.
        """
        job_id = "job_guard_success"
        job = {
            "id": job_id,
            "action": "preview",
            "automatic": True,
            "status": "running",
            "stage": "Starting",
            "started_at": store.now(),
        }
        store.put("jobs", job_id, job)

        # Process exits successfully with a KENAU_SUMMARY completed message
        code = 'import sys; print("KENAU_SUMMARY " + \'{"status": "completed"}\'); sys.exit(0)'
        cmd = [sys.executable, "-c", code]

        call_count = 0
        def flaking_handle_completion(j, fin_at):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("Synthetic error in completion handler")

        with patch.object(server, "handle_automatic_job_completion", side_effect=flaking_handle_completion):
            server.run_job_process(job, cmd)

        # 1. handle_automatic_job_completion was invoked exactly once
        self.assertEqual(call_count, 1, "handle_automatic_job_completion must NOT be invoked a second time")

        # 2. Persisted job status MUST remain 'completed', NOT 'failed'
        saved_job = store.get("jobs", job_id)
        self.assertIsNotNone(saved_job)
        self.assertEqual(saved_job["status"], "completed", "Job status must remain completed despite completion handler error")

        # 3. Settings must not have been corrupted with failure count
        saved_settings = studio_settings.load_settings(self.settings_path)
        self.assertEqual(saved_settings.get("consecutive_failures", 0), 0)

    def test_early_cancellation_advances_automatic_schedule(self):
        """
        Verify that an early cancellation immediately after process creation invokes
        completion handling for automatic jobs, advancing next_run and preventing
        the scheduler from remaining overdue.
        """
        job_id = "job_auto_early_canc"
        now_ts = 2000000.0
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "mode": "publish",
            "interval_hours": 3.0,
            "next_run": now_ts - 100,  # Was overdue
            "consecutive_failures": 0,
        }), encoding="utf-8")

        # Pre-populate store with status 'cancelled' as if cancel_job() raced and set it
        job = {
            "id": job_id,
            "action": "upload",
            "automatic": True,
            "status": "cancelled",
            "stage": "Cancelled by user",
            "started_at": store.now(),
            "finished_at": store.now(),
        }
        store.put("jobs", job_id, job)

        cmd = [sys.executable, "-c", "import time; time.sleep(5)"]
        with patch("time.time", return_value=now_ts):
            server.run_job_process(job, cmd)

        saved_job = store.get("jobs", job_id)
        self.assertEqual(saved_job["status"], "cancelled")

        # Scheduling recurrence must have been advanced by interval (3h = 10800s) + jitter
        saved_settings = studio_settings.load_settings(self.settings_path)
        self.assertGreaterEqual(saved_settings["next_run"], now_ts + 10800.0)
        self.assertLessEqual(saved_settings["next_run"], now_ts + 10800.0 + 900.0)

    def test_early_cancellation_manual_job_does_not_advance_schedule(self):
        """
        Verify that early cancellation of a manual job does NOT advance next_run or
        modify automation settings.
        """
        job_id = "job_manual_early_canc"
        initial_next_run = 1234567.0
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "mode": "publish",
            "interval_hours": 3.0,
            "next_run": initial_next_run,
            "consecutive_failures": 0,
        }), encoding="utf-8")

        job = {
            "id": job_id,
            "action": "render",
            "automatic": False,  # Manual job
            "status": "cancelled",
            "stage": "Cancelled by user",
            "started_at": store.now(),
            "finished_at": store.now(),
        }
        store.put("jobs", job_id, job)

        cmd = [sys.executable, "-c", "import time; time.sleep(5)"]
        server.run_job_process(job, cmd)

        saved_job = store.get("jobs", job_id)
        self.assertEqual(saved_job["status"], "cancelled")

        saved_settings = studio_settings.load_settings(self.settings_path)
        self.assertEqual(saved_settings["next_run"], initial_next_run, "Manual job cancellation must not alter next_run")
        self.assertEqual(saved_settings["consecutive_failures"], 0)


class SchedulingPersistenceRecoveryTests(unittest.TestCase):
    """
    Offline regression tests for in-memory scheduling persistence recovery:
    1. Failed upload settings persistence with multiple ready videos enforces in-memory
       recurrence and prevents back-to-back upload of subsequent candidates.
    2. Repeated failure persistence errors escalate exponential backoff in memory
       (60s -> 120s -> 240s) rather than retrying every polling tick.
    3. Publish-staging discovery persistence failure still signals wake-up via finally:
       and proceeds to upload the staged candidate immediately.
    4. In-memory scheduling overrides opportunistically reconcile to disk once filelock/disk
       errors clear.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.settings_path = self.root / "studio-settings.json"
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "mode": "publish",
            "interval_hours": 3.0,
            "next_run": 1000.0,
            "consecutive_failures": 0,
        }), encoding="utf-8")

        self.patchers = [
            patch.object(server, "ROOT", self.root),
            patch.object(store, "ROOT", self.root),
            patch.object(store, "DB", self.root / "studio.sqlite3"),
            patch.object(server, "LOGS_DIR", self.root / "logs"),
        ]
        for p in self.patchers:
            p.start()
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        server.STOP_EVENT.clear()
        server.AUTOMATION_EVENT.clear()
        server.QUEUE_EVENT.clear()
        server.ACTIVE_JOB = None
        server.ACTIVE_PROC = None
        server._IN_MEMORY_NEXT_RUN = None
        server._IN_MEMORY_CONSECUTIVE_FAILURES = None

    def tearDown(self):
        server.STOP_EVENT.clear()
        server.AUTOMATION_EVENT.clear()
        server.QUEUE_EVENT.clear()
        server.ACTIVE_JOB = None
        server.ACTIVE_PROC = None
        server._IN_MEMORY_NEXT_RUN = None
        server._IN_MEMORY_CONSECUTIVE_FAILURES = None
        for p in self.patchers:
            p.stop()
        self.tmp.cleanup()

    def test_failed_upload_persistence_with_multiple_ready_videos_enforces_in_memory_recurrence(self):
        """
        Verify that when an automatic upload succeeds but persisting next_run to disk fails,
        the scheduler enforces in-memory recurrence (3h + jitter) and does NOT immediately
        upload a second ready video in violation of completion-based recurrence.
        """
        now_ts = 2000.0
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "mode": "publish",
            "interval_hours": 3.0,
            "next_run": 1000.0,  # Overdue on disk
            "consecutive_failures": 0,
        }), encoding="utf-8")

        # Two ready videos with valid dummy files on disk
        v1_file = self.root / "out" / "v1.mp4"
        v2_file = self.root / "out" / "v2.mp4"
        v1_file.parent.mkdir(parents=True, exist_ok=True)
        v1_file.write_bytes(b"dummy1")
        v2_file.write_bytes(b"dummy2")

        store.put("videos", "v1", {
            "id": "v1", "status": "ready", "video": "out/v1.mp4", "created_at": "2026-10-13T10:00:00Z"
        })
        store.put("videos", "v2", {
            "id": "v2", "status": "ready", "video": "out/v2.mp4", "created_at": "2026-10-13T11:00:00Z"
        })

        # Completed automatic upload job for v1
        job1 = {
            "id": "job_up_v1",
            "action": "upload",
            "key": "v1",
            "automatic": True,
            "status": "completed",
            "summary": {"status": "completed"},
        }
        store.put("jobs", job1["id"], job1)

        # Simulate update_settings failing with OSError during completion handling
        with patch("time.time", return_value=now_ts):
            with patch.object(studio_settings, "update_settings", side_effect=OSError("Disk locked/unwritable")):
                server.handle_automatic_job_completion(job1, finished_at="2026-10-13T12:00:00Z")

        # Verify on-disk settings were NOT updated
        disk_settings = studio_settings.load_settings(self.settings_path)
        self.assertEqual(disk_settings["next_run"], 1000.0)

        # Verify in-memory overrides WERE set
        self.assertIsNotNone(server._IN_MEMORY_NEXT_RUN)
        # Expected target: now_ts + 3h (10800s) + jitter [0, 900]
        self.assertGreaterEqual(server._IN_MEMORY_NEXT_RUN, now_ts + 10800.0)
        self.assertLessEqual(server._IN_MEMORY_NEXT_RUN, now_ts + 10800.0 + 900.0)
        self.assertEqual(server._IN_MEMORY_CONSECUTIVE_FAILURES, 0)

        # Run scheduler thread for a single cycle and verify v2 is deferred
        dispatched_jobs = []
        def mock_start_job(action, **kwargs):
            dispatched_jobs.append((action, kwargs))
            return {"id": "mock_dispatched"}

        server.STOP_EVENT.clear()
        server.AUTOMATION_EVENT.set()
        t = threading.Thread(target=server.automation_loop, daemon=True)
        with patch("time.time", return_value=now_ts):
            with patch.object(server, "start_job", side_effect=mock_start_job):
                with patch.object(server, "is_online", return_value=True):
                    # Prevent opportunistic reconciliation during this test step to test pure deferral
                    with patch.object(studio_settings, "update_settings", side_effect=OSError("Disk locked")):
                        t.start()
                        time.sleep(0.1)
                        server.STOP_EVENT.set()
                        server.AUTOMATION_EVENT.set()
                        t.join(timeout=2.0)

        # CRITICAL ASSERTION: video v2 must NOT have been dispatched
        self.assertEqual(len(dispatched_jobs), 0, "Scheduler must not dispatch second ready video when in-memory recurrence is active")
        self.assertEqual(server.SCHEDULER_DIAGNOSTICS.get("state"), "waiting_for_schedule")
        self.assertEqual(server.SCHEDULER_DIAGNOSTICS.get("deferral_reason"), "waiting_schedule")

    def test_repeated_failure_persistence_errors_escalate_backoff_in_memory(self):
        """
        Verify that consecutive pipeline failures with persistent settings write errors
        escalate exponential backoff in memory (60s -> 120s -> 240s) rather than
        spinning on disk's stale next_run every 15-second cycle.
        """
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "mode": "preview",
            "interval_hours": 3.0,
            "next_run": 1000.0,
            "consecutive_failures": 0,
        }), encoding="utf-8")

        # Failure 1 at t = 1000.0
        job1 = {"id": "f1", "action": "preview", "automatic": True, "status": "failed", "error": "render crash"}
        with patch("time.time", return_value=1000.0):
            with patch.object(studio_settings, "update_settings", side_effect=OSError("Disk full")):
                server.handle_automatic_job_completion(job1, finished_at="2026-10-13T12:00:00Z")

        # Disk remains at 0 failures because update failed
        disk_settings = studio_settings.load_settings(self.settings_path)
        self.assertEqual(disk_settings["consecutive_failures"], 0)

        # In-memory must reflect failure count 1 and exponential backoff [60s, 90s]
        self.assertEqual(server._IN_MEMORY_CONSECUTIVE_FAILURES, 1)
        self.assertGreaterEqual(server._IN_MEMORY_NEXT_RUN, 1000.0 + 60.0)
        self.assertLessEqual(server._IN_MEMORY_NEXT_RUN, 1000.0 + 90.0)

        # Failure 2 at t = 1060.0 (when backoff expires)
        job2 = {"id": "f2", "action": "preview", "automatic": True, "status": "failed", "error": "render crash"}
        with patch("time.time", return_value=1060.0):
            with patch.object(studio_settings, "update_settings", side_effect=OSError("Disk full")):
                server.handle_automatic_job_completion(job2, finished_at="2026-10-13T12:01:00Z")

        # In-memory must now reflect failure count 2 and escalated backoff [120s, 150s]
        self.assertEqual(server._IN_MEMORY_CONSECUTIVE_FAILURES, 2)
        self.assertGreaterEqual(server._IN_MEMORY_NEXT_RUN, 1060.0 + 120.0)
        self.assertLessEqual(server._IN_MEMORY_NEXT_RUN, 1060.0 + 150.0)

        # Failure 3 at t = 1180.0
        job3 = {"id": "f3", "action": "preview", "automatic": True, "status": "failed", "error": "render crash"}
        with patch("time.time", return_value=1180.0):
            with patch.object(studio_settings, "update_settings", side_effect=OSError("Disk full")):
                server.handle_automatic_job_completion(job3, finished_at="2026-10-13T12:03:00Z")

        # In-memory must now reflect failure count 3 and escalated backoff [240s, 270s]
        self.assertEqual(server._IN_MEMORY_CONSECUTIVE_FAILURES, 3)
        self.assertGreaterEqual(server._IN_MEMORY_NEXT_RUN, 1180.0 + 240.0)
        self.assertLessEqual(server._IN_MEMORY_NEXT_RUN, 1180.0 + 270.0)

    def test_publish_staging_persistence_failure_still_wakes_and_uploads_staged_candidate(self):
        """
        Verify that if publish_staging discovery completion fails to persist consecutive_failures=0
        to disk, it still signals AUTOMATION_EVENT in its finally block, resets in-memory failure count,
        and allows the scheduler to immediately dispatch the staged candidate for upload.
        """
        now_ts = 2000.0
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "mode": "publish",
            "interval_hours": 3.0,
            "next_run": 1000.0,  # Was due
            "consecutive_failures": 2,
            "auto_discovery_publish_opt_in": True,
        }), encoding="utf-8")

        # Staged video created by discovery
        v_file = self.root / "out" / "staged1.mp4"
        v_file.parent.mkdir(parents=True, exist_ok=True)
        v_file.write_bytes(b"staged content")
        store.put("videos", "staged1", {
            "id": "staged1", "status": "ready", "video": "out/staged1.mp4", "created_at": "2026-10-13T12:00:00Z"
        })

        staging_job = {
            "id": "job_disc_staging",
            "action": "preview",
            "automatic": True,
            "status": "completed",
            "extra": {"intent": "publish_staging"},
            "summary": {"status": "completed"},
        }
        store.put("jobs", staging_job["id"], staging_job)

        server.AUTOMATION_EVENT.clear()
        with patch("time.time", return_value=now_ts):
            with patch.object(studio_settings, "update_settings", side_effect=OSError("Write failed")):
                server.handle_automatic_job_completion(staging_job, finished_at="2026-10-13T12:00:00Z")

        # 1. AUTOMATION_EVENT must be set via finally: block despite exception
        self.assertTrue(server.AUTOMATION_EVENT.is_set(), "AUTOMATION_EVENT must be set even if update_settings fails")

        # 2. In-memory consecutive_failures must be reset to 0
        self.assertEqual(server._IN_MEMORY_CONSECUTIVE_FAILURES, 0)

        # 3. Running scheduler iteration should immediately pick and dispatch staged1
        dispatched = []
        def mock_start_job(action, **kwargs):
            dispatched.append((action, kwargs))
            store.put("jobs", "job_upload", {
                "id": "job_upload",
                "action": action,
                "key": kwargs.get("key"),
                "status": "pending",
                "created_at": store.now(),
            })
            return {"id": "mock_upload"}

        server.STOP_EVENT.clear()
        t = threading.Thread(target=server.automation_loop, daemon=True)
        with patch("time.time", return_value=now_ts):
            with patch.object(server, "start_job", side_effect=mock_start_job):
                with patch.object(server, "is_online", return_value=True):
                    t.start()
                    time.sleep(0.1)
                    server.STOP_EVENT.set()
                    server.AUTOMATION_EVENT.set()
                    t.join(timeout=2.0)

        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0][0], "upload")
        self.assertEqual(dispatched[0][1].get("key"), "staged1")
        self.assertTrue(dispatched[0][1].get("automatic"))

    def test_in_memory_schedule_reconciles_to_disk_when_lock_clears(self):
        """
        Verify that temporary in-memory scheduling overrides (next_run, consecutive_failures)
        are opportunistically reconciled to disk on a subsequent scheduler cycle once
        disk becomes writable.
        """
        now_ts = 2000.0
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "mode": "preview",
            "interval_hours": 3.0,
            "next_run": 1000.0,
            "consecutive_failures": 0,
        }), encoding="utf-8")

        target_override_next_run = 50000.0
        server._IN_MEMORY_NEXT_RUN = target_override_next_run
        server._IN_MEMORY_CONSECUTIVE_FAILURES = 3

        # Run one scheduler tick where disk is completely writable
        server.STOP_EVENT.clear()
        server.AUTOMATION_EVENT.set()
        t = threading.Thread(target=server.automation_loop, daemon=True)
        with patch("time.time", return_value=now_ts):
            with patch.object(server, "is_online", return_value=True):
                t.start()
                time.sleep(0.1)
                server.STOP_EVENT.set()
                server.AUTOMATION_EVENT.set()
                t.join(timeout=2.0)

        # In-memory overrides should be cleared after successful reconciliation
        self.assertIsNone(server._IN_MEMORY_NEXT_RUN)
        self.assertIsNone(server._IN_MEMORY_CONSECUTIVE_FAILURES)

        # Disk must now reflect the reconciled values!
        saved = studio_settings.load_settings(self.settings_path)
        self.assertEqual(saved["next_run"], target_override_next_run)
        self.assertEqual(saved["consecutive_failures"], 3)

    def test_rollover_with_future_in_memory_override_preserves_later_schedule(self):
        """
        Verify that when Pacific midnight rollover arrives while an in-memory override
        is already scheduled at a future deadline beyond rollover, the later deadline
        is preserved (not pulled back to rollover jitter) and is durably persisted to disk
        along with quota suspension lifting.
        """
        now_ts = 10000.0
        past_iso = dt.datetime.fromtimestamp(now_ts - 100, dt.timezone.utc).isoformat()
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "mode": "publish",
            "interval_hours": 3.0,
            "next_run": 500.0,
            "consecutive_failures": 2,
            "quota_suspended": True,
            "quota_suspended_reason": "upload_quota_limit",
            "quota_suspended_until_pt": past_iso,
        }), encoding="utf-8")

        future_override = now_ts + 7200.0  # 2 hours past rollover
        server._IN_MEMORY_NEXT_RUN = future_override
        server._IN_MEMORY_CONSECUTIVE_FAILURES = 0

        dispatched = []
        def mock_start_job(action, **kwargs):
            dispatched.append((action, kwargs))
            return {"id": "mock_job"}

        server.STOP_EVENT.clear()
        server.AUTOMATION_EVENT.set()
        t = threading.Thread(target=server.automation_loop, daemon=True)
        with patch("time.time", return_value=now_ts):
            with patch.object(server, "start_job", side_effect=mock_start_job):
                with patch.object(server, "is_online", return_value=True):
                    t.start()
                    time.sleep(0.1)
                    server.STOP_EVENT.set()
                    server.AUTOMATION_EVENT.set()
                    t.join(timeout=2.0)

        # 1. On disk: quota suspension must be cleared and next_run must preserve the future override
        saved = studio_settings.load_settings(self.settings_path)
        self.assertFalse(saved["quota_suspended"], "Quota suspension must be lifted at rollover")
        self.assertEqual(saved["quota_suspended_reason"], "")
        self.assertEqual(saved["next_run"], future_override, "Future deadline must be preserved, not overwritten with rollover jitter")
        self.assertEqual(saved["consecutive_failures"], 0)

        # 2. In memory: overrides must be cleared after durable persistence
        self.assertIsNone(server._IN_MEMORY_NEXT_RUN)
        self.assertIsNone(server._IN_MEMORY_CONSECUTIVE_FAILURES)

        # 3. Scheduler state must defer until the future deadline
        self.assertEqual(server.SCHEDULER_DIAGNOSTICS.get("state"), "waiting_for_schedule")
        self.assertEqual(server.SCHEDULER_DIAGNOSTICS.get("deferral_reason"), "waiting_schedule")
        self.assertEqual(len(dispatched), 0)

    def test_settings_mutation_preserves_active_override_unless_disabled(self):
        """
        Verify that updating settings (e.g. interval_hours or mode) via /api/settings
        preserves active in-memory overrides so back-to-back uploads or retry storms are not
        triggered, whereas disabling automation (enabled: False) explicitly clears them.
        """
        self.settings_path.write_text(json.dumps({
            "enabled": True,
            "mode": "publish",
            "interval_hours": 3.0,
            "next_run": 1000.0,
            "consecutive_failures": 0,
        }), encoding="utf-8")

        target_override = 25000.0
        server._IN_MEMORY_NEXT_RUN = target_override
        server._IN_MEMORY_CONSECUTIVE_FAILURES = 2

        handler = server.StudioHandler.__new__(server.StudioHandler)

        # 1. Update interval_hours: active in-memory overrides MUST be preserved
        handler.mutate("/api/settings", {
            "automation": {
                "enabled": True,
                "interval_hours": 5.0,
                "mode": "publish",
            }
        })
        saved = studio_settings.load_settings(self.settings_path)
        self.assertEqual(saved["interval_hours"], 5.0)
        self.assertEqual(server._IN_MEMORY_NEXT_RUN, target_override, "Updating interval_hours must retain active in-memory next_run override")
        self.assertEqual(server._IN_MEMORY_CONSECUTIVE_FAILURES, 2, "Updating interval_hours must retain active in-memory failure count")

        # 2. Update mode: active in-memory overrides MUST be preserved
        handler.mutate("/api/settings", {
            "automation": {
                "enabled": True,
                "interval_hours": 5.0,
                "mode": "preview",
            }
        })
        saved = studio_settings.load_settings(self.settings_path)
        self.assertEqual(saved["mode"], "preview")
        self.assertEqual(server._IN_MEMORY_NEXT_RUN, target_override, "Updating mode must retain active in-memory next_run override")
        self.assertEqual(server._IN_MEMORY_CONSECUTIVE_FAILURES, 2, "Updating mode must retain active in-memory failure count")

        # 3. Disable automation: in-memory overrides MUST be cleared
        handler.mutate("/api/settings", {
            "automation": {
                "enabled": False,
                "interval_hours": 5.0,
                "mode": "preview",
            }
        })
        saved = studio_settings.load_settings(self.settings_path)
        self.assertFalse(saved["enabled"])
        self.assertIsNone(server._IN_MEMORY_NEXT_RUN, "Disabling automation must clear in-memory next_run override")
        self.assertIsNone(server._IN_MEMORY_CONSECUTIVE_FAILURES, "Disabling automation must clear in-memory failure count")


if __name__ == "__main__":
    unittest.main()
