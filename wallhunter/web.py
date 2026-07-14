"""Local review queue: ranked cards, keyboard triage, events into the Ledger.

Keys: j/k move · s save · d dismiss · r dismiss with reason · z undo · Enter detail
"""

import html
import json
import re
import sqlite3

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import db
from .config import HIDE_CATEGORIES
from .images import path_for

app = FastAPI(title="Wall Hunter")
_HASH = re.compile(r"^[0-9a-f]{64}$")

CSS = """
body{font-family:-apple-system,Helvetica,Arial,sans-serif;background:#f5f5f4;color:#1c1917;
     max-width:1080px;margin:0 auto;padding:24px}
h1{font-size:20px} .meta{color:#57534e;font-size:13px;margin-bottom:14px}
.card{background:#fff;border:2px solid #e7e5e4;border-radius:8px;padding:14px;margin:12px 0;
      display:flex;gap:16px;transition:opacity .25s}
.card.sel{border-color:#2563eb;box-shadow:0 0 0 3px #bfdbfe}
.card.gone{opacity:0;pointer-events:none}
.card img.crop{width:250px;max-height:250px;object-fit:contain;background:#fafaf9;
               border:1px solid #e7e5e4;border-radius:4px;flex-shrink:0;cursor:pointer}
.tier{display:inline-block;color:#fff;font-weight:700;border-radius:10px;padding:2px 10px;
      font-size:13px;margin-right:6px}
.cat{display:inline-block;background:#eef2ff;border:1px solid #c7d2fe;border-radius:10px;
     padding:1px 8px;font-size:12px;margin-right:6px;color:#3730a3}
.flag{display:inline-block;background:#f5f5f4;border:1px solid #d6d3d1;border-radius:10px;
      padding:1px 8px;font-size:12px;margin:0 4px 4px 0;color:#44403c}
.flag.hot{background:#fef3c7;border-color:#f59e0b}
.field{font-size:13px;margin:3px 0} .field b{color:#57534e}
.sig{background:#ecfdf5;border:1px solid #6ee7b7;border-radius:4px;padding:6px 8px;
     font-size:13px;margin:6px 0}
.unc{color:#78716c;font-size:12px;font-style:italic}
.btns{margin-top:8px} .btns button{margin-right:6px;padding:4px 12px;border-radius:6px;
      border:1px solid #d6d3d1;background:#fff;cursor:pointer;font-size:13px}
.btns button:hover{background:#f5f5f4}
.keybar{position:sticky;top:0;background:#1c1917;color:#e7e5e4;border-radius:8px;
        padding:8px 14px;font-size:12.5px;z-index:5}
.keybar b{color:#fbbf24}
#picker{position:fixed;inset:0;background:rgba(0,0,0,.4);display:none;z-index:10}
#picker .box{background:#fff;max-width:340px;margin:18vh auto;border-radius:10px;padding:18px}
#picker button{display:block;width:100%;margin:6px 0;padding:8px;border-radius:6px;
               border:1px solid #d6d3d1;background:#fafaf9;cursor:pointer;font-size:14px}
a{color:#1d4ed8}
"""

TIER_COLORS = {"A": "#b91c1c", "B": "#b45309", "C": "#6b7280"}


def _conn():
    return db.connect()


def _flags(w) -> str:
    out = []
    if w["sig_visible"]:
        out.append('<span class="flag hot">&#9997; signature</span>')
    if w["label_visible"]:
        out.append('<span class="flag hot">&#127991; label</span>')
    if w["repro_suspect"]:
        out.append('<span class="flag">&#9888; repro?</span>')
    if w["background_only"]:
        out.append('<span class="flag hot">&#128064; uncatalogued</span>')
    return "".join(out)


def _card(w, sale_title: str, ctx: float = 0.0, taste: float = 0.0) -> str:
    e = lambda s: html.escape(str(s or ""))
    color = TIER_COLORS.get(w["tier"] or "C", "#6b7280")
    boost_chips = ""
    if ctx >= 0.4:
        boost_chips += f'<span class="flag hot">&#127968; collector context +{ctx:.1f}</span>'
    if abs(taste) >= 0.15:
        boost_chips += (f'<span class="flag">taste {"+" if taste > 0 else ""}{taste:.1f}</span>')
    sig = (f'<div class="sig"><b>Sig/label text:</b> {e(w["sig_text"])}</div>'
           if w["sig_text"] else "")
    return f"""
<div class="card" id="w{w['id']}" data-id="{w['id']}">
  <img class="crop" src="/img/{e(w['crop_hash'])}" loading="lazy"
       onclick="location='/work/{w['id']}'">
  <div style="flex:1">
    <div><span class="tier" style="background:{color}">{e(w['tier'])} &middot; {w['interest_score']:.1f}</span>
      <span class="cat">{e((w['category'] or 'other').replace('_',' '))}</span>{_flags(w)}{boost_chips}</div>
    <div class="field" style="font-size:15px;margin-top:5px">{e(w['subject'])}</div>
    <div class="field"><b>Medium:</b> {e(w['medium_guess'])} &middot; <b>Period:</b> {e(w['period_guess'])}</div>
    <div class="field"><b>Quality:</b> {e((w['quality_notes'] or '')[:220])}</div>
    {sig}
    <div class="field unc">{e(sale_title)}</div>
    <div class="btns">
      <button onclick="act({w['id']},'save')">&#11088; Save (s)</button>
      <button onclick="act({w['id']},'dismiss')">&#10060; Dismiss (d)</button>
      <button onclick="pick({w['id']})">Dismiss w/ reason (r)</button>
      <button onclick="location='/work/{w['id']}'">Detail (&#9166;)</button>
    </div>
  </div>
</div>"""


JS = """<script>
let sel = -1, last = null;
const cards = () => [...document.querySelectorAll('.card:not(.gone)')];
function select(i){
  const cs = cards(); if(!cs.length) return;
  sel = Math.max(0, Math.min(i, cs.length-1));
  cs.forEach(c=>c.classList.remove('sel'));
  cs[sel].classList.add('sel');
  cs[sel].scrollIntoView({block:'center', behavior:'smooth'});
}
async function act(id, kind, reason){
  const r = await fetch(`/api/works/${id}/action`,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({kind, reason: reason||null})});
  if(!r.ok){alert('action failed'); return}
  last = {id, kind};
  const el = document.getElementById('w'+id);
  if(el){el.classList.add('gone'); setTimeout(()=>el.remove(), 260); }
  document.getElementById('picker').style.display='none';
  setTimeout(()=>select(sel), 280);
  updateCount(-1);
}
async function undo(){
  if(!last) return;
  await fetch(`/api/works/${last.id}/action`,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({kind:'requeue'})});
  location.reload();
}
let pickId = null;
function pick(id){ pickId = id; document.getElementById('picker').style.display='block'; }
function pickReason(reason){
  if(reason==='other'){ reason = prompt('Reason?') || 'other'; }
  act(pickId, 'dismiss', reason);
}
function updateCount(d){
  const n = document.getElementById('count');
  n.textContent = Math.max(0, parseInt(n.textContent)+d);
}
document.addEventListener('keydown', e=>{
  if(e.target.tagName==='INPUT'||e.target.tagName==='TEXTAREA') return;
  if(document.getElementById('picker').style.display==='block' && e.key==='Escape'){
    document.getElementById('picker').style.display='none'; return;
  }
  const cs = cards();
  if(e.key==='j') select(sel+1);
  else if(e.key==='k') select(sel-1);
  else if(e.key==='s' && sel>=0 && cs[sel]) act(cs[sel].dataset.id,'save');
  else if(e.key==='d' && sel>=0 && cs[sel]) act(cs[sel].dataset.id,'dismiss');
  else if(e.key==='r' && sel>=0 && cs[sel]) pick(cs[sel].dataset.id);
  else if(e.key==='z') undo();
  else if(e.key==='Enter' && sel>=0 && cs[sel]) location='/work/'+cs[sel].dataset.id;
});
select(0);
</script>
<div id="picker"><div class="box"><b>Dismiss reason</b>
  <button onclick="pickReason('print/repro')">Print / reproduction</button>
  <button onclick="pickReason('amateur')">Amateur work</button>
  <button onclick="pickReason('not my area')">Not my area</button>
  <button onclick="pickReason('condition')">Condition</button>
  <button onclick="pickReason('other')">Other&hellip;</button>
</div></div>"""


@app.get("/", response_class=HTMLResponse)
def queue(sale: int | None = None, all: int = 0):
    conn = _conn()
    try:
        q = ("SELECT w.*, d.crop_hash, s.title AS sale_title,"
             " COALESCE(s.context_score, 0) AS context_score FROM works w"
             " JOIN detections d ON d.id=w.best_detection_id"
             " JOIN sales s ON s.id=w.sale_id WHERE w.status='screened'")
        params: list = []
        if sale:
            q += " AND w.sale_id=?"
            params.append(sale)
        if not all:
            ph = ",".join("?" * len(HIDE_CATEGORIES))
            q += f" AND COALESCE(lower(w.category),'other') NOT IN ({ph})"
            params.extend(HIDE_CATEGORIES)
        rows = conn.execute(q, params).fetchall()
        # ordering = model score + collector-context boost + learned taste prior;
        # boosts reorder attention but never alter the stored interest_score
        from .taste import category_boosts
        boosts = category_boosts(conn)
        works = sorted(
            rows,
            key=lambda w: (w["interest_score"] or 0)
            + (w["context_score"] or 0)
            + boosts.get((w["category"] or "other").lower(), 0.0),
            reverse=True)
        saved = conn.execute("SELECT COUNT(*) n FROM works WHERE status='saved'").fetchone()["n"]
        cards = "".join(
            _card(w, w["sale_title"] or "", ctx=w["context_score"] or 0,
                  taste=boosts.get((w["category"] or "other").lower(), 0.0))
            for w in works)
        other = ("show hidden categories" if not all else "hide non-collecting categories")
        other_url = f"/?all={0 if all else 1}" + (f"&sale={sale}" if sale else "")
        return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Wall Hunter — queue</title><style>{CSS}</style></head><body>
<h1>&#128269; Wall Hunter review queue</h1>
<div class="meta"><span id="count">{len(works)}</span> queued &middot; {saved} saved &middot;
  <a href="{other_url}">{other}</a></div>
<div class="keybar"><b>j/k</b> move &middot; <b>s</b> save &middot; <b>d</b> dismiss &middot;
  <b>r</b> dismiss w/ reason &middot; <b>z</b> undo &middot; <b>&#9166;</b> detail</div>
{cards if works else '<p style="margin-top:30px;color:#78716c">Queue is empty. &#127881;</p>'}
{JS}</body></html>"""
    finally:
        conn.close()


@app.get("/work/{work_id}", response_class=HTMLResponse)
def work_detail(work_id: int):
    conn = _conn()
    try:
        w = conn.execute(
            "SELECT w.*, s.title AS sale_title, s.url AS sale_url FROM works w"
            " JOIN sales s ON s.id=w.sale_id WHERE w.id=?", (work_id,)).fetchone()
        if not w:
            raise HTTPException(404)
        views = conn.execute(
            "SELECT d.*, p.file_hash AS photo_hash, p.source_url FROM work_detections wd"
            " JOIN detections d ON d.id=wd.detection_id"
            " JOIN photos p ON p.id=d.photo_id WHERE wd.work_id=?"
            " ORDER BY d.crop_area DESC", (work_id,)).fetchall()
        e = lambda s: html.escape(str(s or ""))
        try:
            unc = ", ".join(json.loads(w["uncertainties"] or "[]"))
        except json.JSONDecodeError:
            unc = w["uncertainties"] or ""
        view_html = "".join(
            f'''<div style="display:inline-block;margin:8px;vertical-align:top">
              <img src="/img/{e(v['crop_hash'])}" style="max-width:420px;max-height:420px;
                   border:1px solid #d6d3d1;border-radius:4px"><br>
              <span class="unc">{e(v['description'][:90])}</span><br>
              <a href="/img/{e(v['photo_hash'])}" target="_blank">source photo</a>
              {f' &middot; <a href="{e(v["source_url"])}" target="_blank">original</a>'
               if (v['source_url'] or '').startswith('http') else ''}</div>'''
            for v in views)
        sig = (f'<div class="sig"><b>Sig/label text:</b> {e(w["sig_text"])}</div>'
               if w["sig_text"] else "")
        return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Work #{work_id}</title><style>{CSS}</style></head><body>
<p><a href="/">&larr; back to queue</a></p>
<h1>Work #{work_id} — {e(w['tier'])} &middot; {w['interest_score']:.1f}
  <span class="cat">{e((w['category'] or 'other').replace('_',' '))}</span></h1>
<div class="meta">{e(w['sale_title'])}
  {f'&middot; <a href="{e(w["sale_url"])}" target="_blank">sale page</a>' if w['sale_url'] else ''}</div>
<div class="field" style="font-size:15px">{e(w['subject'])}</div>
<div class="field"><b>Medium:</b> {e(w['medium_guess'])} <span class="unc">({e(w['medium_basis'])})</span></div>
<div class="field"><b>Period:</b> {e(w['period_guess'])} <span class="unc">({e(w['period_basis'])})</span></div>
<div class="field"><b>Quality:</b> {e(w['quality_notes'])}</div>
{sig}
{f'<div class="field"><b>Context:</b> {e(w["background_context"])}</div>' if w['background_context'] else ''}
{f'<div class="unc">Uncertain: {e(unc)}</div>' if unc else ''}
<div class="btns" style="margin:10px 0">
  <button onclick="act({w['id']},'save')">&#11088; Save</button>
  <button onclick="act({w['id']},'dismiss')">&#10060; Dismiss</button>
</div>
<h3>All views ({len(views)})</h3>{view_html}
<script>
async function act(id, kind){{
  await fetch(`/api/works/${{id}}/action`,{{method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{kind}})}});
  location='/';
}}
</script></body></html>"""
    finally:
        conn.close()


@app.post("/api/works/{work_id}/action")
async def work_action(work_id: int, request: Request):
    body = await request.json()
    kind = body.get("kind")
    reason = body.get("reason")
    status_map = {"save": "saved", "dismiss": "dismissed",
                  "promote": "promoted", "requeue": "screened"}
    if kind not in status_map:
        raise HTTPException(400, f"unknown kind {kind!r}")
    conn = _conn()
    try:
        row = conn.execute("SELECT status FROM works WHERE id=?", (work_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        conn.execute("UPDATE works SET status=? WHERE id=?", (status_map[kind], work_id))
        conn.execute(
            "INSERT INTO events (ts, tool, work_id, kind, reason, payload_json)"
            " VALUES (?,?,?,?,?,?)",
            (db.now(), "wall-hunter", work_id, kind, reason,
             json.dumps({"from_status": row["status"]})))
        conn.commit()
        return JSONResponse({"ok": True, "work_id": work_id, "status": status_map[kind]})
    finally:
        conn.close()


@app.get("/img/{file_hash}")
def image(file_hash: str):
    if not _HASH.match(file_hash):
        raise HTTPException(400)
    path = path_for(file_hash)
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})
