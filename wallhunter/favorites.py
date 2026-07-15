"""Favorite auction houses (Daniel's greenlist): houses that have earned
trust get automatic visibility — any auction of theirs in the window is
surfaced prominently and deep-scanned first, regardless of the off-radar
diff or art-signal banding. Matching mirrors the blocklist convention:
case-insensitive substring fragments."""

from . import db


def favorite_fragments(conn) -> list[str]:
    return [r["fragment"] for r in
            conn.execute("SELECT fragment FROM favorite_houses")]


def match_favorite(house: str | None, fragments) -> str | None:
    if not house:
        return None
    h = house.lower()
    for frag in fragments:
        if frag in h:
            return frag
    return None


def find_favorite_auctions(conn, auctions: list[dict]) -> list[dict]:
    """Auctions (from the FULL harvest, pre-diff — a favorite counts even if
    the house is also on LA) whose house matches a favorite fragment."""
    frags = favorite_fragments(conn)
    if not frags:
        return []
    out = [a for a in auctions if match_favorite(a.get("house"), frags)]
    out.sort(key=lambda a: a.get("ends") or "9999")
    return out


def harvest_favorites(conn) -> list[dict]:
    """Dedicated per-fragment HiBid search: the main harvest queries 'art',
    which a favorite's auction title may not contain (e.g. 'Caplans Online
    Auction 4/22'). One cheap GraphQL query per favorite guarantees their
    auctions are always seen."""
    from .exclusives import harvest_hibid
    frags = favorite_fragments(conn)
    found: dict[str, dict] = {}
    for frag in frags:
        try:
            for a in harvest_hibid(query=frag):
                if match_favorite(a.get("house"), [frag]):
                    found[a["url"]] = a
        except Exception as e:
            print(f"favorite harvest '{frag}' failed: {str(e)[:80]}")
    return list(found.values())


def add_favorite(conn, fragment: str, note: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO favorite_houses (fragment, note, added_at)"
        " VALUES (?,?,?)", (fragment.strip().lower(), note, db.now()))
    conn.commit()


def remove_favorite(conn, fragment: str) -> int:
    cur = conn.execute("DELETE FROM favorite_houses WHERE fragment=?",
                       (fragment.strip().lower(),))
    conn.commit()
    return cur.rowcount
