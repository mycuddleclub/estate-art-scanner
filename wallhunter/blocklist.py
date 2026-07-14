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


@lru_cache(maxsize=1)
def load_blocked_houses() -> tuple[str, ...]:
    fragments: list[str] = []
    try:
        tree = ast.parse(ARTSCOUT_CONFIG.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "BLACKLISTED_HOUSES"
                    for t in node.targets):
                value = ast.literal_eval(node.value)
                fragments = [str(v).strip().lower() for v in value if str(v).strip()]
                break
        if not fragments:
            print(f"blocklist: BLACKLISTED_HOUSES not found in {ARTSCOUT_CONFIG}")
    except OSError as e:
        print(f"blocklist: cannot read {ARTSCOUT_CONFIG} ({e}) — using env only")
    except (SyntaxError, ValueError) as e:
        print(f"blocklist: failed to parse {ARTSCOUT_CONFIG} ({e}) — using env only")
    extra = [f.strip().lower() for f in
             os.environ.get("WH_EXTRA_BLOCKED_HOUSES", "").split(",") if f.strip()]
    return tuple(fragments + extra)


def blocked_match(org_name: str | None, blocked=None) -> str | None:
    """Return the matching blocklist fragment, or None."""
    if not org_name:
        return None
    name = org_name.lower()
    for frag in (blocked if blocked is not None else load_blocked_houses()):
        if frag in name:
            return frag
    return None
