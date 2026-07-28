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
                    "input[name='career_stage_mode'][value='graduate']"
                ).is_checked()
                assert page.locator(
                    "[data-career-stage-mode='graduate']"
                ).is_visible()
                assert page.locator(
                    "[data-career-stage-mode='open']"
                ).is_hidden()
                family_fields = page.locator("input[name='target_role_families']")
                assert family_fields.count() == 6
                assert page.locator(
                    "input[name='target_role_families']:checked"
                ).count() == 6
                assert page.locator(
                    "input[name='graduate_max_required_experience_years']"
                ).input_value() == "2"
                assert not page.locator(
                    "input[name='graduate_include_internships']"
                ).is_checked()
                page.locator(
                    "#careerStageControl span", has_text="Open keyword search"
                ).click()
                assert page.locator(
                    "input[name='career_stage_mode'][value='open']"
                ).is_checked()
                assert page.locator(
                    "[data-career-stage-mode='open']"
                ).is_visible()
                assert page.locator(
                    "[data-career-stage-mode='graduate']"
                ).is_hidden()
                page.locator(
                    "#careerStageControl span", has_text="Graduate / Early Career"
                ).click()
                for index in range(family_fields.count()):
                    family_fields.nth(index).uncheck()
                page.locator(
                    "input[name='target_role_families'][value='product']"
                ).check()
                page.locator(
                    "input[name='target_role_families'][value='consulting']"
                ).check()
                page.locator(
                    "input[name='graduate_max_required_experience_years']"
                ).fill("1")
                page.locator(
                    "input[name='graduate_include_internships']"
                ).check()
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
                assert setting("career_stage_mode") == "graduate"
                assert setting("target_role_families") == "product,consulting"
                assert setting("graduate_max_required_experience_years") == "1"
                assert setting("graduate_include_internships") == "true"
                settings_desktop = Path(tmp) / "settings-desktop.png"
                page.screenshot(path=str(settings_desktop), full_page=True)
                assert settings_desktop.stat().st_size > 10_000
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth + 1"
                )
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
                page.locator(".nav-item[data-view='settings']").click()
                assert page.locator(
                    "[data-career-stage-mode='graduate']"
                ).is_visible()
                assert page.locator(".role-family-grid").is_visible()
                settings_mobile = Path(tmp) / "settings-mobile.png"
                page.screenshot(path=str(settings_mobile), full_page=True)
                assert settings_mobile.stat().st_size > 10_000
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
