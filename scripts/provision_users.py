"""
Provision InsightIQ login accounts (idempotent; stores only password hashes).

Interactive (default): asks for each account's password twice with hidden
input (getpass). Passwords are never accepted as command-line arguments,
written to files or logged.

    .venv\\Scripts\\python.exe scripts\\provision_users.py user@insightiq.com demo@insightiq.com

Existing accounts are left unchanged (no accidental password reset); use
--reset to deliberately replace a password (this also signs the user out).
--password-stdin reads one password per account from standard input (one per
line, same order as the emails) for automated provisioning, like
`docker login --password-stdin`.
"""

import argparse
import getpass
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("emails", nargs="+")
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / ".env"))
    parser.add_argument("--reset", action="store_true", help="replace the password of existing accounts")
    parser.add_argument("--password-stdin", action="store_true", help="read passwords from stdin, one per line")
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=False)
    os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(PROJECT_ROOT))

    from app.database.migrations import run_migrations
    from app.services import auth

    run_migrations()
    stdin_lines = iter(sys.stdin.read().splitlines()) if args.password_stdin else None
    failures = 0
    for raw in args.emails:
        try:
            email = auth.normalize_email(raw)
        except auth.AuthError as exc:
            print(f"{raw}: {exc}")
            failures += 1
            continue
        if stdin_lines is not None:
            password = next(stdin_lines, "")
        else:
            password = getpass.getpass(f"Password for {email}: ")
            if getpass.getpass(f"Repeat password for {email}: ") != password:
                print(f"{email}: passwords do not match; skipped.")
                failures += 1
                continue
        try:
            outcome = auth.create_user(email, password, reset=args.reset)
        except auth.AuthError as exc:
            print(f"{email}: {exc}")
            failures += 1
            continue
        finally:
            password = None
        print(f"{email}: {outcome}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
