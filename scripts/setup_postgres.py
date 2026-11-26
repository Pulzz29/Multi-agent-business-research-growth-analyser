"""
Prepare the local PostgreSQL database for InsightIQ (idempotent).

1. Connects to the server with the DB_* (or DATABASE_URL) settings from .env.
2. Creates the application database (DB_NAME) if it does not exist, and,
   with --with-test-db, the test database "<DB_NAME>_test".
3. Applies the schema migrations (app/database/migrations).
4. Prints the server version and the tables; the password is never printed.

Usage: .venv\\Scripts\\python.exe scripts\\setup_postgres.py [--with-test-db] [--env-file PATH]
"""

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))


def ensure_database(name):
    """CREATE DATABASE <name> when missing (connects to the 'postgres' maintenance DB)."""
    from psycopg import sql

    from app.database import db

    admin = db.direct_connect(dbname="postgres", schema="public", autocommit=True)
    try:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = ?", (name,)).fetchone()
        if exists:
            return False
        admin.raw.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        return True
    finally:
        admin.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / ".env"))
    parser.add_argument("--with-test-db", action="store_true",
                        help="also create <DB_NAME>_test for the automated tests")
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=False)
    os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(PROJECT_ROOT))

    from app.core.config import get_settings
    from app.database import db
    from app.database.migrations import run_migrations

    settings = get_settings()
    if settings.db_backend != "postgres":
        sys.exit("DB_BACKEND is not postgres; nothing to set up.")
    target = db.describe_target()
    print(f"Server: {target['host']}:{target['port']}  database: {target['database']}  "
          f"schema: {target['schema']}  sslmode: {target['sslmode']}")

    try:
        created = ensure_database(target["database"])
        print("Database", target["database"], "created." if created else "already exists.")
        if args.with_test_db:
            test_name = f"{target['database']}_test"
            created = ensure_database(test_name)
            print("Test database", test_name, "created." if created else "already exists.")
        applied = run_migrations()
    except db.DatabaseUnavailable as exc:
        sys.exit(f"Cannot connect: {exc}")
    except Exception as exc:  # message only; never credentials
        sys.exit(f"Setup failed: {type(exc).__name__}: {str(exc).splitlines()[0][:300]}")

    print("Migrations applied now:", applied or "none (schema up to date)")
    conn = db.direct_connect()
    try:
        version = conn.execute("SHOW server_version").fetchone()[0]
        tables = db.table_names(conn)
    finally:
        conn.close()
    print("PostgreSQL", version, "-", len(tables), "tables:", ", ".join(tables))
    return 0


if __name__ == "__main__":
    sys.exit(main())
