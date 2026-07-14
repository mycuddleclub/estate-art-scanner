"""Taste priors from the Ledger: per-category save/dismiss ratios.

Deliberately simple and inspectable. A category's boost activates only after
MIN_EVENTS judgments in that category; until then it is neutral. Dismissals
with reason 'print/repro' count against the work, not the category (the
category wasn't the problem, the object was fake).
"""

import math

MIN_EVENTS = 8
MAX_BOOST = 1.0  # score points added/subtracted at the extreme


def category_boosts(conn) -> dict[str, float]:
    """category -> boost in [-MAX_BOOST, +MAX_BOOST]; missing = neutral 0."""
    rows = conn.execute(
        "SELECT COALESCE(lower(w.category),'other') AS cat, e.kind, e.reason"
        " FROM events e JOIN works w ON w.id = e.work_id"
        " WHERE e.kind IN ('save','dismiss','promote')").fetchall()
    stats: dict[str, list[int]] = {}
    for r in rows:
        s = stats.setdefault(r["cat"], [0, 0])  # [positive, negative]
        if r["kind"] in ("save", "promote"):
            s[0] += 1
        elif (r["reason"] or "") != "print/repro":
            s[1] += 1
    out = {}
    for cat, (pos, neg) in stats.items():
        if pos + neg < MIN_EVENTS:
            continue
        # log-odds with Laplace smoothing, squashed to [-1, 1]
        ratio = math.log((pos + 1) / (neg + 1))
        out[cat] = max(-MAX_BOOST, min(MAX_BOOST, ratio / 2))
    return out
