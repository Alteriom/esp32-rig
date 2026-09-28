"""Screenshots of the dashboard, for the guide.

Drives a real Chromium (Playwright) against a running rig -- the demo rig of
demo_rig.py, never a real one -- signs in with its key, opens each page,
waits for it to fill, and writes a WebP per page into OUT (a PNG of the
page's gradient is three times the size for no visible difference).

    pip install playwright pillow && playwright install chromium
    BASE=http://127.0.0.1:8090 KEY=... OUT=docs/images python runner/screenshots/capture.py

Pages are named in SHOTS below; a shot whose page needs a run's id finds one
through the API first. Deterministic enough to re-run after a UI change and
commit the result: same state, same viewport, same pages.
"""
from __future__ import annotations

import io
import json
import os
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8090").rstrip("/")
KEY = os.environ["KEY"]
OUT = Path(os.environ.get("OUT", "docs/images"))
DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}


def api(path: str):
    request = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(request, timeout=20) as answer:
        return json.load(answer)


def a_run(profile: str, status: str) -> str:
    jobs = api("/api/v1/jobs?limit=100")["jobs"]
    return next(job["id"] for job in jobs if job["request"].get("profile") == profile and job["status"] == status)


SHOTS = [
    # (file, hash, viewport, full page)
    ("overview.webp", "#overview", DESKTOP, False),
    ("runs.webp", "#runs", DESKTOP, False),
    ("run.webp", lambda: f"#run/{a_run('painlessmesh', 'passed')}", DESKTOP, True),
    ("run-failed.webp", lambda: f"#run/{a_run('rig-example', 'failed')}", DESKTOP, False),
    ("boards.webp", "#boards", DESKTOP, False),
    ("firmware.webp", "#artifacts", DESKTOP, False),
    ("statistics.webp", "#statistics", DESKTOP, True),
    ("profile-ci.webp", "#profile/ci", DESKTOP, False),
    ("profile-you.webp", "#profile", DESKTOP, False),
    ("settings.webp", "#configuration", DESKTOP, False),
    ("phone-overview.webp", "#overview", PHONE, False),
    ("phone-run.webp", lambda: f"#run/{a_run('painlessmesh', 'passed')}", PHONE, False),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for name, where, viewport, full in SHOTS:
            where = where() if callable(where) else where
            # bypass_csp: the page allows no eval, and Playwright's waits evaluate
            # in the page. The page served is the page a person gets.
            context = browser.new_context(viewport=viewport, device_scale_factor=2 if viewport is PHONE else 1,
                                          color_scheme="dark", bypass_csp=True)
            # The key a person pastes, where the dashboard keeps it: this tab.
            context.add_init_script(f"sessionStorage.setItem('farmToken', {json.dumps(KEY)});")
            page = context.new_page()
            page.goto(f"{BASE}/{where}")
            page.wait_for_function("document.body.dataset.mode", timeout=20000)
            page.wait_for_timeout(2500)   # the page's own loads, after the first status
            # The console bar is fixed to the window: in a picture it only covers the page.
            page.add_style_tag(content="#console{display:none!important}")
            shot = Image.open(io.BytesIO(page.screenshot(full_page=full)))
            shot.save(OUT / name, "WEBP", quality=88, method=6)
            print("wrote", OUT / name)
            context.close()
        browser.close()


if __name__ == "__main__":
    main()
