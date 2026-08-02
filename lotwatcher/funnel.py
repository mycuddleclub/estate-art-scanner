"""The funnel: stage 0 rules -> stage 1 Qwen -> stage 2 evidence -> stage 3 120B.
Phase-batched because both models can't co-reside in the 64 GB VRAM carve."""
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import artist_intel, config, evidence, galleries, llm, stage0, store, vision


def ingest_auction(conn, page, auction_row, fetch_lots_fn) -> int:
    """Fetch every lot of one auction, stage-0 them into the store."""
    from wallhunter.deep import mill_masters, is_mill
    a = dict(auction_row)
    lots = fetch_lots_fn(page, a["url"])

    # Fake-mill tell (Daniel's rule, ported from deep.py): a regional catalog
    # full of "original" blue-chip masters is a fraud mill — block the whole
    # auction before spending a single model token on it.
    names, claims = mill_masters(lots)
    if is_mill(names, claims):
        store.set_auction_status(
            conn, a["key"], "blocked",
            note=f"FAKE MILL: {len(names)} masters / {claims} claims"
                 f" ({', '.join(sorted(names)[:5])})")
        print(f"    MILL BLOCKED: {a['house'][:40]} — "
              f"{len(names)} blue-chip names, {claims} original-claims")
        return 0

    # Auction art density decides how its vague lots are treated (audit
    # 2026-08-02: screening every liquidation lot wasted ~95% of GPU time).
    density = stage0.art_density(lots)
    try:
        conn.execute("UPDATE auctions SET art_density=? WHERE key=?",
                     (density, a["key"]))
    except Exception:
        pass
    if density:
        print(f"    art density {density:.1%}")

    added = 0
    for l in lots:
        if not store.add_lot(conn, a["platform"], l["id"], a["key"],
                             l["title"], l["estimate"], l["bid"], l["url"],
                             detail=l.get("detail", ""), img=l.get("img", "")):
            continue
        added += 1
        key = f"{a['platform']}:{l['id']}"
        keep = stage0.lot_passes_density(
            l["title"], l.get("detail", ""), density,
            a.get("title", ""), a.get("house", ""))
        pri = 1 if stage0.strong_art(l["title"], l.get("detail", "")) else 0
        store.update_lot(conn, key, stage=("s1" if keep else "junk"),
                         s1_priority=pri)
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
            named = len(artist.split()) >= 2      # a real person-name to check
            promote = (
                s1["promise"] >= config.STAGE1_PROMISE_CUTOFF
                or (artist and evidence.standing(artist))
                # named-artist artworks clear a LOWER bar so contemporary
                # gallery artists (absent from authority.db) reach the
                # evidence stage instead of being filed blind at stage 1
                or (named and s1["promise"] >= config.STAGE1_NAMED_CUTOFF))
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



_SIG_NOISE = re.compile(
    r"\b(signed|sgd|illegible|indistinct|lower right|lower left|upper right|"
    r"upper left|verso|recto|dated|circa|ca|no\.?|titled)\b", re.I)


def _clean_signature(sig: str) -> str:
    """A transcribed signature -> a usable artist name, or '' if unusable.
    Conservative: needs >=2 name-like tokens (initials allowed), no digits."""
    if not sig:
        return ""
    s = _SIG_NOISE.sub(" ", sig)
    s = re.sub(r"[\"\u201c\u201d''`]", " ", s)
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[0-9]", " ", s)                 # dates are not names
    s = re.sub(r"[^A-Za-z.\-' ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .-'")
    toks = [t for t in s.split() if len(t) > 1 or t.endswith(".")]
    if len(toks) < 2 or len(toks) > 4:
        return ""
    if not any(len(t.strip(".")) >= 3 for t in toks):   # need a real surname
        return ""
    return " ".join(t.capitalize() if len(t) > 2 else t.upper() for t in toks)


def run_stage3(conn, la_page=None, detail_fetch_fn=None, limit=2000) -> int:
    """120B judgment on candidates, with DB evidence and (LA) detail text."""
    rows = store.lots_in_stage(conn, "s3", limit)
    if not rows:
        return 0
    print(f"stage3: {len(rows)} candidates on {config.STAGE3_MODEL}")
    llm.ensure_model(config.STAGE3_MODEL)
    # comp-engine classify_work must ride the loaded judge model, not force
    # a qwen swap mid-phase (both cannot fit the 64 GB carve)
    import os as _os
    _os.environ["LOCAL_LLM_READ_MODEL"] = config.STAGE3_MODEL

    # LA detail fetches are serial (one shared browser page), done up front
    if detail_fetch_fn and la_page is not None:
        for row in rows:
            r = dict(row)
            if r["key"].startswith("la:") and not (r.get("detail") or ""):
                try:
                    d = detail_fetch_fn(la_page, r["url"]) or ""
                    store.update_lot(conn, r["key"], detail=d)
                except Exception:
                    pass
        conn.commit()
        rows = store.lots_in_stage(conn, "s3", limit)

    # SIGNIFICANCE PRE-GATE (Daniel 2026-08-02): resolve every candidate
    # artist ONCE (cached forever) via local DBs -> model knowledge -> free web
    # search, and DROP lots whose artist is not museum-backed, Tier 1-3
    # gallery, or >= $2,000 documented auction value. The 120B then only
    # judges pre-qualified lots — faster AND far less junk.
    # ---- VISION RESCUE (Daniel's cataloguing-gap thesis) ----------------
    # A lot whose signature exists only in the PHOTO has no artist name from
    # the text screener, so it would be dropped. For strong-art lots with an
    # image we look at the picture FIRST and try to read the signature; a
    # recovered name then flows through the normal significance gate.
    if not _os.environ.get("LW_NO_RESCUE"):
        rescue_budget = int(_os.environ.get("LW_RESCUE_BUDGET", "40"))
        cands = []
        for row in rows:
            r = dict(row)
            if len((r.get("artist") or "").strip().split()) >= 2:
                continue                      # already named
            if not r.get("img"):
                continue
            if not stage0.strong_art(r.get("title", ""), r.get("detail", "")):
                continue                      # only real art lots are worth it
            cands.append(r)
        cands.sort(key=lambda r: -(r.get("promise") or 0))
        cands = cands[:rescue_budget]

        if cands:
            print(f"  vision rescue: reading signatures on {len(cands)} unnamed art lots")
            found = 0

            def _rescue(r):
                v = vision.read_lot(r["img"], r.get("title", ""))
                return r, v

            with ThreadPoolExecutor(max_workers=int(
                    _os.environ.get("LW_VISION_WORKERS", "6"))) as rex:
                for fut in as_completed([rex.submit(_rescue, r) for r in cands]):
                    try:
                        r, v = fut.result()
                    except Exception:
                        continue
                    if not v:
                        continue
                    sig = (v.get("signature") or "").strip()
                    name = _clean_signature(sig) if v.get("signature_legible") else ""
                    store.update_lot(conn, r["key"], vision=json.dumps(v),
                                     **({"artist": name} if name else {}))
                    if name:
                        found += 1
                        print(f"    signature -> {name}  ({r['title'][:44]})")
            conn.commit()
            print(f"  vision rescue: recovered {found} artist names")
            if found:
                rows = store.lots_in_stage(conn, "s3", limit)   # reload w/ names

    _profiles = {}
    if not _os.environ.get("LW_NO_GATE"):
        cand_names = [(dict(r).get("artist") or "").strip() for r in rows]
        try:
            _profiles = artist_intel.resolve([n for n in cand_names if n])
        except Exception as e:
            print(f"  artist_intel error (gate open this cycle): {str(e)[:100]}")
            _profiles = {}
        kept, dropped, deferred = [], 0, 0
        for row in rows:
            r = dict(row)
            a = (r.get("artist") or "").strip()
            # No artist name => can never flag (Daniel's hard rule), so judging
            # it is pure waste. NOTE: this also means an uncatalogued work whose
            # signature only appears in the PHOTO is missed — see vision-rescue.
            if len(a.split()) < 2:
                store.update_lot(conn, r["key"], stage="done", flagged=0,
                                 s3=json.dumps({"flag": "NO",
                                                "reasoning": "no identifiable artist name"}))
                dropped += 1
                continue
            pr = _profiles.get(artist_intel._key(a)) if a else None
            if pr and pr.get("deferred"):
                deferred += 1        # undetermined — stays in s3 for next cycle
            elif pr and not pr.get("significant"):
                store.update_lot(conn, r["key"], stage="done", flagged=0,
                                 s3=json.dumps({"flag": "NO",
                                                "reasoning": "artist not significant: "
                                                             + (pr.get("why") or "no museum, gallery or auction record"),
                                                "_gate": pr}))
                dropped += 1
            else:
                kept.append(row)
        conn.commit()
        if dropped or deferred:
            print(f"  gate: dropped {dropped} insignificant, {deferred} deferred,"
                  f" {len(kept)} to judge")
        rows = kept
        if not rows:
            return 0

    # Main thread: all lotwatcher.db + local-sqlite evidence reads (not
    # thread-safe). Build everything EXCEPT vision here.
    base = []
    for row in rows:
        r = dict(row)
        auction = conn.execute("SELECT * FROM auctions WHERE key=?",
                               (r["auction_key"],)).fetchone()
        auction = dict(auction) if auction else {}
        s1 = json.loads(r["s1"]) if r["s1"] else {}
        detail = r.get("detail") or ""
        ev = evidence.gather(r.get("artist") or "", deep=True)
        cl = evidence.comp_line({**r, "detail": detail}, r.get("artist") or "")
        if cl:
            ev = (ev + "\n" + cl) if ev else cl
        gl = galleries.evidence_line(r.get("artist") or "")
        if gl:
            ev = (ev + "\n" + gl) if ev else gl
        pr = _profiles.get(artist_intel._key((r.get("artist") or "").strip()))
        if pr:
            bits = []
            if pr.get("museums"):
                bits.append("museums: " + pr["museums"])
            if pr.get("gallery"):
                t = pr.get("gallery_tier") or 0
                bits.append(f"gallery: {pr['gallery']}" + (f" (Tier {t})" if t else ""))
            if pr.get("market_high"):
                bits.append(f"documented auction high ${pr['market_high']:,.0f}")
            if bits:
                line = "ARTIST SIGNIFICANCE (" + pr.get("source", "?") + "): " + " | ".join(bits)
                ev = (ev + "\n" + line) if ev else line
        base.append([r, s1, detail, ev, auction])

    prepared = [tuple(b) for b in base]

    def judge(item):
        r, s1, detail, ev, auction = item
        s3 = llm.stage3_judge({**r, "detail": detail}, s1, ev, auction)
        return r, s1, detail, ev, auction, s3

    # PASS 1: text-only judgment (fast, parallel). Vision is NOT run here.
    workers = int(_os.environ.get("LW_STAGE3_WORKERS", "6"))
    judged = []
    n = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(judge, it) for it in prepared]):
            try:
                judged.append(fut.result())
            except Exception as e:
                print(f"  stage3 error: {str(e)[:120]}")
            n += 1
            if n % 25 == 0:
                print(f"  stage3 {n}/{len(rows)}")

    def _flag(s3, r):
        f = 1 if str(s3.get("flag", "")).upper().startswith("Y") else 0
        if f and not (r.get("artist") or "").strip():   # no name = no flag
            f = 0
        return f

    flagged_items = [t for t in judged if _flag(t[5], t[0])]

    # PASS 2: VISION only on the flags (Daniel's call — vision where it counts).
    # Looks at the photo to (a) enrich the digest with a signature/condition and
    # (b) veto a false positive if the image plainly doesn't match the listing.
    if not _os.environ.get("LW_NO_VISION") and flagged_items:
        vworkers = int(_os.environ.get("LW_VISION_WORKERS", "6"))
        print(f"  vision on {len(flagged_items)} flagged lots")

        def _vis(t):
            r = t[0]
            return (r["key"],
                    vision.read_lot(r["img"], r.get("title", "")) if r.get("img") else None)

        vmap = {}
        with ThreadPoolExecutor(max_workers=vworkers) as vex:
            for fut in as_completed([vex.submit(_vis, t) for t in flagged_items]):
                k, v = fut.result()
                vmap[k] = v
    else:
        vmap = {}

    # persist everything, applying the SIGNIFICANCE GATE (Daniel 2026-08-02):
    # a judge-YES only becomes a real flag if the artist is museum-backed,
    # Tier 1-3 gallery, OR has >= $2,000 documented auction value. Everything
    # else is a "researchable unknown" and is dropped.
    import re as _re
    MIN_VALUE = float(_os.environ.get("LW_MIN_VALUE", "2000"))
    _POSTER = _re.compile(r"\b(poster|repro(duction)?|giclee|giclée|"
                          r"offset lithograph|photo.?mechanical)\b", _re.I)
    for r, s1, detail, ev, auction, s3 in judged:
        f = _flag(s3, r)
        artist = (r.get("artist") or "").strip()
        pr = _profiles.get(artist_intel._key(artist)) if artist else None
        gate = dict(pr) if pr else {}
        if f:
            if pr and not pr.get("significant"):
                f = 0
            # poster/reproduction kill (Daniel: "thought we got rid of that")
            if f and _POSTER.search(r.get("title", "")) and not (
                    pr and (pr.get("museums") or (pr.get("gallery_tier") or 0) in (1, 2, 3))):
                f = 0
        v = vmap.get(r["key"])
        vjson = None
        if v:
            vjson = json.dumps(v)
            if v.get("matches_listing") is False:
                f = 0
        # fold the gate facts into the stored s3 so the digest can show them
        if gate:
            s3 = {**s3, "_gate": gate}
        store.update_lot(conn, r["key"], s3=json.dumps(s3), flagged=f,
                         stage="done", vision=vjson,
                         promise=float(s3.get("score", r["promise"] or 0)))
    conn.commit()
    return len(judged)
