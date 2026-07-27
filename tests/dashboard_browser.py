from __future__ import annotations

import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPLYFORME_ROOT"] = tmp

        from job_agent import app
        from job_agent.db import init_db, setting
        from playwright.sync_api import sync_playwright

        init_db()
        app.WEB_DIR = REPO_ROOT / "web"
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            base_url = f"http://{host}:{port}"
            errors: list[str] = []
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base_url, wait_until="networkidle")
                page.locator("button[data-view='applications']").click()
                registry_rows = page.locator(
                    "#adapterRegistryList .adapter-registry-row"
                )
                registry_rows.first.wait_for()
                assert registry_rows.count() == 5
                registry_text = page.locator("#adapterRegistryList").inner_text().casefold()
                for adapter in (
                    "greenhouse",
                    "lever",
                    "ashby",
                    "smartrecruiters",
                    "workday",
                ):
                    assert adapter in registry_text
                assert "version 2026.07.1" in registry_text
                assert page.locator("#adapterRegistryMeta").inner_text().startswith(
                    "5 versioned"
                )
                page.locator(".nav-item[data-view='settings']").click()
                assert page.locator(
                    "input[name='posted_age_mode'][value='days']"
                ).is_checked()
                assert page.locator(
                    "[data-posted-age-mode='days']"
                ).is_visible()
                assert page.locator(
                    "[data-posted-age-mode='hours']"
                ).is_hidden()
                page.locator(".segmented-control span", has_text="Hours").click()
                assert page.locator(
                    "input[name='posted_age_mode'][value='hours']"
                ).is_checked()
                assert page.locator(
                    "[data-posted-age-mode='hours']"
                ).is_visible()
                assert page.locator(
                    "[data-posted-age-mode='days']"
                ).is_hidden()
                assert page.locator(
                    "input[name='include_unknown_posted_at']"
                ).is_checked()
                page.locator("input[name='posted_within_hours']").fill("6")
                page.locator("input[name='include_unknown_posted_at']").uncheck()
                with page.expect_response("**/api/settings"):
                    page.get_by_role("button", name="Save settings").click()
                page.wait_for_load_state("networkidle")
                assert setting("posted_age_mode") == "hours"
                assert setting("posted_within_hours") == "6"
                assert setting("include_unknown_posted_at") == "false"
                page.locator("button[data-view='applications']").click()
                desktop = Path(tmp) / "dashboard-desktop.png"
                page.screenshot(path=str(desktop), full_page=True)
                assert desktop.stat().st_size > 10_000
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth + 1"
                )

                page.set_viewport_size({"width": 390, "height": 844})
                page.reload(wait_until="networkidle")
                page.locator("button[data-view='applications']").click()
                registry_rows.first.wait_for()
                mobile = Path(tmp) / "dashboard-mobile.png"
                page.screenshot(path=str(mobile), full_page=True)
                assert mobile.stat().st_size > 10_000
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth + 1"
                )
                browser.close()
            assert not errors, errors
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("dashboard browser ok")


if __name__ == "__main__":
    main()
