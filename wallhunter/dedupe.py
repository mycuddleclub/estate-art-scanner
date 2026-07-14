"""Merge repeat views of the same artwork into one work per sale (dhash clustering)."""

from . import db
from .config import DEDUPE_MAX_HAMMING
from .images import hamming


def run_dedupe(conn, sale_id: int) -> int:
    """Greedy single-link clustering of this sale's unassigned detections."""
    rows = conn.execute(
        "SELECT d.id, d.dhash, d.crop_area FROM detections d"
        " JOIN photos p ON p.id = d.photo_id"
        " WHERE p.sale_id=? AND d.id NOT IN (SELECT detection_id FROM work_detections)"
        " ORDER BY d.crop_area DESC",
        (sale_id,)).fetchall()
    if not rows:
        return 0

    clusters: list[list] = []
    for row in rows:
        placed = False
        for cluster in clusters:
            if any(hamming(row["dhash"], other["dhash"]) <= DEDUPE_MAX_HAMMING
                   for other in cluster):
                cluster.append(row)
                placed = True
                break
        if not placed:
            clusters.append([row])

    created = 0
    for cluster in clusters:
        best = cluster[0]  # rows pre-sorted by crop_area desc
        cur = conn.execute(
            "INSERT INTO works (sale_id, best_detection_id) VALUES (?,?)",
            (sale_id, best["id"]))
        work_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO work_detections (work_id, detection_id) VALUES (?,?)",
            [(work_id, r["id"]) for r in cluster])
        created += 1
    conn.commit()
    return created
