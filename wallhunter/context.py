"""Lightweight estate-context scoring: does this sale look like a collector's?

One cheap Haiku call per sale (description text + a few sampled photos).
The score (0-1) never changes a work's interest_score - it only boosts
queue ordering, so a strong work in a weak-context sale still surfaces.
"""

import json
import re

import anthropic

from .config import STAGE1_MODEL, CostMeter
from .images import downscale_jpeg_b64, load

PROMPT = """You are assessing an estate sale for COLLECTOR CONTEXT - how likely this
household belonged to a serious art collector, artist, dealer, curator, or academic.
Positive signals: dense multi-work walls ("salon hang"), art reference books,
studio materials, quality/consistent framing, museum or gallery mentions,
professional titles in the text, named collections. Negative: sparse mass-market
decor, big-box furnishings, no art mentioned or visible.
Sale description (may be empty): {desc}
Return ONLY JSON: {{"context_score": 0.0-1.0, "note": "one line why"}}"""

_JSON = re.compile(r"\{.*\}", re.DOTALL)


def _text_of(response) -> str:
    return "".join(b.text for b in response.content
                   if getattr(b, "type", "") == "text").strip()


def score_sale_context(conn, sale_id: int, meter: CostMeter,
                       max_photos: int = 3) -> float:
    sale = conn.execute("SELECT description, context_score FROM sales WHERE id=?",
                        (sale_id,)).fetchone()
    if sale is None:
        return 0.0
    photos = conn.execute(
        "SELECT file_hash FROM photos WHERE sale_id=? ORDER BY id", (sale_id,)).fetchall()
    # sample evenly across the gallery
    step = max(1, len(photos) // max_photos) if photos else 1
    sample = [photos[i]["file_hash"] for i in range(0, len(photos), step)][:max_photos]

    content = []
    for h in sample:
        try:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg",
                "data": downscale_jpeg_b64(load(h), 512, quality=70)}})
        except Exception:
            continue
    desc = re.sub(r"<[^>]+>", " ", sale["description"] or "")[:1200]
    content.append({"type": "text", "text": PROMPT.format(desc=desc or "(none)")})

    client = anthropic.Anthropic()
    try:
        resp = client.messages.create(model=STAGE1_MODEL, max_tokens=300,
                                      messages=[{"role": "user", "content": content}])
        meter.add(STAGE1_MODEL, resp.usage)
        m = _JSON.search(_text_of(resp))
        parsed = json.loads(m.group(0)) if m else {}
        score = max(0.0, min(1.0, float(parsed.get("context_score", 0))))
        note = str(parsed.get("note", ""))[:200]
    except Exception as e:
        score, note = 0.0, f"context scoring failed: {str(e)[:80]}"
    # never lower a score already pinned high (e.g. by named-collection research)
    conn.execute("UPDATE sales SET context_score=MAX(COALESCE(context_score,0), ?),"
                 " context_note=? WHERE id=?", (score, note, sale_id))
    conn.commit()
    return score
