"""Standalone read-only client for the Reference Library (authority.db).

CANONICAL SHARED FILE — Art Scout, the Super Smart Checker, and Art
Appraiser load this by path so there is exactly one implementation:

    import importlib.util as _il
    _spec = _il.spec_from_file_location(
        "authority_client",
        os.path.expanduser("~/estate-art-scanner/wallhunter/authority_client.py"))
    authority_client = _il.module_from_spec(_spec)
    _spec.loader.exec_module(authority_client)

Deliberately dependency-free (no wallhunter imports) so any interpreter on
this machine can use it. The library only ever UPGRADES an artist: lookup
returning None is neutral and must never cause a skip.
"""

import os
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(os.path.expanduser("~/estate-art-scanner/wh_data/authority.db"))

_MAJOR = {"met", "nga", "saam", "npg", "aic", "moma", "whitney",
          "cleveland", "hmsg", "chndm", "nmafa", "acm", "nmaahc"}
_NAMES = {"met": "the Met", "nga": "National Gallery", "saam": "SAAM",
          "npg": "National Portrait Gallery",
          "aic": "Art Institute of Chicago", "moma": "MoMA",
          "whitney": "the Whitney", "cleveland": "Cleveland",
          "hmsg": "Hirshhorn", "chndm": "Cooper Hewitt",
          "nmafa": "Nat. Museum of African Art", "acm": "Anacostia",
          "nmaahc": "NMAAHC"}
_ABBREV = {"wm": "william", "chas": "charles", "jas": "james",
           "thos": "thomas", "geo": "george", "jno": "john",
           "benj": "benjamin", "saml": "samuel", "robt": "robert",
           "richd": "richard", "edwd": "edward", "jos": "joseph",
           "fredk": "frederick", "alex": "alexander", "hy": "henry",
           "eliz": "elizabeth"}

_conn = None


def _key(name):
    return re.sub(r"\s+", " ",
                  re.sub(r"[^a-z ]+", " ", (name or "").lower())).strip()


def _connect():
    global _conn
    if _conn is None:
        if not DB_PATH.exists():
            return None
        _conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True,
                                check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def lookup(name):
    """Authority info dict for a name (any variant spelling/order), or None.
    None is NEUTRAL. Never raises."""
    try:
        conn = _connect()
        key = _key(name)
        if conn is None or len(key) < 4 or " " not in key:
            return None
        expanded = " ".join(_ABBREV.get(w, w) for w in key.split())
        candidates = dict.fromkeys(
            (key, expanded, " ".join(sorted(key.split())),
             " ".join(sorted(expanded.split()))))
        rows = []
        for k in candidates:
            rows = conn.execute(
                "SELECT a.* FROM name_variants v JOIN artists_authority a"
                " ON a.id=v.artist_id WHERE v.variant_key=?", (k,)).fetchall()
            if rows:
                break
        if not rows:
            return None
        a = dict(rows[0])
        hold = conn.execute(
            "SELECT institution, works FROM holdings WHERE artist_id=?"
            " ORDER BY works DESC", (a["id"],)).fetchall()
        a["museums"] = [h["institution"] for h in hold]
        a["museum_count"] = sum(1 for h in hold if h["institution"] in _MAJOR)
        a["awards"] = [r["distinction"] for r in conn.execute(
            "SELECT distinction FROM distinctions WHERE artist_id=?",
            (a["id"],))]
        a["historic_sales"] = (conn.execute(
            "SELECT SUM(records) s FROM market_history WHERE artist_id=?",
            (a["id"],)).fetchone()["s"]) or 0
        try:
            a["wd_collections"] = [r["museum"] for r in conn.execute(
                "SELECT museum FROM wd_collections WHERE artist_id=?",
                (a["id"],))]
        except Exception:
            a["wd_collections"] = []
        return a
    except Exception:
        return None


def standing(auth):
    """'strong' | 'listed' | None (None = neutral, never negative)."""
    if not auth:
        return None
    if auth.get("aaa_papers") or auth.get("museum_count", 0) >= 3 \
            or auth.get("awards"):
        return "strong"
    if auth.get("museum_count", 0) >= 1:
        return "listed"
    return None


def describe(auth):
    """'in the Met, SAAM; papers at Archives of American Art (1838-1921)'"""
    if not auth:
        return ""
    bits = []
    shown = [m for m in auth.get("museums", []) if m in _MAJOR][:4]
    if shown:
        bits.append("in " + ", ".join(_NAMES.get(m, m) for m in shown))
    if auth.get("aaa_papers"):
        bits.append("papers at Archives of American Art")
    bits.extend((auth.get("awards") or [])[:3])
    wd = (auth.get("wd_collections") or [])[:4]
    if wd:
        bits.append("collections per Wikidata: " + ", ".join(wd))
    if auth.get("historic_sales"):
        bits.append(f"{auth['historic_sales']} historic auction records"
                    " (Getty PI)")
    life = ""
    if auth.get("birth_year"):
        life = f" ({auth['birth_year']}-{auth.get('death_year') or ''})"
    return ("; ".join(bits) + life).strip()


def evidence_line(name):
    """One-line evidence string for prompts/emails, or '' if unknown."""
    a = lookup(name)
    if not a:
        return ""
    st = standing(a)
    if st:
        return f"Reference library [{st}]: {a['canonical']} — {describe(a)}"
    # No standing, but real evidence (Wikidata collections, Getty PI sales)
    # still informs — labeled so the trust level is unmistakable.
    if a.get("wd_collections") or a.get("historic_sales"):
        return (f"Reference library [evidence only]: {a['canonical']}"
                f" — {describe(a)}")
    return ""
