#!/usr/bin/env python3
"""
India IPTV Playlist Generator (SS IPTV ready)

  python main.py --repo youruser/yourrepo
  python main.py --source iptvorg          # only the maintained iptv-org list
  python main.py --pages 1 --debug-html    # quick test / save page for debugging
"""

import argparse
import re
import sys
import logging
from collections import Counter, defaultdict
from pathlib import Path

for d in ("logs", "output", "docs", "scripts"):
    Path(d).mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler("logs/scraper.log", mode="a", encoding="utf-8")],
)
logger = logging.getLogger("main")


def norm(name):
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def keep_best(channels, alternates=1):
    """Keep the best `alternates` working streams per channel name (highest quality first)."""
    groups = defaultdict(list)
    for c in channels:
        groups[norm(c["name"])].append(c)
    out = []
    for group in groups.values():
        group.sort(key=lambda c: (c.get("source") != "iptv-org", -c.get("quality", 0)))
        for i, c in enumerate(group[:alternates]):
            if i > 0:
                c = dict(c, name=f"{c['name']} (alt {i + 1})")
            out.append(c)
    return out


def main():
    p = argparse.ArgumentParser(description="India IPTV Playlist Generator")
    p.add_argument("--source", choices=["both", "iptvcat", "iptvorg"], default="both")
    p.add_argument("--pages", type=int, default=10, help="Max IPTVCat pages (default 10; stops early)")
    p.add_argument("--proxy", action="store_true")
    p.add_argument("--all", action="store_true", help="Also include '[Not 24/7]' channels")
    p.add_argument("--no-check", action="store_true", help="Skip health check (NOT recommended)")
    p.add_argument("--workers", type=int, default=40)
    p.add_argument("--no-stable", action="store_true", help="Skip the 'still live after a minute' check")
    p.add_argument("--stable-wait", type=int, default=70, help="Seconds between the two stability samples")
    p.add_argument("--alternates", type=int, default=1, help="Working streams to keep per channel name")
    p.add_argument("--no-split", action="store_true")
    p.add_argument("--debug-html", action="store_true", help="Save IPTVCat page 1 to logs/page1.html")
    p.add_argument("--repo", help="GitHub 'user/repo' for README links")
    p.add_argument("--no-cf-worker", action="store_true")
    args = p.parse_args()

    from scraper import IPTVCatScraper
    from generator import PlaylistGenerator
    from checker import filter_working, filter_stable
    from geobypass import apply_proxy_to_channels, generate_cloudflare_worker, generate_streamlink_script

    if not args.no_cf_worker:
        generate_cloudflare_worker()
        generate_streamlink_script(output_path="scripts/play_channel.sh")

    sc = IPTVCatScraper(use_proxy=args.proxy, debug_html=args.debug_html)
    channels = []
    if args.source in ("both", "iptvorg"):
        channels += sc.scrape_iptvorg()
    if args.source in ("both", "iptvcat"):
        channels += sc.scrape_iptvcat(max_pages=args.pages, include_not_247=args.all)

    # drop exact duplicate stream URLs
    seen, uniq = set(), []
    for c in channels:
        if c["stream_url"] not in seen:
            seen.add(c["stream_url"])
            uniq.append(c)
    channels = uniq
    if not channels:
        logger.error("No channels found. See logs/scraper.log (try --debug-html).")
        sys.exit(1)
    logger.info(f"{len(channels)} candidate streams")

    if not args.no_check:
        channels = filter_working(channels, workers=args.workers)
        if not channels:
            logger.error("No streams passed the health check (check your internet / try again later).")
            sys.exit(1)

        if not args.no_stable:
            channels = filter_stable(channels, wait=args.stable_wait)
            if not channels:
                logger.error("No streams stayed live. Try again later or use --no-stable.")
                sys.exit(1)

    channels = keep_best(channels, args.alternates)
    channels = apply_proxy_to_channels(channels)

    gen = PlaylistGenerator(output_dir="output", repo=args.repo)
    gen.generate_m3u(channels, "india_iptv.m3u")
    if not args.no_split:
        gen.generate_m3u_by_category(channels)
    gen.generate_json_index(channels)
    gen.generate_readme(channels)

    logger.info("=" * 50)
    logger.info(f"WORKING channels in playlist: {len(channels)}")
    for cat, n in Counter(c.get("category", "General") for c in channels).most_common():
        logger.info(f"  {cat:<26}{n:>4}")
    logger.info("Done -> output/india_iptv.m3u")


if __name__ == "__main__":
    main()
