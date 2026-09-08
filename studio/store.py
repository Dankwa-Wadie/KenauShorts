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
    db.execute("CREATE TABLE IF NOT EXISTS subreddits (name TEXT PRIMARY KEY, category TEXT, min_score INTEGER, enabled INTEGER)")
    try:
        with db:
            yield db
    finally:
        db.close()

def put(table: str, key: str, data: dict[str, Any]) -> None:
    assert table in ("videos", "jobs", "subreddits")
    with connect() as db:
        db.execute(
            f"INSERT INTO {table} VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (key, json.dumps(data)),
        )

def get(table: str, key: str) -> dict[str, Any] | None:
    assert table in ("videos", "jobs")
    if not DB.exists():
        return None
    with contextlib.closing(sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(f"SELECT data FROM {table} WHERE id=?", (key,)).fetchone()
    return json.loads(row["data"]) if row else None

def records(table: str) -> list[dict[str, Any]]:
    assert table in ("videos", "jobs")
    if not DB.exists():
        return []
    with contextlib.closing(sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(f"SELECT data FROM {table} ORDER BY rowid DESC").fetchall()
    return [json.loads(r["data"]) for r in rows]

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

def draft(video_stem: str, video_path: Path, poster_path: Path, headline: str, title: str, description: str, config: dict[str, Any], candidate_data: dict[str, Any] | None = None) -> dict[str, Any]:
    record = {
        "id": video_stem,
        "created_at": now(),
        "status": "ready",
        "video": str(video_path),
        "poster": str(poster_path),
        "headline": headline,
        "title": title,
        "description": description,
        "config": config,
        "candidate": candidate_data or {},
        "youtube_id": "",
        "error": "",
    }
    put("videos", record["id"], record)
    return record
