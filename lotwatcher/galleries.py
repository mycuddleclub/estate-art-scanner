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


def _tier_of(gallery: str) -> int:
    g = (gallery or "").lower().strip()
    if not g:
        return 0
    if any(t in g for t in TIER1):
        return 1
    if any(t in g for t in TIER2):
        return 2
    if any(t in g for t in TIER3):
        return 3
    return 4   # represented by *a* gallery, tier unknown = still a plus


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
