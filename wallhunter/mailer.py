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


def _exclusives_html(exclusives: list[dict]) -> str:
    if not exclusives:
        return ""
    e = lambda s: html.escape(str(s or ""))
    rows = "".join(
        f"<li><b>{e(a['house'])}</b> — <a href='{e(a['url'])}'>{e(a['title'])}</a>"
        f" <span style='color:#78716c'>[{e(a['platform'])}]"
        f"{' ' + e(a['info']) if a.get('info') else ''}</span></li>"
        for a in exclusives[:20])
    more = (f"<p style='font-family:Arial;font-size:12px;color:#78716c'>"
            f"+{len(exclusives) - 20} more</p>" if len(exclusives) > 20 else "")
    return f"""<div style="background:#eff6ff;border:1px solid #3b82f6;border-radius:8px;
      padding:12px 16px;margin:10px 0;font-family:Arial;font-size:13px">
      <b>&#128373; Off-radar auctions</b> — houses NOT currently on
      LiveAuctioneers/Invaluable (smaller bidder pools):
      <ul>{rows}</ul>{more}</div>"""


def send_digest(conn, sale_ids: list[int], cost: float,
                exclusives: list[dict] | None = None) -> bool:
    user, pw, to = _smtp_config()
    if not all([user, pw, to]):
        print("digest: SMTP not configured (SMTP_USER/SMTP_PASSWORD/EMAIL_TO) — skipping email")
        return False
    if not sale_ids and not exclusives:
        return False
    sale_ids = sale_ids or [0]
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
<h2 style="font-family:Arial;margin:0">&#128269; Wall Hunter — morning digest</h2>
<p style="font-family:Arial;font-size:13px;color:#57534e">
  {len(works)} A/B-tier works &middot; run cost ${cost:.2f} &middot;
  open <b>Wall Hunter.command</b> on the Desktop to review the full queue</p>
{banners}
{_exclusives_html(exclusives or [])}
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
