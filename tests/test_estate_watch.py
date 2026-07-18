"""Tests for named-estate detection and prioritization."""


def test_named_estate_person():
    from wallhunter.estate_watch import named_estate_person
    assert named_estate_person("Estate of John R. Smith — Fine Art") == \
        "John R. Smith"
    assert named_estate_person("Pfendler Estate Auction") == "Pfendler"
    assert named_estate_person("The Vogel Collection") == "Vogel"
    assert named_estate_person("Estate of Dr. Mary Alice Woodson") == \
        "Mary Alice Woodson"
    # generic titles must not produce a person
    assert named_estate_person("July Estates & Collectibles Auction") is None
    assert named_estate_person("Online Estate Auction — Tools & More") is None
    assert named_estate_person("Amazing Estate Sale Finds") is None
    assert named_estate_person("Lifetime Collection of Coins") is None
    assert named_estate_person("") is None


def test_find_named_estates_sorting(conn):
    from wallhunter.estate_watch import find_named_estates
    conn.execute(
        "INSERT INTO estate_identities (person, house, verdict, confidence,"
        " evidence, notable, checked_at) VALUES"
        " ('Vogel', 'House A', 'collector', 'high', 'famous collectors', 1, 'x'),"
        " ('Pfendler', 'House B', 'not_notable', 'low', 'nothing found', 0, 'x')")
    conn.commit()
    auctions = [
        {"platform": "hibid", "title": "Pfendler Estate Auction",
         "house": "House B", "url": "u1", "ends": "2026-07-20", "info": ""},
        {"platform": "hibid", "title": "Weekly Consignment",
         "house": "House C", "url": "u2", "ends": "2026-07-19", "info": ""},
        {"platform": "hibid", "title": "The Vogel Collection",
         "house": "House A", "url": "u3", "ends": "2026-07-25", "info": ""},
        {"platform": "hibid", "title": "Estate of Jane Q. Unknown",
         "house": "House D", "url": "u4", "ends": "2026-07-21", "info": ""},
    ]
    got = find_named_estates(conn, auctions, meter=None)  # no research
    # notable first, then unresearched, then not_notable; generic title absent
    assert [g["url"] for g in got] == ["u3", "u4", "u1"]
    assert got[0]["notable"] and got[0]["verdict"] == "collector"
    assert got[1]["verdict"] is None
