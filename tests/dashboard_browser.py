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
        from job_agent.db import init_db
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
