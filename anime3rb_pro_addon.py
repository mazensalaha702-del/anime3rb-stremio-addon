#!/usr/bin/env python3
"""
Anime3rb Stremio Addon - Hybrid PRO Version
Strategy:
  1. On startup, use nodriver (real Chrome) ONCE to solve Cloudflare challenge
     and extract cf_clearance cookies.
  2. All subsequent requests use curl_cffi (fast, lightweight) with those cookies.
  3. If cookies expire, auto-refresh in background.
"""

import os
import re
import json
import time
import asyncio
import threading
import urllib.parse
import logging
import sys
import traceback
from typing import Optional, Dict, List, Tuple

import nodriver as uc
from curl_cffi import requests as cffi_requests
from flask import Flask, jsonify, request

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("anime3rb")

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
BASE_URL = "https://anime3rb.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
COOKIE_REFRESH_INTERVAL = 60 * 60  # 1 hour

MANUAL_SLUGS = {
    "tt3296914": "d-frag-eky",
}

# ─────────────────────────────────────────────
# Global State
# ─────────────────────────────────────────────
_cf_cookies: Dict[str, str] = {}
_cf_cookies_lock = threading.Lock()
_cookies_last_refresh: float = 0
_async_loop: Optional[asyncio.AbstractEventLoop] = None
_title_cache: Dict[str, str] = {}


# ─────────────────────────────────────────────
# Async Bridge
# ─────────────────────────────────────────────
def _start_async_loop():
    global _async_loop
    _async_loop = asyncio.new_event_loop()
    _async_loop.run_forever()

def run_async(coro, timeout=120):
    if _async_loop is None:
        raise RuntimeError("Async loop not started")
    return asyncio.run_coroutine_threadsafe(coro, _async_loop).result(timeout=timeout)


# ─────────────────────────────────────────────
# Step 1: Get Cloudflare cookies using nodriver (once)
# ─────────────────────────────────────────────
async def _fetch_cf_cookies_async() -> Dict[str, str]:
    log.info("🌐 Launching Chrome to solve Cloudflare challenge...")
    browser = None
    try:
        browser = await uc.start(
            headless=False,
            no_sandbox=True,
            browser_args=[
                "--window-position=-2000,0",
                "--no-sandbox",
                "--mute-audio",
                "--disable-gpu",
                "--disable-dev-shm-usage",
            ]
        )
        page = await browser.get(BASE_URL)
        
        # Wait for Cloudflare to be solved (max 30s)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                title = await page.evaluate("document.title")
                if title and "Just a moment" not in title and "Attention" not in title:
                    log.info(f"✅ Cloudflare solved! Page title: {title[:40]}")
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)
        
        # Extract cookies
        raw_cookies = await browser.cookies.get_all()
        cookies = {}
        for c in raw_cookies:
            if hasattr(c, 'name') and hasattr(c, 'value'):
                cookies[c.name] = c.value
            elif isinstance(c, dict):
                cookies[c.get('name', '')] = c.get('value', '')
        
        log.info(f"✅ Extracted {len(cookies)} cookies: {list(cookies.keys())}")
        return cookies
    except Exception as e:
        log.error(f"❌ Failed to get CF cookies: {e}")
        return {}
    finally:
        if browser:
            try:
                browser.stop()
            except Exception:
                pass


def refresh_cf_cookies():
    global _cf_cookies, _cookies_last_refresh
    log.info("🔄 Refreshing Cloudflare cookies...")
    try:
        cookies = run_async(_fetch_cf_cookies_async(), timeout=60)
        with _cf_cookies_lock:
            _cf_cookies = cookies
            _cookies_last_refresh = time.time()
        log.info(f"✅ Cookies refreshed. Got: {list(cookies.keys())}")
    except Exception as e:
        log.error(f"❌ Cookie refresh failed: {e}\n{traceback.format_exc()}")


def get_cookies() -> Dict[str, str]:
    """Return current CF cookies, refresh if expired."""
    if time.time() - _cookies_last_refresh > COOKIE_REFRESH_INTERVAL:
        log.info("🕐 Cookies expired, refreshing in background...")
        threading.Thread(target=refresh_cf_cookies, daemon=True).start()
    with _cf_cookies_lock:
        return dict(_cf_cookies)


# ─────────────────────────────────────────────
# Step 2: Fast requests using curl_cffi + cookies
# ─────────────────────────────────────────────
def cffi_get(url: str, **kwargs) -> cffi_requests.Response:
    cookies = get_cookies()
    session = cffi_requests.Session(impersonate="chrome124")
    headers = {
        "User-Agent": UA,
        "Referer": BASE_URL + "/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ar,en;q=0.5",
    }
    return session.get(url, headers=headers, cookies=cookies, timeout=15, **kwargs)


# ─────────────────────────────────────────────
# Anime3rb Logic
# ─────────────────────────────────────────────
def search_slugs(query: str) -> List[str]:
    if not query:
        return []
    slugs = []
    try:
        api_url = f"{BASE_URL}/api/v1/search?q={urllib.parse.quote(query)}"
        resp = cffi_get(api_url)
        log.info(f"[SEARCH] API status: {resp.status_code} for '{query}'")
        if resp.status_code == 200:
            data = resp.json()
            for item in data:
                if isinstance(item, dict) and item.get("slug"):
                    slugs.append(item["slug"])
            if slugs:
                log.info(f"[SEARCH] Found slugs via API: {slugs[:3]}")
                return slugs
    except Exception as e:
        log.warning(f"[SEARCH] API failed: {e}")

    try:
        search_url = f"{BASE_URL}/search?q={urllib.parse.quote(query)}"
        resp = cffi_get(search_url)
        log.info(f"[SEARCH] HTML status: {resp.status_code}")
        if resp.status_code == 200:
            matches = re.findall(r'href=["\'](?:https?://anime3rb\.com)?/titles/([^"\'/?#]+)', resp.text)
            for m in matches:
                if m and m not in slugs:
                    slugs.append(m)
    except Exception as e:
        log.warning(f"[SEARCH] HTML fallback failed: {e}")

    return slugs


def get_episode_streams(slug: str, episode: int) -> List[Dict]:
    url = f"{BASE_URL}/episode/{slug}/{episode}"
    try:
        resp = cffi_get(url)
        log.info(f"[STREAM] Episode page status: {resp.status_code} | {url}")
        if resp.status_code != 200:
            return []

        iframe_url = None
        m = re.search(r'iframe[^>]*src=["\']([^"\']*vid3rb\.com[^"\']*)["\']', resp.text)
        if m:
            iframe_url = m.group(1)
            if iframe_url.startswith('//'):
                iframe_url = 'https:' + iframe_url

        if not iframe_url:
            log.warning(f"[STREAM] No vid3rb iframe found in {url}")
            return []

        log.info(f"[STREAM] vid3rb player: {iframe_url[:80]}")

        # Player page - no Cloudflare protection
        player_resp = cffi_requests.get(
            iframe_url,
            headers={"Referer": url, "User-Agent": UA},
            timeout=15
        )

        sources = []
        video_matches = re.findall(r'var\s+video_sources\s*=\s*(\[.*?\]);', player_resp.text, re.DOTALL)
        if video_matches:
            raw = json.loads(video_matches[0])
            for item in raw:
                if not item.get("premium", False) and item.get("src"):
                    sources.append({"url": item["src"], "label": item.get("label", "Unknown")})

        order = {"1080p": 0, "720p": 1, "480p": 2, "360p": 3}
        sources.sort(key=lambda x: order.get(x["label"].lower(), 99))

        log.info(f"[STREAM] Found {len(sources)} sources for {slug} ep{episode}")
        return sources
    except Exception as e:
        log.error(f"[STREAM] Error: {e}")
        return []


# ─────────────────────────────────────────────
# ID Resolution
# ─────────────────────────────────────────────
def get_series_name(imdb_id: str) -> str:
    if imdb_id.startswith("kitsu:"):
        try:
            kitsu_id = imdb_id.split(":", 1)[1]
            r = cffi_requests.get(
                f"https://kitsu.io/api/edge/anime/{kitsu_id}",
                headers={"Accept": "application/vnd.api+json", "User-Agent": UA},
                timeout=8
            )
            if r.status_code == 200:
                attrs = (r.json().get("data") or {}).get("attributes") or {}
                titles = attrs.get("titles") or {}
                name = titles.get("en") or titles.get("en_us") or titles.get("en_jp")
                if not name:
                    slug = attrs.get("slug", "")
                    if slug:
                        name = slug.replace("-", " ")
                if name:
                    return " ".join(name.split())
        except Exception:
            pass

    if imdb_id.startswith("anilist:"):
        try:
            anilist_id = int(imdb_id.split(":", 1)[1])
            gql = '{"query":"query($id:Int){Media(id:$id,type:ANIME){title{romaji english}}}","variables":{"id":%d}}' % anilist_id
            r = cffi_requests.post(
                "https://graphql.anilist.co",
                content=gql,
                headers={"Content-Type": "application/json", "User-Agent": UA},
                timeout=8
            )
            if r.status_code == 200:
                t = ((r.json().get("data") or {}).get("Media") or {}).get("title") or {}
                name = t.get("english") or t.get("romaji")
                if name:
                    return " ".join(name.split())
        except Exception:
            pass

    for url in [
        f"https://v3-cinemeta.strem.io/meta/series/{imdb_id}.json",
        f"https://cinemeta-live.strem.io/meta/series/{imdb_id}.json",
        f"https://cinemeta-live.strem.io/meta/anime/{imdb_id}.json",
    ]:
        try:
            r = cffi_requests.get(url, headers={"User-Agent": UA}, timeout=8)
            name = r.json().get("meta", {}).get("name")
            if name:
                log.info(f"[RESOLVE] {imdb_id} → '{name}' via Cinemeta")
                return name
        except Exception:
            continue

    return ""


def parse_stremio_id(item_id: str) -> Tuple[str, int]:
    norm = (item_id or "").strip()
    if "/" in norm:
        parts = norm.split("/")
        if parts[0] in {"series", "movie", "anime"}:
            norm = "/".join(parts[1:])

    parts = norm.split(":")
    episode = 1
    imdb_id = norm

    if len(parts) >= 2 and parts[0] in {"kitsu", "anilist", "myanimelist"}:
        provider_id = parts[1]
        if len(parts) >= 3 and parts[-1].isdigit():
            episode = int(parts[-1])
        return f"{parts[0]}:{provider_id}", episode

    if len(parts) >= 4 and parts[-1].isdigit() and parts[-2].isdigit():
        episode = int(parts[-1])
        imdb_id = parts[1]
    elif len(parts) >= 3 and parts[-1].isdigit():
        episode = int(parts[-1])
        imdb_id = parts[-2] if parts[0] in {"series", "movie", "anime"} else parts[0]
    elif len(parts) >= 2 and parts[-1].isdigit():
        episode = int(parts[-1])
        imdb_id = parts[0]

    if "/" in imdb_id:
        imdb_id = imdb_id.split("/")[-1]
    return imdb_id, episode


# ─────────────────────────────────────────────
# Flask App
# ─────────────────────────────────────────────
app = Flask(__name__)


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "message": "Anime3rb Hybrid PRO Addon",
        "cookies_ready": bool(_cf_cookies),
        "cookies_age_mins": round((time.time() - _cookies_last_refresh) / 60, 1),
    })


@app.route("/manifest.json")
def manifest():
    return jsonify({
        "id": "com.anime3rb.hybrid-pro",
        "version": "3.0.0",
        "name": "🎌 Anime3rb (Hybrid PRO)",
        "description": "Fast Anime3rb streams with smart Cloudflare bypass. No heavy browser overhead.",
        "types": ["series"],
        "catalogs": [],
        "resources": ["stream"],
        "idPrefixes": ["tt", "kitsu", "anilist"],
    })


@app.route("/stream/<path:item_id>.json")
def stream(item_id):
    imdb_id, episode = parse_stremio_id(item_id)
    log.info(f"=== Stream: ID={imdb_id}, Ep={episode} ===")

    slugs = []
    if imdb_id in MANUAL_SLUGS:
        slugs.append(MANUAL_SLUGS[imdb_id])

    if imdb_id not in _title_cache:
        _title_cache[imdb_id] = get_series_name(imdb_id)
    title = _title_cache.get(imdb_id, "")
    log.info(f"[STREAM] Title: '{title}'")

    if title:
        for s in search_slugs(title):
            if s not in slugs:
                slugs.append(s)

    if not slugs:
        log.warning(f"[STREAM] No slugs found for {imdb_id}")
        return jsonify({"streams": []})

    log.info(f"[STREAM] Trying slugs: {slugs[:3]}")
    sources = []
    for slug in slugs[:3]:
        sources = get_episode_streams(slug, episode)
        if sources:
            break

    if not sources:
        return jsonify({"streams": []})

    streams = []
    for src in sources:
        label = src.get("label", "Auto")
        streams.append({
            "name": f"🎌 Anime3rb {label}",
            "url": src["url"],
            "behaviorHints": {
                "notWebReady": False,
                "bingeGroup": f"anime3rb-{label}",
            },
        })

    log.info(f"[STREAM] Returning {len(streams)} streams ✅")
    return jsonify({"streams": streams})


# ─────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────
def initialize():
    # Start async event loop
    t = threading.Thread(target=_start_async_loop, daemon=True)
    t.start()
    time.sleep(0.3)

    # Get CF cookies in background on startup
    threading.Thread(target=refresh_cf_cookies, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7860"))
    host = os.environ.get("HOST", "0.0.0.0")
    log.info(f"🚀 Starting Anime3rb Hybrid PRO on http://{host}:{port}")
    initialize()
    app.run(host=host, port=port, threaded=True)
