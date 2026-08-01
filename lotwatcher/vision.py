"""Vision stage: look at the actual lot photo. Runs on Qwen 3.6 (multimodal
via its mmproj) — resident alongside the judge on the 96 GB carve, so no swap.

Only called for stage-3 candidates (a few hundred/cycle), never on raw lots.
Transcribes signatures (the #1 cataloguing-gap signal), identifies the object,
flags condition, and says whether the image matches the listing's claim.
"""
import base64
import json
import re

import httpx

from . import config

VISION_MODEL = config.STAGE1_MODEL   # qwen3.6-35b-a3b, multimodal
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

_PROMPT = """You are examining the photo of a single auction lot for an art collector who hunts overlooked fine/folk/self-taught art. Look closely at the actual image.

Listing says: {title}

Output STRICT JSON only:
{{"object": "what the item actually is, <=12 words",
  "is_artwork": true/false,
  "signature": "transcribe any visible signature/inscription/maker mark EXACTLY, or empty string",
  "signature_legible": true/false,
  "medium_seen": "painting|print|drawing|photo|sculpture|ceramic|textile|other|not_art",
  "condition": "any visible damage/wear/restoration, or 'no obvious issues'",
  "matches_listing": true/false,
  "note": "anything the listing text missed or undersold, <=20 words"}}"""


def full_size(url: str) -> str:
    """Strip the thumbnail size override so img.axd returns the original."""
    if not url:
        return url
    return re.sub(r"&h=\d+&w=\d+$", "", url)


def fetch_image_b64(url: str, max_bytes: int = 3_000_000) -> str | None:
    try:
        r = httpx.get(full_size(url), headers=_UA, timeout=30, follow_redirects=True)
        r.raise_for_status()
        data = r.content
        if not data or len(data) > max_bytes:
            return None
        return base64.b64encode(data).decode()
    except Exception:
        return None


def read_lot(image_url: str, title: str) -> dict | None:
    """Vision read of one lot photo. Returns parsed dict or None on failure."""
    b64 = fetch_image_b64(image_url)
    if not b64:
        return None
    try:
        r = httpx.post(f"{config.LM_BASE}/chat/completions", timeout=180, json={
            "model": VISION_MODEL,
            "max_tokens": 500,
            "temperature": 0.1,
            "reasoning_effort": "none",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": _PROMPT.format(title=title[:200])},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]}],
        })
        r.raise_for_status()
        text = r.json()["choices"][0]["message"].get("content") or ""
        m = re.search(r"\{.*\}", text, re.DOTALL)
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def evidence_line(v: dict) -> str:
    """Render the vision result as an evidence line for the judge."""
    if not v:
        return ""
    parts = [f"VISION: {v.get('object', '?')}"]
    sig = (v.get("signature") or "").strip()
    if sig:
        leg = "legible" if v.get("signature_legible") else "partial"
        parts.append(f"signature seen ({leg}): \"{sig}\"")
    med = v.get("medium_seen")
    if med and med != "not_art":
        parts.append(f"looks like {med}")
    cond = (v.get("condition") or "").strip()
    if cond and cond.lower() not in ("no obvious issues", "none", ""):
        parts.append(f"condition: {cond}")
    if v.get("matches_listing") is False:
        parts.append("IMAGE DOES NOT MATCH LISTING")
    note = (v.get("note") or "").strip()
    if note:
        parts.append(note)
    return " | ".join(parts)
