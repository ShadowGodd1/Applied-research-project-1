#!/usr/bin/env python3
"""
rental_scraper.py - resilient async crawler for Nairobi rental listings.
School project, CRISP-DM phase 2 (Data Understanding).

Install:   pip install httpx beautifulsoup4 lxml
Commands:
  python crawler/rental_scraper.py selftest                  # offline tests, run first
  python crawler/rental_scraper.py probe URL --site NAME     # diagnose parsing on one live page
  python crawler/rental_scraper.py crawl --test              # tiny live run
  python crawler/rental_scraper.py crawl --sites property24
  python crawler/rental_scraper.py parse FOLDER              # parse HTML pages saved from your browser
  python crawler/rental_scraper.py export --out rentals.csv  # dump to CSV for cleaning (data/raw/rentals.csv)

Design:
  * asyncio + httpx with AIMD adaptive concurrency: speeds up while the site is
    happy, halves on 429/5xx, honours Retry-After, gives up fast on 403 (a refusal).
  * SQLite store: compressed raw-HTML cache (free resume), deduped listings, and a
    daily price_history table so weekly re-runs give you a time dimension.
  * Parsing: JSON-LD for the core fields, enriched from the listing's own card
    (bedrooms, baths, toilets, parking, location line, property kind). Falls back
    to CSS selectors, then to heuristic card detection.
  * Phone numbers and emails are masked at ingest; contacts are never stored.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.util
import json
import logging
import random
import re
import signal
import sqlite3
import time
import zlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

log = logging.getLogger("rentals")
PARSER = "lxml" if importlib.util.find_spec("lxml") else "html.parser"

# ───────────────────────────── configuration ─────────────────────────────


@dataclass(frozen=True)
class Site:
    name: str
    url: str                              # template using {ptype} and {loc}
    page_param: str = "page"
    card_selectors: tuple[str, ...] = ()  # tried in order; heuristic if none match
    ptypes: tuple[str, ...] = ()          # per-site defaults (override the globals)
    locations: tuple[str, ...] = ()


SITES: dict[str, Site] = {
    # Blocks automated requests (HTTP 403). Kept for the `parse` command on saved pages.
    "buyrentkenya": Site(
        "buyrentkenya",
        "https://www.buyrentkenya.com/{ptype}-for-rent/{loc}",
        card_selectors=("div.listing-card",),
    ),
    # Property24 Kenya: search pages are /<type>-to-rent-in-<place>-<code>, 20 results per page,
    # pagination is ?Page=N (capital P). The code differs per type (c1890 vs p95), so each ptype
    # below is the FULL path slug and {loc} is unused (it is only a placeholder so --locations
    # has no effect for this site). To add a suburb, append its slug from the site's own URL.
    "property24": Site(
        "property24",
        "https://www.property24.co.ke/{ptype}",
        page_param="Page",
        card_selectors=("div.js_listingTile",),   # one per listing; heuristic detection dropped cards whose text had 2+ prices
        ptypes=("apartments-flats-to-rent-in-nairobi-c1890", "houses-to-rent-in-nairobi-p95"),
        locations=("nairobi",),
    ),
}

# Fallbacks for sites that do not define their own.
LOCATIONS = ["nairobi/kilimani", "nairobi/westlands", "nairobi/kileleshwa", "nairobi/lavington",
             "nairobi/karen", "nairobi/parklands", "nairobi/kasarani", "nairobi/roysambu"]
PROPERTY_TYPES = ["flats-apartments", "houses"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


@dataclass
class Settings:
    db: str = "data/raw/rentals.db"
    concurrency: int = 6        # starting parallelism (adapts between 1 and 2x this)
    delay: float = 0.0          # optional pause after each successful request
    max_pages: int = 500
    timeout: float = 25.0
    retries: int = 6
    backoff_base: float = 5.0
    refresh: bool = False       # ignore cache (use for weekly re-scrapes)


@dataclass
class Stats:
    pages: int = 0
    cached: int = 0
    new: int = 0
    throttled: int = 0
    failed: int = 0
    by_strategy: Counter = field(default_factory=Counter)


# ───────────────────────────── storage ─────────────────────────────

LISTING_COLS = ("source", "title", "price_kes", "period", "bedrooms", "bathrooms", "toilets",
                "parking", "size_sqm", "kind", "ptype", "neighborhood", "location_text", "raw_text")
SCHEMA = """
CREATE TABLE IF NOT EXISTS pages(
    url TEXT PRIMARY KEY, body BLOB NOT NULL, fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS listings(
    url TEXT PRIMARY KEY, source TEXT, title TEXT, price_kes INTEGER, period TEXT,
    bedrooms INTEGER, bathrooms INTEGER, toilets INTEGER, parking INTEGER, size_sqm REAL,
    kind TEXT, ptype TEXT, neighborhood TEXT, location_text TEXT, raw_text TEXT,
    first_seen TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS price_history(
    url TEXT, seen_on TEXT, price_kes INTEGER, PRIMARY KEY(url, seen_on));
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        for col, typ in (("toilets", "INTEGER"), ("parking", "INTEGER"), ("kind", "TEXT"),
                         ("location_text", "TEXT")):           # migrate databases from older versions
            try:
                self.db.execute(f"ALTER TABLE listings ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        self.db.execute("PRAGMA journal_mode=WAL")

    def get_page(self, url: str) -> str | None:
        row = self.db.execute("SELECT body FROM pages WHERE url=?", (url,)).fetchone()
        return zlib.decompress(row[0]).decode("utf-8", "replace") if row else None

    def put_page(self, url: str, html: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?)",
                        (url, zlib.compress(html.encode("utf-8")), _now()))
        self.db.commit()

    def upsert(self, r: dict[str, Any]) -> bool:
        """Insert or update a listing. Returns True if it was new."""
        t = _now()
        exists = self.db.execute("SELECT 1 FROM listings WHERE url=?", (r["url"],)).fetchone()
        vals = [r.get(c) for c in LISTING_COLS]
        if exists:
            sets = ",".join(f"{c}=?" for c in LISTING_COLS)
            self.db.execute(f"UPDATE listings SET {sets}, last_seen=? WHERE url=?", (*vals, t, r["url"]))
        else:
            self.db.execute(
                f"INSERT INTO listings(url,{','.join(LISTING_COLS)},first_seen,last_seen) "
                f"VALUES (?,{','.join('?' * len(LISTING_COLS))},?,?)", (r["url"], *vals, t, t))
        self.db.execute("INSERT OR IGNORE INTO price_history VALUES (?,?,?)",
                        (r["url"], t[:10], r.get("price_kes")))
        self.db.commit()
        return not exists

    def export_csv(self, out: str) -> int:
        cur = self.db.execute(
            "SELECT l.*, (SELECT COUNT(*) FROM price_history h WHERE h.url=l.url) AS n_obs "
            "FROM listings l ORDER BY neighborhood, price_kes")
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([d[0] for d in cur.description])
            n = 0
            for row in cur:
                w.writerow(row)
                n += 1
        return n


# ───────────────────────────── fetching ─────────────────────────────


class AdaptiveLimiter:
    """AIMD concurrency control: +1 after 20 clean successes, halve on trouble."""

    def __init__(self, start: int, lo: int = 1, hi: int | None = None):
        self.limit, self.lo, self.hi = start, lo, hi or start * 2
        self.active, self._ok = 0, 0
        self._cond = asyncio.Condition()

    async def __aenter__(self):
        async with self._cond:
            await self._cond.wait_for(lambda: self.active < self.limit)
            self.active += 1

    async def __aexit__(self, *exc):
        async with self._cond:
            self.active -= 1
            self._cond.notify_all()

    def success(self) -> None:
        self._ok += 1
        if self._ok >= 20 and self.limit < self.hi:
            self.limit, self._ok = self.limit + 1, 0

    def throttle(self) -> None:
        self.limit, self._ok = max(self.lo, self.limit // 2), 0


class Fetcher:
    def __init__(self, cfg: Settings, store: Store, stats: Stats,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.cfg, self.store, self.stats = cfg, store, stats
        self.limiter = AdaptiveLimiter(cfg.concurrency)
        self._pause_until = 0.0
        self.client = httpx.AsyncClient(
            timeout=cfg.timeout, follow_redirects=True, transport=transport,
            headers={"Accept-Language": "en-US,en;q=0.9", "Accept": "text/html,application/xhtml+xml"})

    async def aclose(self) -> None:
        await self.client.aclose()

    async def get(self, url: str) -> str | None:
        if not self.cfg.refresh:
            cached = self.store.get_page(url)
            if cached is not None:
                self.stats.cached += 1
                return cached
        blocked = 0
        for attempt in range(self.cfg.retries):
            if (wait := self._pause_until - time.monotonic()) > 0:
                await asyncio.sleep(wait)
            try:
                async with self.limiter:
                    r = await self.client.get(url, headers={"User-Agent": random.choice(USER_AGENTS)})
            except httpx.HTTPError as e:
                log.warning("network error %s on %s", type(e).__name__, url)
                self.limiter.throttle()
                await asyncio.sleep(self._backoff(attempt))
                continue
            if r.status_code == 200:
                self.limiter.success()
                self.stats.pages += 1
                self.store.put_page(url, r.text)
                if self.cfg.delay:
                    await asyncio.sleep(self.cfg.delay)
                return r.text
            if r.status_code in (404, 410):
                return None
            if r.status_code == 403:
                blocked += 1
                if blocked >= 2:   # a 403 is a refusal, not congestion: waiting will not fix it
                    log.error("403 Forbidden on %s: the site is refusing automated clients. Skipping "
                              "(try another site, or use `parse` on pages saved from your browser).", url)
                    self.stats.failed += 1
                    return None
            self.stats.throttled += 1
            self.limiter.throttle()
            wait = self._retry_after(r) or self._backoff(attempt)
            log.warning("HTTP %s on %s: backing off %.1fs (concurrency now %d)",
                        r.status_code, url, wait, self.limiter.limit)
            if r.status_code in (429, 403):
                self._pause_until = time.monotonic() + wait
            await asyncio.sleep(wait)
        self.stats.failed += 1
        log.error("giving up on %s", url)
        return None

    def _backoff(self, attempt: int) -> float:
        return self.cfg.backoff_base * 2 ** attempt * random.uniform(0.8, 1.2)

    @staticmethod
    def _retry_after(r: httpx.Response) -> float | None:
        try:
            return max(0.0, float(r.headers["Retry-After"]))
        except (KeyError, ValueError):
            return None


# ───────────────────────────── parsing ─────────────────────────────

# Thousands may be separated by comma ("90,000") or a space ("750 000", as Property24 writes it).
PRICE_RE = re.compile(r"(?:KES|KSh|Ksh|Kshs?\.?)\s*(\d{1,3}(?:[,  ]\d{3})+|\d+)", re.I)
UNIT_AFTER = re.compile(r"\s*(?:/|per|a)\s*(mo(?:nth)?|day|night|week|wk|yr|year|annum)\b", re.I)
BED_RE = re.compile(r"(\d+)[\s-]*(?:beds?|bedrooms?|br)\b", re.I)
BATH_RE = re.compile(r"(\d+)\s*(?:baths?|bathrooms?)\b", re.I)
TOILET_RE = re.compile(r"(\d+)\s*toilets?\b", re.I)
PARK_RE = re.compile(r"(\d+)\s*parking\b", re.I)
SQM_RE = re.compile(r"([\d][\d,.]*)\s*(?:sqm|sq\.?\s*m|m²)", re.I)
STAT_LINE_RE = re.compile(r"(?:\d+[\s,.]*(?:beds?|baths?|toilets?|parking|sqm|m²)\s*)+", re.I)
LOC_RE = re.compile(r"^(?P<loc>[^.!?:;|]{3,100},\s*(?:Nairobi|Kiambu|Kajiado|Machakos|Mombasa|Nakuru|Kisumu))"
                    r"(?:\s+Added\b.*)?$", re.I)
KIND_RE = re.compile(r"^(?P<kind>[A-Za-z][A-Za-z /()-]{2,40}) for rent$", re.I)
PHONE_RE = re.compile(r"(?:\+?254|\b0)[\s-]?[17]\d{2}[\s-]?\d{3}[\s-]?\d{3}\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
BAD_HREF = ("tel:", "mailto:", "javascript:", "#", "whatsapp", "wa.me", "sms:")


def scrub(text: str) -> str:
    return EMAIL_RE.sub("[email]", PHONE_RE.sub("[phone]", text))


def _int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def period_of(text: str) -> str:
    """Rent period of the FIRST price only (descriptions often quote daily rates too)."""
    first = PRICE_RE.search(text)
    m = UNIT_AFTER.match(text, first.end()) if first else None
    unit = m.group(1).lower() if m else "mo"
    return ("day" if unit in ("day", "night") else "week" if unit in ("week", "wk")
            else "year" if unit in ("yr", "year", "annum") else "month")


def parse_fields(text: str) -> dict[str, Any]:
    def num(rx: re.Pattern, cast=int):
        m = rx.search(text)
        try:
            return cast(re.sub(r"[,  ]", "", m.group(1))) if m else None
        except ValueError:
            return None

    beds = num(BED_RE)
    if beds is None and re.search(r"bedsitter|studio|single room", text, re.I):
        beds = 0
    return {"price_kes": num(PRICE_RE), "period": period_of(text), "bedrooms": beds,
            "bathrooms": num(BATH_RE), "toilets": num(TOILET_RE), "parking": num(PARK_RE),
            "size_sqm": num(SQM_RE, float)}


def _lines(node: Tag) -> list[str]:
    return [re.sub(r"\s+", " ", ln).strip() for ln in node.get_text("\n", strip=True).split("\n") if ln.strip()]


def structured_stats(node: Tag) -> str:
    """Lines that are purely '3 beds 2 baths ...': more reliable than free-text descriptions."""
    return " ".join(ln for ln in _lines(node) if STAT_LINE_RE.fullmatch(ln))


ICON_TITLES = {"bedrooms": "bedrooms", "bathrooms": "bathrooms", "parking spaces": "parking",
               "floor size": "size_sqm"}


def icon_stats(node: Tag) -> dict[str, Any]:
    """Property24 icon row: <span title="Bathrooms"><svg/><span>5</span></span>.
    Labelled by the title attribute, so no guessing from position."""
    out: dict[str, Any] = {}
    for el in node.find_all(attrs={"title": True}):
        key = ICON_TITLES.get(str(el["title"]).strip().lower())
        m = re.search(r"\d[\d,.]*", el.get_text(" ", strip=True)) if key and key not in out else None
        if m:
            try:
                n = float(m.group().replace(",", ""))
            except ValueError:
                continue
            out[key] = n if key == "size_sqm" else int(n)
    return out


def fields_for(node: Tag, text: str) -> dict[str, Any]:
    base = parse_fields(text)
    if stats := structured_stats(node):
        for k, v in parse_fields(stats).items():
            if k not in ("price_kes", "period") and v is not None:
                base[k] = v
    base.update(icon_stats(node))
    return base


def parse_location(node: Tag) -> str | None:
    for ln in reversed(_lines(node)):
        if m := LOC_RE.match(ln):
            return m.group("loc").strip()
    return None


def parse_kind(node: Tag) -> str | None:
    for ln in _lines(node):
        if m := KIND_RE.match(ln):
            return m.group("kind").lower()
    return None


def area_from_location(loc: str | None) -> str | None:
    """'Yaya, Kilimani, Nairobi' -> 'kilimani' (the part just before the county)."""
    parts = [p.strip() for p in (loc or "").split(",") if p.strip()]
    return parts[-2].lower() if len(parts) >= 2 else None


def area_from_url(url: str) -> str | None:
    """Property24 slug fallback: '.../4-bedroom-apartment-flat-to-rent-in-kitisuru-117698921' -> 'kitisuru'."""
    m = re.search(r"-to-rent-in-([a-z0-9-]+?)-\d{5,}/?$", url, re.I)
    return m.group(1).replace("-", " ").lower() if m else None


def kind_from_url(url: str) -> str | None:
    """'.../4-bedroom-apartment-flat-to-rent-in-...' -> 'apartment flat'."""
    m = re.search(r"/(?:\d+-bedrooms?-)?([a-z-]+?)-to-rent-in-", url, re.I)
    return m.group(1).replace("-", " ").lower() if m else None


def pick_link(card: Tag) -> Tag | None:
    links = [a for a in card.find_all("a", href=True) if not a["href"].lower().startswith(BAD_HREF)]
    return next((a for a in links if re.search(r"\d", a["href"])), links[0] if links else None)


def microdata_price(card: Tag) -> int | None:
    """<meta itemprop="price" content="750000.00"> is the exact rent; descriptions may quote other amounts."""
    m = card.find("meta", attrs={"itemprop": "price"})
    return _int(m.get("content")) if m else None


def card_to_record(card: Tag, page_url: str) -> dict[str, Any] | None:
    text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
    link = pick_link(card)
    fields = fields_for(card, text)
    if (exact := microdata_price(card)) is not None:
        fields["price_kes"] = exact
    if not link or fields["price_kes"] is None:
        return None
    head = card.find(["h1", "h2", "h3", "h4"])
    title = head.get_text(" ", strip=True) if head else (link.get("title") or link.get_text(" ", strip=True))
    return {"url": urljoin(page_url, link["href"]).split("#")[0], "title": scrub(title)[:200], **fields,
            "location_text": parse_location(card), "kind": parse_kind(card), "raw_text": scrub(text)[:1500]}


def heuristic_cards(soup: BeautifulSoup) -> list[Tag]:
    """Outermost small element containing exactly one price and a link."""
    ok: dict[int, Tag] = {}
    for el in soup.find_all(["article", "li", "div", "section"]):
        text = el.get_text(" ", strip=True)
        if len(text) <= 1500 and len(PRICE_RE.findall(text)) == 1 and pick_link(el):
            ok[id(el)] = el
    return [el for el in ok.values() if id(el.parent) not in ok]


def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def from_jsonld(soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        for n in _walk(data):
            offers = n.get("offers") if isinstance(n.get("offers"), dict) else {}
            price = offers.get("price", n.get("price"))
            if not (n.get("url") and price):
                continue
            try:
                price_i = int(float(str(price).replace(",", "")))
            except ValueError:
                continue
            out.append({"url": urljoin(page_url, n["url"]).split("#")[0],
                        "title": scrub(str(n.get("name", "")))[:200], "price_kes": price_i, "period": "month",
                        "bedrooms": _int(n.get("numberOfBedrooms") or n.get("numberOfRooms")),
                        "bathrooms": _int(n.get("numberOfBathroomsTotal")),
                        "raw_text": scrub(str(n.get("description", "")))[:1500]})
    return out


def cards_by_listing_url(soup: BeautifulSoup, page_url: str, urls: set[str]) -> dict[str, Tag]:
    """For each listing URL, climb from its link to the largest ancestor holding no other listing."""
    def norm(a: Tag) -> str:
        return urljoin(page_url, a["href"]).split("#")[0]

    anchors: dict[str, Tag] = {}
    for a in soup.find_all("a", href=True):
        u = norm(a)
        if u in urls:
            anchors.setdefault(u, a)
    out: dict[str, Tag] = {}
    for u, a in anchors.items():
        node = a
        while node.parent is not None and node.parent.name not in ("body", "html", "[document]"):
            inner = {norm(x) for x in node.parent.find_all("a", href=True)} & urls
            if inner - {u}:
                break
            node = node.parent
        out[u] = node
    return out


def enrich_from_cards(soup: BeautifulSoup, page_url: str, recs: list[dict[str, Any]]) -> None:
    nodes = cards_by_listing_url(soup, page_url, {r["url"] for r in recs})
    for r in recs:
        node = nodes.get(r["url"])
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)) if node else ""
        slug = r["url"].rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
        sources = ([fields_for(node, text)] if node else []) + [parse_fields(f"{r['title']} {slug}")]
        for k in ("bedrooms", "bathrooms", "toilets", "parking", "size_sqm"):
            if r.get(k) is None:
                r[k] = next((s[k] for s in sources if s.get(k) is not None), None)
        if node:
            r["period"] = period_of(text)
            r["location_text"] = parse_location(node)
            r["kind"] = parse_kind(node)
            r["raw_text"] = r.get("raw_text") or scrub(text)[:1500]


def extract(html: str, page_url: str, site: Site, ptype: str, loc: str) -> tuple[str, list[dict]]:
    soup = BeautifulSoup(html, PARSER)
    recs, strategy = from_jsonld(soup, page_url), "jsonld"
    if recs:
        enrich_from_cards(soup, page_url, recs)
    else:
        cards: list[Tag] = []
        for sel in site.card_selectors:
            if cards := soup.select(sel):
                break
        strategy = "css"
        if not cards:
            cards, strategy = heuristic_cards(soup), "heuristic"
        recs = [r for c in cards if (r := card_to_record(c, page_url))]
    slug_area = loc.split("?")[0].split("/")[-1].replace("-", " ")
    unique: dict[str, dict] = {}
    for r in recs:
        r["source"], r["ptype"] = site.name, ptype
        r["neighborhood"] = area_from_location(r.get("location_text")) or area_from_url(r["url"]) or slug_area
        r["kind"] = r.get("kind") or kind_from_url(r["url"])
        unique.setdefault(r["url"], r)
    return strategy, list(unique.values())


# ───────────────────────────── crawling ─────────────────────────────


async def crawl_combo(fetcher: Fetcher, store: Store, stats: Stats, stop: asyncio.Event,
                      site: Site, ptype: str, loc: str, max_pages: int) -> int:
    base, seen, total, page = site.url.format(ptype=ptype, loc=loc), set(), 0, 0
    for page in range(1, max_pages + 1):
        if stop.is_set():
            break
        sep = "&" if "?" in base else "?"          # locations may carry filters, e.g. nairobi?bedrooms=2
        url = base if page == 1 else f"{base}{sep}{site.page_param}={page}"
        html = await fetcher.get(url)
        if not html:
            if page > 1:
                log.warning("page %d unavailable, stopping this search: %s", page, url)
            break
        strategy, recs = extract(html, url, site, ptype, loc)
        urls = {r["url"] for r in recs}
        if not recs or urls <= seen:      # empty page, or site repeating its last page
            break
        seen |= urls
        stats.by_strategy[strategy] += 1
        for r in recs:
            stats.new += store.upsert(r)
        total += len(recs)
    else:
        log.warning("%s %s %s hit the max_pages cap (%d); raise --max-pages", site.name, ptype, loc, max_pages)
    log.info("%-20s %-17s %-22s %5d listings", site.name, ptype, loc, total)
    return total


async def run_crawl(cfg: Settings, sites: list[Site], locations: list[str] | None = None,
                    ptypes: list[str] | None = None, transport: httpx.AsyncBaseTransport | None = None,
                    first_only: bool = False) -> Stats:
    store, stats, stop = Store(cfg.db), Stats(), asyncio.Event()
    fetcher = Fetcher(cfg, store, stats, transport)
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGINT, stop.set)
    except (NotImplementedError, RuntimeError):
        pass                               # Windows: KeyboardInterrupt path handles it
    combos = []
    for s in sites:
        locs = list(locations or s.locations or LOCATIONS)
        pts = list(ptypes or s.ptypes or PROPERTY_TYPES)
        if first_only:
            locs, pts = locs[:1], pts[:1]
        combos += list(product([s], pts, locs))
    log.info("%d search combos | start concurrency %d", len(combos), cfg.concurrency)
    try:
        await asyncio.gather(*(crawl_combo(fetcher, store, stats, stop, s, p, l, cfg.max_pages)
                               for s, p, l in combos))
    finally:
        await fetcher.aclose()
        n = store.db.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        log.info("done | pages fetched %d, from cache %d, new listings %d, throttled %d, failed %d | "
                 "total in db: %d | strategies: %s", stats.pages, stats.cached, stats.new,
                 stats.throttled, stats.failed, n, dict(stats.by_strategy))
    return stats


async def run_probe(url: str, site: Site, cfg: Settings) -> None:
    fetcher = Fetcher(cfg, Store(":memory:"), Stats())
    try:
        html = await fetcher.get(url)
    finally:
        await fetcher.aclose()
    if not html:
        print("Could not fetch the page (see messages above).")
        return
    strategy, recs = extract(html, url, site, "?", "?")
    print(f"bytes: {len(html):,} | strategy used: {strategy} | listings parsed: {len(recs)}")
    keys = ("title", "price_kes", "period", "bedrooms", "bathrooms", "toilets", "parking", "kind", "location_text")
    for k in keys[3:]:
        print(f"  {k:14s} filled in {sum(r.get(k) is not None for r in recs)}/{len(recs)}")
    for r in recs[:3]:
        print(json.dumps({k: r.get(k) for k in keys}, ensure_ascii=False))


def run_parse(folder: str, site: Site, ptype: str, loc: str, db: str) -> None:
    """Parse .html files you saved from your browser (Ctrl+S) into the database."""
    store, new, files = Store(db), 0, sorted(Path(folder).glob("*.htm*"))
    for f in files:
        _, recs = extract(f.read_text(encoding="utf-8", errors="replace"),
                          site.url.format(ptype=ptype, loc=loc), site, ptype, loc)
        new += sum(store.upsert(r) for r in recs)
        print(f"{f.name}: {len(recs)} listings")
    print(f"parsed {len(files)} files, {new} new listings -> {db}")


# ───────────────────────────── self-test ─────────────────────────────

KPC_LIKE = """<html><body>
<script type="application/ld+json">{"@graph":[
 {"name":"3 Bedroom Apartment","url":"https://k.test/for-rent/a/1-3-bedroom","offers":{"price":"90000"}},
 {"name":"Furnished","url":"https://k.test/for-rent/a/2-furnished","offers":{"price":170000}}]}</script>
<div><span>Premium</span><span>KSh 90,000 /mo</span><span>Apartment for rent</span><h3>3 Bedroom Apartment</h3>
<p>Lovely. 1 bedroom guest wing too. Daily Rate: KSh 7,000 per night. Call 0712 345 678.</p>
<span>Yaya, Kilimani, Nairobi</span><span>3 beds 3 baths 3 toilets</span><a href="/for-rent/a/1-3-bedroom"></a></div>
<div><span>KSh 170,000 /mo</span><span>Mini flat for rent</span><h3>Furnished</h3>
<span>Kilimani Road, Kilimani Central Estate, Kilimani, Nairobi Added today</span>
<span>2 beds 2 baths 2 toilets 2 Parking</span><a href="/for-rent/a/2-furnished"></a></div></body></html>"""


P24_LIKE = """<html><body>
<div class="card"><a href="/4-bedroom-apartment-flat-to-rent-in-kitisuru-117698921"><span>KSh 750 000</span>
<span>4 Bedroom Apartment / Flat</span></a><span>Kitisuru QRJ2+HM7 Kitisuru, Kitisuru, Nairobi</span>
<span class="p24_icons">
<span class="p24_featureDetails" title="Bedrooms"><svg></svg><span>4</span></span>
<span class="p24_featureDetails" title="Bathrooms"><svg></svg><span>5</span></span>
<span class="p24_featureDetails" title="Parking Spaces"><svg></svg><span>4</span></span>
<span class="p24_size" title="Floor Size"><span>350 m²</span></span></span></div>
<div class="card"><a href="/1-bedroom-apartment-flat-to-rent-in-westlands-117696293"><span>KSh 130 000</span>
<span>1 Bedroom Apartment / Flat</span></a><span>Westlands Kobil Nairobi, Westlands, Nairobi</span></div>
<div class="card"><a href="/3-bedroom-apartment-flat-to-rent-in-kileleshwa-116975213"><span>KSh 207 000</span>
<span>3 Bedroom Apartment / Flat</span></a></div></body></html>"""


# Real Property24 tile shape; the descriptions quote extra amounts, which defeated heuristic detection.
P24_TILES = """<html><body>
<div class="QuellsSabre js_listingTile" data-listing-number="117696293"><div class="p24_regularTile">
<meta itemprop="price" content="320000.00"/>
<a href="/3-bedroom-apartment-flat-to-rent-in-westlands-117696293" title="3 Bedroom Apartment / Flat to Rent in Westlands">
<span class="p24_price">KSh 320 000</span><span class="p24_propertyTitle">3 Bedroom Apartment / Flat</span>
<span class="p24_address">PRW2+65H General Mathenge Drive, Westlands, Nairobi</span>
<span class="p24_excerpt">Deposit KSh 640 000 and service charge KSh 15 000 per month</span></a></div></div>
<div class="QuellsSabre js_listingTile" data-listing-number="117698000"><div class="p24_regularTile">
<meta itemprop="price" content="90000.00"/>
<a href="/1-bedroom-apartment-flat-to-rent-in-kilimani-117698000" title="1 Bedroom Apartment / Flat to Rent in Kilimani">
<span class="p24_price">KSh 90 000</span><span class="p24_propertyTitle">1 Bedroom Apartment / Flat</span>
<span class="p24_address">Argwings Kodhek Road, Kilimani, Nairobi</span>
<span class="p24_excerpt">Rent KSh 90 000, deposit KSh 90 000, agent fee KSh 45 000</span></a></div></div>
</body></html>"""


def selftest() -> None:
    """Offline end-to-end tests: mock server (CSS + heuristic sites, pagination stops, a 429
    retry, PII masking, price history) plus a Kenya-Property-Centre-shaped JSON-LD page."""
    # 1) JSON-LD page enriched from cards
    site = Site("k", "https://k.test/{ptype}/{loc}")
    strategy, recs = extract(KPC_LIKE, "https://k.test/x", site, "flats-apartments", "nairobi")
    a, b = sorted(recs, key=lambda r: r["price_kes"])[::-1]
    assert strategy == "jsonld" and len(recs) == 2
    assert (b["price_kes"], b["bedrooms"], b["bathrooms"], b["toilets"], b["period"]) == (90000, 3, 3, 3, "month"), b
    assert b["kind"] == "apartment" and b["location_text"] == "Yaya, Kilimani, Nairobi" and b["neighborhood"] == "kilimani"
    assert "0712" not in b["raw_text"] and "[phone]" in b["raw_text"]
    assert (a["kind"], a["parking"], a["neighborhood"]) == ("mini flat", 2, "kilimani"), a
    assert a["location_text"].startswith("Kilimani Road"), a

    # 1b) price formats: comma, space-separated (Property24), plain
    for txt, want in (("KSh 90,000 /mo", 90000), ("KSh 750 000 4 Bedroom", 750000),
                      ("KSh 90000 per month", 90000), ("KSh 1,500,000", 1500000)):
        assert parse_fields(txt)["price_kes"] == want, (txt, parse_fields(txt))

    # 1c) Property24-shaped cards: space-separated prices, no JSON-LD, suburb/kind from the URL slug
    p24 = SITES["property24"]
    strategy, recs = extract(P24_LIKE, "https://www.property24.co.ke/x", p24, "apartments", "nairobi")
    by_price = {r["price_kes"]: r for r in recs}
    assert strategy == "heuristic" and sorted(by_price) == [130000, 207000, 750000], (strategy, list(by_price))
    k = by_price[750000]
    assert (k["bedrooms"], k["size_sqm"], k["neighborhood"], k["kind"]) == (4, 350.0, "kitisuru", "apartment flat"), k
    assert (k["bathrooms"], k["parking"]) == (5, 4), k             # read from the titled icons
    assert by_price[130000]["neighborhood"] == "westlands" and by_price[130000]["bedrooms"] == 1
    assert by_price[207000]["neighborhood"] == "kileleshwa"      # no location line: taken from the URL
    assert p24.page_param == "Page"

    # 1d) tiles whose descriptions contain several prices must still be captured (css, exact price from meta)
    strategy, recs = extract(P24_TILES, "https://www.property24.co.ke/x", p24, "apartments", "nairobi")
    got = {r["price_kes"]: r for r in recs}
    assert strategy == "css" and sorted(got) == [90000, 320000], (strategy, list(got))
    assert got[320000]["title"].startswith("3 Bedroom Apartment") and got[320000]["neighborhood"] == "westlands"
    assert got[90000]["bedrooms"] == 1 and got[90000]["neighborhood"] == "kilimani"

    # 2) full mock crawl
    def card(host: str, i: int, tag: str, cls: str) -> str:
        return (f'<{tag} class="{cls}"><a href="/listings/{host}-flat-{i}"><h3>{i % 3 + 1} Bedroom Apartment</h3></a>'
                f'<span>KSh {40000 + i * 1000:,} per month</span>'
                f'<p>{i % 3 + 1} Beds {i % 2 + 1} Baths {90 + i} sqm. Call 0712 345 {600 + i} or a@b.co</p></{tag}>')

    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        page = int(req.url.params.get("page", "1"))
        host = req.url.host
        if host == "t1.local":      # css site: empty page 4 ends the crawl
            body = "".join(card("t1", page * 10 + i, "div", "listing-card") for i in range(5)) if page <= 3 else ""
        else:                       # heuristic site: repeats its last page forever
            body = "".join(card("t2", min(page, 3) * 10 + i, "article", "x") for i in range(5))
        return httpx.Response(200, text=f"<html><body>{body}</body></html>")

    sites = [Site("t1", "https://t1.local/{ptype}/{loc}", card_selectors=("div.listing-card",)),
             Site("t2", "https://t2.local/{ptype}/{loc}")]
    holder: dict[str, Store] = {}
    orig = Store.__init__
    Store.__init__ = lambda self, p: (orig(self, p), holder.setdefault("s", self))[0]  # type: ignore
    try:
        stats = asyncio.run(run_crawl(Settings(db=":memory:", backoff_base=0.01, max_pages=10), sites,
                                      ["nairobi/kilimani"], ["flats"], transport=httpx.MockTransport(handler)))
    finally:
        Store.__init__ = orig  # type: ignore
    db = holder["s"].db
    counts = dict(db.execute("SELECT source, COUNT(*) FROM listings GROUP BY source"))
    assert counts == {"t1": 15, "t2": 15}, counts
    assert stats.throttled == 1, stats
    assert not db.execute("SELECT 1 FROM listings WHERE raw_text LIKE '%0712%' OR raw_text LIKE '%a@b.co%'").fetchone()
    assert db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 30
    row = db.execute("SELECT price_kes, bedrooms, bathrooms, size_sqm, neighborhood FROM listings "
                     "WHERE url LIKE '%t1-flat-10'").fetchone()
    assert row == (50000, 2, 1, 100.0, "kilimani"), row
    print("selftest OK: json-ld enrichment | css + heuristic parsing | 429 retried | PII masked | pagination stops")


# ───────────────────────────── CLI ─────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="data/raw/rentals.db")
    common.add_argument("--concurrency", type=int, default=6)
    common.add_argument("--delay", type=float, default=0.0)
    common.add_argument("--refresh", action="store_true", help="ignore cache (weekly re-scrape)")

    c = sub.add_parser("crawl", parents=[common])
    c.add_argument("--sites", default="property24", help=f"comma list from: {', '.join(SITES)}")
    c.add_argument("--locations", help="comma list of location slugs (default: per-site list)")
    c.add_argument("--types", help="comma list of property types (default: per-site list)")
    c.add_argument("--max-pages", type=int, default=500)
    c.add_argument("--test", action="store_true", help="first location + type only, 2 pages")
    p = sub.add_parser("probe", parents=[common])
    p.add_argument("url")
    p.add_argument("--site", default="property24")
    pa = sub.add_parser("parse", help="parse HTML files saved from your browser")
    pa.add_argument("folder")
    pa.add_argument("--site", default="property24")
    pa.add_argument("--ptype", default="apartments-flats-to-rent-in-nairobi-c1890")
    pa.add_argument("--loc", default="nairobi")
    pa.add_argument("--db", default="data/raw/rentals.db")
    e = sub.add_parser("export")
    e.add_argument("--db", default="data/raw/rentals.db")
    e.add_argument("--out", default="data/raw/rentals.csv")
    sub.add_parser("selftest")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if a.cmd == "selftest":
        return selftest()
    if a.cmd == "parse":
        return run_parse(a.folder, SITES[a.site], a.ptype, a.loc, a.db)
    if a.cmd == "export":
        print(f"exported {Store(a.db).export_csv(a.out)} listings to {a.out}")
        return
    cfg = Settings(db=a.db, concurrency=a.concurrency, delay=a.delay, refresh=a.refresh)
    try:
        if a.cmd == "probe":
            asyncio.run(run_probe(a.url, SITES[a.site], cfg))
        else:
            cfg.max_pages = 2 if a.test else a.max_pages
            asyncio.run(run_crawl(cfg, [SITES[s] for s in a.sites.split(",")],
                                  a.locations.split(",") if a.locations else None,
                                  a.types.split(",") if a.types else None, first_only=a.test))
    except KeyboardInterrupt:
        log.warning("interrupted; progress is saved, re-run the same command to resume")


if __name__ == "__main__":
    main()
