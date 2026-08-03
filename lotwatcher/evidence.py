"""Stage 2: free local-database evidence (authority, prices, charity, artsy).
Same canonical-client pattern as every other consumer; silent-neutral."""
import importlib.util as _il
import os


def _load(name):
    try:
        spec = _il.spec_from_file_location(
            name, os.path.expanduser(f"~/estate-art-scanner/wallhunter/{name}.py"))
        mod = _il.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


authority = _load("authority_client")
prices = _load("prices_client")
charity = _load("charity_client")
artsy = _load("artsy_client")


def gather(artist: str, deep: bool = False) -> str:
    """Evidence lines for an artist. `deep` adds the network-touching artsy
    client (only for already-promising lots, mirroring deep.py's flag path)."""
    if not artist or len(artist.split()) < 2:
        return ""
    lines = []
    for client in (authority, prices, charity):
        if client is None:
            continue
        try:
            ln = client.evidence_line(artist)
            if ln:
                lines.append(ln)
        except Exception:
            pass
    if deep and artsy is not None:
        try:
            ln = artsy.evidence_line(artist)
            if ln:
                lines.append(ln)
        except Exception:
            pass
    return "\n".join(lines)


def standing(artist: str) -> str:
    """'strong' | 'listed' | '' — used to force stage-3 review regardless of
    stage-1 promise (institutional names never get dropped by the small model)."""
    if authority is None or not artist or len(artist.split()) < 2:
        return ""
    try:
        info = authority.lookup(artist) if hasattr(authority, "lookup") else None
        if info and isinstance(info, dict) and info.get("standing"):
            return info["standing"]
    except Exception:
        pass
    # fall back to parsing the evidence line
    try:
        ln = authority.evidence_line(artist) or ""
        if "[strong]" in ln:
            return "strong"
        if "[listed]" in ln:
            return "listed"
    except Exception:
        pass
    return ""

comp_engine = _load("comp_engine")


def comp_line(lot: dict, artist: str) -> str:
    """Weighted comp valuation when the artist has banked tier-A comps.
    Pure math over prices.db — no model call, no network."""
    if comp_engine is None or not artist:
        return ""
    try:
        est = comp_engine.value_lot(
            {"title": lot.get("title", ""), "text": lot.get("detail", "") or ""},
            artist)
        if not est or not est.get("mid"):
            return ""
        line = (f"Comp engine ({est.get('n', '?')} tier-A comps): "
                f"mid ${est['mid']:,.0f} (range ${est.get('low', 0):,.0f}-"
                f"${est.get('high', 0):,.0f}), confidence {est.get('confidence', '?')}")
        vs = est.get("venue_split")
        if vs and vs["mid_tier"]["mid"] and vs["big3"]["mid"]:
            line += (f" | mid-tier house est ${vs['mid_tier']['mid']:,.0f}"
                     f" (n={vs['mid_tier']['n']}) vs big-three est"
                     f" ${vs['big3']['mid']:,.0f} (n={vs['big3']['n']})")
        elif est.get("big3_seen"):
            line += (f" | ARTIST HAS BIG-THREE RESULTS"
                     f" ({est.get('big3_count', 0)} Christie's/Sotheby's/Phillips comps)")
        return line
    except Exception:
        return ""

def authority_info(artist: str) -> dict:
    """{standing, museums, museum_count} from the Reference Library, or {}."""
    if authority is None or not artist or len(artist.split()) < 2:
        return {}
    try:
        a = authority.lookup(artist)
        if not a:
            return {}
        return {"standing": authority.standing(a) or "",
                "museums": authority.describe(a) or "",
                "museum_count": a.get("museum_count", 0)}
    except Exception:
        return {}


def market_detail(artist: str) -> dict:
    """Documented realized prices for the artist from prices.db.

    Reads BOTH tiers: tier A = MutualArt (authoritative, 83 artists) and
    tier B = HiBid realized regional results (1,086 artists) which the gate
    was previously ignoring entirely. Returns {high, n, source}.
    """
    out = {"high": 0.0, "n": 0, "source": ""}
    if not artist or len(artist.split()) < 2:
        return out
    try:
        import os as _o
        import sqlite3 as _sq
        conn = _sq.connect(
            f"file:{_o.path.expanduser('~/estate-art-scanner/wh_data/prices.db')}?mode=ro",
            uri=True, timeout=20)
        key = None
        if prices is not None and hasattr(prices, "_key"):
            key = prices._key(artist)
        if not key:
            import re as _re
            key = _re.sub(r"\s+", " ",
                          _re.sub(r"[^a-z ]+", " ", artist.lower())).strip()
        # MutualArt artist-page summary (fast pass) is the BEST market number —
        # real market averages, unlike HiBid's regional realized prices.
        try:
            am = conn.execute(
                "SELECT avg_realized, max_est, year FROM artist_market"
                " WHERE artist_key=?", (key,)).fetchone()
            if am and (am[0] or am[1]):
                out.update(high=float(am[0] or am[1] or 0), n=0,
                           source=f"MutualArt avg{' ' + am[2] if am[2] else ''}",
                           avg=float(am[0] or 0), year=am[2] or "")
        except Exception:
            pass
        rows = conn.execute(
            "SELECT price_usd, tier FROM prices WHERE artist_key=? AND suspect=0"
            " AND price_usd IS NOT NULL AND outcome IN ('sold','final_bid')",
            (key,)).fetchall()
        conn.close()
        if not rows:
            return out
        a = [float(r[0]) for r in rows if r[1] == "A"]
        b = [float(r[0]) for r in rows if r[1] != "A"]
        if a and max(a) > out["high"]:
            out.update(high=max(a), n=len(a), source="MutualArt")
        if b and (not a or max(b) > out["high"]):
            out.update(high=max(max(b), out["high"]), n=out["n"] + len(b),
                       source=("MutualArt+HiBid" if a else "HiBid realized"))
        return out
    except Exception:
        return out


def market_ceiling(artist: str) -> float:
    """Highest documented realized price (either tier) — the $2,000 gate arm."""
    return market_detail(artist).get("high", 0.0)
