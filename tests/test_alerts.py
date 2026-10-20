"""
tests/test_alerts.py — Test Suite for Milestone 4 Operational Alert Lifecycle
Validates studio/alerts.py schema, transitions, recovery HWM, drain accounting, and replay idempotency.
"""

import os
import sqlite3
import tempfile
import threading
import time
import unittest

import studio.alerts as alerts


class AlertLifecycleTests(unittest.TestCase):
    def setUp(self):
        alerts.reset_engine_for_testing()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)

    def tearDown(self):
        alerts.reset_engine_for_testing()
        try:
            self.conn.close()
        except Exception:
            pass
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def test_schema_and_migration_compatibility(self):
        """Verify schema creation, column migration check, and partial unique index."""
        cur = self.conn.cursor()
        alerts.init_alert_schema(cur)

        # Check tables exist
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}
        self.assertIn("alerts", tables)
        self.assertIn("alert_recovery_markers", tables)

        # Check columns on alerts
        cur.execute("PRAGMA table_info(alerts)")
        cols = {row[1] for row in cur.fetchall()}
        self.assertIn("last_event_id", cols)
        self.assertIn("created_marker", cols)
        self.assertIn("last_marker", cols)
        self.assertIn("resolved_marker", cols)

        # Safe migration test: calling init_alert_schema a second time must be idempotent
        alerts.init_alert_schema(cur)

    def test_migration_preserves_existing_store_tables(self):
        """Verify that upgrading an existing database preserves videos and jobs tables."""
        # Create pre-existing store tables
        self.conn.execute("CREATE TABLE videos (id TEXT PRIMARY KEY, data TEXT);")
        self.conn.execute("INSERT INTO videos VALUES ('v1', '{\"title\":\"preserved\"}');")
        self.conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, data TEXT);")
        self.conn.execute("INSERT INTO jobs VALUES ('j1', '{\"status\":\"pending\"}');")
        self.conn.commit()

        # Run alert schema initialization
        cur = self.conn.cursor()
        alerts.init_alert_schema(cur)
        self.conn.commit()

        # Assert existing records are intact
        cur.execute("SELECT id, data FROM videos WHERE id = 'v1'")
        v_row = cur.fetchone()
        self.assertIsNotNone(v_row)
        self.assertIn("preserved", v_row[1])

        cur.execute("SELECT id, data FROM jobs WHERE id = 'j1'")
        j_row = cur.fetchone()
        self.assertIsNotNone(j_row)
        self.assertIn("pending", j_row[1])

    def test_marker_initialization_across_four_persisted_sources(self):
        """Seed sequence from 4 sources without arbitrary offset and reject duplicate start."""
        cur = self.conn.cursor()
        alerts.init_alert_schema(cur)

        # Row 1: created=10, last=25, resolved=NULL
        cur.execute("""
            INSERT INTO alerts (id, fingerprint, component, reason, status, severity, message, occurrence_count,
                                created_at, last_seen_at, created_marker, last_marker, last_event_id)
            VALUES ('a1', 'fp1', 'comp1', 'r1', 'active', 'warning', 'msg1', 1, '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', 10, 25, 'e1')
        """)
        # Row 2: created=30, last=40, resolved=75
        cur.execute("""
            INSERT INTO alerts (id, fingerprint, component, reason, status, severity, message, occurrence_count,
                                created_at, last_seen_at, created_marker, last_marker, last_event_id, resolved_at, resolved_marker, resolved_by)
            VALUES ('a2', 'fp2', 'comp2', 'r2', 'resolved', 'warning', 'msg2', 1, '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', 30, 40, 'e2', '2026-10-01T01:00:00Z', 75, 'admin')
        """)
        # Recovery HWM table: recovery_marker = 95 (highest)
        cur.execute("""
            INSERT INTO alert_recovery_markers (fingerprint, recovery_marker, updated_at)
            VALUES ('fp3', 95, '2026-10-01T02:00:00Z')
        """)
        self.conn.commit()

        alerts.start_alert_engine(self.conn)

        # First allocated marker must be strictly 95 + 1 = 96
        m1 = alerts.get_transition_marker()
        m2 = alerts.get_transition_marker()
        self.assertEqual(m1, 96)
        self.assertEqual(m2, 97)

        # Duplicate initialization must raise RuntimeError
        with self.assertRaises(RuntimeError):
            alerts.start_alert_engine(self.conn)

    def test_transition_matrix_and_replay_idempotency(self):
        """Test full transition matrix and verify exact replay preserves DB state without mutation."""
        alerts.start_alert_engine(self.conn)
        cur = self.conn.cursor()
        fp = "transcode_overload"

        # 1. New failure -> inserted
        m1 = alerts.get_transition_marker()
        e1 = alerts.AlertEvent("failure", fp, "transcoder", "oom", m1, "OOM 1", event_id=f"evt_{m1}")
        out1 = alerts.apply_transition_in_db(cur, e1)
        self.conn.commit()
        self.assertEqual(out1, "inserted")

        cur.execute("SELECT id, status, occurrence_count, last_marker, last_event_id FROM alerts WHERE fingerprint = ?", (fp,))
        row = cur.fetchone()
        alert_id, status, occ, lm, leid = row
        self.assertEqual(status, "active")
        self.assertEqual(occ, 1)
        self.assertEqual(lm, m1)
        self.assertEqual(leid, f"evt_{m1}")

        # 2. Exact duplicate replay -> redundant_noop (idempotency: zero DB mutation)
        out_replay = alerts.apply_transition_in_db(cur, e1)
        self.assertEqual(out_replay, "redundant_noop")
        cur.execute("SELECT occurrence_count, last_marker, last_event_id FROM alerts WHERE id = ?", (alert_id,))
        row_after_replay = cur.fetchone()
        self.assertEqual(row_after_replay[0], 1)
        self.assertEqual(row_after_replay[1], m1)
        self.assertEqual(row_after_replay[2], f"evt_{m1}")

        # 3. Delayed stale failure (marker < last_marker) -> stale_dropped
        e_stale = alerts.AlertEvent("failure", fp, "transcoder", "oom", m1 - 1, "Delayed", event_id="evt_old")
        out_stale = alerts.apply_transition_in_db(cur, e_stale)
        self.assertEqual(out_stale, "stale_dropped")

        # 4. Legitimate new failure (marker > last_marker) -> updated
        m2 = alerts.get_transition_marker()
        e2 = alerts.AlertEvent("failure", fp, "transcoder", "oom", m2, "OOM 2", event_id=f"evt_{m2}")
        out2 = alerts.apply_transition_in_db(cur, e2)
        self.conn.commit()
        self.assertEqual(out2, "updated")

        cur.execute("SELECT occurrence_count, last_marker, last_event_id FROM alerts WHERE id = ?", (alert_id,))
        row_after_m2 = cur.fetchone()
        self.assertEqual(row_after_m2[0], 2)
        self.assertEqual(row_after_m2[1], m2)
        self.assertEqual(row_after_m2[2], f"evt_{m2}")

        # 5. Acknowledgement -> acknowledged, recovery HWM untouched
        m_ack = alerts.get_transition_marker()
        e_ack = alerts.AlertEvent("acknowledgement", fp, "transcoder", "seen", m_ack, actor="operator_bob")
        out_ack = alerts.apply_transition_in_db(cur, e_ack)
        self.conn.commit()
        self.assertEqual(out_ack, "acknowledged")

        cur.execute("SELECT status, acknowledged_by FROM alerts WHERE id = ?", (alert_id,))
        row_ack = cur.fetchone()
        self.assertEqual(row_ack[0], "acknowledged")
        self.assertEqual(row_ack[1], "operator_bob")

        cur.execute("SELECT recovery_marker FROM alert_recovery_markers WHERE fingerprint = ?", (fp,))
        self.assertIsNone(cur.fetchone())

        # 6. Recovery -> resolved, advances recovery HWM
        m_rec = alerts.get_transition_marker()
        e_rec = alerts.AlertEvent("recovery", fp, "transcoder", "cleared", m_rec, actor="system")
        out_rec = alerts.apply_transition_in_db(cur, e_rec)
        self.conn.commit()
        self.assertEqual(out_rec, "resolved")

        cur.execute("SELECT status, resolved_marker FROM alerts WHERE id = ?", (alert_id,))
        row_rec = cur.fetchone()
        self.assertEqual(row_rec[0], "resolved")
        self.assertEqual(row_rec[1], m_rec)

        cur.execute("SELECT recovery_marker FROM alert_recovery_markers WHERE fingerprint = ?", (fp,))
        self.assertEqual(cur.fetchone()[0], m_rec)

        # 7. Delayed failure arriving after recovery (marker <= recovery_hwm) -> stale_dropped
        e_after_rec_stale = alerts.AlertEvent("failure", fp, "transcoder", "oom", m_rec - 1, "Late failure")
        out_late = alerts.apply_transition_in_db(cur, e_after_rec_stale)
        self.assertEqual(out_late, "stale_dropped")

        # 8. Partial unique index enforcement: duplicate active alert raises IntegrityError
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute("""
                INSERT INTO alerts (id, fingerprint, component, reason, status, severity, message, occurrence_count,
                                    created_at, last_seen_at, created_marker, last_marker)
                VALUES ('raw_1', 'idx_test', 'c', 'r', 'active', 'warning', 'm', 1, '2026-10-01', '2026-10-01', 1, 1)
            """)
            cur.execute("""
                INSERT INTO alerts (id, fingerprint, component, reason, status, severity, message, occurrence_count,
                                    created_at, last_seen_at, created_marker, last_marker)
                VALUES ('raw_2', 'idx_test', 'c', 'r', 'active', 'warning', 'm', 1, '2026-10-01', '2026-10-01', 2, 2)
            """)

    def test_post_commit_exception_and_accounting_invariant(self):
        """Inject exception immediately after SQLite commit and verify counters and fail-stop."""
        alerts.start_alert_engine(self.conn)
        m = alerts.get_transition_marker()
        ev = alerts.AlertEvent("failure", "disk_full", "disk", "full", m, "No space", event_id=f"evt_{m}")
        self.assertTrue(alerts.enqueue_alert_event(ev))

        # Fault after commit
        def fault_after_tx(e):
            return True

        worker_th = threading.Thread(
            target=alerts.drain_worker_loop,
            args=(self.conn,),
            kwargs={"fault_inject_after_tx": fault_after_tx},
            daemon=True,
        )
        worker_th.start()
        worker_th.join(timeout=2.0)

        # Direct query proves row DID persist
        cur = self.conn.cursor()
        cur.execute("SELECT status, last_marker FROM alerts WHERE fingerprint = 'disk_full'")
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "active")
        self.assertEqual(row[1], m)

        clean, diag = alerts.stop_alert_engine(timeout=1.0)
        self.assertFalse(clean, "Post-commit exception must cause unclean shutdown")
        self.assertEqual(diag["accepted"], 1)
        self.assertEqual(diag["committed"], 1)
        self.assertEqual(diag["persistence_failures"], 0)
        self.assertEqual(diag["queue_remaining"], 0)
        self.assertFalse(diag["drain_thread_healthy"])
        self.assertTrue(diag["accounting_matches"])


    def test_realistic_populated_legacy_database_migration(self):
        """Simulate a legacy DB missing all marker columns and containing duplicate active alerts."""
        cur = self.conn.cursor()
        # Create un-indexed legacy schema lacking last_event_id, created_marker, last_marker, resolved_marker
        cur.execute("""
            CREATE TABLE alerts (
                id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                component TEXT NOT NULL,
                reason TEXT NOT NULL,
                source_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                acknowledged_at TEXT DEFAULT NULL,
                acknowledged_by TEXT DEFAULT NULL,
                resolved_at TEXT DEFAULT NULL,
                resolution_reason TEXT DEFAULT NULL,
                resolved_by TEXT DEFAULT NULL
            );
        """)
        # Insert duplicate unresolved rows for fingerprint 'transcode_err'
        cur.execute("""
            INSERT INTO alerts (id, fingerprint, component, reason, status, severity, message, occurrence_count, created_at, last_seen_at)
            VALUES ('old_1', 'transcode_err', 'ffmpeg', 'oom', 'active', 'error', 'OOM 1', 1, '2026-10-18T10:00:00Z', '2026-10-18T10:00:00Z')
        """)
        cur.execute("""
            INSERT INTO alerts (id, fingerprint, component, reason, status, severity, message, occurrence_count, created_at, last_seen_at)
            VALUES ('old_2', 'transcode_err', 'ffmpeg', 'oom', 'active', 'error', 'OOM 2', 2, '2026-10-19T10:00:00Z', '2026-10-19T10:00:00Z')
        """)
        cur.execute("""
            INSERT INTO alerts (id, fingerprint, component, reason, status, severity, message, occurrence_count, created_at, last_seen_at)
            VALUES ('newest_3', 'transcode_err', 'ffmpeg', 'oom', 'active', 'error', 'OOM 3', 5, '2026-10-20T07:00:00Z', '2026-10-20T07:00:00Z')
        """)
        # Insert a pre-existing resolved row
        cur.execute("""
            INSERT INTO alerts (id, fingerprint, component, reason, status, severity, message, occurrence_count, created_at, last_seen_at, resolved_at, resolved_by)
            VALUES ('resolved_0', 'transcode_err', 'ffmpeg', 'oom', 'resolved', 'info', 'Old', 1, '2026-10-17T00:00:00Z', '2026-10-17T00:00:00Z', '2026-10-17T01:00:00Z', 'operator')
        """)
        self.conn.commit()

        # Execute migration
        alerts.init_alert_schema(cur)
        self.conn.commit()

        # 1. Verify all required columns were added
        cur.execute("PRAGMA table_info(alerts)")
        cols = {row[1] for row in cur.fetchall()}
        self.assertIn("last_event_id", cols)
        self.assertIn("created_marker", cols)
        self.assertIn("last_marker", cols)
        self.assertIn("resolved_marker", cols)

        # 2. Verify newest incident remains active
        cur.execute("SELECT status, occurrence_count FROM alerts WHERE id = 'newest_3'")
        row_newest = cur.fetchone()
        self.assertEqual(row_newest[0], "active")
        self.assertEqual(row_newest[1], 5)

        # 3. Verify older duplicate rows were preserved as resolved
        cur.execute("SELECT id, status, resolved_by FROM alerts WHERE id IN ('old_1', 'old_2') ORDER BY id")
        rows_older = cur.fetchall()
        self.assertEqual(len(rows_older), 2)
        for r in rows_older:
            self.assertEqual(r[1], "resolved")
            self.assertEqual(r[2], "migration")

        # 4. Verify exactly 1 active alert for 'transcode_err'
        active = alerts.list_active_alerts(self.conn)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], "newest_3")

        # 5. Verify unique partial index was created and prevents new duplicates
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute("""
                INSERT INTO alerts (id, fingerprint, component, reason, status, severity, message, occurrence_count,
                                    created_at, last_seen_at, created_marker, last_marker)
                VALUES ('fail_dup', 'transcode_err', 'ffmpeg', 'oom', 'active', 'error', 'dup', 1, '2026-10-20', '2026-10-20', 99, 99)
            """)

    def test_recovery_accounting_when_no_active_alert(self):
        """Recovery advancing HWM when idle returns recovery_hwm_advanced and counts as committed."""
        alerts.start_alert_engine(self.conn)
        cur = self.conn.cursor()

        # Enqueue a recovery for condition with no active alert
        m_rec = alerts.get_transition_marker()
        ev = alerts.AlertEvent("recovery", "idle_worker", "worker", "ok", m_rec, "System healthy")
        out = alerts.apply_transition_in_db(cur, ev)
        self.conn.commit()

        # Must report recovery_hwm_advanced, NOT redundant_noop
        self.assertEqual(out, "recovery_hwm_advanced")

        # HWM table must have committed the new marker
        cur.execute("SELECT recovery_marker FROM alert_recovery_markers WHERE fingerprint = 'idle_worker'")
        self.assertEqual(cur.fetchone()[0], m_rec)

        # Replaying identical marker is now stale_dropped
        out_replay = alerts.apply_transition_in_db(cur, ev)
        self.assertEqual(out_replay, "stale_dropped")

    def test_stale_acknowledgement_rejected(self):
        """Stale acknowledgement (marker < last_marker or <= recovery_hwm) is rejected without mutating state."""
        alerts.start_alert_engine(self.conn)
        cur = self.conn.cursor()
        fp = "queue_stall"

        # 1. Active alert created at m1 = 10
        m1 = 10
        e1 = alerts.AlertEvent("failure", fp, "queue", "stalled", m1, "Stall 1")
        self.assertEqual(alerts.apply_transition_in_db(cur, e1), "inserted")
        self.conn.commit()

        # 2. Subsequent failure advances last_marker to m2 = 20
        m2 = 20
        e2 = alerts.AlertEvent("failure", fp, "queue", "stalled", m2, "Stall 2")
        self.assertEqual(alerts.apply_transition_in_db(cur, e2), "updated")
        self.conn.commit()

        # 3. Acknowledgement issued with stale marker (m_ack = 15 < 20)
        e_ack_stale = alerts.AlertEvent("acknowledgement", fp, "queue", "seen", 15, actor="operator_alice")
        out_stale = alerts.apply_transition_in_db(cur, e_ack_stale)
        self.assertEqual(out_stale, "stale_dropped")

        # Verify incident status remains active (NOT acknowledged)
        cur.execute("SELECT status, acknowledged_at FROM alerts WHERE fingerprint = ?", (fp,))
        row = cur.fetchone()
        self.assertEqual(row[0], "active")
        self.assertIsNone(row[1])

        # 4. Fresh acknowledgement with marker m_ack = 25 (> 20) succeeds
        e_ack_fresh = alerts.AlertEvent("acknowledgement", fp, "queue", "seen", 25, actor="operator_alice")
        out_fresh = alerts.apply_transition_in_db(cur, e_ack_fresh)
        self.assertEqual(out_fresh, "acknowledged")
        self.conn.commit()

        cur.execute("SELECT status, acknowledged_by FROM alerts WHERE fingerprint = ?", (fp,))
        row_fresh = cur.fetchone()
        self.assertEqual(row_fresh[0], "acknowledged")
        self.assertEqual(row_fresh[1], "operator_alice")

        # 5. Duplicate acknowledgement is redundant_noop
        out_dup = alerts.apply_transition_in_db(cur, e_ack_fresh)
        self.assertEqual(out_dup, "redundant_noop")

    def test_dedicated_conn_not_closed_if_drain_worker_alive(self):
        """stop_alert_engine() must NOT close _DEDICATED_CONN if the drain worker thread is still alive."""
        alerts.start_alert_engine(self.db_path)
        hold_worker = threading.Event()

        def stuck_worker(conn):
            hold_worker.wait(timeout=2.0)

        # Replace drain thread with a stuck thread
        stuck_th = threading.Thread(target=stuck_worker, args=(self.conn,), daemon=True)
        alerts._DRAIN_THREAD = stuck_th
        stuck_th.start()

        # Stop alert engine with short timeout
        clean, diag = alerts.stop_alert_engine(timeout=0.1)

        self.assertFalse(clean)
        self.assertFalse(diag["drain_thread_stopped"])
        self.assertFalse(diag["connection_closed"], "Connection must NOT be closed while worker is alive")
        self.assertIsNotNone(alerts._DEDICATED_CONN, "Connection handle must remain open while worker is alive")

        hold_worker.set()
        stuck_th.join(timeout=1.0)
        # Teardown will safely close connection

    def test_acknowledgement_marker_equality_and_semantics(self):
        """Test acknowledgement marker semantics: older, equal marker, newer marker, and exact replay."""
        alerts.start_alert_engine(self.conn)
        cur = self.conn.cursor()
        fp = "transcode_gpu_hang"

        # 1. Failure at m1 = 10 -> inserted
        m1 = 10
        e1 = alerts.AlertEvent("failure", fp, "transcoder", "gpu_hang", m1, "Hang 1", event_id=f"evt_{m1}")
        self.assertEqual(alerts.apply_transition_in_db(cur, e1), "inserted")
        self.conn.commit()

        # 2. Subsequent failure at m2 = 20 -> updated
        m2 = 20
        e2 = alerts.AlertEvent("failure", fp, "transcoder", "gpu_hang", m2, "Hang 2", event_id=f"evt_{m2}")
        self.assertEqual(alerts.apply_transition_in_db(cur, e2), "updated")
        self.conn.commit()

        # Check DB state
        cur.execute("SELECT status, last_marker, acknowledged_at FROM alerts WHERE fingerprint = ?", (fp,))
        row = cur.fetchone()
        self.assertEqual(row[0], "active")
        self.assertEqual(row[1], 20)
        self.assertIsNone(row[2])

        # Case A: Acknowledgement older than latest failure (m_ack = 15 < 20) -> stale_dropped
        e_older = alerts.AlertEvent("acknowledgement", fp, "transcoder", "ack", 15, actor="op1", event_id="ack_15")
        out_older = alerts.apply_transition_in_db(cur, e_older)
        self.assertEqual(out_older, "stale_dropped")
        cur.execute("SELECT status, acknowledged_at FROM alerts WHERE fingerprint = ?", (fp,))
        row_after_older = cur.fetchone()
        self.assertEqual(row_after_older[0], "active")
        self.assertIsNone(row_after_older[1])

        # Case B: Acknowledgement with SAME marker as latest failure (m_ack = 20 == 20) but different event_id -> stale_dropped
        e_same = alerts.AlertEvent("acknowledgement", fp, "transcoder", "ack", 20, actor="op1", event_id="ack_same_20")
        out_same = alerts.apply_transition_in_db(cur, e_same)
        self.assertEqual(out_same, "stale_dropped")
        cur.execute("SELECT status, acknowledged_at FROM alerts WHERE fingerprint = ?", (fp,))
        row_after_same = cur.fetchone()
        self.assertEqual(row_after_same[0], "active")
        self.assertIsNone(row_after_same[1])

        # Case C: Valid acknowledgement with newer marker (m_ack = 25 > 20) -> acknowledged
        e_valid = alerts.AlertEvent("acknowledgement", fp, "transcoder", "ack", 25, actor="op1", event_id="ack_valid_25")
        out_valid = alerts.apply_transition_in_db(cur, e_valid)
        self.conn.commit()
        self.assertEqual(out_valid, "acknowledged")
        cur.execute("SELECT status, acknowledged_by FROM alerts WHERE fingerprint = ?", (fp,))
        row_after_valid = cur.fetchone()
        self.assertEqual(row_after_valid[0], "acknowledged")
        self.assertEqual(row_after_valid[1], "op1")

        # Case D: Exact replay of valid acknowledgement -> redundant_noop
        out_replay = alerts.apply_transition_in_db(cur, e_valid)
        self.assertEqual(out_replay, "redundant_noop")
        cur.execute("SELECT status, acknowledged_by FROM alerts WHERE fingerprint = ?", (fp,))
        row_after_replay = cur.fetchone()
        self.assertEqual(row_after_replay[0], "acknowledged")
        self.assertEqual(row_after_replay[1], "op1")

    def test_migration_failure_injection_rollback_and_idempotency(self):
        """
        Comprehensive test of SQLite migration atomicity, failure injection, rollback,
        and second-run idempotency on a populated legacy database.
        """
        # 1. Create a populated legacy database and record schema and data
        self.conn.execute("CREATE TABLE videos (id TEXT PRIMARY KEY, title TEXT);")
        self.conn.execute("INSERT INTO videos VALUES ('vid_1', 'Original Video');")
        self.conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, stage TEXT);")
        self.conn.execute("INSERT INTO jobs VALUES ('job_1', 'render');")
        self.conn.execute("""
            CREATE TABLE alerts (
                id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                component TEXT NOT NULL,
                reason TEXT NOT NULL,
                source_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                acknowledged_at TEXT DEFAULT NULL,
                acknowledged_by TEXT DEFAULT NULL,
                resolved_at TEXT DEFAULT NULL,
                resolution_reason TEXT DEFAULT NULL,
                resolved_by TEXT DEFAULT NULL
            );
        """)
        self.conn.execute("""
            INSERT INTO alerts (id, fingerprint, component, reason, status, severity, message, occurrence_count, created_at, last_seen_at)
            VALUES ('a_dup1', 'fp_collision', 'c1', 'r1', 'active', 'warning', 'Msg 1', 1, '2026-10-18T00:00:00Z', '2026-10-18T01:00:00Z')
        """)
        self.conn.execute("""
            INSERT INTO alerts (id, fingerprint, component, reason, status, severity, message, occurrence_count, created_at, last_seen_at)
            VALUES ('a_dup2', 'fp_collision', 'c1', 'r1', 'active', 'warning', 'Msg 2', 4, '2026-10-19T00:00:00Z', '2026-10-19T05:00:00Z')
        """)
        self.conn.commit()

        cur = self.conn.cursor()
        cur.execute("PRAGMA table_info(alerts)")
        init_cols = [r[1] for r in cur.fetchall()]
        cur.execute("SELECT id, fingerprint, status, occurrence_count FROM alerts ORDER BY id")
        init_alert_rows = cur.fetchall()
        cur.execute("SELECT id, title FROM videos ORDER BY id")
        init_video_rows = cur.fetchall()
        cur.execute("SELECT id, stage FROM jobs ORDER BY id")
        init_job_rows = cur.fetchall()

        # 2 & 3. Start migration with injected failure after schema mutation (ALTER TABLE ADD COLUMN)
        with self.assertRaises(RuntimeError) as ctx:
            alerts.init_alert_schema(cur, fault_inject_after_step="after_columns")
        self.assertIn("Simulated failure after column migration", str(ctx.exception))

        # 4. Reopen the database from disk
        self.conn.close()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cur = self.conn.cursor()

        # 5. Verify exact expected rollback behavior
        cur.execute("PRAGMA table_info(alerts)")
        reopened_cols = [r[1] for r in cur.fetchall()]
        self.assertEqual(init_cols, reopened_cols, "Migrated columns must be rolled back on failure")
        self.assertNotIn("last_event_id", reopened_cols)
        self.assertNotIn("created_marker", reopened_cols)
        self.assertNotIn("last_marker", reopened_cols)
        self.assertNotIn("resolved_marker", reopened_cols)

        cur.execute("SELECT id, fingerprint, status, occurrence_count FROM alerts ORDER BY id")
        reopened_alert_rows = cur.fetchall()
        self.assertEqual(init_alert_rows, reopened_alert_rows, "Incident rows must be preserved in original state")

        cur.execute("SELECT id, title FROM videos ORDER BY id")
        self.assertEqual(init_video_rows, cur.fetchall(), "Unrelated videos table must be intact")
        cur.execute("SELECT id, stage FROM jobs ORDER BY id")
        self.assertEqual(init_job_rows, cur.fetchall(), "Unrelated jobs table must be intact")

        # 6. Now perform a clean, successful migration (Run 1)
        alerts.init_alert_schema(cur)

        # Inspect established state after Run 1
        cur.execute("PRAGMA table_info(alerts)")
        run1_cols = [r[1] for r in cur.fetchall()]
        self.assertIn("last_event_id", run1_cols)
        self.assertIn("created_marker", run1_cols)
        self.assertIn("last_marker", run1_cols)
        self.assertIn("resolved_marker", run1_cols)

        cur.execute("SELECT id, status, occurrence_count, last_marker, resolved_by FROM alerts ORDER BY id")
        state_run1 = cur.fetchall()
        # Older duplicate must be resolved by migration; newer duplicate must remain active
        self.assertEqual(state_run1[0][0], "a_dup1")
        self.assertEqual(state_run1[0][1], "resolved")
        self.assertEqual(state_run1[0][4], "migration")
        self.assertEqual(state_run1[1][0], "a_dup2")
        self.assertEqual(state_run1[1][1], "active")

        # 7 & 8. Run migration a second time (Run 2) - must leave all established state unchanged
        alerts.init_alert_schema(cur)
        cur.execute("SELECT id, status, occurrence_count, last_marker, resolved_by FROM alerts ORDER BY id")
        state_run2 = cur.fetchall()
        self.assertEqual(state_run1, state_run2, "Second migration run must leave established state 100% unchanged")

    def test_migration_with_preexisting_outer_transaction(self):
        """Verify init_alert_schema handles connections with pre-existing outer transactions without nested BEGIN errors."""
        cur = self.conn.cursor()
        self.conn.execute("CREATE TABLE videos (id TEXT PRIMARY KEY, title TEXT);")
        self.conn.commit()

        # Start outer transaction explicitly
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("INSERT INTO videos VALUES ('v_outer', 'Pending outer tx')")
        self.assertTrue(self.conn.in_transaction)

        # Run alert schema migration inside existing transaction
        alerts.init_alert_schema(cur)

        # Outer transaction must still be active and uncommitted
        self.assertTrue(self.conn.in_transaction)

        # Commit outer transaction
        self.conn.commit()

        # Verify both outer work and migration are committed
        cur.execute("SELECT id FROM videos WHERE id = 'v_outer'")
        self.assertIsNotNone(cur.fetchone())
        cur.execute("PRAGMA table_info(alerts)")
        cols = {r[1] for r in cur.fetchall()}
        self.assertIn("created_marker", cols)

    def test_migration_minimal_legacy_schema_all_columns_added(self):
        """
        Verify migration safely handles ultra-minimal legacy alerts table:
        asserts all column definitions/defaults, required indexes, recovery table schema,
        duplicate active incident resolution and prevention, persistence after reopening, and idempotency.
        """
        self.conn.execute("""
            CREATE TABLE alerts (
                id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                component TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
        """)
        # Populate with duplicate active rows to test deduplication and partial index creation
        self.conn.execute("""
            INSERT INTO alerts (id, fingerprint, component, reason, status, severity, message, occurrence_count, created_at, last_seen_at)
            VALUES ('a_dup1', 'fp_collision', 'c1', 'r1', 'active', 'warning', 'Msg 1', 1, '2026-10-18T00:00:00Z', '2026-10-18T01:00:00Z')
        """)
        self.conn.execute("""
            INSERT INTO alerts (id, fingerprint, component, reason, status, severity, message, occurrence_count, created_at, last_seen_at)
            VALUES ('a_dup2', 'fp_collision', 'c1', 'r1', 'active', 'warning', 'Msg 2', 4, '2026-10-19T00:00:00Z', '2026-10-19T05:00:00Z')
        """)
        self.conn.commit()

        cur = self.conn.cursor()
        alerts.init_alert_schema(cur)

        # 1. Assert all 21 columns exist and have expected defaults/nullability
        cur.execute("PRAGMA table_info(alerts)")
        info = {r[1]: {'type': r[2], 'notnull': r[3], 'dflt_value': r[4]} for r in cur.fetchall()}
        expected_cols = {
            "id", "fingerprint", "component", "reason", "source_id", "status", "severity",
            "message", "metadata", "occurrence_count", "created_at", "last_seen_at",
            "created_marker", "last_marker", "last_event_id", "acknowledged_at",
            "acknowledged_by", "resolved_at", "resolved_marker", "resolution_reason", "resolved_by"
        }
        self.assertTrue(expected_cols.issubset(set(info.keys())), f"Missing columns: {expected_cols - set(info.keys())}")
        self.assertEqual(info['created_marker']['notnull'], 1)
        self.assertIn(str(info['created_marker']['dflt_value']), ('0', '0.0'))
        self.assertEqual(info['last_marker']['notnull'], 1)
        self.assertEqual(info['last_event_id']['notnull'], 1)
        self.assertEqual(info['resolved_marker']['notnull'], 0)

        # 2. Assert alert_recovery_markers table and schema
        cur.execute("PRAGMA table_info(alert_recovery_markers)")
        rec_info = {r[1]: r[2] for r in cur.fetchall()}
        self.assertEqual(set(rec_info.keys()), {'fingerprint', 'recovery_marker', 'updated_at'})

        # 3. Assert required indexes and partial index properties
        cur.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'alerts'")
        indexes = {r[0]: (r[1] or "") for r in cur.fetchall()}
        self.assertIn("idx_alerts_active", indexes)
        self.assertIn("idx_alerts_fingerprint", indexes)
        self.assertIn("WHERE resolved_at IS NULL", indexes["idx_alerts_active"])

        # 4. Assert duplicate active incidents deduplication
        cur.execute("SELECT id, status, resolved_by FROM alerts ORDER BY id")
        rows = cur.fetchall()
        self.assertEqual(rows[0], ('a_dup1', 'resolved', 'migration'))
        self.assertEqual(rows[1], ('a_dup2', 'active', None))

        # 5. Assert duplicate active incident prevention via partial unique index
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute("""
                INSERT INTO alerts (id, fingerprint, component, reason, status, severity, message, occurrence_count,
                                    created_at, last_seen_at, created_marker, last_marker)
                VALUES ('a_dup3', 'fp_collision', 'c1', 'r1', 'active', 'warning', 'Msg 3', 1,
                        '2026-10-20T00:00:00Z', '2026-10-20T00:00:00Z', 10, 10)
            """)

        # Inserting a resolved alert with the same fingerprint must succeed
        cur.execute("""
            INSERT INTO alerts (id, fingerprint, component, reason, status, severity, message, occurrence_count,
                                created_at, last_seen_at, created_marker, last_marker, resolved_at, resolved_marker, resolved_by)
            VALUES ('a_dup_res', 'fp_collision', 'c1', 'r1', 'resolved', 'warning', 'Msg Res', 1,
                    '2026-10-20T00:00:00Z', '2026-10-20T00:00:00Z', 11, 11, '2026-10-20T01:00:00Z', 12, 'admin')
        """)
        self.conn.commit()

        # 6. Assert persistence after closing and reopening database
        self.conn.close()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cur_r = self.conn.cursor()
        cur_r.execute("PRAGMA table_info(alerts)")
        reopened_cols = {r[1] for r in cur_r.fetchall()}
        self.assertTrue(expected_cols.issubset(reopened_cols))

        cur_r.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'alerts'")
        reopened_indexes = {r[0] for r in cur_r.fetchall()}
        self.assertIn("idx_alerts_active", reopened_indexes)

        # 7. Assert migration idempotency on second run
        alerts.init_alert_schema(cur_r)
        cur_r.execute("PRAGMA table_info(alerts)")
        run2_cols = {r[1] for r in cur_r.fetchall()}
        self.assertEqual(reopened_cols, run2_cols, "Second migration run must leave schema identical")

    def test_alert_engine_restart_rejected_while_drain_worker_alive(self):
        """A subsequent start_alert_engine() call must fail safely if the previous drain worker is still alive."""
        alerts.start_alert_engine(self.db_path)
        hold_worker = threading.Event()

        def stuck_worker(conn):
            hold_worker.wait(timeout=2.0)

        # Replace drain worker with stuck worker
        stuck_th = threading.Thread(target=stuck_worker, args=(alerts._DEDICATED_CONN,), daemon=True)
        alerts._DRAIN_THREAD = stuck_th
        stuck_th.start()

        # Attempt to stop alert engine - times out
        clean, diag = alerts.stop_alert_engine(timeout=0.1)
        self.assertFalse(clean)
        self.assertFalse(diag["drain_thread_stopped"])

        # Attempt to restart alert engine while worker is still alive must raise RuntimeError
        with self.assertRaises(RuntimeError) as ctx:
            alerts.start_alert_engine(self.db_path)
        self.assertIn("previous drain worker is still alive", str(ctx.exception))

        # Live drain thread reference must NOT be overwritten
        self.assertIs(alerts._DRAIN_THREAD, stuck_th)
        self.assertTrue(stuck_th.is_alive())

        # Unblock and cleanly terminate
        hold_worker.set()
        stuck_th.join(timeout=1.0)

if __name__ == "__main__":
    unittest.main()
