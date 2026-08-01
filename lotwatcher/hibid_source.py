"""HiBid discovery (unauthenticated GraphQL) + every-lot tile harvest.
Reuses wallhunter.deep's proven parse_tile; GraphQL shape from exclusives.py."""
import requests

from wallhunter.deep import parse_tile          # proven tile parser
from . import config

_AUCTION_SEARCH_Q = """query($searchText: String, $pageNum: Int, $pageLength: Int) {
  auctionSearch(input: {status: OPEN, searchText: $searchText},
                pageNumber: $pageNum, pageLength: $pageLength) {
    pagedResults {
      filteredCount
      results { auction {
        id eventName eventDateEnd
        auctioneer { name }
      } }
    }
  }
}"""


def discover(max_pages: int = 25) -> list[dict]:
    """All OPEN HiBid auctions via GraphQL (no browser, no bot defense)."""
    out = []
    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    for page_num in range(1, max_pages + 1):
        try:
            r = sess.post(config.HIBID_GRAPHQL, timeout=30, json={
                "query": _AUCTION_SEARCH_Q,
                "variables": {"searchText": "", "pageNum": page_num,
                              "pageLength": 100}})
            r.raise_for_status()
            paged = r.json()["data"]["auctionSearch"]["pagedResults"]
        except Exception as e:
            print(f"  hibid graphql p{page_num} failed: {str(e)[:80]}")
            break
        results = paged.get("results") or []
        if not results:
            break
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        floor = now + timedelta(days=config.MIN_DAYS_OUT)
        horizon = now + timedelta(days=14)
        added = 0
        for wrap in results:
            a = wrap.get("auction") or {}
            if not a.get("id"):
                continue
            # 'OPEN' status lies on zombie listings; the end date doesn't
            ends = a.get("eventDateEnd") or ""
            try:
                end_dt = datetime.fromisoformat(ends.replace("Z", "").split("+")[0])
                if end_dt < floor or end_dt > horizon:
                    continue
            except Exception:
                pass
            out.append({
                "id": str(a["id"]),
                "title": a.get("eventName") or "",
                "house": (a.get("auctioneer") or {}).get("name") or "",
                "url": f"https://hibid.com/catalog/{a['id']}",
                "ends_at": ends,
            })
            added += 1
        print(f"  hibid graphql p{page_num}: {added} in-window / {len(results)}")
    return out


_TILE_JS = """
() => Array.from(document.querySelectorAll('div.lot-tile, div[class*="lot-item"], article'))
        .map(t => t.innerText).filter(t => t && t.length > 20)
"""

_LOT_LINK_JS = """
() => Array.from(document.querySelectorAll('a[href*="/lot/"]'))
        .map(a => ({href: a.getAttribute('href') || '', text: (a.innerText||'').trim()}))
"""


_LOT_Q = """query($auctionId: Int, $pageNumber: Int!, $pageLength: Int!,
        $status: AuctionLotStatus, $sortOrder: EventItemSortOrder) {
  lotSearch(input: {auctionId: $auctionId, status: $status,
                    sortOrder: $sortOrder, countAsView: false}
            pageNumber: $pageNumber pageLength: $pageLength
            sortDirection: DESC) {
    pagedResults {
      totalCount
      results { id itemId lotNumber lead description estimate
                featuredPicture { fullSizeLocation hdThumbnailLocation thumbnailLocation }
                lotState { bidCount highBid minBid isClosed } }
    }
  }
}"""


def fetch_lots_api(auction_id: str) -> list[dict]:
    """All lots of one auction via HiBid's own GraphQL — no browser, ~1s.
    bidAmount is a placeholder (123.45) without auth, so bids stay unknown;
    estimate + title + full description are real and drive the funnel."""
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                         "Content-Type": "application/json"})
    lots, page_num, total = [], 1, None
    while True:
        r = sess.post(config.HIBID_GRAPHQL, timeout=30, json={
            "query": _LOT_Q,
            "variables": {"auctionId": int(auction_id), "pageNumber": page_num,
                          "pageLength": 100, "status": "OPEN",
                          "sortOrder": "LOT_NUMBER"}})
        r.raise_for_status()
        d = r.json()
        if "errors" in d:
            raise RuntimeError(str(d["errors"])[:200])
        pg = d["data"]["lotSearch"]["pagedResults"]
        total = pg["totalCount"]
        for it in pg["results"] or []:
            lid = str(it.get("id") or it.get("itemId") or "")
            if not lid:
                continue
            ls = it.get("lotState") or {}
            bid_count = ls.get("bidCount") or 0
            high_bid = ls.get("highBid") or 0
            min_bid = ls.get("minBid") or 0
            # current price = high bid if bids exist, else "no bids (opens $X)"
            if bid_count > 0 and high_bid:
                bid_str = f"${high_bid:g} ({bid_count} bids)"
            elif min_bid:
                bid_str = f"no bids (opens ${min_bid:g})"
            else:
                bid_str = ""
            fp = it.get("featuredPicture") or {}
            img = (fp.get("fullSizeLocation") or fp.get("hdThumbnailLocation")
                   or fp.get("thumbnailLocation") or "")
            lots.append({
                "id": lid,
                "title": (it.get("lead") or "")[:300],
                "estimate": (it.get("estimate") or "")[:80],
                "bid": bid_str,
                "url": f"https://hibid.com/lot/{lid}",
                "detail": (it.get("description") or "")[:2500],
                "img": img,
            })
        if len(lots) >= min(total, config.MAX_LOTS_PER_AUCTION) or not pg["results"]:
            break
        page_num += 1
    return lots


def _fetch_lots_browser(page, catalog_url: str) -> list[dict]:
    """Every lot in a HiBid catalog via tile scraping (Angular, needs browser)."""
    lots, seen = [], set()
    for pnum in range(1, config.HIBID_MAX_CATALOG_PAGES + 1):
        url = f"{catalog_url}?apage={pnum}" if pnum > 1 else catalog_url
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
        except Exception:
            break
        try:
            links = page.evaluate(_LOT_LINK_JS)
            tiles = page.evaluate(_TILE_JS)
        except Exception:
            break
        batch = []
        for ln in links:
            href = ln["href"]
            import re as _re
            m = _re.search(r"/lot/(\d+)", href)
            if not m:
                continue
            lid = m.group(1)
            if lid in seen:
                continue
            seen.add(lid)
            batch.append({
                "id": lid,
                "title": ln["text"][:300],
                "estimate": "", "bid": "",
                "url": href if href.startswith("http") else f"https://hibid.com{href}",
            })
        # enrich titles/bids from tile text where parse_tile finds structure
        for t in tiles:
            try:
                info = parse_tile(t)
            except Exception:
                continue
            if not info or not info.get("title"):
                continue
            for b in batch:
                if b["title"] and b["title"][:40] in t:
                    b["bid"] = str(info.get("bid") or info.get("realized_usd") or "")
                    b["estimate"] = str(info.get("estimate") or "")
                    break
        if not batch:
            break
        lots.extend(batch)
        if len(lots) >= config.MAX_LOTS_PER_AUCTION:
            break
    return lots

def fetch_lots(page, catalog_url: str) -> list[dict]:
    """API-first (fast, rich descriptions); browser tiles as fallback."""
    import re as _re
    m = _re.search(r"/catalog/(\d+)", catalog_url)
    if m:
        try:
            return fetch_lots_api(m.group(1))
        except Exception as e:
            print(f"    lot API failed ({str(e)[:60]}) — browser fallback")
    return _fetch_lots_browser(page, catalog_url)
