"""The funnel: stage 0 rules -> stage 1 Qwen -> stage 2 evidence -> stage 3 120B.
Phase-batched because both models can't co-reside in the 64 GB VRAM carve."""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config, evidence, llm, stage0, store


def ingest_auction(conn, page, auction_row, fetch_lots_fn) -> int:
    """Fetch every lot of one auction, stage-0 them into the store."""
    a = dict(auction_row)
    lots = fetch_lots_fn(page, a["url"])
    added = 0
    for l in lots:
        if not store.add_lot(conn, a["platform"], l["id"], a["key"],
                             l["title"], l["estimate"], l["bid"], l["url"]):
            continue
        added += 1
        key = f"{a['platform']}:{l['id']}"
        store.update_lot(conn, key,
                         stage=("s1" if stage0.lot_passes(l["title"]) else "junk"))
    conn.commit()
    store.set_auction_status(conn, a["key"], "fetched", lots_total=len(lots))
    return added


def run_stage1(conn, limit=100000) -> int:
    """Qwen classification for everything in s1. Parallel workers."""
    rows = store.lots_in_stage(conn, "s1", limit)
    if not rows:
        return 0
    print(f"stage1: {len(rows)} lots on {config.STAGE1_MODEL}")
    llm.ensure_model(config.STAGE1_MODEL)
    done = 0

    def work(row):
        try:
            return row["key"], llm.stage1_classify(dict(row)), None
        except Exception as e:
            return row["key"], None, str(e)[:200]

    with ThreadPoolExecutor(max_workers=config.STAGE1_WORKERS) as ex:
        futures = [ex.submit(work, r) for r in rows]
        for fut in as_completed(futures):
            key, s1, err = fut.result()
            if s1 is None:
                store.update_lot(conn, key, stage="s1")   # stays; retried next cycle
                continue
            artist = (s1.get("artist") or "").strip()
            promote = (s1["promise"] >= config.STAGE1_PROMISE_CUTOFF
                       or (artist and evidence.standing(artist)))
            store.update_lot(
                conn, key,
                s1=json.dumps(s1), category=s1.get("category", "other"),
                artist=artist, promise=s1["promise"],
                stage=("s3" if promote and s1.get("is_art") is not False else "done"))
            done += 1
            if done % 200 == 0:
                conn.commit()
                print(f"  stage1 {done}/{len(rows)}")
    conn.commit()
    return done


def run_stage3(conn, la_page=None, detail_fetch_fn=None, limit=2000) -> int:
    """120B judgment on candidates, with DB evidence and (LA) detail text."""
    rows = store.lots_in_stage(conn, "s3", limit)
    if not rows:
        return 0
    print(f"stage3: {len(rows)} candidates on {config.STAGE3_MODEL}")
    llm.ensure_model(config.STAGE3_MODEL)
    n = 0
    for row in rows:
        r = dict(row)
        auction = conn.execute("SELECT * FROM auctions WHERE key=?",
                               (r["auction_key"],)).fetchone()
        auction = dict(auction) if auction else {}
        s1 = json.loads(r["s1"]) if r["s1"] else {}
        detail = r.get("detail") or ""
        if (not detail and detail_fetch_fn and la_page is not None
                and r["key"].startswith("la:")):
            try:
                detail = detail_fetch_fn(la_page, r["url"]) or ""
                store.update_lot(conn, r["key"], detail=detail)
            except Exception:
                pass
        ev = evidence.gather(r.get("artist") or "", deep=True)
        cl = evidence.comp_line({**r, "detail": detail}, r.get("artist") or "")
        if cl:
            ev = (ev + "\n" + cl) if ev else cl
        try:
            s3 = llm.stage3_judge({**r, "detail": detail}, s1, ev, auction)
        except Exception as e:
            print(f"  stage3 error {r['key']}: {str(e)[:120]}")
            continue
        flagged = 1 if str(s3.get("flag", "")).upper().startswith("Y") else 0
        store.update_lot(conn, r["key"], s3=json.dumps(s3), flagged=flagged,
                         stage="done",
                         promise=float(s3.get("score", r["promise"] or 0)))
        n += 1
        if n % 25 == 0:
            conn.commit()
            print(f"  stage3 {n}/{len(rows)}")
    conn.commit()
    return n
