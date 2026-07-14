"""Blocked auction houses, read live from art-scout's canonical list.

The blacklist is maintained in 4 files across Daniel's scanners (see the
art-scout tools); this module READS ~/art-scout/config.py at runtime instead
of adding a fifth copy to keep in sync. Matching follows the established
convention: case-insensitive substring, so short distinctive fragments block
any org name containing them. Extra fragments via WH_EXTRA_BLOCKED_HOUSES.
"""

import ast
import os
from functools import lru_cache
from pathlib import Path

ARTSCOUT_CONFIG = Path.home() / "art-scout" / "config.py"


def _load_artscout_list(var_name: str) -> list[str]:
    """Extract a list/set constant from art-scout's config.py via ast."""
    try:
        tree = ast.parse(ARTSCOUT_CONFIG.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == var_name
                    for t in node.targets):
                value = ast.literal_eval(node.value)
                return [str(v).strip().lower() for v in value if str(v).strip()]
        print(f"blocklist: {var_name} not found in {ARTSCOUT_CONFIG}")
    except OSError as e:
        print(f"blocklist: cannot read {ARTSCOUT_CONFIG} ({e})")
    except (SyntaxError, ValueError) as e:
        print(f"blocklist: failed to parse {ARTSCOUT_CONFIG} ({e})")
    return []


@lru_cache(maxsize=1)
def load_blocked_houses() -> tuple[str, ...]:
    fragments = _load_artscout_list("BLACKLISTED_HOUSES")
    extra = [f.strip().lower() for f in
             os.environ.get("WH_EXTRA_BLOCKED_HOUSES", "").split(",") if f.strip()]
    return tuple(fragments + extra)


@lru_cache(maxsize=1)
def load_non_art_keywords() -> tuple[str, ...]:
    """Art Scout's junk-auction title keywords (surplus, pallets, guns, ...)."""
    return tuple(_load_artscout_list("NON_ART_AUCTION_KEYWORDS"))


def blocked_match(org_name: str | None, blocked=None) -> str | None:
    """Return the matching blocklist fragment, or None."""
    if not org_name:
        return None
    name = org_name.lower()
    for frag in (blocked if blocked is not None else load_blocked_houses()):
        if frag in name:
            return frag
    return None
