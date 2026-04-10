"""
EstateSales.NET API client.

Endpoints discovered via browser devtools - unauthenticated, no API key required.
All endpoints return JSON with x_xsrf: X_XSRF header (though even this is optional).

If these endpoints break, check:
1. Whether estatesales.net has updated their JS bundle (version in URL changes)
2. Whether they've added real authentication
3. Run browser devtools session again on /MI/Detroit to re-discover
"""

import requests
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://www.estatesales.net"
HEADERS = {
    "accept": "application/json, text/plain, */*",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "x_xsrf": "X_XSRF",
}

# US geographic center - covers all 48 contiguous states at 2500 mile radius
US_CENTER_LAT = 39.5
US_CENTER_LNG = -98.35
US_RADIUS_MILES = 2500

# Max IDs per batch request
BATCH_SIZE = 50


def get_all_active_sales(published_within_hours: Optional[int] = None) -> list[dict]:
    """
    Fetch all active estate sales nationwide.

    Args:
        published_within_hours: If set, only return sales published within this window.
                                 Use 48 for daily runs (catches overnight + timezone gaps).

    Returns:
        List of sale dicts with id, stateCode, cityName, postalCodeNumber,
        utcDateFirstPublished, pictureCount.
    """
    url = (
        f"{BASE_URL}/api/sale-details"
        f"?bypass=bycoordinatesanddistance:{US_CENTER_LAT}_{US_CENTER_LNG}_{US_RADIUS_MILES}"
        f"&select=id,stateCode,cityName,postalCodeNumber,utcDateFirstPublished,pictureCount,type"
        f"&explicitTypes=DateTime"
    )

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        sales = resp.json()
        logger.info(f"Fetched {len(sales)} total active sales nationwide")
    except Exception as e:
        logger.error(f"Failed to fetch active sales: {e}")
        raise

    if published_within_hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=published_within_hours)
        before = len(sales)
        sales = [
            s for s in sales
            if s.get("utcDateFirstPublished")
            and datetime.fromisoformat(
                s["utcDateFirstPublished"]["_value"].replace("Z", "+00:00")
            ) > cutoff
        ]
        logger.info(f"Filtered to {len(sales)} sales published in last {published_within_hours}h (was {before})")

    return sales


def get_sale_details_batch(sale_ids: list[int]) -> list[dict]:
    """
    Fetch full metadata + main photo for a batch of sale IDs.
    Automatically chunks into groups of BATCH_SIZE.

    Returns list of sale detail dicts.
    """
    all_results = []

    for i in range(0, len(sale_ids), BATCH_SIZE):
        chunk = sale_ids[i:i + BATCH_SIZE]
        ids_str = ",".join(str(sid) for sid in chunk)

        url = (
            f"{BASE_URL}/api/sale-details"
            f"?bypass=byids:{ids_str}"
            f"&include=mainpicture,dates"
            f"&select=id,orgName,name,address,cityName,postalCodeNumber,stateCode,"
            f"pictureCount,latitude,longitude,firstLocalStartDate,lastLocalEndDate,"
            f"utcDateFirstPublished,utcDateModified,orgWebsite,phoneNumbers,"
            f"showPhoneNumber,orgId,orgPageUrl,orgLogoUrl,auctionUrl"
            f"&explicitTypes=DateTime"
        )

        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            results = resp.json()
            all_results.extend(results)
            logger.info(f"Fetched details for batch {i//BATCH_SIZE + 1}: {len(results)} sales")
        except Exception as e:
            logger.error(f"Failed to fetch batch {i//BATCH_SIZE + 1}: {e}")
            continue

        # Be polite between batches
        if i + BATCH_SIZE < len(sale_ids):
            time.sleep(1)

    return all_results


def get_sale_full(sale_id: int) -> Optional[dict]:
    """
    Fetch complete sale data including ALL photo URLs and full HTML description.

    Returns the full sale dict or None on failure.
    """
    import json as json_lib
    query = json_lib.dumps({"saleId": sale_id, "userId": None, "isSuper": False})

    url = (
        f"{BASE_URL}/api/legacy/queries/traditional-sales/traditional-sale"
        f"?query={requests.utils.quote(query)}"
        f"&explicitTypes=DateTime"
    )

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("sale")
    except Exception as e:
        logger.error(f"Failed to fetch full sale {sale_id}: {e}")
        return None


def get_sale_url(sale: dict) -> str:
    """Build the public URL for a sale."""
    state = sale.get("stateCode", "")
    city = sale.get("cityName", "").replace(" ", "-")
    zip_code = sale.get("postalCodeNumber", "")
    sale_id = sale.get("id", "")
    return f"{BASE_URL}/{state}/{city}/{zip_code}/{sale_id}"


def get_thumbnail_urls(sale_full: dict) -> list[str]:
    """Extract all thumbnail URLs from a full sale object."""
    pictures = sale_full.get("pictures", [])
    return [p["thumbnailUrl"] for p in pictures if p.get("thumbnailUrl")]


def get_fullres_urls(sale_full: dict) -> list[str]:
    """Extract full-resolution photo URLs from a full sale object."""
    pictures = sale_full.get("pictures", [])
    return [p["url"] for p in pictures if p.get("url")]
