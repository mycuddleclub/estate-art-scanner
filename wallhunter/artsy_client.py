"""Standalone client for Artsy's public GraphQL API (metaphysics-cdn).

CANONICAL SHARED FILE — external tools load this by path, same pattern as
authority_client.py. Gives scanners the evidence layer nothing else has:
LIVE primary-market asking prices with gallery names.

No API key required (same public endpoint the charity scraper uses).
Results are cached 30 days in wh_data/artsy_cache.db — including misses —
so repeated scans cost one HTTP call per artist per month. Only call this
for artists that are already flagged/shortlisted, never for every name.

Coverage is spotty (many artists have no Artsy page) and the slug guess
can miss on name variants: ABSENCE IS ALWAYS NEUTRAL. Failures are
silent. Set WH_NO_ARTSY=1 to disable all network calls (tests/offline).
"""

import json
import os
import re
import sqlite3
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENDPOINT = "https://metaphysics-cdn.artsy.net/v2"
CACHE_DB = Path(
    os.path.expanduser("~/estate-art-scanner/wh_data/artsy_cache.db"))
CACHE_DAYS = 30
TIMEOUT = 12

_QUERY = """{ artist(id: "%s") { artworksConnection(first: 20) { edges {
node { title date medium availability saleMessage
dimensions { in } listPrice { ... on Money { display }
... on PriceRange { display } } partner { name } } } } } }"""

_SUFFIX = re.compile(r"\b(?:jr|sr|ii|iii|iv)\b\.?", re.I)


def _slug(name):
    s = _SUFFIX.sub(" ", (name or "").lower())
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return "-".join(s.split())


def _cache():
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS artsy_cache ("
                 "slug TEXT PRIMARY KEY, payload TEXT, fetched_at TEXT)")
    return conn


def _fetch(slug):
    body = json.dumps({"query": _QUERY % slug}).encode()
    req = urllib.request.Request(
        ENDPOINT, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (wallhunter evidence client)"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read())
    artist = (data.get("data") or {}).get("artist") or {}
    edges = ((artist.get("artworksConnection") or {}).get("edges")) or []
    return [e["node"] for e in edges if e.get("node")]


def lookup(name):
    """List of Artsy artwork dicts for this artist (cached), or []."""
    if os.environ.get("WH_NO_ARTSY"):
        return []
    slug = _slug(name)
    if len(slug) < 5 or "-" not in slug:
        return []
    try:
        conn = _cache()
        row = conn.execute(
            "SELECT payload, fetched_at FROM artsy_cache WHERE slug=?",
            (slug,)).fetchone()
        if row:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(
                row[1])
            if age < timedelta(days=CACHE_DAYS):
                conn.close()
                return json.loads(row[0])
        works = _fetch(slug)
        conn.execute(
            "INSERT OR REPLACE INTO artsy_cache VALUES (?,?,?)",
            (slug, json.dumps(works),
             datetime.now(timezone.utc).isoformat(timespec="seconds")))
        conn.commit()
        conn.close()
        return works
    except Exception:
        return []


def evidence_line(name):
    """One compact line for prompts/emails, or '' if unknown (neutral)."""
    works = lookup(name)
    if not works:
        return ""
    for_sale = [w for w in works if (w.get("availability") or "") ==
                "for sale"]
    asks = []
    for w in for_sale:
        disp = (w.get("listPrice") or {}).get("display") \
            or (w.get("saleMessage") or "")
        if disp and "$" in disp:
            partner = (w.get("partner") or {}).get("name") or ""
            dims = (w.get("dimensions") or {}).get("in") or ""
            desc = ", ".join(x for x in (w.get("date"), dims) if x)
            asks.append(f"ask {disp}" + (f" ({desc}, {partner})"
                                         if partner or desc else ""))
    if asks:
        return (f"Artsy: {len(works)} works on record, {len(for_sale)}"
                f" for sale — " + "; ".join(asks[:3]))
    if for_sale:
        return (f"Artsy: {len(works)} works on record, {len(for_sale)}"
                " for sale (price on request)")
    return f"Artsy: {len(works)} works on record, none currently for sale"


if __name__ == "__main__":
    import sys
    print(evidence_line(" ".join(sys.argv[1:]) or "Floyd Newsum")
          or "(not on Artsy — neutral)")
