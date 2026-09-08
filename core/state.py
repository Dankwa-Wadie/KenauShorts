"""State management and deduplication for KenauShorts."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

LOG = logging.getLogger("kenaushorts.state")

class State:
    """Manages seen items, posted videos, and pagination cursors across runs."""

    def __init__(self, path: Path):
        self.path = path
        self.seen: dict[str, float] = {}
        self.failed: dict[str, dict[str, Any]] = {}
        self.posted: list[dict[str, Any]] = []
        self.reddit_cursors: dict[str, int] = {}
        self.run_history: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.seen = data.get("seen", {})
            self.failed = data.get("failed", {})
            self.posted = data.get("posted", [])
            self.reddit_cursors = data.get("reddit_cursors", {})
            self.run_history = data.get("run_history", [])
        except Exception as e:
            LOG.warning("Could not read state file %s (%s); starting fresh", self.path, e)

    def save(self) -> None:
        data = {
            "seen": self.seen,
            "failed": self.failed,
            "posted": self.posted[-500:],  # keep last 500
            "reddit_cursors": self.reddit_cursors,
            "run_history": self.run_history[-100:],
            "updated_at": time.time(),
        }
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            LOG.error("Failed to write state: %s", e)

    def is_seen(self, key: str, max_failed_attempts: int = 3) -> bool:
        if key in self.seen:
            return True
        if key in self.failed and self.failed[key].get("attempts", 0) >= max_failed_attempts:
            return True
        return False

    def mark_seen(self, key: str) -> None:
        self.seen[key] = time.time()
        self.save()

    def mark_failed(self, key: str, reason: str) -> None:
        entry = self.failed.setdefault(key, {"attempts": 0, "reasons": []})
        entry["attempts"] += 1
        entry["reasons"].append({"reason": reason, "time": time.time()})
        self.save()

    def mark_posted(self, entry: dict[str, Any]) -> None:
        self.posted.append(entry)
        if "url" in entry:
            self.seen[entry["url"]] = time.time()
        self.save()

    def get_reddit_cursor(self, category: str) -> int:
        return self.reddit_cursors.get(category, 0)

    def set_reddit_cursor(self, category: str, cursor: int) -> None:
        self.reddit_cursors[category] = cursor
        self.save()
