"""The Reference Library: local institutional artist authority database.

A separate SQLite file (wh_data/authority.db) distilled from open museum and
vocabulary datasets (Getty ULAN, Smithsonian Open Access, Met, NGA, AIC, MoMA,
Whitney, Cleveland...). It answers, offline and for free:

  - is this string a documented artist? (variant spellings included)
  - which museums hold their work, and do the Archives of American Art
    hold their papers?
  - life dates / nationality (for context and sanity checks)

Design guarantee (per Daniel): absence from the library is NEUTRAL — it can
only ever upgrade an artist, never cause a lot to be skipped.
"""

import sqlite3

from .artists import artist_key
from .config import DATA_DIR

AUTHORITY_DB = DATA_DIR / "authority.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS artists_authority (
  id INTEGER PRIMARY KEY,
  canonical TEXT NOT NULL,
  norm_key TEXT NOT NULL,
  ulan_id TEXT,
  wikidata_qid TEXT,
  birth_year INTEGER,
  death_year INTEGER,
  nationality TEXT,
  aaa_papers INTEGER NOT NULL DEFAULT 0,
  sources TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_norm ON artists_authority(norm_key);
CREATE INDEX IF NOT EXISTS idx_auth_ulan ON artists_authority(ulan_id);

CREATE TABLE IF NOT EXISTS name_variants (
  variant_key TEXT NOT NULL,
  artist_id INTEGER NOT NULL REFERENCES artists_authority(id),
  source TEXT,
  UNIQUE(variant_key, artist_id)
);
CREATE INDEX IF NOT EXISTS idx_variant ON name_variants(variant_key);

CREATE TABLE IF NOT EXISTS holdings (
  artist_id INTEGER NOT NULL REFERENCES artists_authority(id),
  institution TEXT NOT NULL,
  works INTEGER NOT NULL DEFAULT 1,
  UNIQUE(artist_id, institution)
);

CREATE TABLE IF NOT EXISTS sources_meta (
  source TEXT PRIMARY KEY,
  imported_at TEXT,
  records INTEGER,
  note TEXT
);
"""

# Institutions whose holdings count toward the "major museum" signal.
MAJOR_MUSEUMS = {"met", "nga", "saam", "npg", "aic", "moma", "whitney",
                 "cleveland", "hmsg", "chndm", "nmafa", "acm"}


def connect(path=None) -> sqlite3.Connection:
    p = path or AUTHORITY_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def upsert_artist(conn, canonical: str, source: str, *, ulan_id=None,
                  wikidata_qid=None, birth_year=None, death_year=None,
                  nationality=None, aaa_papers=False) -> int | None:
    """Insert or merge an artist; returns artist id (None for junk names)."""
    key = artist_key(canonical)
    if len(key) < 4 or " " not in key:
        return None  # single-word / tiny names are too collision-prone to store
    row = None
    if ulan_id:
        row = conn.execute("SELECT * FROM artists_authority WHERE ulan_id=?",
                           (ulan_id,)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM artists_authority WHERE norm_key=?",
                           (key,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO artists_authority (canonical, norm_key, ulan_id,"
            " wikidata_qid, birth_year, death_year, nationality, aaa_papers,"
            " sources) VALUES (?,?,?,?,?,?,?,?,?)",
            (canonical.strip(), key, ulan_id, wikidata_qid, birth_year,
             death_year, nationality, 1 if aaa_papers else 0, source))
        aid = cur.lastrowid
    else:
        aid = row["id"]
        sources = set((row["sources"] or "").split(",")) - {""}
        sources.add(source)
        conn.execute(
            "UPDATE artists_authority SET"
            " ulan_id=COALESCE(ulan_id, ?),"
            " wikidata_qid=COALESCE(wikidata_qid, ?),"
            " birth_year=COALESCE(birth_year, ?),"
            " death_year=COALESCE(death_year, ?),"
            " nationality=COALESCE(nationality, ?),"
            " aaa_papers=MAX(aaa_papers, ?),"
            " sources=? WHERE id=?",
            (ulan_id, wikidata_qid, birth_year, death_year, nationality,
             1 if aaa_papers else 0, ",".join(sorted(sources)), aid))
    add_variant(conn, canonical, aid, source)
    return aid


def sorted_key(key: str) -> str:
    """Word-order-insensitive form: 'walker william aiken' for any ordering.
    Bridges 'Walker, William Aiken' (catalogs) vs 'William Aiken Walker'
    (auction titles) without needing to guess the inversion correctly."""
    return " ".join(sorted(key.split()))


def add_variant(conn, name: str, artist_id: int, source: str):
    key = artist_key(name)
    # two-word minimum: single-word variants ("Walker") would false-match wildly
    if len(key) < 4 or " " not in key:
        return
    conn.execute("INSERT OR IGNORE INTO name_variants (variant_key, artist_id,"
                 " source) VALUES (?,?,?)", (key, artist_id, source))
    skey = sorted_key(key)
    if skey != key:
        conn.execute("INSERT OR IGNORE INTO name_variants (variant_key,"
                     " artist_id, source) VALUES (?,?,?)",
                     (skey, artist_id, source))


def add_holding(conn, artist_id: int, institution: str, works: int = 1):
    conn.execute(
        "INSERT INTO holdings (artist_id, institution, works) VALUES (?,?,?)"
        " ON CONFLICT(artist_id, institution)"
        " DO UPDATE SET works=works+excluded.works",
        (artist_id, institution, works))


def lookup(conn, name: str) -> dict | None:
    """Resolve a name (any documented variant spelling) to authority info.

    Returns None for unknown names AND for names too short/generic to trust —
    callers must treat None as neutral, never as a negative signal.
    """
    key = artist_key(name)
    if len(key) < 4 or " " not in key:
        return None
    rows = conn.execute(
        "SELECT a.* FROM name_variants v JOIN artists_authority a"
        " ON a.id=v.artist_id WHERE v.variant_key=?", (key,)).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT a.* FROM name_variants v JOIN artists_authority a"
            " ON a.id=v.artist_id WHERE v.variant_key=?",
            (sorted_key(key),)).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        # ambiguous variant (several people share the spelling): prefer the
        # one with museum holdings; if still ambiguous, first with life dates
        def rank(r):
            n = conn.execute("SELECT COUNT(*) c FROM holdings WHERE artist_id=?",
                             (r["id"],)).fetchone()["c"]
            return (-n, 0 if r["birth_year"] else 1)
        rows = sorted(rows, key=rank)
    a = dict(rows[0])
    hold = conn.execute(
        "SELECT institution, works FROM holdings WHERE artist_id=?"
        " ORDER BY works DESC", (a["id"],)).fetchall()
    a["museums"] = [h["institution"] for h in hold]
    a["museum_count"] = sum(1 for h in hold if h["institution"] in MAJOR_MUSEUMS)
    a["works_held"] = sum(h["works"] for h in hold)
    a["ambiguous"] = len(rows) > 1
    return a


def institutional_standing(auth: dict | None) -> str | None:
    """Pure (unit-tested): map authority info to a flag-worthiness tier.

    'strong'  — AAA papers, or work in >=3 major museums
    'listed'  — work in >=1 major museum
    None      — no institutional standing (NEUTRAL: not a negative signal)
    """
    if not auth:
        return None
    if auth.get("aaa_papers") or auth.get("museum_count", 0) >= 3:
        return "strong"
    if auth.get("museum_count", 0) >= 1:
        return "listed"
    return None


def describe(auth: dict) -> str:
    """Human evidence string, e.g. 'in Met, SAAM, papers at AAA (1838-1909)'."""
    bits = []
    shown = [m for m in auth.get("museums", []) if m in MAJOR_MUSEUMS][:4]
    if shown:
        names = {"met": "the Met", "nga": "National Gallery", "saam": "SAAM",
                 "npg": "National Portrait Gallery", "aic": "Art Institute of Chicago",
                 "moma": "MoMA", "whitney": "the Whitney", "cleveland": "Cleveland",
                 "hmsg": "Hirshhorn", "chndm": "Cooper Hewitt",
                 "nmafa": "Nat. Museum of African Art", "acm": "Anacostia"}
        bits.append("in " + ", ".join(names.get(m, m) for m in shown))
    if auth.get("aaa_papers"):
        bits.append("papers at Archives of American Art")
    life = ""
    if auth.get("birth_year"):
        life = f" ({auth['birth_year']}-{auth.get('death_year') or ''})"
    return ("; ".join(bits) + life).strip() or "documented artist"


def status(conn) -> dict:
    out = {}
    for t in ("artists_authority", "name_variants", "holdings"):
        out[t] = conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
    out["sources"] = {r["source"]: r["records"] for r in
                      conn.execute("SELECT * FROM sources_meta")}
    return out
