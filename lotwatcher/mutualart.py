"""Demand-driven MutualArt harvest for TOP candidates only (Daniel's rule:
don't overdo it on MutualArt — it's a paid, logged-in account we protect).

Runs between stage 1 and stage 3 so the 120B judgment sees fresh tier-A
comps. The harvester itself is cache-first (skips artists already fresh in
prices.db) and politely paced; we add a hard daily artist cap on top."""
import datetime
import os
import subprocess

from . import store

DAILY_CAP = int(os.environ.get("LW_MUTUALART_DAILY_CAP", "75"))
# per-chunk cap: the deep harvest costs ~6 min/artist, so a full daily batch
# would sit for hours in front of the judgments. A few artists per chunk keeps
# comps flowing without ever blocking the digest for long.
PER_CHUNK = int(os.environ.get("LW_MUTUALART_PER_CHUNK", "6"))
APPRAISER = os.path.expanduser("~/art-appraiser")


def _today_key() -> str:
    return "ma_harvest_" + datetime.date.today().isoformat()


# makers/companies are not artists — don't spend MutualArt slots on them
_MAKER_WORDS = (
    "glass", "pottery", "porcelain", "crystal", "china", "silver co",
    "& sons", "& grondahl", "ltd", " inc", " co.", "copenhagen", "rosenthal",
    "metalcrafters", "woodcarvers", "trifari", "steuben", "royal ",
    "brothers", "bros.", "manufactory", "works", "guild", "studio s",
)


def _is_maker(name: str) -> bool:
    n = " " + name.lower() + " "
    return any(w in n for w in _MAKER_WORDS)


def pick_artists(conn, remaining: int) -> list[str]:
    """Top s3 candidates' artists, best promise first, two-word minimum,
    makers/companies excluded."""
    rows = conn.execute(
        "SELECT artist, MAX(promise) p FROM lots"
        " WHERE stage='s3' AND artist IS NOT NULL AND artist != ''"
        " GROUP BY artist ORDER BY p DESC").fetchall()
    out = []
    for r in rows:
        a = (r["artist"] or "").strip()
        if (len(a.split()) >= 2 and not _is_maker(a)
                and a.lower() not in (x.lower() for x in out)):
            out.append(a)
        if len(out) >= remaining:
            break
    return out


def run(conn) -> int:
    used = int(store.get_meta(conn, _today_key(), "0"))
    remaining = min(max(0, DAILY_CAP - used), PER_CHUNK)
    if remaining == 0:
        print("mutualart: daily cap reached — skipping")
        return 0
    artists = pick_artists(conn, remaining)
    if not artists:
        return 0
    print(f"mutualart: harvesting comps for {len(artists)} top candidates "
          f"({used}/{DAILY_CAP} used today)")
    try:
        r = subprocess.run(
            [os.path.join(APPRAISER, "venv/bin/python"), "mutualart_harvest.py",
             *artists],
            cwd=APPRAISER, capture_output=True, text=True,
            env={**os.environ, "MUTUALART_HEADLESS": "1"},
            timeout=60 * (5 + 2 * len(artists)))
        tail = (r.stdout or "").strip().splitlines()[-6:]
        for ln in tail:
            print(f"  {ln}")
        if r.returncode != 0:
            print(f"  mutualart harvest exit {r.returncode}: "
                  f"{(r.stderr or '')[-200:]}")
    except subprocess.TimeoutExpired:
        print("  mutualart harvest timed out — partial results are still banked")
    store.set_meta(conn, _today_key(), used + len(artists))
    return len(artists)
