#!/usr/bin/env python3
"""check_coverage.py - does the parser capture every listing card on the cached
Property24 pages?

Run from the project root (or from anywhere):
    python crawler/check_coverage.py                 # uses data/raw/rentals.db
    python crawler/check_coverage.py other.db        # or an explicit database

Reads only the local cache. Makes no network requests.
"""
import sqlite3
import sys
import zlib
from collections import Counter
from pathlib import Path

import rental_scraper as r

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "raw" / "rentals.db"

if len(sys.argv) > 1:
    db_path = Path(sys.argv[1])
elif DEFAULT_DB.exists():
    db_path = DEFAULT_DB
else:                      # fallback for a crawler run from the project root
    db_path = Path("rentals.db")

db = sqlite3.connect(db_path)
site = r.SITES["property24"]
rows = db.execute(
    "SELECT url, body FROM pages WHERE url LIKE '%property24%'").fetchall()

per_page_cards: Counter = Counter()   # cards per page -> number of pages
short: list[tuple[str, int, int]] = []
cards_total = parsed_total = 0
all_ids: set[str] = set()

for url, body in rows:
    html = zlib.decompress(body).decode("utf-8", "replace")
    soup = r.BeautifulSoup(html, r.PARSER)
    ids = {t["data-listing-number"] for t in soup.select("[data-listing-number]")}
    _, recs = r.extract(html, url, site, "?", "?")
    per_page_cards[len(ids)] += 1
    cards_total += len(ids)
    parsed_total += len(recs)
    all_ids |= ids
    if len(recs) < len(ids):
        short.append((url, len(ids), len(recs)))

print(f"database: {db_path}")
print(f"pages cached: {len(rows)}")
print(f"cards on pages: {cards_total} | parsed: {parsed_total} | "
      f"missed: {cards_total - parsed_total}")
print(f"distinct listing ids across all pages: {len(all_ids)} "
      f"(so {cards_total - len(all_ids)} cards were repeats across pages)")
print("cards per page (cards: number of pages):",
      dict(sorted(per_page_cards.items())))
print(f"pages where the parser missed cards: {len(short)}")
for url, cards, got in short[:5]:
    print(f"  {cards} cards, parsed {got}: {url}")
