"""Tests for named-collection extraction and photo-refresh selection."""


def test_extract_names_positive_patterns():
    from wallhunter.dossier import extract_names
    assert extract_names("The Collection of Dr. Herbert Rosenfeld") == ["Herbert Rosenfeld"]
    assert extract_names("Estate of the late Margaret Chen - Fine Art & More") == \
        ["Margaret Chen"]
    assert extract_names("The Whitfield Collection | Live Auction") == ["Whitfield"]
    assert "Sam Maloof" in extract_names(
        "Amazing Sale!", "From the residence of Sam Maloof, woodworker.")
    assert extract_names("The Collection of Dorothy and Herbert Vogel") == \
        ["Dorothy and Herbert Vogel"]
    assert extract_names("Estate of Mr. & Mrs. James T. Whitcomb") == \
        ["Mrs. James T. Whitcomb"] or \
        extract_names("Estate of Mr. & Mrs. James T. Whitcomb")[0].endswith("Whitcomb")


def test_extract_names_rejects_generic_titles():
    from wallhunter.dossier import extract_names
    assert extract_names("The Fine Art Collection") == []
    assert extract_names("Estate of a Lifetime Collector") == []
    assert extract_names("Mercer Island Legacy Auction - Fine Art") == []
    assert extract_names("Amazing Jewelry Sale 13B") == []
    assert extract_names("The Coin Collection Auction") == []


def test_sales_needing_refresh():
    from wallhunter.auto import sales_needing_refresh
    ours = [{"id": 1, "held_photos": 100}, {"id": 2, "held_photos": 50},
            {"id": 3, "held_photos": 200}]
    details = [
        {"id": 1, "pictureCount": 130},   # grew by 30 -> refresh
        {"id": 2, "pictureCount": 52},    # grew by 2 -> below threshold
        {"id": 3, "pictureCount": 200},   # unchanged
        {"id": 9, "pictureCount": 500},   # not ours
    ]
    assert sales_needing_refresh(ours, details, min_growth=5) == [1]
