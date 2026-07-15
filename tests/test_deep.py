"""Tests for the deep-scan tile parsing, flag logic, and cache imports."""

import json


def test_parse_tile():
    from wallhunter.deep import parse_tile
    t = ('Lot 3 | Pino (1939-2010)- Hand Embellished Giclee '
         '2,906.25 - 3,875.00 USD 0 Bids Bidding Closed')
    got = parse_tile(t)
    assert got["bid_count"] == 0
    assert got["estimate"] == "2,906.25 - 3,875.00 USD"
    t2 = "Lot 1 | 1996 ERTL High Bid: 6.00 USD 3 Bids 6m 29s"
    got2 = parse_tile(t2)
    assert got2["high_bid_usd"] == 6.0
    assert got2["bid_count"] == 3
    assert parse_tile("")["high_bid_usd"] is None


def _row(tier, high):
    return {"tier": tier, "market_high_usd": high, "artist": "X",
            "artist_key": "x", "market_note": "", "evidence": ""}


def test_flag_reason():
    from wallhunter.deep import flag_reason
    strong = _row("strong", 5000)
    assert "no bids" in flag_reason(strong, {"high_bid_usd": None})
    assert "417x" in flag_reason(strong, {"high_bid_usd": 12.0})
    # ratio below threshold -> no flag
    assert flag_reason(strong, {"high_bid_usd": 4000.0}) is None
    # weak market ceiling -> no flag even with no bids
    assert flag_reason(_row("listed", 150), {"high_bid_usd": None}) is None
    # non-flag tiers
    assert flag_reason(_row("minor", 9999), {"high_bid_usd": None}) is None
    assert flag_reason(None, {"high_bid_usd": None}) is None


def test_unscanned_candidates_watermark(conn):
    from wallhunter import db as wdb
    from wallhunter.deep import unscanned_candidates
    exclusives = [
        {"platform": "hibid", "url": "u1", "title": "Fine Art Sale",
         "house": "Gallery A", "ends": "2026-07-20"},
        {"platform": "hibid", "url": "u2", "title": "Grand Mix",
         "house": "Liquidators", "ends": "2026-07-16"},
        {"platform": "hibid", "url": "u3", "title": "Estate Auction",
         "house": "B", "ends": "2026-07-18"},
        {"platform": "bidsquare", "url": "u4", "title": "x", "house": "C"},
    ]
    conn.execute("INSERT INTO deep_auctions (sale_url, scanned_at)"
                 " VALUES ('u3', ?)", (wdb.now(),))
    conn.commit()
    got = unscanned_candidates(conn, exclusives)
    # u3 already scanned; u4 not hibid; art-signal (u1) before liquidator (u2)
    assert [a["url"] for a in got] == ["u1", "u2"]


def test_is_art_signal():
    from wallhunter.deep import is_art_signal
    assert is_art_signal({"title": "July Fine Art Antiques Auction",
                          "house": "Prime Auction Gallery"})
    assert is_art_signal({"title": "Weekly Consignment", "house": "Kosi Galleries"})
    assert is_art_signal({"title": "Krupicka Estate Collection", "house": "Zalesky"})
    assert not is_art_signal({"title": "Grand Mix Auction July14",
                              "house": "Empire Furniture LLC"})
    assert not is_art_signal({"title": "Pallet Returns Blowout", "house": "Bidable"})


def test_research_artist_propagates_cost_cap(conn, monkeypatch):
    """Regression: CostCapExceeded was swallowed by the broad except and
    recorded as 'research failed' — live incident: $8.96 spend vs $1.50 cap."""
    import pytest
    from wallhunter import artists
    from wallhunter.config import CostCapExceeded, CostMeter

    class FakeUsage:
        input_tokens = 1_000_000
        output_tokens = 1_000_000

    class FakeResp:
        usage = FakeUsage()
        content = []

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                return FakeResp()

    monkeypatch.setattr(artists.anthropic, "Anthropic", lambda: FakeClient)
    meter = CostMeter(0.01)  # first add() blows the cap
    with pytest.raises(CostCapExceeded):
        artists.research_artist(conn, "Someone New", meter)
    # and nothing bogus was cached for the name
    assert artists.lookup(conn, "Someone New") is None


def test_import_checker_cache_shape(conn, tmp_path, monkeypatch):
    from wallhunter import artists
    fake = {"entries": {
        "jane doe": {"artist": "Jane Doe", "artist_key": "jane doe",
                     "max_amount_seen": 8500, "result_count": 4,
                     "market_term_hits": ["auction result"],
                     "source_domains": [["invaluable.com", 3]],
                     "representative_results": [], "checked_at": "2026-06-01"},
        "nobody man": {"artist": "Nobody Man", "artist_key": "nobody man",
                       "max_amount_seen": None, "result_count": 0,
                       "market_term_hits": [], "historical_term_hits": [],
                       "source_domains": [], "representative_results": []},
    }}
    p = tmp_path / "cache.json"
    p.write_text(json.dumps(fake))
    monkeypatch.setattr(artists, "CHECKER_CACHE", p)
    n = artists.import_checker_cache(conn)
    assert n == 2
    jane = artists.lookup(conn, "Jane Doe")
    assert jane["tier"] == "strong" and jane["market_high_usd"] == 8500
    assert artists.lookup(conn, "Nobody Man")["tier"] == "none"


def test_import_artscout_skips_junk_keys(conn, tmp_path, monkeypatch):
    from wallhunter import artists
    fake = {
        "carl gaertner": {"results": "sold for $4,200 at Rachel Davis"},
        "the water": {"results": "irrelevant"},          # junk single concept
        "x": {"results": "too short"},
        "err guy name": {"results": "Search error: Client error '400'"},
    }
    p = tmp_path / "as.json"
    p.write_text(json.dumps(fake))
    monkeypatch.setattr(artists, "ARTSCOUT_CACHE", p)
    n = artists.import_artscout_cache(conn)
    assert n == 1
    carl = artists.lookup(conn, "Carl Gaertner")
    assert carl["tier"] == "strong" and carl["market_high_usd"] == 4200
