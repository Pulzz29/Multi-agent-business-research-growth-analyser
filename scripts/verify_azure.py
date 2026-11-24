"""
Controlled live Azure OpenAI connectivity check (ONE small structured request).

Verifies authentication, endpoint, deployment availability, the model that
actually served the request, the response ID, token usage (including
reasoning tokens), structured-output parsing and status handling, using the
same code path as the research agents (app.services.ai_provider).

Writes docs/validation_evidence/azure_connectivity.json. The API key is never
printed or written; the endpoint is recorded only as its host suffix.

Usage: .venv\\Scripts\\python.exe scripts\\verify_azure.py
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
    from app.services.ai_provider import AICallFailed, RequestProfile, structured_completion

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
        "deployment": settings.azure_openai_deployment,
        "expected_model_prefix": settings.azure_expected_model_prefix or settings.azure_openai_deployment,
        "reasoning_effort": settings.azure_reasoning_effort,
        "api": "Responses API, background mode with polling",
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
    out = PROJECT_ROOT / "docs" / "validation_evidence" / "azure_connectivity.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(evidence, indent=2, default=str)
    key = settings.azure_openai_api_key.get_secret_value()
    assert key not in text, "refusing to write a credential"
    out.write_text(text, encoding="utf-8")

    d = evidence.get("diagnostics") or {}
    print(f"outcome={evidence['outcome']} reason={evidence.get('failure_reason')}")
    print(f"deployment={d.get('deployment')} model_served={d.get('model')} response_id={d.get('response_id')}")
    print(f"tokens in/out/reasoning/total={d.get('input_tokens')}/{d.get('output_tokens')}/"
          f"{d.get('reasoning_tokens')}/{d.get('total_tokens')} elapsed={d.get('elapsed_seconds')}s")
    for attempt in d.get("attempts") or []:
        print("attempt:", {k: attempt.get(k) for k in (
            "outcome", "status_code", "provider_error_code", "provider_error_param",
            "response_status", "incomplete_reason", "model")})
    print("parsed:", evidence.get("parsed_output"), "correct:", evidence.get("structured_output_correct"))
    print(f"evidence written to {out.relative_to(PROJECT_ROOT)}")
    return 0 if evidence["outcome"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
