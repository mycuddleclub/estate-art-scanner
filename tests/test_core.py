"""Unit tests for Wall Hunter core logic (no network, no API)."""

import json

from PIL import Image, ImageDraw


# ── URL parsing (regression: ZIP code vs sale id) ────────────────────────────

def test_parse_sale_ref_takes_last_numeric_segment():
    from wallhunter.ingest import parse_sale_ref
    assert parse_sale_ref("https://www.estatesales.net/WA/Mercer-Island/98040/4984500") == 4984500
    assert parse_sale_ref("https://www.estatesales.net/VA/Mc-Lean/22101/4960104") == 4960104
    assert parse_sale_ref("https://estatesales.net/CA/X/92091/123?utm=1") == 123


def test_parse_sale_ref_bare_id_and_rejects():
    from wallhunter.ingest import parse_sale_ref
    assert parse_sale_ref("4984500") == 4984500
    assert parse_sale_ref("https://example.com/98040/777") is None
    assert parse_sale_ref("nonsense") is None


# ── perceptual hash ───────────────────────────────────────────────────────────

def _img(seed: int, size=(200, 150)) -> Image.Image:
    img = Image.new("RGB", size, ((seed * 37) % 255, (seed * 91) % 255, (seed * 53) % 255))
    d = ImageDraw.Draw(img)
    for i in range(6):
        x = (seed * (i + 3) * 17) % size[0]
        y = (seed * (i + 5) * 13) % size[1]
        d.rectangle([x, y, x + 40, y + 25], fill=((seed + i * 40) % 255, 30 * i % 255, 200))
    return img


def test_dhash_identical_and_resized_match():
    from wallhunter.images import dhash, hamming
    a = _img(7)
    b = a.resize((400, 300))  # same content, different resolution
    assert hamming(dhash(a), dhash(b)) <= 4


def test_dhash_different_images_differ():
    from wallhunter.images import dhash, hamming
    assert hamming(dhash(_img(7)), dhash(_img(31))) > 10


# ── stage-1 box validation ────────────────────────────────────────────────────

def test_valid_box():
    from wallhunter.stage1 import _valid_box
    assert _valid_box([0.1, 0.2, 0.3, 0.4]) == (0.1, 0.2, 0.3, 0.4)
    assert _valid_box([0.0, 0.0, 1.0, 1.0]) is not None
    assert _valid_box([0.5, 0.5, 0.0, 0.2]) is None          # zero width
    assert _valid_box([-0.2, 0.1, 0.3, 0.3]) is None          # negative origin
    assert _valid_box([0.9, 0.9, 0.5, 0.5]) is None           # far out of frame
    assert _valid_box(["a", 0, 0, 0]) is None                 # junk types
    assert _valid_box(None) is None


# ── tiers ────────────────────────────────────────────────────────────────────

def test_tier_thresholds():
    from wallhunter.stage2 import _tier
    assert _tier(9.0) == "A"
    assert _tier(7.5) == "A"
    assert _tier(7.4) == "B"
    assert _tier(5.0) == "B"
    assert _tier(4.9) == "C"


# ── dedupe clustering ────────────────────────────────────────────────────────

def _seed_detection(conn, photo_id: int, dh: str, area: int) -> int:
    cur = conn.execute(
        "INSERT INTO detections (photo_id, bbox_x, bbox_y, bbox_w, bbox_h,"
        " coarse_type, description, crop_hash, dhash, crop_area)"
        " VALUES (?,0.1,0.1,0.5,0.5,'painting','x','deadbeef'||?, ?, ?)",
        (photo_id, dh, dh, area))
    return cur.lastrowid


def test_dedupe_merges_similar_keeps_distinct(conn):
    from wallhunter.dedupe import run_dedupe
    conn.execute("INSERT INTO sales (id, title) VALUES (1, 't')")
    conn.execute("INSERT INTO photos (id, sale_id, file_hash) VALUES (1, 1, 'h1')")
    conn.execute("INSERT INTO photos (id, sale_id, file_hash) VALUES (2, 1, 'h2')")
    # two near-identical hashes (1 bit apart), one distant
    _seed_detection(conn, 1, "0000000000000000", 5000)
    _seed_detection(conn, 2, "0000000000000001", 9000)   # bigger view, same work
    _seed_detection(conn, 2, "ffffffffffffffff", 4000)
    conn.commit()
    created = run_dedupe(conn, 1)
    assert created == 2
    # best view of the merged work is the largest crop
    best = conn.execute(
        "SELECT d.crop_area FROM works w JOIN detections d ON d.id=w.best_detection_id"
        " WHERE w.sale_id=1 ORDER BY d.crop_area DESC").fetchall()
    assert best[0]["crop_area"] == 9000
    # idempotent: second run creates nothing new
    assert run_dedupe(conn, 1) == 0


# ── events / actions schema ──────────────────────────────────────────────────

def test_action_updates_status_and_logs_event(conn, tiny_jpeg):
    from wallhunter import db as wdb
    conn.execute("INSERT INTO sales (id, title) VALUES (5, 's')")
    conn.execute("INSERT INTO photos (id, sale_id, file_hash) VALUES (9, 5, 'ph')")
    det = _seed_detection(conn, 9, "00000000000000ff", 100)
    conn.execute(
        "INSERT INTO works (id, sale_id, best_detection_id, status, interest_score, tier)"
        " VALUES (77, 5, ?, 'screened', 6.0, 'B')", (det,))
    conn.commit()
    conn.execute("UPDATE works SET status='dismissed' WHERE id=77")
    conn.execute(
        "INSERT INTO events (ts, tool, work_id, kind, reason, payload_json)"
        " VALUES (?,?,?,?,?,?)",
        (wdb.now(), "wall-hunter", 77, "dismiss", "print/repro",
         json.dumps({"from_status": "screened"})))
    conn.commit()
    ev = conn.execute("SELECT * FROM events WHERE work_id=77").fetchone()
    assert ev["kind"] == "dismiss" and ev["reason"] == "print/repro"


# ── report generation ────────────────────────────────────────────────────────

def test_report_renders_and_hides_categories(conn, tiny_jpeg):
    from wallhunter.images import store_bytes
    from wallhunter.report import build_report
    crop = store_bytes(tiny_jpeg)
    conn.execute("INSERT INTO sales (id, title, url) VALUES (3, 'Test Sale', 'https://x')")
    conn.execute("INSERT INTO photos (id, sale_id, file_hash, source_url)"
                 " VALUES (4, 3, ?, 'https://p')", (crop,))
    cur = conn.execute(
        "INSERT INTO detections (photo_id, bbox_x, bbox_y, bbox_w, bbox_h, crop_hash,"
        " dhash, crop_area, description) VALUES (4,0,0,1,1,?, '0',100,'d')", (crop,))
    det = cur.lastrowid
    for wid, cat, score in ((1, "painting", 8.0), (2, "jewelry", 6.0)):
        conn.execute(
            "INSERT INTO works (id, sale_id, best_detection_id, status, interest_score,"
            " tier, category, subject, uncertainties) VALUES (?,3,?,'screened',?,?,?,?,?)",
            (wid, det, score, "A" if score >= 7.5 else "B", cat, f"subject-{cat}", "[]"))
        conn.execute("INSERT INTO work_detections (work_id, detection_id) VALUES (?,?)",
                     (wid, det))
    conn.commit()
    path = build_report(conn, 3, open_after=False)
    html_text = open(path).read()
    assert "subject-painting" in html_text
    assert "hidden-cat" in html_text            # jewelry card carries hidden class
    assert "1 works" in html_text               # hidden-count toggle bar present


def test_report_tolerates_corrupt_uncertainties(conn, tiny_jpeg):
    from wallhunter.images import store_bytes
    from wallhunter.report import build_report
    crop = store_bytes(tiny_jpeg)
    conn.execute("INSERT INTO sales (id, title) VALUES (8, 'S8')")
    conn.execute("INSERT INTO photos (id, sale_id, file_hash) VALUES (6, 8, ?)", (crop,))
    cur = conn.execute(
        "INSERT INTO detections (photo_id, bbox_x, bbox_y, bbox_w, bbox_h, crop_hash,"
        " dhash, crop_area) VALUES (6,0,0,1,1,?,'0',100)", (crop,))
    conn.execute(
        "INSERT INTO works (id, sale_id, best_detection_id, status, interest_score,"
        " tier, category, uncertainties) VALUES (11,8,?,'screened',5.0,'B','painting',"
        " '[\"truncated str')", (cur.lastrowid,))
    conn.commit()
    build_report(conn, 8, open_after=False)  # must not raise
