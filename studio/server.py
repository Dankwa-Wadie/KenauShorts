"""
studio/server.py — Local HTTP REST server & automation daemon for KenauShorts.

Pure Python standard library implementation (no Flask/FastAPI required).
Serves the local Studio Web UI and manages background automation on PC.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import mimetypes
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

if __name__ == "__main__" and __package__ is None:
    # A background-service manager (launchd/systemd/Task Scheduler) invokes
    # this file by its raw path rather than `python -m studio.server`, which
    # leaves the repo root off sys.path — the `from studio import store`
    # below would otherwise fail with "No module named 'studio'" every time
    # the service tries to start.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.style_presets import STYLE_PRESETS
from studio import store

LOG = logging.getLogger("kenaushorts.server")
ROOT = store.ROOT
WEB_DIR = ROOT / "studio" / "web"
LOGS_DIR = ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get("KENAU_STUDIO_PORT", "8766"))
CSRF = secrets.token_urlsafe(32)

GUARD = threading.RLock()
ACTIVE_JOB: dict | None = None
ACTIVE_PROC: subprocess.Popen | None = None
QUEUE_EVENT = threading.Event()
STOP_EVENT = threading.Event()
QUEUE_WORKER_THREAD: threading.Thread | None = None

DEFAULT_AUTOMATION = {
    "enabled": False,
    "interval_hours": 5,
    "mode": "preview",  # "preview" or "publish"
    "next_run": 0,
}

def read_json(path: Path, default=None):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return copy.deepcopy(default)

def write_json(path: Path, data: dict, private: bool = False) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    mode = 0o600 if private else 0o644
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def get_automation_settings() -> dict:
    return {**DEFAULT_AUTOMATION, **read_json(ROOT / "studio-settings.json", {})}

def is_online() -> bool:
    for host in ("www.google.com", "1.1.1.1"):
        try:
            with socket.create_connection((host, 443), timeout=3):
                return True
        except OSError:
            pass
    return False

def get_config() -> dict:
    cfg = ROOT / "config.json"
    if not cfg.exists():
        cfg = ROOT / "config.example.json"
    return read_json(cfg, {})

def save_config(data: dict) -> None:
    write_json(ROOT / "config.json", data)

def redact(text: str) -> str:
    env_keys = store.ROOT / "studio-secrets.json"
    if env_keys.exists():
        try:
            for k, v in json.loads(env_keys.read_text()).items():
                if v and len(v) > 4:
                    text = text.replace(v, "[hidden]")
        except Exception:
            pass
    text = re.sub(r"(https?://\S+)[?]\S+", r"\1?[hidden]", text)
    text = re.sub(r"(?i)(key|token|secret|authorization)([=: ]+)[^\s,]+", r"\1\2[hidden]", text)
    return text[-8000:]

def is_process_alive(pid: int) -> bool:
    """Check if process with PID is currently running."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h_proc = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h_proc:
            PROCESS_QUERY_INFORMATION = 0x0400
            h_proc = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_INFORMATION, False, pid)
            if not h_proc:
                return False
        try:
            WAIT_TIMEOUT = 258
            return kernel32.WaitForSingleObject(h_proc, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(h_proc)
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

def is_python_process(pid: int) -> bool:
    """Validate that the process is python or ffmpeg before terminating."""
    if not is_process_alive(pid):
        return False
    if sys.platform == "win32":
        try:
            res = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            out = res.stdout.lower()
            return "python" in out or "ffmpeg" in out
        except Exception:
            return False
    return True

def get_process_creation_time(pid: int) -> int | None:
    """Return process creation time as integer (100ns intervals since 1601 on Windows), or None if unavailable."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        PROCESS_QUERY_INFORMATION = 0x0400
        h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h_proc:
            h_proc = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
            if not h_proc:
                return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            if kernel32.GetProcessTimes(
                h_proc,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        finally:
            kernel32.CloseHandle(h_proc)
        return None
    else:
        try:
            stat_path = Path(f"/proc/{pid}/stat")
            if stat_path.exists():
                fields = stat_path.read_text().split()
                if len(fields) > 21:
                    return int(fields[21])
        except Exception:
            pass
        return None

def terminate_process_tree(pid: int, expected_created_at: int | None = None) -> bool:
    """Safely terminate a process and its full child process tree."""
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        LOG.warning("Refusing to terminate PID %s", pid)
        return False

    if not is_process_alive(pid):
        return True

    if expected_created_at is not None:
        current_created_at = get_process_creation_time(pid)
        if current_created_at is not None and current_created_at != expected_created_at:
            LOG.warning(
                "PID %d creation time %s does not match expected %s (PID recycled) — refusing to terminate",
                pid, current_created_at, expected_created_at,
            )
            return False
        if current_created_at is None and not is_process_alive(pid):
            return True

    if not is_python_process(pid):
        LOG.warning("PID %s is alive but not recognized as a python/ffmpeg process", pid)
        return False

    if sys.platform == "win32":
        try:
            res = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            LOG.info("taskkill PID %d returned %d: %s", pid, res.returncode, res.stdout.strip())
        except Exception as e:
            LOG.warning("Failed to taskkill PID %d: %s", pid, e)
    else:
        import signal
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception as e:
                LOG.warning("Failed to kill PID %d: %s", pid, e)

    # Wait up to 3 seconds for the process to exit
    for _ in range(30):
        if not is_process_alive(pid):
            return True
        time.sleep(0.1)

    return not is_process_alive(pid)

def cleanup_job_artifacts(job: dict) -> None:
    """Remove any partial render files if a job was cancelled."""
    for key in ("target_file", "raw_file"):
        path_str = job.get(key)
        if path_str:
            try:
                p = Path(path_str).resolve()
                out_dir = (ROOT / "out").resolve()
                if p.is_file() and p.is_relative_to(out_dir):
                    p.unlink(missing_ok=True)
                    LOG.info("Removed cancelled artifact: %s", p)
            except Exception as e:
                LOG.warning("Failed to clean up artifact %s: %s", path_str, e)

def run_job_process(job: dict, command: list[str]) -> None:
    global ACTIVE_JOB, ACTIVE_PROC
    log_lines = []
    log_path = LOGS_DIR / f"{job['id']}.log"

    def write_full_log():
        try:
            log_path.write_text("\n".join(log_lines), encoding="utf-8")
        except OSError:
            pass  # A disk-full/permission hiccup here shouldn't crash the job.

    proc = None
    try:
        proc = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        with GUARD:
            ACTIVE_PROC = proc
            job["pid"] = proc.pid
            job["pid_created_at"] = get_process_creation_time(proc.pid)
            store.put("jobs", job["id"], job)

        for line in proc.stdout:
            line_str = line.strip()
            if line_str.startswith("KENAU_PROGRESS "):
                try:
                    p = json.loads(line_str[15:])
                    job["stage"] = p.get("stage", job["stage"])
                except Exception:
                    pass
            elif line_str.startswith("KENAU_SUMMARY "):
                try:
                    summary = json.loads(line_str[14:])
                    job["summary"] = summary
                    if "video" in summary:
                        job["target_file"] = summary["video"]
                except Exception:
                    pass
            else:
                log_lines.append(redact(line_str))
                write_full_log()
            job["log"] = "\n".join(log_lines[-200:])
            store.put("jobs", job["id"], job)

        code = proc.wait()

        with GUARD:
            current = store.get("jobs", job["id"]) or job
            if current.get("status") == "cancelled":
                job["status"] = "cancelled"
                job["stage"] = current.get("stage", "Cancelled")
                cleanup_job_artifacts(job)
            else:
                summary = job.get("summary") or {}
                summary_status = summary.get("status")
                summary_msg = summary.get("message")

                if summary_status == "failed":
                    job["status"] = "failed"
                    job["stage"] = f"Failed: {summary_msg}" if summary_msg else "Failed"
                elif summary_status == "idle":
                    job["status"] = "idle"
                    job["stage"] = f"Idle: {summary_msg}" if summary_msg else "Idle: No new candidates"
                elif summary_status in ("completed", "ok"):
                    job["status"] = "completed"
                    job["stage"] = "Completed"
                elif code == 0:
                    job["status"] = "completed"
                    job["stage"] = "Completed"
                else:
                    job["status"] = "failed"
                    job["stage"] = "Failed"

            job["finished_at"] = store.now()
            job["log_file"] = f"{job['id']}.log"
            store.put("jobs", job["id"], job)
            ACTIVE_JOB = None
            ACTIVE_PROC = None

    except Exception as e:
        with GUARD:
            current = store.get("jobs", job["id"]) or job
            if current.get("status") == "cancelled":
                job["status"] = "cancelled"
                job["stage"] = current.get("stage", "Cancelled")
                cleanup_job_artifacts(job)
            else:
                job["status"] = "failed"
                job["stage"] = "Failed"
                error_line = f"Error: {redact(str(e))}"
                log_lines.append(error_line)
                write_full_log()
                job["log"] = "\n".join(log_lines[-200:])
            job["finished_at"] = store.now()
            job["log_file"] = f"{job['id']}.log"
            store.put("jobs", job["id"], job)
            ACTIVE_JOB = None
            ACTIVE_PROC = None
    finally:
        if proc and proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass
        with GUARD:
            if ACTIVE_JOB and ACTIVE_JOB.get("id") == job.get("id"):
                ACTIVE_JOB = None
                ACTIVE_PROC = None
        QUEUE_EVENT.set()

def get_next_pending_job() -> dict | None:
    try:
        records = store.records("jobs")
    except Exception:
        return None
    pending = [j for j in records if j.get("status") == "pending"]
    if not pending:
        return None
    pending.sort(key=lambda j: j.get("created_at", ""))
    return pending[0]

def queue_worker_loop() -> None:
    global ACTIVE_JOB, ACTIVE_PROC
    LOG.info("Queue worker loop started.")
    while not STOP_EVENT.is_set():
        try:
            job_to_run = None
            command_to_run = None
            with GUARD:
                if ACTIVE_JOB is None and not store.busy():
                    candidate = get_next_pending_job()
                    if candidate:
                        fresh = store.get("jobs", candidate["id"])
                        if fresh and fresh.get("status") == "pending":
                            fresh["status"] = "running"
                            fresh["stage"] = "Starting"
                            store.put("jobs", fresh["id"], fresh)
                            ACTIVE_JOB = fresh
                            job_to_run = fresh
                            command_to_run = fresh.get("command")

            if job_to_run:
                if not command_to_run:
                    python = sys.executable
                    action = job_to_run.get("action", "")
                    if action in ("preview", "run"):
                        command_to_run = [python, "-u", "-m", "core.agent"]
                        if action == "preview":
                            command_to_run.append("--dry-run")
                    elif action in ("upload", "render"):
                        command_to_run = [python, "-u", "-m", "studio.worker", action, job_to_run.get("key", "")]
                    elif action == "youtube_connect":
                        command_to_run = [python, "-u", "-m", "core.youtube_auth"]
                    elif action == "manual":
                        url = job_to_run.get("extra", {}).get("url", "")
                        command_to_run = [python, "-u", "-m", "core.agent", "--dry-run", "--url", url]

                if command_to_run:
                    run_job_process(job_to_run, command_to_run)
                else:
                    with GUARD:
                        job_to_run["status"] = "failed"
                        job_to_run["stage"] = "Failed (Missing command)"
                        job_to_run["finished_at"] = store.now()
                        store.put("jobs", job_to_run["id"], job_to_run)
                        ACTIVE_JOB = None
                continue

            QUEUE_EVENT.wait(timeout=2.0)
            QUEUE_EVENT.clear()
        except Exception as e:
            LOG.error("Queue worker error: %s", e)
            time.sleep(0.5)

def ensure_queue_worker() -> None:
    global QUEUE_WORKER_THREAD
    with GUARD:
        if QUEUE_WORKER_THREAD is None or not QUEUE_WORKER_THREAD.is_alive():
            QUEUE_WORKER_THREAD = threading.Thread(target=queue_worker_loop, daemon=True)
            QUEUE_WORKER_THREAD.start()

def cancel_job(job_id: str | None = None) -> dict:
    global ACTIVE_JOB, ACTIVE_PROC
    with GUARD:
        if not job_id:
            if ACTIVE_JOB:
                job_id = ACTIVE_JOB["id"]
            else:
                return {"status": "idle", "message": "No active job to cancel"}

        # Case 1: Active running job
        if ACTIVE_JOB and ACTIVE_JOB.get("id") == job_id:
            pid = ACTIVE_JOB.get("pid")
            pid_created_at = ACTIVE_JOB.get("pid_created_at")
            ACTIVE_JOB["status"] = "cancelled"
            ACTIVE_JOB["stage"] = "Cancelled by user"
            ACTIVE_JOB["finished_at"] = store.now()
            store.put("jobs", job_id, ACTIVE_JOB)

            if pid:
                terminate_process_tree(pid, expected_created_at=pid_created_at)
            if ACTIVE_PROC and ACTIVE_PROC.poll() is None:
                try:
                    ACTIVE_PROC.kill()
                except Exception:
                    pass
            cleanup_job_artifacts(ACTIVE_JOB)
            return {"status": "cancelled", "id": job_id, "message": "Active job cancelled"}

        # Case 2: Job in store (pending or other)
        job = store.get("jobs", job_id)
        if not job:
            raise ValueError(f"Job not found: {job_id}")

        status = job.get("status")
        if status == "pending":
            job["status"] = "cancelled"
            job["stage"] = "Cancelled from queue"
            job["finished_at"] = store.now()
            store.put("jobs", job_id, job)
            return {"status": "cancelled", "id": job_id, "message": "Pending job removed from queue"}

        if status == "running":
            pid = job.get("pid")
            pid_created_at = job.get("pid_created_at")
            job["status"] = "cancelled"
            job["stage"] = "Cancelled by user"
            job["finished_at"] = store.now()
            store.put("jobs", job_id, job)
            if pid:
                terminate_process_tree(pid, expected_created_at=pid_created_at)
            cleanup_job_artifacts(job)
            return {"status": "cancelled", "id": job_id, "message": "Job cancelled"}

        return {"status": status, "id": job_id, "message": f"Job is already {status}"}

def start_job(action: str, key: str = "", automatic: bool = False, extra: dict | None = None) -> dict:
    if shutil.disk_usage(ROOT).free < 512 * 1024 * 1024:
        raise ValueError("Free at least 512 MB of disk space before starting video generation.")

    python = sys.executable
    if action in ("preview", "run"):
        command = [python, "-u", "-m", "core.agent"]
        if action == "preview":
            command.append("--dry-run")
    elif action in ("upload", "render"):
        command = [python, "-u", "-m", "studio.worker", action, key]
    elif action == "youtube_connect":
        command = [python, "-u", "-m", "core.youtube_auth"]  # port 0 = any free port
    elif action == "manual":
        url = extra.get("url", "") if extra else ""
        if not url:
            raise ValueError("A video URL is required.")
        command = [python, "-u", "-m", "core.agent", "--dry-run", "--url", url]
        headline = (extra or {}).get("headline", "")
        description = (extra or {}).get("description", "")
        if headline:
            command += ["--headline", headline]
        if description:
            command += ["--description", description]
    else:
        raise ValueError(f"Unknown job action: {action}")

    with GUARD:
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "action": action,
            "command": command,
            "key": key,
            "extra": extra or {},
            "status": "pending",
            "stage": "Queued",
            "created_at": store.now(),
            "automatic": automatic,
            "log": "",
        }
        store.put("jobs", job_id, job)
        ensure_queue_worker()
        QUEUE_EVENT.set()

        records = store.records("jobs")
        pending = [j for j in records if j.get("status") == "pending"]
        pending.sort(key=lambda j: j.get("created_at", ""))
        pos = next((i + 1 for i, j in enumerate(pending) if j.get("id") == job_id), 1)

        is_immediate = (ACTIVE_JOB is None and not store.busy() and pos == 1)
        res = dict(job)
        res["queue_position"] = 0 if is_immediate else pos
        return res

def automation_loop() -> None:
    while not STOP_EVENT.is_set():
        try:
            time.sleep(15)
            with GUARD:
                settings = get_automation_settings()
                if not settings.get("enabled", False):
                    continue

                now_ts = time.time()
                next_ts = float(settings.get("next_run", 0))
                has_pending = bool(get_next_pending_job())

                if now_ts >= next_ts and not ACTIVE_JOB and not store.busy() and not has_pending and is_online():
                    # Set next slot before starting
                    interval = float(settings.get("interval_hours", 5))
                    settings["next_run"] = now_ts + interval * 3600
                    write_json(ROOT / "studio-settings.json", settings)

                    mode = settings.get("mode", "preview")
                    start_job("preview" if mode == "preview" else "run", automatic=True)
        except Exception as e:
            LOG.error("Automation error: %s", e)

HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)

def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)

def validate_settings_config(patch: dict) -> dict:
    """
    Validate a config.json write coming from the Studio UI.

    The Style/Automation pages fetch the current config, mutate a couple of
    fields, and POST the whole object back rather than a diff — so instead of
    trusting that payload wholesale (which would let any page that gets past
    the CSRF/Origin check overwrite config.json with arbitrary content), this
    validates every field it recognises and merges only those onto the
    existing on-disk config. Unrecognised keys are dropped rather than
    rejected, since the frontend round-trips sections it doesn't itself edit.
    """
    if not isinstance(patch, dict):
        raise ValueError("Settings must be an object")

    cfg: dict[str, Any] = {}

    account = patch.get("account", {})
    if not isinstance(account, dict):
        raise ValueError("Invalid account settings")
    out = {}
    if "name" in account:
        if not isinstance(account["name"], str) or not account["name"].strip() or len(account["name"]) > 100:
            raise ValueError("Invalid channel name")
        out["name"] = account["name"]
    if "handle" in account:
        if not isinstance(account["handle"], str) or len(account["handle"]) > 100:
            raise ValueError("Invalid channel handle")
        out["handle"] = account["handle"]
    if "avatar" in account:
        if not isinstance(account["avatar"], str) or len(account["avatar"]) > 500:
            raise ValueError("Invalid avatar path")
        out["avatar"] = account["avatar"]
    if "verified" in account:
        if not isinstance(account["verified"], bool):
            raise ValueError("Invalid verified flag")
        out["verified"] = account["verified"]
    if out:
        cfg["account"] = out

    layout = patch.get("layout", {})
    if not isinstance(layout, dict):
        raise ValueError("Invalid layout settings")
    out = {}
    if "border_color" in layout:
        if not isinstance(layout["border_color"], str) or not HEX_COLOR_RE.match(layout["border_color"]):
            raise ValueError("Border color must be a hex value like #1D9BF0")
        out["border_color"] = layout["border_color"]
    if "corner_radius" in layout:
        v = layout["corner_radius"]
        if not _is_int(v) or not 0 <= v <= 200:
            raise ValueError("Corner radius must be 0-200")
        out["corner_radius"] = v
    if "headline_size" in layout:
        v = layout["headline_size"]
        if not _is_int(v) or not 20 <= v <= 160:
            raise ValueError("Headline size must be 20-160")
        out["headline_size"] = v
    if out:
        cfg["layout"] = out

    posting = patch.get("posting", {})
    if not isinstance(posting, dict):
        raise ValueError("Invalid posting settings")
    out = {}
    if "max_clip_seconds" in posting:
        v = posting["max_clip_seconds"]
        if not _is_int(v) or not 3 <= v <= 60:
            raise ValueError("Max clip seconds must be 3-60")
        out["max_clip_seconds"] = v
    if "privacy" in posting:
        if posting["privacy"] not in ("private", "unlisted", "public"):
            raise ValueError("Invalid privacy setting")
        out["privacy"] = posting["privacy"]
    if "tags" in posting:
        tags = posting["tags"]
        if not isinstance(tags, list) or len(tags) > 40 or any(not isinstance(t, str) or len(t) > 60 for t in tags):
            raise ValueError("Use up to 40 short tags")
        out["tags"] = tags
    if out:
        cfg["posting"] = out

    editorial = patch.get("editorial", {})
    if not isinstance(editorial, dict):
        raise ValueError("Invalid editorial settings")
    out = {}
    if "system_prompt" in editorial:
        v = editorial["system_prompt"]
        if not isinstance(v, str) or len(v) > 12000:
            raise ValueError("System prompt is too long")
        out["system_prompt"] = v
    if "provider_order" in editorial:
        order = editorial["provider_order"]
        if (not isinstance(order, list) or not order
                or any(p not in ("gemini", "anthropic", "openai") for p in order)
                or len(set(order)) != len(order)):
            raise ValueError("Choose each editorial provider at most once")
        out["provider_order"] = order
    if out:
        cfg["editorial"] = out

    discovery = patch.get("discovery", {})
    if not isinstance(discovery, dict):
        raise ValueError("Invalid discovery settings")
    out = {}
    if "subreddits" in discovery:
        subs = discovery["subreddits"]
        if not isinstance(subs, list) or len(subs) > 60:
            raise ValueError("Use up to 60 subreddits")
        for s in subs:
            if isinstance(s, str):
                if not s.strip() or len(s) > 60:
                    raise ValueError("Invalid subreddit name")
            elif isinstance(s, dict):
                if not isinstance(s.get("name"), str) or not s["name"].strip() or len(s["name"]) > 60:
                    raise ValueError("Invalid subreddit name")
                if "min_score" in s and (not _is_int(s["min_score"]) or not 0 <= s["min_score"] <= 100000):
                    raise ValueError("Invalid subreddit min_score")
            else:
                raise ValueError("Invalid subreddit entry")
        out["subreddits"] = subs
    if "news_rss" in discovery:
        feeds = discovery["news_rss"]
        if not isinstance(feeds, list) or len(feeds) > 40:
            raise ValueError("Use up to 40 RSS feeds")
        for url in feeds:
            if not isinstance(url, str) or len(url) > 500 or urlsplit(url).scheme != "https":
                raise ValueError("RSS sources must be HTTPS URLs")
        out["news_rss"] = feeds
    if out:
        cfg["discovery"] = out

    story_layout = patch.get("story_layout", {})
    if not isinstance(story_layout, dict):
        raise ValueError("Invalid story layout settings")
    out = {}
    for key in ("card_opacity", "background_dim", "background_zoom",
                "background_anchor_x", "background_anchor_y", "vertical_bias"):
        if key in story_layout:
            v = story_layout[key]
            if not _is_number(v) or not 0 <= v <= 3:
                raise ValueError(f"Invalid value for {key}")
            out[key] = v
    if out:
        cfg["story_layout"] = out

    existing = get_config()
    for section, values in cfg.items():
        existing.setdefault(section, {}).update(values)
    return existing

CSP_HEADER = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)

class StudioHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default noisy console logs

    def allowed(self) -> bool:
        # The Studio is a loopback-only service. Checking Host (rather than
        # trusting the socket is local) defeats DNS rebinding, where a remote
        # page's own JS resolves an attacker domain to 127.0.0.1 mid-session.
        return self.headers.get("Host") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def send_json(self, data: Any, status: int = 200) -> None:
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.allowed():
            self.send_json({"error": "Local access only"}, 403)
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # API Routes
        if path == "/api/status":
            with GUARD:
                cfg = get_automation_settings()
                records = store.records("jobs")
                pending = [j for j in records if j.get("status") == "pending"]
                pending.sort(key=lambda j: j.get("created_at", ""))
                self.send_json({
                    "csrf": CSRF,
                    "online": is_online(),
                    "active_job": ACTIVE_JOB,
                    "busy": store.busy(),
                    "free_space_mb": int(shutil.disk_usage(ROOT).free / (1024 * 1024)),
                    "automation": cfg,
                    "platform": platform.system(),
                    "queue_count": len(pending),
                    "queued_jobs": [j["id"] for j in pending],
                })
            return

        if path == "/api/queue":
            with GUARD:
                records = store.records("jobs")
                pending = [j for j in records if j.get("status") == "pending"]
                pending.sort(key=lambda j: j.get("created_at", ""))
                self.send_json({
                    "active_job": ACTIVE_JOB,
                    "pending": pending,
                    "count": len(pending),
                })
            return

        if path == "/api/videos":
            records = store.records("videos")
            self.send_json(records)
            return

        if path == "/api/failures":
            state_path = ROOT / "state.json"
            data = read_json(state_path, {})
            failed = data.get("failed", {})
            # Sort by most recent failure first, and cap the reasons shown per
            # entry so one badly-behaved candidate can't bloat the response.
            out = []
            for key, info in failed.items():
                reasons = info.get("reasons", [])
                last_time = reasons[-1]["time"] if reasons else 0
                out.append({
                    "key": key,
                    "attempts": info.get("attempts", 0),
                    "last_reason": reasons[-1]["reason"] if reasons else "Unknown",
                    "last_time": last_time,
                })
            out.sort(key=lambda x: x["last_time"], reverse=True)
            self.send_json(out[:50])
            return

        if path == "/api/logs":
            files = sorted(LOGS_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            self.send_json([f.stem for f in files[:100]])
            return

        if path == "/api/logs/file":
            job_id = query.get("id", [""])[0]
            # job_id came from a job we generated ourselves, but treat it as
            # untrusted here too — same resolve()+is_relative_to() pattern
            # used for /media/ below, so a crafted id can't read outside logs/.
            try:
                log_file = (LOGS_DIR / f"{job_id}.log").resolve()
            except (OSError, ValueError):
                self.send_error(404, "Log not found")
                return
            logs_dir_resolved = LOGS_DIR.resolve()
            if log_file.is_file() and log_file.is_relative_to(logs_dir_resolved):
                self.send_json({"id": job_id, "log": log_file.read_text(encoding="utf-8", errors="replace")})
            else:
                self.send_json({"error": "Log not found"}, 404)
            return

        if path == "/api/video":
            vid_id = query.get("id", [""])[0]
            record = store.get("videos", vid_id)
            if record:
                self.send_json(record)
            else:
                self.send_json({"error": "Video not found"}, 404)
            return

        if path == "/api/job":
            job_id = query.get("id", [""])[0]
            record = store.get("jobs", job_id)
            if record:
                self.send_json(record)
            else:
                self.send_json({"error": "Job not found"}, 404)
            return

        if path == "/api/connections":
            secrets_data = read_json(ROOT / "studio-secrets.json", {})
            token_file = ROOT / "token.json"
            client_secret_file = ROOT / "client_secret.json"

            def mask(val: str) -> str:
                return f"...{val[-4:]}" if len(val) > 6 else ("Configured" if val else "")

            self.send_json({
                "gemini": bool(secrets_data.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")),
                "gemini_preview": mask(secrets_data.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")),
                "anthropic": bool(secrets_data.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")),
                "openai": bool(secrets_data.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")),
                "youtube_api": bool(secrets_data.get("YOUTUBE_API_KEY") or os.environ.get("YOUTUBE_API_KEY")),
                "reddit_id": bool(secrets_data.get("REDDIT_CLIENT_ID") or os.environ.get("REDDIT_CLIENT_ID")),
                "youtube_oauth_ready": token_file.exists(),
                "youtube_client_secret_present": client_secret_file.exists(),
            })
            return

        if path == "/api/subreddits":
            cfg = get_config()
            subs = cfg.get("discovery", {}).get("subreddits", [])
            self.send_json(subs)
            return
        if path == "/api/youtube_channels":
            cfg = get_config()
            channels = cfg.get("discovery", {}).get("youtube_channels", [])
            self.send_json(channels)
            return

        if path == "/api/youtube_queries":
            cfg = get_config()
            queries = cfg.get("discovery", {}).get("youtube_queries", [])
            self.send_json(queries)
            return

        if path == "/api/settings":
            cfg = get_config()
            auto = get_automation_settings()
            self.send_json({"config": cfg, "automation": auto})
            return

        # Media serving from out/ or assets/. The path is resolved (which
        # collapses ".." segments) before the containment check, and the
        # check uses is_relative_to on the resolved path rather than testing
        # unresolved .parents — the previous version compared literal path
        # components, so "/media/out/../../../../etc/passwd" satisfied the
        # "out" in media_path.parents test while actually reading outside
        # both directories once the OS resolved it.
        if path.startswith("/media/"):
            rel = path[len("/media/"):]
            try:
                media_path = (ROOT / rel).resolve()
            except (OSError, ValueError):
                self.send_error(404, "Media not found")
                return
            out_dir = (ROOT / "out").resolve()
            assets_dir = (ROOT / "assets").resolve()
            if media_path.is_file() and (media_path.is_relative_to(out_dir) or media_path.is_relative_to(assets_dir)):
                self._serve_file(media_path)
                return
            self.send_error(404, "Media not found")
            return

        # Static Web UI serving — same resolve-then-contain pattern as above.
        if path == "/" or path == "/index.html":
            self._serve_file(WEB_DIR / "index.html")
            return

        web_dir = WEB_DIR.resolve()
        try:
            static_file = (WEB_DIR / path.lstrip("/")).resolve()
        except (OSError, ValueError):
            static_file = None
        if static_file and static_file.is_relative_to(web_dir) and static_file.is_file():
            self._serve_file(static_file)
            return

        # Fallback to index for SPA
        self._serve_file(WEB_DIR / "index.html")

    def do_POST(self):
        parsed = urlsplit(self.path)
        path = parsed.path

        if (not self.allowed()
                or self.headers.get("Origin") not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")
                or not secrets.compare_digest(self.headers.get("X-Studio-Token", ""), CSRF)):
            # Wrong Host defeats DNS rebinding; wrong Origin or a missing/stale
            # CSRF token means the request wasn't issued by this server's own
            # page — reject before touching config, keys, or jobs.
            self.send_json({"error": "Reopen the app to reconnect securely"}, 403)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65536:
                raise ValueError("Request body too large")
            body = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(body.decode("utf-8")) if body else {}
            if not isinstance(data, dict):
                raise ValueError("Invalid request")
            with GUARD:
                result = self.mutate(path, data)
            self.send_json(result if result is not None else {"status": "ok"})
        except (ValueError, KeyError, TypeError) as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            LOG.error("POST %s failed: %s", path, e)
            self.send_json({"error": "The action could not be completed. Check the local service log."}, 500)

    def mutate(self, path: str, data: dict) -> dict | None:
        if path == "/api/job/cancel":
            return cancel_job(data.get("id"))

        if path == "/api/job":
            if data.get("action") == "cancel":
                return cancel_job(data.get("id"))
            return start_job(data.get("action", ""), data.get("key", ""), extra=data)
            
        if path == "/api/video":
            vid_id = data.get("id", "")
            record = store.get("videos", vid_id)
            if not record:
                raise ValueError("Video not found")

            if data.get("action") == "delete":
                for field in ("video", "poster"):
                    file_path = record.get(field)
                    if file_path:
                        p = (ROOT / file_path).resolve()
                        out_dir = (ROOT / "out").resolve()
                        if p.is_relative_to(out_dir) and p.is_file():
                            try:
                                p.unlink()
                            except OSError:
                                pass
                store.delete("videos", vid_id)
                return {"status": "deleted", "id": vid_id}

            if "title" in data:
                record["title"] = data["title"]
            if "description" in data:
                record["description"] = data["description"]
            if "headline" in data:
                record["headline"] = data["headline"]
            if "style_preset" in data:
                val = data["style_preset"]
                if val is None:
                    preset_val = ""
                elif isinstance(val, str):
                    preset_val = val.strip()
                else:
                    raise ValueError(f"Invalid style_preset: expected string, got {type(val).__name__}")
                valid_names = {p["name"] for p in STYLE_PRESETS}
                if preset_val and preset_val not in valid_names:
                    raise ValueError(f"Invalid style_preset: '{preset_val}'. Must be one of {sorted(valid_names)} or empty string")
                record["style_preset"] = preset_val
            store.put("videos", vid_id, record)
            return record

        if path == "/api/connections":
            secrets_path = ROOT / "studio-secrets.json"
            existing = read_json(secrets_path, {})
            allowed_keys = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                            "YOUTUBE_API_KEY", "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET")
            for k, v in data.items():
                if k in allowed_keys:
                    if not isinstance(v, str) or len(v) > 4096:
                        raise ValueError("Invalid API key")
                    cleaned = v.strip()
                    if cleaned:
                        existing[k] = cleaned
                    else:
                        # An explicit empty value means "remove this key",
                        # not "save a blank string".
                        existing.pop(k, None)
            write_json(secrets_path, existing, private=True)
            store.load_secrets()
            return {"status": "saved"}

        if path == "/api/subreddits":
            cfg = get_config()
            discovery = cfg.setdefault("discovery", {})
            subreddits = discovery.setdefault("subreddits", [])

            action = data.get("action", "add")
            if action == "add":
                sub_name = str(data.get("name", "")).strip().lower().replace("r/", "")
                if not sub_name or len(sub_name) > 60:
                    raise ValueError("Invalid subreddit name")
                cat = str(data.get("category", "General"))[:60]
                try:
                    min_score = int(data.get("min_score", 200))
                except (TypeError, ValueError):
                    raise ValueError("Invalid min_score")
                if not 0 <= min_score <= 100000:
                    raise ValueError("Invalid min_score")
                subreddits = [s for s in subreddits if (s.get("name") if isinstance(s, dict) else s) != sub_name]
                if len(subreddits) >= 60:
                    raise ValueError("Use up to 60 subreddits")
                subreddits.append({"name": sub_name, "category": cat, "min_score": min_score})
                discovery["subreddits"] = subreddits
                save_config(cfg)
                return {"status": "added", "subreddits": subreddits}
            elif action == "delete":
                sub_name = str(data.get("name", "")).strip().lower().replace("r/", "")
                discovery["subreddits"] = [s for s in subreddits if (s.get("name") if isinstance(s, dict) else s) != sub_name]
                save_config(cfg)
                return {"status": "deleted", "subreddits": discovery["subreddits"]}
            else:
                raise ValueError("Unknown subreddit action")

        if path == "/api/youtube_channels":
            cfg = get_config()
            discovery = cfg.setdefault("discovery", {})
            channels = discovery.setdefault("youtube_channels", [])

            action = data.get("action", "add")
            if action == "add":
                name = str(data.get("name", "")).strip()[:100]
                channel_id = str(data.get("channel", "")).strip()
                if not name or not channel_id or len(channel_id) > 60:
                    raise ValueError("Channel name and ID are required")
                licence = str(data.get("licence", "unknown"))[:30]
                channels = [c for c in channels if c.get("channel") != channel_id]
                if len(channels) >= 60:
                    raise ValueError("Use up to 60 channels")
                channels.append({"name": name, "channel": channel_id, "licence": licence})
                discovery["youtube_channels"] = channels
                save_config(cfg)
                return {"status": "added", "youtube_channels": channels}
            elif action == "delete":
                channel_id = str(data.get("channel", "")).strip()
                discovery["youtube_channels"] = [c for c in channels if c.get("channel") != channel_id]
                save_config(cfg)
                return {"status": "deleted", "youtube_channels": discovery["youtube_channels"]}
            else:
                raise ValueError("Unknown channel action")

        if path == "/api/youtube_queries":
            cfg = get_config()
            discovery = cfg.setdefault("discovery", {})
            queries = discovery.setdefault("youtube_queries", [])

            action = data.get("action", "add")
            if action == "add":
                q = str(data.get("query", "")).strip()
                if not q or len(q) > 200:
                    raise ValueError("Invalid search query")
                if q not in queries:
                    if len(queries) >= 40:
                        raise ValueError("Use up to 40 search queries")
                    queries.append(q)
                discovery["youtube_queries"] = queries
                save_config(cfg)
                return {"status": "added", "youtube_queries": queries}
            elif action == "delete":
                q = str(data.get("query", "")).strip()
                discovery["youtube_queries"] = [x for x in queries if x != q]
                save_config(cfg)
                return {"status": "deleted", "youtube_queries": discovery["youtube_queries"]}
            else:
                raise ValueError("Unknown query action")

        if path == "/api/subreddits/test":
            from core.scrape_reddit import fetch_subreddit_posts
            sub_name = str(data.get("name", "")).strip().lower().replace("r/", "")
            if not sub_name or len(sub_name) > 60:
                raise ValueError("Subreddit name required")
            posts = fetch_subreddit_posts(sub_name, limit=5)
            return {"subreddit": sub_name, "count": len(posts), "sample": posts[:3]}

        if path == "/api/settings":
            cfg_patch = data.get("config")
            auto = data.get("automation")
            if cfg_patch is not None:
                save_config(validate_settings_config(cfg_patch))
            if auto is not None:
                if (not isinstance(auto, dict)
                        or not isinstance(auto.get("enabled"), bool)
                        or not _is_number(auto.get("interval_hours"))
                        or not 1 <= auto["interval_hours"] <= 168
                        or auto.get("mode") not in ("preview", "publish")):
                    raise ValueError("Choose an interval of 1-168 hours and a valid mode")
                current_auto = get_automation_settings()
                current_auto.update(auto)
                write_json(ROOT / "studio-settings.json", current_auto)
            return {"status": "updated"}

        if path == "/api/wizard":
            # First-run onboarding wizard configuration
            cfg = get_config()
            account = cfg.setdefault("account", {})
            if "name" in data:
                if not isinstance(data["name"], str) or not data["name"].strip() or len(data["name"]) > 100:
                    raise ValueError("Invalid channel name")
                account["name"] = data["name"]
            if "handle" in data:
                if not isinstance(data["handle"], str) or len(data["handle"]) > 100:
                    raise ValueError("Invalid channel handle")
                account["handle"] = data["handle"]
            if "subreddits" in data:
                subs = data["subreddits"]
                if not isinstance(subs, list) or len(subs) > 60:
                    raise ValueError("Invalid subreddits list")
                cfg.setdefault("discovery", {})["subreddits"] = subs
            save_config(cfg)

            if "api_key" in data and "provider" in data:
                prov = str(data["provider"]).lower()
                if prov not in ("gemini", "anthropic", "openai"):
                    raise ValueError("Invalid provider")
                api_key = data["api_key"]
                if not isinstance(api_key, str) or len(api_key) > 4096:
                    raise ValueError("Invalid API key")
                key_name = f"{prov.upper()}_API_KEY"
                secrets_path = ROOT / "studio-secrets.json"
                sec = read_json(secrets_path, {})
                sec[key_name] = api_key.strip()
                write_json(secrets_path, sec, private=True)
                store.load_secrets()

            return {"status": "wizard_completed"}

        raise ValueError("Unknown action")

    def _serve_file(self, file_path: Path) -> None:
        if not file_path.exists() or not file_path.is_file():
            self.send_error(404, "File not found")
            return

        mime, _ = mimetypes.guess_type(str(file_path))
        mime = mime or "application/octet-stream"
        size = file_path.stat().st_size
        start, end, status = 0, size - 1, 200

        # Video scrubbing in the Library modal needs byte-range requests —
        # without Accept-Ranges the browser can only play from the start.
        byte_range = self.headers.get("Range", "")
        if byte_range:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", byte_range)
            if not match:
                self.send_error(416, "Invalid range")
                return
            start = int(match[1])
            end = min(int(match[2]), end) if match[2] else end
            if start > end:
                self.send_error(416, "Invalid range")
                return
            status = 206

        try:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP_HEADER)
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with file_path.open("rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            LOG.error("Error serving file %s: %s", file_path, e)

def recover_interrupted_jobs() -> None:
    """Check for jobs that were running when the server stopped/died, and terminate any orphan process trees."""
    for job in store.records("jobs"):
        if job.get("status") == "running":
            pid = job.get("pid")
            pid_created_at = job.get("pid_created_at")
            if pid and is_process_alive(pid):
                if is_python_process(pid):
                    LOG.info("Terminating orphan process tree for PID %d from interrupted job %s", pid, job["id"])
                    terminate_process_tree(pid, expected_created_at=pid_created_at)
            job.update(
                status="interrupted",
                stage="Interrupted by server restart",
                finished_at=store.now(),
            )
            store.put("jobs", job["id"], job)

    if not store.busy():
        for r in store.records("videos"):
            if r.get("status") in ("uploading", "rendering"):
                r.update(
                    status="upload_unknown" if r["status"] == "uploading" else "render_failed",
                    error="The previous job was interrupted. Review before retrying.",
                )
                store.put("videos", r["id"], r)

@contextlib.contextmanager
def server_lock():
    """Acquire a non-blocking lock on .server.lock to ensure only one server instance runs."""
    lock_file = ROOT / ".server.lock"
    fd = None
    if sys.platform == "win32":
        import msvcrt
        try:
            fd = os.open(lock_file, os.O_RDWR | os.O_CREAT)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except (OSError, IOError) as e:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
            raise RuntimeError(f"Another KenauShorts server instance is already running (locked {lock_file}): {e}") from e
        try:
            yield
        finally:
            if fd is not None:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
                try:
                    os.close(fd)
                except Exception:
                    pass
    else:
        import fcntl
        try:
            fd = os.open(lock_file, os.O_RDWR | os.O_CREAT)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as e:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
            raise RuntimeError(f"Another KenauShorts server instance is already running (locked {lock_file}): {e}") from e
        try:
            yield
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    os.close(fd)
                except Exception:
                    pass

class StudioServer(ThreadingHTTPServer):
    allow_reuse_address = False

def run_server(port: int = PORT) -> None:
    try:
        with server_lock():
            server_address = ("127.0.0.1", port)
            try:
                httpd = StudioServer(server_address, StudioHandler)
            except OSError as e:
                LOG.error("Failed to bind KenauShorts Studio on port %d: %s", port, e)
                return

            store.load_secrets()
            store.import_legacy()
            recover_interrupted_jobs()
            ensure_queue_worker()

            LOG.info("KenauShorts Studio running at http://127.0.0.1:%d/", port)

            # Start background automation thread
            t = threading.Thread(target=automation_loop, daemon=True)
            t.start()

            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                LOG.info("Stopping KenauShorts Studio...")
            finally:
                STOP_EVENT.set()
                httpd.shutdown()
                httpd.server_close()
    except RuntimeError as e:
        LOG.error("Cannot start KenauShorts Studio: %s", e)
        return

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_server()
