"""
Bounded Groq request handling: budgets, HTTP 413 / 429, timeouts, invalid
output, attempt limits and diagnostics hygiene. Test-only fake clients.
"""
import json
import logging

import httpx
import pytest
from groq import APIStatusError, APITimeoutError, BadRequestError, RateLimitError
from pydantic import BaseModel, ConfigDict

from research_v2_support import FakeGroqClient, completion

from app.services import groq_rate_limit
from app.services.groq_structured import (
    GroqCallFailed,
    RequestProfile,
    ResponseRejected,
    estimate_request_tokens,
    structured_completion,
)

SECRET = "gsk_TEST_SECRET_VALUE_DO_NOT_LOG"


class Tiny(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str


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


def status_error(cls, code, message="error", headers=None):
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(code, request=request, headers=headers or {})
    return cls(message, response=response, body=None)


def call(client, budget=5000, validate=None):
    return structured_completion(
        agent="financial", call_name="test", schema_name="tiny",
        schema_model=Tiny, build_messages=build, profiles=profiles(),
        validate=validate, request_token_budget=budget, client=client,
        sleep=lambda s: None,
    )


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(groq_rate_limit.time, "sleep", lambda s: None)
    monkeypatch.setenv("GROQ_API_KEY", SECRET)


def test_success_reports_provider_usage():
    client = FakeGroqClient(lambda kw, n: completion('{"answer":"ok"}', usage=(10, 5)))
    parsed, _, diag = call(client)
    assert parsed.answer == "ok"
    assert diag["status"] == "completed" and diag["profile_used"] == "full"
    assert (diag["prompt_tokens"], diag["completion_tokens"], diag["total_tokens"]) == (10, 5, 15)
    assert diag["token_usage_reported_by_provider"] is True
    assert client.requests[0]["max_completion_tokens"] == 500
    assert client.requests[0]["temperature"] == 0


def test_missing_usage_is_not_fabricated():
    client = FakeGroqClient(lambda kw, n: completion('{"answer":"ok"}', usage=None))
    _, _, diag = call(client)
    assert diag["prompt_tokens"] is None and diag["total_tokens"] is None
    assert diag["token_usage_reported_by_provider"] is False


def test_http_413_moves_to_smaller_profile_and_never_resends_same_request():
    def handler(kw, n):
        if n == 1:
            raise status_error(APIStatusError, 413,
                               "Request too large: Limit 8000, Requested 9100")
        return completion('{"answer":"ok"}')

    client = FakeGroqClient(handler)
    _, _, diag = call(client)
    assert diag["profile_used"] == "reduced"
    assert diag["fallback_reason"] == "payload_too_large"
    first = diag["attempts"][0]
    assert first["status_code"] == 413
    assert (first["provider_limit_tokens"], first["provider_requested_tokens"]) == (8000, 9100)
    assert client.requests[0]["messages"] != client.requests[1]["messages"]
    assert client.requests[1]["max_completion_tokens"] == 300


def test_repeated_413_stops_after_bounded_attempts():
    client = FakeGroqClient(lambda kw, n: (_ for _ in ()).throw(
        status_error(APIStatusError, 413, "too large")))
    with pytest.raises(GroqCallFailed) as info:
        call(client)
    assert info.value.reason == "payload_too_large"
    assert len(client.requests) == 2


def test_budget_precheck_skips_oversized_profile_without_a_request():
    full_estimate = estimate_request_tokens(build(profiles()[0]), {"x": 1})
    client = FakeGroqClient(lambda kw, n: completion('{"answer":"ok"}'))
    # Budget fits only the reduced profile.
    _, _, diag = call(client, budget=full_estimate + 200)
    assert diag["attempts"][0]["outcome"] == "skipped_over_budget"
    assert diag["profile_used"] == "reduced"
    assert len(client.requests) == 1


def test_nothing_fits_budget_fails_without_any_request():
    client = FakeGroqClient(lambda kw, n: completion('{"answer":"ok"}'))
    with pytest.raises(GroqCallFailed) as info:
        call(client, budget=50)
    assert info.value.reason == "skipped_over_budget"
    assert client.requests == []


def test_429_uses_bounded_retry_with_provider_guidance():
    def handler(kw, n):
        if n <= 2:
            raise status_error(RateLimitError, 429, "rate limit", {"retry-after": "1"})
        return completion('{"answer":"ok"}')

    client = FakeGroqClient(handler)
    _, _, diag = call(client)
    assert diag["status"] == "completed"
    assert diag["rate_limit_retries"] == 2


def test_429_exhaustion_is_reported_not_retried_forever():
    client = FakeGroqClient(lambda kw, n: (_ for _ in ()).throw(
        status_error(RateLimitError, 429, "rate limit", {"retry-after": "1"})))
    with pytest.raises(GroqCallFailed) as info:
        call(client)
    assert info.value.reason == "rate_limit_exhausted"
    assert len(client.requests) == groq_rate_limit.MAX_RETRIES + 1


def test_timeout_retried_once_then_next_profile():
    def handler(kw, n):
        if n <= 2:
            raise APITimeoutError(request=httpx.Request("POST", "https://api.groq.com"))
        return completion('{"answer":"ok"}')

    client = FakeGroqClient(handler)
    _, _, diag = call(client)
    assert [a["outcome"] for a in diag["attempts"]] == ["timeout", "timeout", "success"]
    assert diag["profile_used"] == "reduced"
    assert len(client.requests) == 3  # len(profiles) + 1


def test_invalid_json_and_length_move_to_next_profile():
    responses = [completion("{oops"), completion('{"answer":"ok"}')]
    client = FakeGroqClient(lambda kw, n: responses[n - 1])
    _, _, diag = call(client)
    assert diag["attempts"][0]["outcome"] == "invalid_schema"

    responses2 = [completion("", finish_reason="length"), completion("", finish_reason="length")]
    client2 = FakeGroqClient(lambda kw, n: responses2[n - 1])
    with pytest.raises(GroqCallFailed) as info:
        call(client2)
    assert info.value.reason == "output_token_limit"


def test_bad_request_is_not_retried():
    client = FakeGroqClient(lambda kw, n: (_ for _ in ()).throw(
        status_error(BadRequestError, 400, "bad request")))
    with pytest.raises(GroqCallFailed) as info:
        call(client)
    assert info.value.reason == "bad_request"
    assert len(client.requests) == 1


def test_validation_rejection_moves_on_then_fails():
    def reject(parsed, profile):
        raise ResponseRejected("Unknown SEC evidence identifier: SEC_fake")

    client = FakeGroqClient(lambda kw, n: completion('{"answer":"ok"}'))
    with pytest.raises(GroqCallFailed) as info:
        call(client, validate=reject)
    assert info.value.reason == "validation_rejected"
    assert len(client.requests) == 2


def test_missing_credential_fails_without_request(monkeypatch):
    from app.services import groq_structured

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(GroqCallFailed) as info:
        structured_completion(
            agent="news", call_name="t", schema_name="tiny", schema_model=Tiny,
            build_messages=build, profiles=profiles(),
        )
    assert info.value.reason == "credential_missing"


def test_no_secret_or_provider_message_in_diagnostics_or_logs(caplog):
    def handler(kw, n):
        raise status_error(APIStatusError, 413, f"key {SECRET} Limit 8000, Requested 9000")

    caplog.set_level(logging.DEBUG)
    client = FakeGroqClient(handler)
    with pytest.raises(GroqCallFailed) as info:
        call(client)
    dumped = json.dumps(info.value.diagnostics)
    assert SECRET not in dumped and SECRET not in caplog.text
    assert "key " not in dumped  # provider message text is never copied
