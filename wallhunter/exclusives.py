"""Platform-exclusive auction finder: HiBid/Bidsquare auctions whose house is
NOT currently active on LiveAuctioneers or Invaluable.

Thesis: exclusivity is a property of the HOUSE (houses choose platforms), and
a house absent from the big collector platforms has a structurally smaller
bidder pool — less competition on everything it sells. Harvests are
Playwright page-reads (no API spend); the big-platform house set is cached
for ~20h so the heavy LA/Invaluable sweeps run once per day.
"""

import json
import re
import time
from datetime import datetime, timedelta, timezone

from .config import DATA_DIR

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
      " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
CACHE = DATA_DIR / "exclusives_cache.json"
CACHE_MAX_AGE_H = 20

_SUFFIXES = {"llc", "inc", "ltd", "co", "company", "corp"}


def normalize_house(name: str) -> str:
    """lowercase, strip punctuation, drop trailing corporate suffixes."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    words = [w for w in s.split() if w]
    while words and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words)


def houses_match(a_norm: str, b_norm: str) -> bool:
    """Same house across platforms: exact, or the shorter name's words appear
    as consecutive WHOLE words in the longer ('abell' ~ 'abell auction', but
    'gold' !~ 'goldberg auctions')."""
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm:
        return True
    shorter, longer = sorted((a_norm, b_norm), key=len)
    if len(shorter) < 4:
        return False
    sw, lw = shorter.split(), longer.split()
    return any(lw[i:i + len(sw)] == sw for i in range(len(lw) - len(sw) + 1))


def compute_exclusives(auctions: list[dict], big_houses: set[str]) -> list[dict]:
    """Pure (unit-tested): auctions whose normalized house matches nothing in
    the big-platform house set."""
    out = []
    for a in auctions:
        h = normalize_house(a.get("house", ""))
        if not h:
            continue
        if any(houses_match(h, b) for b in big_houses):
            continue
        out.append(a)
    return out


# ── harvesters (Playwright; shared browser passed in) ────────────────────────

def _new_page(browser):
    return browser.new_context(user_agent=UA,
                               viewport={"width": 1440, "height": 1000}).new_page()


HIBID_GRAPHQL = "https://hibid.com/graphql"
_AUCTION_SEARCH_Q = """query($searchText: String, $pageNum: Int, $pageLength: Int) {
  auctionSearch(input: {status: OPEN, searchText: $searchText},
                pageNumber: $pageNum, pageLength: $pageLength) {
    pagedResults {
      filteredCount
      results { auction {
        id eventName eventDateBegin eventDateEnd
        auctioneer { name }
      } }
    }
  }
}"""
HIBID_WINDOW_DAYS = 14


def harvest_hibid(browser=None, query: str = "art",
                  window_days: int = HIBID_WINDOW_DAYS) -> list[dict]:
    """All OPEN HiBid auctions matching the query, via HiBid's own GraphQL
    (unauthenticated, same API the site uses). Replaces tile-scraping, which
    saw only the ~78 soonest-ending of 2,200+ matches. Window: ends between
    now and now+window_days (the API returns zombie listings months stale —
    'OPEN' status lies; the end date doesn't)."""
    import requests as _rq
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    horizon = now + timedelta(days=window_days)
    auctions, seen = [], set()
    page_num = 1
    while page_num <= 40:  # 40 x 100 = well past any realistic result set
        try:
            r = _rq.post(HIBID_GRAPHQL, timeout=30,
                         headers={"User-Agent": UA,
                                  "Content-Type": "application/json"},
                         json={"query": _AUCTION_SEARCH_Q,
                               "variables": {"searchText": query,
                                             "pageNum": page_num,
                                             "pageLength": 100}})
            r.raise_for_status()
            data = r.json()["data"]["auctionSearch"]["pagedResults"]
        except Exception as e:
            print(f"  hibid graphql page {page_num} failed: {str(e)[:80]}")
            break
        results = data.get("results", [])
        if not results:
            break
        for res in results:
            a = res.get("auction") or {}
            if not a.get("id") or a["id"] in seen:
                continue
            seen.add(a["id"])
            try:
                ends = datetime.fromisoformat(a.get("eventDateEnd", ""))
            except ValueError:
                continue
            if not (now - timedelta(hours=12) <= ends <= horizon):
                continue
            house = ((a.get("auctioneer") or {}).get("name") or "").strip()
            if not house:
                continue
            auctions.append({
                "platform": "hibid",
                "title": (a.get("eventName") or "").strip()[:120],
                "house": house[:80],
                "url": f"https://hibid.com/catalog/{a['id']}/_",
                "info": f"ends {ends:%m/%d}",
                "ends": ends.isoformat(),
            })
        page_num += 1
        time.sleep(0.4)
    return auctions


def harvest_bidsquare(browser) -> list[dict]:
    page = _new_page(browser)
    auctions, seen = [], set()
    try:
        page.goto("https://www.bidsquare.com/auctions",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)
        for _ in range(4):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(800)
        hrefs = page.eval_on_selector_all(
            "a[href*='/auctions/']", "els => els.map(e => e.href)")
        for h in hrefs:
            m = re.match(r"https://www\.bidsquare\.com/auctions/([a-z0-9-]+)/([a-z0-9-]+?)(?:/catalog)?/?$", h)
            if not m or m.group(1) in ("", "auctions"):
                continue
            key = f"{m.group(1)}/{m.group(2)}"
            if key in seen:
                continue
            seen.add(key)
            slug_title = m.group(2)
            title = re.sub(r"-\d+$", "", slug_title).replace("-", " ").title()
            auctions.append({"platform": "bidsquare", "title": title[:120],
                             "house": m.group(1).replace("-", " ").title(),
                             "url": f"https://www.bidsquare.com/auctions/{key}",
                             "info": ""})
    finally:
        page.close()
    return auctions


LA_HOUSES_URL = ("https://raw.githubusercontent.com/mycuddleclub/"
                 "auction-checker/main/la_houses.json")


def _github_token() -> str | None:
    import os
    import subprocess
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    try:
        out = subprocess.run(
            ["security", "find-internet-password", "-s", "github.com",
             "-a", "mycuddleclub", "-w"],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:
        return None


def harvest_la_houses(browser=None) -> set[str]:
    """House names with auctions currently on LiveAuctioneers.

    LA blocks headless browsers from residential IPs, so this reads
    la_houses.json published nightly by the auction-checker GitHub Action
    (which scrapes LA successfully from Actions runners). The repo is
    PRIVATE — the contents API needs the keychain token."""
    import base64
    import json as _json

    import requests
    token = _github_token()
    if not token:
        print("  LA house list: no GitHub token available — diff will be"
              " Invaluable-only this run")
        return set()
    try:
        resp = requests.get(
            "https://api.github.com/repos/mycuddleclub/auction-checker"
            "/contents/la_houses.json", timeout=20,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        data = _json.loads(base64.b64decode(resp.json()["content"]))
    except Exception as e:
        print(f"  LA house list unavailable ({str(e)[:80]}) — diff will be"
              " Invaluable-only this run")
        return set()
    print(f"  LA list: {data.get('count')} houses, updated"
          f" {data.get('updated', '?')[:16]}")
    return set(data.get("houses", []))


def harvest_invaluable_houses(browser, scrolls: int = 25) -> set[str]:
    """House names with auctions currently listed on Invaluable."""
    page = _new_page(browser)
    houses: set[str] = set()
    try:
        page.goto("https://www.invaluable.com/auctions/",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        stale = 0
        for _ in range(scrolls):
            page.mouse.wheel(0, 2800)
            page.wait_for_timeout(900)
            names = page.eval_on_selector_all(
                "a[href*='/auction-house/']",
                "els => els.map(e => (e.innerText||'').trim())"
                ".filter(t => t.toLowerCase().startsWith('by '))")
            before = len(houses)
            houses.update(n[3:].strip() for n in names)
            stale = stale + 1 if len(houses) == before else 0
            if stale >= 4:
                break
    finally:
        page.close()
    return houses


# ── orchestration ─────────────────────────────────────────────────────────────

def _load_cache() -> dict | None:
    try:
        c = json.loads(CACHE.read_text())
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(c["harvested_at"])).total_seconds() / 3600
        if age_h < CACHE_MAX_AGE_H:
            return c
    except Exception:
        pass
    return None


def find_exclusives(force_refresh: bool = False) -> list[dict]:
    from playwright.sync_api import sync_playwright
    from .blocklist import blocked_match

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        cache = None if force_refresh else _load_cache()
        if cache:
            big = set(cache["big_houses"])
            print(f"exclusives: using cached big-platform house set"
                  f" ({len(big)} houses)")
        else:
            print("exclusives: harvesting LiveAuctioneers house set...")
            la = harvest_la_houses(browser)
            print(f"  LA: {len(la)} houses")
            print("exclusives: harvesting Invaluable house set...")
            inv = harvest_invaluable_houses(browser)
            print(f"  Invaluable: {len(inv)} houses")
            big = {normalize_house(h) for h in la | inv if normalize_house(h)}
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps({
                "harvested_at": datetime.now(timezone.utc).isoformat(),
                "big_houses": sorted(big)}))
        print("exclusives: harvesting HiBid art auctions...")
        auctions = harvest_hibid(browser)
        print(f"  HiBid: {len(auctions)} auctions")
        print("exclusives: harvesting Bidsquare calendar...")
        bsq = harvest_bidsquare(browser)
        print(f"  Bidsquare: {len(bsq)} auctions")
        auctions += bsq
        browser.close()

    from .blocklist import load_non_art_keywords
    # Art Scout's LA-tuned keywords plus HiBid-specific junk genres
    junk = load_non_art_keywords() + (
        "surplus", "clearance", "overstock", "pallet", "liquidation",
        "unclaimed property", "police seizure", "storage unit", "returns",
        "equipment", "firearm", "guns", "ammo", "electronics", "grand mix")
    exclusives = []
    for a in compute_exclusives(auctions, big):
        if blocked_match(a.get("house")):
            continue
        title_l = a["title"].lower()
        if any(k in title_l for k in junk):
            continue  # surplus/pallets/guns etc. per Art Scout's keyword list
        exclusives.append(a)
    # soonest-ending first (undated entries, e.g. Bidsquare, sort last)
    exclusives.sort(key=lambda a: a.get("ends") or "9999")
    print(f"exclusives: {len(exclusives)}/{len(auctions)} auctions are"
          " off-LA/Invaluable, non-junk, non-blocked")
    return exclusives
