"""Standalone client for Daniel's charity-auction database (auction_data.db).

CANONICAL SHARED FILE — external tools (Art Scout, Art Appraiser, the
Checker) load this by path, same pattern as authority_client.py:

    import importlib.util as _il
    _spec = _il.spec_from_file_location(
        "charity_client",
        "/Users/bigpadre/estate-art-scanner/wallhunter/charity_client.py")
    charity_client = _il.module_from_spec(_spec)
    _spec.loader.exec_module(charity_client)

Evidence source: ~29k lots from museum-benefit auctions where the museum
stated a fair market value, many with the realized winning bid alongside.
FMV = vetted gallery-market ask; highest_bid on a closed sale = a real
transaction (charity context — typically hammers below retail).

Dependency-free, read-only. Failures are silent and neutral — a missing
DB must never break a consumer, and absence of an artist means nothing.
"""

import re
import sqlite3
from pathlib import Path

DB_PATH = Path("/Users/bigpadre/charity_auction_scraper/auction_data.db")

_SUFFIX = re.compile(r"\b(?:jr|sr|ii|iii|iv)\b\.?", re.I)
_index = None  # norm_key -> list[dict], built lazily once per process


def _key(name):
    s = _SUFFIX.sub(" ", (name or "").lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]+", " ", s)).strip()


def _build_index():
    global _index
    if _index is not None:
        return _index
    _index = {}
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT artist, title, year, medium, dimensions, estimate_low,"
            " estimate_high, estimate_single, highest_bid, sale_closed,"
            " auction_name FROM lots WHERE artist IS NOT NULL"
            " AND artist != ''").fetchall()
        conn.close()
        for r in rows:
            k = _key(r["artist"])
            if len(k) >= 5 and " " in k:  # person-shaped names only
                _index.setdefault(k, []).append(dict(r))
    except Exception:
        _index = {}
    return _index


def lookup(name):
    """All charity lots for this artist (suffix/case-insensitive), or []."""
    k = _key(name)
    if not k:
        return []
    return _build_index().get(k, [])


def _fmv(lot):
    if lot.get("estimate_single"):
        return f"FMV ${lot['estimate_single']:,.0f}"
    if lot.get("estimate_low") and lot.get("estimate_high"):
        return f"FMV ${lot['estimate_low']:,.0f}-{lot['estimate_high']:,.0f}"
    return None


def evidence_line(name):
    """One compact line for prompts/emails, or '' if unknown (neutral)."""
    lots = lookup(name)
    if not lots:
        return ""
    # sold results are the strongest evidence — show them first
    lots = sorted(lots, key=lambda x: (x.get("highest_bid") or 0,
                                       x.get("estimate_single")
                                       or x.get("estimate_high") or 0),
                  reverse=True)
    bits = []
    for lot in lots[:3]:
        b = f"'{(lot.get('title') or 'untitled').strip()}'"
        if lot.get("year"):
            b += f" ({lot['year']})"
        fmv = _fmv(lot)
        if fmv:
            b += f" {fmv}"
        if lot.get("highest_bid"):
            b += f", sold ${lot['highest_bid']:,.0f}"
        elif lot.get("sale_closed"):
            b += ", unsold"
        src = (lot.get("auction_name") or "").split(":")[0].strip()
        if src:
            b += f" ({src})"
        bits.append(b)
    more = f" (+{len(lots) - 3} more)" if len(lots) > 3 else ""
    return (f"Charity-benefit history ({len(lots)} lot"
            f"{'s' if len(lots) != 1 else ''}): " + "; ".join(bits) + more)


if __name__ == "__main__":
    import sys
    print(evidence_line(" ".join(sys.argv[1:]) or "Floyd Newsum")
          or "(no charity history — neutral)")
