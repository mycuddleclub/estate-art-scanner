"""SQLite store for sales, photos, detections, works, and events."""

import sqlite3
from datetime import datetime, timezone

from .config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS sales (
  id INTEGER PRIMARY KEY,
  platform TEXT NOT NULL DEFAULT 'estatesales.net',
  url TEXT,
  title TEXT, location TEXT, starts_at TEXT, ends_at TEXT,
  context_score REAL DEFAULT 0,
  fetched_at TEXT, photo_count INTEGER,
  status TEXT DEFAULT 'new'
);

CREATE TABLE IF NOT EXISTS photos (
  id INTEGER PRIMARY KEY,
  sale_id INTEGER REFERENCES sales(id),
  source_url TEXT,
  file_hash TEXT NOT NULL,
  width INTEGER, height INTEGER,
  kind TEXT DEFAULT 'photo',
  stage1_status TEXT DEFAULT 'pending',    -- pending|done|failed|skipped_cost
  stage1_cost_usd REAL,
  photo_note TEXT,
  UNIQUE(sale_id, source_url)
);

CREATE TABLE IF NOT EXISTS detections (
  id INTEGER PRIMARY KEY,
  photo_id INTEGER REFERENCES photos(id),
  bbox_x REAL, bbox_y REAL, bbox_w REAL, bbox_h REAL,   -- fractions
  coarse_type TEXT,
  description TEXT,
  sig_visible INTEGER DEFAULT 0,
  label_visible INTEGER DEFAULT 0,
  prominence TEXT,
  uncertain INTEGER DEFAULT 0,
  crop_hash TEXT,
  dhash TEXT,
  crop_area INTEGER
);

CREATE TABLE IF NOT EXISTS works (
  id INTEGER PRIMARY KEY,
  sale_id INTEGER REFERENCES sales(id),
  best_detection_id INTEGER REFERENCES detections(id),
  medium_guess TEXT, medium_basis TEXT,
  period_guess TEXT, period_basis TEXT,
  subject TEXT, quality_notes TEXT,
  category TEXT,
  sig_text TEXT,
  interest_score REAL, tier TEXT,
  sig_visible INTEGER DEFAULT 0, label_visible INTEGER DEFAULT 0,
  verso_visible INTEGER DEFAULT 0, repro_suspect INTEGER DEFAULT 0,
  background_only INTEGER DEFAULT 0, background_context TEXT,
  uncertainties TEXT,
  stage2_cost_usd REAL,
  status TEXT DEFAULT 'queued'            -- queued|screened|failed|saved|dismissed|promoted
);

CREATE TABLE IF NOT EXISTS work_detections (
  work_id INTEGER REFERENCES works(id),
  detection_id INTEGER REFERENCES detections(id),
  PRIMARY KEY (work_id, detection_id)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  tool TEXT NOT NULL DEFAULT 'wall-hunter',
  work_id INTEGER,
  kind TEXT NOT NULL,
  reason TEXT,
  payload_json TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  sale_id INTEGER,
  started_at TEXT, finished_at TEXT,
  photos_processed INTEGER DEFAULT 0,
  works_created INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0,
  status TEXT DEFAULT 'running'
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


MIGRATIONS = [
    "ALTER TABLE works ADD COLUMN category TEXT",
    "ALTER TABLE sales ADD COLUMN description TEXT",
    "ALTER TABLE sales ADD COLUMN context_note TEXT",
    "ALTER TABLE sales ADD COLUMN identity_name TEXT",
    "ALTER TABLE sales ADD COLUMN identity_verdict TEXT",
    "ALTER TABLE sales ADD COLUMN identity_evidence TEXT",
]


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    for mig in MIGRATIONS:
        try:
            conn.execute(mig)
        except sqlite3.OperationalError:
            pass  # column already exists
    return conn
