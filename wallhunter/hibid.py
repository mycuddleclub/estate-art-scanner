"""HiBid catalog adapter: harvest full-size lot images via Playwright.

HiBid image URLs (media.hibid.com/img.axd?id=...&sz=MAX&checksum=...) carry a
required checksum, so they must be collected from the rendered page, not
synthesized. sz=MAX already serves the largest size.
"""

import re
import time
from urllib.parse import parse_qs, urlparse

import requests

from . import db
from .config import RATE_LIMIT_SECONDS
from .ingest import _record_photo

# Namespace HiBid sale ids away from EstateSales.net ids in the shared table
HIBID_ID_OFFSET = 1_000_000_000

_CATALOG_ID = re.compile(r"hibid\.com/(?:catalog|auction)/(\d+)")
_IMG_FILTER = re.compile(r"img\.axd\?id=\d+")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def parse_hibid_ref(ref: str) -> int | None:
    m = _CATALOG_ID.search(ref)
    return int(m.group(1)) if m else None


def _image_id(url: str) -> str | None:
    q = parse_qs(urlparse(url).query)
    return (q.get("id") or [None])[0]


# Lot titles live on a.lot-link elements: as aria-label on the text link and
# as the URL slug (/lot/<id>/<title-slug>). Build href->title from labeled
# links, then pair each image with its enclosing lot-link's title.
_TILE_TEXT_JS = """() => {
  const titles = {};
  for (const a of document.querySelectorAll('a.lot-link, a[href*="/lot/"]')) {
    const href = (a.href || '').split('?')[0];
    if (!href.includes('/lot/')) continue;
    const label = (a.getAttribute('aria-label') || '').trim();
    if (label && !titles[href]) titles[href] = label;
  }
  const out = [];
  for (const img of document.querySelectorAll('img')) {
    const src = img.src || '';
    if (!src.includes('img.axd')) continue;
    const link = img.closest('a[href*="/lot/"]');
    let text = '';
    if (link) {
      const href = (link.href || '').split('?')[0];
      text = titles[href] || '';
      if (!text) {
        const slug = href.split('/').filter(Boolean).pop() || '';
        text = slug.replace(/-+/g, ' ').trim();
      }
    }
    out.push({src, text});
  }
  return out;
}"""


def _collect_page_images(page) -> dict[str, tuple[str, str]]:
    """Return {image_id: (url, lot_text)} for lot images on the current page."""
    page.wait_for_timeout(2500)
    for _ in range(4):  # trigger lazy loading
        page.mouse.wheel(0, 2400)
        page.wait_for_timeout(700)
    out = {}
    for item in page.evaluate(_TILE_TEXT_JS):
        src = item.get("src") or ""
        # lot images have moved hosts before (media.hibid.com -> cdn.hibid.com);
        # match any hibid host serving img.axd
        if "hibid.com" not in src or not _IMG_FILTER.search(src):
            continue
        if "/logos/" in src or "logo" in src.lower():
            continue
        img_id = _image_id(src)
        if img_id:
            out[img_id] = (src, (item.get("text") or "").strip())
    return out


def add_hibid(conn, url: str, max_photos: int | None = None,
              force: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    catalog_id = parse_hibid_ref(url)
    if catalog_id is None:
        raise SystemExit(f"not a HiBid catalog/auction URL: {url}")
    sale_id = HIBID_ID_OFFSET + catalog_id

    images: dict[str, str] = {}
    title = f"HiBid catalog {catalog_id}"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 1000}
                                   ).new_page()
        base = url.split("?")[0].rstrip("/")
        apage = 1
        while True:
            page.goto(f"{base}?apage={apage}", wait_until="domcontentloaded", timeout=60000)
            if apage == 1:
                t = (page.title() or "").strip()
                if t:
                    title = t[:150]
                if not force:
                    from .blocklist import blocked_match
                    frag = blocked_match(title)
                    if frag:
                        browser.close()
                        raise SystemExit(
                            f"catalog title '{title}' matches blocked house"
                            f" '{frag}'. Use --force to scan it anyway.")
            found = _collect_page_images(page)
            new = {k: v for k, v in found.items() if k not in images}
            images.update(new)
            print(f"  page {apage}: +{len(new)} images ({len(images)} total)")
            if not new or (max_photos and len(images) >= max_photos):
                break
            apage += 1
            time.sleep(RATE_LIMIT_SECONDS)
        browser.close()

    items = list(images.values())  # [(url, lot_text)]
    if max_photos:
        items = items[:max_photos]
    if not items:
        raise SystemExit("no lot images found — page structure may have changed")

    conn.execute(
        "INSERT INTO sales (id, platform, url, title, fetched_at, status)"
        " VALUES (?, 'hibid', ?, ?, ?, 'fetching')"
        " ON CONFLICT(id) DO UPDATE SET title=excluded.title, fetched_at=excluded.fetched_at",
        (sale_id, url, title, db.now()))
    conn.commit()

    known = {r["source_url"] for r in
             conn.execute("SELECT source_url FROM photos WHERE sale_id=?", (sale_id,))}
    new_count = 0
    for i, (u, lot_text) in enumerate(items):
        if u in known:
            # photo already held — still refresh its lot text
            if lot_text:
                conn.execute("UPDATE photos SET lot_text=? WHERE sale_id=? AND source_url=?",
                             (lot_text[:400], sale_id, u))
                conn.commit()
            continue
        try:
            resp = requests.get(u, timeout=25, headers={"User-Agent": UA})
            resp.raise_for_status()
        except Exception as e:
            print(f"  image {i} failed: {e}")
            continue
        if _record_photo(conn, sale_id, u, resp.content, lot_text=lot_text):
            new_count += 1
        time.sleep(RATE_LIMIT_SECONDS)
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(items)} downloaded")

    conn.execute("UPDATE sales SET photo_count=?, status='fetched' WHERE id=?",
                 (len(items), sale_id))
    conn.commit()
    print(f"[{sale_id}] {title} — {new_count} new photos ({len(items)} found)")
    return sale_id
