"""
A forecast whose 95% interval failed its quality gate carries no bounds. Citing
it in an AI finding must not crash the research run (US/SEC companies only have
forecasts, so this broke US research). Fake AI client, isolated runtime.
"""
import pytest

from research_v2_support import (  # noqa: F401  (fixture import)
    default_handler,
    install_fake_ai,
    isolated_runtime,
    payload_of,
    respond_financial,
    schema_name,
)
from research_v2_support import completion


def _forecast_item(low, high):
    return {
        "evidence_id": "FORECAST_revenue",
        "kind": "statistical_forecast",
        "metric": "revenue",
        "predictions": [{"estimate_billion_usd": 41.5, "lower_95_billion_usd": low, "upper_95_billion_usd": high}],
    }


def test_observation_with_validated_interval():
    from app.services.evidence_package import render_observation

    text = render_observation(_forecast_item(39.25, 43.75))
    assert "USD 41.50 billion, 95% model interval 39.25–43.75 [FORECAST_revenue]" in text


def test_observation_without_interval_does_not_crash():
    from app.services.evidence_package import render_observation

    text = render_observation(_forecast_item(None, None))
    assert "USD 41.50 billion" in text
    assert "interval not shown" in text and "None" not in text


def test_us_research_completes_when_ai_cites_forecast_without_interval(isolated_runtime, monkeypatch):
    import json

    from app.agents import financial_agent

    real_build = financial_agent.build_forecasts

    def withheld_intervals(ticker, financials):
        forecasts, quality, unavailable = real_build(ticker, financials)
        for metric in quality:
            quality[metric]["interval_display_allowed"] = False
        return forecasts, quality, unavailable

    monkeypatch.setattr(financial_agent, "build_forecasts", withheld_intervals)

    def handler(kwargs, n):
        if schema_name(kwargs) != "insightiq_financial_analysis":
            return default_handler(kwargs, n)
        evidence = payload_of(kwargs)["evidence"]
        forecast = next(f for f in evidence["forecasts"] if f["evidence_id"] == "FORECAST_revenue")
        assert all(p["lower_95_billion_usd"] is None for p in forecast["predictions"])
        body = respond_financial(kwargs)
        body["findings"].append({
            "title": "Statistical revenue projection and its limits",
            "evidence_ids": ["FORECAST_revenue"],
            "comparison_ids": [],
            "interpretation": (
                "The statistical model projects revenue for the coming quarters from past reported "
                "quarters. It is an estimate rather than a reported result. Its uncertainty range "
                "did not pass the quality check, so no range is given for this projection."
            ),
            "business_significance": "It indicates the direction implied by the reported history.",
            "limitations": "The projection cannot anticipate events that are not in past filings.",
        })
        return completion(json.dumps(body))

    install_fake_ai(monkeypatch, handler)
    from app.agents.research_workflow import run_research_workflow

    report = run_research_workflow("NVDA", options={"refresh": "skip"})
    assert report["status"] == "completed"
    fin = report["financial"]["analysis"]
    cited = [f for f in fin["findings"] if "FORECAST_revenue" in f["evidence_ids"]]
    assert cited, fin["withheld"]
    assert "interval not shown" in cited[0]["verified_observation"]
