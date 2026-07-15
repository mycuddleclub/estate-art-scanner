"""Wall Hunter CLI.

  python -m wallhunter add <estatesales-url | sale-id | folder> [--max-photos N]
  python -m wallhunter run [--sale ID] [--cost-cap USD]
  python -m wallhunter report [--sale ID]
  python -m wallhunter status
"""

import argparse
import os
import sys
from pathlib import Path

from . import db
from .config import RUN_COST_CAP_USD, CostMeter, anthropic_api_key
from .dedupe import run_dedupe
from .ingest import add_estatesales, add_folder, parse_sale_ref
from .report import build_report
from .stage1 import run_stage1
from .stage2 import run_stage2


def _latest_sale(conn) -> int:
    row = conn.execute("SELECT id FROM sales ORDER BY fetched_at DESC LIMIT 1").fetchone()
    if not row:
        raise SystemExit("no sales ingested yet — run `add` first")
    return row["id"]


def cmd_add(conn, args):
    if Path(args.ref).is_dir():
        add_folder(conn, Path(args.ref), args.max_photos)
        return
    if "hibid.com" in args.ref:
        from .hibid import add_hibid
        add_hibid(conn, args.ref, args.max_photos, force=args.force)
        return
    sale_id = parse_sale_ref(args.ref)
    if sale_id is None:
        raise SystemExit(f"not an EstateSales.net/HiBid URL, sale id, or folder: {args.ref}")
    add_estatesales(conn, sale_id, args.max_photos, force=args.force)


def cmd_run(conn, args):
    os.environ.setdefault("ANTHROPIC_API_KEY", anthropic_api_key())
    sale_id = args.sale or _latest_sale(conn)
    meter = CostMeter(args.cost_cap)
    cur = conn.execute(
        "INSERT INTO runs (sale_id, started_at) VALUES (?,?)", (sale_id, db.now()))
    run_id = cur.lastrowid
    conn.commit()

    print(f"== run {run_id}: sale {sale_id}, cost cap ${meter.cap:.2f} ==")
    s1 = run_stage1(conn, sale_id, meter)
    print(f"stage1: {s1}")
    merged = run_dedupe(conn, sale_id)
    print(f"dedupe: {merged} unique works")
    s2 = run_stage2(conn, sale_id, meter)
    print(f"stage2: {s2}")

    conn.execute(
        "UPDATE runs SET finished_at=?, photos_processed=?, works_created=?,"
        " cost_usd=?, status=? WHERE id=?",
        (db.now(), s1["photos"], merged, meter.total,
         "capped" if (s1["skipped_cost"] or s2["skipped_cost"]) else "done", run_id))
    conn.commit()
    tiers = conn.execute(
        "SELECT tier, COUNT(*) n FROM works WHERE sale_id=? AND status='screened'"
        " GROUP BY tier ORDER BY tier", (sale_id,)).fetchall()
    tier_str = " ".join(f"{r['tier']}:{r['n']}" for r in tiers) or "none"
    print(f"== done: {s1['photos']} photos, {merged} works ({tier_str}), "
          f"${meter.total:.2f}, {s1['failed'] + s2['failed']} failures ==")


def cmd_report(conn, args):
    build_report(conn, args.sale or _latest_sale(conn))


def cmd_exclusives(conn, args):
    from .exclusives import find_exclusives
    exclusives = find_exclusives(force_refresh=args.refresh)
    for a in exclusives:
        print(f"[{a['platform']}] {a['house']} — {a['title']}"
              f"{'  (' + a['info'] + ')' if a['info'] else ''}\n    {a['url']}")
    deep_flags = deep_stats = None
    if args.deep:
        import os
        from .artists import import_artscout_cache, import_checker_cache
        from .config import anthropic_api_key
        from .deep import deep_scan
        os.environ.setdefault("ANTHROPIC_API_KEY", anthropic_api_key())
        # refresh shared knowledge from sibling tools when readable
        import_checker_cache(conn)
        import_artscout_cache(conn)
        _, deep_stats = deep_scan(conn, exclusives,
                                  research_cap_usd=args.research_cap,
                                  max_auctions=args.max_auctions)
        # email from the store, not the run: flags found by runs that didn't
        # email (e.g. the overnight backfill) go out on the next send
        deep_flags = [
            {"url": r["lot_url"], "title": r["title"], "house": r["house"],
             "high_bid_usd": r["high_bid_usd"], "estimate": r["estimate"],
             "artist": r["artist"] or r["artist_key"] or "?",
             "reason": r["info"], "market_note": r["market_note"] or "",
             "evidence": (r["evidence"] or "")[:200]}
            for r in conn.execute(
                "SELECT dl.*, a.artist, a.market_note, a.evidence"
                " FROM deep_lots dl LEFT JOIN artists a"
                "   ON a.artist_key = dl.artist_key"
                " WHERE dl.emailed=0 AND dl.info != ''"
                " ORDER BY dl.first_seen DESC")]
        for f in deep_flags:
            print(f"🎯 {f['artist']}: {f['title'][:60]} — {f['reason']}\n    {f['url']}")
    if args.email:
        from .mailer import send_exclusives_email
        if send_exclusives_email(exclusives, deep_flags=deep_flags,
                                 deep_stats=deep_stats) and deep_flags:
            conn.execute("UPDATE deep_lots SET emailed=1"
                         " WHERE emailed=0 AND info != ''")
            conn.commit()


def cmd_auto(conn, args):
    from .auto import run_auto
    run_auto(conn, max_new=args.max_new, daily_cap=args.daily_cap,
             per_sale_cap=args.per_sale_cap, email=not args.no_email)


def cmd_serve(conn, args):
    conn.close()
    import subprocess
    import uvicorn
    from .web import app
    subprocess.Popen(["open", f"http://127.0.0.1:{args.port}/"])
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


def cmd_status(conn, _args):
    for s in conn.execute(
            "SELECT s.id, s.title, s.photo_count,"
            " (SELECT COUNT(*) FROM photos WHERE sale_id=s.id AND stage1_status='done') done,"
            " (SELECT COUNT(*) FROM works WHERE sale_id=s.id AND status='screened') works,"
            " (SELECT COUNT(*) FROM works WHERE sale_id=s.id AND tier='A'"
            "   AND status='screened') a_tier"
            " FROM sales s ORDER BY s.fetched_at DESC"):
        print(f"[{s['id']}] {(s['title'] or '')[:48]:<48} photos {s['done']}/{s['photo_count'] or '?'}"
              f"  works {s['works']}  A-tier {s['a_tier']}")


def main():
    ap = argparse.ArgumentParser(prog="wallhunter")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="ingest a sale's photos")
    p.add_argument("ref", help="EstateSales.net URL, sale id, or image folder")
    p.add_argument("--max-photos", type=int, default=None)
    p.add_argument("--force", action="store_true",
                   help="scan even if the seller matches the blocked-house list")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("run", help="detect, dedupe, screen")
    p.add_argument("--sale", type=int, default=None)
    p.add_argument("--cost-cap", type=float, default=RUN_COST_CAP_USD)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("report", help="build + open ranked HTML report")
    p.add_argument("--sale", type=int, default=None)
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("status", help="pipeline status per sale")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("serve", help="open the review queue (localhost)")
    p.add_argument("--port", type=int, default=8787)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("exclusives", help="HiBid/Bidsquare auctions not on LA/Invaluable")
    p.add_argument("--refresh", action="store_true",
                   help="re-harvest the LA/Invaluable house set (ignores 20h cache)")
    p.add_argument("--email", action="store_true",
                   help="send the Off-Radar Auctions email")
    p.add_argument("--deep", action="store_true",
                   help="per-lot artist intelligence on each off-radar auction")
    p.add_argument("--research-cap", type=float, default=3.0,
                   help="max USD for researching new artist names")
    p.add_argument("--max-auctions", type=int, default=None)
    p.set_defaults(fn=cmd_exclusives)

    p = sub.add_parser("auto", help="morning batch: resume + new watchlist sales + digest")
    p.add_argument("--max-new", type=int, default=2)
    p.add_argument("--daily-cap", type=float, default=5.0)
    p.add_argument("--per-sale-cap", type=float, default=None,
                   help="max spend per sale per day (default: daily-cap / 2)")
    p.add_argument("--no-email", action="store_true")
    p.set_defaults(fn=cmd_auto)

    args = ap.parse_args()
    conn = db.connect()
    try:
        args.fn(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
