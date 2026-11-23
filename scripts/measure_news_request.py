"""Reconstruct the exact News Agent Groq request offline and measure its size.

No network call is made: the Groq client is constructed but guarded_chat_completion is
replaced by a capture stub, and outbound sockets are blocked.

Usage: .venv\\Scripts\\python.exe scripts\\measure_news_request.py [--db PATH] [--json OUT]
  --db   read articles from another SQLite file (read-only), e.g. a backup
"""
import argparse
import json
import os
import socket
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

_real_connect = socket.socket.connect


def _guard(self, address):
    host = address[0] if isinstance(address, tuple) else str(address)
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ConnectionRefusedError("measure_news_request: network blocked")
    return _real_connect(self, address)


socket.socket.connect = _guard


class Captured(Exception):
    pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db")
    parser.add_argument("--json")
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / ".env.test"))
    args = parser.parse_args()

    from run_backend import load_environment

    load_environment(Path(args.env_file).resolve())

    from app.agents import news_agent

    if args.db:
        db = Path(args.db).resolve()

        @contextmanager
        def ro_connect():
            con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            try:
                yield con
            finally:
                con.close()

        news_agent.connect = ro_connect

    captured = {}

    def capture(client, **kwargs):
        captured.update(kwargs)
        raise Captured()

    news_agent.guarded_chat_completion = capture
    budget_fn = getattr(news_agent, "estimate_request_tokens", None)

    rows = []
    for ticker in sorted(news_agent.SUPPORTED_TICKERS):
        captured.clear()
        try:
            articles, _ = news_agent.load_news_evidence(ticker)
        except news_agent.NewsAgentError as exc:
            rows.append({"ticker": ticker, "error": str(exc)})
            continue
        try:
            news_agent.select_news_with_groq(ticker, articles)
        except Captured:
            pass
        except news_agent.NewsAgentError as exc:
            rows.append({"ticker": ticker, "error": str(exc), "candidates": len(articles)})
            continue
        body = json.dumps({k: v for k, v in captured.items()}, ensure_ascii=False)
        user = captured["messages"][1]["content"]
        sent = len(json.loads(user)["articles"])
        desc = [len(a.get("description") or "") for a in articles]
        row = {
            "ticker": ticker,
            "candidates_loaded": len(articles),
            "articles_sent": sent,
            "request_body_bytes": len(body.encode("utf-8")),
            "user_message_chars": len(user),
            "schema_bytes": len(json.dumps(captured["response_format"]).encode("utf-8")),
            "max_completion_tokens": captured["max_completion_tokens"],
            "largest_description_chars": max(desc) if desc else 0,
            "total_description_chars": sum(desc),
            "est_input_tokens_chars_div_4": len(body) // 4,
        }
        if budget_fn:
            row["est_input_tokens_conservative"] = budget_fn(captured["messages"], captured["response_format"])
            row["est_total_requested_tokens"] = row["est_input_tokens_conservative"] + row["max_completion_tokens"]
        rows.append(row)

    for row in rows:
        print(json.dumps(row))
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
