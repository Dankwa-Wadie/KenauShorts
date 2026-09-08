"""
studio/worker.py — Background worker for isolated rendering and upload tasks.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from pathlib import Path

from core import agent, render, render_story
from studio import store

LOG = logging.getLogger("kenaushorts.worker")

def work(action: str, key: str) -> None:
    store.load_secrets()
    with store.pipeline_lock():
        record = store.get("videos", key)
        if not record:
            raise ValueError(f"Video {key} not found")

        cfg_path = store.ROOT / "config.json"
        if not cfg_path.exists():
            cfg_path = store.ROOT / "config.example.json"
        config = json.loads(cfg_path.read_text(encoding="utf-8"))

        if action == "upload":
            if record["status"] == "uploaded":
                raise ValueError("This video has already been uploaded.")
            if not record.get("title", "").strip():
                raise ValueError("Please provide a title before uploading.")

            record["status"] = "uploading"
            record["error"] = ""
            store.put("videos", key, record)

            token_path = store.ROOT / "token.json"
            try:
                vid_id = agent.upload_to_youtube(
                    video_path=Path(record["video"]),
                    title=record["title"],
                    description=record.get("description", ""),
                    tags=config.get("posting", {}).get("tags", []),
                    privacy=config.get("posting", {}).get("privacy", "public"),
                    token_path=token_path,
                )
                if not vid_id:
                    raise RuntimeError("No YouTube video ID returned.")

                record.update(
                    status="uploaded",
                    youtube_id=vid_id,
                    uploaded_at=store.now(),
                )
                store.put("videos", key, record)
                LOG.info("Upload complete for %s -> %s", key, vid_id)

            except Exception as e:
                record.update(status="failed", error=str(e))
                store.put("videos", key, record)
                raise

        elif action == "render":
            record["status"] = "rendering"
            record["error"] = ""
            store.put("videos", key, record)

            out_dir = store.ROOT / "out"
            new_id = f"{key}_edit_{uuid.uuid4().hex[:6]}"
            new_mp4 = out_dir / f"{new_id}.mp4"
            new_poster = out_dir / f"{new_id}.png"

            try:
                cand = record.get("candidate", {})
                if cand.get("kind") == "story":
                    render_story.render_story(
                        headline=record["headline"],
                        commentary=record.get("caption", ""),
                        images=[Path(p) for p in cand.get("images", [])],
                        mascot=Path(config.get("story", {}).get("mascot", "assets/default_mascot.mp4")),
                        out=new_mp4,
                        poster=new_poster,
                        config=config,
                    )
                else:
                    # Look for raw clip in work dir
                    raw_clip = Path(record.get("raw_video", record["video"]))
                    render.render(
                        video=raw_clip,
                        headline=record["headline"],
                        out=new_mp4,
                        config=config,
                        poster=new_poster,
                    )

                record.update(
                    status="ready",
                    video=str(new_mp4),
                    poster=str(new_poster),
                    error="",
                )
                store.put("videos", key, record)
                LOG.info("Re-render complete for %s -> %s", key, new_mp4)

            except Exception as e:
                record.update(status="render_failed", error=str(e))
                store.put("videos", key, record)
                raise
        else:
            raise ValueError(f"Unknown worker action: {action}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 -m studio.worker <action> <key>")
        sys.exit(1)
    work(sys.argv[1], sys.argv[2])
