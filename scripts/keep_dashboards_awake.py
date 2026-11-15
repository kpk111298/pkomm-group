"""
Keeps the Pkomm Group dashboards awake on Streamlit Community Cloud.

Streamlit puts an app to sleep after 12 hours without a visitor. A plain HTTP
request doesn't count as a visit: the page has to run in a browser and open its
WebSocket. So this opens each dashboard in a headless browser, presses the wake
button if the app is asleep, and waits until the app itself has drawn.

Run by .github/workflows/keep-dashboards-awake.yml every 6 hours.
"""

import sys

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

DASHBOARDS = {
    "Pkomm Motors": "https://pkomm-group.streamlit.app/",
    "Pkomm Cart": "https://pkomm-group-cart.streamlit.app/",
}
WAKE_BUTTON = "Yes, get this app back up!"


def visit(page, name, url):
    page.goto(url, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(5_000)

    woke = False
    button = page.get_by_role("button", name=WAKE_BUTTON)
    if button.count():
        button.first.click()
        woke = True

    # The app runs inside an iframe. Wait until it shows the business name.
    try:
        page.frame_locator("iframe[title='streamlitApp']").get_by_text(name).first.wait_for(timeout=180_000)
    except PlaywrightTimeout:
        print(f"{name}: did not finish loading in 3 minutes ({url})")
        return False

    print(f"{name}: {'was asleep, woken up' if woke else 'awake'}")
    return True


def main():
    ok = True
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, url in DASHBOARDS.items():
            page = browser.new_page()
            try:
                ok = visit(page, name, url) and ok
            except Exception as e:  # keep going so one app can't stop the other
                print(f"{name}: {e}")
                ok = False
            finally:
                page.close()
        browser.close()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
