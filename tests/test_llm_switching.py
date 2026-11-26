"""
The model is switched from .env only (LLM_MODEL): request model, startup
errors, per-model parameter adaptation, JSON retry, retries with backoff,
logging, and guards against hardcoded model names or direct SDK imports.

Test-only fake clients; no network (blocked by conftest).
"""
import json
import logging
import re
from pathlib import Path

import httpx
import openai
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from research_v2_support import FakeAIClient, completion

from app.services import ai_provider
from app.services.ai_provider import AICallFailed, RequestProfile, structured_completion

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SECRET = "TEST_SECRET_KEY_FOR_SWITCHING_DO_NOT_LOG"
OK = json.dumps({"answer": "ok"})


class Tiny(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str


def build(profile):
    return [{"role": "system", "content": "s"}, {"role": "user", "content": "u" * profile.options["size"]}]


def two_profiles():
    return [RequestProfile("full", 500, {"size": 400}), RequestProfile("reduced", 300, {"size": 40})]


def one_profile():
    return [RequestProfile("only", 500, {"size": 40})]


def run(client, profiles=None, sleep=None):
    return structured_completion(
        agent="financial", call_name="test", schema_name="tiny", schema_model=Tiny,
        build_messages=build, profiles=profiles or two_profiles(), request_token_budget=5000,
        client=client, sleep=sleep or (lambda s: None),
    )


def bad_request(param=None, code=None, message="bad request"):
    request = httpx.Request("POST", "https://insightiq-test.openai.azure.com/openai/v1/chat/completions")
    body = {"error": {"message": message, "code": code, "param": param}}
    return openai.BadRequestError(message, response=httpx.Response(400, request=request), body=body)


def status(code, headers=None, cls=openai.APIStatusError):
    request = httpx.Request("POST", "https://insightiq-test.openai.azure.com/openai/v1/chat/completions")
    return cls("error", response=httpx.Response(code, request=request, headers=headers or {}), body=None)


@pytest.fixture
def settings_env(monkeypatch):
    """Set env values and reload settings; restored after the test."""
    from app.core.config import get_settings

    def apply(**values):
        for key, value in values.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


def served(model):
    """Fake that answers as ``model`` (the served id is the model plus a version)."""
    return FakeAIClient(lambda kw, n: completion(OK, model=f"{model}-2026-01-01"))


# ------------------------------------------------------- model from .env only

def test_request_model_comes_from_llm_model_and_follows_env_changes(settings_env):
    for name in ("switch-model-a", "switch-model-b"):
        settings_env(LLM_MODEL=name)
        client = served(name)
        _, _, diag = run(client)
        assert client.chat_requests[0]["model"] == name
        assert diag["deployment"] == name and diag["model"].startswith(name)
        assert diag["api"] == "chat"


def test_older_deployment_variable_is_accepted_and_llm_model_wins(settings_env):
    from app.core.config import Settings

    settings_env(LLM_MODEL=None, AZURE_OPENAI_DEPLOYMENT="older-name")
    assert Settings().llm_model == "older-name"
    settings_env(LLM_MODEL="newer-name")
    assert Settings().llm_model == "newer-name"


@pytest.mark.parametrize("missing", ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "LLM_MODEL"])
def test_missing_required_setting_is_a_clear_startup_error(settings_env, missing):
    from app.core.config import Settings

    settings_env(**{missing: None, "AZURE_OPENAI_DEPLOYMENT": None})
    with pytest.raises(ValidationError) as info:
        Settings()
    assert missing in str(info.value)


def test_blank_model_names_the_variable_to_set(settings_env):
    from app.core.config import Settings

    settings_env(LLM_MODEL="  ")
    with pytest.raises(ValidationError) as info:
        Settings()
    assert "Set LLM_MODEL in .env" in str(info.value)


def test_run_backend_reports_missing_model_before_starting(monkeypatch, tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_backend", PROJECT_ROOT / "scripts" / "run_backend.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    with pytest.raises(SystemExit) as info:
        module.load_environment(tmp_path / "missing.env")
    assert "LLM_MODEL" in str(info.value)


# ------------------------------------------------------ parameter adaptation

def test_max_completion_tokens_rejected_switches_to_max_tokens_and_is_remembered():
    def handler(kw, n):
        if n == 1:
            raise bad_request("max_completion_tokens", "unsupported_parameter",
                              "Unsupported parameter: 'max_completion_tokens'. Use 'max_tokens' instead.")
        return completion(OK)

    client = FakeAIClient(handler)
    _, _, diag = run(client)
    assert "max_completion_tokens" in client.chat_requests[0]
    assert client.chat_requests[1]["max_tokens"] == 500 and "max_completion_tokens" not in client.chat_requests[1]
    assert diag["param_adaptations"] == ["token_param:max_tokens"]
    assert diag["provider_requests"] == 1 and diag["profile_used"] == "full"

    later = served("test-model")
    run(later)
    assert "max_tokens" in later.chat_requests[0]  # learned for this model


def test_temperature_rejected_is_dropped_and_remembered():
    def handler(kw, n):
        if "temperature" in kw:
            raise bad_request("temperature", "unsupported_value",
                              "Unsupported value: 'temperature' does not support 0.2 with this model.")
        return completion(OK)

    client = FakeAIClient(handler)
    _, _, diag = run(client)
    assert client.chat_requests[0]["temperature"] == 0.2
    assert "temperature" not in client.chat_requests[1]
    assert diag["param_adaptations"] == ["drop:temperature"]
    later = served("test-model")
    run(later)
    assert "temperature" not in later.chat_requests[0]


def test_reasoning_effort_is_sent_only_when_configured(settings_env):
    client = served("test-model")
    run(client)
    assert "reasoning_effort" not in client.chat_requests[0]
    assert client.chat_requests[0]["temperature"] == 0.2

    settings_env(LLM_REASONING_EFFORT="low")
    client = served("test-model")
    run(client)
    assert client.chat_requests[0]["reasoning_effort"] == "low"
    assert "temperature" not in client.chat_requests[0]  # reasoning models take no sampling parameters


def test_rejected_reasoning_effort_is_dropped(settings_env):
    settings_env(LLM_REASONING_EFFORT="medium")

    def handler(kw, n):
        if "reasoning" in kw:
            raise bad_request(None, None, "Unrecognized request argument supplied: reasoning_effort")
        return completion(OK)

    client = FakeAIClient(handler)
    _, _, diag = run(client)
    assert "reasoning_effort" not in client.chat_requests[1]
    assert diag["param_adaptations"] == ["drop:reasoning"]


def test_output_cap_above_the_model_limit_is_lowered_to_the_reported_limit():
    def handler(kw, n):
        if kw["max_output_tokens"] > 256:
            raise bad_request("max_tokens", "invalid_value",
                              "max_tokens is too large: 500. This model supports at most 256 completion tokens.")
        return completion(OK)

    client = FakeAIClient(handler)
    _, _, diag = run(client)
    assert client.chat_requests[1]["max_completion_tokens"] == 256
    assert diag["max_output_tokens"] == 256 and diag["param_adaptations"] == ["output_limit:256"]


def test_responses_only_model_switches_to_the_responses_api():
    def handler(kw, n):
        if "background" not in kw and n == 1:
            raise bad_request(None, "OperationNotSupported",
                              "The chat completions operation does not work with the specified model.")
        return completion(OK)

    client = FakeAIClient(handler)
    _, _, diag = run(client)
    assert len(client.chat_requests) == 1
    assert client.requests[1]["background"] is True and client.requests[1]["store"] is True
    assert diag["api"] == "responses" and diag["param_adaptations"] == ["api:responses"]


def test_forced_chat_api_does_not_switch(settings_env):
    settings_env(LLM_API="chat")
    client = FakeAIClient(lambda kw, n: (_ for _ in ()).throw(
        bad_request(None, "OperationNotSupported", "The chat completions operation does not work.")))
    with pytest.raises(AICallFailed) as info:
        run(client)
    assert info.value.reason == "bad_request" and len(client.requests) == 1


def test_unsupported_json_schema_falls_back_to_json_mode():
    def handler(kw, n):
        if kw["text"]["format"].get("type") == "json_schema":
            raise bad_request("response_format", "invalid_value", "response_format json_schema is not supported")
        return completion(OK)

    client = FakeAIClient(handler)
    parsed, _, diag = run(client)
    assert parsed.answer == "ok"
    second = client.chat_requests[1]
    assert second["response_format"] == {"type": "json_object"}
    assert second["messages"][0]["role"] == "system" and '"answer"' in second["messages"][0]["content"]
    assert diag["param_adaptations"] == ["json_mode:json_object"]


def test_an_unrelated_bad_request_is_not_adapted():
    client = FakeAIClient(lambda kw, n: (_ for _ in ()).throw(bad_request(None, "x", "something else")))
    with pytest.raises(AICallFailed) as info:
        run(client)
    assert info.value.reason == "bad_request" and len(client.requests) == 1


# ------------------------------------------------------------------- JSON

def test_valid_json_is_parsed():
    parsed, _, diag = run(served("test-model"), profiles=one_profile())
    assert parsed.answer == "ok" and diag["status"] == "completed"


def test_invalid_json_is_retried_once_then_succeeds():
    answers = iter([completion("{not json"), completion(OK)])
    client = FakeAIClient(lambda kw, n: next(answers))
    _, _, diag = run(client, profiles=one_profile())
    assert [a["outcome"] for a in diag["attempts"]] == ["invalid_schema", "success"]
    assert client.chat_requests[0]["messages"] == client.chat_requests[1]["messages"]


def test_invalid_json_twice_is_a_clean_failure():
    client = FakeAIClient(lambda kw, n: completion("{still not json"))
    with pytest.raises(AICallFailed) as info:
        run(client, profiles=one_profile())
    assert info.value.reason == "invalid_schema" and len(client.requests) == 2


# ---------------------------------------------------------------- retries

def test_rate_limit_is_retried_with_exponential_backoff_then_fails_cleanly():
    waits = []
    client = FakeAIClient(lambda kw, n: (_ for _ in ()).throw(status(429, cls=openai.RateLimitError)))
    with pytest.raises(AICallFailed) as info:
        run(client, sleep=waits.append)
    assert info.value.reason == "rate_limit_exhausted"
    assert waits == [2.0, 4.0, 8.0]  # LLM_MAX_RETRIES defaults to 3
    assert len(client.requests) == 4


def test_retry_after_header_is_honoured():
    waits = []

    def handler(kw, n):
        if n == 1:
            raise status(429, {"retry-after": "7"}, cls=openai.RateLimitError)
        return completion(OK)

    _, _, diag = run(FakeAIClient(handler), sleep=waits.append)
    assert waits == [7.0] and diag["rate_limit_retries"] == 1


def test_server_errors_and_timeouts_retry_up_to_llm_max_retries(settings_env):
    settings_env(LLM_MAX_RETRIES="1")
    client = FakeAIClient(lambda kw, n: (_ for _ in ()).throw(status(503)))
    with pytest.raises(AICallFailed) as info:
        run(client)
    assert info.value.reason == "provider_server_error" and len(client.requests) == 2

    timeout = openai.APITimeoutError(request=httpx.Request("POST", "https://insightiq-test.openai.azure.com"))
    client = FakeAIClient(lambda kw, n: (_ for _ in ()).throw(timeout))
    with pytest.raises(AICallFailed) as info:
        run(client)
    assert info.value.reason == "timeout" and len(client.requests) == 2


def test_each_request_carries_a_timeout_within_the_configured_limit(settings_env):
    settings_env(LLM_TIMEOUT_SECONDS="45")
    seen = []

    class Recording(FakeAIClient):
        pass

    client = Recording(lambda kw, n: completion(OK))
    original = client.chat.completions.create

    def create(**kwargs):
        seen.append(kwargs.pop("timeout"))
        return original(**kwargs)

    client.chat.completions.create = create
    run(client)
    assert seen and 1.0 <= seen[0] <= 45.0


# ---------------------------------------------------------------- logging

def test_each_call_logs_model_latency_and_tokens_but_never_the_key(settings_env, caplog):
    settings_env(AZURE_OPENAI_API_KEY=SECRET)
    caplog.set_level(logging.INFO, logger="insightiq.ai")
    run(served("test-model"))
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("ai_call "))
    record = json.loads(line[len("ai_call "):])
    assert record["deployment"] == "test-model" and record["model"].startswith("test-model")
    assert record["elapsed_seconds"] is not None and record["attempt_seconds"]
    assert record["input_tokens"] == 1200 and record["output_tokens"] == 900
    assert SECRET not in caplog.text


# ------------------------------------------------------------------ guards

# Built from fragments so this file does not match its own patterns.
_MODEL_NAME = re.compile(
    r"\b(" + "g" + r"pt-?\d[\w.\-]*|" + "g" + r"pt-4o[\w.\-]*|" + "g" + r"pt-oss[\w.\-]*|"
    r"o[1-4]-(?:mini|pro|preview)\b|" + "lla" + r"ma-?\d[\w.\-]*|" + "mix" + r"tral[\w.\-]*|"
    + "gem" + r"ini-[\w.\-]+|" + "cla" + r"ude-[\w.\-]+)",
    re.I,
)
# Where model names may appear: .env.example and README_INSIGHTIQ.md (defaults and
# examples), and historical records of what actually ran (never rewritten).
_GUARDED = [
    ("backend", "*.py"), ("scripts", "*.py"), ("tests", "*.py"),
    ("frontend/src", "*.js"), ("frontend/src", "*.jsx"),
    ("frontend/tests", "*.mjs"), ("frontend/tests", "*.json"),
]
_GUARDED_FILES = ["README.md", "DEPLOYMENT.md", "docs/DEMO_RUNBOOK.md", "deployment_manifest.json"]


def _guarded_files():
    for folder, pattern in _GUARDED:
        for path in (PROJECT_ROOT / folder).rglob(pattern):
            if "node_modules" not in path.parts and "__pycache__" not in path.parts:
                yield path
    for name in _GUARDED_FILES:
        if (PROJECT_ROOT / name).is_file():
            yield PROJECT_ROOT / name


def test_no_hardcoded_model_names_outside_env_example_and_readme():
    offenders = []
    for path in _guarded_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), 1):
            match = _MODEL_NAME.search(line)
            if match:
                offenders.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{number}: {match.group(0)}")
    assert not offenders, "\n".join(offenders)


def test_only_the_central_module_imports_an_llm_sdk():
    central = PROJECT_ROOT / "backend" / "app" / "services" / "ai_provider.py"
    pattern = re.compile(r"^\s*(?:import|from)\s+(openai|groq|anthropic)\b", re.M)
    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for folder in ("backend", "scripts")
        for path in (PROJECT_ROOT / folder).rglob("*.py")
        if path != central and pattern.search(path.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert not offenders, offenders
