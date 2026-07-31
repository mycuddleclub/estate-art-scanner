"""Standalone client: MutualArt comps, cache-first, banked to prices.db.

CANONICAL SHARED FILE — load by path like authority_client / prices_client.
This is T3 of the every-lot build: it turns MutualArt from a fetch-and-discard
lookup into a COMPOUNDING archive. Every fetch is banked in prices.db at tier A
(via prices_client.record_mutualart), so the same artist is never re-scraped
while fresh, and the local judge values every lot offline.

    cached_comps(artist)        -> banked comps, offline, never scrapes
    bank(artist, items)         -> persist scraped comps (tier A), the seam in
    comps(artist, scrape_fn=..) -> cache-first, scrapes via an injected callable

Cache-first: comps() returns banked tier-A comps if fresh (<= FRESH_DAYS);
otherwise, if given a `scrape_fn`, it calls it, banks the result, and returns
the fresh set. The MutualArt scraper (art-appraiser/scrape_mutualart.py) is
`async def scrape_mutualart(page, query, interactive)` — it needs a logged-in
Playwright page, so the browser-owning caller (the Art Appraiser, or a future
MutualArt harvester) supplies `scrape_fn`. That keeps THIS module dependency-
free and testable anywhere, with the browser/async concern where the session
already lives. With no scrape_fn, comps() is a pure offline lookup.

Freshness policy: individual sale records are permanent facts; the ARTIST's
cache is re-scraped once its newest row is older than FRESH_DAYS, so new sales
get picked up without ever discarding history.
"""

import importlib.util as _il
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(os.path.expanduser("~/estate-art-scanner/wh_data/prices.db"))
FRESH_DAYS = int(os.getenv("MUTUALART_FRESH_DAYS", "90"))
SCRAPER_PATH = os.getenv(
    "MUTUALART_SCRAPER", os.path.expanduser("~/art-appraiser/scrape_mutualart.py"))


def _load(name, path):
    try:
        spec = _il.spec_from_file_location(name, path)
        mod = _il.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


# prices_client is the canonical storage layer — reuse its key normalization
# (so cache keys match exactly) and its tier-A write path.
_prices = _load(
    "prices_client",
    os.path.expanduser("~/estate-art-scanner/wallhunter/prices_client.py"))

_conn = None


def _connect():
    global _conn
    if _conn is None and DB_PATH.exists():
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def _key(artist):
    if _prices is not None:
        try:
            return _prices._key(artist)
        except Exception:
            pass
    return re.sub(r"\s+", " ",
                  re.sub(r"[^a-z ]+", " ", (artist or "").lower())).strip()


def cached_comps(artist):
    """Banked tier-A MutualArt comps for the artist + freshness. Never scrapes.
    Returns {"records": [...], "fresh": bool, "recorded_at": str|None}."""
    out = {"records": [], "fresh": False, "recorded_at": None}
    try:
        conn = _connect()
        ak = _key(artist)
        if conn is None or not ak:
            return out
        rows = conn.execute(
            "SELECT title, price_usd, outcome, estimate, sale_date, recorded_at"
            " FROM prices WHERE artist_key=? AND platform='mutualart'"
            " AND tier='A' ORDER BY recorded_at DESC", (ak,)).fetchall()
        out["records"] = [dict(r) for r in rows]
        if rows and rows[0]["recorded_at"]:
            out["recorded_at"] = rows[0]["recorded_at"]
            try:
                last = datetime.fromisoformat(rows[0]["recorded_at"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                out["fresh"] = (datetime.now(timezone.utc) - last
                                <= timedelta(days=FRESH_DAYS))
            except ValueError:
                out["fresh"] = True  # present but unparseable -> treat as fresh
        return out
    except Exception:
        return out


def bank(artist, items):
    """Persist scraped MutualArt comps at tier A. Returns rows added. The single
    write seam — the Art Appraiser and any harvester call this right after
    scraping, so MutualArt data stops evaporating and starts compounding.

    `items` is the list from scrape_mutualart()'s return dict (its "items" key):
    each dict may carry title / realized_price / estimate / medium / date."""
    if _prices is None or not items:
        return 0
    try:
        return _prices.record_mutualart(artist, items)
    except Exception:
        return 0


def comps(artist, scrape_fn=None):
    """Cache-first MutualArt comps for `artist`.
    Returns {"records": [...], "source": "cache"|"scrape"|"none", "fresh": bool}.

    scrape_fn: optional callable(artist) -> list[dict], supplied by the browser-
    owning caller (it drives the logged-in Playwright session and returns the
    "items" list). Omit it for an offline-only lookup, safe on any machine."""
    c = cached_comps(artist)
    if c["fresh"] and c["records"]:
        return {"records": c["records"], "source": "cache", "fresh": True}
    if scrape_fn is not None:
        try:
            items = list(scrape_fn(artist) or [])
        except Exception:
            items = []
        if items:
            bank(artist, items)                      # single write seam
            refreshed = cached_comps(artist)
            return {"records": refreshed["records"] or items,
                    "source": "scrape", "fresh": True}
    if c["records"]:  # stale-but-present beats nothing
        return {"records": c["records"], "source": "cache", "fresh": False}
    return {"records": [], "source": "none", "fresh": False}


# --------------------------------------------------------------------------- #
#  smoke test — offline cache path only (safe before the machine exists):
#      python3 wallhunter/mutualart_client.py "Fritz Scholder"
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    who = " ".join(sys.argv[1:]) or "Fritz Scholder"
    print(f"prices.db: {DB_PATH}  exists={DB_PATH.exists()}")
    print(f"scraper:   {SCRAPER_PATH}  present={Path(SCRAPER_PATH).exists()}")
    print(f"prices_client loaded: {_prices is not None}  fresh_days={FRESH_DAYS}")
    c = cached_comps(who)
    print(f"cached_comps({who!r}): {len(c['records'])} banked rows, "
          f"fresh={c['fresh']}, newest={c['recorded_at']}")
    r = comps(who)  # offline: no scrape_fn supplied
    print(f"comps({who!r}, offline): source={r['source']} n={len(r['records'])}")
