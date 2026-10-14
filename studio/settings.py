"""
studio/settings.py — Configuration Integrity, Cross-Process Locking & Atomic Persistence

Provides thread-safe and cross-process safe access to studio-settings.json.
Enforces the Stage 7 Phase 4 lock hierarchy:
    GUARD (Level 1) -> studio-settings.lock (Level 2) -> studio-quota.lock (Level 3) -> SQLite (Level 4)
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import json
import math
import os
from pathlib import Path
import random
import sys
import threading
import time
from typing import Any
import uuid

ROOT = Path(__file__).resolve().parent.parent

# Intra-process thread coordination for settings
_SETTINGS_LOCK = threading.RLock()

# Default automation and operational settings (aligned with actual codebase and Milestone 1 scope)
DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "interval_hours": 5,
    "mode": "preview",
    "next_run": 0,
    "auto_discovery_publish_opt_in": False,
}


class SettingsError(Exception):
    """Base exception for settings failures."""
    pass


class SettingsLockError(SettingsError):
    """Raised when the multi-process settings lock cannot be acquired within timeout."""
    pass


class ConfigurationCorruptError(SettingsError):
    """Raised when settings JSON is invalid, corrupt, unreadable, or violates schema."""
    pass


def _is_number(v: Any) -> bool:
    """Return True if v is int or float, NOT bool, and strictly finite."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_int(v: Any) -> bool:
    """Return True if v is int, but NOT bool."""
    return isinstance(v, int) and not isinstance(v, bool)


_LOCAL = threading.local()


@contextlib.contextmanager
def _settings_lock_guard(lock_path: Path | None = None, timeout: float = 5.0):
    """
    Thread-safe and process-safe lock guard for studio-settings.json.
    Combines threading.RLock (for intra-process thread coordination) with
    OS-level file locking on studio-settings.lock (msvcrt on Windows, fcntl on Unix).
    Fails closed by raising SettingsLockError if the OS file lock cannot be acquired within timeout.
    Supports thread-local reentrancy for nested operations within the same thread.
    """
    lock_file = Path(lock_path) if lock_path is not None else (ROOT / "studio-settings.lock")
    try:
        lock_file = lock_file.resolve()
    except Exception:
        pass
    lock_key = str(lock_file)

    held = getattr(_LOCAL, "held_locks", None)
    if held is None:
        held = _LOCAL.held_locks = {}

    if lock_key in held:
        held[lock_key] += 1
        try:
            yield
        finally:
            held[lock_key] -= 1
            if held[lock_key] <= 0:
                del held[lock_key]
        return

    start_time = time.time()
    acquired_thread_lock = _SETTINGS_LOCK.acquire(timeout=timeout)
    if not acquired_thread_lock:
        raise SettingsLockError(f"Could not acquire intra-process settings thread lock within {timeout}s")

    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    acquired_os_lock = False
    try:
        try:
            fd = os.open(lock_file, os.O_RDWR | os.O_CREAT)
        except Exception as open_err:
            raise SettingsLockError(f"Failed to open settings lock file '{lock_file}': {open_err}") from open_err

        elapsed = time.time() - start_time
        remaining_timeout = max(0.001, timeout - elapsed)

        if sys.platform == "win32":
            import msvcrt
            start_os = time.time()
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    acquired_os_lock = True
                    break
                except (OSError, IOError) as lock_err:
                    if time.time() - start_os >= remaining_timeout:
                        raise SettingsLockError(
                            f"Could not acquire multi-process settings file lock within {timeout}s: {lock_err}"
                        ) from lock_err
                    time.sleep(0.01)
        else:
            import fcntl
            start_os = time.time()
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired_os_lock = True
                    break
                except (OSError, IOError) as lock_err:
                    if time.time() - start_os >= remaining_timeout:
                        raise SettingsLockError(
                            f"Could not acquire multi-process settings file lock within {timeout}s: {lock_err}"
                        ) from lock_err
                    time.sleep(0.01)

        held[lock_key] = 1
        yield
    finally:
        held.pop(lock_key, None)
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
        _SETTINGS_LOCK.release()


def validate_settings_schema(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """
    Validate settings schema strictly.
    Rejects malformed types, booleans masquerading as numbers, and out-of-range values.
    Preserves all unknown configuration fields verbatim.
    """
    if not isinstance(data, dict):
        raise ConfigurationCorruptError(f"Settings must be a JSON object, got {type(data).__name__}")

    out = dict(data)

    if "enabled" in out:
        val = out["enabled"]
        if not isinstance(val, bool):
            raise ConfigurationCorruptError(f"'enabled' must be a boolean, got {type(val).__name__}")

    if "interval_hours" in out:
        val = out["interval_hours"]
        if not _is_number(val):
            raise ConfigurationCorruptError(f"'interval_hours' must be a number, got {type(val).__name__}")
        if not (1 <= val <= 168):
            raise ConfigurationCorruptError(f"'interval_hours' must be between 1 and 168, got {val}")

    if "mode" in out:
        val = out["mode"]
        if not isinstance(val, str):
            raise ConfigurationCorruptError(f"'mode' must be a string, got {type(val).__name__}")
        if val == "publish_approved":
            out["mode"] = "publish"
        elif val not in ("preview", "publish"):
            raise ConfigurationCorruptError(f"'mode' must be 'preview' or 'publish', got {val!r}")

    if "auto_discovery_publish_opt_in" in out:
        val = out["auto_discovery_publish_opt_in"]
        if not isinstance(val, bool):
            raise ConfigurationCorruptError(f"'auto_discovery_publish_opt_in' must be a boolean, got {type(val).__name__}")

    if "next_run" in out:
        val = out["next_run"]
        if not _is_number(val):
            raise ConfigurationCorruptError(f"'next_run' must be a number, got {type(val).__name__}")
        if val < 0:
            raise ConfigurationCorruptError(f"'next_run' cannot be negative, got {val}")

    if "consecutive_failures" in out:
        val = out["consecutive_failures"]
        if not _is_int(val):
            raise ConfigurationCorruptError(f"'consecutive_failures' must be an integer, got {type(val).__name__}")
        if val < 0:
            raise ConfigurationCorruptError(f"'consecutive_failures' cannot be negative, got {val}")

    if "quota_suspended" in out:
        val = out["quota_suspended"]
        if not isinstance(val, bool):
            raise ConfigurationCorruptError(f"'quota_suspended' must be a boolean, got {type(val).__name__}")

    for str_field in ("quota_suspended_reason", "quota_suspended_until_pt", "quota_suspended_at"):
        if str_field in out:
            val = out[str_field]
            if not isinstance(val, str):
                raise ConfigurationCorruptError(f"'{str_field}' must be a string, got {type(val).__name__}")

    return out


def load_settings(
    settings_path: Path | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """
    Read and validate settings from disk under cross-process lock.
    Returns default settings if the file does not exist.
    Fails closed with ConfigurationCorruptError if settings file is corrupt.
    Preserves all existing unknown fields.
    """
    path = Path(settings_path) if settings_path is not None else (ROOT / "studio-settings.json")
    lock_file = path.parent / "studio-settings.lock"

    with _settings_lock_guard(lock_path=lock_file, timeout=timeout):
        if not path.exists():
            return copy.deepcopy(DEFAULT_SETTINGS)

        try:
            raw_text = path.read_text(encoding="utf-8")
            data = json.loads(raw_text)
        except Exception as e:
            raise ConfigurationCorruptError(f"Settings file '{path}' is corrupt or unreadable: {e}") from e

        if not isinstance(data, dict):
            raise ConfigurationCorruptError(f"Settings file '{path}' must be a JSON object, got {type(data).__name__}")

        validated = validate_settings_schema(data)
        return {**DEFAULT_SETTINGS, **validated}


def update_settings(
    updates: dict[str, Any],
    settings_path: Path | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """
    Perform an atomic read-modify-write on studio-settings.json under cross-process lock.
    1. Reads and validates existing document (fails closed if corrupt, never overwrites).
    2. Applies validated updates while preserving all unknown fields.
    3. Serializes to a unique temporary file and flushes with fsync.
    4. Atomically replaces destination with os.replace.
    5. Cleans up temporary files on write error.
    """
    if not isinstance(updates, dict):
        raise ConfigurationCorruptError(f"Updates must be a dictionary, got {type(updates).__name__}")

    path = Path(settings_path) if settings_path is not None else (ROOT / "studio-settings.json")
    lock_file = path.parent / "studio-settings.lock"

    with _settings_lock_guard(lock_path=lock_file, timeout=timeout):
        # 1. Read existing configuration under lock
        if path.exists():
            try:
                raw_text = path.read_text(encoding="utf-8")
                existing = json.loads(raw_text)
            except Exception as e:
                raise ConfigurationCorruptError(f"Cannot read existing settings from '{path}': {e}") from e
            if not isinstance(existing, dict):
                raise ConfigurationCorruptError(f"Settings file '{path}' is not a JSON object, got {type(existing).__name__}")
            validated_existing = validate_settings_schema(existing)
        else:
            validated_existing = copy.deepcopy(DEFAULT_SETTINGS)

        # 2. Validate proposed updates
        validated_updates = validate_settings_schema(updates, partial=True)

        # 3. Merge: keep existing unknown fields and apply new values
        merged = dict(validated_existing)
        merged.update(validated_updates)

        # 4. Validate complete resulting document
        final_settings = validate_settings_schema(merged)

        # 5. Atomic write via temporary file in the same directory
        path.parent.mkdir(parents=True, exist_ok=True)
        unique_token = f"{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:8]}"
        tmp_path = path.with_name(f"{path.name}.{unique_token}.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(final_settings, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception as write_err:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            raise SettingsError(f"Failed to atomically persist settings to '{path}': {write_err}") from write_err

        # 6. Return fully populated settings
        return {**DEFAULT_SETTINGS, **final_settings}


def calculate_jitter(interval_seconds: float, rng: random.Random | None = None) -> float:
    """
    Calculate bounded positive uniform jitter in [0, min(900.0, interval_seconds * 0.10)].
    Guaranteed non-negative so scheduled execution never occurs earlier than configured interval.
    """
    max_jitter = min(900.0, max(0.0, float(interval_seconds) * 0.10))
    if max_jitter <= 0.0:
        return 0.0
    r = rng if rng is not None else random
    return round(r.uniform(0.0, max_jitter), 2)


def calculate_rollover_jitter(rng: random.Random | None = None) -> float:
    """
    Calculate bounded uniform rollover jitter in [60.0, 300.0] seconds past Pacific midnight.
    Prevents thundering herd immediately at 00:00:00 PT.
    """
    r = rng if rng is not None else random
    return round(r.uniform(60.0, 300.0), 2)


def calculate_backoff_delay(consecutive_failures: int, rng: random.Random | None = None) -> float:
    """
    Calculate exponential failure backoff delay:
    delay = min(1800.0, 60.0 * 2^(failures - 1)) + uniform(0.0, 30.0).
    Progression: 60s, 120s, 240s, 480s, 960s, capped at 1800s (30m).
    """
    f = max(1, int(consecutive_failures))
    base = min(1800.0, 60.0 * (2.0 ** (f - 1)))
    r = rng if rng is not None else random
    jitter = round(r.uniform(0.0, 30.0), 2)
    return round(base + jitter, 2)


def calculate_effective_due_time(
    next_run: float,
    quota_suspended: bool = False,
    quota_suspended_until: float | str | None = None,
) -> float:
    """
    Derive effective due time: max(normal_next_run, quota_suspended_until_epoch).
    Preserves normal recurrence interval while respecting quota suspension boundary.
    """
    if not quota_suspended or not quota_suspended_until:
        return float(next_run)

    suspension_epoch = 0.0
    if isinstance(quota_suspended_until, (int, float)):
        suspension_epoch = float(quota_suspended_until)
    elif isinstance(quota_suspended_until, str):
        try:
            suspension_epoch = dt.datetime.fromisoformat(quota_suspended_until).timestamp()
        except Exception:
            pass

    return max(float(next_run), suspension_epoch)
