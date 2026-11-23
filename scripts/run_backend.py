"""Start the InsightIQ FastAPI backend locally.

Loads credentials from a .env file (default: <project>/.env) into the process
environment, then runs the application's own controlled launcher
(app.server_entrypoint), which performs startup recovery before importing app.main.

Usage:
    .venv\\Scripts\\python.exe scripts\\run_backend.py [--env-file PATH]
"""
import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT_ROOT / "backend"


def load_environment(env_file):
    from dotenv import load_dotenv

    if env_file.is_file():
        # Values already present in the real environment take precedence.
        load_dotenv(env_file, override=False)
    os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(PROJECT_ROOT))

    required = ("API_ACCESS_KEY", "GROQ_API_KEY", "NEWSDATA_API_KEY", "SEC_USER_AGENT")
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        sys.exit(
            "InsightIQ cannot start. Set these variables in "
            f"{env_file.name} (see .env.example): {', '.join(missing)}"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / ".env"))
    args = parser.parse_args()

    load_environment(Path(args.env_file).resolve())

    os.chdir(BACKEND)
    sys.path.insert(0, str(BACKEND))

    from app.server_entrypoint import main as server_main

    server_main()


if __name__ == "__main__":
    main()
