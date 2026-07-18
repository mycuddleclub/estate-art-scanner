"""Tests for the reference library: name cleaning, variant lookup, tiers."""


def _adb(tmp_path):
    from wallhunter import authority
    return authority.connect(tmp_path / "authority.db")


def test_clean_person_name():
    from wallhunter.authority_import import clean_person_name
    assert clean_person_name("Walker, William Aiken, 1838-1921") == \
        "William Aiken Walker"
    assert clean_person_name("Sargent, John Singer") == "John Singer Sargent"
    assert clean_person_name("Mary Cassatt (American, 1844-1926)") == \
        "Mary Cassatt"
    assert clean_person_name("Crabeth, Wouter Pietersz., II") == \
        "Wouter Pietersz. Crabeth II"
    # junk and non-persons come back None
    assert clean_person_name("Unidentified artist") is None
    assert clean_person_name("Steuben Glass Company") is None
    assert clean_person_name("Rembrandt") is None  # single word: too risky
    assert clean_person_name("") is None


def test_variant_lookup_any_word_order(tmp_path):
    from wallhunter import authority
    conn = _adb(tmp_path)
    aid = authority.upsert_artist(conn, "William Aiken Walker", "met",
                                  ulan_id="500019096", birth_year=1838,
                                  death_year=1921)
    authority.add_variant(conn, "Walker, William Aiken", aid, "ulan")
    authority.add_variant(conn, "Wm. Aiken Walker", aid, "ulan")
    conn.commit()
    for q in ("William Aiken Walker", "Walker William Aiken",
              "walker, william aiken", "Wm Aiken Walker",
              "Aiken Walker William"):
        got = authority.lookup(conn, q)
        assert got and got["canonical"] == "William Aiken Walker", q
    assert authority.lookup(conn, "Walker") is None          # single word
    assert authority.lookup(conn, "Someone Else") is None    # unknown


def test_merge_across_sources(tmp_path):
    from wallhunter import authority
    conn = _adb(tmp_path)
    a1 = authority.upsert_artist(conn, "Edward Hopper", "moma", birth_year=1882)
    a2 = authority.upsert_artist(conn, "Edward Hopper", "whitney")
    assert a1 == a2
    authority.add_holding(conn, a1, "moma")
    authority.add_holding(conn, a1, "whitney")
    authority.add_holding(conn, a1, "nga")
    conn.commit()
    got = authority.lookup(conn, "Edward Hopper")
    assert got["museum_count"] == 3
    assert set(got["museums"]) == {"moma", "whitney", "nga"}
    assert "moma" in got["sources"] and "whitney" in got["sources"]


def test_institutional_standing():
    from wallhunter.authority import institutional_standing
    assert institutional_standing(None) is None
    assert institutional_standing({"museum_count": 0, "aaa_papers": 0}) is None
    assert institutional_standing({"museum_count": 1, "aaa_papers": 0}) == "listed"
    assert institutional_standing({"museum_count": 3, "aaa_papers": 0}) == "strong"
    assert institutional_standing({"museum_count": 0, "aaa_papers": 1}) == "strong"


def test_institutional_flag_reason():
    from wallhunter.deep import institutional_flag_reason
    strong = {"museum_count": 3, "aaa_papers": 0, "museums": ["met", "nga", "moma"],
              "birth_year": 1900, "death_year": 1980}
    listed = {"museum_count": 1, "aaa_papers": 0, "museums": ["moma"]}
    assert "no bids" in institutional_flag_reason(strong, {"high_bid_usd": None})
    assert "$45" in institutional_flag_reason(strong, {"high_bid_usd": 45.0})
    # real bidding on it already -> not an overlooked lot
    assert institutional_flag_reason(strong, {"high_bid_usd": 500.0}) is None
    # 'listed' standing alone never flags (common-name noise control)
    assert institutional_flag_reason(listed, {"high_bid_usd": None}) is None
    assert institutional_flag_reason(None, {"high_bid_usd": None}) is None


def test_awards_grant_strong_standing():
    from wallhunter.authority import institutional_standing
    assert institutional_standing({"museum_count": 0, "aaa_papers": 0,
                                   "awards": ["Guggenheim Fellow"]}) == "strong"
    # historic sales are evidence only — never standing on their own
    assert institutional_standing({"museum_count": 0, "aaa_papers": 0,
                                   "awards": [], "historic_sales": 40}) is None


def test_describe_includes_awards_and_history():
    from wallhunter.authority import describe
    s = describe({"museums": ["moma"], "aaa_papers": 0,
                  "awards": ["Guggenheim Fellow"], "historic_sales": 12,
                  "birth_year": 1940, "death_year": None})
    assert "MoMA" in s and "Guggenheim Fellow" in s
    assert "12 historic auction records" in s and "(1940-)" in s


def test_lookup_neutral_on_missing_db(tmp_path):
    """Empty library must be neutral: lookups return None, no exceptions."""
    from wallhunter import authority
    conn = _adb(tmp_path)
    assert authority.lookup(conn, "Anyone At All") is None
