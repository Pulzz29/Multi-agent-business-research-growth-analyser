"""
Groq fallback: used only when Azure OpenAI fails because its key is missing or
rejected or the deployment is not found, and only when GROQ_API_KEY and
GROQ_MODEL are set.
The same request, schema and caller validation are used. Fake clients only.
"""
import json
import logging
from types import SimpleNamespace

import httpx
import openai
import pytest
from pydantic import BaseModel, ConfigDict

from research_v2_support import FakeAIClient, completion

from app.services import ai_provider
from app.services.ai_provider import AICallFailed, RequestProfile, ResponseRejected, structured_completion

GROQ_SECRET = "TEST_ONLY_GROQ_KEY_DO_NOT_LOG"
GROQ_MODEL = "test-fallback-model"
OK = json.dumps({"answer": "ok"})


class Tiny(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str


def profiles():
    return [RequestProfile("full", 500, {"size": 40}), RequestProfile("reduced", 300, {"size": 5})]


def build(profile):
    return [
        {"role": "system", "content": "s"},
        {"role": "user", "content": json.dumps({"rows": ["x" * 50] * profile.options["size"]})},
    ]


def status_error(code, body=None):
    request = httpx.Request("POST", "https://example.invalid/v1")
    return openai.APIStatusError("error", response=httpx.Response(code, request=request), body=body)


def azure_failing(code):
    def handler(kwargs, n):
        raise status_error(code)
    return FakeAIClient(handler)


def chat(content=OK, model=GROQ_MODEL, finish_reason="stop", refusal=None):
    return SimpleNamespace(
        id="chatcmpl_test", model=model,
        choices=[SimpleNamespace(finish_reason=finish_reason,
                                 message=SimpleNamespace(content=content, refusal=refusal))],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=40, total_tokens=160,
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
                              prompt_tokens_details=None),
    )


class FakeGroq:
    def __init__(self, handler):
        self.requests = []

        def create(**kwargs):
            self.requests.append(kwargs)
            result = handler(kwargs, len(self.requests))
            if isinstance(result, Exception):
                raise result
            return result

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


@pytest.fixture
def groq_configured(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("GROQ_API_KEY", GROQ_SECRET)
    monkeypatch.setenv("GROQ_MODEL", GROQ_MODEL)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def run(azure, groq=None, validate=None):
    return structured_completion(
        agent="financial", call_name="test", schema_name="tiny", schema_model=Tiny,
        build_messages=build, profiles=profiles(), validate=validate,
        request_token_budget=5000, client=azure, groq_client=groq, sleep=lambda s: None,
    )


@pytest.mark.parametrize("code,reason", [(401, "authentication_failed"), (403, "authentication_failed"),
                                         (404, "deployment_unavailable")])
def test_azure_key_or_deployment_failure_falls_back_to_groq(groq_configured, code, reason):
    groq = FakeGroq(lambda kw, n: chat())
    parsed, _, diag = run(azure_failing(code), groq)
    assert parsed.answer == "ok"
    assert diag["provider"] == "groq" and diag["model"] == GROQ_MODEL
    assert diag["fallback_from"] == {"provider": "azure_openai", "reason": reason}
    assert diag["primary_attempts"][0]["status_code"] == code
    assert diag["input_tokens"] == 120 and diag["reasoning_tokens"] == 12
    request = groq.requests[0]
    assert request["model"] == GROQ_MODEL
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["response_format"]["json_schema"]["name"] == "tiny"


def test_missing_azure_client_falls_back(groq_configured, monkeypatch):
    monkeypatch.setattr(ai_provider, "default_client", lambda timeout=None: None)
    _, _, diag = run(None, FakeGroq(lambda kw, n: chat()))
    assert diag["fallback_from"]["reason"] == "credential_missing"


@pytest.mark.parametrize("azure_handler,reason", [
    (lambda kw, n: (_ for _ in ()).throw(status_error(400)), "bad_request"),
    (lambda kw, n: (_ for _ in ()).throw(status_error(500)), "provider_server_error"),
    (lambda kw, n: completion("{not json"), "invalid_schema"),
])
def test_other_azure_failures_do_not_fall_back(groq_configured, azure_handler, reason):
    groq = FakeGroq(lambda kw, n: chat())
    with pytest.raises(AICallFailed) as info:
        run(FakeAIClient(azure_handler), groq)
    assert info.value.reason == reason
    assert groq.requests == []


def test_validation_rejection_on_azure_does_not_fall_back(groq_configured):
    groq = FakeGroq(lambda kw, n: chat())

    def reject(parsed, profile):
        raise ResponseRejected("bad figure")

    with pytest.raises(AICallFailed) as info:
        run(FakeAIClient(lambda kw, n: completion(OK)), groq, validate=reject)
    assert info.value.reason == "validation_rejected" and groq.requests == []


def test_no_groq_key_keeps_original_azure_failure(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(AICallFailed) as info:
        run(azure_failing(401))
    assert info.value.reason == "authentication_failed"
    assert "fallback_from" not in info.value.diagnostics


def test_groq_uses_the_same_validation(groq_configured):
    seen = []

    def validate(parsed, profile):
        seen.append(profile.name)
        if profile.name == "full":
            raise ResponseRejected("unknown evidence id")
        return "validated"

    groq = FakeGroq(lambda kw, n: chat())
    _, validated, diag = run(azure_failing(401), groq, validate=validate)
    assert seen == ["full", "reduced"] and validated == "validated"
    assert [a["outcome"] for a in diag["attempts"]] == ["validation_rejected", "success"]


def test_groq_failure_reports_both_providers(groq_configured):
    groq = FakeGroq(lambda kw, n: status_error(401))
    with pytest.raises(AICallFailed) as info:
        run(azure_failing(401), groq)
    assert info.value.reason == "authentication_failed"
    assert info.value.diagnostics["provider"] == "groq"
    assert info.value.diagnostics["fallback_from"]["reason"] == "authentication_failed"


def test_groq_wrong_model_is_rejected(groq_configured):
    with pytest.raises(AICallFailed) as info:
        run(azure_failing(401), FakeGroq(lambda kw, n: chat(model="other-model-8b")))
    assert info.value.reason == "unexpected_model"


def test_groq_413_and_json_failures_move_to_next_profile(groq_configured):
    responses = iter([
        status_error(413),
        chat(),
    ])
    groq = FakeGroq(lambda kw, n: next(responses))
    _, _, diag = run(azure_failing(404), groq)
    assert diag["profile_used"] == "reduced"

    responses2 = iter([status_error(400, body={"error": {"code": "json_validate_failed"}}), chat()])
    _, _, diag2 = run(azure_failing(404), FakeGroq(lambda kw, n: next(responses2)))
    assert [a["outcome"] for a in diag2["attempts"]] == ["invalid_schema", "success"]


def test_groq_output_limit_and_budget(groq_configured, monkeypatch):
    groq = FakeGroq(lambda kw, n: chat(finish_reason="length") if n == 1 else chat())
    _, _, diag = run(azure_failing(401), groq)
    assert diag["attempts"][0]["outcome"] == "output_token_limit"
    assert all(r["max_completion_tokens"] <= 8000 for r in groq.requests)

    monkeypatch.setenv("INSIGHTIQ_GROQ_INPUT_TOKEN_BUDGET", "1000")
    from app.core.config import get_settings
    get_settings.cache_clear()
    small = FakeGroq(lambda kw, n: chat())
    _, _, diag = structured_completion(
        agent="financial", call_name="test", schema_name="tiny", schema_model=Tiny,
        build_messages=build, request_token_budget=50000, client=azure_failing(401),
        profiles=[RequestProfile("full", 500, {"size": 200}), RequestProfile("reduced", 300, {"size": 5})],
        groq_client=small, sleep=lambda s: None,
    )
    assert diag["attempts"][0]["outcome"] == "skipped_over_budget"
    assert diag["profile_used"] == "reduced" and len(small.requests) == 1


def test_groq_key_never_in_diagnostics_or_logs(groq_configured, caplog):
    caplog.set_level(logging.INFO, logger="insightiq.ai")
    _, _, diag = run(azure_failing(401), FakeGroq(lambda kw, n: chat()))
    assert GROQ_SECRET not in json.dumps(diag, default=str)
    assert GROQ_SECRET not in caplog.text
    assert '"provider": "groq"' in caplog.text


def test_default_fallback_client_targets_groq(groq_configured):
    client = ai_provider.fallback_client()
    assert isinstance(client, openai.OpenAI)
    assert str(client.base_url).rstrip("/") == "https://api.groq.com/openai/v1"
    assert client.max_retries == 0
    assert ai_provider.fallback_settings()["configured"] is True


def test_research_run_completes_on_groq_when_azure_key_is_rejected(groq_configured, monkeypatch, isolated_runtime):
    from research_v2_support import default_handler

    azure = azure_failing(401)
    monkeypatch.setattr(ai_provider, "default_client", lambda timeout=None: azure)
    # The shared fake's realistic answers, served under the fallback model's name.
    groq = FakeAIClient(lambda kwargs, n: _renamed(default_handler(kwargs, n), GROQ_MODEL))
    monkeypatch.setattr(ai_provider, "fallback_client", lambda timeout=None: groq)
    monkeypatch.setattr(ai_provider, "GROQ_BASE_URL", "https://example.invalid")
    from app.core.config import get_settings
    monkeypatch.setenv("INSIGHTIQ_GROQ_INPUT_TOKEN_BUDGET", "200000")
    get_settings.cache_clear()

    from app.agents.research_workflow import run_research_workflow

    report = run_research_workflow("AAPL", options={"refresh": "skip"})
    assert report["status"] == "completed"
    assert report["generation"]["provider"] == "groq"
    assert any("Groq fallback" in item for item in report["limitations"])
    assert all(c.get("fallback_from", {}).get("reason") == "authentication_failed"
               for c in report["generation"]["calls"] if c.get("model"))


def _renamed(response, model):
    response.model = model
    return response


def test_groq_key_without_model_is_a_clear_startup_error(monkeypatch):
    from pydantic import ValidationError

    from app.core.config import Settings

    monkeypatch.setenv("GROQ_API_KEY", GROQ_SECRET)
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    with pytest.raises(ValidationError) as info:
        Settings()
    assert "GROQ_MODEL" in str(info.value) and GROQ_SECRET not in str(info.value)


from research_v2_support import isolated_runtime  # noqa: E402,F401  (fixture import)
