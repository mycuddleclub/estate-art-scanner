"""Morning auto-pipeline: resume capped sales, ingest new watchlist sales,
screen everything within a daily budget, email the digest.

Sale selection is deliberately conservative: watchlist ZIPs only (shared with
the nightly scanner), minimum photo count, newest-photo-richest first, capped
per day. Capped runs resume automatically the next morning.
"""

import sys

from datetime import datetime, timezone

from . import db
from .config import REPO_ROOT, CostCapExceeded, CostMeter, anthropic_api_key
from .context import score_sale_context
from .dedupe import run_dedupe
from .dossier import research_sale_identity
from .ingest import add_estatesales
from .mailer import send_digest
from .stage1 import run_stage1
from .stage2 import run_stage2

sys.path.insert(0, str(REPO_ROOT / "src"))

MIN_PHOTOS = 20
REFRESH_GROWTH_MIN = 5   # re-ingest an active sale when it gained this many photos


PHOTO_RANK_SATURATION = 400
# EstateSales.net sale types (probed 2026-07-14): 1 = in-person estate sale,
# 4 = moving/tag sale, 64 = online auction catalog, 2 = live auction.
# In-person estate/moving sales are the informational edge (uncatalogued,
# chaotic galleries); auction catalogs are itemized, competitive, and 5-10x
# the screening cost - only picked when nothing better exists.
PREFERRED_SALE_TYPES = {1, 4}


def pick_new_sales(active: list[dict], known_ids: set[int],
                   watchlist_zips, min_photos: int, max_new: int) -> list[int]:
    """Pure selection logic (unit-tested): watchlist ZIP, enough photos, not
    already ingested. In-person estate/moving sales rank ahead of auction
    catalogs. Photo-richer ranks higher, but the count saturates at
    PHOTO_RANK_SATURATION so 1,200-photo monsters don't always win; above the
    saturation point, smaller (finishable-today) sales win the tie."""
    candidates = [
        s for s in active
        if s.get("id") not in known_ids
        and s.get("postalCodeNumber") in watchlist_zips
        and (s.get("pictureCount") or 0) >= min_photos
    ]
    candidates.sort(key=lambda s: (
        0 if s.get("type") in PREFERRED_SALE_TYPES else 1,
        -min(s.get("pictureCount") or 0, PHOTO_RANK_SATURATION),
        s.get("pictureCount") or 0))
    return [s["id"] for s in candidates[:max_new]]


def drop_excluded_auctions(details: list[dict], hosts=None) -> list[int]:
    """Pure (unit-tested): sale ids whose auctionUrl does NOT point at an
    excluded platform (e.g. LiveAuctioneers, which Daniel reviews manually)."""
    from .config import EXCLUDE_AUCTION_HOSTS
    hosts = hosts if hosts is not None else EXCLUDE_AUCTION_HOSTS
    out = []
    for d in details:
        url = (d.get("auctionUrl") or "").lower()
        if any(h in url for h in hosts):
            print(f"   skipping sale {d.get('id')} — cross-listed on"
                  f" {next(h for h in hosts if h in url)} (covered manually)")
            continue
        out.append(d["id"])
    return out


def sales_needing_refresh(ours: list[dict], details: list[dict],
                          min_growth: int = REFRESH_GROWTH_MIN) -> list[int]:
    """Pure (unit-tested): active ingested sales whose platform photo count
    grew past what we hold -> re-ingest the delta."""
    held = {s["id"]: s["held_photos"] for s in ours}
    out = []
    for d in details:
        sid = d.get("id")
        if sid in held and (d.get("pictureCount") or 0) >= held[sid] + min_growth:
            out.append(sid)
    return out


def refresh_grown_sales(conn) -> list[int]:
    """Check active (not-yet-ended) EstateSales.net sales for late-added
    photos; delta-ingest any that grew. Costs one details call per 50 sales."""
    from estatesales_client import get_sale_details_batch
    now_iso = datetime.now(timezone.utc).isoformat()
    ours = [dict(r) for r in conn.execute(
        "SELECT s.id, (SELECT COUNT(*) FROM photos p WHERE p.sale_id=s.id) held_photos"
        " FROM sales s WHERE s.platform='estatesales.net'"
        " AND (s.ends_at IS NULL OR s.ends_at >= ?)", (now_iso[:19],))]
    if not ours:
        return []
    details = get_sale_details_batch([s["id"] for s in ours])
    grown = sales_needing_refresh(ours, details)
    for sid in grown:
        print(f"== sale {sid} added photos — delta-ingesting ==")
        try:
            add_estatesales(conn, sid)   # delta-aware: fetches only new photos
        except Exception as e:
            print(f"   refresh of {sid} failed: {str(e)[:120]}")
    return grown


def _process_sale(conn, sale_id: int, meter: CostMeter) -> dict:
    s1 = run_stage1(conn, sale_id, meter)
    merged = run_dedupe(conn, sale_id)
    s2 = run_stage2(conn, sale_id, meter)
    return {"photos": s1["photos"], "works": merged, "screened": s2["screened"],
            "capped": bool(s1["skipped_cost"] or s2["skipped_cost"])}


def run_auto(conn, max_new: int = 2, daily_cap: float = 5.0,
             per_sale_cap: float | None = None, email: bool = True) -> None:
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", anthropic_api_key())
    from estatesales_client import get_all_active_sales
    from watchlist import WATCHLIST_ZIPS

    # A single sale may spend at most half the daily budget, so one monster
    # catalog can't starve the day's other sales; its remainder resumes
    # tomorrow, best detections first (see stage2.pending_works_best_first).
    per_sale_cap = per_sale_cap or max(1.0, daily_cap / 2)
    spent = 0.0
    touched: list[int] = []

    def process_with_slice(sid: int, *, ingest: bool = False) -> bool:
        """Run one sale within its budget slice. Returns False when the
        daily budget is exhausted."""
        nonlocal spent
        remaining = daily_cap - spent
        if remaining < 0.25:
            return False
        meter = CostMeter(min(per_sale_cap, remaining))
        try:
            if ingest:
                add_estatesales(conn, sid)
                research_sale_identity(conn, sid, meter)
                score_sale_context(conn, sid, meter)
            stats = _process_sale(conn, sid, meter)
            print(f"   {stats} (${meter.total:.2f})")
            touched.append(sid)
        except CostCapExceeded:
            print(f"   sale {sid} hit its budget slice (${meter.total:.2f})"
                  " — best works screened, tail resumes tomorrow")
            touched.append(sid)
        except Exception as e:
            print(f"   sale {sid} failed: {str(e)[:150]}")
        finally:
            spent += meter.total
        return daily_cap - spent >= 0.25

    # 0. late-added photos: delta-ingest active sales that grew; their new
    #    'pending' photos make them "unfinished" and step 1 processes them
    try:
        refresh_grown_sales(conn)
    except Exception as e:
        print(f"photo refresh failed: {str(e)[:120]}")

    # 0.5 backfill identity research for sales that predate the feature
    #     (free unless a name pattern fires; those cost ~$0.15 each, once)
    id_meter = CostMeter(0.75)
    for r in conn.execute("SELECT id FROM sales WHERE identity_verdict IS NULL"
                          " AND id > 0").fetchall():
        try:
            research_sale_identity(conn, r["id"], id_meter)
        except CostCapExceeded:
            break
        except Exception as e:
            print(f"identity backfill {r['id']} failed: {str(e)[:100]}")
    spent += id_meter.total

    # 1. resume sales left unfinished by an earlier capped run
    unfinished = [r["id"] for r in conn.execute(
        "SELECT DISTINCT s.id FROM sales s WHERE s.platform='estatesales.net' AND ("
        " EXISTS (SELECT 1 FROM photos p WHERE p.sale_id=s.id"
        "         AND p.stage1_status IN ('pending','skipped_cost'))"
        " OR EXISTS (SELECT 1 FROM works w WHERE w.sale_id=s.id AND w.status='queued'))")]
    budget_ok = True
    for sid in unfinished:
        conn.execute("UPDATE photos SET stage1_status='pending'"
                     " WHERE sale_id=? AND stage1_status='skipped_cost'", (sid,))
        conn.commit()
        print(f"== resuming sale {sid} ==")
        budget_ok = process_with_slice(sid)
        if not budget_ok:
            break

    # 2. discover new watchlist sales
    if budget_ok:
        try:
            active = get_all_active_sales()
        except Exception as e:
            print(f"sale discovery failed: {e}")
            active = []
        from estatesales_client import get_sale_details_batch
        known = {r["id"] for r in conn.execute("SELECT id FROM sales")}
        # over-pick, then drop cross-listed LiveAuctioneers auctions etc.
        shortlist = pick_new_sales(active, known, WATCHLIST_ZIPS, MIN_PHOTOS,
                                   max_new * 3)
        allowed = drop_excluded_auctions(get_sale_details_batch(shortlist)) \
            if shortlist else []
        # preserve the picker's ranking order
        for sid in [s for s in shortlist if s in allowed][:max_new]:
            print(f"== new watchlist sale {sid} ==")
            if not process_with_slice(sid, ingest=True):
                break

    print(f"== auto done: {len(touched)} sales, ${spent:.2f} ==")
    if email and touched:
        send_digest(conn, touched, spent)
