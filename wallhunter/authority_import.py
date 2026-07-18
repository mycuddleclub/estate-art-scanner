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
import urllib.request
from datetime import datetime, timezone

from . import authority

csv.field_size_limit(10_000_000)

JUNK_NAME = re.compile(
    r"unidentified|unknown|anonymous|^after |, after$|copy after|"
    r"manufactor|manufactur| & co| co\.|company|corporation| inc\b|"
    r" ltd\b|publisher|printing house", re.I)


def clean_person_name(raw: str) -> str | None:
    """'Walker, William Aiken, 1838-1921 (painter)' -> 'William Aiken Walker'."""
    s = re.sub(r"\(.*?\)", " ", raw or "")
    s = re.split(r",\s*(?:b\.|d\.|ca\.|circa|active|fl\.|\d)", s)[0]
    s = re.sub(r"\s+", " ", s).strip(" ,;")
    if not s or JUNK_NAME.search(s):
        return None
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) >= 2:  # 'Last, First[, Suffix]' -> 'First Last Suffix'
        s = " ".join([parts[1], parts[0]] + parts[2:])
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
SI_ART_UNITS = ("saam", "npg", "hmsg", "chndm", "nmafa", "acm")
ARTIST_LABEL = re.compile(r"artist|painter|sculptor|printmaker|creator", re.I)


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
    print("authority status:", authority.status(conn))
    conn.close()


if __name__ == "__main__":
    from pathlib import Path
    raw = Path(sys.argv[1])
    srcs = sys.argv[2].split(",") if len(sys.argv) > 2 else None
    run_imports(raw, srcs, force="--force" in sys.argv)
