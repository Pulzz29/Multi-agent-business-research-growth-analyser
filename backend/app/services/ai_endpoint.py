"""
Azure OpenAI endpoint handling (no network, no settings import).

The v1 API is served at ``https://<resource>.openai.azure.com/openai/v1/``
(Microsoft Foundry resources may also use ``*.cognitiveservices.azure.com``
or ``*.services.ai.azure.com``). Foundry shows the resource endpoint with or
without the ``/openai/v1`` suffix; both are accepted and normalised. Legacy
deployment-scoped URLs (``/openai/deployments/...``) and ``api-version``
query strings belong to the older API and are rejected with a clear message.
"""

from urllib.parse import urlsplit

AZURE_HOST_SUFFIXES = (
    ".openai.azure.com",
    ".cognitiveservices.azure.com",
    ".services.ai.azure.com",
)


class EndpointError(ValueError):
    pass


def normalise_endpoint(value):
    text = str(value or "").strip()
    if not text:
        raise EndpointError("AZURE_OPENAI_ENDPOINT is empty.")
    parts = urlsplit(text)
    if parts.scheme != "https":
        raise EndpointError("AZURE_OPENAI_ENDPOINT must start with https://.")
    host = (parts.hostname or "").lower()
    if not host or not host.endswith(AZURE_HOST_SUFFIXES):
        raise EndpointError(
            "AZURE_OPENAI_ENDPOINT must be an Azure OpenAI resource endpoint "
            "(…openai.azure.com, …cognitiveservices.azure.com or …services.ai.azure.com)."
        )
    if parts.query or parts.fragment:
        raise EndpointError(
            "AZURE_OPENAI_ENDPOINT must not contain a query string such as api-version; "
            "the v1 API does not use one."
        )
    path = parts.path.rstrip("/")
    if path not in ("", "/openai", "/openai/v1"):
        raise EndpointError(
            "AZURE_OPENAI_ENDPOINT must be the resource endpoint, optionally ending in /openai/v1 "
            "(not a deployment-specific URL)."
        )
    port = f":{parts.port}" if parts.port else ""
    return f"https://{host}{port}/openai/v1/"
