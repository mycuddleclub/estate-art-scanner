#!/usr/bin/env python3
"""Standalone MutualArt harvester daemon — decoupled from the judgment pipeline.

Runs all day in small clusters with long gaps (human-plausible), banking
tier-A comps for the artists the scanner cares about most: flagged first,
then high-promise candidates. Hard daily cap; stops and emails Daniel if
MutualArt ever challenges the session instead of pushing through.
"""
import datetime
import os
import random
import sqlite3
import subprocess
import sys
import time

DB = os.path.expanduser("~/estate-art-scanner/wh_data/lotwatcher.db")
PRICES = os.path.expanduser("~/estate-art-scanner/wh_data/prices.db")
APPRAISER = os.path.expanduser("~/art-appraiser")
ALERT = os.path.expanduser("~/bin/alert.py")

DAILY_CAP = int(os.environ.get("MA_DAILY_CAP", "100"))
BATCH = int(os.environ.get("MA_BATCH", "5"))
GAP_MIN_S = int(os.environ.get("MA_GAP_MIN_S", "600"))    # 10 min
GAP_MAX_S = int(os.environ.get("MA_GAP_MAX_S", "1500"))   # 25 min

sys.path.insert(0, os.path.expanduser("~/estate-art-scanner"))
from lotwatcher.mutualart import _is_maker  # noqa: E402  (maker stoplist)


def today_key():
    return "ma_harvest_" + datetime.date.today().isoformat()


def used_today(conn):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (today_key(),)).fetchone()
    return int(row[0]) if row else 0


def add_used(conn, n):
    conn.execute(
        "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET"
        " v=CAST(CAST(v AS INTEGER)+? AS TEXT)", (today_key(), str(n), n))
    conn.commit()


def harvested_artists():
    try:
        pc = sqlite3.connect(PRICES, timeout=30)
        rows = pc.execute("SELECT DISTINCT artist_key FROM prices"
                          " WHERE platform='mutualart'").fetchall()
        pc.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def norm_key(a):
    import re
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]+", " ", a.lower())).strip()


def pick(conn, n):
    have = harvested_artists()
    out = []
    rows = conn.execute(
        "SELECT artist, MAX(flagged) f, MAX(promise) p FROM lots"
        " WHERE artist IS NOT NULL AND artist != '' AND stage IN ('s3','done')"
        " GROUP BY artist ORDER BY f DESC, p DESC LIMIT 800").fetchall()
    for r in rows:
        a = (r[0] or "").strip()
        if (len(a.split()) >= 2 and not _is_maker(a)
                and norm_key(a) not in have
                and a.lower() not in (x.lower() for x in out)):
            out.append(a)
        if len(out) >= n:
            break
    return out


def alert(subject, body):
    try:
        with open("/tmp/ma-harvester.txt", "w") as f:
            f.write(body)
        subprocess.run(["python3", ALERT, subject, "/tmp/ma-harvester.txt"],
                       timeout=90)
    except Exception:
        pass


def main():
    print(f"harvester daemon: cap {DAILY_CAP}/day, batches of {BATCH}, "
          f"gaps {GAP_MIN_S}-{GAP_MAX_S}s", flush=True)
    consecutive_zero = 0
    while True:
        conn = sqlite3.connect(DB, timeout=60)
        used = used_today(conn)
        if used >= DAILY_CAP:
            print(f"daily cap reached ({used}/{DAILY_CAP}) — sleeping 1h", flush=True)
            conn.close()
            time.sleep(3600)
            continue
        artists = pick(conn, min(BATCH, DAILY_CAP - used))
        conn.close()
        if not artists:
            print("queue empty — sleeping 1h", flush=True)
            time.sleep(3600)
            continue
        print(f"batch: {artists}", flush=True)
        r = subprocess.run(
            [os.path.join(APPRAISER, "venv/bin/python"), "mutualart_harvest.py",
             *artists],
            cwd=APPRAISER, capture_output=True, text=True,
            env={**os.environ, "MUTUALART_HEADLESS": "1"},
            timeout=60 * (10 + 6 * len(artists)))
        out = (r.stdout or "")
        for ln in out.strip().splitlines()[-len(artists) - 1:]:
            print(f"  {ln}", flush=True)
        conn = sqlite3.connect(DB, timeout=60)
        add_used(conn, len(artists))
        conn.close()
        banked = out.count("banked") and sum(
            int(w.split()[-3]) for w in out.splitlines()
            if " banked" in w and w.split()[-3].isdigit()) or 0
        # challenge/failure tripwire: repeated all-zero batches = stop + email
        if "0 new tier-A comps" in out and banked == 0:
            consecutive_zero += 1
        else:
            consecutive_zero = 0
        if consecutive_zero >= 3:
            alert("MutualArt harvester STOPPED — possible block/challenge",
                  "Three consecutive batches banked zero comps. The session may "
                  "be logged out or challenged. Harvester is pausing for 6h.\n\n"
                  "Last output:\n" + out[-1500:])
            consecutive_zero = 0
            time.sleep(6 * 3600)
            continue
        gap = random.randint(GAP_MIN_S, GAP_MAX_S)
        print(f"gap: {gap}s", flush=True)
        time.sleep(gap)


if __name__ == "__main__":
    main()
