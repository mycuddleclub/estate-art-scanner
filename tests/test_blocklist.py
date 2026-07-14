"""Blocked-house list: live parse of art-scout config + matching semantics."""

import pytest

from wallhunter.blocklist import ARTSCOUT_CONFIG, blocked_match, load_blocked_houses


@pytest.mark.skipif(not ARTSCOUT_CONFIG.exists(), reason="art-scout not installed")
def test_loads_real_artscout_blacklist():
    houses = load_blocked_houses()
    # memory says ~86 entries verified 2026-07-01; guard against silent
    # parse breakage without pinning the exact count
    assert len(houses) > 50
    assert all(h == h.lower() for h in houses)


def test_blocked_match_is_case_insensitive_substring():
    blocked = ("broward auction gallery", "robinhood")
    assert blocked_match("BROWARD Auction Gallery LLC", blocked) == "broward auction gallery"
    assert blocked_match("Robinhood Auctions", blocked) == "robinhood"
    assert blocked_match("Fine Estate Sales of Ohio", blocked) is None
    assert blocked_match(None, blocked) is None
    assert blocked_match("", blocked) is None


def test_drop_excluded_auctions_also_drops_blocked_orgs():
    from wallhunter.auto import drop_excluded_auctions
    details = [
        {"id": 1, "orgName": "Robinhood Auctions", "auctionUrl": None},
        {"id": 2, "orgName": "Nice Estate Co", "auctionUrl": None},
        {"id": 3, "orgName": "Nice Estate Co",
         "auctionUrl": "https://www.liveauctioneers.com/catalog/1_x"},
    ]
    got = drop_excluded_auctions(details, hosts=("liveauctioneers.com",),
                                 blocked=("robinhood",))
    assert got == [2]
