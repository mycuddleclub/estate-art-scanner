"""HiBid closed-auction results harvester (Price Engine tier-B inflow).

Revisits auctions the deep scanner already catalogued (deep_auctions) after
they end and records each art lot's closing state — final bid, bid count,
seller-claimed artist — into prices.db. Closed results age off the platform,
so this is the perishable inflow: every unharvested day is data gone.

Honesty note: a HiBid closing high bid is recorded as outcome='final_bid',
not 'sold' — reserves and non-payers mean it isn't a verified hammer price.
Zero AI involved: deterministic name extraction + library lookups only.
"""

import argparse
from datetime import datetime, timedelta

from . import db, prices
from .artists import lookup as market_lookup
from .blocklist import blocked_match, load_blocked_houses
from .deep import harvest_art_lots
from .stage2 import listing_artist_claim

# don't harvest until the auction has been over this long (late bids settle)
SETTLE_HOURS = 6

# ULAN/authority placeholder records that are attributions, not people
import re
GENERIC_NAMES = re.compile(
    r"^(native american|african american|american indian|old master|"
    r"folk artist|american school|french school|english school|"
    r"chinese school|continental school)$|unidentified|unknown artist",
    re.I)

# '"Title" by Artist Name' — the artist follows the 'by', not the quotes
_BY_RE = re.compile(
    r"[\"“”']\s*,?\s*by\s+([A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+){0,3})")


def claim_from_title(title: str) -> str | None:
    """Pure (unit-tested): artist claim from a lot title, preferring the
    explicit '"Title" by X' form over the leading-name heuristic."""
    m = _BY_RE.search(title or "")
    if m:
        return m.group(1)
    return listing_artist_claim(title)


def candidates(conn, pconn, limit: int) -> list[dict]:
    cutoff = (datetime.now() - timedelta(hours=SETTLE_HOURS)).isoformat()
    rows = conn.execute(
        "SELECT d.sale_url, d.house, d.title, d.ends FROM deep_auctions d"
        " WHERE d.art_lots > 0 AND d.ends < ?"
        " AND NOT EXISTS (SELECT 1 FROM ph.harvested h"
        "                 WHERE h.sale_url = d.sale_url)"
        " ORDER BY d.ends DESC LIMIT ?", (cutoff, limit)).fetchall()
    return [dict(r) for r in rows]


def vetted_ceiling_for(conn, name: str):
    row = market_lookup(conn, name)
    if row and (row["tier"] or "") == "strong":
        return row["market_high_usd"]
    return None


def resolve_person(conn, auth_conn, claim: str) -> str | None:
    """Zero-AI person gate + name salvage. A price records only under a name
    the Reference Library or the artists store confirms; extractor claims
    like 'Arnold Berns Nude Photograph' are progressively trimmed to find
    the real name inside ('Arnold Berns'). Product phrases ('Glass Koala
    Bear') resolve to nothing and never pollute the price DB; genuinely new
    artists start recording once the deep pipeline's classifier admits them
    to the artists store."""
    from . import authority as auth_lib
    if GENERIC_NAMES.search(claim or ""):
        return None
    words = (claim or "").split()
    forms = [claim] + [" ".join(words[:n]) for n in (3, 2)
                       if len(words) > n]
    for form in forms:
        a = auth_lib.lookup(auth_conn, form)
        if a is not None:
            return a["canonical"]
        row = market_lookup(conn, form)
        if row is not None:
            if (row["tier"] or "") == "none" and \
                    (row["source"] or "") == "wallhunter-classifier":
                continue  # classifier already ruled this form product-like
            return row["artist"]
    return None


def harvest_results(limit: int = 40) -> dict:
    conn = db.connect()
    pconn = prices.connect()
    # attach prices.db so the candidate query can anti-join on harvested
    conn.execute("ATTACH DATABASE ? AS ph", (str(prices.PRICES_DB),))
    todo = candidates(conn, pconn, limit)
    print(f"results: {len(todo)} closed auctions to harvest (limit {limit})")
    if not todo:
        return {"auctions": 0, "recorded": 0}
    from . import authority as auth_lib
    auth_conn = auth_lib.connect()
    blocked = load_blocked_houses()
    stats = {"auctions": 0, "recorded": 0, "suspect": 0, "blocked_house": 0,
             "no_claim": 0, "unknown_name": 0, "dup": 0}
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for auc in todo:
            try:
                lots = harvest_art_lots(browser, auc["sale_url"])
            except Exception as e:
                print(f"  results: {auc['house'][:30]} failed"
                      f" ({str(e)[:60]}) — retried next run")
                continue
            house_blocked = blocked_match(auc["house"], blocked) is not None
            n_rec = 0
            for lot in lots:
                claim = claim_from_title(lot.get("title"))
                if not claim:
                    stats["no_claim"] += 1
                    continue
                resolved = resolve_person(conn, auth_conn, claim)
                if not resolved:
                    stats["unknown_name"] += 1
                    continue
                claim = resolved
                realized = lot.get("realized_usd")
                bid = lot.get("high_bid_usd")
                n_bids = lot.get("bid_count")
                if realized:
                    outcome, price = "sold", realized  # HiBid-verified hammer
                elif bid and (n_bids or 0) > 0:
                    outcome, price = "final_bid", bid
                else:
                    outcome, price = "unsold", None
                got = prices.record(
                    pconn, artist=claim, title=lot.get("title") or "",
                    price_usd=price,
                    outcome=outcome, bid_count=n_bids,
                    estimate=lot.get("estimate") or "",
                    house=auc["house"] or "", platform="hibid", tier="B",
                    sale_date=(auc.get("ends") or "")[:10],
                    key=lot.get("url") or "", source="results_harvest",
                    vetted_ceiling=vetted_ceiling_for(conn, claim),
                    blocked_house=house_blocked)
                if got in ("recorded", "suspect"):
                    n_rec += 1
                    stats["recorded"] += 1
                    if got == "suspect":
                        stats["suspect"] += 1
                elif got == "blocked":
                    stats["blocked_house"] += 1
                elif got == "dup":
                    stats["dup"] += 1
            pconn.execute(
                "INSERT OR REPLACE INTO harvested (sale_url, at, lots_recorded)"
                " VALUES (?,?,?)", (auc["sale_url"], prices.now(), n_rec))
            pconn.commit()
            stats["auctions"] += 1
            print(f"  results: {auc['house'][:36]} — {len(lots)} art lots,"
                  f" {n_rec} priced")
    print(f"results: {stats}")
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()
    harvest_results(args.limit)
