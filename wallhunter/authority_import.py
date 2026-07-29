"""Importers that distill open museum/vocabulary datasets into authority.db.

Each importer is idempotent (skips if already recorded in sources_meta unless
force=True) and streams its raw file — nothing raw is kept in the database.
Raw downloads live in a scratch dir and are deleted by the caller.
"""

import csv
import json
import re
import sys
import tarfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import authority

csv.field_size_limit(10_000_000)

JUNK_NAME = re.compile(
    r"unidentified|unknown|anonymous|^after |, after$|copy after|"
    r"manufactor|manufactur| & co| co\.|company|corporation| inc\b|"
    r" ltd\b|publisher|printing house|gallery|museum|church|university|"
    r"association|school|center|centre|foundation|society|committee|"
    r"council|institute|studio\b|press\b|archives", re.I)

# Biographical qualifiers that ride along in comma-separated name strings
# ("Floyd Newsum, American" / "Powell, Colin, General") — never name parts.
QUALIFIER_PART = re.compile(
    r"^(?:african[- ]?american|american|british|english|french|german|dutch|"
    r"spanish|italian|irish|scottish|welsh|swedish|norwegian|danish|russian|"
    r"polish|austrian|swiss|belgian|greek|portuguese|mexican|canadian|cuban|"
    r"brazilian|haitian|jamaican|trinidadian|puerto rican|nigerian|ghanaian|"
    r"ethiopian|kenyan|senegalese|malian|congolese|liberian|south african|"
    r"egyptian|moroccan|japanese|chinese|korean|indian|filipino|vietnamese|"
    r"thai|indonesian|australian|israeli|iranian|turkish|lebanese)$"
    r"|^(?:dr|gen(?:eral)?|col(?:onel)?|capt(?:ain)?|maj(?:or)?|lt|"
    r"rev(?:erend)?|bishop|elder|deacon|pastor|rabbi|sir|dame|madam|hon|"
    r"mrs|mr|miss|ms|judge|senator|congressman|congresswoman|governor|"
    r"mayor|professor|prof|sgt|sergeant|pvt|private|admiral|commander)\.?$",
    re.I)


def clean_person_name(raw: str) -> str | None:
    """'Walker, William Aiken, 1838-1921 (painter)' -> 'William Aiken Walker'."""
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", raw or "")
    s = re.split(r",\s*(?:b\.|d\.|ca\.|circa|active|fl\.|\d)", s)[0]
    s = re.sub(r"\s+", " ", s).strip(" ,;")
    if not s or JUNK_NAME.search(s) or re.search(r"\d", s):
        return None
    parts = [p.strip() for p in s.split(",") if p.strip()]
    parts = parts[:1] + [p for p in parts[1:] if not QUALIFIER_PART.match(p)]
    if len(parts) >= 2:  # 'Last, First[, Suffix]' -> 'First Last Suffix'
        s = " ".join([parts[1], parts[0]] + parts[2:])
    else:
        s = parts[0] if parts else ""
    return s if len(s) >= 5 and " " in s else None


def _done(conn, source: str, force: bool) -> bool:
    if force:
        return False
    return conn.execute("SELECT 1 FROM sources_meta WHERE source=?",
                        (source,)).fetchone() is not None


def _record(conn, source: str, n: int, note: str = ""):
    conn.execute(
        "INSERT OR REPLACE INTO sources_meta (source, imported_at, records,"
        " note) VALUES (?,?,?,?)",
        (source, datetime.now(timezone.utc).isoformat(timespec="seconds"),
         n, note))
    conn.commit()
    print(f"  authority: {source}: {n:,} artists")


def _year(v) -> int | None:
    m = re.search(r"-?\d{3,4}", str(v or ""))
    if not m:
        return None
    y = int(m.group())
    return y if 1000 <= y <= 2030 else None


# ---------------------------------------------------------------- museums

def import_moma(conn, path, force=False):
    if _done(conn, "moma", force):
        return
    n = 0
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = clean_person_name(row["DisplayName"])
            if not name:
                continue
            aid = authority.upsert_artist(
                conn, name, "moma",
                ulan_id=(row.get("ULAN") or "").strip() or None,
                wikidata_qid=(row.get("Wiki QID") or "").strip() or None,
                birth_year=_year(row.get("BeginDate")),
                death_year=_year(row.get("EndDate")),
                nationality=(row.get("Nationality") or "").strip() or None)
            if aid:
                authority.add_holding(conn, aid, "moma")
                n += 1
    conn.commit()
    _record(conn, "moma", n)


def import_whitney(conn, artists_path, artworks_path, force=False):
    if _done(conn, "whitney", force):
        return
    counts: dict[str, int] = {}
    with open(artworks_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            for i in (row.get("artist_ids") or "").split(";"):
                i = i.strip()
                if i:
                    counts[i] = counts.get(i, 0) + 1
    n = 0
    with open(artists_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = clean_person_name(row["display_name"])
            if not name:
                continue
            ulan = (row.get("getty_ulan_id") or "").strip() or None
            aid = authority.upsert_artist(
                conn, name, "whitney", ulan_id=ulan,
                wikidata_qid=(row.get("wikidata_id") or "").strip() or None,
                birth_year=_year(row.get("begin_date")),
                death_year=_year(row.get("end_date")))
            if aid:
                authority.add_holding(conn, aid, "whitney",
                                      counts.get(row["id"].strip(), 1))
                n += 1
    conn.commit()
    _record(conn, "whitney", n)


def import_nga(conn, con_path, objcon_path, force=False):
    if _done(conn, "nga", force):
        return
    counts: dict[str, int] = {}
    with open(objcon_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if (row.get("role") or "").strip().lower() == "artist":
                c = row["constituentid"].strip()
                counts[c] = counts.get(c, 0) + 1
    n = 0
    with open(con_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if (row.get("artistofngaobject") or "0").strip() not in ("1", "True"):
                continue
            if (row.get("constituenttype") or "").strip().lower() not in \
                    ("individual", ""):
                continue
            name = clean_person_name(row.get("forwarddisplayname")
                                     or row.get("preferreddisplayname"))
            if not name:
                continue
            aid = authority.upsert_artist(
                conn, name, "nga",
                ulan_id=(row.get("ulanid") or "").strip() or None,
                wikidata_qid=(row.get("wikidataid") or "").strip() or None,
                birth_year=_year(row.get("beginyear")),
                death_year=_year(row.get("endyear")),
                nationality=(row.get("nationality") or "").strip() or None)
            if aid:
                authority.add_holding(conn, aid, "nga",
                                      counts.get(row["constituentid"].strip(), 1))
                n += 1
    conn.commit()
    _record(conn, "nga", n)


def import_cleveland(conn, path, force=False):
    if _done(conn, "cleveland", force):
        return
    n = 0
    # creators: 'John Singer Sargent (American, 1856-1925), artist; ...'
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            for chunk in (row.get("creators") or "").split(";"):
                m = re.match(r"\s*([^(]+?)\s*\(([^,)]*)[,)]", chunk)
                if not m:
                    continue
                name = clean_person_name(m.group(1))
                if not name:
                    continue
                aid = authority.upsert_artist(
                    conn, name, "cleveland",
                    nationality=m.group(2).strip() or None,
                    birth_year=_year((re.search(r"(\d{4})\s*-", chunk) or [None, None])[1]),
                    death_year=_year((re.search(r"-\s*(\d{4})", chunk) or [None, None])[1]))
                if aid:
                    authority.add_holding(conn, aid, "cleveland")
                    n += 1
    conn.commit()
    _record(conn, "cleveland", n)


def import_met(conn, path, force=False):
    if _done(conn, "met", force):
        return
    seen_works = 0
    n = 0
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        for row in csv.DictReader(f):
            names = (row.get("Artist Display Name") or "").split("|")
            roles = (row.get("Artist Role") or "").split("|")
            ulans = (row.get("Artist ULAN URL") or "").split("|")
            wikis = (row.get("Artist Wikidata URL") or "").split("|")
            begs = (row.get("Artist Begin Date") or "").split("|")
            ends = (row.get("Artist End Date") or "").split("|")
            nats = (row.get("Artist Nationality") or "").split("|")
            for i, raw in enumerate(names):
                role = roles[i].strip().lower() if i < len(roles) else "artist"
                if role and "artist" not in role and "painter" not in role \
                        and "sculptor" not in role and "maker" not in role:
                    continue
                name = clean_person_name(raw)
                if not name:
                    continue
                ulan = re.search(r"(\d{9})", ulans[i]) if i < len(ulans) else None
                wiki = re.search(r"(Q\d+)", wikis[i]) if i < len(wikis) else None
                aid = authority.upsert_artist(
                    conn, name, "met",
                    ulan_id=ulan.group(1) if ulan else None,
                    wikidata_qid=wiki.group(1) if wiki else None,
                    birth_year=_year(begs[i] if i < len(begs) else None),
                    death_year=_year(ends[i] if i < len(ends) else None),
                    nationality=(nats[i].strip() if i < len(nats) else "") or None)
                if aid:
                    authority.add_holding(conn, aid, "met")
                    n += 1
            seen_works += 1
            if seen_works % 100000 == 0:
                conn.commit()
                print(f"    met: {seen_works:,} objects...", flush=True)
    conn.commit()
    _record(conn, "met", n)


def import_aic(conn, tar_path, force=False):
    if _done(conn, "aic", force):
        return
    agents: dict[str, dict] = {}
    counts: dict[str, int] = {}
    done_files = 0
    with tarfile.open(tar_path, "r|bz2") as tar:
        for m in tar:
            nm = m.name
            if "/.git/" in nm or not nm.endswith(".json"):
                continue
            if "/json/agents/" in nm:
                d = json.load(tar.extractfile(m))
                agents[str(d.get("id"))] = d
            elif "/json/artworks/" in nm:
                d = json.load(tar.extractfile(m))
                a = d.get("artist_id")
                if a is not None:
                    counts[str(a)] = counts.get(str(a), 0) + 1
            done_files += 1
            if done_files % 25000 == 0:
                print(f"    aic: {done_files:,} files...", flush=True)
    n = 0
    for aid_s, d in agents.items():
        if d.get("is_artist") is False:
            continue
        name = clean_person_name(d.get("title") or "")
        if not name:
            continue
        row_id = authority.upsert_artist(
            conn, name, "aic",
            ulan_id=str(d["ulan_id"]) if d.get("ulan_id") else None,
            birth_year=_year(d.get("birth_date")),
            death_year=_year(d.get("death_date")))
        if row_id:
            authority.add_holding(conn, row_id, "aic", counts.get(aid_s, 1))
            n += 1
    conn.commit()
    _record(conn, "aic", n)


# ---------------------------------------------------------------- ULAN

def import_ulan(conn, rel_dir, force=False):
    """Getty ULAN relational dump: SUBJECT.out (persons), TERM.out (names +
    variants), BIOGRAPHY.out (life dates, sex, nationality-ish text)."""
    if _done(conn, "ulan", force):
        return
    persons: set[str] = set()
    with open(rel_dir / "SUBJECT.out", encoding="utf-8", errors="replace") as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            if len(c) >= 7 and c[3] == "P":
                persons.add(c[6])
    print(f"    ulan: {len(persons):,} person records", flush=True)

    bio: dict[str, tuple] = {}
    with open(rel_dir / "BIOGRAPHY.out", encoding="utf-8", errors="replace") as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            if len(c) < 10:
                continue
            sid = c[9]
            if sid in persons and sid not in bio:
                nat = (re.match(r"([A-Za-z]+)", c[1] or "") or [""])[0]
                bio[sid] = (_year(c[2]), _year(c[5]), nat or None)

    # subject_id -> id in authority db; preferred term creates the artist,
    # non-preferred terms attach as variants
    sid_to_aid: dict[str, int] = {}
    pending_variants: dict[str, list] = {}
    n_terms = 0
    with open(rel_dir / "TERM.out", encoding="utf-8", errors="replace") as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            if len(c) < 11:
                continue
            sid, term, preferred = c[9], c[10], c[2]
            if sid not in persons:
                continue
            name = clean_person_name(term)
            if not name:
                continue
            if preferred == "Y" and sid not in sid_to_aid:
                b = bio.get(sid, (None, None, None))
                aid = authority.upsert_artist(
                    conn, name, "ulan", ulan_id=sid,
                    birth_year=b[0], death_year=b[1], nationality=b[2])
                if aid:
                    sid_to_aid[sid] = aid
                    for v in pending_variants.pop(sid, []):
                        authority.add_variant(conn, v, aid, "ulan")
            elif sid in sid_to_aid:
                authority.add_variant(conn, name, sid_to_aid[sid], "ulan")
            else:
                pending_variants.setdefault(sid, []).append(name)
            n_terms += 1
            if n_terms % 200000 == 0:
                conn.commit()
                print(f"    ulan: {n_terms:,} terms, {len(sid_to_aid):,}"
                      " artists...", flush=True)
    # subjects whose preferred flag never appeared: promote first variant
    for sid, names in pending_variants.items():
        b = bio.get(sid, (None, None, None))
        aid = authority.upsert_artist(conn, names[0], "ulan", ulan_id=sid,
                                      birth_year=b[0], death_year=b[1],
                                      nationality=b[2])
        if aid:
            for v in names[1:]:
                authority.add_variant(conn, v, aid, "ulan")
    conn.commit()
    _record(conn, "ulan", len(sid_to_aid) + len(pending_variants))


# ---------------------------------------------------------------- Smithsonian

SI_BASE = "https://smithsonian-open-access.s3-us-west-2.amazonaws.com/metadata/edan/"
SI_ART_UNITS = ("saam", "npg", "hmsg", "chndm", "nmafa", "acm", "nmaahc")
ARTIST_LABEL = re.compile(
    r"artist|painter|sculptor|printmaker|creator|created by"
    r"|photograph(?:ed)? by|painted by|drawn by|illustrated by|designed by",
    re.I)


def _si_chunks(unit: str):
    idx = urllib.request.urlopen(SI_BASE + unit + "/index.txt", timeout=60)
    for url in idx.read().decode().split():
        if url.strip():
            yield url.strip()


def _si_names(record: dict):
    ft = (record.get("content") or {}).get("freetext") or {}
    for entry in ft.get("name") or []:
        if ARTIST_LABEL.search(entry.get("label") or ""):
            yield entry.get("content") or ""


def import_si_unit(conn, unit: str, aaa=False, force=False):
    source = f"si-{unit}"
    if _done(conn, source, force):
        return
    counts: dict[str, str] = {}
    chunks = list(_si_chunks(unit))
    for i, url in enumerate(chunks):
        try:
            resp = urllib.request.urlopen(url, timeout=300)
            for line in resp:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                for raw in _si_names(rec):
                    name = clean_person_name(raw)
                    if name:
                        counts[name.lower()] = name
        except Exception as e:
            print(f"    {source}: chunk {i} failed ({str(e)[:60]}) — continuing",
                  flush=True)
        if (i + 1) % 10 == 0:
            print(f"    {source}: {i + 1}/{len(chunks)} chunks,"
                  f" {len(counts):,} names...", flush=True)
    n = 0
    for name in counts.values():
        aid = authority.upsert_artist(conn, name, source, aaa_papers=aaa)
        if aid:
            if not aaa:
                authority.add_holding(conn, aid, unit)
            n += 1
    conn.commit()
    _record(conn, source, n)




def import_wikidata_collections(conn, force=False):
    """Wikidata P6379 'has works in the collection of' — EVIDENCE ONLY.

    Guardrails (agreed 2026-07-25): matches by QID/ULAN cross-reference
    only, never by name, and never creates artist rows. Claims land in
    wd_collections, which describe() shows but standing() ignores — the
    crowd-sourced layer can't inflate flags until the trial says so.

    Pass 1 permanently back-fills wikidata_qid for ULAN-bearing rows via
    P245; pass 2 fetches P6379 claims for every QID-bearing row.
    """
    import time
    source = "wikidata-collections"
    if _done(conn, source, force):
        return
    rows = conn.execute(
        "SELECT id, ulan_id FROM artists_authority WHERE"
        " (wikidata_qid IS NULL OR wikidata_qid='') AND ulan_id IS NOT NULL"
        " AND ulan_id != ''").fetchall()
    ulan_to_id = {str(r["ulan_id"]).strip(): r["id"] for r in rows}
    ulans = list(ulan_to_id)
    resolved = 0
    B = 150
    for i in range(0, len(ulans), B):
        vals = " ".join(f'"{u}"' for u in ulans[i:i + B])
        q = "SELECT ?p ?u WHERE { VALUES ?u { %s } ?p wdt:P245 ?u }" % vals
        try:
            for r in _wdqs(q):
                qid = r["p"]["value"].rsplit("/", 1)[-1]
                u = r["u"]["value"]
                if u in ulan_to_id and qid.startswith("Q"):
                    conn.execute(
                        "UPDATE artists_authority SET wikidata_qid=? WHERE"
                        " id=? AND (wikidata_qid IS NULL OR wikidata_qid='')",
                        (qid, ulan_to_id[u]))
                    resolved += 1
        except Exception as e:
            print(f"    {source}: ulan batch {i // B} failed"
                  f" ({str(e)[:60]}) — continuing", flush=True)
        if (i // B) % 25 == 0:
            conn.commit()
            print(f"    {source}: ULAN->QID {min(i + B, len(ulans)):,}/"
                  f"{len(ulans):,}, resolved {resolved:,}...", flush=True)
        time.sleep(0.8)
    conn.commit()
    rows = conn.execute(
        "SELECT id, wikidata_qid q FROM artists_authority WHERE"
        " wikidata_qid IS NOT NULL AND wikidata_qid != ''").fetchall()
    qid_to_id = {r["q"]: r["id"] for r in rows
                 if str(r["q"]).startswith("Q")}
    qids = list(qid_to_id)
    n = 0
    for i in range(0, len(qids), B):
        vals = " ".join(f"wd:{q}" for q in qids[i:i + B])
        q = ("SELECT ?p ?m ?mLabel WHERE { VALUES ?p { %s }"
             " ?p wdt:P6379 ?m . SERVICE wikibase:label"
             ' { bd:serviceParam wikibase:language "en" } }' % vals)
        try:
            for r in _wdqs(q):
                pq = r["p"]["value"].rsplit("/", 1)[-1]
                mq = r["m"]["value"].rsplit("/", 1)[-1]
                label = (r.get("mLabel") or {}).get("value", "")
                if not label or label == mq or pq not in qid_to_id:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO wd_collections (artist_id,"
                    " museum, museum_qid) VALUES (?,?,?)",
                    (qid_to_id[pq], label[:90], mq))
                n += 1
        except Exception as e:
            print(f"    {source}: P6379 batch {i // B} failed"
                  f" ({str(e)[:60]}) — continuing", flush=True)
        if (i // B) % 25 == 0:
            conn.commit()
            print(f"    {source}: collections {min(i + B, len(qids)):,}/"
                  f"{len(qids):,}, {n:,} claims...", flush=True)
        time.sleep(0.8)
    conn.commit()
    _record(conn, source, n, note=f"qids resolved: {resolved}")


# ------------------------------------------------- Getty Provenance Index

GPI_BASE = ("https://jpgt-or-prd-provenance-index-csv.s3.us-west-2"
            ".amazonaws.com/")
GPI_FILES = ([f"sales_catalogs/sales_contents_{i}.csv" for i in range(1, 14)]
             + ["knoedler/knoedler.csv", "goupil/goupil.csv"])
ATTRIB_HEDGE = re.compile(
    r"copy|school|circle|manner|after|imitator|follower|style|attributed",
    re.I)


def import_provenance(conn, force=False):
    """Historic auction/stockbook records (1650-1945): count per artist,
    joined by ULAN id when present, else by authority name. Rows with
    attribution hedges ('school of', 'copy after'...) are not counted."""
    import codecs
    if _done(conn, "getty-pi", force):
        return
    by_ulan: dict[str, int] = {}
    by_name: dict[str, tuple[str, int]] = {}
    for path in GPI_FILES:
        try:
            resp = urllib.request.urlopen(GPI_BASE + path, timeout=600)
            reader = csv.DictReader(codecs.iterdecode(resp, "utf-8",
                                                      errors="replace"))
            cols = [c for c in (reader.fieldnames or [])
                    if re.match(r"art(ist)?_authority_\d+$", c)]
            n_rows = 0
            for row in reader:
                for col in cols:
                    idx = col.rsplit("_", 1)[1]
                    hedge = (row.get(f"attrib_mod_auth_{idx}") or "") + \
                        (row.get(f"attrib_mod_{idx}") or "")
                    if hedge and ATTRIB_HEDGE.search(hedge):
                        continue
                    ulan = (row.get(f"artist_ulan_{idx}") or "").strip()
                    if ulan and re.match(r"\d{9}$", ulan):
                        by_ulan[ulan] = by_ulan.get(ulan, 0) + 1
                        continue
                    name = clean_person_name(row.get(col) or "")
                    if name:
                        k = name.lower()
                        by_name[k] = (name, by_name.get(k, ("", 0))[1] + 1)
                n_rows += 1
            print(f"    getty-pi: {path.split('/')[-1]}: {n_rows:,} rows",
                  flush=True)
        except Exception as e:
            print(f"    getty-pi: {path} failed ({str(e)[:70]}) — continuing",
                  flush=True)
    n = 0
    for ulan, cnt in by_ulan.items():
        row = conn.execute("SELECT id FROM artists_authority WHERE ulan_id=?",
                           (ulan,)).fetchone()
        if row:
            conn.execute(
                "INSERT INTO market_history (artist_id, source, records)"
                " VALUES (?,?,?) ON CONFLICT(artist_id, source)"
                " DO UPDATE SET records=records+excluded.records",
                (row["id"], "getty-pi", cnt))
            n += 1
    for name, cnt in by_name.values():
        aid = authority.upsert_artist(conn, name, "getty-pi")
        if aid:
            conn.execute(
                "INSERT INTO market_history (artist_id, source, records)"
                " VALUES (?,?,?) ON CONFLICT(artist_id, source)"
                " DO UPDATE SET records=records+excluded.records",
                (aid, "getty-pi", cnt))
            n += 1
    conn.commit()
    _record(conn, "getty-pi", n,
            f"ulan-joined={len(by_ulan):,} name-joined={len(by_name):,}")


# ------------------------------------------------- Wikidata distinctions

WDQS = "https://query.wikidata.org/sparql"
WD_UA = "WilliamsArtReferenceLibrary/1.0 (williamsdaniel85@gmail.com)"
VISUAL_OCCS = ("wd:Q1028181 wd:Q1281618 wd:Q33231 wd:Q483501 wd:Q10862983"
               " wd:Q3391743 wd:Q21550489 wd:Q17505902")
_EDITION = "((wdt:P179|wdt:P361|wdt:P31)?)"  # person -> edition -> series
AWARD_SPECS = [
    ("Guggenheim Fellow", "?p wdt:P166 wd:Q1316544 ."),
    ("MacArthur Fellow", "?p wdt:P166 wd:Q1543268 ."),
    ("Venice Biennale", f"?p wdt:P1344/{_EDITION} wd:Q205751 ."),
    ("Whitney Biennial", f"?p wdt:P1344/{_EDITION} wd:Q677294 ."),
]


def _wdqs(query: str) -> list[dict]:
    url = WDQS + "?" + urllib.parse.urlencode(
        {"format": "json", "query": query})
    req = urllib.request.Request(url, headers={"User-Agent": WD_UA})
    data = json.load(urllib.request.urlopen(req, timeout=180))
    return data["results"]["bindings"]


def import_wikidata_awards(conn, force=False):
    if _done(conn, "wikidata-awards", force):
        return
    n = 0
    for label, pattern in AWARD_SPECS:
        q = f"""SELECT DISTINCT ?p ?pLabel ?b ?d WHERE {{
          {pattern}
          ?p wdt:P106 ?occ . VALUES ?occ {{ {VISUAL_OCCS} }}
          OPTIONAL {{ ?p wdt:P569 ?b }} OPTIONAL {{ ?p wdt:P570 ?d }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" }}
        }} LIMIT 30000"""
        try:
            rows = _wdqs(q)
        except Exception as e:
            print(f"    wikidata: {label} query failed ({str(e)[:70]})"
                  " — continuing", flush=True)
            continue
        added = 0
        for r in rows:
            name = r.get("pLabel", {}).get("value", "")
            qid = r.get("p", {}).get("value", "").rsplit("/", 1)[-1]
            if not name or name == qid or JUNK_NAME.search(name):
                continue  # unlabeled items come back as bare QIDs
            aid = authority.upsert_artist(
                conn, name, "wikidata", wikidata_qid=qid,
                birth_year=_year(r.get("b", {}).get("value")),
                death_year=_year(r.get("d", {}).get("value")))
            if aid:
                conn.execute(
                    "INSERT OR IGNORE INTO distinctions (artist_id,"
                    " distinction) VALUES (?,?)", (aid, label))
                added += 1
        conn.commit()
        print(f"    wikidata: {label}: {added:,} artists", flush=True)
        n += added
    _record(conn, "wikidata-awards", n)


# ---------------------------------------------------------------- driver

def run_imports(raw_dir, sources=None, force=False):
    conn = authority.connect()
    have = set(sources or ["all"])
    every = "all" in have

    def want(s):
        return every or s in have

    if want("moma"):
        import_moma(conn, raw_dir / "moma_artists.csv", force)
    if want("whitney"):
        import_whitney(conn, raw_dir / "whitney_artists.csv",
                       raw_dir / "whitney_artworks.csv", force)
    if want("nga"):
        import_nga(conn, raw_dir / "nga_constituents.csv",
                   raw_dir / "nga_objcon.csv", force)
    if want("cleveland"):
        import_cleveland(conn, raw_dir / "cleveland.csv", force)
    if want("met"):
        import_met(conn, raw_dir / "met.csv", force)
    if want("aic"):
        import_aic(conn, raw_dir / "artic.tar.bz2", force)
    if want("ulan"):
        import_ulan(conn, raw_dir, force)
    if want("si"):
        for unit in SI_ART_UNITS:
            import_si_unit(conn, unit, force=force)
        import_si_unit(conn, "aaa", aaa=True, force=force)
    if want("gpi"):
        import_provenance(conn, force)
    if want("awards"):
        import_wikidata_awards(conn, force)
    if want("collections"):
        import_wikidata_collections(conn, force)
    print("authority status:", authority.status(conn))
    conn.close()


if __name__ == "__main__":
    from pathlib import Path
    raw = Path(sys.argv[1])
    srcs = sys.argv[2].split(",") if len(sys.argv) > 2 else None
    run_imports(raw, srcs, force="--force" in sys.argv)
