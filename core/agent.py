"""
agent.py — Core discovery, editorial curation, rendering, and publishing engine for KenauShorts.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from core.power import keep_awake
from core.render import Account, RenderResult, render
from core.render_story import render_story
from core.scrape_reddit import fetch_subreddit_posts, get_reddit_token
from core.state import State

LOG = logging.getLogger("kenaushorts.agent")

ROOT = Path(__file__).resolve().parent.parent

@dataclass
class Candidate:
    kind: str  # "clip" | "story"
    key: str
    title: str
    url: str
    source: str
    channel: str = ""
    licence: str = "unknown"
    text: str = ""
    images: list[str] = field(default_factory=list)
    published_at: float = 0.0

@dataclass
class Pick:
    candidate: Candidate
    headline: str
    caption: str
    title: str
    description: str
    hashtags: list[str] = field(default_factory=list)
    mood: str = "neutral"

def emit_progress(stage: str, **extras) -> None:
    payload = {"stage": stage, **extras}
    print(f"KENAU_PROGRESS {json.dumps(payload)}", flush=True)

def emit_summary(summary: dict[str, Any]) -> None:
    print(f"KENAU_SUMMARY {json.dumps(summary)}", flush=True)

# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def discover_youtube_rss(channels: list[dict[str, Any]], per_channel: int = 6) -> list[Candidate]:
    candidates = []
    headers = {"User-Agent": "KenauShorts/1.0"}

    for ch in channels:
        ch_id = ch.get("channel", "")
        name = ch.get("name", ch_id)
        licence = ch.get("licence", "unknown")
        if not ch_id:
            continue

        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={ch_id}"
        try:
            resp = requests.get(feed_url, headers=headers, timeout=12)
            if resp.status_code != 200:
                continue

            root = ET.fromstring(resp.content)
            ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
            entries = root.findall("atom:entry", ns)[:per_channel]

            for entry in entries:
                vid_id_elem = entry.find("yt:videoId", ns)
                title_elem = entry.find("atom:title", ns)
                link_elem = entry.find("atom:link", ns)
                pub_elem = entry.find("atom:published", ns)

                if vid_id_elem is None or title_elem is None:
                    continue

                vid_id = vid_id_elem.text or ""
                title = title_elem.text or ""
                url = link_elem.attrib.get("href", f"https://www.youtube.com/watch?v={vid_id}") if link_elem is not None else f"https://www.youtube.com/watch?v={vid_id}"

                pub_ts = time.time()
                if pub_elem is not None and pub_elem.text:
                    try:
                        pub_ts = dt.datetime.fromisoformat(pub_elem.text.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        pass

                candidates.append(Candidate(
                    kind="clip",
                    key=f"yt_{vid_id}",
                    title=title,
                    url=url,
                    source="youtube",
                    channel=name,
                    licence=licence,
                    published_at=pub_ts,
                ))
        except Exception as e:
            LOG.warning("Failed to fetch YouTube RSS for %s: %s", name, e)

    return candidates

def discover_reddit(subreddits_config: list[dict[str, Any]], limit_per_sub: int = 10, min_score: int = 150) -> list[Candidate]:
    """Fetch stories and video clips from user-configured subreddits."""
    candidates = []
    token = get_reddit_token()

    for item in subreddits_config:
        sub_name = item.get("name", "") if isinstance(item, dict) else str(item)
        score_thresh = item.get("min_score", min_score) if isinstance(item, dict) else min_score
        if not sub_name:
            continue

        posts = fetch_subreddit_posts(sub_name, sort="hot", limit=limit_per_sub, token=token)
        for p in posts:
            if p["score"] < score_thresh or p["over_18"]:
                continue

            # Determine kind: video clip if Reddit video exists, else story
            if p["is_video"] and p["video_url"]:
                candidates.append(Candidate(
                    kind="clip",
                    key=f"reddit_{p['id']}",
                    title=p["title"],
                    url=p["video_url"],
                    source="reddit",
                    channel=f"r/{p['subreddit']}",
                    licence="standard",
                    text=p["selftext"][:400],
                    published_at=p["created_utc"],
                ))
            elif p["images"] or len(p["selftext"]) > 40:
                candidates.append(Candidate(
                    kind="story",
                    key=f"reddit_{p['id']}",
                    title=p["title"],
                    url=p["permalink"],
                    source="reddit",
                    channel=f"r/{p['subreddit']}",
                    licence="standard",
                    text=p["selftext"][:500],
                    images=p["images"][:2],
                    published_at=p["created_utc"],
                ))

        time.sleep(0.3)

    return candidates

def discover_all_candidates(config: dict[str, Any], state: State) -> list[Candidate]:
    discovery = config.get("discovery", {})
    channels = discovery.get("youtube_channels", [])
    subreddits = discovery.get("subreddits", [])

    emit_progress("Discovering content")
    yt_candidates = discover_youtube_rss(channels, per_channel=int(discovery.get("per_channel", 6)))
    reddit_candidates = discover_reddit(subreddits)

    all_c = yt_candidates + reddit_candidates
    fresh = [c for c in all_c if not state.is_seen(c.key)]
    LOG.info("Found %d fresh candidates out of %d total", len(fresh), len(all_c))
    return fresh

# --------------------------------------------------------------------------
# AI Editorial Curation
# --------------------------------------------------------------------------

def _call_gemini(prompt: str, system: str, key: str, model: str = "gemini-1.5-flash") -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    payload = {
        "contents": [{"parts": [{"text": f"{system}\n\n{prompt}"}]}],
        "generationConfig": {"temperature": 0.4, "response_mime_type": "application/json"},
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=25)
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini API HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]

def _call_claude(prompt: str, system: str, key: str, model: str = "claude-3-5-sonnet-20241022") -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=model,
        max_tokens=800,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text

def _call_openai(prompt: str, system: str, key: str, model: str = "gpt-4o-mini") -> str:
    import openai
    client = openai.OpenAI(api_key=key)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content or "{}"

def pick_best_story(candidates: list[Candidate], config: dict[str, Any]) -> Pick | None:
    if not candidates:
        return None

    editorial = config.get("editorial", {})
    providers = editorial.get("provider_order", ["gemini", "anthropic", "openai"])
    system = editorial.get("system_prompt", "You are an expert short-form video editor. Select the single best story.")

    # Prepare story listings
    listings = []
    for idx, c in enumerate(candidates[:20]):
        listings.append(f"[{idx}] Source: {c.channel} ({c.source})\nTitle: {c.title}\nDetails: {c.text}\n")

    user_prompt = (
        "Analyze the following candidates and pick the #1 best candidate for a 30-second YouTube Short.\n"
        "Output valid JSON ONLY matching this schema:\n"
        "{\n"
        '  "pick_index": 0,\n'
        '  "headline": "SHORT PUNCHY ALL-CAPS HEADLINE (UNDER 10 WORDS)",\n'
        '  "caption": "A 1-2 sentence commentary or punchy explanation",\n'
        '  "title": "Engaging YouTube Shorts Title #Shorts",\n'
        '  "description": "2-sentence YouTube description with hashtags",\n'
        '  "hashtags": ["shorts", "tech", "news"]\n'
        "}\n\n"
        + "\n".join(listings)
    )

    emit_progress("AI Editorial Selection")
    raw_json = ""
    for prov in providers:
        key = os.environ.get(f"{prov.upper()}_API_KEY", "")
        if not key:
            continue
        try:
            LOG.info("Calling editorial provider: %s", prov)
            if prov == "gemini":
                raw_json = _call_gemini(user_prompt, system, key, editorial.get("gemini_model", "gemini-1.5-flash"))
            elif prov == "anthropic":
                raw_json = _call_claude(user_prompt, system, key, editorial.get("anthropic_model", "claude-3-5-sonnet-20241022"))
            elif prov == "openai":
                raw_json = _call_openai(user_prompt, system, key, editorial.get("openai_model", "gpt-4o-mini"))
            if raw_json:
                break
        except Exception as e:
            LOG.warning("Provider %s failed: %s", prov, e)

    if not raw_json:
        # Fallback: simple heuristic selection if no LLM key is configured
        LOG.info("No AI provider responded; using top candidate fallback.")
        top_cand = candidates[0]
        return Pick(
            candidate=top_cand,
            headline=top_cand.title[:60].upper(),
            caption=top_cand.text[:120] or top_cand.title,
            title=f"{top_cand.title[:80]} #Shorts",
            description=f"{top_cand.title}\n\nSource: {top_cand.channel}",
            hashtags=["shorts", "trending"],
        )

    try:
        # Clean markdown wrappers if any
        cleaned = re.sub(r"^```json\s*|\s*```$", "", raw_json.strip())
        parsed = json.loads(cleaned)
        chosen_idx = int(parsed.get("pick_index", 0))
        chosen_c = candidates[min(chosen_idx, len(candidates) - 1)]
        return Pick(
            candidate=chosen_c,
            headline=parsed.get("headline", chosen_c.title.upper()),
            caption=parsed.get("caption", ""),
            title=parsed.get("title", f"{chosen_c.title} #Shorts"),
            description=parsed.get("description", ""),
            hashtags=parsed.get("hashtags", ["shorts"]),
        )
    except Exception as e:
        LOG.error("Failed to parse LLM response: %s (raw: %s)", e, raw_json)
        return None

# --------------------------------------------------------------------------
# Downloader
# --------------------------------------------------------------------------

def download_clip(url: str, out_path: Path, max_seconds: int = 35) -> bool:
    """Download source video clip using yt-dlp."""
    emit_progress("Downloading source media")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--max-filesize", "150M",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", str(out_path),
        url,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=90)
        return out_path.exists()
    except Exception as e:
        LOG.error("Failed to download clip %s: %s", url, e)
        return False

# --------------------------------------------------------------------------
# YouTube Upload
# --------------------------------------------------------------------------

def upload_to_youtube(
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    privacy: str,
    token_path: Path,
    category_id: str = "28",
) -> str | None:
    """Upload completed video to YouTube Shorts."""
    emit_progress("Uploading to YouTube")
    if not token_path.exists():
        raise FileNotFoundError("YouTube token.json not found. Authorize YouTube via Studio first.")

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials.from_authorized_user_file(str(token_path))
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            emit_progress(f"Uploading ({int(status.progress() * 100)}%)")

    video_id = response.get("id")
    LOG.info("Uploaded successfully! Video ID: %s", video_id)
    return video_id

# --------------------------------------------------------------------------
# Main Runner Pipeline
# --------------------------------------------------------------------------

def run_pipeline(config_path: Path, dry_run: bool = False) -> None:
    with keep_awake():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        state = State(ROOT / "state.json")

        candidates = discover_all_candidates(config, state)
        if not candidates:
            LOG.info("No candidates found.")
            emit_summary({"status": "idle", "message": "No new candidates"})
            return

        pick = pick_best_story(candidates, config)
        if not pick:
            LOG.warning("Editorial could not make a pick.")
            emit_summary({"status": "failed", "message": "No pick generated"})
            return

        LOG.info("Selected pick: %s (%s)", pick.headline, pick.candidate.url)
        work_dir = ROOT / "work"
        out_dir = ROOT / "out"
        work_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)

        stem = f"short_{int(time.time())}_{pick.candidate.key[:10]}"
        raw_clip = work_dir / f"{stem}_raw.mp4"
        final_mp4 = out_dir / f"{stem}.mp4"
        poster_png = out_dir / f"{stem}.png"

        if pick.candidate.kind == "clip":
            success = download_clip(pick.candidate.url, raw_clip, max_seconds=config.get("posting", {}).get("max_clip_seconds", 35))
            if not success:
                state.mark_failed(pick.candidate.key, "Download failed")
                emit_summary({"status": "failed", "message": "Download failed"})
                return

            emit_progress("Rendering video card")
            render(
                video=raw_clip,
                headline=pick.headline,
                out=final_mp4,
                config=config,
                max_seconds=float(config.get("posting", {}).get("max_clip_seconds", 35)),
                poster=poster_png,
            )
        else:
            emit_progress("Rendering story card")
            # Story format with images/mascot
            mascot = Path(config.get("story", {}).get("mascot", "assets/default_mascot.mp4"))
            render_story(
                headline=pick.headline,
                commentary=pick.caption,
                images=[Path(p) for p in pick.candidate.images],
                mascot=mascot if mascot.exists() else raw_clip,
                out=final_mp4,
                poster=poster_png,
                config=config,
            )

        video_id = ""
        if not dry_run:
            token_path = ROOT / "token.json"
            privacy = config.get("posting", {}).get("privacy", "private")
            tags = config.get("posting", {}).get("tags", []) + pick.hashtags
            try:
                video_id = upload_to_youtube(final_mp4, pick.title, pick.description, tags, privacy, token_path)
            except Exception as e:
                LOG.error("Upload error: %s", e)
        else:
            LOG.info("[DRY-RUN] Video rendered to %s (not uploaded)", final_mp4)

        state.mark_posted({
            "key": pick.candidate.key,
            "headline": pick.headline,
            "title": pick.title,
            "video_path": str(final_mp4),
            "youtube_id": video_id,
            "dry_run": dry_run,
            "at": time.time(),
        })

        emit_summary({
            "status": "completed",
            "video": str(final_mp4),
            "headline": pick.headline,
            "title": pick.title,
            "youtube_id": video_id,
            "dry_run": dry_run,
        })

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="KenauShorts Pipeline")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--dry-run", action="store_true", help="Render draft without uploading")
    args = parser.parse_args()

    cfg_file = Path(args.config)
    if not cfg_file.exists():
        cfg_file = ROOT / "config.example.json"

    run_pipeline(cfg_file, dry_run=args.dry_run)
