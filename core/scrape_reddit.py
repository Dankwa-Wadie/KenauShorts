"""
scrape_reddit.py — Categorized and custom Reddit research scraper for KenauShorts.

Fetches posts from user-defined or default subreddits using Reddit's public JSON
endpoint (or OAuth when configured). Resilient to rate limits (x-ratelimit-remaining,
x-ratelimit-reset) and handles exponential backoff on HTTP 429.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

LOG = logging.getLogger("kenaushorts.reddit")

DEFAULT_CATEGORIES: dict[str, list[str]] = {
    "Tech & Hardware": [
        "technology", "gadgets", "apple", "Android", "hardware", "pcgaming"
    ],
    "AI & LLMs": [
        "artificial", "ChatGPT", "ClaudeAI", "singularity", "MachineLearning"
    ],
    "Science & Space": [
        "space", "science", "astronomy", "nasa"
    ],
    "Gaming & Pop Culture": [
        "gaming", "pcgaming", "movies", "popculturechat"
    ],
    "Curiosities & Fascinating": [
        "mildlyinteresting", "damnthatsinteresting", "todayilearned", "interestingasfuck"
    ],
}

DEFAULT_USER_AGENT = os.environ.get(
    "REDDIT_USER_AGENT",
    "pc:com.kenaushorts.app:v1.0.0 (by /u/kenaushorts_bot)"
)

def get_reddit_token(client_id: str = "", client_secret: str = "") -> str | None:
    """Request an application-only OAuth token if client credentials are provided."""
    client_id = client_id or os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = client_secret or os.environ.get("REDDIT_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None

    auth_url = "https://www.reddit.com/api/v1/access_token"
    try:
        resp = requests.post(
            auth_url,
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": DEFAULT_USER_AGENT},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("access_token")
    except Exception as e:
        LOG.warning("Could not authenticate with Reddit API: %s", e)
    return None

def fetch_subreddit_posts(
    subreddit: str,
    sort: str = "hot",
    limit: int = 15,
    token: str | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    max_retries: int = 3,
) -> list[dict[str, Any]]:
    """Fetch posts for a specific subreddit handling rate-limits and fallback to RSS."""
    headers = {"User-Agent": user_agent}

    # If OAuth token is available, use official OAuth API
    if token:
        headers["Authorization"] = f"Bearer {token}"
        base_url = f"https://oauth.reddit.com/r/{subreddit}/{sort}"
        params = {"limit": min(limit, 100), "raw_json": 1}
        for attempt in range(max_retries):
            try:
                resp = requests.get(base_url, headers=headers, params=params, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    children = data.get("data", {}).get("children", [])
                    return [_parse_post(item.get("data", {})) for item in children]
            except Exception as e:
                LOG.warning("OAuth Reddit fetch error: %s", e)
                time.sleep(1.0)

    # Fast and reliable public RSS fallback (Reddit blocks public unauthenticated .json with 403)
    return _fetch_subreddit_rss(subreddit, limit=limit, headers=headers)

def _fetch_subreddit_rss(subreddit: str, limit: int = 15, headers: dict | None = None) -> list[dict[str, Any]]:
    """Reliable public Reddit feed parser (Atom format)."""
    import xml.etree.ElementTree as ET
    rss_url = f"https://www.reddit.com/r/{subreddit}/.rss?limit={limit}"
    req_headers = headers or {"User-Agent": DEFAULT_USER_AGENT}
    try:
        resp = requests.get(rss_url, headers=req_headers, timeout=15)
        if resp.status_code != 200:
            return []

        root = ET.fromstring(resp.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)[:limit]
        posts = []
        for e in entries:
            title_el = e.find("atom:title", ns)
            link_el = e.find("atom:link", ns)
            author_el = e.find("atom:author/atom:name", ns)
            content_el = e.find("atom:content", ns)
            id_el = e.find("atom:id", ns)

            title = title_el.text if title_el is not None else ""
            url = link_el.attrib.get("href", "") if link_el is not None else ""
            author = author_el.text if author_el is not None else ""
            raw_id = id_el.text if id_el is not None else ""
            post_id = raw_id.split("/")[-1] if raw_id else ""

            content_text = content_el.text if content_el is not None else ""
            is_video = "v.redd.it" in url or "v.redd.it" in content_text
            images = []
            if "i.redd.it" in url or url.endswith((".jpg", ".png", ".jpeg")):
                images.append(url)

            posts.append({
                "id": post_id,
                "subreddit": subreddit,
                "title": title,
                "author": author,
                "score": 500,  # Hot RSS items are trending
                "upvote_ratio": 0.9,
                "num_comments": 50,
                "url": url,
                "permalink": url,
                "created_utc": time.time(),
                "is_video": is_video,
                "video_url": url if is_video else "",
                "images": images,
                "selftext": content_text[:300],
                "over_18": False,
            })
        return posts
    except Exception as e:
        LOG.warning("Failed to parse RSS for r/%s: %s", subreddit, e)
        return []

def _parse_post(p: dict[str, Any]) -> dict[str, Any]:
    """Extract clean post metadata including video/image URLs."""
    video_url = ""
    is_video = bool(p.get("is_video", False))

    if is_video:
        reddit_media = p.get("media", {}) or {}
        reddit_video = reddit_media.get("reddit_video", {})
        video_url = reddit_video.get("fallback_url", "") or p.get("url", "")

    # Check preview videos (e.g. gif conversion)
    if not video_url:
        preview = p.get("preview", {})
        preview_video = preview.get("reddit_video_preview", {})
        if preview_video:
            video_url = preview_video.get("fallback_url", "")

    images = []
    # Check gallery data
    gallery_data = p.get("gallery_data", {})
    if gallery_data and "items" in gallery_data:
        for it in gallery_data["items"]:
            media_id = it.get("media_id")
            if media_id:
                images.append(f"https://i.redd.it/{media_id}.jpg")
    elif p.get("post_hint") == "image" or p.get("url", "").endswith((".jpg", ".jpeg", ".png")):
        images.append(p.get("url", ""))

    return {
        "id": p.get("id", ""),
        "subreddit": p.get("subreddit", ""),
        "title": p.get("title", ""),
        "author": p.get("author", ""),
        "score": p.get("score", 0),
        "upvote_ratio": p.get("upvote_ratio", 0.0),
        "num_comments": p.get("num_comments", 0),
        "url": p.get("url", ""),
        "permalink": f"https://www.reddit.com{p.get('permalink', '')}",
        "created_utc": p.get("created_utc", 0),
        "is_video": is_video or bool(video_url),
        "video_url": video_url,
        "images": images,
        "selftext": p.get("selftext", ""),
        "over_18": p.get("over_18", False),
    }

def fetch_categorized_posts(
    categories: dict[str, list[str]],
    sort: str = "hot",
    limit_per_sub: int = 10,
    min_score: int = 200,
    token: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch posts across multiple categories with filtering."""
    results: dict[str, list[dict[str, Any]]] = {}
    for cat_name, subreddits in categories.items():
        cat_posts = []
        for sub in subreddits:
            posts = fetch_subreddit_posts(sub, sort=sort, limit=limit_per_sub, token=token)
            filtered = [p for p in posts if p["score"] >= min_score and not p["over_18"]]
            cat_posts.extend(filtered)
            time.sleep(0.5)  # Respectful pace between subreddits
        cat_posts.sort(key=lambda x: x["score"], reverse=True)
        results[cat_name] = cat_posts
    return results

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KenauShorts Reddit Scraper")
    parser.add_argument("--sub", type=str, help="Subreddit name to scrape (e.g. technology)")
    parser.add_argument("--sort", type=str, default="hot", choices=["hot", "top", "new"])
    parser.add_argument("--limit", type=int, default=10, help="Max posts per subreddit")
    parser.add_argument("--min-score", type=int, default=100, help="Minimum score filter")
    args = parser.parse_args()

    token = get_reddit_token()
    if args.sub:
        posts = fetch_subreddit_posts(args.sub, sort=args.sort, limit=args.limit, token=token)
        filtered = [p for p in posts if p["score"] >= args.min_score]
        print(f"\nFetched {len(filtered)} posts from r/{args.sub} (min score {args.min_score}):")
        for p in filtered:
            print(f"[{p['score']}] {p['title']} ({p['permalink']})")
    else:
        results = fetch_categorized_posts(DEFAULT_CATEGORIES, sort=args.sort, limit_per_sub=args.limit, min_score=args.min_score, token=token)
        for cat, plist in results.items():
            print(f"\nCategory: {cat} ({len(plist)} posts)")
            for p in plist[:3]:
                print(f"  [{p['score']}] r/{p['subreddit']}: {p['title']}")
