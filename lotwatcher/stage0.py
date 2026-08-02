"""Stage 0: free rules. Blocklist (canonical art-scout config via
wallhunter.blocklist) + art-signal band (deep.py precedent, Daniel-approved:
clear art signals OR too vague to rule out go forward; obvious junk is cut)."""
import re

from wallhunter.blocklist import load_blocked_houses, load_non_art_keywords  # ~/art-scout/config.py

from . import config

_BLOCKED = None


def blocked() -> tuple:
    global _BLOCKED
    if _BLOCKED is None:
        try:
            _BLOCKED = tuple(b.lower() for b in load_blocked_houses())
        except Exception:
            _BLOCKED = ()
    return _BLOCKED


def house_blocked(house: str) -> bool:
    h = (house or "").lower()
    return any(b in h for b in blocked())


def lot_passes(title: str) -> bool:
    """True -> lot goes to stage 1. Recall-first:
    - any art signal            -> pass
    - hard negative, no signal  -> cut
    - vague/unknown             -> pass (uncertainty goes forward)"""
    t = (title or "").lower()
    if not t.strip():
        return False
    if any(k in t for k in config.ART_SIGNAL):
        return True
    if any(k in t for k in config.HARD_NEGATIVE):
        return False
    return True

_NON_ART = None


def non_art_keywords() -> tuple:
    global _NON_ART
    if _NON_ART is None:
        try:
            _NON_ART = tuple(k.lower() for k in load_non_art_keywords())
        except Exception:
            _NON_ART = ()
    return _NON_ART


def auction_skippable(title: str, house: str) -> str | None:
    """Whole-auction skip: blocked house, or a non-art genre title
    (livestock, firearms, vehicles...). Returns the reason or None.
    Auction/house skips are time-cost calls (Daniel's rule) — recall-first
    protection applies to lots and artists, not junk-genre auctions."""
    if house_blocked(house):
        return "blocked house"
    combined = f"{(title or '').lower()} {(house or '').lower()}"
    for kw in non_art_keywords():
        if kw in combined:
            return f"non-art genre: {kw}"
    return None

# Auctions that are unambiguously consumer-goods liquidation. Their lots are
# never art, and they were flooding the screening queue (391k lots, 95% junk).
LIQUIDATION_AUCTION = (
    "liquidation", "overstock", "unclaimed parcel", "returned parcel",
    "returns auction", "amazon return", "shelf pull", "surplus",
    "wholesale", "pallet", "closeout", "dollar store", "flea market",
    "storage unit", "self storage", "mini storage",
)

# Signals that an auction is an art/estate/antique sale -> recall-first inside.
ART_CONTEXT = (
    "estate", "antique", "fine art", "art auction", "gallery", "collection",
    "collector", "americana", "decorative art", "folk art", "consignment",
    "heirloom", "vintage", "modern design", "mid century", "midcentury",
)

# Attribution patterns that mark a lot as art even with no medium word
# (e.g. "Karl Wirsum (1939-2021)", "attributed to Homer", ", American,").
_ATTRIB = re.compile(
    r"\(\s*(?:b\.\s*)?1[6-9]\d{2}\s*[-–]\s*(?:1[6-9]|20)?\d{0,2}\s*\)"
    r"|\battributed to\b|\bafter [A-Z]|\bmanner of\b|\bschool of\b"
    r"|\bcircle of\b|\bfollower of\b|\billus(?:trated)? by\b"
    r"|,\s*(?:American|French|British|German|Italian|Dutch|Spanish|Mexican|"
    r"Canadian|Russian|Japanese|Chinese)\s*,",
    re.I)


def auction_is_liquidation(title: str, house: str = "") -> bool:
    t = f"{(title or '').lower()} {(house or '').lower()}"
    return any(k in t for k in LIQUIDATION_AUCTION)


def auction_is_art_context(title: str, house: str = "") -> bool:
    t = f"{(title or '').lower()} {(house or '').lower()}"
    return any(k in t for k in ART_CONTEXT)


def lot_passes_ctx(title: str, auction_title: str = "", auction_house: str = "",
                   detail: str = "") -> bool:
    """Context-aware stage-0 (Daniel 2026-08-02 audit).

    Recall-first is preserved WHERE IT MATTERS (estate/art/antique sales:
    vague titles still pass). But in liquidation/overstock/parcel auctions a
    lot must show a real art signal or an attribution pattern — otherwise
    98% of the model's time goes to yoga socks and blenders.
    """
    t = (title or "")
    tl = t.lower()
    if not tl.strip():
        return False
    if any(k in tl for k in config.HARD_NEGATIVE):
        return False
    has_art = any(k in tl for k in config.ART_SIGNAL) or bool(_ATTRIB.search(t))
    if has_art:
        return True
    if auction_is_liquidation(auction_title, auction_house):
        return False                      # junk sale + no art signal -> skip
    if auction_is_art_context(auction_title, auction_house):
        return True                       # art/estate sale -> recall-first
    d = (detail or "")[:300].lower()
    return any(k in d for k in config.ART_SIGNAL)


def strong_art(title: str, detail: str = "") -> bool:
    """Unambiguous art signal (strict vocabulary or an attribution pattern)."""
    t = title or ""
    tl = t.lower()
    return (any(k in tl for k in config.STRONG_ART) or bool(_ATTRIB.search(t))
            or any(k in (detail or "")[:300].lower() for k in config.STRONG_ART))


def art_density(lots) -> float:
    """Share of an auction's lots showing a strong art signal."""
    if not lots:
        return 0.0
    n = sum(1 for l in lots
            if strong_art(l.get("title", ""), l.get("detail", "")))
    return n / len(lots)


def lot_passes_density(title: str, detail: str, density: float,
                       auction_title: str = "", auction_house: str = "") -> bool:
    """Stage-0 with auction context (audit 2026-08-02).

    * explicit art lot            -> always screened
    * art-bearing sale (density)  -> recall-first, vague lots still screened
    * consumer-goods sale         -> skipped unless explicitly art
    """
    tl = (title or "").lower()
    if not tl.strip():
        return False
    if any(k in tl for k in config.HARD_NEGATIVE):
        return False
    if strong_art(title, detail):
        return True
    if auction_is_liquidation(auction_title, auction_house):
        return False
    if density >= config.ART_DENSITY_MIN:
        return any(k in tl for k in config.ART_SIGNAL) or True   # recall-first
    return False
