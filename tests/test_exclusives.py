"""Pure-logic tests for the platform-exclusives finder."""

from wallhunter.exclusives import compute_exclusives, houses_match, normalize_house


def test_normalize_house():
    assert normalize_house("Abell Auction Co.") == "abell auction"
    assert normalize_house("DuMouchelles, LLC") == "dumouchelles"
    assert normalize_house("500 Gallery Inc") == "500 gallery"
    assert normalize_house("  Freeman's  ") == "freeman s"
    assert normalize_house("") == ""


def test_houses_match():
    assert houses_match("abell auction", "abell auction")
    assert houses_match(normalize_house("Abell"), normalize_house("Abell Auction Co"))
    assert houses_match("dumouchelles", "dumouchelles art gallery")
    # short fragments must not fuzzy-match (avoid 'gold' ~ 'goldberg')
    assert not houses_match("gold", "goldberg auctions")
    assert not houses_match("smith auctions", "jones auctions")


def test_compute_exclusives():
    auctions = [
        {"platform": "hibid", "house": "Tiny Town Auction LLC", "title": "a"},
        {"platform": "hibid", "house": "DuMouchelles", "title": "b"},
        {"platform": "bidsquare", "house": "Abell", "title": "c"},
        {"platform": "hibid", "house": "", "title": "no house -> dropped"},
    ]
    big = {normalize_house("DuMouchelles Art Gallery"),
           normalize_house("Abell Auction Co")}
    got = compute_exclusives(auctions, big)
    assert [a["house"] for a in got] == ["Tiny Town Auction LLC"]
