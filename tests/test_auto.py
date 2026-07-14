"""Tests for auto-pipeline selection and taste priors."""


def test_pick_new_sales_filters_and_ranks():
    from wallhunter.auto import pick_new_sales
    zips = {"98040", "22101"}
    active = [
        {"id": 1, "postalCodeNumber": "98040", "pictureCount": 300},
        {"id": 2, "postalCodeNumber": "98040", "pictureCount": 50},
        {"id": 3, "postalCodeNumber": "99999", "pictureCount": 400},  # off-watchlist
        {"id": 4, "postalCodeNumber": "22101", "pictureCount": 10},   # too few photos
        {"id": 5, "postalCodeNumber": "22101", "pictureCount": 120},
        {"id": 6, "postalCodeNumber": "98040", "pictureCount": 80},   # already known
    ]
    got = pick_new_sales(active, known_ids={6}, watchlist_zips=zips,
                         min_photos=20, max_new=2)
    assert got == [1, 5]  # richest first, capped at 2


def test_pick_new_sales_saturates_monster_catalogs():
    from wallhunter.auto import pick_new_sales
    zips = {"60035"}
    active = [
        {"id": 1, "postalCodeNumber": "60035", "pictureCount": 1228},  # monster
        {"id": 2, "postalCodeNumber": "60035", "pictureCount": 450},   # also >saturation
        {"id": 3, "postalCodeNumber": "60035", "pictureCount": 300},
    ]
    got = pick_new_sales(active, set(), zips, 20, 3)
    # both >=400 rank equal on saturated count; smaller (finishable) wins
    assert got == [2, 1, 3]


def test_pick_new_sales_empty_ok():
    from wallhunter.auto import pick_new_sales
    assert pick_new_sales([], set(), {"98040"}, 20, 3) == []


def _seed_work(conn, wid: int, category: str):
    conn.execute("INSERT OR IGNORE INTO sales (id, title) VALUES (99, 'T')")
    conn.execute(
        "INSERT INTO works (id, sale_id, status, category, interest_score, tier)"
        " VALUES (?, 99, 'screened', ?, 5, 'B')", (wid, category))


def _seed_event(conn, wid: int, kind: str, reason: str | None = None):
    from wallhunter import db as wdb
    conn.execute("INSERT INTO events (ts, tool, work_id, kind, reason)"
                 " VALUES (?, 'wall-hunter', ?, ?, ?)", (wdb.now(), wid, kind, reason))


def test_pick_new_sales_prefers_estate_over_auction_catalogs():
    from wallhunter.auto import pick_new_sales
    zips = {"60035"}
    active = [
        {"id": 1, "postalCodeNumber": "60035", "pictureCount": 900, "type": 64},  # catalog
        {"id": 2, "postalCodeNumber": "60035", "pictureCount": 60, "type": 1},    # estate
        {"id": 3, "postalCodeNumber": "60035", "pictureCount": 200, "type": 4},   # moving
    ]
    got = pick_new_sales(active, set(), zips, 20, 3)
    assert got == [3, 2, 1]  # in-person sales first, catalog last resort


def test_drop_excluded_auctions_filters_liveauctioneers():
    from wallhunter.auto import drop_excluded_auctions
    details = [
        {"id": 1, "auctionUrl": "https://www.liveauctioneers.com/catalog/422108_x"},
        {"id": 2, "auctionUrl": "https://eastsideestateco.com/auction-group/294"},
        {"id": 3, "auctionUrl": None},
        {"id": 4},  # plain estate sale, no auction at all
    ]
    assert drop_excluded_auctions(details, hosts=("liveauctioneers.com",)) == [2, 3, 4]


def test_nonart_gate_files_confident_nonart_only(conn):
    from wallhunter.stage2 import apply_nonart_gate
    conn.execute("INSERT INTO sales (id, title) VALUES (12, 'S')")
    conn.execute("INSERT INTO photos (id, sale_id, file_hash) VALUES (120, 12, 'h')")
    def det(ctype, sig=0, unc=0):
        cur = conn.execute(
            "INSERT INTO detections (photo_id, bbox_x, bbox_y, bbox_w, bbox_h,"
            " coarse_type, sig_visible, uncertain, crop_hash, dhash, crop_area,"
            " description) VALUES (120,0,0,1,1,?,?,?,'c','0',100,'d')",
            (ctype, sig, unc))
        return conn.execute("INSERT INTO works (sale_id, best_detection_id, status)"
                            " VALUES (12, ?, 'queued')", (cur.lastrowid,)).lastrowid
    ring = det("jewelry")
    signed_ring = det("jewelry", sig=1)        # signed -> deep screen anyway
    odd_jewelry = det("jewelry", unc=1)        # uncertain -> deep screen
    painting = det("painting")
    coin = det("coin")
    conn.commit()
    gated = apply_nonart_gate(conn, 12)
    assert gated == 2
    status = {w: conn.execute("SELECT status, category FROM works WHERE id=?", (w,)).fetchone()
              for w in (ring, signed_ring, odd_jewelry, painting, coin)}
    assert status[ring]["status"] == "screened" and status[ring]["category"] == "jewelry"
    assert status[coin]["status"] == "screened" and status[coin]["category"] == "decor"
    assert status[signed_ring]["status"] == "queued"
    assert status[odd_jewelry]["status"] == "queued"
    assert status[painting]["status"] == "queued"


def test_stage2_queue_is_best_first(conn):
    from wallhunter.stage2 import pending_works_best_first
    conn.execute("INSERT INTO sales (id, title) VALUES (7, 'S')")
    conn.execute("INSERT INTO photos (id, sale_id, file_hash) VALUES (70, 7, 'h')")
    def det(sig, lbl, ctype, prom, area):
        cur = conn.execute(
            "INSERT INTO detections (photo_id, bbox_x, bbox_y, bbox_w, bbox_h,"
            " coarse_type, prominence, sig_visible, label_visible, crop_hash,"
            " dhash, crop_area) VALUES (70,0,0,1,1,?,?,?,?,'c','0',?)",
            (ctype, prom, sig, lbl, area))
        w = conn.execute("INSERT INTO works (sale_id, best_detection_id, status)"
                         " VALUES (7, ?, 'queued')", (cur.lastrowid,)).lastrowid
        return w
    plain_print = det(0, 0, "print", "featured", 9000)
    signed_painting = det(1, 0, "painting", "featured", 2000)
    labeled_ceramic = det(0, 1, "ceramic", "background", 1000)
    conn.commit()
    order = [r["work_id"] for r in pending_works_best_first(conn, 7)]
    # signature+painting (5.0) > label+background+ceramic (4.0) > plain print (0.5)
    assert order == [signed_painting, labeled_ceramic, plain_print]


def test_stage2_stops_spending_after_cost_cap(conn, monkeypatch):
    """Regression: the pool must not keep executing (billed) jobs after the
    cap trips. Live incident 2026-07-14: 778 pre-submitted jobs kept calling
    the API after an ~80-work cap, spending ~$11 on discarded results."""
    from wallhunter import stage2
    from wallhunter.config import CostCapExceeded, CostMeter

    conn.execute("INSERT INTO sales (id, title) VALUES (20, 'S')")
    conn.execute("INSERT INTO photos (id, sale_id, file_hash) VALUES (200, 20, 'h')")
    for i in range(40):
        cur = conn.execute(
            "INSERT INTO detections (photo_id, bbox_x, bbox_y, bbox_w, bbox_h,"
            " coarse_type, crop_hash, dhash, crop_area) VALUES"
            " (200,0,0,1,1,'painting','c','0',100)")
        conn.execute("INSERT INTO works (sale_id, best_detection_id, status)"
                     " VALUES (20, ?, 'queued')", (cur.lastrowid,))
    conn.commit()

    calls = {"n": 0}

    def fake_screen(client, meter, crop, ctx, desc, prom):
        calls["n"] += 1
        meter.total += 1.0
        if meter.total >= meter.cap:
            raise CostCapExceeded("cap")
        return ({"interest_score": 5, "category": "painting", "flags": {},
                 "medium_guess": {}, "period_guess": {}}, 1.0)

    monkeypatch.setattr(stage2, "_screen_one", fake_screen)
    monkeypatch.setattr(stage2.anthropic, "Anthropic", lambda: object())
    monkeypatch.setattr(stage2, "downscale_jpeg_b64", lambda *a, **k: "x")
    monkeypatch.setattr(stage2, "load", lambda h: object())

    stats = stage2.run_stage2(conn, 20, CostMeter(5.0), workers=3)
    # cap trips on the 5th call; a few in-flight extras are fine,
    # but nowhere near all 40 (the leak executed every job)
    assert calls["n"] <= 5 + 3, f"pool kept spending after cap: {calls['n']} calls"
    assert stats["skipped_cost"] >= 30


def test_taste_boosts_activate_at_threshold(conn):
    from wallhunter.taste import category_boosts
    for i in range(10):
        _seed_work(conn, 100 + i, "ceramics")
        _seed_event(conn, 100 + i, "save")
    for i in range(3):
        _seed_work(conn, 200 + i, "print")
        _seed_event(conn, 200 + i, "dismiss")
    conn.commit()
    boosts = category_boosts(conn)
    assert boosts.get("ceramics", 0) > 0.5          # 10 saves -> strong positive
    assert "print" not in boosts                     # only 3 events -> inactive


def test_taste_repro_dismissals_dont_punish_category(conn):
    from wallhunter.taste import category_boosts
    for i in range(9):
        _seed_work(conn, 300 + i, "painting")
        _seed_event(conn, 300 + i, "dismiss", "print/repro")
    conn.commit()
    # all negatives were repro dismissals -> category has < MIN_EVENTS real
    # judgments and stays neutral
    assert "painting" not in category_boosts(conn)


def test_taste_negative_boost(conn):
    from wallhunter.taste import category_boosts
    for i in range(12):
        _seed_work(conn, 400 + i, "glass")
        _seed_event(conn, 400 + i, "dismiss", "not my area")
    conn.commit()
    assert category_boosts(conn)["glass"] < -0.5
