"""Tests for the Price Engine: classification, suspect rule, summaries."""


def _pdb(tmp_path):
    from wallhunter import prices
    return prices.connect(tmp_path / "prices.db")


def test_classify_work_class():
    from wallhunter.prices import classify_work_class
    assert classify_work_class("Signed Chagall Lithograph 45/300") == "edition"
    assert classify_work_class("Thomas Kinkade LE Canvas") == "edition"
    assert classify_work_class("Wyland A.P. 12/95") == "edition"
    assert classify_work_class("Oil on canvas, signed lower right") == "unique"
    assert classify_work_class("Watercolor of Maine Harbor") == "unique"
    assert classify_work_class("Framed Wall Decor Lot") == "unknown"
    # 'lithe' must not trip the litho stem, 'Le Pho' not the LE token
    assert classify_work_class("Le Pho Oil On Silk") == "unique"


def test_parse_price():
    from wallhunter.prices import parse_price
    assert parse_price("1,234.56 USD") == 1234.56
    assert parse_price("$500") == 500.0
    assert parse_price(42) == 42.0
    assert parse_price("no price here") is None
    assert parse_price(None) is None
    assert parse_price(0) is None


def test_is_suspect():
    from wallhunter.prices import is_suspect
    # $180 'Rockwell' vs $500k vetted ceiling -> suspect
    assert is_suspect(180.0, "final_bid", 500_000)
    # $6,000 vs $500k -> not suspect (legit sketches/studies exist)
    assert not is_suspect(6_000.0, "sold", 500_000)
    # low price vs modest ceiling -> fine (that IS the regional market)
    assert not is_suspect(180.0, "sold", 4_000)
    # unsold rows are never suspect
    assert not is_suspect(180.0, "unsold", 500_000)
    assert not is_suspect(None, "sold", 500_000)
    assert not is_suspect(180.0, "sold", None)


def test_record_and_summary(tmp_path):
    from wallhunter import prices
    conn = _pdb(tmp_path)
    for i, price in enumerate((40, 55, 60, 45, 50)):
        assert prices.record(conn, artist="Volume Decorator",
                             title=f"Print {i} 12/500", price_usd=price,
                             outcome="sold", platform="hibid",
                             source="t") == "recorded"
    prices.record(conn, artist="Thin Master", title="Oil on canvas",
                  price_usd=45_000, outcome="sold", platform="mutualart",
                  tier="A", source="t")
    conn.commit()
    s = prices.artist_summary(conn, "Volume Decorator")
    assert s["sold_n"] == 5 and s["median_usd"] == 50
    line = prices.summary_line(s)
    assert "5 sales" in line and "median $50" in line
    s2 = prices.artist_summary(conn, "Thin Master")
    assert s2["median_usd"] == 45_000
    assert prices.artist_summary(conn, "Nobody Here") is None


def test_blocked_and_suspect_flow(tmp_path):
    from wallhunter import prices
    conn = _pdb(tmp_path)
    assert prices.record(conn, artist="Any Artist", price_usd=100,
                         blocked_house=True) == "blocked"
    got = prices.record(conn, artist="Norman Rockwell",
                        title="Rockwell oil painting", price_usd=180,
                        outcome="final_bid", house="Sketchy Gallery LLC",
                        platform="hibid", vetted_ceiling=500_000, source="t")
    assert got == "suspect"
    conn.commit()
    # suspect rows never enter the summary
    assert prices.artist_summary(conn, "Norman Rockwell") is None or \
        prices.artist_summary(conn, "Norman Rockwell")["sold_n"] == 0
    # but they surface in the fake-density house report
    rep = prices.house_report(conn, min_suspect=1)
    assert rep and rep[0]["house"] == "Sketchy Gallery LLC"


def test_firewall_high_value_artists(tmp_path):
    from wallhunter import prices
    conn = _pdb(tmp_path)
    # tier-B regional rows for a $250k artist
    prices.record(conn, artist="Big Name", title="oil painting",
                  price_usd=900, outcome="sold", tier="B", source="t")
    prices.record(conn, artist="Big Name", title="oil on canvas",
                  price_usd=120_000, outcome="sold", tier="A", source="t")
    conn.commit()
    s = prices.artist_summary(conn, "Big Name", vetted_ceiling=250_000)
    assert s["firewalled_to_tier_a"] is True
    assert s["sold_n"] == 1 and s["median_usd"] == 120_000  # B row excluded
    # same artist without the vetted ceiling: both rows count
    s2 = prices.artist_summary(conn, "Big Name")
    assert s2["sold_n"] == 2


def test_dedupe(tmp_path):
    from wallhunter import prices
    conn = _pdb(tmp_path)
    a = prices.record(conn, artist="Jane Doe", title="Oil", price_usd=100,
                      key="https://x/lot/1", source="t")
    b = prices.record(conn, artist="Jane Doe", title="Oil", price_usd=100,
                      key="https://x/lot/1", source="t")
    assert a == "recorded" and b == "dup"
