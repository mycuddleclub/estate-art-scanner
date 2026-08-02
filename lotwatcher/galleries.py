"""Gallery-tier signal (Daniel's model, from his evaluation prompt — the
Magnus Resch 4-tier hierarchy). Gallery representation is a positive signal;
the TIER of the gallery is how strong. Pulls represented-by galleries from
the Artsy client (partner names) and classifies them.

Tier 1 mega-galleries = strongest; folk/self-taught artists picked up by a
serious gallery is exactly the discovery signal Daniel hunts.
"""
import importlib.util as _il
import os
import re

# --- Daniel's tier lists (verbatim from his prompt; extendable) ---
TIER1 = [  # Mega-galleries / market-makers
    "gagosian", "hauser & wirth", "hauser and wirth", "david zwirner", "zwirner",
    "pace gallery", "pace", "white cube", "thaddaeus ropac", "ropac",
    "lévy gorvy", "levy gorvy", "gladstone", "marian goodman", "sprüth magers",
    "spruth magers", "victoria miro",
]
TIER2 = [  # High-power launchpads
    "jack shainman", "james cohan", "perrotin", "casey kaplan", "kavi gupta",
    "blum & poe", "blum and poe", "lehmann maupin", "lisson", "matthew marks",
    "sean kelly", "regen projects", "goodman gallery", "kasmin", "petzel",
    "andrew kreps", "anton kern", "luhring augustine", "paula cooper",
    "salon 94", "tanya bonakdar",
]
TIER3 = [  # Incubators
    "p·p·o·w", "ppow", "p.p.o.w", "denny dimin", "denny gallery", "anna zorina",
    "various small fires", "vsf", "nicelle beauchene", "jack hanley",
    "kate werble", "night gallery", "the hole", "steve turner", "de boer",
    "shulamit nazarian", "charles moffett", "half gallery",
]
# Tier 4 = anything else that reads as a gallery (represented at all = a plus)

_ARTSY = None


def _artsy():
    global _ARTSY
    if _ARTSY is None:
        try:
            p = os.path.expanduser(
                "~/estate-art-scanner/wallhunter/artsy_client.py")
            spec = _il.spec_from_file_location("artsy_client", p)
            m = _il.module_from_spec(spec)
            spec.loader.exec_module(m)
            _ARTSY = m
        except Exception:
            _ARTSY = False
    return _ARTSY or None


import json as _json
import re as _re
import sqlite3 as _sql
import time as _time

import httpx as _httpx

from . import config as _cfg

_CACHE_DB = _cfg.WH_DATA / "galleries.db"
_REFRESH_DAYS = int(os.environ.get("LW_GALLERY_REFRESH_DAYS", "180"))
_gconn = None


def _cache():
    global _gconn
    if _gconn is None:
        _cfg.data_dirs()
        _gconn = _sql.connect(_CACHE_DB, timeout=30)
        _gconn.execute("CREATE TABLE IF NOT EXISTS gallery_tier ("
                       "gkey TEXT PRIMARY KEY, name TEXT, tier INT, "
                       "confidence TEXT, why TEXT, source TEXT, at REAL)")
        _gconn.commit()
    return _gconn


def _gkey(name):
    return _re.sub(r"[^a-z0-9 ]", "", (name or "").lower()).strip()


_CLASSIFY_PROMPT = """Classify this art gallery on the Magnus Resch 4-tier model. Use your knowledge of its program, artists, art-fair presence, and reputation.
Tier 1 = mega/market-maker (Gagosian, Zwirner, Hauser & Wirth, Pace, White Cube).
Tier 2 = high-power launchpad (Jack Shainman, Perrotin, Casey Kaplan, Lehmann Maupin).
Tier 3 = respected incubator/feeder gallery (serious program, scholarly shows, major-fair presence, represents museum-collected artists).
Tier 4 = minor/regional/vanity gallery, little market influence.

Gallery: {g}

Return STRICT JSON: {{"tier": 1-4, or 0 if you genuinely do not know this gallery, "confidence": "high/med/low", "why": "one sentence"}}"""


def _model_classify(name):
    try:
        r = _httpx.post(f"{_cfg.LM_BASE}/chat/completions", timeout=90, json={
            "model": _cfg.STAGE3_MODEL, "max_tokens": 400, "reasoning_effort": "low",
            "messages": [{"role": "user", "content": _CLASSIFY_PROMPT.format(g=name)}]})
        t = r.json()["choices"][0]["message"]["content"] or ""
        m = _re.search(r"\{.*\}", t, _re.S)
        if not m:
            return None
        d = _json.loads(m.group(0))
        return {"tier": int(d.get("tier", 0)), "confidence": d.get("confidence", "low"),
                "why": d.get("why", "")}
    except Exception:
        return None


def classify_gallery(name: str) -> dict:
    """Tier a gallery: hardcoded anchors -> cache -> 120B -> cache.
    Returns {tier, confidence, why, source}. tier 0 = unknown."""
    g = (name or "").lower().strip()
    if not g:
        return {"tier": 0, "confidence": "", "why": "", "source": "empty"}
    # fast-path anchors (certain, no model call)
    if any(t in g for t in TIER1):
        return {"tier": 1, "confidence": "high", "why": "anchor", "source": "anchor"}
    if any(t in g for t in TIER2):
        return {"tier": 2, "confidence": "high", "why": "anchor", "source": "anchor"}
    if any(t in g for t in TIER3):
        return {"tier": 3, "confidence": "high", "why": "anchor", "source": "anchor"}

    key = _gkey(name)
    conn = _cache()
    row = conn.execute("SELECT tier, confidence, why, source, at FROM gallery_tier"
                       " WHERE gkey=?", (key,)).fetchone()
    if row and (_time.time() - (row[4] or 0)) < _REFRESH_DAYS * 86400:
        return {"tier": row[0], "confidence": row[1], "why": row[2], "source": row[3]}

    d = _model_classify(name)
    if d is None:
        # leave uncached (retry next time); web-search fallback plugs in here later
        return {"tier": 0, "confidence": "low", "why": "unresolved", "source": "none"}
    d["source"] = "model"
    conn.execute("INSERT OR REPLACE INTO gallery_tier"
                 "(gkey,name,tier,confidence,why,source,at) VALUES (?,?,?,?,?,?,?)",
                 (key, name[:120], d["tier"], d["confidence"], d["why"], "model", _time.time()))
    conn.commit()
    return d


def _tier_of(gallery: str) -> int:
    return classify_gallery(gallery).get("tier", 0)


def represented_galleries(artist: str) -> list[str]:
    """Distinct gallery/partner names Artsy associates with the artist."""
    ac = _artsy()
    if ac is None or not artist:
        return []
    try:
        works = ac.lookup(artist) or []
    except Exception:
        return []
    # exclude non-representation partners: auction houses, benefit/charity
    # auctions, and museums list themselves as Artsy "partner" too
    NOISE = ("auction", "benefit", "phillips", "christie", "sotheby",
             "bonhams", "freeman", "heritage", "swann", "museum",
             "artadia", "biennial", "fair", "foundation")
    seen, out = set(), []
    for w in works:
        name = ((w.get("partner") or {}).get("name") or "").strip()
        k = name.lower()
        if name and k not in seen and not any(n in k for n in NOISE):
            seen.add(k)
            out.append(name)
    return out


_TIER_LABEL = {1: "Tier-1 mega-gallery", 2: "Tier-2 launchpad",
               3: "Tier-3 incubator", 4: "gallery-represented"}


def evidence_line(artist: str) -> str:
    """'GALLERY: represented by Jack Shainman (Tier-2 launchpad)' or ''."""
    galleries = represented_galleries(artist)
    if not galleries:
        return ""
    best_tier = min(_tier_of(g) for g in galleries)   # 1 is best
    if best_tier == 0:
        return ""
    named = ", ".join(galleries[:4])
    return f"GALLERY: represented by {named} — best is {_TIER_LABEL[best_tier]}"


def best_tier(artist: str) -> int:
    """Lowest (best) tier among the artist's real galleries, or 0 if none.
    1=mega, 2=launchpad, 3=incubator/feeder, 4=other gallery."""
    gs = represented_galleries(artist)
    tiers = [_tier_of(g) for g in gs if _tier_of(g)]
    return min(tiers) if tiers else 0
