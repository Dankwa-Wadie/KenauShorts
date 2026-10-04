"""
tests/test_stage7_phase4_retention.py — Tests for Stage 7 Phase 4 Milestone 2:
Retention Reference Engine and Dry-Run Audit API.

Covers:
- Raw file discovery and Windows path safety (containment, traversal, symlink/reparse)
- Age assessment from acquisition epoch (thresholds, ranges, clock skew, no fallback)
- Reference detection across SQLite videos, jobs, queue, and state.json
- Fail-closed behavior on database error, corrupt JSON, or uncertain state
- Deterministic retention classification safety precedence
- Server HTTP API (/api/retention/audit) read-only dry-run behavior
- Verification that no files are modified, moved, renamed, or deleted
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from studio import retention
from studio.retention import (
    AGE_ELIGIBLE,
    AGE_ELIGIBLE_UNREFERENCED,
    INSPECTION_ERROR,
    INVALID_METADATA,
    INVALID_PATH,
    INVALID_TIMESTAMP,
    REFERENCED,
    RETENTION_TARGET_SECONDS,
    TOO_YOUNG,
    UNKNOWN_AGE,
    UNKNOWN_REFERENCE,
    UNREFERENCED,
    audit_retention,
    check_file_references,
    classify_retention,
    evaluate_file_age,
    is_symlink_or_reparse_point,
    load_reference_index,
    parse_raw_acquisition_timestamp,
    validate_raw_path,
)
import studio.server as server


class RetentionDiscoveryAndPathSafetyTests(unittest.TestCase):
    """Tests discovery, containment, traversal prevention, and symlink/reparse point rejection."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.work_dir = self.root / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_raw_download_path_accepted(self):
        valid_file = self.work_dir / "short_1790000000_goodclip_raw.mp4"
        valid_file.write_bytes(b"dummy")
        is_valid, err = validate_raw_path(valid_file, self.work_dir)
        self.assertTrue(is_valid)
        self.assertEqual(err, "")

    def test_path_outside_work_dir_rejected(self):
        outside_file = self.root / "short_1790000000_outside_raw.mp4"
        outside_file.write_bytes(b"dummy")
        is_valid, err = validate_raw_path(outside_file, self.work_dir)
        self.assertFalse(is_valid)
        self.assertIn("escapes approved root", err)

    def test_subdirectory_traversal_rejected(self):
        sub_dir = self.work_dir / "nested"
        sub_dir.mkdir(parents=True, exist_ok=True)
        nested_file = sub_dir / "short_1790000000_nested_raw.mp4"
        nested_file.write_bytes(b"dummy")
        is_valid, err = validate_raw_path(nested_file, self.work_dir)
        self.assertFalse(is_valid)
        self.assertIn("not a direct child", err)

    def test_alternate_data_stream_rejected(self):
        ads_path = self.work_dir / "short_1790000000_clip_raw.mp4:hidden"
        is_valid, err = validate_raw_path(ads_path, self.work_dir)
        self.assertFalse(is_valid)
        self.assertIn("Alternate data stream", err)

    def test_symlink_rejected(self):
        target = self.work_dir / "real_file.mp4"
        target.write_bytes(b"real content")
        link_path = self.work_dir / "short_1790000000_symlink_raw.mp4"
        try:
            link_path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not supported in this test environment")

        self.assertTrue(is_symlink_or_reparse_point(link_path))
        is_valid, err = validate_raw_path(link_path, self.work_dir)
        self.assertFalse(is_valid)
        self.assertIn("Symlink or reparse point rejected", err)

    def test_reparse_point_flag_detected_and_rejected(self):
        valid_file = self.work_dir / "short_1790000000_fake_reparse_raw.mp4"
        valid_file.write_bytes(b"content")

        class MockStat:
            st_file_attributes = 0x0400  # FILE_ATTRIBUTE_REPARSE_POINT

        with patch("os.lstat", return_value=MockStat()):
            self.assertTrue(is_symlink_or_reparse_point(valid_file))
            is_valid, err = validate_raw_path(valid_file, self.work_dir)
            self.assertFalse(is_valid)
            self.assertIn("Symlink or reparse point rejected", err)

    def test_case_insensitive_path_containment_on_windows(self):
        # Verify uppercase WORK directory resolves properly
        upper_work = Path(str(self.work_dir).upper())
        valid_file = self.work_dir / "short_1790000000_clip_raw.mp4"
        valid_file.write_bytes(b"content")
        is_valid, err = validate_raw_path(valid_file, upper_work)
        self.assertTrue(is_valid)


class RetentionAgeAssessmentTests(unittest.TestCase):
    """Tests file age assessment from acquisition timestamp without fallbacks."""

    def setUp(self):
        self.fixed_now = 1791000000.0  # reference time

    def test_parse_valid_filename_timestamp(self):
        fn = "short_1790500000_yt_clip123_raw.mp4"
        status, epoch, msg = parse_raw_acquisition_timestamp(fn, current_time=self.fixed_now)
        self.assertEqual(status, "valid")
        self.assertEqual(epoch, 1790500000.0)
        self.assertEqual(msg, "")

    def test_parse_malformed_filename_fails_closed(self):
        for bad_name in (
            "not_a_short.mp4",
            "short_abc_key_raw.mp4",
            "short_1790000000_raw.mp4",
            "short_1790000000_key.mp4",
            "clip_raw.mp4",
            "short_123_key_raw.mp4",  # epoch too short
        ):
            status, epoch, msg = parse_raw_acquisition_timestamp(bad_name, current_time=self.fixed_now)
            self.assertEqual(status, INVALID_METADATA)
            self.assertIsNone(epoch)

    def test_parse_out_of_range_epoch(self):
        # Epoch before year 2001 (MIN_VALID_EPOCH = 1_000_000_000)
        status, epoch, msg = parse_raw_acquisition_timestamp(
            "short_0000500000_clip_raw.mp4", current_time=self.fixed_now
        )
        self.assertEqual(status, INVALID_TIMESTAMP)
        self.assertIsNone(epoch)

    def test_parse_future_timestamp_rejected(self):
        # 1000 seconds into the future (exceeds 300s skew tolerance)
        future_epoch = int(self.fixed_now + 1000)
        status, epoch, msg = parse_raw_acquisition_timestamp(
            f"short_{future_epoch}_future_raw.mp4", current_time=self.fixed_now
        )
        self.assertEqual(status, INVALID_TIMESTAMP)
        self.assertIsNone(epoch)

    def test_evaluate_file_age_just_below_seven_days(self):
        # 604,799 seconds old -> just under 7 days (604,800s)
        epoch = self.fixed_now - (RETENTION_TARGET_SECONDS - 1)
        status, age_sec, msg = evaluate_file_age(epoch, current_time=self.fixed_now)
        self.assertEqual(status, TOO_YOUNG)
        self.assertAlmostEqual(age_sec, 604799.0)

    def test_evaluate_file_age_exactly_seven_days(self):
        # Exactly 604,800 seconds old
        epoch = self.fixed_now - RETENTION_TARGET_SECONDS
        status, age_sec, msg = evaluate_file_age(epoch, current_time=self.fixed_now)
        self.assertEqual(status, AGE_ELIGIBLE)
        self.assertAlmostEqual(age_sec, RETENTION_TARGET_SECONDS)

    def test_evaluate_file_age_above_seven_days(self):
        # 10 days old (864,000s)
        epoch = self.fixed_now - 864000.0
        status, age_sec, msg = evaluate_file_age(epoch, current_time=self.fixed_now)
        self.assertEqual(status, AGE_ELIGIBLE)
        self.assertAlmostEqual(age_sec, 864000.0)

    def test_evaluate_file_age_none_epoch_returns_unknown_age(self):
        status, age_sec, msg = evaluate_file_age(None, current_time=self.fixed_now)
        self.assertEqual(status, UNKNOWN_AGE)
        self.assertEqual(age_sec, 0.0)

    def test_no_fallback_to_ctime_or_mtime(self):
        # Verify evaluate_file_age strictly computes from epoch argument and never touches filesystem
        with patch("os.path.getctime", side_effect=AssertionError("getctime must not be called")), \
             patch("os.path.getmtime", side_effect=AssertionError("getmtime must not be called")):
            status, _, _ = evaluate_file_age(self.fixed_now - 700000, current_time=self.fixed_now)
            self.assertEqual(status, AGE_ELIGIBLE)


class RetentionReferenceDetectionTests(unittest.TestCase):
    """Tests reference detection across SQLite (videos, jobs), state.json, and re-renders."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.work_dir = self.root / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "studio.sqlite3"

        # Initialize test SQLite schema
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS videos (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def _insert_video(self, vid_id: str, data: dict):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.execute("INSERT OR REPLACE INTO videos VALUES (?, ?)", (vid_id, json.dumps(data)))
            db.commit()

    def _insert_job(self, job_id: str, data: dict):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?)", (job_id, json.dumps(data)))
            db.commit()

    def test_file_referenced_by_draft_in_sqlite(self):
        raw_file = self.work_dir / "short_1790100000_draftvid_raw.mp4"
        raw_file.write_bytes(b"clip content")

        self._insert_video("short_1790100000_draftvid", {
            "id": "short_1790100000_draftvid",
            "status": "ready",
            "title": "Draft Video",
        })

        index, err = load_reference_index(root=self.root)
        self.assertEqual(err, "")
        self.assertIsNotNone(index)

        ref_status, reasons, _ = check_file_references(raw_file, index, root=self.root)
        self.assertEqual(ref_status, REFERENCED)
        self.assertTrue(any(r.get("video_id") == "short_1790100000_draftvid" or r.get("stem") == "short_1790100000_draftvid" for r in reasons))

    def test_file_referenced_by_active_job(self):
        raw_file = self.work_dir / "short_1790200000_jobclip_raw.mp4"
        raw_file.write_bytes(b"clip content")

        self._insert_job("job_active_1", {
            "id": "job_active_1",
            "action": "render",
            "key": "short_1790200000_jobclip",
            "status": "running",
        })

        index, err = load_reference_index(root=self.root)
        self.assertIsNotNone(index)

        ref_status, reasons, _ = check_file_references(raw_file, index, root=self.root)
        self.assertEqual(ref_status, REFERENCED)
        self.assertTrue(any(r.get("job_id") == "job_active_1" for r in reasons))

    def test_file_referenced_by_queued_pending_job(self):
        raw_file = self.work_dir / "short_1790200000_queued_raw.mp4"
        raw_file.write_bytes(b"clip content")

        self._insert_job("job_pending_1", {
            "id": "job_pending_1",
            "action": "upload",
            "key": "short_1790200000_queued",
            "status": "pending",
        })

        index, err = load_reference_index(root=self.root)
        ref_status, reasons, _ = check_file_references(raw_file, index, root=self.root)
        self.assertEqual(ref_status, REFERENCED)
        self.assertTrue(any(r.get("job_id") == "job_pending_1" for r in reasons))

    def test_file_referenced_by_re_render_lineage(self):
        # Original raw file used as source for an _edit_ video
        raw_file = self.work_dir / "short_1790300000_origclip_raw.mp4"
        raw_file.write_bytes(b"clip content")

        self._insert_video("short_1790300000_origclip_edit_ab12cd", {
            "id": "short_1790300000_origclip_edit_ab12cd",
            "status": "ready",
            "raw_video": str(raw_file),
        })

        index, err = load_reference_index(root=self.root)
        ref_status, reasons, _ = check_file_references(raw_file, index, root=self.root)
        self.assertEqual(ref_status, REFERENCED)

    def test_file_referenced_by_unresolved_upload(self):
        raw_file = self.work_dir / "short_1790400000_unresolved_raw.mp4"
        raw_file.write_bytes(b"clip content")

        self._insert_video("short_1790400000_unresolved", {
            "id": "short_1790400000_unresolved",
            "status": "upload_unknown",
            "resumable_uri": "https://upload.youtube.com/foo",
        })

        index, err = load_reference_index(root=self.root)
        ref_status, reasons, _ = check_file_references(raw_file, index, root=self.root)
        self.assertEqual(ref_status, REFERENCED)

    def test_file_unreferenced_when_not_in_any_state(self):
        raw_file = self.work_dir / "short_1790500000_orphaned_raw.mp4"
        raw_file.write_bytes(b"clip content")

        index, err = load_reference_index(root=self.root)
        self.assertIsNotNone(index)
        ref_status, reasons, _ = check_file_references(raw_file, index, root=self.root)
        self.assertEqual(ref_status, UNREFERENCED)
        self.assertEqual(len(reasons), 0)

    def test_database_failure_fails_closed(self):
        raw_file = self.work_dir / "short_1790600000_orphaned_raw.mp4"
        raw_file.write_bytes(b"clip content")

        # Corrupt the SQLite database with invalid garbage bytes
        self.db_path.write_bytes(b"NOT A SQLITE DATABASE")

        index, err = load_reference_index(root=self.root)
        self.assertIsNone(index)
        self.assertIn("error", err.lower())

        ref_status, reasons, msg = check_file_references(raw_file, index, index_error=err, root=self.root)
        self.assertEqual(ref_status, UNKNOWN_REFERENCE)
        self.assertIn("fail closed", msg.lower())

    def test_corrupt_state_json_fails_closed(self):
        raw_file = self.work_dir / "short_1790700000_clip_raw.mp4"
        raw_file.write_bytes(b"clip content")

        state_path = self.root / "state.json"
        state_path.write_text("{ corrupt json data", encoding="utf-8")

        index, err = load_reference_index(root=self.root)
        self.assertIsNone(index)
        self.assertIn("state.json is corrupt", err)

        ref_status, reasons, msg = check_file_references(raw_file, index, index_error=err, root=self.root)
        self.assertEqual(ref_status, UNKNOWN_REFERENCE)


class RetentionClassificationSafetyPrecedenceTests(unittest.TestCase):
    """Tests the deterministic safety precedence of retention classification."""

    def test_inspection_error_has_highest_precedence(self):
        status, _ = classify_retention(
            path_valid=True, path_error="", age_status=AGE_ELIGIBLE, ref_status=UNREFERENCED,
            inspection_error="Disk read failure",
        )
        self.assertEqual(status, INSPECTION_ERROR)

    def test_invalid_path_precedes_reference_and_age(self):
        status, _ = classify_retention(
            path_valid=False, path_error="Symlink rejected", age_status=AGE_ELIGIBLE, ref_status=UNREFERENCED,
        )
        self.assertEqual(status, INVALID_PATH)

    def test_unknown_reference_precedes_age_eligible(self):
        status, _ = classify_retention(
            path_valid=True, path_error="", age_status=AGE_ELIGIBLE, ref_status=UNKNOWN_REFERENCE,
        )
        self.assertEqual(status, UNKNOWN_REFERENCE)

    def test_unknown_age_precedes_unreferenced(self):
        status, _ = classify_retention(
            path_valid=True, path_error="", age_status=UNKNOWN_AGE, ref_status=UNREFERENCED,
        )
        self.assertEqual(status, UNKNOWN_AGE)

    def test_referenced_prevents_age_eligible(self):
        status, _ = classify_retention(
            path_valid=True, path_error="", age_status=AGE_ELIGIBLE, ref_status=REFERENCED,
        )
        self.assertEqual(status, REFERENCED)

    def test_too_young_unreferenced_classified_as_too_young(self):
        status, _ = classify_retention(
            path_valid=True, path_error="", age_status=TOO_YOUNG, ref_status=UNREFERENCED,
        )
        self.assertEqual(status, TOO_YOUNG)

    def test_age_eligible_unreferenced_strictly_requires_both(self):
        status, _ = classify_retention(
            path_valid=True, path_error="", age_status=AGE_ELIGIBLE, ref_status=UNREFERENCED,
        )
        self.assertEqual(status, AGE_ELIGIBLE_UNREFERENCED)


class RetentionAuditDryRunTests(unittest.TestCase):
    """Tests high-level audit_retention() execution, summary metrics, and non-destructive guarantee."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.work_dir = self.root / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "studio.sqlite3"

        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS videos (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.commit()

        self.fixed_now = 1791000000.0

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_work_dir_returns_zero_counts(self):
        res = audit_retention(work_dir=self.work_dir, current_time=self.fixed_now, root=self.root)
        self.assertEqual(res["mode"], "dry_run")
        self.assertFalse(res["deletion_enabled"])
        self.assertFalse(res["deletion_occurred"])
        self.assertEqual(res["summary"]["total_files_scanned"], 0)
        self.assertEqual(len(res["files"]), 0)

    def test_audit_identifies_age_eligible_unreferenced_and_referenced_files(self):
        # 1. Old orphaned file (10 days old, no DB reference) -> age_eligible_unreferenced
        old_epoch = int(self.fixed_now - 864000.0)
        file1 = self.work_dir / f"short_{old_epoch}_orphaned1_raw.mp4"
        file1.write_bytes(b"A" * 1024 * 1024)  # 1 MB

        # 2. Young unreferenced file (2 days old, no DB reference) -> too_young
        young_epoch = int(self.fixed_now - 172800.0)
        file2 = self.work_dir / f"short_{young_epoch}_youngorphan_raw.mp4"
        file2.write_bytes(b"B" * 512 * 1024)  # 0.5 MB

        # 3. Old referenced file (10 days old, active draft in DB) -> referenced
        file3 = self.work_dir / f"short_{old_epoch}_activedraft_raw.mp4"
        file3.write_bytes(b"C" * 2048 * 1024)  # 2 MB
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.execute("INSERT INTO videos VALUES (?, ?)", (
                f"short_{old_epoch}_activedraft",
                json.dumps({"id": f"short_{old_epoch}_activedraft", "status": "ready"}),
            ))
            db.commit()

        # Run audit
        res = audit_retention(work_dir=self.work_dir, current_time=self.fixed_now, root=self.root)

        self.assertEqual(res["mode"], "dry_run")
        self.assertFalse(res["deletion_occurred"])
        summary = res["summary"]
        self.assertEqual(summary["total_files_scanned"], 3)
        self.assertEqual(summary["age_eligible_unreferenced_count"], 1)
        self.assertEqual(summary["too_young_count"], 1)
        self.assertEqual(summary["referenced_count"], 1)
        self.assertEqual(summary["estimated_reclaimable_bytes"], 1024 * 1024)
        self.assertAlmostEqual(summary["estimated_reclaimable_mb"], 1.0, places=1)

        # Verify no files were altered or deleted
        self.assertTrue(file1.exists())
        self.assertTrue(file2.exists())
        self.assertTrue(file3.exists())
        self.assertEqual(file1.stat().st_size, 1024 * 1024)
        self.assertEqual(file2.stat().st_size, 512 * 1024)
        self.assertEqual(file3.stat().st_size, 2048 * 1024)


class ServerRetentionApiIntegrationTests(unittest.TestCase):
    """Tests the server GET/POST /api/retention/audit endpoint integration."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.work_dir = self.root / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "studio.sqlite3"

        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS videos (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.commit()

        # Patch server ROOT to temporary directory
        self.patch_root = patch.object(server, "ROOT", self.root)
        self.patch_root.start()

    def tearDown(self):
        self.patch_root.stop()
        self.tmp.cleanup()

    def test_server_mutate_retention_audit(self):
        handler = server.StudioHandler.__new__(server.StudioHandler)
        res = handler.mutate("/api/retention/audit", {})
        self.assertIsNotNone(res)
        self.assertEqual(res["mode"], "dry_run")
        self.assertFalse(res["deletion_occurred"])
        self.assertIn("summary", res)
        self.assertIn("files", res)

    def test_server_get_retention_audit(self):
        # Verify direct audit execution matches server output
        expected = retention.audit_retention(root=self.root)
        handler = server.StudioHandler.__new__(server.StudioHandler)
        actual = handler.mutate("/api/retention", {})
        self.assertEqual(actual["mode"], expected["mode"])
        self.assertEqual(actual["summary"], expected["summary"])


if __name__ == "__main__":
    unittest.main()
