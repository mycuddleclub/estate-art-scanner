"""Stage 0: free rules. Blocklist (canonical art-scout config via
wallhunter.blocklist) + art-signal band (deep.py precedent, Daniel-approved:
clear art signals OR too vague to rule out go forward; obvious junk is cut)."""
from wallhunter.blocklist import load_blocked_houses  # reads ~/art-scout/config.py

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
