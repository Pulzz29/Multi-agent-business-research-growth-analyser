"""
Azure OpenAI (Responses API, background mode) request handling: endpoint and
deployment configuration, strict schemas, polling, incomplete/failed/cancelled
responses, HTTP 429 / 401 / 404 / 400, timeouts, deadlines, job cancellation,
served-model verification, attempt limits and diagnostics hygiene.

Test-only fake clients; no network (blocked by conftest).
"""
import json
import logging
from types import SimpleNamespace

import httpx
import openai
import pytest
from pydantic import BaseModel, ConfigDict, Field

from research_v2_support import TEST_MODEL, FakeAIClient, completion

from app.services import ai_provider
from app.services.ai_endpoint import EndpointError, normalise_endpoint
from app.services.ai_provider import (
    AICallFailed,
    RequestProfile,
    ResponseRejected,
    estimate_request_tokens,
    split_messages,
    strict_json_schema,
    structured_completion,
    text_format,
)

SECRET = "TEST_SECRET_AZURE_KEY_DO_NOT_LOG"


class Inner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=40)
    score: float | None = None


class Tiny(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str
    items: list[Inner] = []


def profiles():
    return [
        RequestProfile("full", 500, {"size": 40}),
        RequestProfile("reduced", 300, {"size": 5}),
    ]


def build(profile):
    return [
        {"role": "system", "content": "s"},
        {"role": "user", "content": json.dumps({"rows": ["x" * 50] * profile.options["size"]})},
    ]


def status_error(code, message="error", headers=None, body=None, cls=openai.APIStatusError):
    request = httpx.Request("POST", "https://insightiq-test.openai.azure.com/openai/v1/responses")
    response = httpx.Response(code, request=request, headers=headers or {})
    return cls(message, response=response, body=body)


def call(client, budget=5000, validate=None, clock=None):
    kwargs = dict(
        agent="financial", call_name="test", schema_name="tiny",
        schema_model=Tiny, build_messages=build, profiles=profiles(),
        validate=validate, request_token_budget=budget, client=client,
        sleep=lambda s: None,
    )
    if clock:
        kwargs["clock"] = clock
    return structured_completion(**kwargs)


OK = '{"answer":"ok","items":[]}'


# ----------------------------------------------------------- configuration

@pytest.mark.parametrize("value", [
    "https://res.openai.azure.com",
    "https://res.openai.azure.com/",
    "https://res.openai.azure.com/openai",
    "https://res.openai.azure.com/openai/v1",
    "https://RES.openai.azure.com/openai/v1/",
])
def test_endpoint_is_normalised_to_v1(value):
    assert normalise_endpoint(value) == "https://res.openai.azure.com/openai/v1/"


@pytest.mark.parametrize("value", [
    "",
    "http://res.openai.azure.com",
    "https://example.com/openai/v1",
    "https://res.openai.azure.com/openai/deployments/gpt-5.4-pro/chat/completions",
    "https://res.openai.azure.com/openai/v1?api-version=2025-04-01-preview",
])
def test_invalid_endpoints_are_rejected(value):
    with pytest.raises(EndpointError):
        normalise_endpoint(value)


def test_settings_reject_unsupported_reasoning_effort(monkeypatch):
    from pydantic import ValidationError

    from app.core.config import Settings

    monkeypatch.setenv("INSIGHTIQ_AZURE_REASONING_EFFORT", "turbo")
    with pytest.raises(ValidationError):
        Settings()
    for effort in ("low", "xhigh"):
        monkeypatch.setenv("INSIGHTIQ_AZURE_REASONING_EFFORT", effort.upper())
        assert Settings().azure_reasoning_effort == effort


def test_expected_model_prefix_defaults_to_deployment(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.delenv("INSIGHTIQ_AZURE_EXPECTED_MODEL_PREFIX", raising=False)
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-6.2-sol")
    get_settings.cache_clear()
    try:
        assert ai_provider.ai_runtime_settings()["expected_model_prefix"] == "gpt-6.2-sol"
        monkeypatch.setenv("INSIGHTIQ_AZURE_EXPECTED_MODEL_PREFIX", "gpt-6.2")
        get_settings.cache_clear()
        assert ai_provider.ai_runtime_settings()["expected_model_prefix"] == "gpt-6.2"
    finally:
        get_settings.cache_clear()


def test_default_client_uses_official_sdk_and_v1_endpoint():
    client = ai_provider.default_client()
    assert isinstance(client, openai.OpenAI)
    assert str(client.base_url) == "https://insightiq-test.openai.azure.com/openai/v1/"
    assert client.max_retries == 0


# ------------------------------------------------------------------ schemas

def test_strict_schema_closes_every_object_and_requires_every_field():
    schema = strict_json_schema(Tiny)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            assert not {"default", "title", "minLength", "maxLength"} & set(node)
            if "$ref" in node:
                assert set(node) == {"$ref"}
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    assert set(schema["required"]) == {"answer", "items"}


def test_every_application_schema_converts_to_strict_form():
    from app.agents.financial_agent import FinancialAnalysisResponse
    from app.agents.news_agent import NewsAnalysisResponse, NewsSelections
    from app.agents.strategic_agent import StrategicAnalysisResponse
    from app.services.executive_summary import ExecutiveSummary
    from app.services.sector_comparison import SectorNarrative

    for model in (FinancialAnalysisResponse, NewsAnalysisResponse, NewsSelections,
                  StrategicAnalysisResponse, ExecutiveSummary, SectorNarrative):
        fmt = text_format("x", model)["format"]
        assert fmt["type"] == "json_schema" and fmt["strict"] is True
        json.dumps(fmt["schema"])  # serialisable


# ----------------------------------------------------------------- requests

def test_success_uses_responses_api_background_and_reports_usage():
    client = FakeAIClient(lambda kw, n: completion(OK, usage=(10, 5), reasoning_tokens=3))
    parsed, _, diag = call(client)
    assert parsed.answer == "ok"
    request = client.requests[0]
    assert request["model"] == "gpt-5.4-pro"
    assert request["background"] is True and request["store"] is True
    assert request["max_output_tokens"] == 500
    assert request["reasoning"] == {"effort": "medium"}
    assert request["instructions"] == "s" and request["input"][0]["role"] == "user"
    assert request["text"]["format"]["name"] == "tiny"
    assert "temperature" not in request and "messages" not in request
    assert diag["status"] == "completed" and diag["profile_used"] == "full"
    assert diag["provider"] == "azure_openai" and diag["deployment"] == "gpt-5.4-pro"
    assert diag["model"] == TEST_MODEL
    assert (diag["input_tokens"], diag["output_tokens"], diag["total_tokens"]) == (10, 5, 15)
    assert diag["reasoning_tokens"] == 3
    assert diag["response_id"] == "resp_test_1"
    assert diag["token_usage_reported_by_provider"] is True


def test_missing_usage_is_not_fabricated():
    client = FakeAIClient(lambda kw, n: completion(OK, usage=None))
    _, _, diag = call(client)
    assert diag["input_tokens"] is None and diag["total_tokens"] is None
    assert diag["token_usage_reported_by_provider"] is False


def test_background_response_is_polled_until_completed():
    final = completion(OK, response_id="resp_bg")
    states = iter(["queued", "in_progress", "in_progress"])

    def retrieve(response_id):
        status = next(states, "completed")
        return final if status == "completed" else SimpleNamespace(id="resp_bg", status=status)

    created = []
    client = SimpleNamespace(responses=SimpleNamespace(
        create=lambda **kw: created.append(kw) or SimpleNamespace(id="resp_bg", status="queued"),
        retrieve=retrieve,
        cancel=lambda rid: pytest.fail("must not cancel a healthy response"),
    ))
    parsed, _, diag = call(client)
    assert parsed.answer == "ok" and diag["response_id"] == "resp_bg"
    assert len(created) == 1


def test_deadline_cancels_the_background_response():
    cancelled = []
    ticks = iter(range(0, 100000, 400))
    client = SimpleNamespace(responses=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(id="resp_slow", status="in_progress"),
        retrieve=lambda rid: SimpleNamespace(id="resp_slow", status="in_progress"),
        cancel=lambda rid: cancelled.append(rid),
    ))
    with pytest.raises(AICallFailed) as info:
        call(client, clock=lambda: next(ticks))
    assert info.value.reason == "deadline_exceeded"
    assert cancelled == ["resp_slow"]


def test_job_cancellation_cancels_the_background_response():
    cancelled = []
    flag = {"cancel": False}

    def retrieve(rid):
        flag["cancel"] = True  # the user cancels the job while it runs
        return SimpleNamespace(id="resp_c", status="in_progress")

    client = SimpleNamespace(responses=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(id="resp_c", status="queued"),
        retrieve=retrieve,
        cancel=lambda rid: cancelled.append(rid),
    ))
    with ai_provider.job_context(cancel_check=lambda: flag["cancel"]):
        with pytest.raises(AICallFailed) as info:
            call(client)
    assert info.value.reason == "cancelled"
    assert info.value.diagnostics["status"] == "cancelled"
    assert cancelled == ["resp_c"]


def test_incomplete_output_limit_moves_to_next_profile_then_fails():
    client = FakeAIClient(lambda kw, n: completion("", finish_reason="length"))
    with pytest.raises(AICallFailed) as info:
        call(client)
    assert info.value.reason == "output_token_limit"
    assert info.value.diagnostics["attempts"][0]["incomplete_reason"] == "max_output_tokens"
    assert len(client.requests) == 2


def test_content_filter_is_reported():
    client = FakeAIClient(lambda kw, n: completion("", finish_reason="content_filter"))
    with pytest.raises(AICallFailed) as info:
        call(client)
    assert info.value.reason == "content_filtered"


def test_failed_server_error_is_retried_once():
    def handler(kw, n):
        if n == 1:
            return SimpleNamespace(id="resp_f", status="failed", model=TEST_MODEL,
                                   error=SimpleNamespace(code="server_error"), output=[], usage=None)
        return completion(OK)

    client = FakeAIClient(handler)
    _, _, diag = call(client)
    assert [a["outcome"] for a in diag["attempts"]] == ["provider_failed", "success"]
    assert diag["profile_used"] == "full"


def test_unexpected_served_model_is_rejected_never_used():
    client = FakeAIClient(lambda kw, n: completion(OK, model="gpt-4o-mini-2024-07-18"))
    with pytest.raises(AICallFailed) as info:
        call(client)
    assert info.value.reason == "unexpected_model"
    assert info.value.diagnostics["model"] == "gpt-4o-mini-2024-07-18"


def test_refusal_is_not_treated_as_content():
    refused = SimpleNamespace(
        id="resp_r", status="completed", model=TEST_MODEL, usage=None, output_text="",
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal", refusal="no")])],
    )
    client = FakeAIClient(lambda kw, n: refused)
    with pytest.raises(AICallFailed) as info:
        call(client)
    assert info.value.reason == "refused"


def test_oversize_input_moves_to_smaller_profile_and_never_resends_same_request():
    def handler(kw, n):
        if n == 1:
            raise status_error(400, "context too long", body={"error": {"code": "context_length_exceeded"}})
        return completion(OK)

    client = FakeAIClient(handler)
    _, _, diag = call(client)
    assert diag["profile_used"] == "reduced"
    assert diag["fallback_reason"] == "payload_too_large"
    assert client.requests[0]["input"] != client.requests[1]["input"]
    assert client.requests[1]["max_output_tokens"] == 300


def test_budget_precheck_skips_oversized_profile_without_a_request():
    instructions, items = split_messages(build(profiles()[0]))
    full_estimate = estimate_request_tokens(instructions, items, text_format("tiny", Tiny))
    client = FakeAIClient(lambda kw, n: completion(OK))
    _, _, diag = call(client, budget=full_estimate - 1)
    assert diag["attempts"][0]["outcome"] == "skipped_over_budget"
    assert diag["profile_used"] == "reduced"
    assert len(client.requests) == 1


def test_nothing_fits_budget_fails_without_any_request():
    client = FakeAIClient(lambda kw, n: completion(OK))
    with pytest.raises(AICallFailed) as info:
        call(client, budget=50)
    assert info.value.reason == "skipped_over_budget"
    assert client.requests == []


def test_429_uses_bounded_retry_with_retry_after():
    def handler(kw, n):
        if n <= 2:
            raise status_error(429, "rate limit", {"retry-after": "1"}, cls=openai.RateLimitError)
        return completion(OK)

    client = FakeAIClient(handler)
    _, _, diag = call(client)
    assert diag["status"] == "completed"
    assert diag["rate_limit_retries"] == 2


def test_429_exhaustion_is_reported_not_retried_forever():
    client = FakeAIClient(lambda kw, n: (_ for _ in ()).throw(
        status_error(429, "rate limit", {"retry-after": "1"}, cls=openai.RateLimitError)))
    with pytest.raises(AICallFailed) as info:
        call(client)
    assert info.value.reason == "rate_limit_exhausted"
    assert len(client.requests) == ai_provider.MAX_RATE_LIMIT_RETRIES + 1


def test_timeout_retried_once_then_next_profile():
    def handler(kw, n):
        if n <= 2:
            raise openai.APITimeoutError(request=httpx.Request("POST", "https://insightiq-test.openai.azure.com"))
        return completion(OK)

    client = FakeAIClient(handler)
    _, _, diag = call(client)
    assert [a["outcome"] for a in diag["attempts"]] == ["timeout", "timeout", "success"]
    assert diag["profile_used"] == "reduced"
    assert len(client.requests) == 3  # len(profiles) + 1


def test_invalid_json_moves_to_next_profile():
    responses = [completion("{oops"), completion(OK)]
    client = FakeAIClient(lambda kw, n: responses[n - 1])
    _, _, diag = call(client)
    assert diag["attempts"][0]["outcome"] == "invalid_schema"


@pytest.mark.parametrize("code,reason", [
    (400, "bad_request"),
    (401, "authentication_failed"),
    (403, "authentication_failed"),
    (404, "deployment_unavailable"),
])
def test_configuration_errors_are_not_retried(code, reason):
    client = FakeAIClient(lambda kw, n: (_ for _ in ()).throw(
        status_error(code, "error", body={"error": {"code": "DeploymentNotFound" if code == 404 else "x"}})))
    with pytest.raises(AICallFailed) as info:
        call(client)
    assert info.value.reason == reason
    assert len(client.requests) == 1


def test_validation_rejection_moves_on_then_fails():
    def reject(parsed, profile):
        raise ResponseRejected("Unknown SEC evidence identifier: SEC_fake")

    client = FakeAIClient(lambda kw, n: completion(OK))
    with pytest.raises(AICallFailed) as info:
        call(client, validate=reject)
    assert info.value.reason == "validation_rejected"
    assert len(client.requests) == 2


def test_missing_credential_fails_without_request(monkeypatch):
    monkeypatch.setattr(ai_provider, "default_client", lambda timeout=None: None)
    with pytest.raises(AICallFailed) as info:
        structured_completion(
            agent="news", call_name="t", schema_name="tiny", schema_model=Tiny,
            build_messages=build, profiles=profiles(),
        )
    assert info.value.reason == "credential_missing"


def test_no_secret_or_provider_message_in_diagnostics_or_logs(caplog, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", SECRET)

    def handler(kw, n):
        raise status_error(400, f"key {SECRET} context too long",
                           body={"error": {"code": "context_length_exceeded"}})

    caplog.set_level(logging.DEBUG)
    client = FakeAIClient(handler)
    with pytest.raises(AICallFailed) as info:
        call(client)
    dumped = json.dumps(info.value.diagnostics)
    assert SECRET not in dumped and SECRET not in caplog.text
    assert "key " not in dumped  # provider message text is never copied


def test_rejected_responses_are_still_counted_as_billed_tokens():
    # Azure bills a response even when InsightIQ's validation rejects it: the
    # call's diagnostics must show those tokens, never None.
    def reject(parsed, profile):
        raise ResponseRejected("Expected at most four selected articles.")

    client = FakeAIClient(lambda kw, n: completion(OK, usage=(100, 40), reasoning_tokens=7))
    with pytest.raises(AICallFailed) as info:
        call(client, validate=reject)
    diag = info.value.diagnostics
    assert all(a["usage"]["input_tokens"] == 100 for a in diag["attempts"])
    assert diag["billed_tokens_all_attempts"]["responses"] == 2
    assert (diag["input_tokens"], diag["output_tokens"], diag["reasoning_tokens"]) == (200, 80, 14)
    assert diag["total_tokens"] == 280 and diag["token_usage_reported_by_provider"] is True


def test_billed_total_includes_a_rejected_attempt_before_success():
    def reject_first(parsed, profile):
        if profile.name == "full":
            raise ResponseRejected("rejected")

    client = FakeAIClient(lambda kw, n: completion(OK, usage=(100, 40)))
    _, _, diag = call(client, validate=reject_first)
    assert diag["status"] == "completed"
    assert (diag["input_tokens"], diag["output_tokens"]) == (100, 40)  # the response that was used
    assert diag["billed_tokens_all_attempts"]["input_tokens"] == 200   # what was billed
