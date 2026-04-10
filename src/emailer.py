"""
Email alert builder and sender using SendGrid.
"""

import logging
import os
import re
from datetime import datetime
import requests

logger = logging.getLogger(__name__)

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"


def _format_sale_date(sale_detail: dict) -> str:
    start = sale_detail.get("firstLocalStartDate", {}).get("_value", "")
    end = sale_detail.get("lastLocalEndDate", {}).get("_value", "")
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if start_dt.date() == end_dt.date():
            return start_dt.strftime("%A %b %d, %Y")
        return f"{start_dt.strftime('%a %b %d')} – {end_dt.strftime('%a %b %d, %Y')}"
    except Exception:
        return "Date TBD"


def _clean_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text).strip()


def _build_html_alert(alert: dict) -> str:
    sale = alert["sale_detail"]
    assessment = alert["assessment"]
    priority = assessment["priority"]
    source = alert.get("source", "NEW")

    color = {"HIGH": "#c0392b", "MEDIUM": "#e67e22"}.get(priority, "#7f8c8d")

    name = sale.get("name", "Estate Sale")
    city = sale.get("cityName", "")
    state = sale.get("stateCode", "")
    zip_code = sale.get("postalCodeNumber", "")
    org = sale.get("orgName") or "Unknown company"
    pic_count = sale.get("pictureCount", 0)
    date_str = _format_sale_date(sale)
    score = assessment["score"]
    summary = _clean_html(assessment["summary"]).replace("\n", "<br>")
    sale_url = alert["sale_url"]

    source_badge = ""
    if source == "PENDING":
        source_badge = '<span style="background:#8e44ad;color:white;padding:3px 8px;border-radius:12px;font-size:11px;margin-left:6px;">RE-CHECK</span>'

    photo_html = ""
    for url in alert.get("art_photo_urls", [])[:4]:
        photo_html += (
            f'<img src="{url}" style="width:180px;height:140px;'
            f'object-fit:cover;margin:4px;border-radius:4px;display:inline-block;">'
        )

    phones = sale.get("phoneNumbers") or []
    phone_html = f'<span style="color:#666;">📞 {phones[0]}</span>' if phones else ""

    website = sale.get("orgWebsite") or ""
    website_html = (
        f'<a href="{website}" style="color:#3498db;font-size:13px;">{website}</a>'
        if website else ""
    )

    return f"""
    <div style="border:2px solid {color};border-radius:8px;padding:20px;
                margin-bottom:24px;font-family:Arial,sans-serif;background:white;">

        <div style="margin-bottom:10px;">
            <span style="background:{color};color:white;padding:4px 12px;
                         border-radius:12px;font-size:12px;font-weight:bold;">
                {priority} PRIORITY
            </span>
            <span style="background:#27ae60;color:white;padding:4px 10px;
                         border-radius:12px;font-size:12px;margin-left:6px;">
                {score}/10
            </span>
            {source_badge}
            <span style="color:#999;font-size:12px;float:right;">{pic_count} photos total</span>
        </div>

        <h2 style="margin:8px 0;font-size:17px;color:#2c3e50;">{name}</h2>
        <p style="margin:4px 0;color:#555;font-size:13px;">
            📍 {city}, {state} {zip_code} &nbsp;|&nbsp; 🏢 {org} &nbsp;|&nbsp; {phone_html}
        </p>
        <p style="margin:4px 0;color:#555;font-size:13px;">
            📅 {date_str} &nbsp;|&nbsp; {website_html}
        </p>

        <div style="margin:14px 0;">{photo_html}</div>

        <div style="background:#f8f9fa;border-left:4px solid {color};
                    padding:12px;border-radius:0 4px 4px 0;
                    font-size:13px;color:#333;line-height:1.6;">
            {summary}
        </div>

        <a href="{sale_url}"
           style="display:inline-block;background:#2c3e50;color:white;
                  padding:10px 20px;border-radius:6px;text-decoration:none;
                  font-size:14px;margin-top:12px;">
            View Full Sale →
        </a>
    </div>
    """


def build_email_html(alerts: list[dict], run_date: str, stats: dict) -> str:
    if not alerts:
        body = f"""
        <div style="text-align:center;padding:40px;color:#666;font-family:Arial,sans-serif;">
            <h2 style="color:#95a5a6;">No alerts today</h2>
            <p>Scanned {stats.get('scanned', 0)} sales in target zip codes.<br>
               Nothing met the quality threshold.</p>
        </div>
        """
    else:
        high = [a for a in alerts if a["assessment"]["priority"] == "HIGH"]
        medium = [a for a in alerts if a["assessment"]["priority"] == "MEDIUM"]
        sections = ""
        if high:
            sections += "<h2 style='color:#c0392b;font-family:Arial;margin-top:28px;'>🔴 HIGH PRIORITY</h2>"
            for a in high:
                sections += _build_html_alert(a)
        if medium:
            sections += "<h2 style='color:#e67e22;font-family:Arial;margin-top:28px;'>🟠 MEDIUM PRIORITY</h2>"
            for a in medium:
                sections += _build_html_alert(a)
        body = sections

    rechecked = stats.get("rechecked_pending", 0)
    recheck_html = (
        f'<span>🔄 <strong>{rechecked}</strong> pending re-checked</span>'
        if rechecked else ""
    )

    return f"""<!DOCTYPE html>
<html>
<body style="max-width:820px;margin:0 auto;padding:20px;background:#f0f2f5;">

    <div style="background:white;border-radius:12px;padding:28px;margin-bottom:20px;
                box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <h1 style="font-family:Arial;color:#2c3e50;margin:0;font-size:24px;">
            🏠 Estate Art Scanner
        </h1>
        <p style="color:#888;font-family:Arial;margin:6px 0 0 0;font-size:14px;">{run_date}</p>
        <div style="display:flex;gap:20px;flex-wrap:wrap;margin-top:16px;
                    font-family:Arial;font-size:13px;color:#555;">
            <span>🔍 <strong>{stats.get('total_new', 0)}</strong> new sales nationwide</span>
            <span>📍 <strong>{stats.get('in_watchlist', 0)}</strong> in target zips</span>
            <span>🖼️ <strong>{stats.get('scanned', 0)}</strong> photo-scanned</span>
            <span>🚨 <strong>{len(alerts)}</strong> alerts</span>
            {recheck_html}
        </div>
    </div>

    {body}

    <div style="text-align:center;color:#bbb;font-family:Arial;font-size:11px;padding:16px;">
        Estate Art Scanner · runs nightly via GitHub Actions
    </div>
</body>
</html>"""


def send_email(html: str, subject: str, to_address: str, alert_count: int):
    """Send via SendGrid API."""
    api_key = os.environ["SENDGRID_API_KEY"]
    from_address = os.environ["ALERT_EMAIL_FROM"]

    payload = {
        "personalizations": [{"to": [{"email": to_address}]}],
        "from": {"email": from_address},
        "subject": subject,
        "content": [{"type": "text/html", "value": html}],
    }

    resp = requests.post(
        SENDGRID_API_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=15,
    )

    if resp.status_code in (200, 202):
        logger.info(f"Email sent to {to_address}: {subject}")
    else:
        logger.error(f"SendGrid error {resp.status_code}: {resp.text}")
        resp.raise_for_status()
