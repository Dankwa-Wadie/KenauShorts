"""
studio/alerts.py — Operational Alert Lifecycle and SQLite Partial Index
Provides monotonic marker allocation, SQLite schema with partial index,
transition matrix enforcement, operator mutations, and fail-stop drain worker.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger("kenaushorts.alerts")

COMMITTED_OUTCOMES = frozenset({"inserted", "updated", "resolved", "acknowledged", "recovery_hwm_advanced"})
STALE_OR_REDUNDANT_OUTCOMES = frozenset({"stale_dropped", "redundant_noop"})

# ---------------------------------------------------------------------------
# Global State & Locks
# ---------------------------------------------------------------------------
_TRANSITION_LOCK = threading.Lock()
_TRANSITION_SEQ: int = 0
_INITIALIZED: bool = False

_ENQUEUE_LOCK = threading.Lock()
_ENGINE_STATE: str = "uninitialized"  # uninitialized -> running -> stopping -> stopped | degraded_*
_ALERT_QUEUE: queue.Queue[AlertEvent] = queue.Queue(maxsize=1024)
_SHUTDOWN_EVENT = threading.Event()
_DRAIN_THREAD: Optional[threading.Thread] = None
_DEDICATED_CONN: Optional[sqlite3.Connection] = None
_CURRENT_EVENT_BEING_PROCESSED: Optional[AlertEvent] = None
_DRAIN_THREAD_HEALTHY: bool = True

_ACCEPTED_EVENTS_COUNT: int = 0
_COMMITTED_EVENTS_COUNT: int = 0
_DROPPED_STALE_COUNT: int = 0
_DROPPED_ALERTS_COUNT: int = 0
_PERSISTENCE_FAILURES_COUNT: int = 0
_HAS_PERSISTENCE_FAILURE: bool = False
_ACTIVE_OPERATOR_TRANSITIONS: int = 0


class AlertEvent:
    def __init__(
        self,
        event_type: str,
        fingerprint: str,
        component: str,
        reason: str,
        marker: int,
        message: str = "",
        metadata: Optional[dict[str, Any]] = None,
        severity: str = "warning",
        actor: str = "system",
        source_id: str = "",
        event_id: str = "",
    ):
        self.type = event_type
        self.fingerprint = fingerprint
        self.component = component
        self.reason = reason
        self.marker = marker
        self.message = message
        self.metadata = metadata or {}
        self.severity = severity
        self.actor = actor
        self.source_id = source_id
        self.event_id = event_id or f"evt_{fingerprint}_{marker}"

    @property
    def metadata_json(self) -> str:
        try:
            return json.dumps(self.metadata)
        except Exception:
            return "{}"


class AlertNotFoundError(Exception):
    pass


# ---------------------------------------------------------------------------
# Schema DDL & Safe Migration
# ---------------------------------------------------------------------------
def init_alert_schema(cur: sqlite3.Cursor, fault_inject_after_step: Optional[str] = None) -> None:
    """
    Initializes schema, migrations, backfills, deduplication, and partial index.
    Uses an explicit SAVEPOINT transaction to guarantee rollback atomicity and recovery safety
    for both fresh databases and connections with pre-existing transactions.
    """
    sp_name = "alert_schema_migration_sp"
    cur.execute(f"SAVEPOINT {sp_name};")
    try:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            component TEXT NOT NULL CHECK(length(component) <= 32),
            reason TEXT NOT NULL CHECK(length(reason) <= 64),
            source_id TEXT NOT NULL DEFAULT '' CHECK(length(source_id) <= 128),
            status TEXT NOT NULL CHECK(status IN ('active', 'acknowledged', 'resolved')),
            severity TEXT NOT NULL CHECK(severity IN ('info', 'warning', 'error', 'critical')),
            message TEXT NOT NULL CHECK(length(message) <= 500),
            metadata TEXT NOT NULL DEFAULT '{}' CHECK(length(metadata) <= 2048),
            occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK(occurrence_count >= 1),
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            created_marker INTEGER NOT NULL,
            last_marker INTEGER NOT NULL,
            last_event_id TEXT NOT NULL DEFAULT '' CHECK(length(last_event_id) <= 128),
            acknowledged_at TEXT DEFAULT NULL,
            acknowledged_by TEXT DEFAULT NULL CHECK(acknowledged_by IS NULL OR length(acknowledged_by) <= 64),
            resolved_at TEXT DEFAULT NULL,
            resolved_marker INTEGER DEFAULT NULL,
            resolution_reason TEXT DEFAULT NULL CHECK(resolution_reason IS NULL OR length(resolution_reason) <= 255),
            resolved_by TEXT DEFAULT NULL CHECK(resolved_by IS NULL OR length(resolved_by) <= 64),
            CHECK (
                (status = 'active' AND acknowledged_at IS NULL AND acknowledged_by IS NULL AND resolved_at IS NULL AND resolved_marker IS NULL AND resolution_reason IS NULL AND resolved_by IS NULL) OR
                (status = 'acknowledged' AND acknowledged_at IS NOT NULL AND acknowledged_by IS NOT NULL AND resolved_at IS NULL AND resolved_marker IS NULL AND resolution_reason IS NULL AND resolved_by IS NULL) OR
                (status = 'resolved' AND resolved_at IS NOT NULL AND resolved_marker IS NOT NULL AND resolved_by IS NOT NULL AND (
                    (acknowledged_at IS NULL AND acknowledged_by IS NULL) OR
                    (acknowledged_at IS NOT NULL AND acknowledged_by IS NOT NULL)
                ))
            )
        );
        """)

        # Safe migration: ensure all required columns exist if pre-existing schema lacked them
        cur.execute("PRAGMA table_info(alerts)")
        cols = {row[1] for row in cur.fetchall()}
        if cols:
            if "source_id" not in cols:
                cur.execute("ALTER TABLE alerts ADD COLUMN source_id TEXT NOT NULL DEFAULT '' CHECK(length(source_id) <= 128);")
            if "metadata" not in cols:
                cur.execute("ALTER TABLE alerts ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}' CHECK(length(metadata) <= 2048);")
            if "last_event_id" not in cols:
                cur.execute("ALTER TABLE alerts ADD COLUMN last_event_id TEXT NOT NULL DEFAULT '' CHECK(length(last_event_id) <= 128);")
            if "created_marker" not in cols:
                cur.execute("ALTER TABLE alerts ADD COLUMN created_marker INTEGER NOT NULL DEFAULT 0;")
                cur.execute("UPDATE alerts SET created_marker = rowid WHERE created_marker = 0;")
            if "last_marker" not in cols:
                cur.execute("ALTER TABLE alerts ADD COLUMN last_marker INTEGER NOT NULL DEFAULT 0;")
                cur.execute("UPDATE alerts SET last_marker = created_marker WHERE last_marker = 0;")
            if "acknowledged_at" not in cols:
                cur.execute("ALTER TABLE alerts ADD COLUMN acknowledged_at TEXT DEFAULT NULL;")
            if "acknowledged_by" not in cols:
                cur.execute("ALTER TABLE alerts ADD COLUMN acknowledged_by TEXT DEFAULT NULL CHECK(acknowledged_by IS NULL OR length(acknowledged_by) <= 64);")
            if "resolved_at" not in cols:
                cur.execute("ALTER TABLE alerts ADD COLUMN resolved_at TEXT DEFAULT NULL;")
            if "resolved_marker" not in cols:
                cur.execute("ALTER TABLE alerts ADD COLUMN resolved_marker INTEGER DEFAULT NULL;")
                cur.execute("UPDATE alerts SET resolved_marker = last_marker WHERE status = 'resolved' AND resolved_marker IS NULL;")
            if "resolution_reason" not in cols:
                cur.execute("ALTER TABLE alerts ADD COLUMN resolution_reason TEXT DEFAULT NULL CHECK(resolution_reason IS NULL OR length(resolution_reason) <= 255);")
            if "resolved_by" not in cols:
                cur.execute("ALTER TABLE alerts ADD COLUMN resolved_by TEXT DEFAULT NULL CHECK(resolved_by IS NULL OR length(resolved_by) <= 64);")

        if fault_inject_after_step == "after_columns":
            raise RuntimeError("Simulated failure after column migration")

        # Deduplicate unresolved alerts before creating unique partial index:
        cur.execute("""
            SELECT fingerprint, COUNT(*) as cnt
            FROM alerts
            WHERE resolved_at IS NULL
            GROUP BY fingerprint
            HAVING cnt > 1
        """)
        duplicate_fps = [row[0] for row in cur.fetchall()]
        for fp in duplicate_fps:
            cur.execute("""
                SELECT id, last_seen_at, last_marker, acknowledged_at, acknowledged_by
                FROM alerts
                WHERE fingerprint = ? AND resolved_at IS NULL
                ORDER BY last_seen_at DESC, rowid DESC
            """, (fp,))
            rows = cur.fetchall()
            for dup in rows[1:]:
                dup_id = dup[0]
                dup_seen = dup[1] or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                dup_marker = dup[2] if (dup[2] is not None and dup[2] > 0) else 1
                ack_by = dup[4] if dup[3] else None
                ack_at = dup[3] if dup[4] else None
                cur.execute("""
                    UPDATE alerts
                    SET status = 'resolved',
                        resolved_at = ?,
                        resolved_marker = ?,
                        resolution_reason = 'Auto-resolved during migration (superseded by active incident)',
                        resolved_by = 'migration',
                        acknowledged_at = ?,
                        acknowledged_by = ?
                    WHERE id = ?
                """, (dup_seen, dup_marker, ack_at, ack_by, dup_id))

        if fault_inject_after_step == "after_dedup":
            raise RuntimeError("Simulated failure after deduplication")

        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_active ON alerts(fingerprint) WHERE resolved_at IS NULL;")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_fingerprint ON alerts(fingerprint, last_marker DESC);")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS alert_recovery_markers (
            fingerprint TEXT PRIMARY KEY,
            recovery_marker INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)

        cur.execute(f"RELEASE SAVEPOINT {sp_name};")
    except Exception:
        try:
            cur.execute(f"ROLLBACK TO SAVEPOINT {sp_name};")
            cur.execute(f"RELEASE SAVEPOINT {sp_name};")
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# Monotonic Sequence Seeding & Marker Allocation
# ---------------------------------------------------------------------------
def start_alert_engine(db_or_conn: Optional[sqlite3.Connection | Path | str] = None) -> None:
    global _TRANSITION_SEQ, _INITIALIZED, _ENGINE_STATE, _DRAIN_THREAD, _DEDICATED_CONN, _DRAIN_THREAD_HEALTHY
    global _ACCEPTED_EVENTS_COUNT, _COMMITTED_EVENTS_COUNT, _DROPPED_STALE_COUNT, _DROPPED_ALERTS_COUNT
    global _PERSISTENCE_FAILURES_COUNT, _HAS_PERSISTENCE_FAILURE

    with _TRANSITION_LOCK:
        if _DRAIN_THREAD and _DRAIN_THREAD.is_alive():
            raise RuntimeError(
                "Cannot start alert engine: previous drain worker is still alive after timeout. Restart rejected."
            )
        if _INITIALIZED:
            raise RuntimeError("Alert engine is already initialized. Duplicate initialization rejected.")

        conn_to_use: sqlite3.Connection
        spawn_worker = False

        if isinstance(db_or_conn, sqlite3.Connection):
            conn_to_use = db_or_conn
        else:
            if db_or_conn is None:
                import studio.store as store
                db_path = store.DB
            else:
                db_path = Path(db_or_conn)
            _DEDICATED_CONN = sqlite3.connect(db_path, check_same_thread=False, timeout=15.0)
            _DEDICATED_CONN.execute("PRAGMA journal_mode=WAL;")
            _DEDICATED_CONN.execute("PRAGMA synchronous=NORMAL;")
            _DEDICATED_CONN.execute("PRAGMA busy_timeout=15000;")
            conn_to_use = _DEDICATED_CONN
            spawn_worker = True

        cur = conn_to_use.cursor()
        init_alert_schema(cur)
        if spawn_worker and conn_to_use.in_transaction:
            conn_to_use.commit()

        cur.execute("""
            SELECT COALESCE(MAX(m), 0) FROM (
                SELECT MAX(created_marker) AS m FROM alerts
                UNION ALL
                SELECT MAX(last_marker) AS m FROM alerts
                UNION ALL
                SELECT MAX(resolved_marker) AS m FROM alerts
                UNION ALL
                SELECT MAX(recovery_marker) AS m FROM alert_recovery_markers
            )
        """)
        db_max = cur.fetchone()[0]
        _TRANSITION_SEQ = db_max
        _INITIALIZED = True

    with _ENQUEUE_LOCK:
        _ENGINE_STATE = "running"
        _SHUTDOWN_EVENT.clear()
        _DRAIN_THREAD_HEALTHY = True
        _ACCEPTED_EVENTS_COUNT = 0
        _COMMITTED_EVENTS_COUNT = 0
        _DROPPED_STALE_COUNT = 0
        _DROPPED_ALERTS_COUNT = 0
        _PERSISTENCE_FAILURES_COUNT = 0
        _HAS_PERSISTENCE_FAILURE = False

        if spawn_worker and _DEDICATED_CONN:
            _DRAIN_THREAD = threading.Thread(
                target=drain_worker_loop,
                args=(_DEDICATED_CONN,),
                name="AlertDrainWorker",
                daemon=True,
            )
            _DRAIN_THREAD.start()


def reset_engine_for_testing() -> None:
    """Testing helper to reset global state between isolated unit tests."""
    global _TRANSITION_SEQ, _INITIALIZED, _ENGINE_STATE, _DRAIN_THREAD, _DEDICATED_CONN, _DRAIN_THREAD_HEALTHY
    global _ACCEPTED_EVENTS_COUNT, _COMMITTED_EVENTS_COUNT, _DROPPED_STALE_COUNT, _DROPPED_ALERTS_COUNT
    global _PERSISTENCE_FAILURES_COUNT, _HAS_PERSISTENCE_FAILURE, _CURRENT_EVENT_BEING_PROCESSED

    _SHUTDOWN_EVENT.set()
    if _DRAIN_THREAD and _DRAIN_THREAD.is_alive():
        _DRAIN_THREAD.join(timeout=1.0)

    with _TRANSITION_LOCK:
        _TRANSITION_SEQ = 0
        _INITIALIZED = False

    with _ENQUEUE_LOCK:
        _ENGINE_STATE = "uninitialized"
        _SHUTDOWN_EVENT.clear()
        _DRAIN_THREAD = None
        if _DEDICATED_CONN:
            try:
                _DEDICATED_CONN.close()
            except Exception:
                pass
            _DEDICATED_CONN = None
        _CURRENT_EVENT_BEING_PROCESSED = None
        _DRAIN_THREAD_HEALTHY = True
        _ACCEPTED_EVENTS_COUNT = 0
        _COMMITTED_EVENTS_COUNT = 0
        _DROPPED_STALE_COUNT = 0
        _DROPPED_ALERTS_COUNT = 0
        _PERSISTENCE_FAILURES_COUNT = 0
        _HAS_PERSISTENCE_FAILURE = False
        while not _ALERT_QUEUE.empty():
            try:
                _ALERT_QUEUE.get_nowait()
                _ALERT_QUEUE.task_done()
            except Exception:
                pass


def get_transition_marker() -> int:
    with _TRANSITION_LOCK:
        if not _INITIALIZED:
            raise RuntimeError("Alert engine is not initialized. Cannot allocate markers.")
        global _TRANSITION_SEQ
        _TRANSITION_SEQ += 1
        return _TRANSITION_SEQ


# ---------------------------------------------------------------------------
# Database Transitions
# ---------------------------------------------------------------------------
def apply_transition_in_db(cur: sqlite3.Cursor, event: AlertEvent, now_iso: Optional[str] = None) -> str:
    fp = event.fingerprint
    now = now_iso or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # 1. Authoritative recovery high-water mark
    cur.execute("SELECT recovery_marker FROM alert_recovery_markers WHERE fingerprint = ?", (fp,))
    hwm_row = cur.fetchone()
    recovery_hwm = hwm_row[0] if hwm_row else 0

    # 2. Active unresolved incident (partial index lookup)
    cur.execute(
        "SELECT id, status, last_marker, occurrence_count, last_event_id FROM alerts WHERE fingerprint = ? AND resolved_at IS NULL",
        (fp,)
    )
    active_row = cur.fetchone()

    # Case A: RECOVERY
    if event.type == "recovery":
        # Stale relative to recovery HWM
        if event.marker <= recovery_hwm:
            return "stale_dropped"

        # Stale relative to active incident's latest failure
        if active_row and event.marker <= active_row[2]:
            return "stale_dropped"

        # Advance recovery HWM atomically
        cur.execute(
            """
            INSERT INTO alert_recovery_markers (fingerprint, recovery_marker, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                recovery_marker = MAX(recovery_marker, excluded.recovery_marker),
                updated_at = excluded.updated_at
            """,
            (fp, event.marker, now)
        )

        if active_row:
            cur.execute(
                """
                UPDATE alerts
                SET status = 'resolved',
                    resolved_at = ?,
                    resolved_marker = ?,
                    resolution_reason = ?,
                    resolved_by = ?
                WHERE id = ?
                """,
                (now, event.marker, (event.reason or "Condition cleared")[:255], event.actor[:64], active_row[0])
            )
            return "resolved"
        else:
            return "recovery_hwm_advanced"

    # Case B: ACKNOWLEDGEMENT
    elif event.type == "acknowledgement":
        if not active_row:
            return "stale_dropped"
        if event.marker <= recovery_hwm:
            return "stale_dropped"
        if active_row[1] == "acknowledged":
            return "redundant_noop"
        if event.marker <= active_row[2]:
            return "stale_dropped"

        # Acknowledgement NEVER advances recovery HWM
        cur.execute(
            """
            UPDATE alerts
            SET status = 'acknowledged',
                acknowledged_at = ?,
                acknowledged_by = ?
            WHERE id = ?
            """,
            (now, event.actor[:64], active_row[0])
        )
        return "acknowledged"

    # Case C: FAILURE
    elif event.type == "failure":
        if active_row:
            last_m = active_row[2]
            last_eid = active_row[4] or ""
            # Persisted idempotency: exact duplicate replay (identical event_id or identical marker)
            if (event.event_id and last_eid == event.event_id) or (event.marker == last_m):
                return "redundant_noop"
            # Marker-based stale rejection: older event superseded by newer processed marker
            elif event.marker < last_m:
                return "stale_dropped"
            else:
                cur.execute(
                    """
                    UPDATE alerts
                    SET occurrence_count = occurrence_count + 1,
                        last_seen_at = ?,
                        last_marker = ?,
                        last_event_id = ?,
                        message = ?,
                        metadata = ?
                    WHERE id = ?
                    """,
                    (now, event.marker, event.event_id[:128], event.message[:500], event.metadata_json, active_row[0])
                )
                return "updated"
        else:
            if event.marker <= recovery_hwm:
                return "stale_dropped"

            new_id = f"alert_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"
            cur.execute(
                """
                INSERT INTO alerts (
                    id, fingerprint, component, reason, source_id, status, severity,
                    message, metadata, occurrence_count, created_at, last_seen_at,
                    created_marker, last_marker, last_event_id
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (new_id, fp, event.component[:32], event.reason[:64], event.source_id[:128],
                 event.severity, event.message[:500], event.metadata_json,
                 now, now, event.marker, event.marker, event.event_id[:128])
            )
            return "inserted"

    return "stale_dropped"


# ---------------------------------------------------------------------------
# Enqueue & Producer APIs
# ---------------------------------------------------------------------------
def close_alert_intake() -> None:
    """Closes alert ingestion gate so no further alert events can be accepted."""
    global _ENGINE_STATE
    with _ENQUEUE_LOCK:
        if _ENGINE_STATE == "running":
            _ENGINE_STATE = "stopping"


def wait_for_drain(timeout: float = 2.0) -> bool:
    """Waits for all enqueued alert events to be processed by the drain worker."""
    deadline = time.monotonic() + timeout
    while not _ALERT_QUEUE.empty() or _CURRENT_EVENT_BEING_PROCESSED is not None:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)
    return True


def enqueue_alert_event(event: AlertEvent) -> bool:
    global _ACCEPTED_EVENTS_COUNT, _DROPPED_ALERTS_COUNT
    with _ENQUEUE_LOCK:
        if _ENGINE_STATE != "running":
            _DROPPED_ALERTS_COUNT += 1
            return False
        try:
            _ALERT_QUEUE.put_nowait(event)
            _ACCEPTED_EVENTS_COUNT += 1
            return True
        except queue.Full:
            _DROPPED_ALERTS_COUNT += 1
            return False


def fire_alert(
    fingerprint: str,
    component: str,
    reason: str,
    message: str = "",
    severity: str = "warning",
    metadata: Optional[dict[str, Any]] = None,
    source_id: str = "",
) -> Optional[int]:
    """Public producer API for reporting conditions/failures."""
    try:
        marker = get_transition_marker()
    except RuntimeError:
        return None

    ev = AlertEvent(
        event_type="failure",
        fingerprint=fingerprint,
        component=component,
        reason=reason,
        marker=marker,
        message=message,
        metadata=metadata,
        severity=severity,
        source_id=source_id,
    )
    if enqueue_alert_event(ev):
        return marker
    return None


def resolve_condition(fingerprint: str, reason: str = "", actor: str = "system") -> Optional[int]:
    """Public producer API for reporting condition clearance."""
    try:
        marker = get_transition_marker()
    except RuntimeError:
        return None

    ev = AlertEvent(
        event_type="recovery",
        fingerprint=fingerprint,
        component="",
        reason=reason,
        marker=marker,
        actor=actor,
    )
    if enqueue_alert_event(ev):
        return marker
    return None


def get_active_operator_transitions() -> int:
    """Returns count of active operator transitions under enqueue lock."""
    with _ENQUEUE_LOCK:
        return _ACTIVE_OPERATOR_TRANSITIONS


def execute_operator_transition(conn: sqlite3.Connection, action: str, target_id: str, actor: str, reason: str = "") -> dict[str, Any]:
    """Operator mutation endpoint (resolve, acknowledge) executed atomically in SQLite."""
    global _ACTIVE_OPERATOR_TRANSITIONS
    with _ENQUEUE_LOCK:
        if _ENGINE_STATE != "running":
            raise RuntimeError("Server is shutting down; alert mutations rejected.")
        _ACTIVE_OPERATOR_TRANSITIONS += 1
        marker = get_transition_marker()

    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with conn:
            cur = conn.cursor()
            cur.execute("SELECT id, fingerprint, status, last_marker, resolved_at FROM alerts WHERE id = ?", (target_id,))
            row = cur.fetchone()
            if not row:
                raise AlertNotFoundError(f"Alert '{target_id}' not found")

            fp = row[1]
            if action == "resolve":
                if row[4] is not None:
                    return {"id": target_id, "status": "resolved", "idempotent": True}

                # Advance recovery HWM atomically in same SQLite write transaction
                cur.execute(
                    """
                    INSERT INTO alert_recovery_markers (fingerprint, recovery_marker, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        recovery_marker = MAX(recovery_marker, excluded.recovery_marker),
                        updated_at = excluded.updated_at
                    """,
                    (fp, marker, now)
                )

                cur.execute(
                    """
                    UPDATE alerts
                    SET status = 'resolved',
                        resolved_at = ?,
                        resolved_marker = ?,
                        resolution_reason = ?,
                        resolved_by = ?
                    WHERE id = ?
                    """,
                    (now, marker, (reason or "Manually resolved")[:255], actor[:64], target_id)
                )
                return {"id": target_id, "status": "resolved", "resolved_marker": marker}

            elif action == "acknowledge":
                if row[4] is not None:
                    raise ValueError("Cannot acknowledge a resolved alert")
                if row[2] == "acknowledged":
                    return {"id": target_id, "status": "acknowledged", "idempotent": True}

                cur.execute(
                    """
                    UPDATE alerts
                    SET status = 'acknowledged',
                        acknowledged_at = ?,
                        acknowledged_by = ?
                    WHERE id = ?
                    """,
                    (now, actor[:64], target_id)
                )
                return {"id": target_id, "status": "acknowledged"}
            else:
                raise ValueError(f"Unknown action '{action}'")
    finally:
        with _ENQUEUE_LOCK:
            _ACTIVE_OPERATOR_TRANSITIONS -= 1


def list_active_alerts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Query active unresolved alerts using the partial index."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, fingerprint, component, reason, source_id, status, severity,
               message, metadata, occurrence_count, created_at, last_seen_at,
               created_marker, last_marker, acknowledged_at, acknowledged_by
        FROM alerts
        WHERE resolved_at IS NULL
        ORDER BY last_marker DESC
        """
    )
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Drain Worker with Guaranteed Accounting & Fail-Stop
# ---------------------------------------------------------------------------
def drain_worker_loop(
    conn: sqlite3.Connection,
    fault_inject_before_tx: Optional[callable] = None,
    fault_inject_after_tx: Optional[callable] = None,
    simulate_busy_retries: int = 0,
):
    global _CURRENT_EVENT_BEING_PROCESSED, _COMMITTED_EVENTS_COUNT, _DROPPED_STALE_COUNT
    global _PERSISTENCE_FAILURES_COUNT, _HAS_PERSISTENCE_FAILURE, _DRAIN_THREAD_HEALTHY, _ENGINE_STATE

    _DRAIN_THREAD_HEALTHY = True
    while True:
        try:
            event = _ALERT_QUEUE.get(timeout=0.05)
        except queue.Empty:
            if _SHUTDOWN_EVENT.is_set():
                break
            continue

        _CURRENT_EVENT_BEING_PROCESSED = event
        outcome = None
        committed = False
        event_accounted = False
        should_fail_stop = False

        try:
            if fault_inject_before_tx and fault_inject_before_tx(event):
                raise RuntimeError("Fault injected before transaction")

            max_retries = 3
            busy_count = 0
            for attempt in range(max_retries):
                if busy_count < simulate_busy_retries:
                    busy_count += 1
                    time.sleep(0.01)
                    continue

                try:
                    with conn:
                        outcome = apply_transition_in_db(conn.cursor(), event)
                    committed = True
                    break
                except sqlite3.OperationalError as op_err:
                    err_msg = str(op_err).lower()
                    if ("locked" in err_msg or "busy" in err_msg) and attempt < (max_retries - 1):
                        time.sleep(0.01)
                        continue
                    break
                except Exception:
                    break

            if fault_inject_after_tx and fault_inject_after_tx(event):
                # Transaction committed, but post-commit exception occurred!
                raise RuntimeError("Fault injected immediately after commit")

            # Classification
            if committed and outcome in COMMITTED_OUTCOMES:
                _COMMITTED_EVENTS_COUNT += 1
                event_accounted = True
            elif committed and outcome in STALE_OR_REDUNDANT_OUTCOMES:
                _DROPPED_STALE_COUNT += 1
                event_accounted = True
            else:
                _PERSISTENCE_FAILURES_COUNT += 1
                _HAS_PERSISTENCE_FAILURE = True
                event_accounted = True
                should_fail_stop = True

        except Exception:
            if committed:
                # Crucial invariant: Transaction DID commit to DB! Do NOT count as failed persistence!
                if not event_accounted:
                    if outcome in COMMITTED_OUTCOMES:
                        _COMMITTED_EVENTS_COUNT += 1
                    else:
                        _DROPPED_STALE_COUNT += 1
                    event_accounted = True
            else:
                if not event_accounted:
                    _PERSISTENCE_FAILURES_COUNT += 1
                    _HAS_PERSISTENCE_FAILURE = True
                    event_accounted = True
            should_fail_stop = True

        finally:
            _CURRENT_EVENT_BEING_PROCESSED = None
            _ALERT_QUEUE.task_done()

        if should_fail_stop:
            _DRAIN_THREAD_HEALTHY = False
            with _ENQUEUE_LOCK:
                _ENGINE_STATE = "degraded_persistence_failure"
            break


def stop_alert_engine(timeout: float = 2.0, producers_quiescent: bool = True) -> tuple[bool, dict[str, Any]]:
    global _ENGINE_STATE, _DEDICATED_CONN, _INITIALIZED, _DRAIN_THREAD
    deadline = time.monotonic() + timeout

    with _ENQUEUE_LOCK:
        if _ENGINE_STATE == "running":
            _ENGINE_STATE = "stopping"

    _SHUTDOWN_EVENT.set()
    if _DRAIN_THREAD and _DRAIN_THREAD.is_alive():
        rem = max(0.0, deadline - time.monotonic())
        _DRAIN_THREAD.join(timeout=rem)

    drain_thread_stopped = (_DRAIN_THREAD is None) or (not _DRAIN_THREAD.is_alive())
    queue_empty = _ALERT_QUEUE.empty()
    no_in_flight = (_CURRENT_EVENT_BEING_PROCESSED is None)
    no_operator_in_flight = (_ACTIVE_OPERATOR_TRANSITIONS == 0)
    zero_persistence_failures = (_PERSISTENCE_FAILURES_COUNT == 0) and (not _HAS_PERSISTENCE_FAILURE)

    accounting_matches = (
        _ACCEPTED_EVENTS_COUNT == (
            _COMMITTED_EVENTS_COUNT +
            _DROPPED_STALE_COUNT +
            _PERSISTENCE_FAILURES_COUNT +
            _ALERT_QUEUE.qsize()
        )
    )

    accounting_complete = accounting_matches and producers_quiescent and no_operator_in_flight

    timed_out = (time.monotonic() >= deadline)

    clean = (
        drain_thread_stopped and
        queue_empty and
        no_in_flight and
        no_operator_in_flight and
        _DRAIN_THREAD_HEALTHY and
        zero_persistence_failures and
        accounting_matches and
        producers_quiescent and
        not timed_out
    )

    if clean:
        with _TRANSITION_LOCK:
            _INITIALIZED = False
        with _ENQUEUE_LOCK:
            _ENGINE_STATE = "stopped"
            _DRAIN_THREAD = None
    else:
        with _ENQUEUE_LOCK:
            _ENGINE_STATE = "degraded"

    if drain_thread_stopped and _DEDICATED_CONN:
        try:
            _DEDICATED_CONN.close()
        except Exception:
            pass
        _DEDICATED_CONN = None

    return clean, {
        "accepted": _ACCEPTED_EVENTS_COUNT,
        "committed": _COMMITTED_EVENTS_COUNT,
        "dropped_stale": _DROPPED_STALE_COUNT,
        "dropped_alerts": _DROPPED_ALERTS_COUNT,
        "persistence_failures": _PERSISTENCE_FAILURES_COUNT,
        "queue_remaining": _ALERT_QUEUE.qsize(),
        "drain_thread_healthy": _DRAIN_THREAD_HEALTHY,
        "drain_thread_stopped": drain_thread_stopped,
        "connection_closed": (_DEDICATED_CONN is None),
        "accounting_matches": accounting_matches,
        "accounting_complete": accounting_complete,
        "producers_quiescent": producers_quiescent,
        "no_operator_in_flight": no_operator_in_flight,
        "active_operator_transitions": _ACTIVE_OPERATOR_TRANSITIONS,
        "engine_state": _ENGINE_STATE,
        "timed_out": timed_out,
    }
