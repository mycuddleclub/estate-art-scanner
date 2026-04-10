"""
Main estate art scanner.

Run nightly via GitHub Actions. Workflow:
1. Fetch all new US estate sales (published in last 48h)
2. Filter to watchlist zip codes
3. For qualifying sales: fetch all photos + description
4. Vision AI: identify art photos, assess collection quality
5. Email alert with results

Also maintains a "pending" list of watchlist sales that had too few photos
when first seen. These are re-checked each run — estate sale companies often
list a sale days before uploading photos.

Environment variables required:
    OPENAI_API_KEY       - OpenAI API key for GPT-4o Vision
    GMAIL_USER           - Gmail address to send from
    GMAIL_APP_PASSWORD   - Gmail app password (not your login password)
    ALERT_EMAIL          - Email address to send alerts to
"""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic

from estatesales_client import (
    get_all_active_sales,
    get_sale_details_batch,
    get_sale_full,
    get_sale_url,
    get_thumbnail_urls,
)
from emailer import build_email_html, send_email
from vision import assess_collection_quality, filter_art_photos
from watchlist import WATCHLIST_ZIPS, score_description

# ── Config ────────────────────────────────────────────────────────────────────
PUBLISHED_WITHIN_HOURS = 48     # Catch sales from last 2 days (handles timezone gaps)
MIN_PHOTO_COUNT = 12            # Skip sales with too few photos
MIN_ART_PHOTOS_FOR_ASSESSMENT = 2
PENDING_MAX_AGE_DAYS = 14       # Drop pending sales older than this
PENDING_MIN_PHOTO_JUMP = 5      # Re-process pending sale if photo count grew by this much

DATA_DIR = Path(__file__).parent.parent / "data"
SEEN_SALES_FILE = DATA_DIR / "seen_sales.json"
PENDING_SALES_FILE = DATA_DIR / "pending_sales.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Persistence ───────────────────────────────────────────────────────────────

def load_seen_sales() -> set[int]:
    """Load set of fully-processed sale IDs."""
    if SEEN_SALES_FILE.exists():
        try:
            return set(json.loads(SEEN_SALES_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_seen_sales(seen: set[int]):
    """Persist seen IDs. Cap at 10k to avoid unbounded growth."""
    DATA_DIR.mkdir(exist_ok=True)
    SEEN_SALES_FILE.write_text(json.dumps(sorted(seen)[-10000:]))


def load_pending_sales() -> dict:
    """
    Load pending sales — watchlist matches that had too few photos when first seen.

    Structure: { "sale_id": { "zip": str, "city": str, "state": str,
                               "first_seen": ISO str, "last_photo_count": int } }
    """
    if PENDING_SALES_FILE.exists():
        try:
            return json.loads(PENDING_SALES_FILE.read_text())
        except Exception:
            pass
    return {}


def save_pending_sales(pending: dict):
    DATA_DIR.mkdir(exist_ok=True)
    PENDING_SALES_FILE.write_text(json.dumps(pending, indent=2))


def add_to_pending(pending: dict, sale: dict, photo_count: int):
    """Add a low-photo sale to the pending watch list."""
    sale_id = str(sale["id"])
    pending[sale_id] = {
        "zip": sale.get("postalCodeNumber", ""),
        "city": sale.get("cityName", ""),
        "state": sale.get("stateCode", ""),
        "name": sale.get("name", ""),
        "first_seen": datetime.now(timezone.utc).isoformat(),
        "last_photo_count": photo_count,
    }
    logger.info(f"  → Added to pending ({photo_count} photos): {sale.get('name', sale_id)}")


def prune_pending(pending: dict) -> dict:
    """Remove pending sales older than PENDING_MAX_AGE_DAYS."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=PENDING_MAX_AGE_DAYS)
    before = len(pending)
    pending = {
        sid: data for sid, data in pending.items()
        if datetime.fromisoformat(data["first_seen"]) > cutoff
    }
    pruned = before - len(pending)
    if pruned:
        logger.info(f"Pruned {pruned} expired pending sales")
    return pending


# ── Main scan logic ───────────────────────────────────────────────────────────

def run_scan():
    # Validate environment
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    sendgrid_key = os.environ.get("SENDGRID_API_KEY")
    alert_email_from = os.environ.get("ALERT_EMAIL_FROM")
    alert_email = os.environ.get("ALERT_EMAIL_TO")

    if not all([anthropic_key, sendgrid_key, alert_email_from, alert_email]):
        missing = [k for k, v in {
            "ANTHROPIC_API_KEY": anthropic_key,
            "SENDGRID_API_KEY": sendgrid_key,
            "ALERT_EMAIL_FROM": alert_email_from,
            "ALERT_EMAIL_TO": alert_email,
        }.items() if not v]
        raise EnvironmentError(f"Missing required env vars: {missing}")

    client = anthropic.Anthropic(api_key=anthropic_key)
    seen_sales = load_seen_sales()
    pending_sales = load_pending_sales()
    pending_sales = prune_pending(pending_sales)
    run_date = datetime.now(timezone.utc).strftime("%A, %B %d %Y")

    stats = {"total_new": 0, "in_watchlist": 0, "rechecked_pending": 0, "scanned": 0}
    alerts = []

    # ── Step 1: New sales (published in last 48h) ─────────────────────────────
    logger.info("── Step 1: Fetching new sales nationwide ──")
    all_new = get_all_active_sales(published_within_hours=PUBLISHED_WITHIN_HOURS)
    stats["total_new"] = len(all_new)

    truly_new = [s for s in all_new if s["id"] not in seen_sales]
    watchlist_new = [s for s in truly_new if s.get("postalCodeNumber") in WATCHLIST_ZIPS]
    logger.info(f"{len(watchlist_new)} new watchlist sales to evaluate")
    stats["in_watchlist"] = len(watchlist_new)

    # ── Step 2: Re-check pending sales ───────────────────────────────────────
    logger.info("── Step 2: Re-checking pending low-photo sales ──")
    pending_ids = [int(sid) for sid in pending_sales]

    if pending_ids:
        pending_details = get_sale_details_batch(pending_ids)
        pending_to_process = []

        for sale in pending_details:
            sid = str(sale["id"])
            prev = pending_sales.get(sid, {})
            prev_count = prev.get("last_photo_count", 0)
            curr_count = sale.get("pictureCount") or 0

            if curr_count >= MIN_PHOTO_COUNT and curr_count >= prev_count + PENDING_MIN_PHOTO_JUMP:
                logger.info(f"  Pending sale upgraded: [{sale['id']}] {sale.get('name','')} "
                            f"({prev_count} → {curr_count} photos)")
                pending_to_process.append(sale)
                pending_sales[sid]["last_photo_count"] = curr_count
            else:
                # Update photo count even if not ready yet
                pending_sales[sid]["last_photo_count"] = curr_count

        stats["rechecked_pending"] = len(pending_to_process)
        if pending_to_process:
            logger.info(f"{len(pending_to_process)} pending sales now have enough photos")
    else:
        pending_to_process = []
        logger.info("No pending sales to re-check")

    # ── Step 3: Get details for new watchlist sales ───────────────────────────
    logger.info("── Step 3: Fetching details for new watchlist sales ──")
    new_ids = [s["id"] for s in watchlist_new]
    new_details = get_sale_details_batch(new_ids) if new_ids else []

    # Split new details: enough photos → process, too few → pending
    new_to_process = []
    for sale in new_details:
        pic_count = sale.get("pictureCount") or 0
        if pic_count >= MIN_PHOTO_COUNT:
            new_to_process.append(sale)
        else:
            add_to_pending(pending_sales, sale, pic_count)
            seen_sales.add(sale["id"])  # Don't re-add to new next run; pending handles it

    logger.info(f"{len(new_to_process)} new sales ready for vision scan")
    logger.info(f"{len(new_details) - len(new_to_process)} added to pending (too few photos)")

    # ── Step 4: Vision scan ───────────────────────────────────────────────────
    all_to_scan = new_to_process + pending_to_process
    logger.info(f"── Step 4: Vision scanning {len(all_to_scan)} sales ──")

    processed_ids = set()

    for i, sale_detail in enumerate(all_to_scan):
        sale_id = sale_detail["id"]
        sale_name = sale_detail.get("name", f"Sale {sale_id}")
        city = sale_detail.get("cityName", "")
        state = sale_detail.get("stateCode", "")
        source = "PENDING" if str(sale_id) in pending_sales else "NEW"
        logger.info(f"[{i+1}/{len(all_to_scan)}] [{source}] {sale_name} — {city}, {state}")

        try:
            sale_full = get_sale_full(sale_id)
            if not sale_full:
                logger.warning(f"  Could not fetch full data, skipping")
                processed_ids.add(sale_id)
                continue

            thumbnail_urls = get_thumbnail_urls(sale_full)
            description = sale_full.get("htmlDescription", "") or ""
            desc_score = score_description(description)
            logger.info(f"  Photos: {len(thumbnail_urls)} | Description score: {desc_score}")

            stats["scanned"] += 1

            # Stage 1: filter to art photos
            art_photo_urls = filter_art_photos(thumbnail_urls, client)

            if len(art_photo_urls) < MIN_ART_PHOTOS_FOR_ASSESSMENT:
                logger.info(f"  Only {len(art_photo_urls)} art photos — skipping assessment")
                processed_ids.add(sale_id)
                time.sleep(1)
                continue

            # Stage 2: assess quality
            assessment = assess_collection_quality(art_photo_urls, description, client)

            if assessment["alert_worthy"]:
                logger.info(f"  ✓ ALERT: {assessment['priority']} — score {assessment['score']}/10")
                alerts.append({
                    "sale_detail": sale_detail,
                    "sale_full": sale_full,
                    "assessment": assessment,
                    "art_photo_urls": art_photo_urls,
                    "sale_url": get_sale_url(sale_detail),
                    "desc_score": desc_score,
                    "source": source,
                })
            else:
                logger.info(f"  ✗ Score {assessment['score']}/10 — below threshold")

        except Exception as e:
            logger.error(f"  Error processing {sale_id}: {e}")

        processed_ids.add(sale_id)
        time.sleep(2)

    # ── Step 5: Finalize and send ─────────────────────────────────────────────
    alerts.sort(key=lambda a: a["assessment"]["score"], reverse=True)
    logger.info(f"── Done: {len(alerts)} alerts from {stats['scanned']} scanned ──")

    # Remove fully processed sales from pending
    for sid in processed_ids:
        pending_sales.pop(str(sid), None)

    # Mark everything as seen
    seen_sales.update({s["id"] for s in truly_new})
    seen_sales.update(processed_ids)

    save_seen_sales(seen_sales)
    save_pending_sales(pending_sales)

    _send_email(alerts, run_date, stats, alert_email)


def _send_email(alerts, run_date, stats, alert_email):
    count = len(alerts)
    high = sum(1 for a in alerts if a["assessment"]["priority"] == "HIGH")

    if count == 0:
        subject = f"Estate Art Scanner — No alerts ({run_date})"
    elif high:
        subject = f"🚨 Estate Art Scanner — {count} alerts, {high} HIGH ({run_date})"
    else:
        subject = f"Estate Art Scanner — {count} alert{'s' if count > 1 else ''} ({run_date})"

    html = build_email_html(alerts, run_date, stats)
    send_email(html, subject, alert_email, count)


if __name__ == "__main__":
    run_scan()
