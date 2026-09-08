"""
agent.py — Core discovery, editorial curation, rendering, and publishing engine for KenauShorts.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
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
from studio import store

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

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text or "")).strip()

def discover_rss(feeds: list[str], per_feed: int = 10) -> list[Candidate]:
    """Parse RSS/Atom news feeds into story candidates (context, not a video source)."""
    import feedparser

    out: list[Candidate] = []
    for feed in feeds:
        label = urllib.parse.urlparse(feed).netloc or feed
        before = len(out)
        try:
            resp = requests.get(feed, headers={"User-Agent": "KenauShorts/1.0"}, timeout=15)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            for entry in parsed.entries[:per_feed]:
                title = getattr(entry, "title", "").strip()
                link = getattr(entry, "link", "").strip()
                if not title or not link:
                    continue
                summary = _clean(getattr(entry, "summary", ""))[:400]
                pub_ts = time.time()
                if getattr(entry, "published_parsed", None):
                    pub_ts = time.mktime(entry.published_parsed)
                out.append(Candidate(
                    kind="story",
                    key=f"rss_{hashlib.sha1(link.encode()).hexdigest()[:16]}",
                    title=title,
                    url=link,
                    source=f"rss:{label}",
                    channel=label,
                    licence="standard",
                    text=summary,
                    published_at=pub_ts,
                ))
            LOG.info("rss %s -> %d items", label, len(out) - before)
        except Exception as e:
            LOG.warning("rss %s failed: %s", label, e)
    return out

WIKI_FEED = "https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{}/{}/{}"

def _wiki_image(page: dict[str, Any], min_width: int = 600) -> str:
    """Prefer the original file; fall back to the thumbnail."""
    for key in ("originalimage", "thumbnail"):
        img = page.get(key) or {}
        src = img.get("source")
        if src and int(img.get("width") or 0) >= (min_width if key == "thumbnail" else 0):
            return src
    return ""

def discover_wikimedia(sections: list[str], days_back: int = 2, max_per_section: int = 6) -> list[Candidate]:
    """
    Story candidates from Wikimedia's featured-content feed.

    One request per day returns the picture of the day, on-this-day events and
    the most-read articles — all explicitly licensed for reuse (unlike a
    Reddit repost, which is why this source exists at all).
    """
    out: list[Candidate] = []
    today = dt.datetime.now(dt.timezone.utc).date()

    for delta in range(days_back):
        day = today - dt.timedelta(days=delta)
        url = WIKI_FEED.format(day.year, f"{day.month:02d}", f"{day.day:02d}")
        if delta:
            time.sleep(1.0)
        try:
            resp = requests.get(url, headers={"User-Agent": "KenauShorts/1.0"}, timeout=25)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            LOG.warning("wikimedia feed %s failed: %s", day, e)
            continue

        if "image" in sections and isinstance(data.get("image"), dict):
            img = data["image"]
            src = (img.get("original") or {}).get("source") or ""
            desc = _clean(((img.get("description") or {}).get("text")) or "")
            artist = _clean(((img.get("artist") or {}).get("text")) or "")
            lic = (img.get("license") or {}).get("type") or ""
            if src and desc:
                key_src = img.get("file_page") or src
                out.append(Candidate(
                    kind="story",
                    key=f"wiki_{hashlib.sha1(key_src.encode()).hexdigest()[:16]}",
                    title=desc[:300],
                    url=img.get("file_page") or src,
                    source="wikimedia:picture-of-the-day",
                    channel=" / ".join(x for x in (artist, lic) if x) or "Wikimedia Commons",
                    licence="cc",
                    text=desc[:600],
                    images=[src],
                    published_at=dt.datetime.combine(day, dt.time()).timestamp(),
                ))

        if "onthisday" in sections:
            for ev in (data.get("onthisday") or [])[:max_per_section]:
                pages = [p for p in (ev.get("pages") or []) if _wiki_image(p)]
                if not pages:
                    continue
                year = ev.get("year")
                text = _clean(ev.get("text") or "")
                if not text:
                    continue
                page = pages[0]
                page_url = (page.get("content_urls") or {}).get("desktop", {}).get("page", "")
                title = (f"On this day in {year}: {text}" if year else text)[:300]
                out.append(Candidate(
                    kind="story",
                    key=f"wiki_{hashlib.sha1((page_url or title).encode()).hexdigest()[:16]}",
                    title=title,
                    url=page_url,
                    source="wikimedia:on-this-day",
                    channel="Wikimedia Commons",
                    licence="cc",
                    text=_clean(page.get("extract") or "")[:600],
                    images=[_wiki_image(p) for p in pages[:2]],
                    published_at=dt.datetime.combine(day, dt.time()).timestamp(),
                ))

        if "mostread" in sections:
            for page in ((data.get("mostread") or {}).get("articles") or [])[:max_per_section]:
                src = _wiki_image(page)
                extract = _clean(page.get("extract") or "")
                if not src or len(extract) < 80:
                    continue
                page_url = (page.get("content_urls") or {}).get("desktop", {}).get("page", "")
                out.append(Candidate(
                    kind="story",
                    key=f"wiki_{hashlib.sha1((page_url or extract).encode()).hexdigest()[:16]}",
                    title=extract[:300],
                    url=page_url,
                    source="wikimedia:most-read",
                    channel="Wikimedia Commons",
                    licence="cc",
                    text=extract[:600],
                    images=[src],
                    published_at=dt.datetime.combine(day, dt.time()).timestamp(),
                ))

        LOG.info("wikimedia %s -> %d story candidates", day, len(out))
        time.sleep(0.5)
    return out

WIKI_API = "https://en.wikipedia.org/w/api.php"

def is_free_image_url(url: str) -> bool:
    """
    Free-licence check with no extra request.

    Wikimedia serves free files from /wikipedia/commons/ — Commons only
    accepts freely-licensed material. Non-free files (film posters, album
    covers, box art) are uploaded locally to a language wiki under a fair-use
    rationale that covers Wikipedia's own use and nobody else's, and serve
    from /wikipedia/en/ instead. So the path segment alone tells reusable
    from "will get you a copyright claim", for free, on every image.
    """
    return "/wikipedia/commons/" in url

def discover_wikipedia_search(queries: list[str], per_query: int = 8, free_only: bool = True) -> list[Candidate]:
    """Topic-driven story candidates: search Wikipedia, keep articles with images."""
    out: list[Candidate] = []
    for q in queries:
        before = len(out)
        params = {
            "action": "query", "format": "json", "formatversion": "2",
            "generator": "search", "gsrsearch": q, "gsrlimit": per_query,
            "gsrnamespace": "0",
            "prop": "extracts|pageimages|info", "inprop": "url",
            "exintro": "1", "explaintext": "1", "exsentences": "3",
            "piprop": "original|thumbnail", "pithumbsize": "1200",
        }
        try:
            resp = requests.get(WIKI_API, params=params, headers={"User-Agent": "KenauShorts/1.0"}, timeout=25)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            LOG.warning("wikipedia search %r failed: %s", q, e)
            continue

        skipped_nonfree = 0
        for page in (data.get("query", {}) or {}).get("pages", []):
            img = (page.get("original") or page.get("thumbnail") or {}).get("source", "")
            extract = _clean(page.get("extract") or "")
            if not img or len(extract) < 80:
                continue
            if free_only and not is_free_image_url(img):
                skipped_nonfree += 1
                continue
            page_url = page.get("fullurl") or f"https://en.wikipedia.org/wiki/{page.get('title', '')}"
            out.append(Candidate(
                kind="story",
                key=f"wiki_{hashlib.sha1(page_url.encode()).hexdigest()[:16]}",
                title=extract[:300],
                url=page_url,
                source=f"wikipedia:{q[:28]}",
                channel="Wikimedia Commons" if is_free_image_url(img) else "Wikipedia",
                licence="cc" if is_free_image_url(img) else "standard",
                text=extract[:600],
                images=[img],
            ))
        msg = f"wikipedia '{q[:34]}' -> {len(out) - before} candidates"
        if skipped_nonfree:
            msg += f" ({skipped_nonfree} dropped: non-free image)"
        LOG.info(msg)
        time.sleep(0.4)
    return out

def _video_id(url: str) -> str:
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else ""

def discover_youtube(queries: list[str], api_key: str, per_query: int = 8,
                     days_back: int = 0, cc_only: bool = True) -> list[Candidate]:
    """
    YouTube Data API search — a clip source driven by topic rather than channel.

    videoLicense=creativeCommon makes YouTube filter server-side, so results
    are reusable by construction. Costs 100 quota units per query, so keep
    the query list short.
    """
    if not api_key or not queries:
        if queries and not api_key:
            LOG.warning("discovery.youtube_queries is set but YOUTUBE_API_KEY is not — "
                        "skipping the search-based clip source entirely")
        return []
    out: list[Candidate] = []

    for q in queries:
        params: dict[str, Any] = {
            "part": "snippet", "q": q, "type": "video", "order": "viewCount",
            "maxResults": per_query,
            "videoDuration": "short", "videoEmbeddable": "true", "key": api_key,
        }
        if cc_only:
            params["videoLicense"] = "creativeCommon"
        if days_back:
            params["publishedAfter"] = (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_back)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            resp = requests.get("https://www.googleapis.com/youtube/v3/search", params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            LOG.warning("youtube search %r failed: %s", q, e)
            continue

        before = len(out)
        for item in data.get("items", []):
            vid = item.get("id", {}).get("videoId")
            sn = item.get("snippet", {})
            if not vid:
                continue
            pub_ts = time.time()
            if sn.get("publishedAt"):
                try:
                    pub_ts = dt.datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00")).timestamp()
                except Exception:
                    pass
            out.append(Candidate(
                kind="clip",
                key=f"yt_{vid}",
                title=sn.get("title", ""),
                url=f"https://www.youtube.com/watch?v={vid}",
                source="youtube_search",
                channel=sn.get("channelTitle", q),
                licence="unknown",
                text=sn.get("description", "")[:400],
                published_at=pub_ts,
            ))
        LOG.info("youtube search %r -> %d clip(s)%s", q, len(out) - before,
                 " [CC only]" if cc_only else "")
    return out

def verify_youtube_licenses(cands: list[Candidate], api_key: str, max_seconds: int = 0) -> list[Candidate]:
    """
    Keep only YouTube clips we are actually allowed to repost.

    Without this, the agent cannot tell a Creative Commons upload from an
    all-rights-reserved one, and a bot that reposts the wrong one collects
    strikes instead of views. videos.list costs one quota unit per call and
    takes 50 ids at a time.

    A clip passes if status.license == "creativeCommon" (YouTube's own CC-BY
    flag), or its channel was declared "public-domain"/"cc" in
    discovery.youtube_channels — needed because US government work (NASA,
    NOAA) is public domain by statute but still shows the default "youtube"
    licence. The same call also returns duration and liveStreamingDetails, so
    unstarted premieres and unfinished livestreams are dropped here instead
    of reaching the downloader.
    """
    yt = [c for c in cands if _video_id(c.url)]
    other = [c for c in cands if not _video_id(c.url)]
    if not yt:
        return cands

    trusted = [c for c in yt if c.licence in ("public-domain", "cc")]
    if not api_key:
        LOG.warning(
            "no YOUTUBE_API_KEY — cannot read licence flags, so only the %d "
            "clip(s) from channels marked public-domain/cc in config are "
            "usable (%d dropped). Set YOUTUBE_API_KEY to open this up.",
            len(trusted), len(yt) - len(trusted))
        return other + trusted

    by_id = {_video_id(c.url): c for c in yt}
    ids = list(by_id)
    kept: list[Candidate] = []
    reasons: dict[str, int] = {}

    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        params = {
            "part": "status,contentDetails,liveStreamingDetails,snippet",
            "id": ",".join(batch), "key": api_key,
        }
        try:
            resp = requests.get("https://www.googleapis.com/youtube/v3/videos", params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            LOG.warning("licence check failed (%s) — dropping %d clip(s) rather than guessing", e, len(batch))
            reasons["licence check failed"] = reasons.get("licence check failed", 0) + len(batch)
            continue

        for item in data.get("items", []):
            c = by_id.get(item.get("id", ""))
            if not c:
                continue
            st = item.get("status", {})
            cd = item.get("contentDetails", {})
            live = item.get("snippet", {}).get("liveBroadcastContent", "none")

            def drop(why: str) -> None:
                reasons[why] = reasons.get(why, 0) + 1

            if live and live != "none":
                drop(f"live/upcoming ({live})")
                continue
            if item.get("liveStreamingDetails") and not item["liveStreamingDetails"].get("actualEndTime"):
                drop("livestream not finished")
                continue
            if st.get("privacyStatus") != "public":
                drop("not public")
                continue
            if cd.get("regionRestriction", {}).get("blocked"):
                drop("region blocked")
                continue

            secs = _iso8601_seconds(cd.get("duration", ""))
            if max_seconds and secs and secs > max_seconds:
                drop(f"longer than {max_seconds}s")
                continue
            if secs == 0:
                drop("zero/unknown duration")
                continue

            lic = st.get("license", "youtube")
            if lic == "creativeCommon":
                kept.append(c)
            elif c.licence in ("public-domain", "cc"):
                kept.append(c)
            else:
                drop("all rights reserved")

    if reasons:
        LOG.info("licence check dropped %d clip(s): %s",
                 sum(reasons.values()),
                 ", ".join(f"{n}x {w}" for w, n in sorted(reasons.items(), key=lambda kv: -kv[1])))
    LOG.info("licence check kept %d of %d clip(s) (cost: %d quota unit(s))",
             len(kept), len(yt), (len(ids) + 49) // 50)
    return other + kept

def _iso8601_seconds(dur: str) -> int:
    m = re.fullmatch(r"P(?:\d+D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", dur or "")
    if not m:
        return 0
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s

def discover_all_candidates(config: dict[str, Any], state: State) -> list[Candidate]:
    discovery = config.get("discovery", {})
    channels = discovery.get("youtube_channels", [])
    subreddits = discovery.get("subreddits", [])
    youtube_api_key = os.environ.get("YOUTUBE_API_KEY", "")

    emit_progress("Discovering content")
    yt_candidates = discover_youtube_rss(channels, per_channel=int(discovery.get("per_channel", 6)))
    reddit_candidates = discover_reddit(subreddits)

    # These sources are opt-in — an empty list in config (the default) keeps
    # discovery exactly as it was, so existing setups are unaffected.
    yt_search_candidates = discover_youtube(
        discovery.get("youtube_queries", []), youtube_api_key,
        cc_only=bool(discovery.get("youtube_cc_only", True)),
    )
    rss_candidates = discover_rss(discovery.get("news_rss", []))
    wikimedia_candidates = discover_wikimedia(
        discovery.get("wikimedia_sections", []),
        days_back=int(discovery.get("wikimedia_days_back", 2)),
    )
    wikipedia_candidates = discover_wikipedia_search(
        discovery.get("wikipedia_queries", []),
        free_only=bool(discovery.get("free_images_only", True)),
    )

    # Only YouTube clips need a licence check — Reddit's own over_18/score
    # gate is separate, and the other sources are licensed at discovery time.
    max_clip_seconds = int(config.get("posting", {}).get("max_clip_seconds", 35))
    verified_yt = verify_youtube_licenses(
        yt_candidates + yt_search_candidates, youtube_api_key, max_seconds=max_clip_seconds,
    )

    all_c = verified_yt + reddit_candidates + rss_candidates + wikimedia_candidates + wikipedia_candidates
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

def _fallback_picks(candidates: list[Candidate], n: int) -> list[Pick]:
    """Heuristic picks used when no editorial provider is configured or all fail."""
    picks = []
    for c in candidates[:n]:
        picks.append(Pick(
            candidate=c,
            headline=c.title[:60].upper(),
            caption=c.text[:120] or c.title,
            title=f"{c.title[:80]} #Shorts",
            description=f"{c.title}\n\nSource: {c.channel}",
            hashtags=["shorts", "trending"],
        ))
    return picks

def pick_stories(candidates: list[Candidate], config: dict[str, Any], n: int = 1) -> list[Pick]:
    """
    Ask an LLM for the best N candidates, best first.

    Returning more than one matters: if the top pick's download or render
    fails, run_pipeline can fall through to the next one instead of failing
    the whole cycle — a bad network fetch on one clip no longer costs an
    entire run.
    """
    if not candidates:
        return []

    editorial = config.get("editorial", {})
    providers = editorial.get("provider_order", ["gemini", "anthropic", "openai"])
    system = editorial.get("system_prompt", "You are an expert short-form video editor. Select the best stories.")

    listings = []
    for idx, c in enumerate(candidates[:20]):
        listings.append(f"[{idx}] Source: {c.channel} ({c.source})\nTitle: {c.title}\nDetails: {c.text}\n")

    user_prompt = (
        f"Analyze the following candidates and pick up to {n} of the best, best first, for a "
        "30-second YouTube Short. Only the first will normally be used — the rest are spares in "
        "case its download fails, so order matters. Return fewer if the rest aren't good enough.\n"
        "Output valid JSON ONLY matching this schema:\n"
        "{\n"
        '  "picks": [\n'
        "    {\n"
        '      "pick_index": 0,\n'
        '      "headline": "SHORT PUNCHY ALL-CAPS HEADLINE (UNDER 10 WORDS)",\n'
        '      "caption": "A 1-2 sentence commentary or punchy explanation",\n'
        '      "title": "Engaging YouTube Shorts Title #Shorts",\n'
        '      "description": "2-sentence YouTube description with hashtags",\n'
        '      "hashtags": ["shorts", "tech", "news"]\n'
        "    }\n"
        "  ]\n"
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
        LOG.info("No AI provider responded; using top candidate(s) as fallback.")
        return _fallback_picks(candidates, n)

    try:
        cleaned = re.sub(r"^```json\s*|\s*```$", "", raw_json.strip())
        parsed = json.loads(cleaned)
        raw_picks = parsed.get("picks")
        if not isinstance(raw_picks, list):
            # Tolerate a provider that ignores the array wrapper and returns
            # one pick object directly — the earlier single-pick schema.
            raw_picks = [parsed] if "pick_index" in parsed else []
    except Exception as e:
        LOG.error("Failed to parse LLM response: %s (raw: %s)", e, raw_json)
        return _fallback_picks(candidates, n)

    picks: list[Pick] = []
    for p in raw_picks[:n]:
        try:
            idx = int(p.get("pick_index", -1))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(candidates)):
            LOG.warning("editorial returned an out-of-range pick_index: %r", idx)
            continue
        chosen_c = candidates[idx]
        picks.append(Pick(
            candidate=chosen_c,
            headline=p.get("headline", chosen_c.title.upper()),
            caption=p.get("caption", ""),
            title=p.get("title", f"{chosen_c.title} #Shorts"),
            description=p.get("description", ""),
            hashtags=p.get("hashtags", ["shorts"]),
        ))

    return picks or _fallback_picks(candidates, n)

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

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

def download_images(urls: list[str], dest_dir: Path, stem: str) -> list[Path]:
    """
    Fetch story images to local files. Returns only the ones that arrived.

    Story candidates carry remote image URLs, not local files — wrapping a
    URL string in a Path() (as this used to do) never downloads anything, it
    just hands Pillow a nonexistent local path, which fails silently and
    renders the card with zero images. A partial set is still usable, so one
    failed download doesn't sink the whole post.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for i, url in enumerate(urls):
        ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
        if ext not in IMAGE_EXTS:
            ext = ".jpg"
        target = dest_dir / f"{stem}_img{i}{ext}"
        try:
            resp = requests.get(url, headers={"User-Agent": "KenauShorts/1.0"}, timeout=30)
            resp.raise_for_status()
            target.write_bytes(resp.content)
            saved.append(target)
        except Exception as e:
            LOG.warning("image %d failed (%s): %s", i + 1, url[:60], e)
    if saved:
        LOG.info("downloaded %d/%d image(s)", len(saved), len(urls))
    return saved

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

def _render_pick(pick: Pick, config: dict[str, Any], work_dir: Path, out_dir: Path, stem: str) -> Path | None:
    """Download the source media and render one pick. Returns the final mp4, or None on failure."""
    raw_clip = work_dir / f"{stem}_raw.mp4"
    final_mp4 = out_dir / f"{stem}.mp4"
    poster_png = out_dir / f"{stem}.png"
    max_clip_seconds = config.get("posting", {}).get("max_clip_seconds", 35)

    if pick.candidate.kind == "clip":
        if not download_clip(pick.candidate.url, raw_clip, max_seconds=max_clip_seconds):
            return None

        emit_progress("Rendering video card")
        render(
            video=raw_clip,
            headline=pick.headline,
            out=final_mp4,
            config=config,
            max_seconds=float(max_clip_seconds),
            poster=poster_png,
        )
    else:
        emit_progress("Rendering story card")
        images = download_images(pick.candidate.images, work_dir, stem)
        mascot = Path(config.get("story", {}).get("mascot", "assets/default_mascot.mp4"))
        render_story(
            headline=pick.headline,
            commentary=pick.caption,
            images=images,
            mascot=mascot if mascot.exists() else raw_clip,
            out=final_mp4,
            poster=poster_png,
            config=config,
        )

    return final_mp4

def run_pipeline(config_path: Path, dry_run: bool = False) -> None:
    with keep_awake():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        state = State(ROOT / "state.json")

        candidates = discover_all_candidates(config, state)
        if not candidates:
            LOG.info("No candidates found.")
            emit_summary({"status": "idle", "message": "No new candidates"})
            return

        max_candidates = int(config.get("editorial", {}).get("max_candidates", 3))
        picks = pick_stories(candidates, config, n=max_candidates)
        if not picks:
            LOG.warning("Editorial could not make a pick.")
            emit_summary({"status": "failed", "message": "No pick generated"})
            return

        work_dir = ROOT / "work"
        out_dir = ROOT / "out"
        work_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)

        # A failed download shouldn't cost the whole run — try each backup
        # pick in order before giving up on this cycle entirely.
        pick: Pick | None = None
        final_mp4: Path | None = None
        stem = ""
        for candidate_pick in picks:
            LOG.info("Trying pick: %s (%s)", candidate_pick.headline, candidate_pick.candidate.url)
            stem = f"short_{int(time.time())}_{candidate_pick.candidate.key[:10]}"
            try:
                result = _render_pick(candidate_pick, config, work_dir, out_dir, stem)
            except Exception as e:
                # A render crash (bad ffmpeg input, missing mascot asset, a
                # corrupt download) must not take down the whole run when
                # there are backup picks left to try.
                LOG.error("Render error for %s: %s", candidate_pick.candidate.url, e)
                result = None
            if result is not None:
                pick, final_mp4 = candidate_pick, result
                break
            LOG.warning("Pick failed (%s) — trying the next backup", candidate_pick.candidate.url)
            state.mark_failed(candidate_pick.candidate.key, "Download or render failed")

        if pick is None or final_mp4 is None:
            emit_summary({"status": "failed", "message": "All picks failed to download or render"})
            return

        poster_png = out_dir / f"{stem}.png"

        # Register the render with the Studio so it shows up in the Library
        # for review/edit/upload — without this, a "preview" job renders a
        # video that nothing in the UI can ever find.
        video_record = store.draft(
            video_stem=stem,
            video_path=final_mp4,
            poster_path=poster_png,
            headline=pick.headline,
            title=pick.title,
            description=pick.description,
            config=config,
            candidate_data=dataclasses.asdict(pick.candidate),
        )

        video_id = ""
        if not dry_run:
            token_path = ROOT / "token.json"
            privacy = config.get("posting", {}).get("privacy", "private")
            tags = config.get("posting", {}).get("tags", []) + pick.hashtags
            try:
                video_id = upload_to_youtube(final_mp4, pick.title, pick.description, tags, privacy, token_path)
                video_record.update(status="uploaded", youtube_id=video_id, uploaded_at=store.now())
                store.put("videos", video_record["id"], video_record)
            except Exception as e:
                LOG.error("Upload error: %s", e)
                video_record.update(status="failed", error=str(e))
                store.put("videos", video_record["id"], video_record)
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
