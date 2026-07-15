"""One-off: Haiku-rank all unscanned window auctions by title promise,
deep-scan the top N, mark the rest skipped so steady state starts tomorrow.

Usage: python -m wallhunter.rank_night [top_n]
"""

import json
import os
import re
import sys

import anthropic

from . import db
from .artists import import_artscout_cache, import_checker_cache
from .config import CostMeter, anthropic_api_key
from .deep import deep_scan, unscanned_candidates
from .exclusives import find_exclusives

RANK_MODEL = "claude-haiku-4-5-20251001"
RANK_PROMPT = """Rate each auction 0-10 for how likely it is to contain COLLECTIBLE
FINE/FOLK ART (paintings, works on paper, listed artists, real estates of
collectors) vs liquidation stock, mass decor, coins, toys, or equipment.
Auctions (title | house):
{rows}
Reply ONLY with lines like "3:7" (index:score), one per auction."""


def rank_titles(auctions, meter) -> list[tuple[float, dict]]:
    client = anthropic.Anthropic()
    scored = []
    for start in range(0, len(auctions), 50):
        batch = auctions[start:start + 50]
        rows = "\n".join(f"{i+1}. {a['title'][:90]} | {a['house'][:40]}"
                         for i, a in enumerate(batch))
        try:
            resp = client.messages.create(
                model=RANK_MODEL, max_tokens=1200,
                messages=[{"role": "user",
                           "content": RANK_PROMPT.format(rows=rows)}])
            meter.add(RANK_MODEL, resp.usage)
            txt = "".join(b.text for b in resp.content
                          if getattr(b, "type", "") == "text")
            marks = dict(re.findall(r"(\d+)\s*:\s*(\d+(?:\.\d+)?)", txt))
            for i, a in enumerate(batch):
                scored.append((float(marks.get(str(i + 1), 0)), a))
        except Exception as e:
            print(f"rank batch @{start} failed ({str(e)[:60]}) — scoring 5s")
            scored.extend((5.0, a) for a in batch)
    scored.sort(key=lambda x: (-x[0], x[1].get("ends") or "9999"))
    return scored


def main(top_n: int = 150):
    os.environ.setdefault("ANTHROPIC_API_KEY", anthropic_api_key())
    conn = db.connect()
    import_checker_cache(conn)
    import_artscout_cache(conn)
    exclusives = find_exclusives()
    candidates = unscanned_candidates(conn, exclusives)
    print(f"{len(candidates)} unscanned auctions to rank")
    meter = CostMeter(1.0)
    scored = rank_titles(candidates, meter)
    print(f"title ranking cost ${meter.total:.3f}; top of list:")
    for s, a in scored[:12]:
        print(f"  {s:>4.1f}  {a['house'][:32]:<33} {a['title'][:52]}")

    chosen = [a for _, a in scored[:top_n]]
    skipped = [a for _, a in scored[top_n:]]
    flags, stats = deep_scan(conn, chosen, research_cap_usd=5.0,
                             max_auctions=top_n)
    # mark the low-ranked remainder as covered: Daniel chose to skip the
    # current backlog; steady state (daily inflow only) starts tomorrow
    for a in skipped:
        conn.execute(
            "INSERT OR IGNORE INTO deep_auctions (sale_url, house, title,"
            " ends, art_lots, scanned_at) VALUES (?,?,?,?,NULL,?)",
            (a["url"], a["house"], a["title"], a.get("ends"),
             db.now() + " skipped-backlog"))
    conn.commit()
    print(f"done: {stats} | {len(flags)} flags stored (email at 7:45)"
          f" | {len(skipped)} low-ranked backlog auctions marked skipped")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 150)
