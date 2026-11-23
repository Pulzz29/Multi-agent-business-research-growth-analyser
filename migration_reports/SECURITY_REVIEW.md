# Security review

## Authentication (preserved, not weakened)

- Every `/api/v1/*` route requires `Authorization: Bearer <API_ACCESS_KEY>`, compared with `hmac.compare_digest`. Tests confirm 401 with no key, a wrong key or the wrong scheme, including on both POST routes.
- If `API_ACCESS_KEY` is unset, the API rejects every request (fail-closed), and `scripts/run_backend.py` refuses to start.
- `/docs`, `/redoc` and `/openapi.json` are disabled (404).
- The server binds to `127.0.0.1` only (`server_entrypoint.py`), so it is not reachable from the network.

## Credentials

| Item | Status |
|---|---|
| Hardcoded secrets in source, reports, scripts, bundle | None found (pattern scan for Groq `gsk_`/NewsData `pub_` keys and the local access keys) |
| `.env` | Created with a locally generated random `API_ACCESS_KEY` (48 bytes, `secrets.token_urlsafe`). Provider keys are blank, for you to fill. ACL limited to your account. Git-ignored. |
| `.env.test` | Separate random access key; provider keys are deliberately invalid placeholders, so an accidental provider call fails instead of consuming quota. Git-ignored. |
| Insecure fallbacks | None added. `config.py` still requires all four variables, and the SEC identity validator still requires an email. |
| Colab Secrets / getpass / ipywidgets key helper | Replaced by `.env` loading in `run_backend.py`; the app reads environment variables exactly as before |
| Values printed or logged | No secret values were printed during migration. Reading the test key to type it into the browser was blocked by the permission system, and I did not work around it. |
| Frontend | Key is kept in React state only, never in local/session storage; the field is `type=password` |

## Input validation and error disclosure

- Ticker allowlists on job submission (Pydantic `Literal`, `extra="forbid"`), news refresh and news status; canonical UUID checks on run IDs.
- Report retrieval reads only `<project>/reports/research_runs/<run_id>.json` and re-checks the document identity. The migration's path-rebasing helper only maps the exact legacy Colab root, so arbitrary stored paths are still rejected.
- Backup paths must stay inside `data/sqlite_backups`, which is unchanged.
- Error bodies contain no tracebacks, SQLite errors or filesystem paths (tested).

## Dependencies

- `npm audit`: 0 vulnerabilities (all and production-only).
- `pip check`: clean. A Python CVE scan (`pip-audit`) was **not run**, because the tool is not installed and installing new tooling was out of scope. Optional: `.venv\Scripts\pip install pip-audit; .venv\Scripts\pip-audit`.
- npm reported that `esbuild`'s postinstall script was not run (npm allow-scripts policy). The build still works, using the prebuilt platform binary.

## Observations / recommendations (not changed)

1. The API key is a single shared bearer secret with no rotation or expiry. That's fine for a local single-user tool; consider per-user auth before any hosting.
2. No CORS middleware is configured. That is correct for proxy-only local use; keep it that way unless the frontend is served from another origin.
3. Before any public deployment, add rate limiting beyond the existing news cooldown and research queue cap, and use TLS. Nothing was published.
