"""Morning auto-pipeline: resume capped sales, ingest new watchlist sales,
screen everything within a daily budget, email the digest.

Sale selection is deliberately conservative: watchlist ZIPs only (shared with
the nightly scanner), minimum photo count, newest-photo-richest first, capped
per day. Capped runs resume automatically the next morning.
"""

import sys

from . import db
from .config import REPO_ROOT, CostCapExceeded, CostMeter, anthropic_api_key
from .context import score_sale_context
from .dedupe import run_dedupe
from .ingest import add_estatesales
from .mailer import send_digest
from .stage1 import run_stage1
from .stage2 import run_stage2

sys.path.insert(0, str(REPO_ROOT / "src"))

MIN_PHOTOS = 20


def pick_new_sales(active: list[dict], known_ids: set[int],
                   watchlist_zips, min_photos: int, max_new: int) -> list[int]:
    """Pure selection logic (unit-tested): watchlist ZIP, enough photos,
    not already ingested; photo-richest first."""
    candidates = [
        s for s in active
        if s.get("id") not in known_ids
        and s.get("postalCodeNumber") in watchlist_zips
        and (s.get("pictureCount") or 0) >= min_photos
    ]
    candidates.sort(key=lambda s: -(s.get("pictureCount") or 0))
    return [s["id"] for s in candidates[:max_new]]


def _process_sale(conn, sale_id: int, meter: CostMeter) -> dict:
    s1 = run_stage1(conn, sale_id, meter)
    merged = run_dedupe(conn, sale_id)
    s2 = run_stage2(conn, sale_id, meter)
    return {"photos": s1["photos"], "works": merged, "screened": s2["screened"],
            "capped": bool(s1["skipped_cost"] or s2["skipped_cost"])}


def run_auto(conn, max_new: int = 2, daily_cap: float = 5.0,
             email: bool = True) -> None:
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", anthropic_api_key())
    from estatesales_client import get_all_active_sales
    from watchlist import WATCHLIST_ZIPS

    meter = CostMeter(daily_cap)
    touched: list[int] = []

    # 1. resume sales left unfinished by an earlier capped run
    unfinished = [r["id"] for r in conn.execute(
        "SELECT DISTINCT s.id FROM sales s WHERE s.platform='estatesales.net' AND ("
        " EXISTS (SELECT 1 FROM photos p WHERE p.sale_id=s.id"
        "         AND p.stage1_status IN ('pending','skipped_cost'))"
        " OR EXISTS (SELECT 1 FROM works w WHERE w.sale_id=s.id AND w.status='queued'))")]
    for sid in unfinished:
        conn.execute("UPDATE photos SET stage1_status='pending'"
                     " WHERE sale_id=? AND stage1_status='skipped_cost'", (sid,))
        conn.commit()
        print(f"== resuming sale {sid} ==")
        try:
            stats = _process_sale(conn, sid, meter)
            touched.append(sid)
            print(f"   {stats}")
        except CostCapExceeded:
            print("   daily budget exhausted while resuming")
            break

    # 2. discover new watchlist sales
    if meter.total < meter.cap:
        try:
            active = get_all_active_sales()
        except Exception as e:
            print(f"sale discovery failed: {e}")
            active = []
        known = {r["id"] for r in conn.execute("SELECT id FROM sales")}
        for sid in pick_new_sales(active, known, WATCHLIST_ZIPS, MIN_PHOTOS, max_new):
            print(f"== new watchlist sale {sid} ==")
            try:
                add_estatesales(conn, sid)
                score_sale_context(conn, sid, meter)
                stats = _process_sale(conn, sid, meter)
                touched.append(sid)
                print(f"   {stats}")
            except CostCapExceeded:
                print("   daily budget exhausted — will resume tomorrow")
                touched.append(sid)
                break
            except Exception as e:
                print(f"   sale {sid} failed: {str(e)[:150]}")

    print(f"== auto done: {len(touched)} sales, ${meter.total:.2f} ==")
    if email and touched:
        send_digest(conn, touched, meter.total)
