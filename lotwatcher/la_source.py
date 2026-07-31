"""LiveAuctioneers discovery + every-lot harvest via the persistent browser.
JS extraction adapted from art-scout scraper.py (proven selectors)."""
from . import browser as B
from . import config

_AUCTIONS_JS = """
() => {
    const results = [];
    const seen = new Set();
    const links = document.querySelectorAll('a[href*="/catalog/"]');
    for (const link of links) {
        const href = link.getAttribute('href') || '';
        const m = href.match(/\\/catalog\\/(\\d+)[_-]/);
        if (!m) continue;
        const id = m[1];
        if (seen.has(id)) continue;
        seen.add(id);
        let card = link;
        for (let i = 0; i < 10; i++) {
            if (!card.parentElement) break;
            card = card.parentElement;
            if (card.querySelector('a[href*="/auctioneer/"]')) break;
        }
        let title = '';
        const h2 = link.querySelector('h2');
        const h3 = link.querySelector('h3');
        if (h2) title = h2.innerText.trim();
        else if (h3) title = h3.innerText.trim();
        else title = href.split('/').filter(p=>p).pop()
                        .replace(/^\\d+[_-]/, '').replace(/-/g,' ');
        let house = '';
        const hl = card.querySelector('a[href*="/auctioneer/"]');
        if (hl) house = hl.innerText.trim();
        results.push({ id, title, house,
            url: href.startsWith('http') ? href
                 : 'https://www.liveauctioneers.com' + href });
    }
    return results;
}
"""

_LOTS_JS = """
() => {
    const results = [];
    const seen = new Set();
    const links = document.querySelectorAll('a[href*="/item/"]');
    for (const link of links) {
        const href = link.getAttribute('href') || '';
        const m = href.match(/\\/item\\/(\\d+)[_-]/);
        if (!m) continue;
        const id = m[1];
        if (seen.has(id)) continue;
        seen.add(id);
        let card = link;
        for (let i = 0; i < 8; i++) {
            if (!card.parentElement) break;
            card = card.parentElement;
        }
        const cardText = card.innerText || '';
        let title = link.innerText.trim();
        if (!title) {
            const h = link.querySelector('h1,h2,h3,h4,h5');
            if (h) title = h.innerText.trim();
        }
        if (!title) {
            title = href.replace(/.*\\/item\\/\\d+[_-]/, '')
                       .replace(/\\/$/, '').replace(/-/g, ' ');
        }
        const estMatch = cardText.match(/Est[^$\\n]*\\$[\\d,]+[^\\n]*/i)
                      || cardText.match(/\\$[\\d,]+\\s*[-\\u2013]\\s*\\$[\\d,]+/);
        const estimate = estMatch ? estMatch[0].trim() : '';
        const bidMatch = cardText.match(/(?:Current|Starting|Opening)[^$\\n]*\\$[\\d,]+/i);
        const bid = bidMatch ? bidMatch[0].trim() : '';
        results.push({ id, title, estimate, bid,
            url: href.startsWith('http') ? href
                 : 'https://www.liveauctioneers.com' + href });
    }
    return results;
}
"""


def discover(page) -> list[dict]:
    """All upcoming LA auctions from the paginated search listing."""
    out, session_ids = [], set()
    for page_num in range(1, config.LA_MAX_LISTING_PAGES + 1):
        url = (f"{config.LA_SEARCH_URL}?page={page_num}"
               if page_num > 1 else config.LA_SEARCH_URL)
        if not B.goto(page, url, "auction discovery", wait="networkidle"):
            break
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)
        auctions = page.evaluate(_AUCTIONS_JS)
        fresh = [a for a in auctions if a["id"] not in session_ids]
        if not fresh:
            break                       # nav-link loop = real end of listing
        session_ids.update(a["id"] for a in fresh)
        out.extend(fresh)
        print(f"  LA listing p{page_num}: {len(fresh)} auctions (total {len(out)})")
        B.polite_sleep(config.LA_PAGE_DELAY_S)
    return out


def fetch_lots(page, catalog_url: str) -> list[dict]:
    """Every lot in one auction catalog (paginated)."""
    lots, seen = [], set()
    for pnum in range(1, config.LA_MAX_CATALOG_PAGES + 1):
        url = f"{catalog_url}?page={pnum}" if pnum > 1 else catalog_url
        if not B.goto(page, url, "catalog lots"):
            break
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
        try:
            batch = page.evaluate(_LOTS_JS)
        except Exception:
            break
        fresh = [l for l in batch if l["id"] not in seen]
        if not fresh:
            break
        seen.update(l["id"] for l in fresh)
        lots.extend(fresh)
        if len(lots) >= config.MAX_LOTS_PER_AUCTION:
            break
        B.polite_sleep(config.LA_PAGE_DELAY_S)
    return lots


def fetch_detail(page, lot_url: str) -> str:
    """Detail-page text for stage-3 candidates only (description/condition)."""
    if not B.goto(page, lot_url, "lot detail"):
        return ""
    page.wait_for_timeout(1200)
    try:
        txt = page.evaluate(
            "() => document.body ? document.body.innerText : ''")
    except Exception:
        return ""
    # keep the middle of the page (skip nav header / footer boilerplate)
    return txt[500:4500]
