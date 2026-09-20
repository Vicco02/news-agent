#!/usr/bin/env python3
"""Verifica feeds RSS candidatos: python probe_feeds.py URL [URL...]
Para cada URL imprime el status HTTP, si feedparser encuentra entradas, los
primeros titulares y, si es una página HTML, los links que parecen RSS."""
import re
import sys

import feedparser
import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

for url in sys.argv[1:]:
    print(f"\n=== {url}")
    try:
        r = requests.get(url, headers=UA, timeout=20)
    except Exception as e:
        print(f"  ERROR: {e}")
        continue
    print(f"  HTTP {r.status_code} | {r.headers.get('content-type', '?')} | {len(r.content)} bytes")
    feed = feedparser.parse(r.content)
    if feed.entries:
        print(f"  RSS OK: {len(feed.entries)} entradas (bozo={feed.bozo})")
        for e in feed.entries[:6]:
            print(f"   * {(e.get('published') or e.get('updated') or '?')[:25]} | {e.get('title', '')[:100]}")
    else:
        links = sorted(set(re.findall(r'href="([^"]*(?:rss|feed)[^"]*)"', r.text, re.I)))
        print(f"  Sin entradas RSS. Links con rss/feed en la página ({len(links)}):")
        for l in links[:40]:
            print(f"   - {l}")
