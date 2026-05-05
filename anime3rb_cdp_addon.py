#!/usr/bin/env python3
"""
Anime3rb Stremio addon (Python) using short-lived headless Chrome via CDP
to bypass Cloudflare, then plain requests for vid3rb video sources.
"""

import json
import os
import random
import re
import signal
from difflib import SequenceMatcher
import shutil
import socket
import string
import subprocess
import tempfile
import time
from typing import Dict, List, Optional, Tuple

import requests
import websocket
import browser_cookie3
from flask import Flask, jsonify, Response, request

# Hard-coded fallback video_sources (from live player capture)
HARDCODED_SOURCES = {
    "d-frag-eky-1": [
        {
            "src": "https://video.vid3rb.com/video/9af9ffc8-8c46-4eb3-a3b0-9db66c63030a?speed=234&token=40636d88fc12eac67a6cd9d013990d35fb195492f102b7a28df85f866e3e0f06&expires=1775942244",
            "type": "video/mp4",
            "label": "720p",
            "res": "720",
            "premium": False,
        },
        {
            "src": "https://video.vid3rb.com/video/9af9ff7-61ed-49fa-9850-e35ac36103fc?speed=103&token=fc780b0c044d520a38c1e342723399bc442ddafb925a70925b19e708409dfd28&expires=1775942244",
            "type": "video/mp4",
            "label": "480p",
            "res": "480",
            "premium": False,
        },
    ],
}
# ─────────────────────────────
# Chrome CDP helpers
# ─────────────────────────────
def _detect_chrome_path() -> Optional[str]:
    configured = os.environ.get("CHROME_PATH") or os.environ.get("GOOGLE_CHROME_BIN")
    if configured:
        return configured

    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    for executable in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(executable)
        if found:
            return found
    return None


CHROME_PATH = _detect_chrome_path()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _mk_profile_dir() -> str:
    rnd = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    path = os.path.join(tempfile.gettempdir(), f"anime3rb_cdp_{rnd}")
    os.makedirs(path, exist_ok=True)
    return path


def launch_chrome(port: int, profile_dir: str, headless: bool = True) -> subprocess.Popen:
    if not CHROME_PATH:
        raise RuntimeError("Chrome executable not found. Set CHROME_PATH or GOOGLE_CHROME_BIN.")
    args = [
        CHROME_PATH,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]
    if os.name != "nt" or _env_flag("CHROME_NO_SANDBOX"):
        args.append("--no-sandbox")
    if headless:
        args.append("--headless=new")
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


def terminate_chrome(proc: subprocess.Popen, port: Optional[int] = None, profile_dir: Optional[str] = None) -> None:
    if proc.poll() is not None:
        # The parent may exit while children stay alive on Windows; do targeted cleanup below.
        pass
    else:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=6,
                )
            else:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=3)
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    # Extra safety for Windows: kill any orphaned chrome children tied to this profile/port.
    if os.name == "nt":
        ps_filters: List[str] = []
        if profile_dir:
            needle = profile_dir.replace("'", "''")
            ps_filters.append(f"($_.CommandLine -like \"*{needle}*\")")
        if port:
            ps_filters.append(f"($_.CommandLine -like \"*--remote-debugging-port={port}*\")")
        if ps_filters:
            script = (
                "$procs = Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                f"Where-Object {{ {' -or '.join(ps_filters)} }}; "
                "foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", script],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                )
            except Exception:
                pass


def wait_devtools(port: int, timeout: float = 6.0) -> None:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=1)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("DevTools endpoint not reachable")


def open_tab(port: int, target_url: str) -> str:
    quoted = requests.utils.requote_uri(target_url)
    resp = requests.put(f"http://127.0.0.1:{port}/json/new?{quoted}", timeout=5)
    resp.raise_for_status()
    return resp.json()["webSocketDebuggerUrl"]


def fetch_html_cdp(target_url: str) -> str:
    print(f"[CDP] fetch_html_cdp -> {target_url}", flush=True)
    port = _free_port()
    profile_dir = _mk_profile_dir()
    proc = launch_chrome(port, profile_dir, headless=True)
    try:
        wait_devtools(port)
        ws_url = open_tab(port, target_url)
        ws = websocket.create_connection(ws_url, timeout=5)
        try:
            ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
            ws.send(json.dumps({"id": 2, "method": "Runtime.enable"}))
            time.sleep(1.2)
            ws.send(
                json.dumps(
                    {
                        "id": 3,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": "document.documentElement.outerHTML",
                            "returnByValue": True,
                        },
                    }
                )
            )
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == 3:
                    html = msg["result"]["result"]["value"]
                    print(f"[CDP] got HTML length {len(html)}", flush=True)
                    return html
        finally:
            ws.close()
    finally:
        terminate_chrome(proc, port=port, profile_dir=profile_dir)
        shutil.rmtree(profile_dir, ignore_errors=True)


def fetch_player_src_cdp(page_url: str) -> Optional[str]:
    """Navigate to episode page and return iframe src for vid3rb via CDP (exec JS)."""
    port = _free_port()
    profile_dir = _mk_profile_dir()
    proc = launch_chrome(port, profile_dir, headless=True)
    try:
        wait_devtools(port)
        ws_url = open_tab(port, page_url)
        ws = websocket.create_connection(ws_url, timeout=5)
        try:
            ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
            ws.send(json.dumps({"id": 2, "method": "Runtime.enable"}))
            # Poll iframe for a few seconds; some episode pages lazy-load it.
            for i in range(12):
                eval_id = 100 + i
                ws.send(
                    json.dumps(
                        {
                            "id": eval_id,
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": "(()=>{var f=document.querySelector(\"iframe[src*='vid3rb']\")||document.querySelector(\"iframe[data-src*='vid3rb']\"); if(!f) return ''; return f.src||f.getAttribute('src')||f.getAttribute('data-src')||'';})()",
                                "returnByValue": True,
                            },
                        }
                    )
                )
                deadline = time.time() + 1.2
                while time.time() < deadline:
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        break
                    msg = json.loads(raw)
                    if msg.get("id") == eval_id:
                        val = msg.get("result", {}).get("result", {}).get("value") or ""
                        if val and "vid3rb" in val:
                            return val
                        break
                time.sleep(0.5)
            try:
                ws.send(
                    json.dumps(
                        {
                            "id": 999,
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": "(()=>{const html=document.documentElement.outerHTML||''; const text=(document.body&&document.body.innerText||'').replace(/\\s+/g,' ').slice(0,180); return {title:document.title, href:location.href, len:html.length, hasVid3rb:html.includes('vid3rb'), hasIframe:!!document.querySelector('iframe'), hasCloudflare:/cloudflare|cf-|turnstile/i.test(html), text};})()",
                                "returnByValue": True,
                            },
                        }
                    )
                )
                deadline = time.time() + 1.5
                while time.time() < deadline:
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        break
                    msg = json.loads(raw)
                    if msg.get("id") == 999:
                        diag = msg.get("result", {}).get("result", {}).get("value") or {}
                        print(f"[CDP] no iframe diag={diag}", flush=True)
                        break
            except Exception as e:
                print(f"[CDP] no iframe diag failed: {e}", flush=True)
            return None
        finally:
            ws.close()
    finally:
        terminate_chrome(proc, port=port, profile_dir=profile_dir)
        shutil.rmtree(profile_dir, ignore_errors=True)


def fetch_video_sources_cdp(player_url: str, referer: str) -> List[Dict]:
    """Open player page, sniff XHR for video_sources or evaluate via JS."""
    print(f"[CDP] fetch_video_sources_cdp -> {player_url}", flush=True)
    port = _free_port()
    profile_dir = _mk_profile_dir()
    proc = launch_chrome(port, profile_dir, headless=True)
    try:
        wait_devtools(port)
        ws_url = open_tab(port, "about:blank")
        ws = websocket.create_connection(ws_url, timeout=5)
        try:
            ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
            ws.send(json.dumps({"id": 2, "method": "Runtime.enable"}))
            ws.send(json.dumps({"id": 3, "method": "Network.enable"}))
            ws.send(
                json.dumps(
                    {
                        "id": 4,
                        "method": "Network.setExtraHTTPHeaders",
                        "params": {"headers": {"Referer": referer, "User-Agent": UA}},
                    }
                )
            )
            ws.send(
                json.dumps(
                    {
                        "id": 5,
                        "method": "Network.setUserAgentOverride",
                        "params": {"userAgent": UA},
                    }
                )
            )
            ws.send(
                json.dumps(
                    {"id": 6, "method": "Page.navigate", "params": {"url": player_url}}
                )
            )

            deadline = time.time() + 10.0
            got_sources: List[Dict] = []
            interesting_ids = set()

            while time.time() < deadline and not got_sources:
                try:
                    msg = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue

                # Collect response ids that look like json
                if msg.get("method") == "Network.responseReceived":
                    res = msg.get("params", {}).get("response", {})
                    mime = res.get("mimeType", "")
                    url = res.get("url", "")
                    req_id = msg.get("params", {}).get("requestId")
                    if any(x in mime for x in ["json", "javascript"]) or "video_sources" in url or "mp4" in url:
                        interesting_ids.add(req_id)

                # Try evaluate video_sources
                if msg.get("method") == "Page.loadEventFired":
                    ws.send(
                        json.dumps(
                            {
                                "id": 7,
                                "method": "Runtime.evaluate",
                                "params": {
                                    "expression": "(()=>{try{return JSON.stringify(video_sources);}catch(e){return ''}})()",
                                    "returnByValue": True,
                                },
                            }
                        )
                    )

                if msg.get("id") == 7:
                    val = msg.get("result", {}).get("result", {}).get("value")
                    if val:
                        try:
                            got_sources = json.loads(val)
                            break
                        except Exception:
                            pass

                # Fetch bodies of interesting responses
                if interesting_ids:
                    rid = interesting_ids.pop()
                    ws.send(json.dumps({"id": 8, "method": "Network.getResponseBody", "params": {"requestId": rid}}))
                    try:
                        bodymsg = json.loads(ws.recv())
                        if bodymsg.get("id") == 8:
                            body = bodymsg.get("result", {}).get("body", "")
                            if bodymsg.get("result", {}).get("base64Encoded"):
                                import base64
                                body = base64.b64decode(body).decode(errors="ignore")
                            if "video_sources" in body:
                                try:
                                    # Try JSON parse directly first.
                                    js = json.loads(body)
                                    if isinstance(js, list):
                                        tok = js
                                    elif isinstance(js, dict) and isinstance(js.get("video_sources"), list):
                                        tok = js["video_sources"]
                                    else:
                                        tok = []
                                except Exception:
                                    tok = []
                                    m = re.search(r'video_sources"?\s*[:=]\s*(\[[\s\S]*?\])', body)
                                    if m:
                                        try:
                                            tok = json.loads(m.group(1))
                                        except Exception:
                                            tok = []
                                if tok:
                                    got_sources = tok
                                    break
                    except Exception:
                        pass

            if got_sources:
                return got_sources
            print("[CDP] video_sources not found", flush=True)
            return []
        finally:
            ws.close()
    finally:
        terminate_chrome(proc, port=port, profile_dir=profile_dir)
        shutil.rmtree(profile_dir, ignore_errors=True)
    return []


# ─────────────────────────────
# Cookie helpers
# ─────────────────────────────
def get_vid3rb_cookies() -> Dict[str, str]:
    """Extract cookies for video.vid3rb.com from Chrome (via browser_cookie3)."""
    try:
        jar = browser_cookie3.chrome(domain_name="vid3rb.com")
        return {c.name: c.value for c in jar if "vid3rb.com" in c.domain}
    except Exception as e:
        print(f"[COOKIE] failed to load Chrome cookies: {e}", flush=True)
        return {}


# ─────────────────────────────
# Anime3rb helpers
# ─────────────────────────────
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
MANUAL_SLUGS = {
    "tt3296914": "d-frag-eky",  # D-Frag!
}
ANILIST_ALIAS_CACHE: Dict[str, Tuple[List[str], Optional[str]]] = {}


def slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def _dedup_keep_order(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        if not isinstance(item, str):
            continue
        val = item.strip()
        if not val:
            continue
        key = val.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(val)
    return out


def _is_mostly_latin(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    latin = [ch for ch in letters if ord(ch) < 128]
    return (len(latin) / len(letters)) >= 0.7


def _expand_slug_transliteration_variants(slug: str) -> List[str]:
    variants = [slug]
    if "kusogee" in slug:
        variants.append(slug.replace("kusogee", "kusoge"))
    if "russiago" in slug:
        variants.append(slug.replace("russiago", "russia-go"))
    if "bijutsubu" in slug:
        variants.append(slug.replace("bijutsubu", "bijutsu-bu"))
    if "bijutsu-bu" in slug:
        variants.append(slug.replace("bijutsu-bu", "bijutsubu"))
    return _dedup_keep_order(variants)


def normalize_match_text(value: str) -> str:
    text = (value or "").lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[^a-z0-9\s-]+", " ", text)
    text = text.replace("-", " ")
    return " ".join(text.split())


def text_similarity(a: str, b: str) -> float:
    aa = normalize_match_text(a)
    bb = normalize_match_text(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 100.0
    ratio = SequenceMatcher(None, aa, bb).ratio() * 100.0
    a_tokens = set(aa.split())
    b_tokens = set(bb.split())
    if a_tokens and b_tokens:
        overlap = len(a_tokens & b_tokens) / max(len(a_tokens), len(b_tokens))
        ratio = max(ratio, overlap * 100.0)
    return ratio


def fetch_anilist_aliases(title: str) -> Tuple[List[str], Optional[str]]:
    key = title.strip().lower()
    if not key:
        return [], None
    if key in ANILIST_ALIAS_CACHE:
        return ANILIST_ALIAS_CACHE[key]

    query = """
    query ($search: String) {
      Media(type: ANIME, search: $search) {
        title { romaji english native }
        synonyms
      }
    }
    """

    aliases: List[str] = []
    best_slug: Optional[str] = None
    try:
        resp = requests.post(
            "https://graphql.anilist.co",
            json={"query": query, "variables": {"search": title}},
            headers={"Content-Type": "application/json", "User-Agent": UA},
            timeout=10,
        )
        if resp.status_code == 200:
            media = (resp.json().get("data") or {}).get("Media") or {}
            titles = media.get("title") or {}
            raw_aliases = [
                titles.get("english"),
                titles.get("romaji"),
                titles.get("native"),
                *(media.get("synonyms") or []),
            ]
            for item in raw_aliases:
                if isinstance(item, str):
                    item = " ".join(item.split()).strip()
                    if item:
                        aliases.append(item)

            aliases = _dedup_keep_order(aliases)
            aliases = [a for a in aliases if _is_mostly_latin(a)]
            best_slug = slugify(
                (titles.get("romaji") or titles.get("english") or title)
            ) or None
    except Exception as e:
        print(f"[ANILIST] aliases fetch failed: {e}", flush=True)

    ANILIST_ALIAS_CACHE[key] = (aliases, best_slug)
    return aliases, best_slug


def slug_candidates(name: str) -> List[str]:
    raw = (name or "").strip().strip("/")
    if not raw:
        return []

    # Keep pre-slugged values as-is to avoid over-normalization.
    if re.fullmatch(r"[a-z0-9-]+", raw.lower()):
        base = raw.lower()
    else:
        base = slugify(raw)

    cands: List[str] = []
    for b in _expand_slug_transliteration_variants(base):
        if not b:
            continue
        cands.append(b)
        cands.append(f"{b}-2")
        cands.append(f"{b}-season-2")
        cands.append(f"{b}-2nd-season")
        parts = b.split("-")
        if len(parts) > 3:
            cands.append("-".join(parts[:3]))
            cands.append("-".join(parts[:4]))
    return _dedup_keep_order([c for c in cands if c])


def search_anime3rb_slugs_cdp(query: str) -> List[str]:
    search_q = " ".join((query or "").split()).strip()
    if len(search_q) < 2:
        return []
    search_url = f"https://anime3rb.com/search?q={requests.utils.quote(search_q, safe='')}"
    try:
        html = fetch_html_cdp(search_url)
    except Exception as e:
        print(f"[SEARCH] CDP search failed q={search_q!r} err={e}", flush=True)
        return []

    slugs: List[str] = []
    patterns = [
        r'href=["\'](?:https?://anime3rb\.com)?/titles/([^"\'/?#]+)',
        r'https?://anime3rb\.com/titles/([^"\'/?#]+)',
        r'\\?/titles/([a-z0-9-]{3,})',
        r'"slug"\s*:\s*"([a-z0-9-]{3,})"',
    ]
    for pat in patterns:
        slugs.extend(re.findall(pat, html))
    return _dedup_keep_order([s.strip("/") for s in slugs if s])


def fetch_title_page_names_cdp(slug: str) -> List[str]:
    names: List[str] = []
    try:
        html = fetch_html_cdp(f"https://anime3rb.com/titles/{slug}")
    except Exception:
        return []

    if "ERR_CONNECTION_RESET" in html and "This site can’t be reached" in html:
        return []

    patterns = [
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)',
        r"<title>([^<]+)</title>",
        r"<h1[^>]*>([^<]+)</h1>",
        r"<h2[^>]*>([^<]+)</h2>",
    ]
    for pat in patterns:
        for m in re.findall(pat, html, flags=re.IGNORECASE):
            if isinstance(m, str):
                value = re.sub(r"\s+", " ", m).strip()
                if value:
                    names.append(value)
    return _dedup_keep_order(names)[:16]


def choose_closest_slug(
    expected_title: str,
    romaji_slug: Optional[str],
    slug_candidates_raw: List[str],
) -> List[str]:
    unique_slugs = _dedup_keep_order(slug_candidates_raw)
    if not unique_slugs:
        return []

    expected_values: List[str] = [expected_title]
    if romaji_slug:
        expected_values.append(romaji_slug.replace("-", " "))
        expected_values.append(romaji_slug)
    expected_values = _dedup_keep_order(expected_values)

    scored: List[Tuple[float, str]] = []
    for slug in unique_slugs[:24]:
        best = 0.0
        slug_text = slug.replace("-", " ")
        for e in expected_values:
            best = max(best, text_similarity(e, slug_text))

        page_names = fetch_title_page_names_cdp(slug)
        for n in page_names:
            for e in expected_values:
                best = max(best, text_similarity(e, n))

        scored.append((best, slug))

    scored.sort(key=lambda item: item[0], reverse=True)
    if scored:
        print(f"[SEARCH] best slug match: {scored[0][1]} score={scored[0][0]:.1f}", flush=True)
    return [slug for _, slug in scored]


def extract_player_url(html: str) -> Optional[str]:
    # Prefer /player/ URLs to avoid poster /video/ links
    m = re.search(r"https?://[^\"'\\s>]*vid3rb\\.com/player/[^\"'\\s<]*", html)
    if not m:
        m = re.search(r"https?://[^\"'\\s>]*vid3rb\\.com[^\"'\\s<]*", html)
    if m:
        return m.group(0).replace("&amp;", "&")
    return None


def extract_sources(player_url: str, referer: str) -> List[Dict]:
    # If player page, try plain requests first, then CDP fallback
    if "player" in player_url:
        headers = {
            "User-Agent": UA,
            "Referer": referer,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        blocks: List[str] = []
        try:
            resp = requests.get(player_url, headers=headers, timeout=15)
            if resp.status_code == 200:
                found = re.findall(r"var\s+video_sources\s*=\s*(\[[\s\S]*?\]);", resp.text)
                if found:
                    blocks = found
        except Exception as e:
            print(f"[STREAM] requests player fetch failed {e}", flush=True)

        if not blocks:
            data = fetch_video_sources_cdp(player_url, referer)
            blocks = [json.dumps(data)] if data else []
    else:
        headers = {
            "User-Agent": UA,
            "Referer": referer,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        # Direct video link? validate content-type
        if player_url.endswith(".mp4") or "/video/" in player_url:
            try:
                h = requests.head(player_url, headers=headers, timeout=10, allow_redirects=True)
                ctype = h.headers.get("Content-Type", "")
                if ctype.startswith("video/"):
                    return [{"url": player_url, "label": "720p"}]
                else:
                    print(f"[STREAM] direct url rejected (ctype={ctype}) {player_url}", flush=True)
                    return []
            except Exception as e:
                print(f"[STREAM] head failed for direct url {e}", flush=True)
                return []
        resp = requests.get(player_url, headers=headers, timeout=20)
        if resp.status_code != 200:
            return []
        blocks = re.findall(r"var\s+video_sources\s*=\s*(\[[\s\S]*?\]);", resp.text)

    sources: List[Dict] = []
    for block in blocks:
        try:
            data = json.loads(block)
            for item in data:
                src = item.get("src")
                label = item.get("label", "unknown")
                if src:
                    sources.append({"url": src, "label": label})
        except Exception:
            continue
    order = {"1080p": 0, "720p": 1, "480p": 2, "360p": 3}
    sources.sort(key=lambda x: order.get(x["label"].lower(), 99))
    return sources


def get_series_name(imdb_id: str) -> str:
    if imdb_id.startswith("kitsu:"):
        try:
            kitsu_id = imdb_id.split(":", 1)[1]
            r = requests.get(
                f"https://kitsu.io/api/edge/anime/{kitsu_id}",
                timeout=10,
                headers={"Accept": "application/vnd.api+json", "User-Agent": UA},
            )
            if r.status_code == 200:
                attrs = (r.json().get("data") or {}).get("attributes") or {}
                titles = attrs.get("titles") or {}
                name = (
                    titles.get("en")
                    or titles.get("en_us")
                    or titles.get("en_jp")
                    or titles.get("ja_jp")
                )
                if not name:
                    slug = attrs.get("slug")
                    if isinstance(slug, str) and slug.strip():
                        name = slug.replace("-", " ")
                if isinstance(name, str) and name.strip():
                    return " ".join(name.split())
        except Exception:
            pass

    if imdb_id.startswith("anilist:"):
        try:
            anilist_id = int(imdb_id.split(":", 1)[1])
            query = """
            query ($id: Int) {
              Media(id: $id, type: ANIME) {
                title { romaji english native }
              }
            }
            """
            r = requests.post(
                "https://graphql.anilist.co",
                json={"query": query, "variables": {"id": anilist_id}},
                headers={"Content-Type": "application/json", "User-Agent": UA},
                timeout=10,
            )
            if r.status_code == 200:
                titles = ((r.json().get("data") or {}).get("Media") or {}).get("title") or {}
                name = titles.get("english") or titles.get("romaji") or titles.get("native")
                if isinstance(name, str) and name.strip():
                    return " ".join(name.split())
        except Exception:
            pass

    # cinemeta endpoints
    urls = [
        f"https://v3-cinemeta.strem.io/meta/series/{imdb_id}.json",
        f"https://cinemeta-live.strem.io/meta/anime/{imdb_id}.json",
        f"https://cinemeta-live.strem.io/meta/series/{imdb_id}.json",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=8, headers={"User-Agent": UA})
            data = r.json()
            name = data.get("meta", {}).get("name")
            if name:
                return name
        except Exception:
            continue

    # IMDb fallback for better title quality when Cinemeta fails/noisy data.
    if imdb_id.startswith("tt"):
        try:
            r = requests.get(
                f"https://www.imdb.com/title/{imdb_id}/",
                timeout=10,
                headers={
                    "User-Agent": UA,
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            if r.status_code == 200:
                m = re.search(r'"name"\s*:\s*"([^"]+)"', r.text)
                if m:
                    return m.group(1).strip()
        except Exception:
            pass
    return ""


def resolve_streams(imdb_id: str, episode: int) -> Tuple[Optional[str], List[Dict]]:
    start = time.time()
    print(f"[STREAM] resolve_streams id={imdb_id} ep={episode}", flush=True)
    title = get_series_name(imdb_id)
    if not title and "-" in imdb_id and not imdb_id.startswith("tt"):
        title = imdb_id.replace("-", " ")
    aliases, alias_slug = fetch_anilist_aliases(title) if title else ([], None)

    # Phase 1 target: romaji slug first (plus manual override if defined).
    romaji_seed = MANUAL_SLUGS.get(imdb_id) or alias_slug
    if not romaji_seed and aliases:
        # Fallback: first latin alias slug
        romaji_seed = slugify(aliases[0])

    primary_slug_candidates: List[str] = []
    if romaji_seed:
        primary_slug_candidates.extend(slug_candidates(romaji_seed))
    if "-" in imdb_id and not imdb_id.startswith("tt"):
        primary_slug_candidates.extend(slug_candidates(imdb_id))
    primary_slug_candidates = _dedup_keep_order(primary_slug_candidates)

    print(
        f"[STREAM] romaji_slug={romaji_seed or 'N/A'} title={title or 'N/A'}",
        flush=True,
    )
    if primary_slug_candidates:
        print(f"[STREAM] phase1 slugs: {primary_slug_candidates[:8]}", flush=True)

    tried_slugs = set()

    def _try_slug(slug: str) -> Tuple[bool, List[Dict]]:
        ep_url = f"https://anime3rb.com/episode/{slug}/{episode}"
        for attempt in range(1, 3):
            print(f"[STREAM] trying slug={slug} attempt={attempt}/2", flush=True)
            try:
                player = fetch_player_src_cdp(ep_url)
            except Exception as e:
                print(f"[STREAM] CDP fetch failed slug={slug} err={e}", flush=True)
                player = None
            if not player:
                if attempt < 2:
                    time.sleep(0.8)
                continue
            sources_local = extract_sources(player, referer=ep_url)
            if sources_local:
                print(f"[STREAM] success slug={slug} sources={len(sources_local)} in {time.time()-start:.1f}s", flush=True)
                return True, sources_local
            if attempt < 2:
                time.sleep(0.8)
        print(f"[STREAM] no player in slug={slug}", flush=True)
        return False, []

    # Phase 1: romaji slug strategy.
    for slug in primary_slug_candidates:
        if slug in tried_slugs:
            continue
        tried_slugs.add(slug)
        ok, sources = _try_slug(slug)
        if ok:
            return slug, sources

    # Phase 2: search by full title, then choose closest slug (hybrid-like last option).
    search_queries: List[str] = []
    if title:
        search_queries.append(title)
        normalized_title = normalize_match_text(title)
        if normalized_title and normalized_title != title.lower().strip():
            search_queries.append(normalized_title)
    search_queries = _dedup_keep_order(search_queries)

    searched_slugs: List[str] = []
    if search_queries:
        print(f"[STREAM] phase2 search queries: {search_queries}", flush=True)
    for q in search_queries:
        found_slugs = search_anime3rb_slugs_cdp(q)
        if found_slugs:
            print(f"[SEARCH] q={q!r} found={len(found_slugs)}", flush=True)
            searched_slugs.extend(found_slugs)

    ranked = choose_closest_slug(title or imdb_id, romaji_seed, searched_slugs)
    if ranked:
        print(f"[SEARCH] ranked slugs: {ranked[:8]}", flush=True)
    for slug in ranked:
        if slug in tried_slugs:
            continue
        tried_slugs.add(slug)
        ok, sources = _try_slug(slug)
        if ok:
            return slug, sources

    print(f"[STREAM] failed all candidates in {time.time()-start:.1f}s", flush=True)
    return None, []


# ─────────────────────────────
# Flask Stremio addon endpoints
# ─────────────────────────────
app = Flask(__name__)


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    if request.path.startswith("/stream/"):
        # Direct video URLs can expire, so avoid clients reusing stale stream JSON.
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/manifest.json")
def manifest():
    return jsonify(
        {
            "id": "com.anime3rb.cdp",
            "version": "1.1.0",
            "name": "Anime3rb (CDP)",
            "description": "Anime3rb streams via short-lived headless Chrome bypass. Direct video links by default, optional proxy fallback.",
            "types": ["series"],
            "catalogs": [],
            "resources": ["stream"],
            "idPrefixes": ["tt", "kitsu"],
        }
    )


def parse_stremio_id(item_id: str) -> Tuple[str, int]:
    """
    Stremio stream ids typically look like:
      series:tt1234567:1:3  (type, imdb, season, episode)
      movie:tt7654321:1     (type, imdb, episode)
      tt1234567:1           (imdb, episode)
    We only need imdb_id and episode. Strip any leading type prefixes.
    """
    normalized = (item_id or "").strip()
    # Handle path-style ids coming as "series/kitsu:42297:1"
    if "/" in normalized:
        first, rest = normalized.split("/", 1)
        if first in {"series", "movie", "anime"} and rest:
            normalized = rest

    parts = normalized.split(":")
    episode = 1
    imdb_id = normalized

    # Provider ids (kitsu/anilist/myanimelist): provider:id:episode
    if len(parts) >= 2 and parts[0] in {"kitsu", "anilist", "myanimelist"}:
        provider = parts[0]
        provider_id = parts[1]
        if len(parts) >= 3 and parts[-1].isdigit():
            episode = int(parts[-1])
        return f"{provider}:{provider_id}", episode

    if len(parts) >= 4 and parts[-1].isdigit() and parts[-2].isdigit():
        # type : imdb : season : episode
        episode = int(parts[-1])
        imdb_id = parts[1]
    elif len(parts) >= 3 and parts[-1].isdigit():
        # imdb : season : episode  OR type : imdb : episode
        episode = int(parts[-1])
        imdb_id = parts[-2] if parts[0] in {"series", "movie", "anime"} else parts[0]
    elif len(parts) >= 2 and parts[-1].isdigit():
        episode = int(parts[-1])
        imdb_id = parts[0]

    # drop any leading type prefix like "series/tt..."
    if "/" in imdb_id:
        imdb_id = imdb_id.split("/")[-1]
    return imdb_id, episode


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def use_stream_proxy() -> bool:
    """
    Direct mode keeps Cloud Run out of the video path. Set STREAM_MODE=proxy
    or USE_PROXY=1 to restore the old proxy behavior when a client needs it.
    """
    mode = os.environ.get("STREAM_MODE", "direct").strip().lower()
    return mode == "proxy" or _env_flag("USE_PROXY")


def public_base_url() -> str:
    configured = os.environ.get("ADDON_BASE_URL") or os.environ.get("PUBLIC_URL")
    if configured:
        return configured.rstrip("/")

    proto = request.headers.get("X-Forwarded-Proto", request.scheme).split(",", 1)[0].strip()
    host = request.headers.get("X-Forwarded-Host", request.host).split(",", 1)[0].strip()
    return f"{proto}://{host}".rstrip("/")


def proxy_url(video_url: str, referer: str) -> str:
    return (
        f"{public_base_url()}/proxy"
        f"?url={requests.utils.quote(video_url, safe='')}"
        f"&ref={requests.utils.quote(referer, safe='')}"
    )


def stream_object(src: Dict, slug: str, episode: int) -> Optional[Dict]:
    video_url = src.get("url")
    if not video_url:
        return None

    label = src.get("label") or "auto"
    referer = f"https://anime3rb.com/episode/{slug}/{episode}"
    hdrs = {
        "Referer": referer,
        "User-Agent": UA,
    }

    if use_stream_proxy():
        url = proxy_url(video_url, referer)
        behavior_hints = {
            "notWebReady": False,
            "bingeGroup": f"anime3rb-{label}",
        }
    else:
        url = video_url
        behavior_hints = {
            "notWebReady": True,
            "proxyHeaders": {"request": hdrs},
            "bingeGroup": f"anime3rb-{label}",
        }

    return {
        "name": f"Anime3rb {label}",
        "url": url,
        "behaviorHints": behavior_hints,
        "headers": hdrs,  # Backward compatibility for clients that honor top-level headers.
        "type": "file",
    }


@app.route("/stream/<path:item_id>.json")
def stream(item_id):
    imdb_id, episode = parse_stremio_id(item_id)
    slug, sources = resolve_streams(imdb_id, episode)
    if not sources:
        return jsonify({"streams": []})
    streams = []
    for src in sources:
        item = stream_object(src, slug, episode)
        if item:
            streams.append(item)
    return jsonify({"streams": streams})


@app.route("/")
def home():
    return jsonify(
        {
            "status": "ok",
            "message": "Anime3rb CDP addon",
            "streamMode": "proxy" if use_stream_proxy() else "direct",
        }
    )


@app.route("/proxy")
def proxy():
    url = request.args.get("url")
    ref = request.args.get("ref") or "https://anime3rb.com/"
    if not url:
        return "missing url", 400

    # Forward Range if present to support streaming/seek
    range_header = request.headers.get("Range")
    headers = {"User-Agent": UA, "Referer": ref}
    if range_header:
        headers["Range"] = range_header

    try:
        r = requests.get(url, headers=headers, stream=True, timeout=25)

        def generate():
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        resp_headers = {}
        # Propagate important headers
        for k in ["Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"]:
            if k in r.headers:
                resp_headers[k] = r.headers[k]

        status = r.status_code  # 200 or 206 typically
        return Response(generate(), status=status, headers=resp_headers)
    except Exception as e:
        return f"proxy error: {e}", 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8088"))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port, threaded=True)
