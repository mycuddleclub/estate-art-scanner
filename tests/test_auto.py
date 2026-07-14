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
