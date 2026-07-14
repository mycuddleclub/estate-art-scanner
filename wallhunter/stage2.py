"""Stage 2 — per-work screening (Sonnet): medium, period, quality, flags, score."""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic

from . import db
from .config import (
    STAGE2_CONTEXT_MAX_EDGE, STAGE2_CROP_MAX_EDGE, STAGE2_MODEL,
    TIER_A_MIN, TIER_B_MIN, CostCapExceeded, CostMeter,
)
from .images import downscale_jpeg_b64, load

SCREEN_PROMPT = """You are screening ONE artwork found in an estate-sale photo for a \
collector who hunts OVERLOOKED and UNDERRECOGNIZED art: documented-but-forgotten \
artists, folk/self-taught/outsider work, regional schools, works on paper, studio \
ceramics, unfashionable periods. Image 1 is the cropped artwork; image 2 is the \
full source photo for context.

Rules: state the VISUAL BASIS for every judgment; transcribe any visible signature, \
label, inscription, or stamp, even partially; NEVER attribute the work to a named \
artist (signature transcription is fine, attribution is not); if image quality \
limits you, say so and cap your score; do not dismiss work for being unfashionable, \
naive, regional, or decorative-looking.

Return ONLY this JSON:
{"medium_guess": {"value": "...", "basis": "..."},
 "period_guess": {"value": "...", "basis": "..."},
 "category": "painting|work_on_paper|print|photograph|sculpture|ceramics|textile|glass|jewelry|metalware|furniture|decor|book|other",
 "subject": "...",
 "quality_notes": "...",
 "sig_text": "transcription of any visible signature/label text, or null",
 "interest_score": 0.0-10.0,
 "flags": {"sig_visible": bool, "label_visible": bool, "verso_visible": bool,
           "repro_suspect": bool, "background_only": bool,
           "background_context": "... or null"},
 "uncertainties": ["..."]}
Scoring: 9-10 = strong evidence of a serious, possibly documented hand (score it \
even if condition is poor); 7-8 = confident, skilled original worth researching; \
4-6 = competent original of uncertain merit; 1-3 = likely reproduction/poster/decor."""

_JSON = re.compile(r"\{.*\}", re.DOTALL)


def _text_of(response) -> str:
    return "".join(b.text for b in response.content
                   if getattr(b, "type", "") == "text").strip()


def _screen_one(client, meter: CostMeter, crop_b64: str, ctx_b64: str,
                detection_desc: str, prominence: str) -> tuple[dict, float]:
    cost = 0.0
    intro = (f"Detector's note: \"{detection_desc}\" (prominence: {prominence}). "
             "Judge from the images yourself; the note may be wrong.")
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "Image 1 — the artwork crop:"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": crop_b64}},
        {"type": "text", "text": "Image 2 — full source photo for context:"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": ctx_b64}},
        {"type": "text", "text": intro + "\n\n" + SCREEN_PROMPT},
    ]}]
    for attempt in range(2):
        resp = client.messages.create(
            model=STAGE2_MODEL, max_tokens=1200,
            # Sonnet 5 defaults to adaptive thinking, which shares max_tokens
            # and can starve the JSON output on image-heavy calls.
            thinking={"type": "disabled"},
            messages=messages)
        cost += meter.add(STAGE2_MODEL, resp.usage)
        txt = _text_of(resp)
        m = _JSON.search(txt)
        if m:
            try:
                return json.loads(m.group(0)), cost
            except json.JSONDecodeError as e:
                err = str(e)
        else:
            err = "no JSON object found"
        if attempt == 0:
            messages = messages + [
                {"role": "assistant", "content": txt or "(empty)"},
                {"role": "user", "content":
                    f"Your response was not valid JSON ({err}). Reply with ONLY the JSON object."},
            ]
    raise ValueError("stage2: model returned unparseable output twice")


def _tier(score: float) -> str:
    if score >= TIER_A_MIN:
        return "A"
    if score >= TIER_B_MIN:
        return "B"
    return "C"


def pending_works_best_first(conn, sale_id: int):
    """Queued works ordered by stage-1 promise, so a cost-capped run screens
    the most promising detections first and leaves only the weak tail for
    the next day's resume."""
    return conn.execute(
        "SELECT w.id AS work_id, d.crop_hash, d.description, d.prominence, d.uncertain,"
        "       p.file_hash AS photo_hash"
        " FROM works w JOIN detections d ON d.id = w.best_detection_id"
        " JOIN photos p ON p.id = d.photo_id"
        " WHERE w.sale_id=? AND w.status='queued'"
        " ORDER BY (d.sig_visible*3.0 + d.label_visible*2.0"
        "   + CASE WHEN d.prominence='background' THEN 1.0 ELSE 0 END"
        "   + CASE d.coarse_type WHEN 'painting' THEN 2.0 WHEN 'drawing' THEN 1.5"
        "       WHEN 'sculpture' THEN 1.0 WHEN 'ceramic' THEN 1.0"
        "       WHEN 'unknown' THEN 0.75 ELSE 0.5 END) DESC,"
        "   d.crop_area DESC",
        (sale_id,)).fetchall()


def run_stage2(conn, sale_id: int, meter: CostMeter, workers: int = 3) -> dict:
    client = anthropic.Anthropic()
    rows = pending_works_best_first(conn, sale_id)
    stats = {"screened": 0, "failed": 0, "skipped_cost": 0}
    capped = False

    def job(row):
        crop_b64 = downscale_jpeg_b64(load(row["crop_hash"]), STAGE2_CROP_MAX_EDGE)
        ctx_b64 = downscale_jpeg_b64(load(row["photo_hash"]), STAGE2_CONTEXT_MAX_EDGE, quality=70)
        parsed, cost = _screen_one(client, meter, crop_b64, ctx_b64,
                                   row["description"] or "", row["prominence"] or "featured")
        return row, parsed, cost

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(job, row): row for row in rows}
        for fut in as_completed(futures):
            row = futures[fut]
            if capped:
                stats["skipped_cost"] += 1
                continue
            try:
                row, a, cost = fut.result()
            except CostCapExceeded:
                capped = True
                stats["skipped_cost"] += 1
                continue
            except Exception as e:
                conn.execute("UPDATE works SET status='failed' WHERE id=?", (row["work_id"],))
                stats["failed"] += 1
                conn.commit()
                print(f"  work {row['work_id']} failed: {str(e)[:120]}")
                continue

            flags = a.get("flags") or {}
            try:
                score = max(0.0, min(10.0, float(a.get("interest_score", 0))))
            except (TypeError, ValueError):
                score = 0.0
            medium = a.get("medium_guess") or {}
            period = a.get("period_guess") or {}
            from .config import WORK_CATEGORIES
            category = str(a.get("category", "other")).strip().lower()
            if category not in WORK_CATEGORIES:
                category = "other"
            conn.execute(
                "UPDATE works SET category=?, medium_guess=?, medium_basis=?, period_guess=?,"
                " period_basis=?, subject=?, quality_notes=?, sig_text=?, interest_score=?,"
                " tier=?, sig_visible=?, label_visible=?, verso_visible=?, repro_suspect=?,"
                " background_only=?, background_context=?, uncertainties=?,"
                " stage2_cost_usd=?, status='screened' WHERE id=?",
                (category,
                 str(medium.get("value", ""))[:120], str(medium.get("basis", ""))[:300],
                 str(period.get("value", ""))[:120], str(period.get("basis", ""))[:300],
                 str(a.get("subject", ""))[:300], str(a.get("quality_notes", ""))[:600],
                 (str(a["sig_text"])[:300] if a.get("sig_text") else None),
                 score, _tier(score),
                 1 if flags.get("sig_visible") else 0,
                 1 if flags.get("label_visible") else 0,
                 1 if flags.get("verso_visible") else 0,
                 1 if flags.get("repro_suspect") else 0,
                 1 if flags.get("background_only") else 0,
                 (str(flags["background_context"])[:300]
                  if flags.get("background_context") else None),
                 json.dumps([str(u)[:200] for u in (a.get("uncertainties") or [])][:6]),
                 cost, row["work_id"]))
            conn.commit()
            stats["screened"] += 1
            if stats["screened"] % 10 == 0:
                print(f"  stage2: {stats['screened']}/{len(rows)} works, ${meter.total:.2f}")
    return stats
