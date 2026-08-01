"""Local model access: direct LM Studio OpenAI endpoint + model swapping.
Stage 1 (Qwen) and stage 3 (GPT-OSS-120B) can't co-reside in the 64 GB carve,
so cycles run in phases and swap via the lms CLI (Windows interop)."""
import os
import json
import re
import subprocess

import httpx

from . import config


KEEP_BOTH = os.environ.get("LW_KEEP_BOTH", "0") == "1"


def ensure_model(name: str):
    """Make `name` servable. With LW_KEEP_BOTH=1 (96 GB carve: both models
    fit resident) we never unload — just load if missing. Otherwise swap."""
    try:
        r = httpx.get(f"{config.LM_BASE}/models", timeout=10)
        loaded = [m["id"] for m in r.json().get("data", [])]
    except Exception:
        loaded = []
    # LM Studio lists all downloaded models at /models; ask lms what is loaded
    ps = subprocess.run([config.LMS_EXE, "ps"], capture_output=True, text=True, timeout=60)
    if name in (ps.stdout or ""):
        return
    if not KEEP_BOTH:
        subprocess.run([config.LMS_EXE, "unload", "--all"], capture_output=True, timeout=120)
    subprocess.run([config.LMS_EXE, "load", name, "--gpu", "max",
                    "--context-length", "8192", "-y"],
                   capture_output=True, text=True, timeout=600)


def _chat(model: str, prompt: str, max_tokens: int, temperature: float = 0.2,
          reasoning: str = "low") -> str:
    r = httpx.post(f"{config.LM_BASE}/chat/completions", timeout=300, json={
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "reasoning_effort": reasoning,
        "messages": [{"role": "user", "content": prompt}],
    })
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    return msg.get("content") or ""


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON in: {text[:200]}")
    return json.loads(m.group(0))


STAGE1_PROMPT = """You screen auction lots for an art collector. Given ONE lot, output STRICT JSON only:
{{"is_art": true/false, "category": "painting|print|drawing|photo|sculpture|ceramics|textile|jewelry|glass|metalware|furniture|decor|book|other|not_art", "artist": "extracted artist name or empty string", "promise": 0-10, "reason": "max 15 words"}}

promise = likelihood this is a collectible fine/folk/self-taught artwork by an identifiable artist worth researching. Signed/attributed works with a plausible artist name score high. Mass reproductions, decor, and junk score low. When uncertain, lean HIGHER (recall over precision).

LOT: {title}
DESCRIPTION: {desc}
ESTIMATE: {estimate}  CURRENT BID: {bid}
JSON:"""


def stage1_classify(lot: dict) -> dict:
    text = _chat(config.STAGE1_MODEL, STAGE1_PROMPT.format(
        title=lot["title"][:400],
        desc=(lot.get("detail") or "none")[:250],
        estimate=lot["estimate"] or "unknown",
        bid=lot["bid"] or "none"), max_tokens=300, reasoning="none")
    d = _extract_json(text)
    d["promise"] = float(d.get("promise", 0))
    return d


STAGE3_PROMPT = """You are the final judge for an art collector hunting overlooked fine/folk/self-taught art at regional auctions. His thesis: the gap between institutional IMPORTANCE and current PRICE is the opportunity. Recall over precision — he would rather see noise than miss a trophy.

LOT
title: {title}
estimate: {estimate}   current bid: {bid}
auction: {auction} — {house} ({platform})
listing detail (may be empty): {detail}

STAGE-1 READ: artist claim "{artist}", category {category}, promise {promise}

A VISION line (if present) is the model's own look at the lot PHOTO — a transcribed signature or an image-does-not-match-listing flag is strong primary evidence, weigh it heavily.

EVIDENCE (local databases — absence is NEUTRAL, never disqualifying):
{evidence}

Output STRICT JSON only:
{{"flag": "YES/NO", "confidence": "HIGH/MEDIUM/LOW", "score": 0-10, "reasoning": "2-3 sentences: why this is/isn't worth his personal review", "headline": "max 12 words for the email subject line"}}
Flag YES ONLY when there is a SPECIFIC, IDENTIFIABLE artist by name (trained OR self-taught is fine) who is genuinely listed/collected/documented — someone Daniel can research and build a buying case on — AND the lot is materially underpriced vs the evidence or sitting with no/low bids. The artist may be named in the listing or identified from a legible signature in the VISION line.
Flag NO for: anonymous or undocumented makers even if the piece is old/rare/folk (no name = no case, he does not want these); reproductions and mass-produced prints; decorative mass goods; fairly-priced lots; and speculative "might be something" lots with no identifiable artist. "Old, rare, no bids" is NOT enough — there must be a real, name-able, documented artist behind it.
Skepticism rules: attribution hedges ("attributed to", "after", "school/style/circle/manner/follower of") lower confidence sharply — flag only with independent evidence. A blue-chip master name (Picasso, Dali, Chagall...) claimed as an ORIGINAL at a regional house is presumptively fake — the fake economy operates on famous names; flag NO unless provenance in the listing is specific and verifiable."""


def stage3_judge(lot: dict, s1: dict, evidence: str, auction: dict) -> dict:
    text = _chat(config.STAGE3_MODEL, STAGE3_PROMPT.format(
        title=lot["title"][:400],
        estimate=lot["estimate"] or "unknown",
        bid=lot["bid"] or "none",
        auction=auction.get("title", ""), house=auction.get("house", ""),
        platform=auction.get("platform", ""),
        detail=(lot.get("detail") or "")[:1800],
        artist=s1.get("artist", ""), category=s1.get("category", ""),
        promise=s1.get("promise", 0),
        evidence=evidence or "(none found — neutral)"), max_tokens=1600)
    return _extract_json(text)
