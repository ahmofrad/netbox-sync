"""Capture real Operations responses at desktop and narrow widths.

Run with Playwright installed in an isolated acceptance-only path. Credentials
are read from the ignored generated administrator file, never embedded here.
"""
import json
import os
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
RESULTS.mkdir(exist_ok=True)
BASE = os.environ.get("NETBOX_SYNC_ACCEPTANCE_URL", "http://127.0.0.1:18080")
CREDENTIALS = json.loads((ROOT / "runtime" / "credentials.json").read_text(encoding="utf-8"))
SCENARIOS = {
    name: json.loads((RESULTS / f"{name}.json").read_text(encoding="utf-8"))
    for name in os.environ.get("NETBOX_SYNC_VISUAL_SCENARIOS", "populated").split(",")
}


def capture(browser, scenario, response, width):
    page = browser.new_page(viewport={"width": width, "height": 1000})
    page.route("**/plugins/netbox-sync/operations/", lambda route: route.fulfill(
        status=200, content_type="text/html", body=response)
        if route.request.resource_type == "document" else route.continue_())
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.goto(f"{BASE}/plugins/netbox-sync/operations/", wait_until="networkidle")
    assert page.locator(".sync-operations").count() == 1
    expected_banner = SCENARIOS[scenario]["expected_banner"]
    assert expected_banner in page.locator(".sync-operations").inner_text()
    if scenario == "empty-error":
        assert page.locator("#sync-run-select option").inner_text() == "No completed runs"
        assert page.get_by_text("Some operational history is temporarily unavailable. No action has been taken.").count() == 1
    expected_segments = page.locator("[data-health-chart]").evaluate(
        "chart => ['healthy', 'attention', 'offline'].filter(key => Number(chart.dataset[key]) > 0).length"
    ) if page.locator("[data-health-chart]").count() else 0
    assert page.locator(".sync-donut-segment").count() == expected_segments
    assert page.locator(".sync-operations").evaluate(
        "element => getComputedStyle(element).backgroundColor === 'rgb(220, 228, 222)'")
    assert not errors, errors
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    target = RESULTS / f"{scenario}-{width}.png"
    page.screenshot(path=target, full_page=True)
    metrics = page.locator(".sync-operations").evaluate("""element => ({
        heading: getComputedStyle(element.querySelector('h1')).fontSize,
        background: getComputedStyle(element).backgroundColor,
        chartSegments: element.querySelectorAll('.sync-donut-segment').length,
        localStylesheet: !!document.querySelector('link[href*="netbox_sync_plugin/operations.css"]')
    })""")
    page.close()
    return {"viewport": width, "overflow": False, "screenshot": target.name, **metrics}


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(urljoin(BASE, "/login/"), wait_until="networkidle")
    page.get_by_label("Username").fill(CREDENTIALS["username"])
    page.get_by_label("Password").fill(CREDENTIALS["password"])
    page.get_by_role("button", name="Sign In").click()
    page.wait_for_url(f"{BASE}/")
    evidence = {}
    for scenario, response in SCENARIOS.items():
        evidence[scenario] = [capture(browser, scenario, response["html"], width)
                              for width in (1440, 390)]
    browser.close()

for name in SCENARIOS:
    (RESULTS / f"{name}.json").unlink()
print(json.dumps(evidence, indent=2))
