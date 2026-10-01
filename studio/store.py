"""
studio/store.py — SQLite database and cross-platform process coordination for KenauShorts.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "studio.sqlite3"

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


def get_pacific_date() -> str:
    """Return today's date string (YYYY-MM-DD) in US Pacific Time (midnight reset boundary for YouTube quota)."""
    now_utc = dt.datetime.now(dt.timezone.utc)
    year = now_utc.year
    mar1_weekday = dt.date(year, 3, 1).weekday()
    dst_start_day = 1 + ((6 - mar1_weekday) % 7) + 7
    dst_start = dt.datetime(year, 3, dst_start_day, 10, 0, tzinfo=dt.timezone.utc)
    nov1_weekday = dt.date(year, 11, 1).weekday()
    dst_end_day = 1 + ((6 - nov1_weekday) % 7)
    dst_end = dt.datetime(year, 11, dst_end_day, 9, 0, tzinfo=dt.timezone.utc)
    offset_hours = -7 if (dst_start <= now_utc < dst_end) else -8
    pt_time = now_utc + dt.timedelta(hours=offset_hours)
    return pt_time.strftime("%Y-%m-%d")


def get_local_quota_tracker() -> dict[str, Any]:
    """Retrieve local YouTube API quota usage estimate for the current Pacific Time day."""
    today_pt = get_pacific_date()
    quota_path = ROOT / "studio-quota.json"
    data: dict[str, Any] = {}
    if quota_path.exists():
        try:
            data = json.loads(quota_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if data.get("date_pt") != today_pt:
        data = {
            "date_pt": today_pt,
            "videos_insert_count": 0,
            "search_list_count": 0,
            "general_units": 0,
            "videos_insert_limit": 100,
            "search_list_limit": 100,
            "general_units_limit": 10000,
            "disclaimer": "Local estimate only. Google Developer Console is the authoritative source of truth.",
        }
        try:
            quota_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass
    return data


def record_local_quota_activity(action_type: str, units: int = 1) -> dict[str, Any]:
    """Record YouTube API activity in the local daily tracker."""
    tracker = get_local_quota_tracker()
    if action_type == "videos_insert":
        tracker["videos_insert_count"] = tracker.get("videos_insert_count", 0) + units
    elif action_type == "search_list":
        tracker["search_list_count"] = tracker.get("search_list_count", 0) + units
    elif action_type == "general":
        tracker["general_units"] = tracker.get("general_units", 0) + units
    try:
        (ROOT / "studio-quota.json").write_text(json.dumps(tracker, indent=2), encoding="utf-8")
    except Exception:
        pass
    return tracker
