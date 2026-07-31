"""Comparable-sales valuation engine — how MutualArt data becomes a number.

CANONICAL SHARED FILE — load by path like the other wallhunter clients.

Division of labor (the whole design):
  * THE MODEL decides what the subject work IS — medium, size, and especially
    its series/subject — because that's fuzzy judgment (llm_client / an injected
    classify_fn supplies it).
  * THE DATA decides what that's WORTH — this module filters the artist's banked
    comps to the same medium, weights the survivors by size, period, sale
    recency, sold-vs-unsold, and series match, trend-adjusts to today, and
    returns a weighted price range with an honest confidence.

Pure and dependency-free: parse_* and comp_estimate() take plain dicts and do
math only, so they're unit-testable with no DB and no network. fetch_comps()
and value_lot() add the prices.db read and the model hop on top.

    comp_estimate(subject, comps, now_year) -> {mid, low, high, confidence, ...}
    value_lot(lot, artist, classify_fn, now_year) -> the above, end to end
"""

import os
import importlib.util as _il
import math
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(os.path.expanduser("~/estate-art-scanner/wh_data/prices.db"))

# --- weighting knobs (tunable; documented so the first live crawl can calibrate)
SIZE_SIGMA = 0.55       # gaussian width on ln(area ratio); ~0.55 => 2x size ~0.6
YEAR_SIGMA = 9.0        # gaussian width on |work-year delta| in years
RECENCY_HALFLIFE = 6.0  # a sale's weight halves every N years (post trend-adjust)
SERIES_BOOST = 1.7      # multiplier for a comp in the same series as the subject
SOLD_ONLY_FOR_VALUE = True   # unsold lots inform demand/confidence, not the median
MISSING_SIZE_W = 0.6    # neutral partial weight when a size is unknown
MISSING_YEAR_W = 0.7    # neutral partial weight when a work-year is unknown
MAX_TREND_CAGR = 0.30   # clamp the fitted artist trend to +/-30%/yr (anti-blowup)
BIG3 = ("christie", "sotheby", "phillips")   # venue split: the big three


def venue_tier(house) -> str:
    h = (house or "").lower()
    return "big3" if any(b in h for b in BIG3) else "mid"



# --------------------------------------------------------------------------- #
#  parsers  (also used at ingest so comps are stored already-enriched)
# --------------------------------------------------------------------------- #
_FRAC = {"¼": .25, "½": .5, "¾": .75, "⅓": 1/3, "⅔": 2/3, "⅛": .125}


def _num(tok):
    """'24', '24.5', '24 1/2', '24½' -> float, or None."""
    if tok is None:
        return None
    t = tok.strip()
    for g, v in _FRAC.items():
        t = t.replace(g, f" {v}")
    m = re.match(r"^\s*(\d+(?:\.\d+)?)(?:\s+(\d+)\s*/\s*(\d+))?\s*$", t)
    if not m:
        m2 = re.match(r"^\s*(\d+(?:\.\d+)?)\s+(\d*\.\d+)\s*$", t)  # "24 0.5"
        if m2:
            return float(m2.group(1)) + float(m2.group(2))
        return None
    whole = float(m.group(1))
    if m.group(2) and m.group(3):
        whole += float(m.group(2)) / float(m.group(3))
    return whole


_DIM_RE = re.compile(
    r"(\d+(?:\.\d+)?(?:\s+\d+\s*/\s*\d+)?|\d+\s*[¼½¾⅓⅔⅛])\s*[x×by]\s*"
    r"(\d+(?:\.\d+)?(?:\s+\d+\s*/\s*\d+)?|\d+\s*[¼½¾⅓⅔⅛])"
    r"(?:\s*[x×by]\s*\d+(?:\.\d+)?)?", re.I)


def parse_dimensions(text):
    """Best-effort (width_in, height_in, area_sqin) from a listing. Prefers an
    inch measurement; converts a cm-only measurement. Returns (None,None,None)
    if nothing parseable."""
    if not text:
        return (None, None, None)
    t = str(text)
    candidates = []  # (is_inches, w, h)
    for m in _DIM_RE.finditer(t):
        w, h = _num(m.group(1)), _num(m.group(2))
        if not w or not h:
            continue
        tail = t[m.end():m.end() + 12].lower()
        head = t[max(0, m.start() - 4):m.start()].lower()
        is_cm = "cm" in tail or "cm" in head or "centim" in tail
        is_in = ('"' in tail or "in" in tail or "inch" in tail
                 or head.endswith("h") or head.endswith("w"))
        candidates.append((is_in and not is_cm, w, h, is_cm))
    if not candidates:
        return (None, None, None)
    inch = [(w, h) for ok, w, h, cm in candidates if ok]
    if inch:
        w, h = inch[0]
    else:
        # cm -> in for the first (or the only) match
        _, w, h, cm = candidates[0]
        if cm:
            w, h = w / 2.54, h / 2.54
    if not (0.5 <= w <= 600 and 0.5 <= h <= 600):
        return (None, None, None)
    return (round(w, 1), round(h, 1), round(w * h, 1))


def parse_work_year(*texts):
    """The year the WORK was made (not the sale). Prefers a parenthetical year;
    else any plausible 1850-2035 four-digit year. Returns int or None."""
    for t in texts:
        if not t:
            continue
        t = str(t)
        m = re.search(r"\((?:c(?:irca)?\.?\s*)?((?:18|19|20)\d{2})\)", t, re.I)
        if m:
            return int(m.group(1))
    for t in texts:
        if not t:
            continue
        for y in re.findall(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)", str(t)):
            yi = int(y)
            if 1850 <= yi <= 2035:
                return yi
    return None


_MEDIA = [
    ("print", r"\b(print|lithograph|litho|serigraph|screen ?print|silk ?screen|"
              r"etching|engraving|aquatint|woodcut|linocut|giclee|giclée|"
              r"offset|edition of|\b\d{1,4}\s*/\s*\d{1,4}\b)"),
    ("sculpture", r"\b(sculpture|bronze|cast|carv|marble|terracotta|maquette|"
                  r"welded|patina)"),
    ("photograph", r"\b(photograph|gelatin silver|c-?print|chromogenic|"
                   r"platinum print|albumen|daguerreo|type c)"),
    ("painting", r"\b(oil|acrylic|tempera|enamel|encaustic|alkyd)\b|on canvas|"
                 r"on panel|on board|on masonite"),
    ("work_on_paper", r"\b(watercolou?r|gouache|pastel|charcoal|graphite|"
                      r"pen and ink|\bink\b|drawing|crayon|conté|conte)\b|"
                      r"on paper"),
    ("textile", r"\b(tapestry|textile|needlework|embroider|quilt|weaving)"),
]


def medium_category(*texts):
    """Market-meaningful category for the hard filter: painting / work_on_paper /
    print / sculpture / photograph / textile / other. Prints are checked first
    (an 'oil'-mentioning print listing is still a print)."""
    blob = " ".join(str(t or "") for t in texts).lower()
    for cat, pat in _MEDIA:
        if re.search(pat, blob, re.I):
            return cat
    return "other"


# --------------------------------------------------------------------------- #
#  money / currency  (MutualArt is international — many comps are £ or €)
# --------------------------------------------------------------------------- #
# Approximate USD rates for a *rough* conversion. We always store the NATIVE
# amount + currency too, so these can be refined (or made historical) later
# without re-scraping.
CURRENCY_RATES = {
    "USD": 1.0, "GBP": 1.27, "EUR": 1.08, "JPY": 0.0067, "CHF": 1.13,
    "HKD": 0.128, "CNY": 0.14, "CAD": 0.73, "AUD": 0.66, "SGD": 0.74,
}
# Order matters — prefixed dollars before a bare "$", ¥ resolved to JPY last.
_CCY_TOKENS = [
    ("HK$", "HKD"), ("HKD", "HKD"), ("CA$", "CAD"), ("C$", "CAD"),
    ("CAD", "CAD"), ("AU$", "AUD"), ("A$", "AUD"), ("AUD", "AUD"),
    ("S$", "SGD"), ("SGD", "SGD"), ("US$", "USD"), ("USD", "USD"),
    ("£", "GBP"), ("GBP", "GBP"), ("€", "EUR"), ("EUR", "EUR"),
    ("CHF", "CHF"), ("RMB", "CNY"), ("CN¥", "CNY"), ("CNY", "CNY"),
    ("JP¥", "JPY"), ("JPY", "JPY"), ("¥", "JPY"), ("$", "USD"),
]


def _detect_currency(text):
    up = text.upper()
    # longest token first: "US$" must win over the "S$" it contains, "CA$"
    # over "A$", etc.; the bare "$" is length 1 and checked last.
    for tok, code in sorted(_CCY_TOKENS, key=lambda x: -len(x[0])):
        if tok.upper() in up:
            return code
    return "USD"


def _extract_amount(text):
    """Largest numeric run in the string, resolving US (1,234.56) vs European
    (1.234,56) thousands/decimal conventions. Returns float or None."""
    runs = re.findall(r"\d[\d.,]*\d|\d", text.replace(" ", ""))
    if not runs:
        return None
    s = max(runs, key=len)
    has_c, has_d = "," in s, "." in s
    if has_c and has_d:
        if s.rfind(",") > s.rfind("."):          # 1.234,56 -> euro
            s = s.replace(".", "").replace(",", ".")
        else:                                     # 1,234.56 -> us
            s = s.replace(",", "")
    elif has_c:
        parts = s.split(",")
        s = (s.replace(",", ".") if len(parts) == 2 and len(parts[1]) in (1, 2)
             else s.replace(",", ""))
    elif has_d:
        parts = s.split(".")
        if not (len(parts) == 2 and len(parts[1]) in (1, 2)):
            s = s.replace(".", "")                # 1.234(.567) -> euro thousands
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def parse_money(text):
    """'£180,000' / '€45.000' / 'US$12,500' / 'HK$1,200,000' ->
    (amount_native, currency_code, usd_amount). (None,None,None) if unparseable.
    USD is approximate (CURRENCY_RATES); the native amount is exact and stored."""
    if text is None:
        return (None, None, None)
    if isinstance(text, (int, float)):
        return (float(text), "USD", float(text)) if text > 0 else (None, None, None)
    amt = _extract_amount(str(text))
    if amt is None:
        return (None, None, None)
    ccy = _detect_currency(str(text))
    return (amt, ccy, round(amt * CURRENCY_RATES.get(ccy, 1.0), 2))


# --------------------------------------------------------------------------- #
#  weighting helpers
# --------------------------------------------------------------------------- #
def _w_size(a_sub, a_comp):
    if not a_sub or not a_comp:
        return MISSING_SIZE_W
    r = math.log(a_comp / a_sub)
    return math.exp(-(r * r) / (2 * SIZE_SIGMA * SIZE_SIGMA))


def _w_year(y_sub, y_comp):
    if not y_sub or not y_comp:
        return MISSING_YEAR_W
    d = y_comp - y_sub
    return math.exp(-(d * d) / (2 * YEAR_SIGMA * YEAR_SIGMA))


def _w_recency(sale_year, now_year):
    if not sale_year or not now_year:
        return 0.7
    return 0.5 ** (max(0, now_year - sale_year) / RECENCY_HALFLIFE)


def _series_match(subject_tokens, comp_title):
    if not subject_tokens or not comp_title:
        return False
    t = comp_title.lower()
    return any(tok and tok.lower() in t for tok in subject_tokens)


def _fit_trend(points):
    """Log-linear CAGR from (sale_year, price) points. Clamped. None if too few
    or unstable."""
    pts = [(y, p) for y, p in points if y and p and p > 0]
    if len(pts) < 6:
        return None
    xs = [y for y, _ in pts]
    ys = [math.log(p) for _, p in pts]
    n = len(pts)
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    cagr = math.exp(slope) - 1
    if not math.isfinite(cagr):
        return None
    return max(-MAX_TREND_CAGR, min(MAX_TREND_CAGR, cagr))


def _weighted_percentile(pairs, q):
    """pairs = [(value, weight)]. Returns the weighted q-quantile."""
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None
    acc = 0.0
    target = q * total
    for v, w in pairs:
        acc += w
        if acc >= target:
            return v
    return pairs[-1][0]


# --------------------------------------------------------------------------- #
#  the estimate
# --------------------------------------------------------------------------- #
def comp_estimate(subject, comps, now_year=2026, _stratify=True):
    """Value the subject from a list of comps. PURE — no DB, no model.

    subject: {"medium": str, "area_sqin": float|None, "work_year": int|None,
              "series_tokens": [str]}   (series_tokens come from the model)
    comps:   [{"title","medium"|"medium_category","area_sqin","work_year",
               "price_usd","outcome","sale_year"}]
    Returns {mid, low, high, n, n_effective, confidence, medium_category,
             trend_cagr, used[], note}.
    """
    scat = subject.get("medium_category") or medium_category(
        subject.get("medium"), subject.get("title"))
    stoks = subject.get("series_tokens") or []
    a_sub = subject.get("area_sqin")
    y_sub = subject.get("work_year")

    same = []
    unsold_same = 0
    for c in comps:
        ccat = c.get("medium_category") or medium_category(
            c.get("medium"), c.get("title"))
        if ccat != scat:
            continue
        sold = (c.get("outcome") in ("sold", "final_bid")
                and c.get("price_usd"))
        if not sold:
            unsold_same += 1
            if SOLD_ONLY_FOR_VALUE:
                continue
        same.append(c)

    if not same:
        return {"mid": None, "low": None, "high": None, "n": 0,
                "n_effective": 0.0, "confidence": "none",
                "medium_category": scat, "trend_cagr": None, "used": [],
                "note": f"no sold {scat} comps for this artist"}

    cagr = _fit_trend([(c.get("sale_year"), c.get("price_usd")) for c in same])

    weighted = []
    for c in same:
        price = float(c["price_usd"])
        sy = c.get("sale_year")
        if cagr is not None and sy:
            price *= (1 + cagr) ** (now_year - sy)          # trend-adjust to now
        w = (_w_size(a_sub, c.get("area_sqin"))
             * _w_year(y_sub, c.get("work_year"))
             * _w_recency(sy, now_year))
        if _series_match(stoks, c.get("title")):
            w *= SERIES_BOOST
        if w <= 0:
            continue
        weighted.append((price, w, c))

    if not weighted:
        return {"mid": None, "low": None, "high": None, "n": 0,
                "n_effective": 0.0, "confidence": "none",
                "medium_category": scat, "trend_cagr": cagr, "used": [],
                "note": "comps found but all weights zero"}

    pairs = [(p, w) for p, w, _ in weighted]
    mid = _weighted_percentile(pairs, 0.5)
    low = _weighted_percentile(pairs, 0.25)
    high = _weighted_percentile(pairs, 0.75)
    n = len(weighted)
    n_eff = sum(w for _, w in pairs)
    spread = (high - low) / mid if mid else 9.9

    if n_eff >= 4 and n >= 5 and spread <= 1.2:
        conf = "high"
    elif n_eff >= 1.5 and n >= 2:
        conf = "medium"
    else:
        conf = "low"

    used = sorted(weighted, key=lambda t: t[1], reverse=True)[:6]
    used_out = [{"title": (c.get("title") or "")[:70],
                 "price_usd": round(p), "weight": round(w, 3),
                 "sale_year": c.get("sale_year"),
                 "area_sqin": c.get("area_sqin"),
                 "work_year": c.get("work_year")} for p, w, c in used]

    note = (f"{n} sold {scat} comps (n_eff {n_eff:.1f}), "
            f"{unsold_same} unsold; "
            + (f"trend {cagr*100:+.0f}%/yr; " if cagr is not None else "")
            + f"spread {spread:.0%}")

    out = {"mid": round(mid), "low": round(low), "high": round(high), "n": n,
           "n_effective": round(n_eff, 2), "confidence": conf,
           "medium_category": scat, "trend_cagr": cagr, "used": used_out,
           "note": note}

    # Venue split (Daniel's model): what it fetches at a mid-tier house vs at
    # the big three (if they would take it) — plus the standalone signal that
    # big-three results exist at all. Same weighting, per venue stratum.
    if _stratify:
        big3 = [c for c in comps if venue_tier(c.get("house")) == "big3"]
        out["big3_seen"] = bool(big3)
        out["big3_count"] = len(big3)
        if big3:
            others = [c for c in comps if venue_tier(c.get("house")) != "big3"]
            e3 = comp_estimate(subject, big3, now_year, _stratify=False)
            em = (comp_estimate(subject, others, now_year, _stratify=False)
                  if others else {"mid": None, "n": 0, "confidence": "none"})
            out["venue_split"] = {
                "big3":     {"mid": e3.get("mid"), "low": e3.get("low"),
                             "high": e3.get("high"), "n": e3.get("n", 0),
                             "confidence": e3.get("confidence")},
                "mid_tier": {"mid": em.get("mid"), "low": em.get("low"),
                             "high": em.get("high"), "n": em.get("n", 0),
                             "confidence": em.get("confidence")},
            }
    return out


# --------------------------------------------------------------------------- #
#  DB + model layers on top of the pure estimate
# --------------------------------------------------------------------------- #
_conn = None


def _connect():
    global _conn
    if _conn is None and DB_PATH.exists():
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def _load(name, path):
    try:
        spec = _il.spec_from_file_location(name, path)
        mod = _il.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


_prices = _load(
    "prices_client",
    os.path.expanduser("~/estate-art-scanner/wallhunter/prices_client.py"))


def fetch_comps(artist):
    """All banked tier-A MutualArt comps for the artist, enriched with the
    parsed fields the estimate needs. Missing columns degrade gracefully."""
    try:
        conn = _connect()
        ak = _prices._key(artist) if _prices else re.sub(
            r"\s+", " ", re.sub(r"[^a-z ]+", " ", (artist or "").lower())).strip()
        if conn is None or not ak:
            return []
        cols = {r[1] for r in conn.execute("PRAGMA table_info(prices)")}
        want = ["title", "price_usd", "outcome", "estimate", "sale_date"]
        extra = [c for c in ("medium", "area_sqin", "work_year", "size_raw",
                             "currency", "price_native", "house") if c in cols]
        rows = conn.execute(
            f"SELECT {', '.join(want + extra)} FROM prices"
            " WHERE artist_key=? AND platform='mutualart' AND tier='A'"
            " AND suspect=0", (ak,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if not d.get("area_sqin"):
                d["area_sqin"] = (parse_dimensions(d.get("size_raw"))[2]
                                  or parse_dimensions(d.get("title"))[2])
            if not d.get("work_year"):
                d["work_year"] = parse_work_year(d.get("title"))
            d["medium_category"] = medium_category(d.get("medium"), d.get("title"))
            d["sale_year"] = parse_work_year(d.get("sale_date")) or (
                int(str(d.get("sale_date"))[:4])
                if str(d.get("sale_date"))[:4].isdigit() else None)
            out.append(d)
        return out
    except Exception:
        return []


def value_lot(lot, artist, classify_fn=None, now_year=2026):
    """End to end: classify the subject (model), value it (data).

    classify_fn(lot) -> {"medium","area_sqin","work_year","series_tokens"}.
    If omitted, defaults to llm_client.classify_work (the model supplies the
    series intelligence); if the model endpoint is down, classify_work returns
    {} and the subject falls back to parse-only. Returns comp_estimate(...) plus
    'subject'."""
    if classify_fn is None:
        _llm = _load(
            "llm_client",
            os.path.expanduser("~/estate-art-scanner/wallhunter/llm_client.py"))
        classify_fn = getattr(_llm, "classify_work", None) if _llm else None
    subject = {}
    if classify_fn is not None:
        try:
            subject = classify_fn(lot) or {}
        except Exception:
            subject = {}
    text = " ".join(str(lot.get(k, "")) for k in ("title", "description", "desc"))
    subject.setdefault("title", lot.get("title"))
    subject.setdefault("medium", lot.get("medium") or text)
    if not subject.get("area_sqin"):
        subject["area_sqin"] = parse_dimensions(text)[2]
    if not subject.get("work_year"):
        subject["work_year"] = parse_work_year(text)
    subject.setdefault("series_tokens", [])
    est = comp_estimate(subject, fetch_comps(artist), now_year)
    est["subject"] = {k: subject.get(k) for k in
                      ("medium", "medium_category", "area_sqin", "work_year",
                       "series_tokens")}
    est["subject"]["medium_category"] = est["medium_category"]
    return est


if __name__ == "__main__":
    import json
    print("=== parser checks ===")
    for s in ["40 x 30 in", "24 1/2 x 18 1/4 inches", "101.6 x 76.2 cm",
              'oil on canvas, 60 × 48"', "sight 12 x 9 in (30.5 x 22.9 cm)"]:
        print(f"  {s!r:42} -> {parse_dimensions(s)}")
    for s in ["Elegy (1975)", "Untitled, circa 1962", "painted 1988, sold 2021"]:
        print(f"  year {s!r:32} -> {parse_work_year(s)}")
    for s in ["Oil on canvas", "Lithograph, ed. 44/100", "Bronze",
              "Watercolor on paper", "Gelatin silver print"]:
        print(f"  medium {s!r:28} -> {medium_category(s)}")
