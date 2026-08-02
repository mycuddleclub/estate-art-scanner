"""Artist significance intelligence — the gate that decides what Daniel sees.

Daniel's rule (2026-08-02): a lot only reaches him if the artist is
  (a) museum-backed, OR
  (b) represented by a Tier 1-3 gallery, OR
  (c) has >= $2,000 of documented auction value.
Significance beats price: a gallery/museum artist with no auction record yet
is exactly the early catch he wants.

Cascade, cheapest first, every result cached FOREVER (artists are stable):
  1. LOCAL   authority.db museums + banked comps + Artsy galleries   (instant, $0)
  2. TRIAGE  the 120B batch-rates who is PLAUSIBLY a real listed artist,
             purely to spend the search budget well — its claims are NEVER
             accepted as proof (it hallucinated "Ron Lee: MoMA, Zwirner")
  3. WEB     free DuckDuckGo/Bing search + grounded read = the only way an
             artist outside the local databases can earn significance

Nothing here ever calls a paid API.
"""
import html as _html
import json
import os
import re
import sqlite3
import time

import httpx

from . import config, evidence, galleries

DB = config.WH_DATA / "artist_intel.db"
MIN_VALUE = float(os.environ.get("LW_MIN_VALUE", "2000"))
WEB_BUDGET = int(os.environ.get("LW_WEB_BUDGET", "25"))     # searches per cycle
BATCH = int(os.environ.get("LW_KNOW_BATCH", "20"))          # artists per model call
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
_conn = None
_last_search = [0.0]


def _db():
    global _conn
    if _conn is None:
        config.data_dirs()
        _conn = sqlite3.connect(DB, timeout=30)
        _conn.execute("""CREATE TABLE IF NOT EXISTS artist (
            akey TEXT PRIMARY KEY, name TEXT, significant INT, why TEXT,
            standing TEXT, museums TEXT, gallery TEXT, gallery_tier INT,
            market_high REAL, source TEXT, at REAL)""")
        _conn.commit()
    return _conn


def _key(name):
    return re.sub(r"[^a-z ]", "", (name or "").lower()).strip()


def _cached(name):
    row = _db().execute(
        "SELECT significant, why, standing, museums, gallery, gallery_tier,"
        " market_high, source FROM artist WHERE akey=?", (_key(name),)).fetchone()
    if not row:
        return None
    return {"significant": bool(row[0]), "why": row[1], "standing": row[2],
            "museums": row[3], "gallery": row[4], "gallery_tier": row[5],
            "market_high": row[6], "source": row[7], "name": name}


def _save(p):
    _db().execute(
        "INSERT OR REPLACE INTO artist (akey,name,significant,why,standing,"
        "museums,gallery,gallery_tier,market_high,source,at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (_key(p["name"]), p["name"][:120], int(p["significant"]), p["why"][:300],
         p.get("standing", ""), p.get("museums", "")[:400], p.get("gallery", "")[:160],
         int(p.get("gallery_tier") or 0), float(p.get("market_high") or 0),
         p.get("source", ""), time.time()))
    _db().commit()


# --------------------------------------------------------------------------- #
#  1. LOCAL — free, instant
# --------------------------------------------------------------------------- #
def _local(name):
    ai = evidence.authority_info(name) or {}
    tier = galleries.best_tier(name)
    ceiling = evidence.market_ceiling(name)
    standing = ai.get("standing", "")
    reasons = []
    if standing:
        reasons.append("museum" if standing == "strong" else "listed")
    if 1 <= tier <= 3:
        reasons.append(f"gallery T{tier}")
    if ceiling >= MIN_VALUE:
        reasons.append(f"auction ${ceiling:,.0f}")
    return {"name": name, "significant": bool(reasons), "why": " + ".join(reasons),
            "standing": standing, "museums": ai.get("museums", ""),
            "gallery": "", "gallery_tier": tier, "market_high": ceiling,
            "source": "local"}


# --------------------------------------------------------------------------- #
#  2. MODEL — batched, free
# --------------------------------------------------------------------------- #
_KNOW = """You are an art-market reference. For EACH artist below, answer ONLY from knowledge you actually have. Never invent museums or prices — if you do not know the artist, say known:false.

Return a STRICT JSON array, one object per artist, same order:
[{{"name":"...","known":true/false,"museums":"comma-separated major museums holding their work, or empty","auction_high_usd":<highest realized auction price you know in USD, else 0>,"gallery":"their main commercial gallery, or empty","note":"<=10 words"}}]

Artists:
{lst}"""


def _model_batch(names):
    if not names:
        return {}
    lst = "\n".join(f"{i+1}. {n}" for i, n in enumerate(names))
    try:
        r = httpx.post(f"{config.LM_BASE}/chat/completions", timeout=240, json={
            "model": config.STAGE3_MODEL, "max_tokens": 2400,
            "reasoning_effort": "low", "temperature": 0.1,
            "messages": [{"role": "user", "content": _KNOW.format(lst=lst)}]})
        txt = r.json()["choices"][0]["message"].get("content") or ""
        m = re.search(r"\[.*\]", txt, re.S)
        if not m:
            return {}
        out = {}
        for d in json.loads(m.group(0)):
            n = (d.get("name") or "").strip()
            if n:
                out[_key(n)] = d
        return out
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
#  3. WEB — free search, polite, budgeted
# --------------------------------------------------------------------------- #
def _search(q):
    """Free web search: DuckDuckGo HTML, Bing fallback. Returns snippet text."""
    gap = time.time() - _last_search[0]
    if gap < 3.0:
        time.sleep(3.0 - gap)
    _last_search[0] = time.time()
    for attempt in range(2):
        try:
            r = httpx.post("https://html.duckduckgo.com/html/", data={"q": q},
                           headers=_UA, timeout=25, follow_redirects=True)
            snips = re.findall(r'class="result__snippet".*?>(.*?)</a>', r.text, re.S)
            texts = [_html.unescape(re.sub("<[^>]+>", "", s)).strip() for s in snips[:6]]
            texts = [t for t in texts if t]
            if texts:
                return "\n".join(texts)
        except Exception:
            pass
        time.sleep(4)
    try:  # Bing fallback
        r = httpx.get("https://www.bing.com/search", params={"q": q},
                      headers=_UA, timeout=25, follow_redirects=True)
        snips = re.findall(r'<p class="b_lineclamp[^"]*">(.*?)</p>', r.text, re.S)
        texts = [_html.unescape(re.sub("<[^>]+>", "", s)).strip() for s in snips[:6]]
        return "\n".join(t for t in texts if t)
    except Exception:
        return ""


_WEB_JUDGE = """From these web search results, assess the artist. Use ONLY what the results support.

Artist: {name}
Search results:
{snips}

STRICT JSON: {{"significant": true/false, "museums":"museums named in results, or empty", "auction_high_usd": <highest realized price shown, else 0>, "gallery":"gallery named, or empty", "why":"<=15 words"}}
significant = museum-collected OR a real documented auction market OR represented by a serious commercial gallery."""


def _web(name):
    snips = _search(f'"{name}" artist auction record museum collection gallery')
    if not snips:
        return None
    try:
        r = httpx.post(f"{config.LM_BASE}/chat/completions", timeout=180, json={
            "model": config.STAGE3_MODEL, "max_tokens": 500,
            "reasoning_effort": "low", "temperature": 0.1,
            "messages": [{"role": "user", "content": _WEB_JUDGE.format(
                name=name, snips=snips[:2500])}]})
        txt = r.json()["choices"][0]["message"].get("content") or ""
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  public: resolve a batch of artists
# --------------------------------------------------------------------------- #
def resolve(names, web_budget=None):
    """{artist_key: profile} for every name. Cached forever; cascade local ->
    model-batch -> web (budgeted). Never calls a paid API."""
    budget = WEB_BUDGET if web_budget is None else web_budget
    names = [n.strip() for n in names if n and len(n.split()) >= 2]
    out, todo = {}, []

    for n in names:
        k = _key(n)
        if k in out:
            continue
        c = _cached(n)
        if c:
            out[k] = c
            continue
        loc = _local(n)
        if loc["significant"]:          # locally proven — done, cache it
            _save(loc)
            out[k] = loc
        else:
            todo.append((k, n, loc))

    # --- TRIAGE (model): rank who is worth a web search. NOT proof. ---
    hints = {}
    for i in range(0, len(todo), BATCH):
        hints.update(_model_batch([n for _, n, _ in todo[i:i + BATCH]]))

    def _priority(item):
        d = hints.get(item[0]) or {}
        # plausible real artists first; obvious unknowns last
        return (0 if d.get("known") else 1,
                -float(d.get("auction_high_usd") or 0))

    todo.sort(key=_priority)

    # --- WEB: the only path to significance outside the local databases ---
    used = 0
    for k, n, base in todo:
        if used >= budget:
            base = dict(base)
            base["deferred"] = True   # undetermined: retry next cycle, never drop
            out[k] = base
            continue
        used += 1
        d = _web(n)
        if not d:
            base = dict(base)
            base["deferred"] = True   # search failed — do NOT cache a false drop
            out[k] = base
            continue
        museums = (d.get("museums") or "").strip()
        high = float(d.get("auction_high_usd") or 0)
        gal = (d.get("gallery") or "").strip()
        tier = base.get("gallery_tier") or (
            galleries.classify_gallery(gal)["tier"] if gal else 0)
        why = []
        if museums:
            why.append("museum")
        if 1 <= tier <= 3:
            why.append(f"gallery T{tier}")
        if high >= MIN_VALUE:
            why.append(f"auction ${high:,.0f}")
        p = {"name": n, "significant": bool(why) and bool(d.get("significant")),
             "why": " + ".join(why) or "web: no museum, gallery or auction record",
             "standing": base["standing"] or ("strong" if museums else ""),
             "museums": museums or base["museums"], "gallery": gal,
             "gallery_tier": tier, "market_high": max(high, base["market_high"]),
             "source": "web"}
        _save(p)              # a real determination — cache it forever
        out[k] = p
    return out


def profile(name):
    return resolve([name]).get(_key(name))
