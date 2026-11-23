import json
from pathlib import Path

from app.services.startup_recovery import (
    StartupRecoveryError,
    prepare_runtime_database,
    verified_backup,
)


class ServerBootstrapError(RuntimeError):
    pass


def latest_backup_pair(backup_directory):
    directory = Path(backup_directory).resolve()

    if not directory.is_dir():
        raise ServerBootstrapError(
            "SQLite backup directory is unavailable."
        )

    candidates = sorted(
        directory.glob("insightiq_*.manifest.json"),
        key=lambda p: p.name,
        reverse=True,
    )

    if not candidates:
        raise ServerBootstrapError(
            "No SQLite backup manifest is available."
        )

    # Select the newest manifest, not the newest
    # *valid* manifest. A corrupted newest backup
    # must not trigger an older, potentially stale
    # restore without explicit operator action.
    manifest_path = candidates[0]

    try:
        manifest = json.loads(
            manifest_path.read_text(
                encoding="utf-8"
            )
        )

        backup_name = manifest["backup_file"]

        if (
            not isinstance(backup_name, str)
            or Path(backup_name).name != backup_name
        ):
            raise ValueError(
                "Invalid backup filename."
            )

        backup_path = (
            directory / backup_name
        ).resolve()

        if backup_path.parent != directory:
            raise ValueError(
                "Backup escaped its directory."
            )

        verified_backup(
            backup_path,
            manifest_path,
        )

    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        StartupRecoveryError,
    ) as exc:
        raise ServerBootstrapError(
            "Latest SQLite backup failed verification."
        ) from exc

    return backup_path, manifest_path, manifest


def bootstrap_api_database(
    runtime_database_path,
    backup_directory,
    enforce_source_identity=True,
):
    """
    This function must run before app.main is
    imported by the serving process.

    Only invoke while the intended workers are
    stopped. It does not safely reconcile jobs
    belonging to another active server process.
    """
    target = Path(
        runtime_database_path
    ).resolve()

    backup_path, manifest_path, manifest = (
        latest_backup_pair(
            backup_directory
        )
    )

    if enforce_source_identity:
        recorded = manifest.get(
            "source_database_path"
        )

        if (
            not recorded
            or Path(recorded).resolve() != target
        ):
            raise ServerBootstrapError(
                "Runtime database path does not match "
                "the verified backup configuration."
            )

    try:
        result = prepare_runtime_database(
            target,
            backup_path,
            manifest_path,
            reconcile_jobs=True,
        )

    except Exception as exc:
        raise ServerBootstrapError(
            "SQLite startup recovery failed."
        ) from exc

    return {
        **result,
        "backup_path": str(backup_path),
        "manifest_path": str(manifest_path),
        "source_identity_enforced": (
            enforce_source_identity
        ),
    }
