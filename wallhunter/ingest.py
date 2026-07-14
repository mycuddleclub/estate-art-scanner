"""Ingest a sale's photos into the store: EstateSales.net URL/ID or local folder."""

import re
import sys
import time
from pathlib import Path

import requests

from . import db
from .config import RATE_LIMIT_SECONDS, REPO_ROOT
from .images import load, path_for, store_bytes

sys.path.insert(0, str(REPO_ROOT / "src"))
from estatesales_client import (  # noqa: E402
    get_sale_details_batch, get_sale_full, get_sale_url, get_fullres_urls,
)

def parse_sale_ref(ref: str) -> int | None:
    """EstateSales.net URL or bare numeric ID -> sale id; None if not numeric/URL.

    The sale id is the LAST all-digit path segment (URLs also contain the ZIP:
    /WA/Mercer-Island/98040/4984500)."""
    if ref.isdigit():
        return int(ref)
    if "estatesales.net" not in ref:
        return None
    segments = [s for s in re.split(r"[/?#]", ref) if s.isdigit()]
    return int(segments[-1]) if segments else None


def _record_photo(conn, sale_id: int, source_url: str | None, data: bytes) -> bool:
    file_hash = store_bytes(data)
    try:
        img = load(file_hash)
        w, h = img.size
    except Exception:
        return False
    try:
        conn.execute(
            "INSERT OR IGNORE INTO photos (sale_id, source_url, file_hash, width, height)"
            " VALUES (?,?,?,?,?)",
            (sale_id, source_url or file_hash, file_hash, w, h),
        )
        return True
    finally:
        conn.commit()


def add_estatesales(conn, sale_id: int, max_photos: int | None = None) -> int:
    detail = (get_sale_details_batch([sale_id]) or [None])[0]
    full = get_sale_full(sale_id)
    if not full:
        raise SystemExit(f"could not fetch sale {sale_id}")
    title = (detail or {}).get("name") or full.get("name") or f"Sale {sale_id}"
    loc = ""
    if detail:
        loc = f"{detail.get('cityName','')}, {detail.get('stateCode','')} {detail.get('postalCodeNumber','')}"
    starts = ((detail or {}).get("firstLocalStartDate") or {}).get("_value")
    ends = ((detail or {}).get("lastLocalEndDate") or {}).get("_value")
    url = get_sale_url(detail) if detail else None

    conn.execute(
        "INSERT INTO sales (id, url, title, location, starts_at, ends_at, fetched_at, status)"
        " VALUES (?,?,?,?,?,?,?, 'fetching')"
        " ON CONFLICT(id) DO UPDATE SET url=excluded.url, title=excluded.title,"
        " location=excluded.location, starts_at=excluded.starts_at,"
        " ends_at=excluded.ends_at, fetched_at=excluded.fetched_at",
        (sale_id, url, title, loc, starts, ends, db.now()),
    )
    conn.commit()

    urls = get_fullres_urls(full)
    if max_photos:
        urls = urls[:max_photos]
    known = {r["source_url"] for r in
             conn.execute("SELECT source_url FROM photos WHERE sale_id=?", (sale_id,))}
    new = 0
    for i, u in enumerate(urls):
        if u in known:
            continue
        try:
            resp = requests.get(u, timeout=25)
            resp.raise_for_status()
        except Exception as e:
            print(f"  photo {i} failed: {e}")
            continue
        if _record_photo(conn, sale_id, u, resp.content):
            new += 1
        time.sleep(RATE_LIMIT_SECONDS)
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(urls)} photos")

    conn.execute("UPDATE sales SET photo_count=?, status='fetched' WHERE id=?",
                 (len(urls), sale_id))
    conn.commit()
    print(f"[{sale_id}] {title} — {new} new photos ({len(urls)} listed)")
    return sale_id


def add_folder(conn, folder: Path, max_photos: int | None = None) -> int:
    """Local folder of images -> synthetic sale (negative id from path hash)."""
    files = sorted(p for p in folder.iterdir()
                   if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    if max_photos:
        files = files[:max_photos]
    if not files:
        raise SystemExit(f"no images in {folder}")
    sale_id = -abs(hash(str(folder.resolve()))) % 2_000_000_000
    conn.execute(
        "INSERT OR IGNORE INTO sales (id, platform, url, title, fetched_at, status)"
        " VALUES (?, 'folder', ?, ?, ?, 'fetched')",
        (sale_id, str(folder.resolve()), folder.name, db.now()),
    )
    conn.commit()
    new = sum(1 for p in files
              if _record_photo(conn, sale_id, str(p.resolve()), p.read_bytes()))
    conn.execute("UPDATE sales SET photo_count=? WHERE id=?", (len(files), sale_id))
    conn.commit()
    print(f"[{sale_id}] folder {folder} — {new} photos ingested")
    return sale_id
