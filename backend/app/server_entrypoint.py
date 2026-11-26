"""
Controlled InsightIQ FastAPI launcher.

Launch only in a dedicated server process:

    python -m app.server_entrypoint

This module intentionally does not import app.main
until startup recovery has finished successfully.
"""

import json
import os
from pathlib import Path


def main():
    from app.core.config import get_settings
    from app.services.runtime_startup import (
        prepare_server_runtime,
    )

    root = Path(
        get_settings().project_root
    ).resolve()

    config_path = (
        root
        / "data"
        / "server_startup_config.json"
    )

    if not config_path.is_file():
        raise RuntimeError(
            "Server startup configuration is missing."
        )

    config = json.loads(
        config_path.read_text(
            encoding="utf-8"
        )
    )

    if config.get("version") != 1:
        raise RuntimeError(
            "Unsupported server startup configuration."
        )

    if (
        Path(config["project_root"]).resolve()
        != root
    ):
        raise RuntimeError(
            "Server project root mismatch."
        )

    if not os.getenv("API_ACCESS_KEY"):
        raise RuntimeError(
            "Server API credential is unavailable."
        )

    missing_ai = [
        name for name in ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT")
        if not os.getenv(name, "").strip()
    ]
    if not (os.getenv("LLM_MODEL", "").strip() or os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip()):
        missing_ai.append("LLM_MODEL")
    if missing_ai:
        raise RuntimeError(
            "LLM configuration is incomplete. Set in .env: " + ", ".join(missing_ai)
        )

    from app.database import db

    if db.is_postgres():
        _start_postgres_runtime()
        _run_server()
        return

    # The per-session temp folder can change between Windows logins;
    # realign the configured runtime database path before recovery.
    from app.services.runtime_location import (
        align_runtime_location,
    )

    alignment = align_runtime_location(
        root,
        config_path,
        config,
    )

    for action in alignment["actions"]:
        print("Runtime database:", action)

    config = alignment["config"]

    database_path = Path(
        config["runtime_database_path"]
    ).resolve()

    backup_directory = (
        root / "data" / "sqlite_backups"
    )

    startup = prepare_server_runtime(
        database_path,
        backup_directory,
        root,
    )

    print(
        "InsightIQ startup recovery verified."
    )
    print(
        "Database restored:",
        startup["database_restored"],
    )
    print(
        "Interrupted jobs reconciled:",
        startup["interrupted_jobs_reconciled"],
    )

    _run_server()


def _start_postgres_runtime():
    """PostgreSQL: apply schema migrations, then reset interrupted jobs.

    The SQLite-file steps (temp-folder realignment, restore from verified
    file backups) do not apply; PostgreSQL keeps committed data durable.
    """
    from app.database import db
    from app.database.migrations import run_migrations
    from app.services.startup_recovery import reconcile_interrupted_jobs

    target = db.describe_target()
    try:
        applied = run_migrations()
    except db.DatabaseUnavailable as exc:
        raise RuntimeError(
            f"{exc} (target {target['host']}:{target['port']}, database {target['database']}). "
            "Check DB_HOST, DB_PORT, DB_NAME, DB_USER and DB_PASSWORD in .env, and that the "
            "PostgreSQL service is running."
        ) from exc
    print("Database: PostgreSQL", f"{target['host']}:{target['port']}/{target['database']}",
          f"schema={target['schema']}")
    print("Schema migrations applied now:", applied or "none")
    print("Interrupted jobs reconciled:", len(reconcile_interrupted_jobs(None)))


def _run_server():
    # Do not import the FastAPI application until
    # database restoration and reconciliation
    # have successfully completed.
    import uvicorn

    port = int(
        os.getenv(
            "INSIGHTIQ_API_PORT",
            "8000",
        )
    )

    if not 1 <= port <= 65535:
        raise RuntimeError(
            "Invalid API port."
        )

    from app.core.security import install_redaction, uvicorn_options

    install_redaction()
    # Host, trusted proxies (X-Forwarded-*) and optional TLS from the environment.
    uvicorn.run(
        "app.main:app",
        port=port,
        workers=1,
        reload=False,
        **uvicorn_options(),
    )


if __name__ == "__main__":
    main()
