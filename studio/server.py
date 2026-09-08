"""
studio/server.py — Local HTTP REST server & automation daemon for KenauShorts.

Pure Python standard library implementation (no Flask/FastAPI required).
Serves the local Studio Web UI and manages background automation on PC.
"""

from __future__ import annotations

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
from urllib.parse import parse_qs, urlsplit

from studio import store

LOG = logging.getLogger("kenaushorts.server")
ROOT = store.ROOT
WEB_DIR = ROOT / "studio" / "web"
PORT = int(os.environ.get("KENAU_STUDIO_PORT", "8766"))
CSRF = secrets.token_urlsafe(32)

GUARD = threading.RLock()
ACTIVE_JOB: dict | None = None
STOP_EVENT = threading.Event()

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

def run_job_process(job: dict, command: list[str]) -> None:
    global ACTIVE_JOB
    log_lines = []
    try:
        proc = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

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
                    job["summary"] = json.loads(line_str[14:])
                except Exception:
                    pass
            else:
                log_lines.append(redact(line_str))
            job["log"] = "\n".join(log_lines[-40:])
            store.put("jobs", job["id"], job)

        code = proc.wait()
        job["status"] = "completed" if code == 0 else "failed"
        job["stage"] = "Completed" if code == 0 else "Failed"
        job["finished_at"] = store.now()

    except Exception as e:
        job["status"] = "failed"
        job["stage"] = "Failed"
        job["log"] += f"\nError: {redact(str(e))}"
        job["finished_at"] = store.now()
    finally:
        store.put("jobs", job["id"], job)
        with GUARD:
            ACTIVE_JOB = None

def start_job(action: str, key: str = "", automatic: bool = False) -> dict:
    global ACTIVE_JOB
    with GUARD:
        if ACTIVE_JOB or store.busy():
            raise ValueError("A job is currently running on this PC. Please wait for it to finish.")

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
            command = [python, "-u", "-m", "core.youtube_auth", "--port", "8080"]
        else:
            raise ValueError(f"Unknown job action: {action}")

        job = {
            "id": uuid.uuid4().hex,
            "action": action,
            "status": "running",
            "stage": "Starting",
            "created_at": store.now(),
            "automatic": automatic,
            "log": "",
        }
        ACTIVE_JOB = job
        store.put("jobs", job["id"], job)
        threading.Thread(target=run_job_process, args=(job, command), daemon=True).start()
        return job

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

                if now_ts >= next_ts and not ACTIVE_JOB and not store.busy() and is_online():
                    # Set next slot before starting
                    interval = float(settings.get("interval_hours", 5))
                    settings["next_run"] = now_ts + interval * 3600
                    write_json(ROOT / "studio-settings.json", settings)

                    mode = settings.get("mode", "preview")
                    start_job("preview" if mode == "preview" else "run", automatic=True)
        except Exception as e:
            LOG.error("Automation error: %s", e)

class StudioHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default noisy console logs

    def send_json(self, data: Any, status: int = 200) -> None:
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # API Routes
        if path == "/api/status":
            with GUARD:
                cfg = get_automation_settings()
                self.send_json({
                    "online": is_online(),
                    "active_job": ACTIVE_JOB,
                    "busy": store.busy(),
                    "free_space_mb": int(shutil.disk_usage(ROOT).free / (1024 * 1024)),
                    "automation": cfg,
                    "platform": platform.system(),
                })
            return

        if path == "/api/videos":
            records = store.records("videos")
            self.send_json(records)
            return

        if path == "/api/video":
            vid_id = query.get("id", [""])[0]
            record = store.get("videos", vid_id)
            if record:
                self.send_json(record)
            else:
                self.send_json({"error": "Video not found"}, 404)
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

        if path == "/api/settings":
            cfg = get_config()
            auto = get_automation_settings()
            self.send_json({"config": cfg, "automation": auto})
            return

        # Media serving from out/ or assets/
        if path.startswith("/media/"):
            rel = path[7:]
            media_path = ROOT / rel
            if media_path.exists() and media_path.is_file() and (ROOT / "out" in media_path.parents or ROOT / "assets" in media_path.parents):
                self._serve_file(media_path)
                return
            self.send_error(404, "Media not found")
            return

        # Static Web UI serving
        if path == "/" or path == "/index.html":
            self._serve_file(WEB_DIR / "index.html")
            return

        static_file = WEB_DIR / path.lstrip("/")
        if static_file.exists() and static_file.is_file():
            self._serve_file(static_file)
            return

        # Fallback to index for SPA
        self._serve_file(WEB_DIR / "index.html")

    def do_POST(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"

        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            data = {}

        if path == "/api/job":
            action = data.get("action", "")
            key = data.get("key", "")
            try:
                job = start_job(action, key)
                self.send_json(job)
            except ValueError as e:
                self.send_json({"error": str(e)}, 400)
            return

        if path == "/api/video":
            vid_id = data.get("id", "")
            record = store.get("videos", vid_id)
            if not record:
                self.send_json({"error": "Video not found"}, 404)
                return
            if "title" in data:
                record["title"] = data["title"]
            if "description" in data:
                record["description"] = data["description"]
            if "headline" in data:
                record["headline"] = data["headline"]
            store.put("videos", vid_id, record)
            self.send_json(record)
            return

        if path == "/api/connections":
            # Save API keys securely
            secrets_path = ROOT / "studio-secrets.json"
            existing = read_json(secrets_path, {})
            allowed = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "YOUTUBE_API_KEY", "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET")
            for k, v in data.items():
                if k in allowed:
                    existing[k] = v.strip()
            write_json(secrets_path, existing, private=True)
            store.load_secrets()
            self.send_json({"status": "saved"})
            return

        if path == "/api/subreddits":
            cfg = get_config()
            discovery = cfg.setdefault("discovery", {})
            subreddits = discovery.setdefault("subreddits", [])

            action = data.get("action", "add")
            if action == "add":
                sub_name = data.get("name", "").strip().lower().replace("r/", "")
                cat = data.get("category", "General")
                min_score = int(data.get("min_score", 200))
                # Remove if existing duplicate
                subreddits = [s for s in subreddits if (s.get("name") if isinstance(s, dict) else s) != sub_name]
                subreddits.append({"name": sub_name, "category": cat, "min_score": min_score})
                discovery["subreddits"] = subreddits
                save_config(cfg)
                self.send_json({"status": "added", "subreddits": subreddits})
            elif action == "delete":
                sub_name = data.get("name", "").strip().lower().replace("r/", "")
                discovery["subreddits"] = [s for s in subreddits if (s.get("name") if isinstance(s, dict) else s) != sub_name]
                save_config(cfg)
                self.send_json({"status": "deleted", "subreddits": discovery["subreddits"]})
            else:
                self.send_json({"error": "Unknown subreddit action"}, 400)
            return

        if path == "/api/subreddits/test":
            from core.scrape_reddit import fetch_subreddit_posts
            sub_name = data.get("name", "").strip().lower().replace("r/", "")
            if not sub_name:
                self.send_json({"error": "Subreddit name required"}, 400)
                return
            posts = fetch_subreddit_posts(sub_name, limit=5)
            self.send_json({"subreddit": sub_name, "count": len(posts), "sample": posts[:3]})
            return

        if path == "/api/settings":
            cfg = data.get("config")
            auto = data.get("automation")
            if cfg:
                save_config(cfg)
            if auto:
                current_auto = get_automation_settings()
                current_auto.update(auto)
                write_json(ROOT / "studio-settings.json", current_auto)
            self.send_json({"status": "updated"})
            return

        if path == "/api/wizard":
            # First-run onboarding wizard configuration
            cfg = get_config()
            account = cfg.setdefault("account", {})
            if "name" in data:
                account["name"] = data["name"]
            if "handle" in data:
                account["handle"] = data["handle"]
            if "subreddits" in data:
                cfg.setdefault("discovery", {})["subreddits"] = data["subreddits"]
            save_config(cfg)

            if "api_key" in data and "provider" in data:
                prov = data["provider"].lower()
                key_name = f"{prov.upper()}_API_KEY"
                secrets_path = ROOT / "studio-secrets.json"
                sec = read_json(secrets_path, {})
                sec[key_name] = data["api_key"].strip()
                write_json(secrets_path, sec, private=True)
                store.load_secrets()

            self.send_json({"status": "wizard_completed"})
            return

        self.send_error(404, "Endpoint not found")

    def _serve_file(self, file_path: Path) -> None:
        if not file_path.exists() or not file_path.is_file():
            self.send_error(404, "File not found")
            return

        mime, _ = mimetypes.guess_type(str(file_path))
        mime = mime or "application/octet-stream"

        try:
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, f"Error serving file: {e}")

def run_server(port: int = PORT) -> None:
    store.load_secrets()
    server_address = ("127.0.0.1", port)
    httpd = ThreadingHTTPServer(server_address, StudioHandler)
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

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_server()
