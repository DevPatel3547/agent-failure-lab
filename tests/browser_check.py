"""Browser acceptance checks; requires the dev extra and Playwright Chromium."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from playwright.sync_api import sync_playwright

from agent_failure_lab.providers import ScriptedAgent
from agent_failure_lab.report import bundle, write_report
from agent_failure_lab.runner import run_experiment
from agent_failure_lab.schema import load_scenarios
from agent_failure_lab.storage import Database


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db = Database(root / "lab.sqlite3")
        experiment = run_experiment(db, load_scenarios(), ScriptedAgent())
        data = bundle(db, experiment)
        # A report must display untrusted text literally, even in a script-like string.
        data["results"][0]["description"] += ' <img src=x onerror="window.injected=true">'
        report = write_report(data, root / "report")
        errors, external_requests = [], []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "request",
                lambda request: (
                    external_requests.append(request.url) if request.url.startswith("http") else None
                ),
            )
            page.goto(report.as_uri())
            page.locator(".case-button").first.wait_for()
            assert page.locator(".case-button").count() == 72
            assert page.locator(".summary-card").count() == 3
            assert "No AI models were evaluated" in page.locator("#notice").inner_text()
            assert page.locator(".trace-event.world").count() == 0
            page.locator("#show-world").check()
            assert page.locator(".trace-event.world").count() > 0
            assert page.locator(".trace-event.grader").count() == 1
            slider = page.get_by_role("slider", name="Trace event position")
            slider.fill("0")
            assert page.locator(".timeline li").count() == 0
            page.locator("#search").fill("lost acknowledgement")
            # The fixture name is not assumed: search by its stable id.
            page.locator("#search").fill("lost-ack")
            assert page.locator(".case-button").count() == 3
            page.locator("#mode").select_option("guarded")
            assert page.locator(".case-button").count() == 1
            assert "Confirmed correctly" in page.locator("#detail").inner_text()
            page.locator("#search").fill("no-match-xyz")
            assert "No cases match" in page.locator("#detail").inner_text()
            page.locator("#search").fill("")
            page.locator("#mode").select_option("")
            page.locator("#outcome").select_option("issue")
            assert 0 < page.locator(".case-button").count() < 72
            page.locator("#outcome").select_option("")
            with page.expect_download() as download:
                page.locator("#download").click()
            saved = root / "export.json"
            download.value.save_as(saved)
            assert len(json.loads(saved.read_text())["results"]) == 72
            assert page.evaluate("window.injected") is None
            assert not external_requests, external_requests
            # Capture the shipped fixture without the injected test string.
            clean = write_report(bundle(db, experiment), root / "clean")
            page.goto(clean.as_uri())
            page.locator(".case-button").first.wait_for()
            output = Path(__file__).resolve().parents[1] / "docs" / "report-preview.png"
            page.screenshot(path=str(output), full_page=False)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
                "Mobile horizontal overflow"
            )
            page.locator("#search").fill("lost-ack")
            assert page.locator(".case-button").count() == 3
            page.screenshot(path=str(root / "mobile.png"), full_page=True)
            wire_report = Path("reports/wire/index.html")
            if wire_report.exists():
                page.set_viewport_size({"width": 1440, "height": 1100})
                page.goto(wire_report.resolve().as_uri())
                page.locator(".case-button").first.wait_for()
                assert page.locator(".case-button").count() == 24
                assert "Real HTTP" in page.locator("#metadata").inner_text()
                assert "No AI models were evaluated" in page.locator("#notice").inner_text()
                page.locator("#mode").select_option("reference")
                assert page.locator(".case-button").count() == 8
                page.locator("#wire-events summary").click()
                assert "upstream_responded" in page.locator("#wire-events").inner_text()
                page.locator("#mode").select_option("")
                page.goto(wire_report.resolve().as_uri())
                page.locator(".case-button").first.wait_for()
                page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(path=str(output.with_name("wire-preview.png")), full_page=False)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            # Synthetic API responses must never appear as real model evidence.
            from agent_failure_lab.evaluation import evaluate, make_plan
            from test_evaluation import Answers, finish

            plan = make_plan(
                load_scenarios()[:1],
                "openai",
                "test-model",
                input_price="1",
                output_price="2",
                context_tokens=1000,
                price_source="https://example.test/pricing",
                max_usd="1",
                max_requests=1,
                max_output_tokens=100,
                trials=1,
                max_steps=2,
            )
            evaluated = evaluate(
                plan,
                root / "evaluation.sqlite3",
                root / "evaluation",
                transport=Answers([finish()]),
                api_key="test-only",
            )
            page.goto((root / "evaluation" / "index.html").as_uri())
            assert "Injected test responses" in page.locator("#notice").inner_text()
            assert "Model decisions came from" not in page.locator("#notice").inner_text()
            assert "Incomplete evidence" in page.locator("#notice").inner_text()
            page.locator("#evaluation-audit summary").click()
            assert "charged_or_reserved_usd" in page.locator("#evaluation-audit").inner_text()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert evaluated["coverage"]["not_started"] == 1
            sdk_report = Path("reports/sdk/index.html")
            if sdk_report.exists():
                page.goto(sdk_report.resolve().as_uri())
                assert page.locator(".case-button").count() == 16
                assert "openai-agents" in page.locator("#metadata").inner_text()
                page.locator("#sdk-inputs summary").click()
                assert "function_call_output" in page.locator("#sdk-inputs").inner_text()
                assert "No AI models were evaluated" in page.locator("#notice").inner_text()
            assert not external_requests, external_requests
            assert not errors, errors
            browser.close()
        print(
            "Browser checks passed: filters, world reveal, trace scrub, export, XSS, offline behavior, desktop and mobile layout."
        )


if __name__ == "__main__":
    main()
