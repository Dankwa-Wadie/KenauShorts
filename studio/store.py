"""
studio/store.py — SQLite database and cross-platform process coordination for KenauShorts.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

LOG = logging.getLogger("kenaushorts.store")
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "studio.sqlite3"
_QUOTA_LOCK = threading.RLock()

class QuotaError(RuntimeError):
    """Base exception for YouTube quota governance errors."""
    pass

class QuotaLockError(QuotaError):
    """Raised when multi-process quota file lock acquisition fails or times out."""
    pass

class QuotaPersistenceError(QuotaError):
    """Raised when quota tracker data cannot be safely persisted to disk."""
    pass

class QuotaCorruptError(QuotaError):
    """Raised when existing quota tracker data is corrupt, unreadable, or invalid."""
    pass

class QuotaBlockedError(QuotaError):
    """Raised when YouTube API quota is exceeded or unverified before upload."""
    pass

def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

@contextlib.contextmanager
def connect():
    db = sqlite3.connect(DB, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS videos (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    db.execute("PRAGMA journal_mode=WAL;")
    db.execute("PRAGMA synchronous=NORMAL;")
    db.execute("PRAGMA busy_timeout=15000;")
    try:
        with db:
            yield db
    finally:
        db.close()

def put(table: str, key: str, data: dict[str, Any]) -> None:
    if table not in ("videos", "jobs"):
        raise ValueError(f"Unsupported table: {table}")
    with connect() as db:
        db.execute(
            f"INSERT INTO {table} VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (key, json.dumps(data)),
        )

def get(table: str, key: str) -> dict[str, Any] | None:
    if table not in ("videos", "jobs"):
        raise ValueError(f"Unsupported table: {table}")
    if not DB.exists():
        return None
    with contextlib.closing(sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(f"SELECT data FROM {table} WHERE id=?", (key,)).fetchone()
    return json.loads(row["data"]) if row else None

def records(table: str) -> list[dict[str, Any]]:
    if table not in ("videos", "jobs"):
        raise ValueError(f"Unsupported table: {table}")
    if not DB.exists():
        return []
    with contextlib.closing(sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(f"SELECT data FROM {table} ORDER BY rowid DESC").fetchall()
    return [json.loads(r["data"]) for r in rows]

def delete(table: str, key: str) -> None:
    if table not in ("videos", "jobs"):
        raise ValueError(f"Unsupported table: {table}")
    if not DB.exists():
        return
    with connect() as db:
        db.execute(f"DELETE FROM {table} WHERE id=?", (key,))

def jobs_list(limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
    assert limit > 0
    if not DB.exists():
        return []
    with contextlib.closing(sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT data FROM jobs ORDER BY rowid DESC").fetchall()
    out = []
    for r in rows:
        try:
            job = json.loads(r["data"])
            if status and status != "all" and job.get("status") != status:
                continue
            out.append(job)
            if len(out) >= limit:
                break
        except Exception:
            continue
    return out

# Cross-platform single instance locking
@contextlib.contextmanager
def pipeline_lock():
    lock_file = ROOT / ".pipeline.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        import msvcrt
        handle = open(lock_file, "a")
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except (IOError, OSError):
            handle.close()
            raise RuntimeError("Another pipeline job is currently running on this PC.")
        try:
            yield
        finally:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            handle.close()
    else:
        import fcntl
        with open(lock_file, "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, IOError):
                raise RuntimeError("Another pipeline job is currently running on this PC.")
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

def busy() -> bool:
    try:
        with pipeline_lock():
            return False
    except RuntimeError:
        return True

def load_secrets() -> None:
    path = ROOT / "studio-secrets.json"
    if path.exists():
        try:
            for key, value in json.loads(path.read_text(encoding="utf-8")).items():
                if value:
                    os.environ[key] = value
                else:
                    os.environ.pop(key, None)
        except Exception:
            pass

def draft(
    video_stem: str,
    video_path: Path,
    poster_path: Path,
    headline: str,
    title: str,
    description: str,
    config: dict[str, Any],
    candidate_data: dict[str, Any] | None = None,
    style_preset: str = "",
    review_status: str = "unreviewed",
    privacy: str = "",
) -> dict[str, Any]:
    posting_cfg = config.get("posting", {}) if isinstance(config, dict) else {}
    eff_privacy = privacy or posting_cfg.get("privacy", "public")
    record = {
        "id": video_stem,
        "created_at": now(),
        "status": "ready",
        "review_status": review_status,
        "video": str(video_path),
        "poster": str(poster_path),
        "headline": headline,
        "title": title,
        "description": description,
        "config": config,
        "candidate": candidate_data or {},
        "style_preset": style_preset,
        "privacy": eff_privacy,
        "youtube_id": "",
        "youtube_url": "",
        "resumable_uri": "",
        "upload_failure_reason": "",
        "error": "",
    }
    put("videos", record["id"], record)
    return record

def enrich_video_record(record: dict[str, Any]) -> dict[str, Any]:
    """
    Enrich a video record dictionary with default review_status, privacy,
    canonical youtube_url, and artifact existence flags (without mutating SQLite).
    Masks internal resumable_uri from client exposure while signaling session presence.
    """
    rec = dict(record)
    rec.setdefault("review_status", "unreviewed")
    rec.setdefault("status", "ready")
    rec.setdefault("privacy", "public")

    yt_id = rec.get("youtube_id", "").strip()
    if yt_id and not rec.get("youtube_url"):
        rec["youtube_url"] = f"https://youtube.com/shorts/{yt_id}"

    # Protect sensitive resumable session URI from browser exposure
    raw_uri = rec.get("resumable_uri")
    rec["has_resumable_session"] = bool(raw_uri)
    rec.pop("resumable_uri", None)

    video_path = rec.get("video")
    poster_path = rec.get("poster")
    raw_path = rec.get("raw_video")

    v_file = (ROOT / video_path).resolve() if video_path else None
    p_file = (ROOT / poster_path).resolve() if poster_path else None

    # Check raw_video or fall back to work/{id}_raw.mp4
    raw_file = (ROOT / raw_path).resolve() if raw_path else None
    if not (raw_file and raw_file.is_file()):
        key = rec.get("id", "")
        cand_raw = (ROOT / "work" / f"{key}_raw.mp4").resolve()
        if cand_raw.is_file():
            raw_file = cand_raw
        elif "_edit_" in key:
            orig_key = key.split("_edit_")[0]
            cand_orig = (ROOT / "work" / f"{orig_key}_raw.mp4").resolve()
            if cand_orig.is_file():
                raw_file = cand_orig

    video_exists = bool(v_file and v_file.is_file())
    poster_exists = bool(p_file and p_file.is_file())
    raw_exists = bool(raw_file and raw_file.is_file())

    rec["video_exists"] = video_exists
    rec["poster_exists"] = poster_exists
    rec["raw_exists"] = raw_exists
    rec["file_size_mb"] = round(v_file.stat().st_size / (1024 * 1024), 2) if video_exists else 0.0
    return rec

def import_legacy() -> None:
    """
    Backfill Studio video records from out/*.mp4 files that predate the
    sqlite store, or that were rendered by a CLI run before core.agent
    started registering drafts directly — otherwise a file sitting right
    there in out/ never shows up in the Library.

    state.json's "posted" entries carry their own video_path, so this
    matches on that directly rather than parsing it back out of a filename.
    """
    state_path = ROOT / "state.json"
    out_dir = ROOT / "out"
    if not state_path.exists() or not out_dir.exists():
        return
    try:
        state_data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return

    posted_by_path: dict[str, dict[str, Any]] = {}
    for entry in state_data.get("posted", []):
        video_path = entry.get("video_path")
        if video_path:
            posted_by_path[str(Path(video_path))] = entry

    for video in sorted(out_dir.glob("*.mp4")):
        if "_edit_" in video.stem:
            continue  # a re-render's own draft is created by studio.worker directly
        if get("videos", video.stem):
            continue  # already indexed

        old = posted_by_path.get(str(video), {})
        uploaded = bool(old.get("youtube_id")) and not old.get("dry_run")
        poster = video.with_suffix(".png")
        put("videos", video.stem, {
            "id": video.stem,
            "created_at": dt.datetime.fromtimestamp(video.stat().st_mtime, dt.timezone.utc).isoformat(),
            "status": "uploaded" if uploaded else "ready",
            "review_status": "unreviewed",
            "video": str(video),
            "poster": str(poster) if poster.exists() else "",
            "headline": old.get("headline", video.stem),
            "title": old.get("title", old.get("headline", "")),
            "description": "",
            "config": {},
            "candidate": {"key": old.get("key", "")},
            "youtube_id": old.get("youtube_id", "") if uploaded else "",
            "error": "",
            "legacy": True,
        })


def get_pacific_now(now_utc: dt.datetime | None = None) -> dt.datetime:
    """
    Return current datetime in US Pacific Time (America/Los_Angeles).
    Attempts standard zoneinfo first, falling back to an exact US daylight
    saving time calculation if tzdata is not installed on Windows.
    """
    if now_utc is None:
        now_utc = dt.datetime.now(dt.timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=dt.timezone.utc)

    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("America/Los_Angeles")
        return now_utc.astimezone(tz)
    except Exception:
        # Exact US Daylight Saving Time calculation fallback (2nd Sun March to 1st Sun Nov)
        year = now_utc.year
        mar1_w = dt.date(year, 3, 1).weekday()
        dst_start_day = 1 + ((6 - mar1_w) % 7) + 7
        dst_start = dt.datetime(year, 3, dst_start_day, 10, 0, tzinfo=dt.timezone.utc)

        nov1_w = dt.date(year, 11, 1).weekday()
        dst_end_day = 1 + ((6 - nov1_w) % 7)
        dst_end = dt.datetime(year, 11, dst_end_day, 9, 0, tzinfo=dt.timezone.utc)

        is_dst = dst_start <= now_utc < dst_end
        offset_hours = -7 if is_dst else -8
        pt_tz = dt.timezone(dt.timedelta(hours=offset_hours), name="PDT" if is_dst else "PST")
        return now_utc.astimezone(pt_tz)


def get_pacific_date(now_utc: dt.datetime | None = None) -> str:
    """Return today's date string (YYYY-MM-DD) in US Pacific Time (midnight reset boundary for YouTube quota)."""
    return get_pacific_now(now_utc).strftime("%Y-%m-%d")


def get_pacific_reset_info(now_utc: dt.datetime | None = None) -> dict[str, Any]:
    """Return next midnight Pacific Time reset timestamp, ISO string, and seconds remaining."""
    pt_now = get_pacific_now(now_utc)
    target_date = pt_now.date() + dt.timedelta(days=1)
    # Estimate UTC around 8 AM on the target date to evaluate target date's Pacific offset
    est_utc = dt.datetime(target_date.year, target_date.month, target_date.day, 8, 0, tzinfo=dt.timezone.utc)
    target_pt = get_pacific_now(est_utc)
    next_midnight_pt = dt.datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=target_pt.tzinfo)
    reset_ts = next_midnight_pt.timestamp()
    current_utc = now_utc if now_utc is not None else dt.datetime.now(dt.timezone.utc)
    if current_utc.tzinfo is None:
        current_utc = current_utc.replace(tzinfo=dt.timezone.utc)
    now_ts = current_utc.timestamp()
    return {
        "reset_at": round(reset_ts, 1),
        "reset_at_iso": next_midnight_pt.isoformat(),
        "seconds_remaining": max(0.0, round(reset_ts - now_ts, 1)),
        "timezone": "America/Los_Angeles",
        "is_dst": target_pt.tzname() == "PDT" or target_pt.utcoffset() == dt.timedelta(hours=-7),
    }


def _save_quota_data(quota_path: Path, data: dict[str, Any]) -> None:
    """Safely persist quota data using atomic write. Fails closed on any write or replacement error."""
    try:
        tmp_path = quota_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp_path.replace(quota_path)
    except Exception as e:
        LOG.error("Failed to atomically persist %s: %s", quota_path, e)
        raise QuotaPersistenceError(f"Failed to persist quota data to '{quota_path}': {e}") from e


def _load_and_sanitize_quota_data(quota_path: Path, today_pt: str) -> dict[str, Any]:
    """
    Load and validate existing quota tracker data.
    - If the quota file does NOT exist, initializes a default tracker for today.
    - If the quota file exists but is corrupt, unreadable, or contains invalid types,
      raises QuotaCorruptError to fail closed.
    - If the quota file belongs to a previous Pacific date, resets counts for the new day
      while preserving configured limits.
    """
    if not quota_path.exists():
        data = {
            "date_pt": today_pt,
            "videos_insert_count": 0,
            "search_list_count": 0,
            "general_units": 0,
            "videos_insert_limit": 100,
            "search_list_limit": 100,
            "general_units_limit": 10000,
            "google_quota_exhausted": False,
            "google_quota_exhausted_reason": "",
            "google_quota_exhausted_at": "",
            "disclaimer": "Local estimate only. Google Developer Console is the authoritative source of truth.",
        }
        _save_quota_data(quota_path, data)
        return data

    try:
        raw_text = quota_path.read_text(encoding="utf-8")
    except Exception as read_err:
        raise QuotaCorruptError(
            f"YouTube quota file '{quota_path}' is unreadable: {read_err}. "
            "Quota governance failed closed. Review Google Developer Console or follow recovery procedure in NOTES.md."
        ) from read_err

    try:
        data = json.loads(raw_text)
    except Exception as parse_err:
        raise QuotaCorruptError(
            f"YouTube quota file '{quota_path}' contains malformed JSON: {parse_err}. "
            "Quota governance failed closed. Review Google Developer Console or follow recovery procedure in NOTES.md."
        ) from parse_err

    if not isinstance(data, dict):
        raise QuotaCorruptError(
            f"YouTube quota file '{quota_path}' has invalid structure (expected JSON object, got {type(data).__name__}). "
            "Quota governance failed closed. Review Google Developer Console or follow recovery procedure in NOTES.md."
        )

    # Validate date_pt field
    date_pt = data.get("date_pt")
    if not isinstance(date_pt, str) or not date_pt.strip():
        raise QuotaCorruptError(
            f"YouTube quota file '{quota_path}' is missing valid 'date_pt'. "
            "Quota governance failed closed. Review Google Developer Console or follow recovery procedure in NOTES.md."
        )

    # Validate integer count fields (must not be booleans, dicts, lists, or non-integer strings)
    for k in ("videos_insert_count", "search_list_count", "general_units"):
        if k not in data:
            data[k] = 0
            continue
        val = data[k]
        if isinstance(val, bool) or not isinstance(val, (int, str)):
            raise QuotaCorruptError(
                f"YouTube quota file '{quota_path}' has invalid data type for '{k}' ({type(val).__name__}). "
                "Quota governance failed closed."
            )
        try:
            int_val = int(val)
            if int_val < 0:
                raise ValueError("negative value")
            data[k] = int_val
        except (ValueError, TypeError) as type_err:
            raise QuotaCorruptError(
                f"YouTube quota file '{quota_path}' has invalid integer value for '{k}' ({val!r}). "
                "Quota governance failed closed."
            ) from type_err

    # Validate integer limit fields
    for k in ("videos_insert_limit", "search_list_limit", "general_units_limit"):
        if k not in data:
            data[k] = 100 if "general" not in k else 10000
            continue
        val = data[k]
        if isinstance(val, bool) or not isinstance(val, (int, str)):
            raise QuotaCorruptError(
                f"YouTube quota file '{quota_path}' has invalid data type for '{k}' ({type(val).__name__}). "
                "Quota governance failed closed."
            )
        try:
            int_val = int(val)
            if int_val < 1:
                raise ValueError("limit must be >= 1")
            data[k] = int_val
        except (ValueError, TypeError) as type_err:
            raise QuotaCorruptError(
                f"YouTube quota file '{quota_path}' has invalid limit value for '{k}' ({val!r}). "
                "Quota governance failed closed."
            ) from type_err


    # Validate boolean flag
    ex = data.get("google_quota_exhausted", False)
    if not isinstance(ex, bool):
        raise QuotaCorruptError(
            f"YouTube quota file '{quota_path}' has invalid type for 'google_quota_exhausted' ({type(ex).__name__}). "
            "Quota governance failed closed."
        )

    # Legitimate date rollover: if the file belongs to a prior Pacific date, roll over safely
    if date_pt != today_pt:
        data["date_pt"] = today_pt
        data["videos_insert_count"] = 0
        data["search_list_count"] = 0
        data["general_units"] = 0
        data["google_quota_exhausted"] = False
        data["google_quota_exhausted_reason"] = ""
        data["google_quota_exhausted_at"] = ""
        _save_quota_data(quota_path, data)

    data.setdefault("google_quota_exhausted_reason", "")
    data.setdefault("google_quota_exhausted_at", "")
    data.setdefault("disclaimer", "Local estimate only. Google Developer Console is the authoritative source of truth.")
    return data


@contextlib.contextmanager
def _quota_lock_guard():
    """
    Thread-safe and process-safe lock guard for studio-quota.json.
    Combines threading.RLock (for intra-process thread coordination) with
    OS-level file locking on studio-quota.lock (msvcrt on Windows, fcntl on Unix).
    Fails closed by raising QuotaLockError if the OS file lock cannot be acquired.
    """
    with _QUOTA_LOCK:
        lock_file = ROOT / "studio-quota.lock"
        fd = None
        acquired_os_lock = False
        try:
            try:
                fd = os.open(lock_file, os.O_RDWR | os.O_CREAT)
            except Exception as open_err:
                raise QuotaLockError(f"Failed to open quota lock file '{lock_file}': {open_err}") from open_err

            if sys.platform == "win32":
                import msvcrt
                start = time.time()
                while True:
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        acquired_os_lock = True
                        break
                    except (OSError, IOError) as lock_err:
                        if time.time() - start > 5.0:
                            raise QuotaLockError(f"Could not acquire multi-process quota file lock within 5s: {lock_err}") from lock_err
                        time.sleep(0.01)
            else:
                import fcntl
                start = time.time()
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired_os_lock = True
                        break
                    except (OSError, IOError) as lock_err:
                        if time.time() - start > 5.0:
                            raise QuotaLockError(f"Could not acquire multi-process quota file lock within 5s: {lock_err}") from lock_err
                        time.sleep(0.01)
            yield
        finally:
            if fd is not None:
                if acquired_os_lock:
                    if sys.platform == "win32":
                        try:
                            os.lseek(fd, 0, os.SEEK_SET)
                            import msvcrt
                            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                        except Exception:
                            pass
                    else:
                        try:
                            import fcntl
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        except Exception:
                            pass
                try:
                    os.close(fd)
                except Exception:
                    pass



def get_local_quota_tracker(now_utc: dt.datetime | None = None) -> dict[str, Any]:
    """Retrieve local YouTube API quota usage estimate for the current Pacific Time day."""
    today_pt = get_pacific_date(now_utc)
    reset_info = get_pacific_reset_info(now_utc)
    quota_path = ROOT / "studio-quota.json"
    with _quota_lock_guard():
        data = _load_and_sanitize_quota_data(quota_path, today_pt)

    tracker = dict(data)
    tracker.update(reset_info)
    tracker["google_quota_exhausted"] = bool(tracker.get("google_quota_exhausted", False))
    tracker["search_limit_reached"] = bool(
        tracker.get("search_list_count", 0) >= tracker.get("search_list_limit", 100)
        or tracker["google_quota_exhausted"]
    )
    return tracker


def mark_google_quota_exhausted(reason: str = "quotaExceeded", now_utc: dt.datetime | None = None) -> dict[str, Any]:
    """Trip the persistent date-scoped Google quota circuit breaker for today's Pacific Time day."""
    with _quota_lock_guard():
        today_pt = get_pacific_date(now_utc)
        quota_path = ROOT / "studio-quota.json"
        data = _load_and_sanitize_quota_data(quota_path, today_pt)

        data["google_quota_exhausted"] = True
        data["google_quota_exhausted_reason"] = str(reason)
        data["google_quota_exhausted_at"] = (now_utc or dt.datetime.now(dt.timezone.utc)).isoformat()

        _save_quota_data(quota_path, data)

        out = dict(data)
        out.update(get_pacific_reset_info(now_utc))
        out["search_limit_reached"] = True
        return out


def check_and_reserve_quota(action_type: str, units: int = 1, now_utc: dt.datetime | None = None) -> tuple[bool, dict[str, Any]]:
    """
    Atomically check whether quota is available and reserve units under lock.
    Prevents race conditions where concurrent discovery runs or workers exceed configured limits.
    Returns (True, tracker) if admitted and reserved; (False, tracker) if limit reached or exhausted.
    """
    with _quota_lock_guard():
        today_pt = get_pacific_date(now_utc)
        quota_path = ROOT / "studio-quota.json"
        data = _load_and_sanitize_quota_data(quota_path, today_pt)

        # If persistent circuit breaker is tripped, reject admission
        if data.get("google_quota_exhausted", False):
            out = dict(data)
            out.update(get_pacific_reset_info(now_utc))
            out["search_limit_reached"] = True
            return False, out

        if action_type == "search_list":
            limit = data.get("search_list_limit", 100)
            current = data.get("search_list_count", 0)
            if current + units > limit:
                out = dict(data)
                out.update(get_pacific_reset_info(now_utc))
                out["search_limit_reached"] = True
                return False, out
            data["search_list_count"] = current + units
        elif action_type == "videos_insert":
            limit = data.get("videos_insert_limit", 100)
            current = data.get("videos_insert_count", 0)
            if current + units > limit:
                out = dict(data)
                out.update(get_pacific_reset_info(now_utc))
                out["search_limit_reached"] = bool(data.get("search_list_count", 0) >= data.get("search_list_limit", 100))
                return False, out
            data["videos_insert_count"] = current + units
        elif action_type == "general":
            limit = data.get("general_units_limit", 10000)
            current = data.get("general_units", 0)
            if current + units > limit:
                out = dict(data)
                out.update(get_pacific_reset_info(now_utc))
                out["search_limit_reached"] = bool(data.get("search_list_count", 0) >= data.get("search_list_limit", 100))
                return False, out
            data["general_units"] = current + units

        _save_quota_data(quota_path, data)

        out = dict(data)
        out.update(get_pacific_reset_info(now_utc))
        out["google_quota_exhausted"] = bool(out.get("google_quota_exhausted", False))
        out["search_limit_reached"] = bool(
            out.get("search_list_count", 0) >= out.get("search_list_limit", 100)
            or out["google_quota_exhausted"]
        )
        return True, out


def record_local_quota_activity(action_type: str, units: int = 1, now_utc: dt.datetime | None = None) -> dict[str, Any]:
    """Record YouTube API activity in the local daily tracker."""
    with _quota_lock_guard():
        today_pt = get_pacific_date(now_utc)
        quota_path = ROOT / "studio-quota.json"
        data = _load_and_sanitize_quota_data(quota_path, today_pt)

        if action_type == "videos_insert":
            data["videos_insert_count"] = max(0, data.get("videos_insert_count", 0) + units)
        elif action_type == "search_list":
            data["search_list_count"] = max(0, data.get("search_list_count", 0) + units)
        elif action_type == "general":
            data["general_units"] = max(0, data.get("general_units", 0) + units)

        _save_quota_data(quota_path, data)

        out = dict(data)
        out.update(get_pacific_reset_info(now_utc))
        out["google_quota_exhausted"] = bool(out.get("google_quota_exhausted", False))
        out["search_limit_reached"] = bool(
            out.get("search_list_count", 0) >= out.get("search_list_limit", 100)
            or out["google_quota_exhausted"]
        )
        return out
