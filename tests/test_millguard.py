"""Tests for the fake-mill auto-blocker."""


def _lots(*titles):
    return [{"title": t} for t in titles]


def test_mill_masters_counts_original_claims_only():
    from wallhunter.deep import mill_masters
    names, claims = mill_masters(_lots(
        "Original Oil Signed Monet",
        "Van Gogh Sunflowers Oil On Canvas",
        "Picasso Abstract Signed Painting",
        "Picasso Lithograph 45/200",          # honest label: exempt
        "Print After Renoir",                  # honest label: exempt
        "Vintage Table Lamp"))
    assert names == {"monet", "van gogh", "picasso"}
    assert claims == 3


def test_is_mill_thresholds():
    from wallhunter.deep import is_mill
    assert is_mill({"monet", "dali", "picasso"}, 3)         # 3 distinct
    assert not is_mill({"picasso"}, 2)                      # a real estate
    assert not is_mill({"dali", "picasso"}, 4)              # 2 masters ok
    assert is_mill({"dali"}, 8)                             # claim flood


def test_auto_block_roundtrip(conn):
    from wallhunter.deep import auto_block_house, is_auto_blocked
    auto_block_house(conn, "Sketchy Masterworks LLC", "u1",
                     {"monet", "dali", "goya"}, 12)
    assert is_auto_blocked(conn, "Sketchy Masterworks LLC")
    assert is_auto_blocked(conn, "SKETCHY MASTERWORKS LLC")  # case-insensitive
    assert not is_auto_blocked(conn, "Honest House")
    assert not is_auto_blocked(conn, "")
