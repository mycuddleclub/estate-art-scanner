"""Morning digest email via Zoho SMTP (reuses art-scout's SMTP_* env vars)."""

import html
import os
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

from .images import downscale_jpeg_b64, load

_SMTP_FALLBACKS = [
    Path(__file__).resolve().parent.parent / ".env",
    Path.home() / "art-scout/.env",
]


def _smtp_config():
    for p in _SMTP_FALLBACKS:
        try:
            if p.exists():
                load_dotenv(p, override=False)
        except OSError:  # TCC-protected path under launchd
            continue
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASSWORD")
    to = os.environ.get("EMAIL_TO") or user
    return user, pw, to


def _work_row(w) -> str:
    e = lambda s: html.escape(str(s or ""))
    try:
        thumb = downscale_jpeg_b64(load(w["crop_hash"]), 220, quality=70)
    except Exception:
        thumb = ""
    color = {"A": "#b91c1c", "B": "#b45309"}.get(w["tier"], "#6b7280")
    flags = "".join([
        " &#9997;sig" if w["sig_visible"] else "",
        " &#127991;label" if w["label_visible"] else "",
        " &#128064;uncatalogued" if w["background_only"] else "",
    ])
    sig = (f"<br><span style='color:#047857;font-size:12px'>&ldquo;{e(w['sig_text'])}&rdquo;</span>"
           if w["sig_text"] else "")
    return f"""<tr>
<td style="padding:8px;vertical-align:top">
  <img src="data:image/jpeg;base64,{thumb}" style="width:180px;border-radius:4px"></td>
<td style="padding:8px;vertical-align:top;font-family:Arial;font-size:13px">
  <span style="background:{color};color:#fff;border-radius:9px;padding:1px 9px;font-weight:bold">
  {e(w['tier'])} {w['interest_score']:.1f}</span> <b>{e((w['category'] or '').replace('_',' '))}</b>{flags}<br>
  {e(w['subject'])}<br>
  <span style="color:#57534e">{e(w['medium_guess'])} &middot; {e(w['period_guess'])}</span>
  {sig}</td></tr>"""


def send_exclusives_email(exclusives: list[dict] | None,
                          deep_flags: list[dict] | None = None,
                          deep_stats: dict | None = None,
                          favorites: list[dict] | None = None) -> bool:
    """exclusives=None -> flags-only email (no calendar section)."""
    """Standalone Off-Radar Auctions email (separate program from the
    estate-sale digest, per Daniel's request)."""
    user, pw, to = _smtp_config()
    if not all([user, pw, to]):
        print("exclusives email: SMTP not configured — skipping")
        return False
    e = lambda s: html.escape(str(s or ""))
    deep_html = ""
    if favorites:
        rows = "".join(
            f"<li style='margin:5px 0'><b>{e(a['house'])}</b> — "
            f"<a href='{e(a['url'])}'>{e(a['title'])}</a>"
            f" <span style='color:#78716c'>{e(a.get('info', ''))}</span></li>"
            for a in favorites)
        deep_html += (
            f"<div style='background:#fdf4ff;border:2px solid #a855f7;"
            f"border-radius:8px;padding:10px 14px;margin:0 0 8px'>"
            f"&#11088; <b>FAVORITE HOUSES have {len(favorites)} auction(s)"
            f" in the window:</b><ul style='margin:6px 0 0;padding-left:18px'>"
            f"{rows}</ul></div>")
    if deep_stats and deep_stats.get("capped"):
        deep_html += (
            f"<div style='background:#fffbeb;border:2px solid #f59e0b;"
            f"border-radius:8px;padding:10px 14px;margin:0 0 8px;"
            f"font-weight:bold'>&#9888;&#65039; RESEARCH BUDGET CAP HIT"
            f" (${deep_stats.get('spend', 0):.2f} spent) —"
            f" {deep_stats.get('names_deferred', 0)} artist names deferred"
            f" to the next run. Unusually high spend may mean an unusually"
            f" rich day or a malfunction — glance at the numbers below.</div>")
    if deep_stats:
        # always show the tally, so "checked, nothing found" is visibly
        # different from "didn't check"
        deep_html += (
            f"<p style='background:#f0fdf4;border:1px solid #86efac;"
            f"border-radius:8px;padding:8px 14px'>&#127919; <b>Deep scan:</b>"
            f" {deep_stats.get('auctions', 0)} auctions &middot;"
            f" {deep_stats.get('lots', 0)} art lots checked &middot;"
            f" {deep_stats.get('researched', 0)} new artists researched"
            f" (${deep_stats.get('spend', 0):.2f}) &middot;"
            f" <b>{len(deep_flags or [])} flags</b></p>")
    if deep_flags:
        items = "".join(
            f"""<div style="background:#fef2f2;border:2px solid #dc2626;border-radius:8px;
                 padding:10px 14px;margin:8px 0">
              <b><a href='{e(f['url'])}'>{e(f['title'])}</a></b><br>
              {e(f['house'])} &middot; bid: {('$%.0f' % f['high_bid_usd']) if f.get('high_bid_usd') else 'none'}
              {f"&middot; {e(f['estimate'])}" if f.get('estimate') else ''}<br>
              <b>{e(f['artist'])}</b> — {e(f['reason'])}<br>
              <span style="color:#57534e;font-size:12px">{e(f['market_note'])}
              {e(f['evidence'])}</span></div>"""
            for f in deep_flags)  # all of them — Daniel: never truncate finds
        deep_html += (f"<h3 style='margin:14px 0 4px'>&#127919; Deep finds"
                      f" ({len(deep_flags)})</h3>" + items)
    if exclusives is None:
        body_core = ""  # flags-only email
    elif exclusives:
        # full list per Daniel — grouped by end date to stay scannable
        sections = []
        current_day = object()
        for a in exclusives:  # pre-sorted soonest-ending first
            day = (a.get("ends") or "")[:10] or "no date"
            if day != current_day:
                if sections:
                    sections.append("</ul>")
                label = day if day == "no date" else day[5:].replace("-", "/")
                sections.append(
                    f"<h4 style='margin:12px 0 2px;color:#44403c'>Ends"
                    f" {label}</h4><ul style='padding-left:18px;margin:4px 0'>")
                current_day = day
            sections.append(
                f"<li style='margin:4px 0'><b>{e(a['house'])}</b> — "
                f"<a href='{e(a['url'])}'>{e(a['title'])}</a>"
                f" <span style='color:#78716c'>[{e(a['platform'])}]</span></li>")
        sections.append("</ul>")
        body_core = "".join(sections)
    else:
        body_core = "<p>No off-radar auctions found today.</p>"
    body = f"""<html><body style="background:#f5f5f4;padding:16px">
<div style="max-width:720px;margin:0 auto;background:#fff;border-radius:10px;
     padding:22px;font-family:Arial;font-size:13px">
<h2 style="margin:0">&#128373; Off-Radar Auctions</h2>
<p style="color:#57534e">Auctions on HiBid / Bidsquare whose houses are NOT
currently active on LiveAuctioneers or Invaluable — smaller bidder pools.
Junk genres and your blocked houses are filtered out.</p>
{deep_html}
{body_core}
</div></body></html>"""
    msg = MIMEText(body, "html")
    if exclusives is None:
        msg_subject = (f"\U0001F3AF Off-Radar Deep — {len(deep_flags or [])}"
                       f" flagged lots")
    else:
        msg_subject = f"\U0001F575 Off-Radar Auctions — {len(exclusives)} today"
    msg["Subject"] = msg_subject
    msg["From"] = user
    msg["To"] = to
    try:
        with smtplib.SMTP("smtp.zoho.com", 587, timeout=30) as server:
            server.starttls()
            server.login(user, pw)
            server.send_message(msg)
        print(f"exclusives email sent to {to}: {msg_subject}")
        return True
    except Exception as ex:
        print(f"exclusives email failed: {ex}")
        return False


def send_digest(conn, sale_ids: list[int], cost: float,
                cap_events: list[str] | None = None) -> bool:
    user, pw, to = _smtp_config()
    if not all([user, pw, to]):
        print("digest: SMTP not configured (SMTP_USER/SMTP_PASSWORD/EMAIL_TO) — skipping email")
        return False
    if not sale_ids:
        return False
    ph = ",".join("?" * len(sale_ids))
    works = conn.execute(
        f"SELECT w.*, d.crop_hash, s.title AS sale_title FROM works w"
        f" JOIN detections d ON d.id=w.best_detection_id"
        f" JOIN sales s ON s.id=w.sale_id"
        f" WHERE w.sale_id IN ({ph}) AND w.status='screened' AND w.tier IN ('A','B')"
        f" ORDER BY w.interest_score DESC LIMIT 14", sale_ids).fetchall()
    a_count = sum(1 for w in works if w["tier"] == "A")

    sales = conn.execute(
        f"SELECT id, title, location, ends_at, url, context_score, context_note,"
        f" identity_name, identity_verdict, identity_evidence,"
        f" (SELECT COUNT(*) FROM works WHERE sale_id=sales.id AND status='screened') n"
        f" FROM sales WHERE id IN ({ph})", sale_ids).fetchall()
    e = lambda s: html.escape(str(s or ""))
    from .dossier import NOTABLE
    banners = "".join(
        f"""<div style="background:#fef2f2;border:2px solid #dc2626;border-radius:8px;
             padding:12px 16px;margin:10px 0;font-family:Arial;font-size:14px">
        &#128293; <b>NAMED COLLECTION:</b> &ldquo;{e(s['identity_name'])}&rdquo;
        ({e(s['identity_verdict'])}) — {e(s['identity_evidence'])}<br>
        <a href="{e(s['url'])}">{e(s['title'])}</a>, ends {e((s['ends_at'] or '?')[:10])}</div>"""
        for s in sales if (s["identity_verdict"] or "") in NOTABLE)
    sale_lines = "".join(
        f"<li><a href='{e(s['url'])}'>{e(s['title'])}</a> — {e(s['location'])},"
        f" ends {e((s['ends_at'] or '?')[:10])} &middot; {s['n']} works"
        f" &middot; context {s['context_score'] or 0:.1f}"
        f" <i style='color:#78716c'>{e(s['context_note'] or '')}</i></li>"
        for s in sales)
    rows = "".join(_work_row(w) for w in works) or \
        "<tr><td style='font-family:Arial;padding:10px'>No A/B-tier works today.</td></tr>"

    body = f"""<html><body style="background:#f5f5f4;padding:16px">
<div style="max-width:720px;margin:0 auto;background:#fff;border-radius:10px;padding:22px">
{"".join(f'''<div style="background:#fffbeb;border:2px solid #f59e0b;border-radius:8px;
  padding:10px 14px;margin:0 0 10px;font-family:Arial;font-size:13px;font-weight:bold">
  &#9888;&#65039; BUDGET CAP: {html.escape(ev)}</div>''' for ev in (cap_events or []))}
<h2 style="font-family:Arial;margin:0">&#128269; Wall Hunter — morning digest</h2>
<p style="font-family:Arial;font-size:13px;color:#57534e">
  {len(works)} A/B-tier works &middot; run cost ${cost:.2f} &middot;
  open <b>Wall Hunter.command</b> on the Desktop to review the full queue</p>
{banners}
<ul style="font-family:Arial;font-size:13px">{sale_lines}</ul>
<table>{rows}</table>
</div></body></html>"""

    subject = (f"\U0001F50D Wall Hunter — {len(works)} finds"
               + (f", {a_count} A-TIER" if a_count else "")
               + f" ({len(sale_ids)} sales)")
    msg = MIMEText(body, "html")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    try:
        with smtplib.SMTP("smtp.zoho.com", 587, timeout=30) as server:
            server.starttls()
            server.login(user, pw)
            server.send_message(msg)
        print(f"digest sent to {to}: {subject}")
        return True
    except Exception as ex:
        print(f"digest email failed: {ex}")
        return False
