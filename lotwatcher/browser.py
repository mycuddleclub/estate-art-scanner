"""Persistent headed browser for LiveAuctioneers.

The whole point: solve the CAPTCHA ONCE in a real visible window; the
clearance cookie lives in the persistent profile. If LA re-challenges,
we email Daniel and wait for him to click — never fail silently.
"""
import random
import subprocess
import time

from playwright.sync_api import sync_playwright
from . import config

ALERT = str(config.ROOT.parent / "bin" / "alert.py")  # ~/bin/alert.py

_CHALLENGE_MARKERS = (
    "px-captcha", "just a moment", "unusual activity", "are you a human",
    "verify you are human", "checking your browser", "press & hold",
)


def launch(p):
    """Headed persistent context (WSLg shows the window on the Windows desktop)."""
    config.LA_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ctx = p.chromium.launch_persistent_context(
        str(config.LA_PROFILE_DIR),
        headless=False,
        viewport={"width": 1440, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    return ctx


def polite_sleep(bounds):
    time.sleep(random.uniform(*bounds))


def is_challenged(page) -> bool:
    try:
        title = (page.title() or "").lower()
        body = page.evaluate("() => document.body ? document.body.innerText.slice(0, 2000) : ''").lower()
    except Exception:
        return False
    hay = title + " " + body
    return any(m in hay for m in _CHALLENGE_MARKERS)


def send_alert(subject: str, body: str):
    try:
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(body)
        subprocess.run(["python3", ALERT, subject, f.name], timeout=60)
        os.unlink(f.name)
    except Exception as e:
        print(f"  alert send failed: {e}")


def wait_out_challenge(page, context: str, max_wait_s: int = 1800) -> bool:
    """CAPTCHA hit: email Daniel, then poll until he solves it in the open
    window. Returns True once clear, False if we timed out."""
    print(f"  !! CAPTCHA/challenge detected ({context}) — emailing Daniel, waiting...")
    send_alert(
        "LA CAPTCHA — click the browser window on the Z13",
        f"LiveAuctioneers is showing a human check while: {context}\n\n"
        "A Chromium window is open on the Z13 desktop. Solve the check there\n"
        "and the watcher resumes on its own (checks every 30s, waits up to 30 min).\n"
        "After solving once, the session cookie persists — this should be rare.")
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        time.sleep(30)
        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception:
            continue
        if not is_challenged(page):
            print("  challenge cleared — resuming")
            return True
    print("  challenge NOT cleared in time — giving up this cycle")
    return False


def goto(page, url: str, context: str, wait: str = "domcontentloaded") -> bool:
    """Navigate with challenge handling. Returns False if blocked."""
    try:
        page.goto(url, wait_until=wait, timeout=90000)
    except Exception:
        pass
    if is_challenged(page):
        if not wait_out_challenge(page, f"{context}: {url}"):
            return False
    return True
