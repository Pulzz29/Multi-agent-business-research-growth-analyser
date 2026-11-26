"""
Home Overview for one market (US or IN), from the database only: universe
and stored-company counts, sector breakdown, largest companies by stored
market capitalisation, and recent research runs of that market's companies.
Nothing is hard-coded; no provider or AI calls are made.
"""

from app.database.sqlite_manager import connect
from app.services import market_registry as registry
from app.services import market_storage as storage


def _market_of_ticker(ticker, stored_markets):
    if ticker in stored_markets:
        return stored_markets[ticker]
    return "IN" if str(ticker).upper().endswith((".NS", ".BO")) else "US"


def overview(market, top_limit=8, runs_limit=8):
    market = registry.require_market(market)
    cfg = registry.market_config(market)
    stored = {c["symbol"]: c for c in storage.list_companies(market)}
    with connect() as conn:
        with_statements = {r[0] for r in conn.execute(
            "SELECT DISTINCT s.symbol FROM market_statements s "
            "JOIN market_companies c ON c.symbol = s.symbol WHERE c.market = ?", (market,)).fetchall()}
        with_quotes = {r[0] for r in conn.execute(
            "SELECT DISTINCT q.symbol FROM market_quotes q "
            "JOIN market_companies c ON c.symbol = q.symbol WHERE c.market = ?", (market,)).fetchall()}
        all_markets = {r[0]: r[1] for r in conn.execute("SELECT symbol, market FROM market_companies").fetchall()}
        runs = conn.execute(
            "SELECT run_id, ticker, status, created_at_utc, finished_at_utc FROM research_execution_runs "
            "ORDER BY created_at_utc DESC LIMIT 500").fetchall()

    sectors = []
    for key, sector in cfg["sectors"].items():
        universe = [c["symbol"] for c in sector["companies"]]
        loaded = [s for s, c in stored.items() if c.get("sector_key") == key]
        sectors.append({
            "sector_key": key,
            "label": sector["label"],
            "universe_count": len(universe),
            "loaded_count": len(loaded),
            "with_statements": sum(1 for s in loaded if s in with_statements),
        })
    market_runs = [dict(r) for r in runs if _market_of_ticker(r["ticker"], all_markets) == market]
    names = {s: c["name"] for s, c in stored.items()}
    recent = [{**r, "name": names.get(r["ticker"])} for r in market_runs[:runs_limit]]
    top = registry.top_companies(market, top_limit)
    return {
        "market": market,
        "label": cfg["label"],
        "currency": cfg["currency"],
        "counts": {
            "universe_companies": sum(s["universe_count"] for s in sectors),
            "stored_companies": len(stored),
            "with_statements": len(with_statements),
            "with_quotes": len(with_quotes),
            "sectors": len(sectors),
            "research_runs": len(market_runs),
            "completed_research_runs": sum(1 for r in market_runs if r["status"] == "completed"),
        },
        "sectors": sectors,
        "top_companies": top["companies"],
        "top_basis": top["basis"],
        "recent_runs": recent,
    }
