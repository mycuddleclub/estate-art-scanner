"""Ranked HTML review report for a sale (self-contained, crops embedded)."""

import html
import json

from . import db
from .config import HIDE_CATEGORIES, REPORT_DIR
from .images import downscale_jpeg_b64, load

TIER_COLORS = {"A": "#b91c1c", "B": "#b45309", "C": "#6b7280"}

CSS = """
body{font-family:-apple-system,Helvetica,Arial,sans-serif;background:#f5f5f4;color:#1c1917;
     max-width:1080px;margin:0 auto;padding:24px}
h1{font-size:22px} .meta{color:#57534e;font-size:14px;margin-bottom:20px}
.card{background:#fff;border:1px solid #e7e5e4;border-radius:8px;padding:16px;margin:14px 0;
      display:flex;gap:16px}
.card img.crop{width:280px;max-height:280px;object-fit:contain;background:#fafaf9;
               border:1px solid #e7e5e4;border-radius:4px;flex-shrink:0}
.tier{display:inline-block;color:#fff;font-weight:700;border-radius:10px;padding:2px 10px;
      font-size:13px;margin-right:8px}
.flag{display:inline-block;background:#f5f5f4;border:1px solid #d6d3d1;border-radius:10px;
      padding:1px 8px;font-size:12px;margin:0 4px 4px 0;color:#44403c}
.flag.hot{background:#fef3c7;border-color:#f59e0b}
.field{font-size:13px;margin:3px 0} .field b{color:#57534e}
.sig{background:#ecfdf5;border:1px solid #6ee7b7;border-radius:4px;padding:6px 8px;
     font-size:13px;margin:6px 0}
.unc{color:#78716c;font-size:12px;font-style:italic}
.cov{background:#fff;border:1px solid #e7e5e4;border-radius:8px;padding:12px 16px;font-size:13px}
a{color:#1d4ed8}
.cat{display:inline-block;background:#eef2ff;border:1px solid #c7d2fe;border-radius:10px;
     padding:1px 8px;font-size:12px;margin-right:6px;color:#3730a3}
.card.hidden-cat{display:none}
body.show-hidden .card.hidden-cat{display:flex;opacity:.75}
.togglebar{background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;padding:10px 16px;
           font-size:13px;margin-top:10px}
.togglebar button{font-size:13px;padding:3px 12px;border-radius:6px;border:1px solid #d6d3d1;
                  background:#fff;cursor:pointer}
"""

TOGGLE_JS = """<script>
function toggleHidden(btn){
  document.body.classList.toggle('show-hidden');
  btn.textContent = document.body.classList.contains('show-hidden') ? 'Hide again' : 'Show them';
}
</script>"""


def _flags_html(w) -> str:
    out = []
    if w["sig_visible"]:
        out.append('<span class="flag hot">&#9997; signature visible</span>')
    if w["label_visible"]:
        out.append('<span class="flag hot">&#127991; label visible</span>')
    if w["verso_visible"]:
        out.append('<span class="flag">verso visible</span>')
    if w["repro_suspect"]:
        out.append('<span class="flag">&#9888; repro suspect</span>')
    if w["background_only"]:
        out.append('<span class="flag hot">&#128064; background/uncatalogued</span>')
    return "".join(out)


def build_report(conn, sale_id: int, open_after: bool = True) -> str:
    sale = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
    if not sale:
        raise SystemExit(f"unknown sale {sale_id}")
    works = conn.execute(
        "SELECT w.*, d.crop_hash, d.description AS det_desc, p.file_hash AS photo_hash,"
        "       p.source_url"
        " FROM works w JOIN detections d ON d.id=w.best_detection_id"
        " JOIN photos p ON p.id=d.photo_id"
        " WHERE w.sale_id=? AND w.status IN ('screened','saved','promoted')"
        " ORDER BY w.interest_score DESC",
        (sale_id,)).fetchall()
    cov = conn.execute(
        "SELECT stage1_status, COUNT(*) n FROM photos WHERE sale_id=? GROUP BY stage1_status",
        (sale_id,)).fetchall()
    cost = conn.execute(
        "SELECT COALESCE(SUM(stage1_cost_usd),0) c FROM photos WHERE sale_id=?",
        (sale_id,)).fetchone()["c"]
    cost += conn.execute(
        "SELECT COALESCE(SUM(stage2_cost_usd),0) c FROM works WHERE sale_id=?",
        (sale_id,)).fetchone()["c"]

    tiers = {"A": 0, "B": 0, "C": 0}
    for w in works:
        tiers[w["tier"] or "C"] = tiers.get(w["tier"] or "C", 0) + 1

    cards = []
    hidden_count = 0
    for w in works:
        category = (w["category"] or "other").lower()
        is_hidden = category in HIDE_CATEGORIES
        if is_hidden:
            hidden_count += 1
        try:
            crop_b64 = downscale_jpeg_b64(
                load(w["crop_hash"]), 360 if is_hidden else 560,
                quality=65 if is_hidden else 78)
        except Exception:
            crop_b64 = ""
        color = TIER_COLORS.get(w["tier"] or "C", "#6b7280")
        e = lambda s: html.escape(str(s or ""))
        try:
            unc = ", ".join(json.loads(w["uncertainties"] or "[]"))
        except json.JSONDecodeError:
            unc = (w["uncertainties"] or "").strip("[]\"")
        sig_html = (f'<div class="sig"><b>Signature/label text:</b> {e(w["sig_text"])}</div>'
                    if w["sig_text"] else "")
        views = conn.execute(
            "SELECT COUNT(*) n FROM work_detections WHERE work_id=?", (w["id"],)).fetchone()["n"]
        cards.append(f"""
<div class="card{' hidden-cat' if is_hidden else ''}" data-category="{e(category)}">
  <img class="crop" src="data:image/jpeg;base64,{crop_b64}" loading="lazy">
  <div>
    <div><span class="tier" style="background:{color}">{e(w['tier'])} &middot; {w['interest_score']:.1f}</span>
         <span class="cat">{e(category.replace('_', ' '))}</span>
         {_flags_html(w)}</div>
    <div class="field" style="font-size:15px;margin-top:6px"><b></b>{e(w['subject'])}</div>
    <div class="field"><b>Medium:</b> {e(w['medium_guess'])} <span class="unc">({e(w['medium_basis'])})</span></div>
    <div class="field"><b>Period:</b> {e(w['period_guess'])} <span class="unc">({e(w['period_basis'])})</span></div>
    <div class="field"><b>Quality:</b> {e(w['quality_notes'])}</div>
    {sig_html}
    {f'<div class="field"><b>Context:</b> {e(w["background_context"])}</div>' if w['background_context'] else ''}
    {f'<div class="unc">Uncertain: {e(unc)}</div>' if unc else ''}
    <div class="field" style="margin-top:6px"><b>Views:</b> {views} &middot;
      <a href="{e(w['source_url'])}" target="_blank">source photo</a> &middot; work #{w['id']}</div>
  </div>
</div>""")

    cov_html = " &middot; ".join(f"{r['stage1_status']}: {r['n']}" for r in cov)
    title = html.escape(sale["title"] or str(sale_id))
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Wall Hunter — {title}</title><style>{CSS}</style></head><body>
<h1>&#128269; {title}</h1>
<div class="meta">{html.escape(sale['location'] or '')} &middot;
  {html.escape((sale['starts_at'] or '?')[:10])} &rarr; {html.escape((sale['ends_at'] or '?')[:10])} &middot;
  {f'<a href="{html.escape(sale["url"])}" target="_blank">sale page</a>' if sale['url'] else ''}</div>
<div class="cov"><b>{len(works) - hidden_count} works shown</b> of {len(works)}
  (A: {tiers.get('A',0)} &middot; B: {tiers.get('B',0)} &middot; C: {tiers.get('C',0)})
  &middot; photos — {cov_html} &middot; run cost ${cost:.2f}</div>
{f'''<div class="togglebar">{hidden_count} works in categories you don&#39;t collect
  ({html.escape(", ".join(sorted(HIDE_CATEGORIES)))}) are hidden.
  <button onclick="toggleHidden(this)">Show them</button></div>''' if hidden_count else ''}
{''.join(cards)}
{TOGGLE_JS}
</body></html>"""

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"sale_{sale_id}.html"
    out.write_text(doc)
    print(f"report: {out}")
    if open_after:
        import subprocess
        subprocess.run(["open", str(out)], check=False)
    return str(out)
