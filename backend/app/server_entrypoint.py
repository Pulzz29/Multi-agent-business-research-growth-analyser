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

    if not os.getenv("GROQ_API_KEY"):
        raise RuntimeError(
            "Server Groq credential is unavailable."
        )

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

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=port,
        workers=1,
        reload=False,
    )


if __name__ == "__main__":
    main()
