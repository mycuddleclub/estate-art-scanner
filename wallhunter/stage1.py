"""Stage 1 — high-recall artwork detection with bounding boxes (Haiku)."""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic

from . import db
from .config import STAGE1_MAX_EDGE, STAGE1_MODEL, CostCapExceeded, CostMeter
from .images import crop_fraction_box, dhash, downscale_jpeg_b64, load, save_crop

DETECT_PROMPT = """Find every artwork visible anywhere in this estate-sale photo: \
paintings, drawings, watercolors, fine prints, photographs, sculpture, studio \
ceramics/pottery, textile art. Include works in the background, hanging on walls, \
leaning against furniture, stacked, partially visible, or reflected. High recall: \
when uncertain whether something is an artwork, include it and mark it uncertain. \
Exclude mirrors, TVs, windows, and obvious commercial posters.
Return ONLY this JSON, no other text:
{"artworks": [{"box": [x, y, w, h], "type": "painting|print|drawing|photo|sculpture|ceramic|textile|unknown", \
"desc": "one line incl. any visible signature/label text", "sig_visible": true/false, \
"label_visible": true/false, "prominence": "featured|background", "uncertain": true/false}], \
"photo_note": "one line: what this photo mainly shows"}
box values are fractions (0-1) of image width/height: [left, top, width, height]."""

_JSON = re.compile(r"\{.*\}", re.DOTALL)


def _text_of(response) -> str:
    return "".join(b.text for b in response.content
                   if getattr(b, "type", "") == "text").strip()


def _detect_one(client, meter: CostMeter, b64: str) -> tuple[dict, float]:
    cost = 0.0
    messages = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
        {"type": "text", "text": DETECT_PROMPT},
    ]}]
    for attempt in range(2):
        resp = client.messages.create(model=STAGE1_MODEL, max_tokens=1500, messages=messages)
        cost += meter.add(STAGE1_MODEL, resp.usage)
        txt = _text_of(resp)
        m = _JSON.search(txt)
        if m:
            try:
                parsed = json.loads(m.group(0))
                if isinstance(parsed.get("artworks"), list):
                    return parsed, cost
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
    raise ValueError("stage1: model returned unparseable output twice")


def _valid_box(b) -> tuple | None:
    try:
        x, y, w, h = (float(v) for v in b)
    except (TypeError, ValueError):
        return None
    if w <= 0.005 or h <= 0.005 or x < 0 or y < 0 or x + w > 1.02 or y + h > 1.02:
        return None
    return (min(x, 1.0), min(y, 1.0), min(w, 1.0), min(h, 1.0))


def run_stage1(conn, sale_id: int, meter: CostMeter, workers: int = 3) -> dict:
    client = anthropic.Anthropic()
    rows = conn.execute(
        "SELECT id, file_hash FROM photos WHERE sale_id=? AND stage1_status='pending'",
        (sale_id,)).fetchall()
    stats = {"photos": 0, "detections": 0, "failed": 0, "skipped_cost": 0}
    capped = False

    def job(row):
        img = load(row["file_hash"])
        b64 = downscale_jpeg_b64(img, STAGE1_MAX_EDGE)
        parsed, cost = _detect_one(client, meter, b64)
        return row, img, parsed, cost

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(job, row): row for row in rows}
        for fut in as_completed(futures):
            row = futures[fut]
            if capped:
                conn.execute("UPDATE photos SET stage1_status='skipped_cost' WHERE id=?",
                             (row["id"],))
                stats["skipped_cost"] += 1
                continue
            try:
                row, img, parsed, cost = fut.result()
            except CostCapExceeded:
                capped = True
                conn.execute("UPDATE photos SET stage1_status='skipped_cost' WHERE id=?",
                             (row["id"],))
                stats["skipped_cost"] += 1
                conn.commit()
                continue
            except Exception as e:
                conn.execute("UPDATE photos SET stage1_status='failed' WHERE id=?", (row["id"],))
                stats["failed"] += 1
                conn.commit()
                print(f"  photo {row['id']} failed: {str(e)[:120]}")
                continue

            for art in parsed.get("artworks", []):
                box = _valid_box(art.get("box"))
                if not box:
                    continue
                crop = crop_fraction_box(img, box)
                crop_hash, area = save_crop(crop)
                conn.execute(
                    "INSERT INTO detections (photo_id, bbox_x, bbox_y, bbox_w, bbox_h,"
                    " coarse_type, description, sig_visible, label_visible, prominence,"
                    " uncertain, crop_hash, dhash, crop_area) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["id"], *box, str(art.get("type", "unknown"))[:20],
                     str(art.get("desc", ""))[:500],
                     1 if art.get("sig_visible") else 0,
                     1 if art.get("label_visible") else 0,
                     str(art.get("prominence", "featured"))[:12],
                     1 if art.get("uncertain") else 0,
                     crop_hash, dhash(crop), area),
                )
                stats["detections"] += 1
            conn.execute(
                "UPDATE photos SET stage1_status='done', stage1_cost_usd=?, photo_note=? WHERE id=?",
                (cost, str(parsed.get("photo_note", ""))[:300], row["id"]))
            conn.commit()
            stats["photos"] += 1
            if stats["photos"] % 20 == 0:
                print(f"  stage1: {stats['photos']}/{len(rows)} photos, "
                      f"{stats['detections']} detections, ${meter.total:.2f}")
    return stats
