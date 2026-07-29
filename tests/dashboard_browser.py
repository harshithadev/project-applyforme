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
        from playwright.sync_api import expect, sync_playwright

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
                expect(page.locator("#viewTitle")).to_have_text("Workflow")
                expect(page.locator("#scanBtn")).to_have_text("Scan now")
                assert page.locator(".workflow-tabs button").count() == 4
                assert page.locator("#workflowAgePreset option").count() == 6
                workflow_scope = page.locator(".workflow-scope").inner_text()
                assert "United States" in workflow_scope
                assert "CPT/OPT" in workflow_scope
                assert page.locator("#workflowQueueList").is_visible()
                workflow_desktop = Path(tmp) / "workflow-desktop.png"
                page.screenshot(path=str(workflow_desktop), full_page=True)
                assert workflow_desktop.stat().st_size > 10_000
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth + 1"
                )
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
                source_types = page.locator("#careerSourceType option")
                assert source_types.count() == 6
                assert source_types.all_text_contents() == [
                    "Greenhouse",
                    "Lever",
                    "Ashby",
                    "SmartRecruiters",
                    "Workday",
                    "Generic company career page",
                ]
                provider_fields = page.locator(
                    "input[name='discovery_providers']"
                )
                assert provider_fields.count() == 5
                assert page.locator(
                    "input[name='discovery_providers']:checked"
                ).count() == 5
                assert page.locator(
                    "select[name='target_company_mode']"
                ).input_value() == "prefer"
                page.locator("#careerSourceType").select_option("lever")
                assert (
                    page.locator("#careerSourceUrl").get_attribute("placeholder")
                    == "https://jobs.lever.co/company"
                )
                page.locator("#careerSourceUrl").fill("https://jobs.lever.co")
                page.get_by_role("button", name="Add source").click()
                expect(page.locator("#toast")).to_have_text(
                    "Enter a Lever company board URL."
                )
                assert setting("career_urls") == ""
                page.locator("#careerSourceUrl").fill(
                    "https://jobs.lever.co/exampleco"
                )
                with page.expect_response("**/api/settings"):
                    page.get_by_role("button", name="Add source").click()
                assert (
                    setting("career_urls")
                    == "https://jobs.lever.co/exampleco"
                )
                expect(
                    page.locator("#settingsForm textarea[name='career_urls']")
                ).to_have_value(
                    "https://jobs.lever.co/exampleco"
                )
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
                ).input_value() == "3"
                assert page.locator(
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
                page.locator(
                    "select[name='work_authorization_mode']"
                ).select_option("cpt_opt_future_sponsorship")
                page.locator(
                    "select[name='sponsorship_unknown_handling']"
                ).select_option("review")
                assert page.locator(
                    "input[name='posted_age_mode'][value='hours']"
                ).is_checked()
                assert page.locator(
                    "[data-posted-age-mode='hours']"
                ).is_visible()
                assert page.locator(
                    "[data-posted-age-mode='days']"
                ).is_hidden()
                assert not page.locator(
                    "input[name='include_unknown_posted_at']"
                ).is_checked()
                page.locator(
                    ".segmented-control span", has_text="Calendar days"
                ).click()
                assert page.locator(
                    "input[name='posted_age_mode'][value='days']"
                ).is_checked()
                assert page.locator(
                    "[data-posted-age-mode='days']"
                ).is_visible()
                page.locator(".segmented-control span", has_text="Hours").click()
                page.locator("input[name='posted_within_hours']").fill("6")
                page.locator("input[name='include_unknown_posted_at']").uncheck()
                page.locator(
                    "input[name='discovery_providers'][value='remotive']"
                ).uncheck()
                page.locator("select[name='target_company_mode']").select_option(
                    "only"
                )
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
                assert (
                    setting("work_authorization_mode")
                    == "cpt_opt_future_sponsorship"
                )
                assert setting("sponsorship_unknown_handling") == "review"
                assert (
                    setting("discovery_providers")
                    == "jobicy,weworkremotely,arbeitnow,himalayas"
                )
                assert setting("target_company_mode") == "only"
                settings_desktop = Path(tmp) / "settings-desktop.png"
                page.screenshot(path=str(settings_desktop), full_page=True)
                assert settings_desktop.stat().st_size > 10_000
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth + 1"
                )
                page.locator(".nav-item[data-view='jobs']").click()
                assisted_links = page.locator("#assistedSearchList a")
                assert assisted_links.count() == 14
                assisted_hrefs = assisted_links.evaluate_all(
                    "(links) => links.map((link) => link.href)"
                )
                assert sum("wellfound.com" in href for href in assisted_hrefs) == 4
                assert any("hiring.cafe" in href for href in assisted_hrefs)
                jobs_desktop = Path(tmp) / "jobs-desktop.png"
                page.screenshot(path=str(jobs_desktop), full_page=True)
                assert jobs_desktop.stat().st_size > 10_000
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
                expect(page.locator("#viewTitle")).to_have_text("Workflow")
                assert page.locator(".workflow-tabs button").count() == 4
                workflow_mobile = Path(tmp) / "workflow-mobile.png"
                page.screenshot(path=str(workflow_mobile), full_page=True)
                assert workflow_mobile.stat().st_size > 10_000
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth + 1"
                )
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
                assert page.locator(
                    ".role-family-settings .role-family-grid"
                ).is_visible()
                assert page.locator(
                    ".discovery-provider-settings"
                ).is_visible()
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
