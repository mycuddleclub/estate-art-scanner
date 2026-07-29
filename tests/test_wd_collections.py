"""Wikidata collection claims are evidence-only: shown, never standing."""
from wallhunter import authority


def _mkconn(tmp_path):
    return authority.connect(tmp_path / "auth_test.db")


def test_wd_collections_never_grant_standing(tmp_path):
    conn = _mkconn(tmp_path)
    aid = authority.upsert_artist(conn, "Testy Contemporary", "test")
    for i in range(5):  # five museums per Wikidata — still no standing
        conn.execute("INSERT INTO wd_collections VALUES (?,?,?)",
                     (aid, f"Museum {i}", f"Q{i}"))
    conn.commit()
    auth = authority.lookup(conn, "Testy Contemporary")
    assert auth["wd_collections"] and len(auth["wd_collections"]) == 5
    assert authority.institutional_standing(auth) is None


def test_wd_collections_shown_in_describe(tmp_path):
    conn = _mkconn(tmp_path)
    aid = authority.upsert_artist(conn, "Testy Contemporary", "test")
    conn.execute("INSERT INTO wd_collections VALUES (?,?,?)",
                 (aid, "Philadelphia Museum of Art", "Q510324"))
    conn.commit()
    auth = authority.lookup(conn, "Testy Contemporary")
    d = authority.describe(auth)
    assert "collections per Wikidata: Philadelphia Museum of Art" in d


def test_real_holdings_still_outrank(tmp_path):
    conn = _mkconn(tmp_path)
    aid = authority.upsert_artist(conn, "Testy Documented", "test")
    for m in ("moma", "whitney", "met"):
        authority.add_holding(conn, aid, m)
    conn.commit()
    auth = authority.lookup(conn, "Testy Documented")
    assert authority.institutional_standing(auth) == "strong"
