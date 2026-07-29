"""Charity + Artsy evidence clients: neutral-on-absence is the contract."""
import os

from wallhunter import artsy_client, charity_client


def test_charity_known_artist_has_sold_price():
    line = charity_client.evidence_line("Floyd Newsum")
    assert "Charity-benefit history" in line
    assert "$40,000" in line and "$65,000" in line


def test_charity_suffix_and_case_insensitive():
    assert charity_client.evidence_line("floyd NEWSUM, Jr.") == \
        charity_client.evidence_line("Floyd Newsum")


def test_charity_unknown_is_neutral():
    assert charity_client.evidence_line("Zyx Qwertson") == ""
    assert charity_client.evidence_line("") == ""
    assert charity_client.evidence_line(None) == ""


def test_artsy_disabled_env_is_neutral(monkeypatch):
    monkeypatch.setenv("WH_NO_ARTSY", "1")
    assert artsy_client.lookup("Floyd Newsum") == []
    assert artsy_client.evidence_line("Floyd Newsum") == ""


def test_artsy_short_or_single_word_never_queries(monkeypatch):
    monkeypatch.delenv("WH_NO_ARTSY", raising=False)
    assert artsy_client.lookup("Picasso") == []   # single word: no slug guess
    assert artsy_client.lookup("ab") == []
