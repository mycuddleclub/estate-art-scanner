"""Named-collection detection: 'Estate of X' / 'The X Collection' titles,
web-researched once per sale.

A notable identity (documented collector, artist, dealer, curator, academic)
pins the sale's context score high and gets a banner in the digest. The
verdict is stored WITH its evidence and is always presented as researched
context, never as certainty.
"""

import json
import re

import anthropic

from . import db
from .config import CostCapExceeded, CostMeter

RESEARCH_MODEL = "claude-sonnet-5"
WEB_SEARCH_COST_USD = 0.01  # per search, billed on top of tokens

# keyword parts match any case via scoped (?i:...); the captured name itself
# must be capitalized, so "collection of a lifetime" can't produce a "name"
NAME_PATTERNS = [
    re.compile(
        r"(?i:collection|estate|property|residence|home)\s+(?i:of)\s+"
        r"(?i:the\s+)?(?i:late\s+)?(?i:(?:dr|mr|mrs|ms|prof)\.?\s+)?"
        # couple names: allow 'and'/'&' between capitalized words
        r"([A-Z][A-Za-z.'-]+(?:\s+(?:(?i:and)\s+|&\s+)?[A-Z][A-Za-z.'-]+){0,4})"),
    re.compile(r"(?i:\bthe\s+)([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,2})"
               r"\s+(?i:collection\b)"),
]
# words that make a "name" clearly not a person
_STOPWORDS = {
    "art", "fine", "estate", "estates", "jewelry", "jewellery", "antique",
    "antiques", "auction", "auctions", "moving", "downsizing", "lifetime",
    "private", "personal", "entire", "amazing", "huge", "online", "tag",
    "vintage", "collector", "collectors", "collection", "collections",
    "midcentury", "mid-century", "modern", "century", "furniture", "coin",
    "coins", "stamp", "stamps", "record", "records", "book", "books",
    "toy", "toys", "tool", "tools", "gun", "guns", "car", "cars",
}


def extract_names(title: str, description: str = "") -> list[str]:
    """Candidate person/family names from sale title + description text."""
    text = f"{title or ''}\n{re.sub(r'<[^>]+>', ' ', description or '')[:600]}"
    found: list[str] = []
    for pat in NAME_PATTERNS:
        for m in pat.finditer(text):
            name = re.sub(r"\s+", " ", m.group(1)).strip(" .,'&-")
            words = name.split()
            if not words or any(w.lower() in _STOPWORDS for w in words):
                continue
            if len(name) < 4 or name.lower() in ("the", "his", "her"):
                continue
            if name not in found:
                found.append(name)
    return found[:3]


PROMPT = """Research whether "{name}" — associated with an estate sale in {location} —
is a DOCUMENTED art collector, artist, art dealer, gallerist, curator, or art
academic. Search the web (museum sites, news, obituaries, gallery records).
Common names need corroborating detail (location, dates) before a match counts.
Return ONLY JSON:
{{"verdict": "collector|artist|dealer|curator|academic|not_notable|unknown",
 "confidence": "high|medium|low",
 "evidence": "one or two sentences citing what you found and where, or why nothing matched"}}
Be conservative: verdict other than not_notable/unknown requires specific,
checkable evidence about THIS person, not just a name coincidence."""

_JSON = re.compile(r"\{.*\}", re.DOTALL)
NOTABLE = {"collector", "artist", "dealer", "curator", "academic"}


def _text_of(response) -> str:
    return "".join(b.text for b in response.content
                   if getattr(b, "type", "") == "text").strip()


def research_sale_identity(conn, sale_id: int, meter: CostMeter) -> str | None:
    """Extract + research a named identity. Returns verdict or None. Runs once
    per sale (skips if already researched); free when no name pattern fires."""
    sale = conn.execute(
        "SELECT title, description, location, identity_verdict FROM sales WHERE id=?",
        (sale_id,)).fetchone()
    if sale is None or sale["identity_verdict"] is not None:
        return sale["identity_verdict"] if sale else None
    names = extract_names(sale["title"] or "", sale["description"] or "")
    if not names:
        conn.execute("UPDATE sales SET identity_verdict='no_name' WHERE id=?", (sale_id,))
        conn.commit()
        return "no_name"

    name = names[0]
    client = anthropic.Anthropic()
    verdict, confidence, evidence = "unknown", "low", "research call failed"
    try:
        resp = client.messages.create(
            model=RESEARCH_MODEL, max_tokens=2500,
            thinking={"type": "disabled"},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": PROMPT.format(
                name=name, location=sale["location"] or "unknown location")}])
        meter.add(RESEARCH_MODEL, resp.usage)
        searches = getattr(getattr(resp.usage, "server_tool_use", None),
                           "web_search_requests", 0) or 0
        meter.total += searches * WEB_SEARCH_COST_USD
        m = _JSON.search(_text_of(resp))
        if m:
            parsed = json.loads(m.group(0))
            verdict = str(parsed.get("verdict", "unknown")).lower()
            confidence = str(parsed.get("confidence", "low")).lower()
            evidence = str(parsed.get("evidence", ""))[:500]
    except CostCapExceeded:
        raise  # budget stop must propagate, not be recorded as a verdict
    except Exception as e:
        evidence = f"research failed: {str(e)[:150]}"

    is_notable = verdict in NOTABLE and confidence in ("high", "medium")
    conn.execute(
        "UPDATE sales SET identity_name=?, identity_verdict=?, identity_evidence=?,"
        " context_score=CASE WHEN ? THEN MAX(COALESCE(context_score,0), 0.9)"
        " ELSE context_score END WHERE id=?",
        (name, verdict, evidence, 1 if is_notable else 0, sale_id))
    conn.commit()
    tag = "NOTABLE" if is_notable else verdict
    print(f"   identity: '{name}' -> {tag} ({confidence}) — {evidence[:100]}")
    return verdict
