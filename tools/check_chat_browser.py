"""Browser interaction QA with isolated HTTP fixtures; no live database changes.

Uses existing system Python Playwright and installed Chrome, not new packages.
Backend conversation semantics are separately covered by tests/test_chat.py.
"""
import json
import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"
SESSION = "5ebef4ab-58ad-49a9-9974-851d8e3213db"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Use the running local backend instead of browser fixtures")
    parser.add_argument("--scope-only", action="store_true", help="Check live intent/incident clarification without triggering model inference")
    args = parser.parse_args()
    requests, errors = [], []
    def handle(route):
        request = route.request
        path = request.url.removeprefix("http://127.0.0.1:8765")
        if request.method == "DELETE":
            requests.append(("DELETE", path))
            return route.fulfill(json={"status": "cleared"})
        if request.method == "POST" and path == "/chat":
            body = request.post_data_json
            requests.append(("POST", body))
            message = body["message"]
            if message == "Can I pay monthly or annually?":
                response = {"status": "clarification_required", "answer": "Car, home or business insurance?", "product": None}
            elif message == "Car insurance":
                assert body["session_id"] == SESSION
                response = {"status": "answered", "answer": "UI test passage. <img src=x onerror=alert(1)> This is not policy information.", "product": "car",
                            "sources": [{"url": "https://www.allianz.com.au/car-insurance.html"}, {"url": "http://["}]}
            else:
                response = {"status": "abstained", "answer": "UI test: no sufficient evidence.", "product": "car"}
            return route.fulfill(json={"session_id": SESSION, "sources": [], "sources_checked": [],
                                       "discovery_url": "https://www.allianz.com.au/sitemap.xml", **response})
        mapping = {"/": ("index.html", "text/html"), "/assets/chat.css": ("chat.css", "text/css"),
                   "/assets/chat.js": ("chat.js", "text/javascript")}
        if path in mapping:
            name, content_type = mapping[path]
            return route.fulfill(path=str(WEB / name), content_type=content_type)
        return route.fulfill(status=404, body="Not found")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        if not args.live:
            page.route("http://127.0.0.1:8765/**", handle)
        page.goto("http://127.0.0.1:8000/" if args.live else "http://127.0.0.1:8765/")
        assert page.locator("#welcome").is_visible()
        if args.scope_only:
            if not args.live:
                raise ValueError("--scope-only requires --live")
            page.locator("#message").fill("my car broke")
            page.locator("#message").press("Enter")
            page.get_by_role("button", name="Repair coverage", exact=True).wait_for()
            page.get_by_role("button", name="Repair coverage", exact=True).click()
            page.get_by_role("button", name="Mechanical breakdown", exact=True).wait_for()
            assert "Car insurance" in page.locator("#context").inner_text()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.get_by_role("button", name="New conversation", exact=False).click()
            page.locator("#welcome").wait_for(state="visible")
            assert not errors, errors
            browser.close()
            print(json.dumps({"passed": True, "live_backend": True,
                "checked": ["intent choices", "incident choices", "pending context", "mobile layout", "reset"],
                "model_inference": "not triggered"}, indent=2))
            return
        page.locator("#message").fill("Can I pay monthly or annually?")
        page.locator("#message").press("Enter")
        page.get_by_role("button", name="Car insurance", exact=True).wait_for()
        page.get_by_role("button", name="Car insurance", exact=True).click()
        page.get_by_text("With Allianz Car Insurance", exact=False).wait_for() if args.live else page.get_by_text("UI test passage.", exact=False).wait_for()
        assert page.locator(".source-link[href='https://www.allianz.com.au/car-insurance.html']").count() == 1
        assert page.locator("img").count() == 0
        assert not page.locator("#send").is_disabled()
        assert "Car insurance" in page.locator("#context").inner_text()
        ROOT.joinpath("data").mkdir(exist_ok=True)
        page.screenshot(path=str(ROOT / "data/chat-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(ROOT / "data/chat-mobile.png"), full_page=True)
        page.get_by_role("button", name="New conversation", exact=False).click()
        page.locator("#welcome").wait_for(state="visible")
        assert page.locator(".turn").count() == 0
        if not args.live:
            assert any(method == "DELETE" for method, _ in requests)
        assert not errors, errors
        browser.close()
    print(json.dumps({"passed": True, "live_backend": args.live, "checked": ["send", "clarification", "session continuation",
          "source links", "safe rendering", "mobile overflow", "new conversation"],
          "screenshots": ["data/chat-desktop.png", "data/chat-mobile.png"]}, indent=2))


if __name__ == "__main__":
    main()
