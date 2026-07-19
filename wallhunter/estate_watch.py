"""Named-estate watch for off-radar auctions (per Daniel, 2026-07-18).

Auctions titled after a specific person — 'Estate of John R. Smith',
'Pfendler Estate Auction', 'The Vogel Collection' — go to the TOP of the
off-radar email for his personal double-check, and the named person is
web-researched once (with the auction house as the disambiguating locator)
to see whether they are a documented collector/artist/dealer.

Reuses the estate-sale dossier machinery: same conservative prompt, same
notable-verdict rules, cached forever per (person, house). Detection is
free regex; research is ~$0.15/name, capped per run as a tripwire.
"""

import json
import re

import anthropic

from .config import CostCapExceeded, CostMeter
from .dossier import (NAME_PATTERNS, RESEARCH_MODEL, WEB_SEARCH_COST_USD,
                      _JSON, _STOPWORDS, _text_of, NOTABLE)

# Own prompt, deliberately NOT the dossier's: searches must chase the
# PERSON in a PLACE (obituaries, donor records, news) — naming the auction
# house just pulls up the auction's own listing pages (Daniel's catch).
PROMPT = """Research whether "{name}" — a person in {location} whose personal
estate is being auctioned — is/was a DOCUMENTED art collector, artist, art
dealer, gallerist, curator, or art academic. Search obituaries, local news,
museum donor and collection records, gallery and exhibition history.
Do NOT search for or cite the estate auction or sale listing itself —
auction listings are the starting point, never evidence. Common names need
corroborating detail (location, dates, profession) before a match counts.
Return ONLY JSON:
{{"verdict": "collector|artist|dealer|curator|academic|not_notable|unknown",
 "confidence": "high|medium|low",
 "evidence": "one or two sentences citing what you found and where, or why nothing matched"}}
Be conservative: verdict other than not_notable/unknown requires specific,
checkable evidence about THIS person, not just a name coincidence."""

# 'Greenville NC' / 'Jonesborough, TN' inside a title
_TITLE_LOC = re.compile(
    r"\b([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)?),?\s+"
    r"(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|"
    r"MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|"
    r"WA|WV|WI|WY)\b")


def location_for(auction: dict) -> str:
    if auction.get("location"):
        return auction["location"]
    m = _TITLE_LOC.search(auction.get("title") or "")
    if m:
        return f"{m.group(1)}, {m.group(2)}"
    return "the United States (exact city unknown)"

# 'Pfendler Estate Auction' — name BEFORE the word estate (dossier's
# patterns only cover 'estate of X' and 'the X collection')
_NAME_THEN_ESTATE = re.compile(
    r"([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,2})\s+(?i:estate\b)")

# realty/marketing vocabulary that precedes 'Estate' without naming anyone
_EXTRA_STOP = {
    "club", "country", "living", "prominent", "luxury", "luxurious", "major",
    "star", "treasures", "treasure", "heritage", "preserve", "royal", "palm",
    "yacht", "prestigious", "upscale", "exclusive", "premier", "grand",
    "classic", "quality", "complete", "full", "final", "beautiful",
    "stunning", "waterfront", "lakefront", "lakeside", "ranch", "farm",
    "warehouse", "storage", "local", "large", "spectacular", "incredible",
    "packed", "loaded", "real", "multi", "big", "sale", "sales", "wonderful",
    # months, streets, and location fragments are not people
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "ave", "avenue", "street", "st", "rd", "road", "blvd", "boulevard",
    "court", "ct", "lane", "ln", "drive", "hwy", "highway", "person",
    "inhome", "in", "at", "on", "va", "nc", "sc", "ga", "fl", "tx", "ca",
    "ny", "pa", "oh", "mi", "il", "tn", "ky", "az", "wa", "mo", "wi", "mn",
}


def named_estate_person(title: str) -> str | None:
    """Pure (unit-tested): the person a sale is named after, or None."""
    t = title or ""
    for pat in list(NAME_PATTERNS) + [_NAME_THEN_ESTATE]:
        for m in pat.finditer(t):
            name = re.sub(r"\s+", " ", m.group(1)).strip(" .,'&-")
            words = name.split()
            if not words or any(
                    w.lower() in _STOPWORDS or w.lower() in _EXTRA_STOP
                    for w in words):
                continue
            if len(name) < 4:
                continue
            if len(words) == 1 and name.isupper():
                continue  # '5 STAR ESTATE' shouting, not a surname
            return name
    return None


def _lookup(conn, person: str, house: str):
    return conn.execute(
        "SELECT * FROM estate_identities WHERE person=? AND house=?",
        (person, house)).fetchone()


def _research(conn, person: str, house: str, meter: CostMeter,
              location: str = ""):
    """One conservative web-research verdict, cached forever. Transient
    failures cache nothing (retried next run); CostCapExceeded propagates."""
    from . import db as wdb
    client = anthropic.Anthropic()
    try:
        resp = client.messages.create(
            model=RESEARCH_MODEL, max_tokens=2500,
            thinking={"type": "disabled"},
            tools=[{"type": "web_search_20260209", "name": "web_search",
                    "max_uses": 3}],
            messages=[{"role": "user", "content": PROMPT.format(
                name=person,
                location=location or "the United States (exact city unknown)")}])
        meter.add(RESEARCH_MODEL, resp.usage)
        searches = getattr(getattr(resp.usage, "server_tool_use", None),
                           "web_search_requests", 0) or 0
        meter.total += searches * WEB_SEARCH_COST_USD
        verdict, confidence, evidence = "unknown", "low", ""
        m = _JSON.search(_text_of(resp))
        if m:
            parsed = json.loads(m.group(0))
            verdict = str(parsed.get("verdict", "unknown")).lower()
            confidence = str(parsed.get("confidence", "low")).lower()
            evidence = str(parsed.get("evidence", ""))[:500]
    except CostCapExceeded:
        raise  # budget stop must propagate, never be cached as a verdict
    except Exception as e:
        print(f"  estate-watch: research of '{person}' failed transiently"
              f" ({str(e)[:70]}) — will retry next run")
        return None
    if verdict == "unknown" and confidence == "low":
        # carries no information worth freezing — often means web search
        # was throttled mid-call. Leave uncached so a later run retries.
        print(f"  estate-watch: '{person}' inconclusive (search-limited?)"
              " — will retry next run")
        return None
    notable = 1 if (verdict in NOTABLE and confidence in ("high", "medium")) \
        else 0
    conn.execute(
        "INSERT OR REPLACE INTO estate_identities (person, house, verdict,"
        " confidence, evidence, notable, checked_at) VALUES (?,?,?,?,?,?,?)",
        (person, house, verdict, confidence, evidence, notable, wdb.now()))
    conn.commit()
    tag = "🔥 NOTABLE " + verdict.upper() if notable else verdict
    print(f"  estate-watch: '{person}' ({house[:30]}) -> {tag}")
    return _lookup(conn, person, house)


def find_named_estates(conn, auctions: list[dict],
                       meter: CostMeter | None = None) -> list[dict]:
    """Named-person estate auctions, researched where budget allows, sorted
    notable-first for the top of the email. meter=None -> detection and
    cached verdicts only (no new research)."""
    out = []
    for a in auctions:
        person = named_estate_person(a.get("title") or "")
        if not person:
            continue
        # bare surnames are unresearchable ('Anderson' + a house name can
        # only ever come back unknown) — they show in the email for
        # Daniel's own eye but never spend research money
        researchable = len(person.split()) >= 2
        row = _lookup(conn, person, a.get("house") or "")
        if row is None and researchable and meter is not None:
            try:
                row = _research(conn, person, a.get("house") or "", meter,
                                location=location_for(a))
            except CostCapExceeded:
                print("  estate-watch: research budget cap hit —"
                      " remaining names carry to next run")
                meter = None
        out.append({**a, "person": person, "researchable": researchable,
                    "verdict": row["verdict"] if row else None,
                    "notable": bool(row["notable"]) if row else False,
                    "evidence": (row["evidence"] or "") if row else ""})
    out.sort(key=lambda x: (0 if x["notable"] else
                            (1 if x["verdict"] is None else 2),
                            x.get("ends") or "9999"))
    return out
