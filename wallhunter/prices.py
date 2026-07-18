"""The Price Engine: a local database of realized prices, built by capture.

prices.db accumulates auction outcomes from sources the tools already visit:
HiBid closed auctions (tier B), LiveAuctioneers watched artists via the cloud
checker (tier B), MutualArt comps from Art Appraiser runs (tier A), and the
Getty Provenance Index (tier A, via authority.db).

The product is the per-artist MARKET SUMMARY (Daniel's triage question):
"50,000 sales averaging $50" vs "$45k typical, thin volume" — sale count,
median, band, recency. Individual rows exist to feed that summary.

Fake defense (structural, not heuristic):
- Blacklisted houses are never recorded at all.
- Rows contradicting a vetted high-value market get suspect=1 and are
  excluded from summaries — they feed the fake-density map instead.
- Artists with a vetted market above VETTED_FIREWALL_USD never use tier-B
  rows as market evidence (the fake economy operates on famous names;
  regional data for them is intelligence about fakes, not about prices).
"""

import hashlib
import re
import sqlite3
from datetime import datetime, timezone

from .artists import artist_key
from .config import DATA_DIR

PRICES_DB = DATA_DIR / "prices.db"

# Artists whose vetted (tier A) ceiling exceeds this never take tier-B
# evidence: that's the stratum where faking pays.
VETTED_FIREWALL_USD = 10_000.0
# suspect if a 'sold' price is under both caps vs a strong vetted ceiling
SUSPECT_CEILING_MIN = 10_000.0
SUSPECT_FRACTION = 0.01
SUSPECT_ABS_MAX = 500.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE,             -- lot_url or synthetic dedupe key
  artist_key TEXT NOT NULL,
  artist TEXT,
  title TEXT,
  work_class TEXT,             -- unique | edition | unknown
  price_usd REAL,
  outcome TEXT,                -- sold | final_bid | unsold | listed
  bid_count INTEGER,
  estimate TEXT,
  house TEXT,
  platform TEXT,               -- hibid | liveauctioneers | mutualart | ...
  tier TEXT NOT NULL,          -- A (vetted sources) | B (regional capture)
  suspect INTEGER NOT NULL DEFAULT 0,
  sale_date TEXT,
  source TEXT,
  recorded_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_prices_artist ON prices(artist_key);
CREATE INDEX IF NOT EXISTS idx_prices_house ON prices(house);

CREATE TABLE IF NOT EXISTS harvested (
  sale_url TEXT PRIMARY KEY,
  at TEXT,
  lots_recorded INTEGER
);
"""

EDITION_RE = re.compile(
    r"print|giclee|giclée|litho|serigraph|etching|engraving|poster|"
    r"reproduction|offset|\b\d{1,4}\s*/\s*\d{1,4}\b", re.I)
EDITION_CASE_RE = re.compile(r"(?<![A-Za-z])(?:LE|L\.E\.?|AP|A\.P\.?)(?![A-Za-z])")
UNIQUE_RE = re.compile(
    r"\boil\b|watercolor|watercolour|acrylic|gouache|pastel|charcoal|"
    r"mixed media|on canvas|on board|on panel|bronze|carving|sculpture", re.I)


def classify_work_class(title: str) -> str:
    t = title or ""
    if EDITION_RE.search(t) or EDITION_CASE_RE.search(t):
        return "edition"
    if UNIQUE_RE.search(t):
        return "unique"
    return "unknown"


def parse_price(text) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if text > 0 else None
    m = re.search(r"([\d,]+(?:\.\d{1,2})?)", str(text).replace("$", ""))
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
        return v if v > 0 else None
    except ValueError:
        return None


def connect(path=None) -> sqlite3.Connection:
    p = path or PRICES_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_suspect(price_usd, outcome: str, vetted_ceiling) -> bool:
    """Pure (unit-tested): a 'sale' far beneath a strong vetted market is
    presumptively a fake/misattribution, not a comp."""
    if not price_usd or not vetted_ceiling:
        return False
    if outcome not in ("sold", "final_bid"):
        return False
    return (vetted_ceiling >= SUSPECT_CEILING_MIN
            and price_usd < SUSPECT_ABS_MAX
            and price_usd < vetted_ceiling * SUSPECT_FRACTION)


def record(conn, *, artist: str, title: str = "", price_usd=None,
           outcome: str = "sold", bid_count=None, estimate: str = "",
           house: str = "", platform: str = "", tier: str = "B",
           sale_date: str = "", key: str = "", source: str = "",
           vetted_ceiling=None, blocked_house: bool = False) -> str:
    """Returns 'recorded' | 'suspect' | 'blocked' | 'dup' | 'skipped'."""
    ak = artist_key(artist)
    if not ak or " " not in ak:
        return "skipped"
    if blocked_house:
        return "blocked"
    price = parse_price(price_usd)
    if not key:
        key = "synth:" + hashlib.sha1(
            f"{ak}|{title}|{price}|{sale_date}|{platform}".encode()
        ).hexdigest()[:20]
    suspect = 1 if is_suspect(price, outcome, vetted_ceiling) else 0
    cur = conn.execute(
        "INSERT OR IGNORE INTO prices (key, artist_key, artist, title,"
        " work_class, price_usd, outcome, bid_count, estimate, house,"
        " platform, tier, suspect, sale_date, source, recorded_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, ak, artist.strip(), (title or "")[:300],
         classify_work_class(title), price, outcome, bid_count,
         (estimate or "")[:80], (house or "")[:120], platform, tier,
         suspect, sale_date or "", source, now()))
    if cur.rowcount == 0:
        return "dup"
    return "suspect" if suspect else "recorded"


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def artist_summary(conn, name: str, vetted_ceiling=None) -> dict | None:
    """Daniel's triage datapoint: volume + typical price + band + recency.

    Tier-B rows are excluded entirely for artists whose vetted market
    exceeds the firewall (that's fake territory; use tier A only).
    Suspect rows never count anywhere.
    """
    ak = artist_key(name)
    if not ak:
        return None
    firewall = bool(vetted_ceiling and vetted_ceiling >= VETTED_FIREWALL_USD)
    tier_clause = " AND tier='A'" if firewall else ""
    rows = conn.execute(
        f"SELECT price_usd, outcome, work_class, sale_date FROM prices"
        f" WHERE artist_key=? AND suspect=0{tier_clause}", (ak,)).fetchall()
    if not rows:
        return None
    sold = [r["price_usd"] for r in rows
            if r["price_usd"] and r["outcome"] in ("sold", "final_bid")]
    uniq = [r["price_usd"] for r in rows
            if r["price_usd"] and r["outcome"] in ("sold", "final_bid")
            and r["work_class"] == "unique"]
    decided = [r for r in rows if r["outcome"] in ("sold", "final_bid", "unsold")]
    unsold_n = sum(1 for r in decided if r["outcome"] == "unsold")
    dates = sorted(r["sale_date"] for r in rows if r["sale_date"])
    fake_rows = conn.execute(
        "SELECT COUNT(*) c FROM prices WHERE artist_key=? AND suspect=1",
        (ak,)).fetchone()["c"]
    return {
        "records": len(rows),
        "sold_n": len(sold),
        "median_usd": _median(sold),
        "median_unique_usd": _median(uniq),
        "high_usd": max(sold) if sold else None,
        "unsold_n": unsold_n,
        "latest": dates[-1] if dates else None,
        "firewalled_to_tier_a": firewall,
        "suspect_rows": fake_rows,
    }


def summary_line(s: dict | None) -> str:
    """One-line market check for emails/prompts, or ''."""
    if not s or not s.get("sold_n"):
        return ""
    med = s.get("median_unique_usd") or s.get("median_usd")
    bits = [f"{s['sold_n']} sales", f"median ${med:,.0f}" if med else ""]
    if s.get("high_usd"):
        bits.append(f"high ${s['high_usd']:,.0f}")
    if s.get("latest"):
        bits.append(f"latest {s['latest'][:10]}")
    if s.get("firewalled_to_tier_a"):
        bits.append("(vetted sources only)")
    if s.get("suspect_rows"):
        bits.append(f"⚠︎{s['suspect_rows']} suspect lots excluded")
    return "market: " + ", ".join(b for b in bits if b)


def house_report(conn, min_suspect: int = 3) -> list[dict]:
    """Houses accumulating suspect rows — blacklist candidates."""
    return [dict(r) for r in conn.execute(
        "SELECT house, SUM(suspect) s, COUNT(*) n FROM prices"
        " WHERE house != '' GROUP BY house HAVING s >= ? ORDER BY s DESC",
        (min_suspect,))]
