"""Shared artist-knowledge store.

One SQLite table seeded from the Smart Checker's structured market cache
(1,500+ artists) and Art Scout's raw search cache (11k+ entries), extended by
Wall Hunter's own research. Keys follow the Checker's convention (lowercased
name). Living in wh_data keeps it readable by launchd (the source caches on
the Desktop are TCC-blocked for background agents, so imports run when a
Terminal session can read them and persist here).
"""

import json
import re
from pathlib import Path

import anthropic

from . import db
from .config import CostCapExceeded, CostMeter

CHECKER_CACHE = (Path.home() / "Desktop/williams-art-engine/arbitrage_smart"
                 / "data/artist_market_cache.json")
ARTSCOUT_CACHE = Path.home() / "art-scout/data/artist_cache.json"

RESEARCH_MODEL = "claude-sonnet-5"
WEB_SEARCH_COST_USD = 0.01


def artist_key(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]+", " ", (name or "").lower())).strip()


def _tier_from_checker(e: dict) -> str:
    if e.get("max_amount_seen") and e["max_amount_seen"] >= 2000:
        return "strong"
    if e.get("max_amount_seen") or e.get("market_term_hits"):
        return "listed"
    if e.get("historical_term_hits"):
        return "listed"
    if e.get("result_count"):
        return "minor"
    return "none"


def import_checker_cache(conn) -> int:
    """Structured market evidence from the Smart Checker (Desktop; may be
    unreadable under launchd — call from Terminal sessions)."""
    try:
        entries = json.loads(CHECKER_CACHE.read_text()).get("entries", {})
    except OSError as e:
        print(f"artists: checker cache unreadable ({e}) — skipping import")
        return 0
    n = 0
    for key, e in entries.items():
        if not isinstance(e, dict) or not e.get("artist"):
            continue
        evidence = json.dumps({
            "domains": e.get("source_domains", [])[:6],
            "market_terms": e.get("market_term_hits", []),
            "historical_terms": e.get("historical_term_hits", []),
            "results": e.get("representative_results", [])[:3],
        })[:2000]
        cur = conn.execute(
            "INSERT INTO artists (artist_key, artist, source, tier,"
            " market_high_usd, evidence, researched_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(artist_key) DO UPDATE SET"
            "  tier=excluded.tier, market_high_usd=excluded.market_high_usd,"
            "  evidence=excluded.evidence, researched_at=excluded.researched_at"
            " WHERE artists.source != 'wallhunter'",  # own research wins
            (key, e["artist"], "checker_cache", _tier_from_checker(e),
             e.get("max_amount_seen"), evidence, e.get("checked_at")))
        n += cur.rowcount
    conn.commit()
    return n


_AMOUNT = re.compile(r"\$\s?([\d,]{3,})")


def import_artscout_cache(conn) -> int:
    """Raw market-search text from Art Scout; fills gaps only (never
    overwrites structured rows). Junk keys (search-extraction noise) are
    skipped by requiring 2-4 words and a letter-only-ish shape."""
    try:
        entries = json.loads(ARTSCOUT_CACHE.read_text())
    except OSError as e:
        print(f"artists: art-scout cache unreadable ({e}) — skipping import")
        return 0
    _JUNK_LEAD = {"the", "a", "an", "of", "in", "on", "and", "or", "by",
                  "for", "with", "this", "that"}
    n = 0
    for key, v in entries.items():
        k = artist_key(key)
        words = k.split()
        if not (2 <= len(words) <= 4) or any(len(w) < 2 for w in words):
            continue
        if words[0] in _JUNK_LEAD:
            continue  # "the water", "a bland guy" — extraction noise, not names
        text = (v or {}).get("results", "") if isinstance(v, dict) else str(v)
        if not text or "Search error" in text[:120]:
            continue
        amounts = [int(a.replace(",", "")) for a in _AMOUNT.findall(text)[:20]]
        high = max(amounts) if amounts else None
        tier = "strong" if (high or 0) >= 2000 else ("listed" if high else "minor")
        cur = conn.execute(
            "INSERT OR IGNORE INTO artists (artist_key, artist, source, tier,"
            " market_high_usd, evidence) VALUES (?,?,?,?,?,?)",
            (k, key.title(), "artscout_cache", tier, high, text[:1500]))
        n += cur.rowcount
    conn.commit()
    return n


def lookup(conn, name: str):
    return conn.execute("SELECT * FROM artists WHERE artist_key=?",
                        (artist_key(name),)).fetchone()


CLASSIFY_MODEL = "claude-haiku-4-5-20251001"

CLASSIFY_PROMPT = """These strings were extracted from auction lot titles as possible
artist names. Many are actually product/object descriptions in Title Case.
For each, answer P if it is plausibly a PERSON'S NAME (artist attribution)
or X if it is a product/object/place/brand description.
Strings:
{numbered}
Reply with ONLY lines like "1:P" or "2:X", one per string."""


def classify_person_names(names: list[str], meter: CostMeter) -> dict[str, bool]:
    """Batch Haiku gate before spending web-research money. Fail-open (treat
    as person) so a classifier outage can't silently drop real artists."""
    if not names:
        return {}
    client = anthropic.Anthropic()
    numbered = "\n".join(f"{i+1}. {n}" for i, n in enumerate(names))
    try:
        resp = client.messages.create(
            model=CLASSIFY_MODEL, max_tokens=800,
            messages=[{"role": "user",
                       "content": CLASSIFY_PROMPT.format(numbered=numbered)}])
        meter.add(CLASSIFY_MODEL, resp.usage)
        txt = "".join(b.text for b in resp.content
                      if getattr(b, "type", "") == "text")
        verdicts = dict(re.findall(r"(\d+)\s*:\s*([PX])", txt.upper()))
        return {n: verdicts.get(str(i + 1), "P") == "P"
                for i, n in enumerate(names)}
    except CostCapExceeded:
        raise
    except Exception as e:
        print(f"   name classifier failed ({str(e)[:80]}) — failing open")
        return {n: True for n in names}


RESEARCH_PROMPT = """Research the artist "{name}". Search auction records and market
evidence. Return ONLY JSON:
{{"tier": "strong|listed|minor|none",
 "market_note": "one line: typical/high auction prices with source, or 'no market found'",
 "market_high_usd": number or null,
 "evidence": "one or two sentences citing what you found"}}
tier: strong = documented results over ~$2,000 or clear institutional importance;
listed = real auction/market presence below that; minor = traces only; none =
nothing credible for THIS artist (beware name coincidences - be conservative)."""

_JSON = re.compile(r"\{.*\}", re.DOTALL)


def research_artist(conn, name: str, meter: CostMeter):
    """One web-researched verdict per new artist, cached forever."""
    row = lookup(conn, name)
    if row:
        return row
    client = anthropic.Anthropic()
    tier, note, high, evidence = "unknown", "", None, "research failed"
    try:
        resp = client.messages.create(
            model=RESEARCH_MODEL, max_tokens=1500,
            thinking={"type": "disabled"},
            tools=[{"type": "web_search_20260209", "name": "web_search",
                    "max_uses": 2}],
            messages=[{"role": "user",
                       "content": RESEARCH_PROMPT.format(name=name)}])
        meter.add(RESEARCH_MODEL, resp.usage)
        searches = getattr(getattr(resp.usage, "server_tool_use", None),
                           "web_search_requests", 0) or 0
        meter.total += searches * WEB_SEARCH_COST_USD
        txt = "".join(b.text for b in resp.content
                      if getattr(b, "type", "") == "text")
        m = _JSON.search(txt)
        if m:
            parsed = json.loads(m.group(0))
            tier = str(parsed.get("tier", "unknown")).lower()
            note = str(parsed.get("market_note", ""))[:300]
            evidence = str(parsed.get("evidence", ""))[:500]
            try:
                high = float(parsed["market_high_usd"]) if parsed.get(
                    "market_high_usd") else None
            except (TypeError, ValueError):
                high = None
    except CostCapExceeded:
        raise  # the budget stop must reach the caller — never record it as
        # a "failed research" result (that swallowed the cap: live $8.96
        # spend against a $1.50 cap on 2026-07-14)
    except Exception as e:
        evidence = f"research failed: {str(e)[:120]}"
    conn.execute(
        "INSERT OR REPLACE INTO artists (artist_key, artist, source, tier,"
        " market_high_usd, market_note, evidence, researched_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (artist_key(name), name, "wallhunter", tier, high, note, evidence,
         db.now()))
    conn.commit()
    print(f"   researched '{name}' -> {tier}"
          f"{f' (high ${high:,.0f})' if high else ''}")
    return lookup(conn, name)
