#!/usr/bin/env python3
"""
Stream health checker.

WORKING  = a real media segment downloads (master -> media playlist -> first segment).
STABLE   = the playlist is still advancing after a wait (stream is really live).

Every request has a hard time limit, and every phase has a global time budget, so one
endless or trickling stream can never hang the run (this caused a 1-hour hang before).
"""

import re
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _first_uri(text):
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def _get_text(url, timeout, max_bytes=300_000, deadline=15):
    """Read at most max_bytes / deadline seconds, even if the server streams forever."""
    start = time.monotonic()
    with requests.get(url, headers=UA, timeout=timeout, stream=True, allow_redirects=True) as r:
        if r.status_code != 200:
            return None, r.url
        buf = b""
        for chunk in r.iter_content(chunk_size=8192):
            buf += chunk
            if len(buf) >= max_bytes or time.monotonic() - start > deadline:
                break
        return buf.decode("utf-8", "ignore"), r.url


def _segment_ok(url, timeout, deadline=15):
    start = time.monotonic()
    with requests.get(url, headers=UA, timeout=timeout, stream=True) as r:
        if r.status_code not in (200, 206):
            return False
        ctype = r.headers.get("Content-Type", "").lower()
        chunk = next(r.iter_content(chunk_size=1024), b"")
        if time.monotonic() - start > deadline:
            return False
        return bool(chunk) and "text/html" not in ctype and b"<html" not in chunk[:200].lower()


def check_stream(url, timeout=10):
    try:
        text, final = _get_text(url, timeout)
        if text is None:
            return False
        if not text.lstrip().startswith("#EXTM3U"):
            # not a playlist: accept only a direct video stream
            with requests.get(url, headers=UA, timeout=timeout, stream=True) as r:
                ct = r.headers.get("Content-Type", "").lower()
                return r.status_code == 200 and (ct.startswith("video/") or "mp2t" in ct)

        if "#EXT-X-STREAM-INF" in text:                  # master playlist
            variant = _first_uri(text)
            if not variant:
                return False
            final = urljoin(final, variant)
            text, final = _get_text(final, timeout)
            if text is None or not text.lstrip().startswith("#EXTM3U"):
                return False

        if "#EXT-X-ENDLIST" in text:                     # VOD, not live
            return False
        seg = _first_uri(text)
        if not seg:
            return False
        return _segment_ok(urljoin(final, seg), timeout)
    except Exception:
        return False


# ─── Stability: still live after a wait? ─────────────────────────────────────
def _media_playlist(url, timeout):
    text, final = _get_text(url, timeout)
    if text is None or not text.lstrip().startswith("#EXTM3U"):
        return None
    if "#EXT-X-STREAM-INF" in text:
        variant = _first_uri(text)
        if not variant:
            return None
        text, final = _get_text(urljoin(final, variant), timeout)
        if text is None or not text.lstrip().startswith("#EXTM3U"):
            return None
    return text, final


def _last_segment(text, final):
    uris = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    return urljoin(final, uris[-1]) if uris else None


def _media_seq(text):
    m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text)
    return int(m.group(1)) if m else None


def check_stable(url, wait_s=70, timeout=10):
    try:
        a = _media_playlist(url, timeout)
        if not a:
            return False
        time.sleep(wait_s)
        b = _media_playlist(url, timeout)
        if not b:
            return False
        sa, sb = _media_seq(a[0]), _media_seq(b[0])
        la, lb = _last_segment(*a), _last_segment(*b)
        advanced = (sa is not None and sb is not None and sb > sa) or (la != lb)
        return bool(advanced and lb and _segment_ok(lb, timeout))
    except Exception:
        return False


# ─── Pool runner with a global time budget ───────────────────────────────────
def _run(fn, items, workers, budget, label):
    """Run fn(item) in parallel. Anything unfinished when `budget` seconds pass counts as False."""
    done_count, lock = [0], threading.Lock()

    def wrapped(item):
        res = fn(item)
        with lock:
            done_count[0] += 1
            if done_count[0] % 100 == 0:
                logger.info(f"  {label}: {done_count[0]}/{len(items)} done")
        return res

    ex = ThreadPoolExecutor(max_workers=workers)
    futures = [ex.submit(wrapped, it) for it in items]
    done, not_done = wait(futures, timeout=budget)
    if not_done:
        logger.warning(f"  {label}: time budget hit, {len(not_done)} streams treated as failed")
    results = [f.result() if f in done and not f.exception() else False for f in futures]
    ex.shutdown(wait=False, cancel_futures=True)
    return results


def filter_working(channels, workers=40, timeout=10, budget=1200):
    logger.info(f"Checking {len(channels)} streams (max {budget // 60} min)...")
    res = _run(lambda c: check_stream(c["stream_url"], timeout), channels, workers, budget, "health")
    alive = []
    for ch, ok in zip(channels, res):
        ch["is_online"] = ok
        if ok:
            alive.append(ch)
    logger.info(f"{len(alive)}/{len(channels)} streams are working")
    return alive


def filter_stable(channels, wait=70, workers=100, timeout=10, budget=900):
    logger.info(f"Stability check on {len(channels)} streams (~{wait}s wait)...")
    res = _run(lambda c: check_stable(c["stream_url"], wait, timeout), channels, workers, budget, "stable")
    stable = [c for c, ok in zip(channels, res) if ok]
    logger.info(f"{len(stable)}/{len(channels)} streams are still live after {wait}s")
    return stable
