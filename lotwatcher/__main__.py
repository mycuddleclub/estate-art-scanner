"""CLI: python -m lotwatcher {discover|cycle|daemon|status|digest}

discover  — find new auctions on both platforms, record them (no lot fetch)
cycle     — one full pass: discover -> fetch lots -> stage1 -> stage3 -> digest
daemon    — cycle forever (LW_CYCLE_HOURS between, default 3); browser stays open
status    — pipeline counts
digest    — send any unemailed flags now
"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

from . import browser as B
from . import config, digest, funnel, hibid_source, la_source, mutualart, stage0, store

CYCLE_HOURS = float(os.environ.get("LW_CYCLE_HOURS", "3"))


def do_discover(conn, page) -> int:
    new = 0
    print("== discover: HiBid (GraphQL) ==")
    skipped = 0
    for a in (hibid_source.discover() if "hibid" in config.PLATFORMS else []):
        if stage0.auction_skippable(a["title"], a["house"]):
            skipped += 1
            continue
        if store.upsert_auction(conn, "hibid", a["id"], a["title"], a["house"],
                                a["url"], a.get("ends_at")):
            new += 1
    print("== discover: LiveAuctioneers (browser) ==")
    for a in (la_source.discover(page) if "la" in config.PLATFORMS else []):
        if stage0.auction_skippable(a["title"], a["house"]):
            skipped += 1
            continue
        if store.upsert_auction(conn, "la", a["id"], a["title"], a["house"], a["url"]):
            new += 1
    print(f"discover: {new} new auctions ({skipped} skipped: blocked/non-art genre)")
    return new


def do_fetch(conn, page) -> int:
    plats = ",".join("'" + p + "'" for p in config.PLATFORMS)
    rows = conn.execute(
        f"SELECT * FROM auctions WHERE status='new' AND platform IN ({plats})"
        " ORDER BY (ends_at IS NULL), ends_at, discovered_at LIMIT ?",
        (config.MAX_AUCTIONS_PER_CYCLE,)).fetchall()
    total = 0
    for i, a in enumerate(rows, 1):
        fetch = la_source.fetch_lots if a["platform"] == "la" else hibid_source.fetch_lots
        print(f"[{i}/{len(rows)}] lots for {a['platform']}:{a['ext_id']} — {a['title'][:60]}")
        try:
            n = funnel.ingest_auction(conn, page, a, fetch)
            total += n
            print(f"    {n} new lots")
        except Exception as e:
            store.set_auction_status(conn, a["key"], "error", note=str(e)[:200])
            print(f"    ERROR: {str(e)[:120]}")
        B.polite_sleep(config.LA_AUCTION_DELAY_S)
    return total


def do_cycle(conn, page):
    do_discover(conn, page)
    do_fetch(conn, page)
    funnel.run_stage1(conn)
    mutualart.run(conn)          # fresh tier-A comps for top candidates
    funnel.run_stage3(conn, la_page=page, detail_fetch_fn=la_source.fetch_detail)
    digest.send_digest(conn)
    print("cycle done:", dict(store.counts(conn)))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    conn = store.connect()

    if cmd == "status":
        for k, v in sorted(store.counts(conn).items()):
            print(f"  {k:>18}: {v}")
        return

    if cmd == "digest":
        digest.send_digest(conn)
        return

    with sync_playwright() as p:
        ctx = B.launch(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if cmd == "discover":
            do_discover(conn, page)
        elif cmd == "cycle":
            do_cycle(conn, page)
        elif cmd == "daemon":
            print(f"daemon: cycle every {CYCLE_HOURS}h; browser window stays open")
            while True:
                try:
                    do_cycle(conn, page)
                except Exception as e:
                    print(f"cycle error: {str(e)[:200]}")
                    B.send_alert("Lot Watcher cycle error", str(e)[:2000])
                time.sleep(CYCLE_HOURS * 3600)
        else:
            print(__doc__)
        ctx.close()


if __name__ == "__main__":
    main()
