"""SQLite store for lotwatcher: auctions, lots, funnel state. Resumable."""
import json
import sqlite3
import time
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS auctions (
    key TEXT PRIMARY KEY,              -- platform:id
    platform TEXT NOT NULL,            -- la | hibid
    ext_id TEXT NOT NULL,
    title TEXT, house TEXT, url TEXT,
    discovered_at REAL, ends_at TEXT,
    status TEXT DEFAULT 'new',         -- new | fetched | done | blocked | error
    lots_total INTEGER DEFAULT 0,
    note TEXT
);
CREATE TABLE IF NOT EXISTS lots (
    key TEXT PRIMARY KEY,              -- platform:lotid
    auction_key TEXT NOT NULL,
    title TEXT, estimate TEXT, bid TEXT, url TEXT,
    detail TEXT,
    stage TEXT DEFAULT 's0',           -- s0 | s1 | s3 | done | junk
    s1 TEXT, s3 TEXT,                  -- json blobs
    category TEXT, artist TEXT, promise REAL,
    flagged INTEGER DEFAULT 0,
    emailed INTEGER DEFAULT 0,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_lots_stage ON lots(stage);
CREATE INDEX IF NOT EXISTS idx_lots_flag ON lots(flagged, emailed);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect() -> sqlite3.Connection:
    config.data_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_auction(conn, platform, ext_id, title, house, url, ends_at=None) -> bool:
    """Insert if new. Returns True if this auction was not seen before."""
    key = f"{platform}:{ext_id}"
    cur = conn.execute("SELECT 1 FROM auctions WHERE key=?", (key,))
    if cur.fetchone():
        return False
    conn.execute(
        "INSERT INTO auctions(key, platform, ext_id, title, house, url, discovered_at, ends_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (key, platform, ext_id, title, house, url, time.time(), ends_at))
    conn.commit()
    return True


def set_auction_status(conn, key, status, note=None, lots_total=None):
    conn.execute("UPDATE auctions SET status=?, note=COALESCE(?,note),"
                 " lots_total=COALESCE(?,lots_total) WHERE key=?",
                 (status, note, lots_total, key))
    conn.commit()


def add_lot(conn, platform, lot_id, auction_key, title, estimate, bid, url,
            detail="") -> bool:
    key = f"{platform}:{lot_id}"
    cur = conn.execute("SELECT 1 FROM lots WHERE key=?", (key,))
    if cur.fetchone():
        return False
    conn.execute(
        "INSERT INTO lots(key, auction_key, title, estimate, bid, url, detail,"
        " updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (key, auction_key, title, estimate, bid, url, detail, time.time()))
    return True


def lots_in_stage(conn, stage, limit=100000):
    return conn.execute(
        "SELECT * FROM lots WHERE stage=? ORDER BY updated_at LIMIT ?",
        (stage, limit)).fetchall()


def update_lot(conn, key, **fields):
    fields["updated_at"] = time.time()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE lots SET {sets} WHERE key=?", (*fields.values(), key))


def unemailed_flags(conn):
    return conn.execute(
        "SELECT l.*, a.title AS auction_title, a.house, a.platform, a.url AS auction_url"
        " FROM lots l JOIN auctions a ON a.key = l.auction_key"
        " WHERE l.flagged=1 AND l.emailed=0 ORDER BY l.promise DESC").fetchall()


def mark_emailed(conn, keys):
    conn.executemany("UPDATE lots SET emailed=1 WHERE key=?", [(k,) for k in keys])
    conn.commit()


def counts(conn):
    out = {}
    for row in conn.execute("SELECT stage, COUNT(*) n FROM lots GROUP BY stage"):
        out[row["stage"]] = row["n"]
    for row in conn.execute("SELECT status, COUNT(*) n FROM auctions GROUP BY status"):
        out[f"auctions_{row['status']}"] = row["n"]
    out["flagged"] = conn.execute("SELECT COUNT(*) FROM lots WHERE flagged=1").fetchone()[0]
    return out


def get_meta(conn, k, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def set_meta(conn, k, v):
    conn.execute("INSERT INTO meta(k,v) VALUES(?,?)"
                 " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    conn.commit()
