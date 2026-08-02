"""Email digest of newly flagged lots. Hidden categories are a DISPLAY filter
only (Daniel's rule — detection is never filtered); hidden flags stay in the
DB and are listed in a collapsed count line."""
import os
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

from . import config, store


def _smtp_creds():
    env = {}
    for p in ("~/estate-art-scanner/.env", "~/art-scout/.env"):
        try:
            for ln in Path(p).expanduser().read_text().splitlines():
                if "=" in ln and not ln.strip().startswith("#"):
                    k, _, v = ln.partition("=")
                    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except FileNotFoundError:
            pass
    return env


def _signif(s3) -> str:
    """Green significance line: museums + gallery tier + realized ceiling."""
    g = (s3 or {}).get("_gate") or {}
    bits = []
    if g.get("museums"):
        bits.append("🏛 " + g["museums"])
    elif g.get("standing"):
        bits.append(f"{g['standing']} institutional standing")
    t = g.get("gallery_tier") or 0
    if 1 <= t <= 3:
        label = {1: "Tier-1 mega-gallery", 2: "Tier-2 launchpad",
                 3: "Tier-3 feeder gallery"}[t]
        bits.append("🖼 " + (f"{g['gallery']} — {label}" if g.get("gallery") else label))
    high = g.get("market_high") or g.get("ceiling")
    if high:
        bits.append(f"💰 auction high ${high:,.0f}")
    if g.get("source") and bits:
        bits.append(f"<span style='color:#999'>[{g['source']}]</span>")
    if not bits:
        return ""
    return ('<div style="margin-top:5px;color:#0a7;font-size:13px;">'
            + " · ".join(bits) + "</div>")


def _card(r) -> str:
    import json
    s3 = json.loads(r["s3"]) if r["s3"] else {}
    conf = s3.get("confidence", "?")
    return f"""
    <div style="border:1px solid #ddd;border-radius:8px;padding:14px;margin:10px 0;">
      <div style="font-weight:bold;font-size:15px;">
        <a href="{r['url']}">{r['title']}</a></div>
      <div style="color:#666;margin:4px 0;">{r['auction_title']} — {r['house']}
        <span style="background:#eee;border-radius:4px;padding:1px 6px;margin-left:6px;">{r['platform'].upper()}</span>
        <span style="background:#e3f2fd;border-radius:4px;padding:1px 6px;">{conf}</span>
        <span style="background:#f3e5f5;border-radius:4px;padding:1px 6px;">score {r['promise']:.0f}</span>
      </div>
      <div>Est: {r['estimate'] or '—'} &nbsp; Bid: {r['bid'] or '—'} &nbsp; Artist: {r['artist'] or '—'}</div>
      {_signif(s3)}
      <div style="margin-top:6px;">{s3.get('reasoning','')}</div>
    </div>"""


def send_digest(conn) -> int:
    rows = store.unemailed_flags(conn)
    if not rows:
        return 0
    visible = [r for r in rows if (r["category"] or "other") not in config.HIDE_CATEGORIES]
    hidden_n = len(rows) - len(visible)

    env = _smtp_creds()
    user, pw = env.get("SMTP_USER"), env.get("SMTP_PASSWORD")
    to = env.get("EMAIL_TO") or user
    if not (user and pw):
        print("digest: no SMTP creds — leaving flags unemailed")
        return 0

    if visible:
        top = visible[0]
        import json
        headline = (json.loads(top["s3"]).get("headline")
                    if top["s3"] else None) or top["title"][:60]
        subject = f"🔭 Lot Watcher: {len(visible)} flags — {headline}"
        cards = "".join(_card(r) for r in visible[:60])
    else:
        subject = f"🔭 Lot Watcher: {hidden_n} flags (all in hidden categories)"
        cards = ""

    hidden_line = (f"<p style='color:#999'>+ {hidden_n} flags in hidden categories "
                   f"(jewelry/glass/furniture/decor…) — in the DB, one query away.</p>"
                   if hidden_n else "")
    c = store.counts(conn)
    footer = (f"<hr><p style='color:#999;font-size:12px'>Pipeline: "
              f"{c.get('junk',0)} junk · {c.get('s1',0)} awaiting stage-1 · "
              f"{c.get('s3',0)} awaiting judgment · {c.get('done',0)} done · "
              f"{c.get('flagged',0)} flagged all-time. Local models, $0.</p>")

    html = f"<html><body style='font-family:sans-serif'>{cards}{hidden_line}{footer}</body></html>"
    msg = MIMEText(html, "html")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    s = smtplib.SMTP_SSL(env.get("SMTP_HOST", "smtp.zoho.com"),
                         int(env.get("SMTP_PORT", "465")), timeout=60)
    s.login(user, pw)
    s.send_message(msg)
    s.quit()
    store.mark_emailed(conn, [r["key"] for r in rows])   # hidden ones too — no re-email
    print(f"digest: emailed {len(visible)} flags ({hidden_n} hidden) to {to}")
    return len(visible)
