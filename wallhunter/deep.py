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

import os

# lot titles containing these are skipped outright (per Daniel): mass-market
# prints, and the attribution-hedge family ("after X", "attributed to X" =
# not actually by the artist — flagging them against the artist's market
# would be exactly wrong). Terms ending in * match stems (print* -> prints,
# printed); plain terms match exact words (after != afternoon).
_DEFAULT_SKIP = ("print*,giclee*,poster*,reproduction*,etching*,litho*,"
                 "limited edition,attributed to,after,manner of,school of,"
                 "style of,circle of,follower of")


def _skip_regex(spec: str) -> re.Pattern:
    parts = []
    for term in (t.strip() for t in spec.split(",") if t.strip()):
        if term.endswith("*"):
            parts.append(rf"\b{re.escape(term[:-1])}\w*")
        else:
            parts.append(rf"\b{re.escape(term)}\b")
    return re.compile("|".join(parts), re.I)


SKIP_TITLE_WORDS = _skip_regex(os.environ.get("WH_SKIP_TITLE_WORDS",
                                              _DEFAULT_SKIP))

# Case-SENSITIVE standalone tokens: "LE" / "L.E." = limited edition (per
# Daniel), but only uppercase and free-standing — "Le Pho" and "Le Corbusier"
# are name particles (title case) and sale/stolen/lemon never match because
# the token may not touch other letters.
SKIP_CASE_TOKENS = re.compile(
    r"(?<![A-Za-z])(?:LE|L\.E\.?)(?![A-Za-z])")


def skip_lot(title: str) -> bool:
    t = title or ""
    return bool(SKIP_TITLE_WORDS.search(t) or SKIP_CASE_TOKENS.search(t))


# The fake-mill tell (Daniel's rule, automated): no real regional auction
# has a catalog full of "original" masterpieces. Honest labels (print/
# litho/after...) are exempt via skip_lot; favorites are immune.
BLUE_CHIP = re.compile(
    r"\b(van gogh|monet|manet|picasso|dali|renoir|rembrandt|matisse|degas|"
    r"cezanne|c[ée]zanne|gauguin|chagall|modigliani|klimt|schiele|kandinsky|"
    r"miro|mir[óo]|magritte|warhol|basquiat|haring|pollock|rothko|"
    r"de kooning|lichtenstein|banksy|kahlo|vermeer|caravaggio|goya|"
    r"toulouse.?lautrec|munch|hockney|kusama)\b", re.I)
MILL_MIN_MASTERS = 3   # distinct blue-chip names claimed as originals
MILL_MIN_CLAIMS = 8    # or this many original-claim lots of any mix


def mill_masters(lots: list[dict]) -> tuple[set[str], int]:
    """Pure (unit-tested): distinct blue-chip names claimed as ORIGINALS
    (honestly-labeled prints/copies don't count), and the claim count."""
    names, claims = set(), 0
    for lot in lots:
        t = lot.get("title") or ""
        m = BLUE_CHIP.search(t)
        if m and not skip_lot(t):
            names.add(m.group(1).lower())
            claims += 1
    return names, claims


def is_mill(names: set[str], claims: int) -> bool:
    return len(names) >= MILL_MIN_MASTERS or claims >= MILL_MIN_CLAIMS


def auto_block_house(conn, house: str, sale_url: str, names: set[str],
                     claims: int):
    conn.execute(
        "INSERT OR REPLACE INTO auto_blocked_houses (house, sale_url,"
        " masters, claims, detected_at) VALUES (?,?,?,?,?)",
        (house, sale_url, ",".join(sorted(names)), claims, db.now()))
    conn.commit()


def is_auto_blocked(conn, house: str) -> bool:
    if not house:
        return False
    return conn.execute(
        "SELECT 1 FROM auto_blocked_houses WHERE house=? COLLATE NOCASE",
        (house.strip(),)).fetchone() is not None


ART_SIGNAL = re.compile(
    r"fine art|gallery|galleries|estate|antique|painting|artwork|"
    r"art auction|collection|artist|decorative arts|americana|folk art",
    re.I)


def is_art_signal(auction: dict) -> bool:
    """Pure (unit-tested): does the auction title or house name suggest real
    art inventory rather than liquidation stock?"""
    return bool(ART_SIGNAL.search(f"{auction.get('title', '')} "
                                  f"{auction.get('house', '')}"))


_BID = re.compile(r"(?:High Bid|Current Bid)[:\s]*([\d,.]+)\s*USD", re.I)
_REALIZED = re.compile(r"Price Realized[:\s]*([\d,.]+)\s*USD", re.I)
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
    realized = _REALIZED.search(tile_text)
    bids_n = _BIDS_N.search(tile_text)
    est = _EST.search(tile_text)
    return {
        "high_bid_usd": float(bid.group(1).replace(",", "")) if bid else None,
        "realized_usd": (float(realized.group(1).replace(",", ""))
                         if realized else None),
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


def institutional_flag_reason(auth: dict | None, lot: dict) -> str | None:
    """Pure (unit-tested): flag artists the museums document but the market
    search can't see. Only 'strong' institutional standing (AAA papers or
    work in 3+ major museums) qualifies — 'listed' alone enriches evidence
    but doesn't flag, so common-name noise stays out of the email."""
    from .authority import describe, institutional_standing
    if not auth or institutional_standing(auth) != "strong":
        return None
    bid = lot.get("high_bid_usd")
    if bid is None or bid <= 0:
        return f"museum-documented artist — {describe(auth)} — no bids yet"
    if bid <= 100:
        return (f"museum-documented artist — {describe(auth)} — current bid"
                f" ${bid:,.0f}")
    return None


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


SAFETY_CEILING = 400  # bounds a runaway night, not a quota

_PRICES_CONN = None


def _prices_conn():
    global _PRICES_CONN
    if _PRICES_CONN is None:
        from . import prices as prices_mod
        _PRICES_CONN = prices_mod.connect()
    return _PRICES_CONN


def unscanned_candidates(conn, exclusives: list[dict]) -> list[dict]:
    """Watermark selection (unit-tested): HiBid auctions in the window not
    yet in deep_auctions — favorite houses first, then art-signal houses,
    then the tail; soonest-ending within each band."""
    from .favorites import favorite_fragments, match_favorite
    frags = favorite_fragments(conn)
    candidates = [a for a in exclusives if a["platform"] == "hibid"
                  and not conn.execute(
                      "SELECT 1 FROM deep_auctions WHERE sale_url=?",
                      (a["url"],)).fetchone()]
    candidates.sort(key=lambda a: (
        0 if match_favorite(a.get("house"), frags) else
        (1 if is_art_signal(a) else 2),
        a.get("ends") or "9999"))
    return candidates


def deep_scan(conn, exclusives: list[dict], research_cap_usd: float = 25.0,
              max_auctions: int | None = None) -> tuple[list[dict], dict]:
    from playwright.sync_api import sync_playwright

    # Watermark model (Daniel's design): every auction in the window is
    # scanned exactly ONCE, as it enters — the nightly workload is one day's
    # inflow (plus anything listed late into the window). deep_auctions is
    # the watermark.
    candidates = unscanned_candidates(conn, exclusives)
    hibid = candidates[:max_auctions or SAFETY_CEILING]
    already = sum(1 for a in exclusives if a["platform"] == "hibid") - len(candidates)
    print(f"  deep: {len(candidates)} unscanned auctions in window"
          f" ({already} already covered), running {len(hibid)}")
    meter = CostMeter(research_cap_usd)
    flagged = []
    budget_left = True

    # pass 1: harvest every auction's art lots, collect unknown claimed names
    from .artists import artist_key, classify_person_names
    from .favorites import favorite_fragments, match_favorite
    fav_frags = favorite_fragments(conn)
    auto_blocked_this_run: list[dict] = []
    per_auction: list[tuple[dict, list[dict]]] = []
    unknown_names: dict[str, str] = {}  # key -> display name
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for auction in hibid:
            if is_auto_blocked(conn, auction.get("house")):
                continue
            try:
                lots = harvest_art_lots(browser, auction["url"])
            except Exception as e:
                print(f"  deep: {auction['house'][:30]} harvest failed:"
                      f" {str(e)[:80]}")
                continue  # NOT marked scanned — retried tomorrow
            conn.execute(
                "INSERT OR REPLACE INTO deep_auctions (sale_url, house, title,"
                " ends, art_lots, scanned_at, location) VALUES (?,?,?,?,?,?,?)",
                (auction["url"], auction["house"], auction["title"],
                 auction.get("ends"), len(lots), db.now(),
                 auction.get("location") or ""))
            conn.commit()
            if not lots:
                continue
            names, claims = mill_masters(lots)
            if is_mill(names, claims) and \
                    not match_favorite(auction.get("house"), fav_frags):
                auto_block_house(conn, auction["house"], auction["url"],
                                 names, claims)
                auto_blocked_this_run.append(
                    {"house": auction["house"], "masters": len(names),
                     "claims": claims, "names": sorted(names)})
                print(f"  deep: 🚫 AUTO-BLOCKED fake mill:"
                      f" {auction['house'][:40]} — {len(names)} masters"
                      f" claimed ({', '.join(sorted(names)[:5])}...)")
                continue  # marked scanned; lots never researched or flagged
            print(f"  deep: {auction['house'][:36]} — {len(lots)} art lots")
            new_lots = [l for l in lots
                        if not skip_lot(l["title"])
                        and not conn.execute(
                            "SELECT 1 FROM deep_lots WHERE lot_url=?",
                            (l["url"],)).fetchone()]
            per_auction.append((auction, new_lots))
            for lot in new_lots:
                name = listing_artist_claim(lot["title"])
                lot["claim"] = name
                if name and lookup(conn, name) is None:
                    unknown_names.setdefault(artist_key(name), name)

    # pass 2: penny-cheap gate — only person-like names get web research;
    # product-like strings are cached tier 'none' so they never recur.
    # Names the authority library recognizes are confirmed people: they skip
    # the classifier entirely ($0) and go straight to market research.
    from . import authority as auth_lib
    auth_conn = auth_lib.connect()
    auth_confirmed: list[str] = []
    for k in list(unknown_names):
        if auth_lib.lookup(auth_conn, unknown_names[k]) is not None:
            auth_confirmed.append(unknown_names.pop(k))
    if auth_confirmed:
        print(f"  deep: {len(auth_confirmed)} names known to the reference"
              " library — classifier skipped")
    verdicts = classify_person_names(list(unknown_names.values()), meter)
    persons = [n for n, ok in verdicts.items() if ok is True] + auth_confirmed
    rejected = [n for n, ok in verdicts.items() if ok is False]
    deferred = sum(1 for ok in verdicts.values() if ok is None)
    if deferred:
        print(f"  deep: {deferred} names unclassified (batch failures)"
              " — deferred to next run")
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
    researched_n = 0
    for n in persons:
        if not budget_left:
            break
        try:
            research_artist(conn, n, meter)
            researched_n += 1
        except CostCapExceeded:
            print(f"  deep: research budget cap hit (${meter.total:.2f})"
                  f" — remaining names carry to tomorrow")
            budget_left = False
        except Exception as e:
            print(f"  deep: research of '{n}' errored ({str(e)[:60]}) — continuing")

    # pass 3: flag against the (now warm) store + the reference library
    for auction, lots in per_auction:
        for lot in lots:
            claim = lot.get("claim")
            row = lookup(conn, claim) if claim else None
            auth = auth_lib.lookup(auth_conn, claim) if claim else None
            if row is None and auth and artist_key(auth["canonical"]) != \
                    artist_key(claim):
                # variant bridge: 'Walker, Wm. Aiken' -> canonical name that
                # the market store may already know
                row = lookup(conn, auth["canonical"])
            reason = flag_reason(row, lot) if row else None
            if reason and auth:
                reason += f" [{auth_lib.describe(auth)}]"
            if reason is None:
                reason = institutional_flag_reason(auth, lot)
            if reason:
                # Daniel's triage datapoint: local price-DB market summary
                from . import prices as prices_mod
                ceiling = (row["market_high_usd"] if row
                           and (row["tier"] or "") == "strong" else None)
                mline = prices_mod.summary_line(prices_mod.artist_summary(
                    _prices_conn(), claim, vetted_ceiling=ceiling))
                if mline:
                    reason += f" · {mline}"
                # charity-benefit FMVs + live Artsy asks (flagged lots only,
                # so the network cost stays tiny; both neutral on absence)
                from . import artsy_client, charity_client
                for extra in (charity_client.evidence_line(claim),
                              artsy_client.evidence_line(claim)):
                    if extra:
                        reason += f" · {extra}"
            conn.execute(
                "INSERT OR IGNORE INTO deep_lots (lot_url, sale_url, house,"
                " title, artist_key, high_bid_usd, bid_count, estimate,"
                " info, first_seen, emailed) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                (lot["url"], auction["url"], auction["house"], lot["title"],
                 row["artist_key"] if row else
                 (artist_key(auth["canonical"]) if auth else None),
                 lot["high_bid_usd"],
                 lot["bid_count"], lot["estimate"], reason or "", db.now()))
            if reason:
                flagged.append({**lot, "house": auction["house"],
                                "artist": row["artist"] if row else
                                auth["canonical"], "reason": reason,
                                "market_note": (row["market_note"] or "")
                                if row else "",
                                "evidence": ((row["evidence"] or "")[:200])
                                if row else auth_lib.describe(auth)})
        conn.commit()
    stats = {
        "auctions": len(per_auction),
        "auto_blocked": auto_blocked_this_run,
        "lots": sum(len(lots) for _, lots in per_auction),
        "new_names": len(unknown_names),
        "researched": researched_n,
        "spend": round(meter.total, 2),
        "capped": not budget_left,
        "names_deferred": max(0, len(persons) - researched_n),
    }
    print(f"deep: {len(flagged)} flagged lots, research spend ${meter.total:.2f}")
    return flagged, stats
