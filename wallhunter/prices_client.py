"""Standalone client for the Price Engine (prices.db).

CANONICAL SHARED FILE — external tools (Art Appraiser, Art Scout, the
Checker) load this by path, same pattern as authority_client.py:

    import importlib.util as _il
    _spec = _il.spec_from_file_location(
        "prices_client",
        "/Users/bigpadre/estate-art-scanner/wallhunter/prices_client.py")
    prices_client = _il.module_from_spec(_spec)
    _spec.loader.exec_module(prices_client)

Dependency-free. Failures are silent and neutral — a missing prices.db
must never break a consumer.
"""

import hashlib
import importlib.util as _il
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("/Users/bigpadre/estate-art-scanner/wh_data/prices.db")

_conn = None


def _key(name):
    return re.sub(r"\s+", " ",
                  re.sub(r"[^a-z ]+", " ", (name or "").lower())).strip()


def _parse_price(text):
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if text > 0 else None
    m = re.search(r"([\d,]+(?:\.\d{1,2})?)", str(text).replace("$", ""))
    try:
        v = float(m.group(1).replace(",", "")) if m else None
        return v if v and v > 0 else None
    except ValueError:
        return None


_EDITION = re.compile(
    r"print|giclee|giclée|litho|serigraph|etching|engraving|poster|"
    r"reproduction|offset|\b\d{1,4}\s*/\s*\d{1,4}\b", re.I)
_UNIQUE = re.compile(
    r"\boil\b|watercolor|watercolour|acrylic|gouache|pastel|charcoal|"
    r"mixed media|on canvas|on board|on panel|bronze|carving|sculpture", re.I)


def _work_class(title):
    t = title or ""
    if _EDITION.search(t):
        return "edition"
    if _UNIQUE.search(t):
        return "unique"
    return "unknown"


def _connect():
    global _conn
    if _conn is None and DB_PATH.exists():
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


_ce = None
_ce_tried = False


def _get_ce():
    """Lazy, cached comp_engine (for the medium/size/year parsers). Guarded so a
    missing comp_engine just means the enrichment columns stay NULL."""
    global _ce, _ce_tried
    if not _ce_tried:
        _ce_tried = True
        try:
            spec = _il.spec_from_file_location(
                "comp_engine",
                "/Users/bigpadre/estate-art-scanner/wallhunter/comp_engine.py")
            mod = _il.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _ce = mod
        except Exception:
            _ce = None
    return _ce


def _ensure_ma_columns(conn):
    """Idempotent: add the comp-engine columns if an older prices.db lacks them.
    ADD COLUMN is instant and non-destructive — existing rows get NULL."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(prices)")}
    for col, typ in (("medium", "TEXT"), ("area_sqin", "REAL"),
                     ("work_year", "INTEGER"), ("size_raw", "TEXT"),
                     ("price_raw", "TEXT"), ("price_native", "REAL"),
                     ("currency", "TEXT")):
        if col not in have:
            try:
                conn.execute(f"ALTER TABLE prices ADD COLUMN {col} {typ}")
            except Exception:
                pass


def record_mutualart(artist: str, items: list) -> int:
    """Persist MutualArt comps (tier A) permanently, enriched for the comp
    engine: medium; size (raw string + parsed area); work-year; the realized
    price in its NATIVE currency + currency code + a rough USD (MutualArt is
    international — many comps are GBP/EUR); and the auction house/venue.
    price_usd stays the USD value so summaries and the estimator work in one
    currency. Returns rows added."""
    try:
        conn = _connect()
        ak = _key(artist)
        if conn is None or not ak or " " not in ak:
            return 0
        _ensure_ma_columns(conn)
        ce = _get_ce()
        n = 0
        for it in items or []:
            title = (it.get("title") or "")[:300]
            price_raw = (it.get("realized_price") or "")[:60]
            if ce is not None:
                native, currency, usd = ce.parse_money(price_raw)
            else:
                usd = _parse_price(price_raw)
                native, currency = usd, ("USD" if usd else None)
            outcome = "sold" if usd else (
                "unsold" if it.get("estimate") else "listed")
            medium = (it.get("medium") or "")[:120]
            size_raw = (it.get("size") or "")[:120]
            house = (it.get("house") or "")[:120]
            area = year = None
            if ce is not None:
                area = ce.parse_dimensions(size_raw or title)[2]
                year = ce.parse_work_year(title)  # work year from title, not sale
            key = "mutualart:" + hashlib.sha1(
                f"{ak}|{title}|{usd}|{it.get('date','')}".encode()
            ).hexdigest()[:20]
            cur = conn.execute(
                "INSERT OR IGNORE INTO prices (key, artist_key, artist,"
                " title, work_class, price_usd, outcome, estimate, house,"
                " platform, tier, suspect, sale_date, source, recorded_at,"
                " medium, area_sqin, work_year, size_raw, price_raw,"
                " price_native, currency)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?,?)",
                (key, ak, artist.strip(), title,
                 _work_class(f"{title} {it.get('medium', '')}"), usd,
                 outcome, (it.get("estimate") or "")[:80], house,
                 "mutualart", "A", (it.get("date") or "")[:40],
                 "art-appraiser",
                 datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 medium, area, year, size_raw, price_raw, native, currency))
            n += cur.rowcount
        conn.commit()
        return n
    except Exception:
        return 0


def market_line(artist: str) -> str:
    """One-line market summary for prompts/emails, or ''. Suspect rows and
    unsold-only artists yield ''."""
    try:
        conn = _connect()
        ak = _key(artist)
        if conn is None or not ak:
            return ""
        rows = conn.execute(
            "SELECT price_usd FROM prices WHERE artist_key=? AND suspect=0"
            " AND price_usd IS NOT NULL AND outcome IN ('sold','final_bid')"
            " ORDER BY price_usd", (ak,)).fetchall()
        if not rows:
            return ""
        vals = [r["price_usd"] for r in rows]
        med = vals[len(vals) // 2]
        return (f"local price DB: {len(vals)} sales, median ${med:,.0f},"
                f" high ${max(vals):,.0f}")
    except Exception:
        return ""
