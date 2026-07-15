"""Off-Radar Deep: per-lot artist intelligence on off-radar HiBid auctions.

For each off-radar auction: load its ART category (HiBid's own taxonomy via
?g=40089), harvest lot titles + bids + estimates from the tiles, extract
seller-named artists, resolve each against the shared artists store
(researching new names within a budget), and flag lots where a real artist's
market dwarfs the current bid. The whole point: these houses have no
LiveAuctioneers/Invaluable audience, so a flagged lot has both evidence AND
a thin bidder pool.
"""

import re

from . import db
from .artists import lookup, research_artist
from .config import CostMeter
from .exclusives import UA, _new_page
from .stage2 import listing_artist_claim

ART_CATEGORY = 40089
FLAG_TIERS = {"strong", "listed"}
# flag when documented high price >= this multiple of the current bid
MIN_RATIO = 8.0
MIN_MARKET_HIGH = 400.0

_BID = re.compile(r"(?:High Bid|Current Bid)[:\s]*([\d,.]+)\s*USD", re.I)
_BIDS_N = re.compile(r"(\d+)\s+Bids?", re.I)
_EST = re.compile(r"([\d,.]+\s*-\s*[\d,.]+\s*USD)", re.I)

_LOT_TILE_JS = """() => {
  const titles = {};
  for (const a of document.querySelectorAll('a[href*="/lot/"]')) {
    const href = (a.href || '').split('?')[0];
    const label = (a.getAttribute('aria-label') || '').trim();
    if (label && !titles[href]) titles[href] = label;
  }
  const out = []; const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/lot/"]')) {
    const href = (a.href || '').split('?')[0];
    if (seen.has(href) || !titles[href]) continue;
    seen.add(href);
    const tile = a.closest('.lot-tile, [class*=lot-tile], [class*=list-group-item]')
        || a.parentElement?.parentElement?.parentElement;
    out.push({url: href, title: titles[href],
              tile: tile ? tile.innerText.trim().replace(/\\s+/g, ' ').slice(0, 300) : ''});
  }
  return out;
}"""


def parse_tile(tile_text: str) -> dict:
    """Pure (unit-tested): bid, bid count, estimate from tile text."""
    bid = _BID.search(tile_text)
    bids_n = _BIDS_N.search(tile_text)
    est = _EST.search(tile_text)
    return {
        "high_bid_usd": float(bid.group(1).replace(",", "")) if bid else None,
        "bid_count": int(bids_n.group(1)) if bids_n else None,
        "estimate": est.group(1) if est else None,
    }


def harvest_art_lots(browser, catalog_url: str, max_pages: int = 4) -> list[dict]:
    """Lots in the auction's own Art category (?g=40089)."""
    page = _new_page(browser)
    lots, seen = [], set()
    base = catalog_url.split("?")[0].rstrip("/")
    try:
        for n in range(1, max_pages + 1):
            page.goto(f"{base}?g={ART_CATEGORY}&apage={n}",
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3500)
            new = 0
            for item in page.evaluate(_LOT_TILE_JS):
                if item["url"] in seen:
                    continue
                seen.add(item["url"])
                lots.append({"url": item["url"], "title": item["title"][:200],
                             **parse_tile(item["tile"])})
                new += 1
            if not new:
                break
    finally:
        page.close()
    return lots


def flag_reason(artist_row, lot: dict) -> str | None:
    """Pure (unit-tested): why this lot deserves attention, or None."""
    if artist_row is None or (artist_row["tier"] or "") not in FLAG_TIERS:
        return None
    high = artist_row["market_high_usd"] or 0
    if high < MIN_MARKET_HIGH:
        return None
    bid = lot.get("high_bid_usd")
    if bid is None or bid <= 0:
        return (f"{artist_row['tier']} artist, documented to ${high:,.0f},"
                " no bids yet")
    if high / bid >= MIN_RATIO:
        return (f"{artist_row['tier']} artist documented to ${high:,.0f}"
                f" vs current bid ${bid:,.0f} ({high / bid:.0f}x)")
    return None


def deep_scan(conn, exclusives: list[dict], research_cap_usd: float = 3.0,
              max_auctions: int | None = None) -> tuple[list[dict], dict]:
    from playwright.sync_api import sync_playwright

    # soonest-ending first (act-now relevance); deep_lots dedupe means each
    # night's quota advances through the window rather than rescanning
    hibid = sorted((a for a in exclusives if a["platform"] == "hibid"),
                   key=lambda a: a.get("ends") or "9999")
    hibid = hibid[:max_auctions or 25]
    meter = CostMeter(research_cap_usd)
    flagged = []
    budget_left = True

    # pass 1: harvest every auction's art lots, collect unknown claimed names
    from .artists import artist_key, classify_person_names
    per_auction: list[tuple[dict, list[dict]]] = []
    unknown_names: dict[str, str] = {}  # key -> display name
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for auction in hibid:
            try:
                lots = harvest_art_lots(browser, auction["url"])
            except Exception as e:
                print(f"  deep: {auction['house'][:30]} harvest failed:"
                      f" {str(e)[:80]}")
                continue
            if not lots:
                continue
            print(f"  deep: {auction['house'][:36]} — {len(lots)} art lots")
            new_lots = [l for l in lots if not conn.execute(
                "SELECT 1 FROM deep_lots WHERE lot_url=?", (l["url"],)).fetchone()]
            per_auction.append((auction, new_lots))
            for lot in new_lots:
                name = listing_artist_claim(lot["title"])
                lot["claim"] = name
                if name and lookup(conn, name) is None:
                    unknown_names.setdefault(artist_key(name), name)

    # pass 2: penny-cheap gate — only person-like names get web research;
    # product-like strings are cached tier 'none' so they never recur
    verdicts = classify_person_names(list(unknown_names.values()), meter)
    persons = [n for n, ok in verdicts.items() if ok]
    rejected = [n for n, ok in verdicts.items() if not ok]
    for n in rejected:
        conn.execute(
            "INSERT OR IGNORE INTO artists (artist_key, artist, source, tier,"
            " evidence) VALUES (?,?,?,?,?)",
            (artist_key(n), n, "wallhunter-classifier", "none",
             "classifier: product/object description, not a person name"))
    conn.commit()
    print(f"  deep: {len(unknown_names)} new names -> {len(persons)} person-like,"
          f" {len(rejected)} product-like (skipped)")
    from .config import CostCapExceeded
    for n in persons:
        if not budget_left:
            break
        try:
            research_artist(conn, n, meter)
        except CostCapExceeded:
            print(f"  deep: research budget cap hit (${meter.total:.2f})"
                  f" — remaining names carry to tomorrow")
            budget_left = False
        except Exception as e:
            print(f"  deep: research of '{n}' errored ({str(e)[:60]}) — continuing")

    # pass 3: flag against the (now warm) store
    for auction, lots in per_auction:
        for lot in lots:
            row = lookup(conn, lot["claim"]) if lot.get("claim") else None
            reason = flag_reason(row, lot) if row else None
            conn.execute(
                "INSERT OR IGNORE INTO deep_lots (lot_url, sale_url, house,"
                " title, artist_key, high_bid_usd, bid_count, estimate,"
                " info, first_seen, emailed) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                (lot["url"], auction["url"], auction["house"], lot["title"],
                 row["artist_key"] if row else None, lot["high_bid_usd"],
                 lot["bid_count"], lot["estimate"], reason or "", db.now()))
            if reason:
                flagged.append({**lot, "house": auction["house"],
                                "artist": row["artist"], "reason": reason,
                                "market_note": row["market_note"] or "",
                                "evidence": (row["evidence"] or "")[:200]})
        conn.commit()
    stats = {
        "auctions": len(per_auction),
        "lots": sum(len(lots) for _, lots in per_auction),
        "new_names": len(unknown_names),
        "researched": len(persons),
        "spend": round(meter.total, 2),
    }
    print(f"deep: {len(flagged)} flagged lots, research spend ${meter.total:.2f}")
    return flagged, stats
