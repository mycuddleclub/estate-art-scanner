"""Standalone client for the local LLM endpoint (LM Studio / OpenAI-compatible).

CANONICAL SHARED FILE — external tools (the Checker, Art Scout, the scanners)
load this by path, same pattern as authority_client.py / prices_client.py:

    import importlib.util as _il
    _spec = _il.spec_from_file_location(
        "llm_client",
        os.path.expanduser("~/estate-art-scanner/wallhunter/llm_client.py"))
    llm_client = _il.module_from_spec(_spec)
    _spec.loader.exec_module(llm_client)

Dependency-free (stdlib urllib only — no openai SDK). Failures are silent and
neutral: if the endpoint is unreachable, read_lot()/judge_lot() return a dict
with available=False so callers fall back to their existing rules path, exactly
like a missing DB never breaks a consumer.

This is the T2 rewire — the one seam where every tool stops calling
api.anthropic.com and starts calling a model on localhost. Two calls:

    read_lot(lot)            stage 1 — cheap, runs on EVERY lot  (Qwen 3.6 35B)
    judge_lot(lot, evidence) stage 4 — deep, gated candidates    (GPT-OSS-120B)

Config via env (all optional):
    LOCAL_LLM_BASE_URL     default http://localhost:1234/v1
    LOCAL_LLM_READ_MODEL   stage-1 workhorse   (default "qwen3.6-35b-a3b")
    LOCAL_LLM_JUDGE_MODEL  stage-4 judge       (default "gpt-oss-120b")
    LOCAL_LLM_TIMEOUT      seconds             (default 120)
"""

import json
import os
import re
import urllib.error
import urllib.request

BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:1234/v1").rstrip("/")
READ_MODEL = os.getenv("LOCAL_LLM_READ_MODEL", "qwen3.6-35b-a3b")
JUDGE_MODEL = os.getenv("LOCAL_LLM_JUDGE_MODEL", "gpt-oss-120b")
TIMEOUT = float(os.getenv("LOCAL_LLM_TIMEOUT", "120"))


# --------------------------------------------------------------------------- #
#  low level
# --------------------------------------------------------------------------- #
def _chat(model, system, user, *, max_tokens=512, temperature=0.2,
          json_mode=True):
    """POST one chat completion. Returns the assistant text, or None on any
    error (endpoint down, timeout, bad status, malformed body)."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"]
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError,
            IndexError, ValueError, TimeoutError):
        return None


def _extract_json(text):
    """Pull the first JSON object out of a model reply. Tolerates ```json
    fences and leading/trailing prose. Returns a dict, or None."""
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(),
                  flags=re.I | re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    depth = start = 0
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except ValueError:
                    return None
    return None


def available():
    """True if the endpoint answers a models list. Cheap health check."""
    try:
        req = urllib.request.Request(f"{BASE_URL}/models", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


# --------------------------------------------------------------------------- #
#  lot field helpers — tolerant of the various shapes lots arrive in
# --------------------------------------------------------------------------- #
def _lot_text(lot):
    title = (lot.get("title") or "").strip()
    desc = (lot.get("description") or lot.get("desc")
            or lot.get("full_description") or "").strip()
    hint = (lot.get("claimed_artist") or lot.get("artist_name")
            or lot.get("artist") or "").strip()
    bid = lot.get("current_bid")
    est = lot.get("estimate")
    parts = [f"TITLE: {title}"]
    if hint:
        parts.append(f"LISTED ARTIST: {hint}")
    if desc:
        parts.append(f"DESCRIPTION: {desc[:1800]}")
    if bid:
        parts.append(f"CURRENT BID: {bid}")
    if est:
        parts.append(f"ESTIMATE: {est}")
    return "\n".join(parts)


_READ_SYS = (
    "You are an art-auction triage assistant. Given one lot's listing, decide "
    "whether it is fine art or an art object worth a closer look, pull out the "
    "primary artist and medium, and give a preliminary 0-100 interest score "
    "(higher = more likely a valuable, collectible, or overlooked work). Be "
    "decisive but honest about uncertainty. Respond with ONLY a JSON object: "
    '{"is_art": bool, "artist": string|null, "medium": string, '
    '"prelim_score": int, "confidence": "low"|"medium"|"high", '
    '"reason": string (<=200 chars)}')

_READ_DEFAULT = {
    "available": False, "is_art": None, "artist": None, "medium": "",
    "prelim_score": 0, "confidence": "low", "reason": "",
}


def read_lot(lot):
    """Stage 1: cheap read of a single lot on the workhorse model. Runs on
    EVERY lot, so keep it fast. Returns a normalized dict; on endpoint failure
    returns available=False so the caller can fall back to rules."""
    reply = _chat(READ_MODEL, _READ_SYS, _lot_text(lot), max_tokens=300)
    parsed = _extract_json(reply)
    if parsed is None:
        return dict(_READ_DEFAULT)
    try:
        score = int(parsed.get("prelim_score") or 0)
    except (TypeError, ValueError):
        score = 0
    conf = str(parsed.get("confidence", "low")).lower()
    return {
        "available": True,
        "is_art": bool(parsed.get("is_art")),
        "artist": (parsed.get("artist") or None),
        "medium": str(parsed.get("medium") or ""),
        "prelim_score": max(0, min(100, score)),
        "confidence": conf if conf in ("low", "medium", "high") else "low",
        "reason": str(parsed.get("reason") or "")[:300],
    }


_JUDGE_SYS = (
    "You are a senior art-market analyst advising a serious private collector. "
    "You are given one auction lot plus assembled EVIDENCE (institutional "
    "standing and market comps already looked up for you — trust the comps for "
    "value; your job is judgment, not recall). Decide a review priority and "
    "write a tight verdict a collector can act on. Weigh authenticity/"
    "attribution risk, how strong an example this is, and price vs the comps. "
    "Respond with ONLY a JSON object: "
    '{"priority": "A"|"B"|"C"|"D", "verdict": string (<=400 chars), '
    '"value_opinion": string, "confidence": "low"|"medium"|"high", '
    '"reason": string (<=200 chars)}  '
    "A = bid on it, B = watch closely, C = marginal, D = pass.")

_JUDGE_DEFAULT = {
    "available": False, "priority": None, "verdict": "", "value_opinion": "",
    "confidence": "low", "reason": "",
}


def judge_lot(lot, evidence=""):
    """Stage 4: deep judgment on a gated candidate, on the heavyweight model.
    `evidence` is the assembled evidence string (authority + prices/MutualArt +
    charity). Returns a normalized dict; available=False on endpoint failure."""
    if isinstance(evidence, dict):
        evidence = "\n".join(f"{k}: {v}" for k, v in evidence.items() if v)
    user = _lot_text(lot) + "\n\nEVIDENCE:\n" + (evidence or "(none found)")
    reply = _chat(JUDGE_MODEL, _JUDGE_SYS, user, max_tokens=700)
    parsed = _extract_json(reply)
    if parsed is None:
        return dict(_JUDGE_DEFAULT)
    pri = str(parsed.get("priority", "")).strip().upper()[:1]
    conf = str(parsed.get("confidence", "low")).lower()
    return {
        "available": True,
        "priority": pri if pri in ("A", "B", "C", "D") else "C",
        "verdict": str(parsed.get("verdict") or "")[:600],
        "value_opinion": str(parsed.get("value_opinion") or "")[:300],
        "confidence": conf if conf in ("low", "medium", "high") else "low",
        "reason": str(parsed.get("reason") or "")[:300],
    }


_CLASSIFY_SYS = (
    "You classify ONE auction lot for comparable-sales valuation. Identify the "
    "work's medium, its physical size, the year the work was MADE (not sold), "
    "and — most important — the SERIES or subject it belongs to within the "
    "artist's body of work, plus a few lowercase tokens that would identify that "
    "series in other auction listings (an Elegy gives ['elegy','spanish "
    "republic']; a Warhol Marilyn gives ['marilyn']). If the artist has no "
    "recognizable series or you are unsure, use series=null and an empty token "
    "list rather than guessing. Respond with ONLY JSON: {\"medium\": string, "
    "\"width_in\": number|null, \"height_in\": number|null, \"work_year\": "
    "int|null, \"series\": string|null, \"series_tokens\": [string]}")


def classify_work(lot):
    """Model classification of the subject for the comp engine: medium, size,
    work-year, and SERIES intelligence — the fuzzy judgment only a model does
    well. Returns {medium, area_sqin, work_year, series, series_tokens}; on
    endpoint failure returns {} so value_lot() falls back to parse-only."""
    reply = _chat(READ_MODEL, _CLASSIFY_SYS, _lot_text(lot), max_tokens=300)
    p = _extract_json(reply)
    if p is None:
        return {}

    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    w, h = _f(p.get("width_in")), _f(p.get("height_in"))
    area = round(w * h, 1) if (w and h) else None
    try:
        yr = int(p["work_year"]) if p.get("work_year") else None
    except (TypeError, ValueError):
        yr = None
    toks = p.get("series_tokens") or []
    toks = ([str(t).lower() for t in toks if t][:6]
            if isinstance(toks, list) else [])
    return {"medium": str(p.get("medium") or ""), "area_sqin": area,
            "work_year": yr, "series": p.get("series") or None,
            "series_tokens": toks}


# --------------------------------------------------------------------------- #
#  smoke test — T2 "done when": run read_lot on sample lots, get JSON back
#      python3 wallhunter/llm_client.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    print(f"endpoint: {BASE_URL}  read={READ_MODEL}  judge={JUDGE_MODEL}")
    if not available():
        print("  ENDPOINT NOT REACHABLE — start LM Studio's server (T1) first.")
        print("  (This is the correct silent-neutral result until the box is up.)")
        raise SystemExit(0)
    samples = [
        {"title": "Fritz Scholder (1937-2005), 'Indian No. 4', oil on canvas",
         "description": "Signed lower right, 40 x 30 in. Provenance: private "
                        "collection, acquired from the artist.",
         "current_bid": 4200},
        {"title": "Set of 6 pressed-glass tumblers, mid-century",
         "description": "Some wear. No maker's mark.", "current_bid": 15},
        {"title": "After Robert Motherwell, screenprint, 'Elegy'",
         "description": "Edition 44/100, unframed.", "current_bid": 300},
    ]
    for lot in samples:
        r = read_lot(lot)
        print(f"\n{lot['title'][:60]}")
        print("  read_lot:", json.dumps(r, ensure_ascii=False))
