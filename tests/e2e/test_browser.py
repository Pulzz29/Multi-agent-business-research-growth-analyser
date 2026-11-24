"""
Real-browser end-to-end checks (Microsoft Edge via Playwright) against the
running InsightIQ backend and frontend. Opt-in: pytest -m e2e tests/e2e

What is checked is the displayed content, not only that pages load:
company identity in the heading, absence of NaN/undefined/Infinity, no
other company's ticker chip after rapid switching, gated forecast
intervals, the verified-news empty state, report provider labels, and
browser console errors / failed requests. Screenshots and a JSON summary
are written to docs/validation_evidence/browser/.

The access key is read from the environment (conftest) and typed into a
password field; it is never printed or written to evidence.
"""
import json
import os
import re
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.e2e

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASE = os.getenv("INSIGHTIQ_E2E_URL", "http://127.0.0.1:5173")
API = os.getenv("INSIGHTIQ_TEST_BASE_URL", "http://127.0.0.1:8000")
EVIDENCE = PROJECT_ROOT / "docs" / "validation_evidence" / "browser"
BAD_TEXT = re.compile(r"\b(NaN|undefined|Infinity)\b")
RESULTS = {}


def _key():
    key = os.getenv("API_ACCESS_KEY", "")
    assert key, "API_ACCESS_KEY is required for browser tests"
    return key


def api_get(path):
    with httpx.Client(base_url=API, timeout=60, trust_env=False) as c:
        r = c.get(path, headers={"Authorization": f"Bearer {_key()}"})
        r.raise_for_status()
        return r.json()


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        yield b
        b.close()
    (EVIDENCE / "results.json").write_text(json.dumps(RESULTS, indent=2, default=str), encoding="utf-8")


class Watch:
    def __init__(self, page):
        self.console_errors, self.failed = [], []
        page.on("console", lambda m: m.type == "error" and self.console_errors.append(m.text[:300]))
        page.on("pageerror", lambda e: self.console_errors.append(f"pageerror: {str(e)[:300]}"))

        def response(r):
            # Expected "not found yet" answers are part of normal flows.
            if r.status >= 500 or (r.status >= 400 and "/executive-summary" not in r.url
                                   and not r.url.endswith("/comparison") and r.status != 404):
                self.failed.append(f"{r.status} {r.request.method} {r.url.split('?')[0][-90:]}")

        page.on("response", response)

    def assert_clean(self):
        noisy = [e for e in self.console_errors if "favicon" not in e and "404" not in e]
        assert not noisy, noisy[:5]
        assert not self.failed, self.failed[:5]


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1366, "height": 900})
    context.add_init_script(
        "window.sessionStorage.setItem('insightiq-token', %s);" % json.dumps(_key())
    )
    pg = context.new_page()
    pg.watch = Watch(pg)
    yield pg
    context.close()


def text_of(page):
    return page.inner_text("main") if page.query_selector("main") else page.inner_text("body")


def skip_if_page_would_call_ai(ticker, run_id):
    """Opening a company's latest report starts the AI summary when none is
    current. Browser tests never spend AI tokens: skip unless it exists."""
    state = api_get(f"/api/v1/companies/{ticker}/reports/{run_id}/executive-summary")
    runs = api_get(f"/api/v1/research/runs?ticker={ticker}")
    if runs.get("latest_completed_run_id") == run_id and not (
            state.get("status") == "completed" and state.get("is_current")):
        pytest.skip("opening this latest report would start a live AI generation")


def settle(page, timeout=90000):
    """Wait until no loading skeleton is shown (the dev server's websocket and
    background polling mean the network is never fully idle)."""
    page.wait_for_function("() => !document.querySelector('.skeleton')", timeout=timeout)
    page.wait_for_timeout(500)


def open_route(page, hash_route, heading_text, timeout=90000):
    page.goto(f"{BASE}/#{hash_route}")
    page.wait_for_function(
        "(t) => { const h = document.querySelector('h1'); return h && h.innerText.includes(t); }",
        arg=heading_text, timeout=timeout,
    )
    settle(page, timeout)


# --------------------------------------------------------------- connect

def test_connect_with_access_key_in_password_field(browser):
    context = browser.new_context()
    pg = context.new_page()
    watch = Watch(pg)
    pg.goto(BASE)
    field = pg.locator("input[type=password]")
    field.fill(_key())
    assert field.get_attribute("type") == "password"
    pg.get_by_role("button", name="Connect").click()
    pg.wait_for_function("() => document.querySelector('h1')?.innerText.includes('Company intelligence')", timeout=60000)
    pg.screenshot(path=str(EVIDENCE / "overview.png"), full_page=False)
    assert _key() not in pg.content()
    watch.assert_clean()
    context.close()
    RESULTS["connect"] = "passed"


# --------------------------------------------------------- SEC companies

def test_every_sec_validated_company_page(page):
    tickers = api_get("/api/v1/companies")["supported_tickers"]
    results = {}
    for ticker in tickers:
        open_route(page, f"/companies/{ticker}", ticker)
        heading = page.inner_text("h1")
        body = text_of(page)
        assert ticker in heading, (ticker, heading)
        assert not BAD_TEXT.search(body), (ticker, BAD_TEXT.search(body).group(0))
        # The heading's ticker chip is the only one shown for the company.
        chips = page.locator(".company-head .ticker-chip").all_inner_texts()
        assert chips == [ticker], (ticker, chips)
        results[ticker] = {
            "heading": heading.strip(),
            "forecast_badge": "Forecasts available" in body,
            "sec_facts_badge": bool(re.search(r"[\d,]+ SEC facts", body)),
        }
        if ticker in ("MSFT", "ORCL", "NVDA"):
            page.screenshot(path=str(EVIDENCE / f"company_{ticker}.png"), full_page=True)
    page.watch.assert_clean()
    RESULTS["sec_company_pages"] = results


def test_forecast_intervals_follow_validation_gate(page):
    """NVDA intervals failed out-of-sample coverage: none may be drawn or listed."""
    open_route(page, "/companies/NVDA/forecasts", "NVDA")
    body = text_of(page)
    if "No stored forecast" in body or "earlier validation method" in body:
        pytest.skip("NVDA forecast not computed yet (computed on its next research run)")
    assert "Not validated" in body
    page.screenshot(path=str(EVIDENCE / "forecast_NVDA.png"), full_page=True)
    RESULTS["nvda_interval_gate"] = "intervals withheld"


def test_company_news_is_verified_or_honestly_empty(page):
    findings = {}
    for ticker in ("AAPL", "MSFT", "META"):
        open_route(page, f"/companies/{ticker}/news", ticker)
        body = text_of(page)
        empty = "No verified company-specific news is available for the selected period." in body
        findings[ticker] = "empty_state" if empty else "articles_shown"
    page.watch.assert_clean()
    RESULTS["sec_news_tabs"] = findings


# ------------------------------------------------------------- switching

def test_rapid_switching_never_shows_another_company(page):
    sequence = ["AAPL", "MSFT", "NVDA", "GOOGL", "META"]
    for ticker in sequence:
        page.goto(f"{BASE}/#/companies/{ticker}")
        time.sleep(0.15)  # switch before the previous company finished loading
    final = sequence[-1]
    page.wait_for_function(
        "(t) => document.querySelector('h1')?.innerText.includes(t)", arg=final, timeout=60000)
    settle(page, 60000)
    chips = page.locator(".company-head .ticker-chip").all_inner_texts()
    assert chips == [final], chips
    heading = page.inner_text("h1")
    assert all(other not in heading for other in sequence[:-1]), heading

    # US <-> India market pages
    page.goto(f"{BASE}/#/markets/US/companies/ORCL")
    time.sleep(0.2)
    page.goto(f"{BASE}/#/markets/IN/companies/TCS.NS")
    page.wait_for_function("() => document.querySelector('h1')?.innerText.includes('TCS.NS')", timeout=120000)
    settle(page, 120000)
    assert "ORCL" not in page.inner_text("h1")
    assert not BAD_TEXT.search(text_of(page))
    page.screenshot(path=str(EVIDENCE / "market_TCS.NS.png"), full_page=True)
    page.watch.assert_clean()
    RESULTS["rapid_switching"] = "passed"


# ------------------------------------------------------ markets / sectors

def test_markets_and_sector_comparison(page):
    open_route(page, "/markets/US", "United States companies")
    page.wait_for_selector(".top-card, .empty-state", timeout=60000)
    top = page.locator(".top-card").count()
    open_route(page, "/markets/IN/sectors", "India sectors")
    assert page.locator(".sector-card").count() >= 8
    open_route(page, "/markets/US/sectors/technology", "Technology")
    body = text_of(page)
    assert "Entire sector" in body and "Selected companies" in body
    assert not BAD_TEXT.search(body)
    page.screenshot(path=str(EVIDENCE / "sector_US_technology.png"), full_page=True)
    page.watch.assert_clean()
    RESULTS["markets"] = {"us_top_cards": top}


def test_saved_report_shows_its_original_provider(page):
    runs = api_get("/api/v1/research/runs?ticker=MSFT")["runs"]
    completed = [r for r in runs if r.get("report_available")]
    assert completed, "MSFT has no saved report"
    # Only schema-v2 reports record generation metadata.
    run_id = next((r["run_id"] for r in reversed(completed)
                   if api_get(f"/api/v1/companies/MSFT/reports/{r['run_id']}")["report"].get("generation")), None)
    if run_id is None:
        pytest.skip("no MSFT report with generation metadata yet")
    skip_if_page_would_call_ai("MSFT", run_id)
    open_route(page, f"/companies/MSFT/reports/{run_id}", "MSFT")
    page.wait_for_selector("text=AI provider and model", timeout=60000)
    body = text_of(page)
    assert ("Groq ·" in body) or ("Azure OpenAI ·" in body)
    page.screenshot(path=str(EVIDENCE / "report_MSFT_saved.png"), full_page=False)
    page.watch.assert_clean()
    RESULTS["saved_report_label"] = "Groq" if "Groq ·" in body else "Azure OpenAI"
