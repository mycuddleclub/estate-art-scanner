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
    # all first-time rows are emailable
    assert all(g["emailable"] and g["new"] for g in got)


def test_send_only_on_news(conn):
    from wallhunter.estate_watch import (find_named_estates,
                                         mark_named_estates_sent)
    auctions = [{"platform": "hibid", "title": "Estate of Jane Q. Sample",
                 "house": "House X", "url": "u9", "ends": "2026-07-25",
                 "info": ""}]
    first = find_named_estates(conn, auctions, meter=None)
    assert first[0]["emailable"] and first[0]["new"]
    mark_named_estates_sent(conn, first)
    # second run, nothing changed -> silent
    second = find_named_estates(conn, auctions, meter=None)
    assert not second[0]["emailable"] and not second[0]["new"]
    # a verdict lands -> emailable again as an update
    conn.execute(
        "INSERT INTO estate_identities (person, house, verdict, confidence,"
        " evidence, notable, checked_at) VALUES"
        " ('Jane Q. Sample', 'House X', 'collector', 'high', 'donor wall', 1, 'x')")
    conn.commit()
    third = find_named_estates(conn, auctions, meter=None)
    assert third[0]["emailable"] and third[0]["verdict"] == "collector"
    mark_named_estates_sent(conn, third)
    # notable entering final 48h -> one closing reminder, then silent
    soon = [{**auctions[0], "ends": "2026-07-19"}]
    fourth = find_named_estates(conn, soon, meter=None)
    assert fourth[0]["emailable"] and fourth[0]["closing_reminder"]
    mark_named_estates_sent(conn, fourth)
    fifth = find_named_estates(conn, soon, meter=None)
    assert not fifth[0]["emailable"]
