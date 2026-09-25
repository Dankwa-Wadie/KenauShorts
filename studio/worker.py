"""
studio/worker.py — Background worker for isolated rendering and upload tasks.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from pathlib import Path

from core import agent, render, render_story
from core.state import State
from core.style_presets import (
    apply_style_preset,
    choose_style_preset,
    get_source_aspect_ratio,
    get_style_preset,
    match_style_preset_to_aspect,
)
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
                agent.emit_summary({
                    "status": "completed",
                    "video_id": vid_id,
                    "message": "Uploaded to YouTube successfully",
                })

                # Without this, an upload triggered from the Studio (as
                # opposed to a full core.agent run) never reaches state.json —
                # the same story could be picked and re-uploaded again later.
                candidate_key = record.get("candidate", {}).get("key")
                if candidate_key:
                    state = State(store.ROOT / "state.json")
                    state.mark_posted({
                        "key": candidate_key,
                        "headline": record.get("headline", ""),
                        "title": record["title"],
                        "video_path": record["video"],
                        "youtube_id": vid_id,
                        "dry_run": False,
                        "at": time.time(),
                    })

            except Exception as e:
                agent.emit_summary({"status": "failed", "message": str(e)})
                record.update(status="failed", error=str(e))
                store.put("videos", key, record)
                raise

        elif action == "render":
            agent.emit_progress("Preparing render")
            record["status"] = "rendering"
            record["error"] = ""
            store.put("videos", key, record)

            out_dir = store.ROOT / "out"
            new_id = f"{key}_edit_{uuid.uuid4().hex[:6]}"
            new_mp4 = out_dir / f"{new_id}.mp4"
            new_poster = out_dir / f"{new_id}.png"

            try:
                preset_name = record.get("style_preset", "")
                preset = get_style_preset(preset_name) if preset_name else None

                cand = record.get("candidate", {})
                if cand.get("kind") == "story":
                    agent.emit_progress("Compositing story card")
                    render_config = apply_style_preset(config, preset) if preset else config
                    render_story.render_story(
                        headline=record["headline"],
                        commentary=record.get("caption", ""),
                        images=[Path(p) for p in cand.get("images", [])],
                        mascot=Path(config.get("story", {}).get("mascot", "assets/default_mascot.mp4")),
                        out=new_mp4,
                        poster=new_poster,
                        config=render_config,
                    )
                else:
                    # Look for raw clip in work dir
                    raw_candidate = store.ROOT / "work" / f"{key}_raw.mp4"
                    if raw_candidate.is_file():
                        raw_clip = raw_candidate
                    else:
                        orig_key = key.split("_edit_")[0]
                        orig_raw = store.ROOT / "work" / f"{orig_key}_raw.mp4"
                        if orig_raw.is_file():
                            raw_clip = orig_raw
                        else:
                            raw_clip = Path(record.get("raw_video", record["video"]))

                    if not preset:
                        agent.emit_progress("Analyzing source aspect ratio")
                        source_aspect = get_source_aspect_ratio(raw_clip)
                        if source_aspect is not None:
                            preset = match_style_preset_to_aspect(source_aspect)
                            LOG.info(
                                "Matched style preset '%s' (%s) for source aspect ratio %.3f (%s)",
                                preset["name"],
                                preset.get("layout", {}).get("video_aspect"),
                                source_aspect,
                                raw_clip.name,
                            )
                        else:
                            LOG.warning(
                                "Could not determine source aspect ratio for %s; falling back to random preset",
                                raw_clip.name,
                            )
                            preset = choose_style_preset()
                    else:
                        agent.emit_progress(f"Applying style preset ({preset_name})")

                    render_config = apply_style_preset(config, preset)

                    agent.emit_progress("Compositing video card")
                    render.render(
                        video=raw_clip,
                        headline=record["headline"],
                        out=new_mp4,
                        config=render_config,
                        poster=new_poster,
                    )

                agent.emit_progress("Finalizing render")
                record.update(
                    status="ready",
                    review_status="unreviewed",
                    video=str(new_mp4),
                    poster=str(new_poster),
                    raw_video=str(raw_clip) if cand.get("kind") != "story" else "",
                    style_preset=record.get("style_preset", ""),
                    error="",
                )
                store.put("videos", key, record)
                LOG.info("Re-render complete for %s -> %s", key, new_mp4)
                agent.emit_summary({
                    "status": "completed",
                    "video": str(new_mp4),
                    "message": "Render completed successfully",
                })

            except Exception as e:
                agent.emit_summary({"status": "failed", "message": str(e)})
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
