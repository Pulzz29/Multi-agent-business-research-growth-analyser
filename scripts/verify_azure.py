"""
Controlled live LLM connectivity check (ONE small structured request, plus
at most one extra request per parameter the model rejects).

Verifies authentication, endpoint, that LLM_MODEL is deployed, the model that
actually served the request, the API used (Chat Completions or Responses),
any parameter adaptations, the response ID, token usage, structured-output
parsing and status handling, using the same code path as the research agents
(app.services.ai_provider).

Writes docs/validation_evidence/azure_connectivity_<UTC time>.json (earlier
evidence files are never overwritten). The API key is never printed or
written; the endpoint is recorded only as its host suffix.

Usage: .venv\\Scripts\\python.exe scripts\\verify_azure.py [--fallback]

--fallback sends the probe through the Groq fallback path only (needs
GROQ_API_KEY and GROQ_MODEL) and writes groq_fallback_connectivity_<UTC time>.json.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))


def main():
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(PROJECT_ROOT))

    from pydantic import BaseModel, ConfigDict

    from app.core.config import get_settings
    from app.services import ai_provider
    from app.services.ai_provider import AICallFailed, RequestProfile, structured_completion

    use_fallback = "--fallback" in sys.argv[1:]

    class Probe(BaseModel):
        model_config = ConfigDict(extra="forbid")
        status: str
        sum_of_numbers: int

    settings = get_settings()
    endpoint_host = settings.azure_openai_endpoint.split("://", 1)[1].split("/", 1)[0]
    evidence = {
        "check": "azure_openai_connectivity",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "endpoint_host_suffix": ".".join(endpoint_host.split(".")[1:]),
        "endpoint_path": "/openai/v1/",
        "model": settings.llm_model,
        "expected_model_prefix": settings.llm_expected_model_prefix or settings.llm_model,
        "reasoning_effort": settings.llm_reasoning_effort,
        "temperature": settings.llm_temperature,
        "api_setting": settings.llm_api,
    }

    def build(profile):
        return [
            {"role": "system", "content": "You are a connectivity probe. Reply only with the requested JSON."},
            {"role": "user", "content": json.dumps({
                "task": "Set status to 'ok' and sum_of_numbers to the sum of the numbers.",
                "numbers": [17, 25],
            })},
        ]

    try:
        if use_fallback:
            fallback = ai_provider.fallback_settings() or {}
            client = ai_provider.fallback_client()
            if client is None or not fallback.get("configured"):
                print("GROQ_API_KEY and GROQ_MODEL must both be set in .env first.")
                return 2
            evidence.update({"check": "groq_fallback_connectivity", "fallback_model": fallback.get("model")})
            parsed, _, diagnostics = ai_provider._run_call(
                target=ai_provider._groq_target(client, fallback),
                agent="connectivity", call_name="groq_fallback_probe",
                schema_name="insightiq_connectivity_probe", schema_model=Probe,
                build_messages=build, profiles=[RequestProfile("probe", 4000)],
                primary=AICallFailed("forced_fallback_check", {"attempts": []}),
            )
        else:
            parsed, _, diagnostics = structured_completion(
                agent="connectivity", call_name="azure_connectivity_probe",
                schema_name="insightiq_connectivity_probe", schema_model=Probe,
                build_messages=build, profiles=[RequestProfile("probe", 4000)],
            )
        evidence.update({
            "outcome": "success",
            "parsed_output": parsed.model_dump(),
            "structured_output_correct": parsed.status == "ok" and parsed.sum_of_numbers == 42,
            "diagnostics": diagnostics,
        })
    except AICallFailed as failure:
        evidence.update({
            "outcome": "failed",
            "failure_reason": failure.reason,
            "diagnostics": failure.diagnostics,
        })

    evidence["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{'groq_fallback' if use_fallback else 'azure'}_connectivity_{stamp}.json"
    out = PROJECT_ROOT / "docs" / "validation_evidence" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(evidence, indent=2, default=str)
    for secret in (settings.azure_openai_api_key, settings.groq_api_key):
        value = secret.get_secret_value() if secret else ""
        assert not value or value not in text, "refusing to write a credential"
    out.write_text(text, encoding="utf-8")

    d = evidence.get("diagnostics") or {}
    print(f"outcome={evidence['outcome']} reason={evidence.get('failure_reason')}")
    print(f"model={d.get('deployment')} model_served={d.get('model')} api={d.get('api')} "
          f"response_id={d.get('response_id')}")
    print(f"parameter adaptations={d.get('param_adaptations')}")
    print(f"tokens in/out/reasoning/total={d.get('input_tokens')}/{d.get('output_tokens')}/"
          f"{d.get('reasoning_tokens')}/{d.get('total_tokens')} elapsed={d.get('elapsed_seconds')}s")
    for attempt in d.get("attempts") or []:
        print("attempt:", {k: attempt.get(k) for k in (
            "api", "outcome", "status_code", "provider_error_code", "provider_error_param",
            "response_status", "incomplete_reason", "model")})
    print("parsed:", evidence.get("parsed_output"), "correct:", evidence.get("structured_output_correct"))
    print(f"evidence written to {out.relative_to(PROJECT_ROOT)}")
    return 0 if evidence["outcome"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
