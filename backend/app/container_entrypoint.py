"""
Container / Kubernetes entry point for the API.

1. Applies the PostgreSQL schema migrations (idempotent; advisory-locked, so
   several pods may start together) and resets research jobs interrupted by
   a previous shutdown.
2. Starts uvicorn with host, trusted-proxy and optional TLS settings from the
   environment (INSIGHTIQ_API_HOST, TRUSTED_PROXY_IPS, SSL_CERTFILE/KEYFILE).

The SQLite-file runtime steps of app.server_entrypoint (temp-folder
realignment, file-backup restore) do not apply in containers.

    python -m app.container_entrypoint            # API
    python -m app.container_entrypoint --migrate  # migrations only (Kubernetes Job)
"""

import os
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    from app.core.security import install_redaction, uvicorn_options
    from app.database import db

    install_redaction()
    if not db.is_postgres():
        raise SystemExit("Containers require DB_BACKEND=postgres.")
    from app.server_entrypoint import _start_postgres_runtime

    if "--migrate" in argv:
        from app.database.migrations import run_migrations

        print("Schema migrations applied now:", run_migrations() or "none")
        return 0
    _start_postgres_runtime()

    import uvicorn

    port = int(os.getenv("INSIGHTIQ_API_PORT", "8000"))
    options = uvicorn_options()
    options["host"] = os.getenv("INSIGHTIQ_API_HOST", "0.0.0.0")
    # One worker per pod: research jobs and their cancellation run in-process.
    uvicorn.run("app.main:app", port=port, workers=1, reload=False, **options)
    return 0


if __name__ == "__main__":
    sys.exit(main())
