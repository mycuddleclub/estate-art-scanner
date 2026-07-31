"""Stage 0: free rules. Blocklist (canonical art-scout config via
wallhunter.blocklist) + art-signal band (deep.py precedent, Daniel-approved:
clear art signals OR too vague to rule out go forward; obvious junk is cut)."""
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
