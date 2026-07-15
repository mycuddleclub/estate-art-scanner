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


_SUBDOMAIN_Q = """query { auctionSearch(input: {status: OPEN},
  pageNumber: 1, pageLength: 100) { pagedResults {
    results { auction { id eventName eventDateBegin eventDateEnd
                        auctioneer { name } } } } } }"""


def harvest_favorites(conn) -> list[dict]:
    """Every favorite house's OPEN auctions, via its HiBid subdomain GraphQL
    (Host-scoped to the house — no 'art' query, no 14-day window: a favorite
    gets flagged whenever it has anything at all)."""
    import requests

    from .exclusives import UA
    found: dict[str, dict] = {}
    rows = conn.execute("SELECT fragment, subdomain FROM favorite_houses"
                        " WHERE subdomain IS NOT NULL").fetchall()
    for r in rows:
        try:
            resp = requests.post(
                f"https://{r['subdomain']}.hibid.com/graphql", timeout=30,
                headers={"User-Agent": UA, "Content-Type": "application/json"},
                json={"query": _SUBDOMAIN_Q})
            resp.raise_for_status()
            results = (resp.json()["data"]["auctionSearch"]["pagedResults"]
                       or {}).get("results", [])
        except Exception as e:
            print(f"favorite '{r['fragment']}' subdomain harvest failed:"
                  f" {str(e)[:80]}")
            continue
        for res in results:
            a = res.get("auction") or {}
            if not a.get("id"):
                continue
            ends = (a.get("eventDateEnd") or "")[:10]
            found[f"https://hibid.com/catalog/{a['id']}/_"] = {
                "platform": "hibid",
                "title": (a.get("eventName") or "").strip()[:120],
                "house": ((a.get("auctioneer") or {}).get("name")
                          or r["fragment"]).strip()[:80],
                "url": f"https://hibid.com/catalog/{a['id']}/_",
                "info": f"ends {ends}" if ends else "",
                "ends": a.get("eventDateEnd"),
            }
    return list(found.values())


def add_favorite(conn, fragment: str, note: str = "",
                 subdomain: str | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO favorite_houses (fragment, note, subdomain,"
        " added_at) VALUES (?,?,?,?)",
        (fragment.strip().lower(), note,
         (subdomain or "").strip().lower() or None, db.now()))
    conn.commit()


def remove_favorite(conn, fragment: str) -> int:
    cur = conn.execute("DELETE FROM favorite_houses WHERE fragment=?",
                       (fragment.strip().lower(),))
    conn.commit()
    return cur.rowcount
