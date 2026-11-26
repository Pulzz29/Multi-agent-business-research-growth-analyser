"""
Copy an InsightIQ SQLite database into PostgreSQL and prove the copy.

* Tables are copied in foreign-key order with
  ``INSERT ... ON CONFLICT DO NOTHING``, so re-running never duplicates rows.
* Identity sequences are moved past the copied ids.
* ``validate`` compares row counts and a SHA-256 checksum over every row
  (ordered by primary key, values normalised the same way on both sides).

The SQLite source is opened read-only and is never modified.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

# Parents before children (foreign keys).
TABLE_ORDER = (
    "companies",
    "sec_facts",
    "news_articles",
    "source_refreshes",
    "research_jobs",
    "research_reports",
    "forecast_cache",
    "ai_runs",
    "ai_trace_events",
    "observability_alerts",
    "schema_migrations",
    "market_companies",
    "market_quotes",
    "market_statements",
    "provider_usage",
    "research_execution_migrations",
    "research_execution_runs",
    "observability_schema_migrations",
    "research_observability_events",
    "reliability_monitor_evaluations",
    "sec_company_validation",
)
BATCH = 2000


def open_sqlite_readonly(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {path}")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    return conn


def _sqlite_tables(src):
    return {r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}


def _sqlite_columns(src, table):
    return [r[1] for r in src.execute(f'PRAGMA table_info("{table}")')]


def _pg_columns(pg, table):
    rows = pg.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ? ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _primary_key(pg, table):
    rows = pg.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = (quote_ident(current_schema()) || '.' || quote_ident(?))::regclass
          AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """,
        (table,),
    ).fetchall()
    return [r[0] for r in rows]


def _convert(value, pg_type):
    """SQLite value -> value for a PostgreSQL column of ``pg_type`` (no data changes)."""
    if value is None:
        return None
    if pg_type == "text" and not isinstance(value, str):
        if isinstance(value, bytes):
            raise ValueError("binary value in a text column")
        return _canonical(value)
    if pg_type in ("bigint", "integer", "smallint") and isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def copy_database(source_path, pg, tables=None, progress=None):
    """Copy every table of ``source_path`` into the PostgreSQL connection ``pg``
    (schema already migrated). Returns per-table results."""
    src = open_sqlite_readonly(source_path)
    results = {}
    try:
        present = _sqlite_tables(src)
        for table in tables or TABLE_ORDER:
            if table not in present:
                results[table] = {"status": "absent_in_source", "source_rows": 0, "inserted": 0}
                continue
            pg_cols = _pg_columns(pg, table)
            if not pg_cols:
                raise RuntimeError(f"Table {table} is missing in PostgreSQL; run the migrations first.")
            cols = [c for c in _sqlite_columns(src, table) if c in pg_cols]
            extra = [c for c in _sqlite_columns(src, table) if c not in pg_cols]
            if extra:
                raise RuntimeError(f"Columns of {table} not in PostgreSQL schema: {extra}")
            quoted = ", ".join(f'"{c}"' for c in cols)
            insert = (f'INSERT INTO "{table}" ({quoted}) VALUES ({", ".join("?" for _ in cols)}) '
                      "ON CONFLICT DO NOTHING")
            types = [pg_cols[c] for c in cols]
            source_rows = inserted = 0
            cursor = src.execute(f'SELECT {quoted} FROM "{table}"')
            while True:
                batch = cursor.fetchmany(BATCH)
                if not batch:
                    break
                source_rows += len(batch)
                values = [tuple(_convert(v, t) for v, t in zip(row, types)) for row in batch]
                before = pg.total_changes
                pg.executemany(insert, values)
                inserted += pg.total_changes - before
            pg.commit()
            results[table] = {"status": "copied", "source_rows": source_rows, "inserted": inserted}
            if progress:
                progress(table, results[table])
        reset_sequences(pg)
        pg.commit()
    finally:
        src.close()
    return results


def reset_sequences(pg):
    """Move every identity sequence past the highest stored id."""
    rows = pg.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND is_identity = 'YES'"
    ).fetchall()
    for table, column in rows:
        pg.execute(
            f'SELECT setval(pg_get_serial_sequence(?, ?), COALESCE(MAX("{column}"), 0) + 1, false) '
            f'FROM "{table}"',
            (f'{table}', column),
        )


# ------------------------------------------------------------- validation

def _canonical(value):
    """One text form per value on both engines (checksums)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(float(value))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


def _digest(rows):
    h = hashlib.sha256()
    count = 0
    for row in rows:
        h.update(json.dumps([_canonical(v) for v in row], ensure_ascii=False).encode("utf-8"))
        h.update(b"\n")
        count += 1
    return count, h.hexdigest()


def validate(source_path, pg, tables=None):
    """Compare counts and row checksums. Returns {table: {...match...}}."""
    src = open_sqlite_readonly(source_path)
    report = {}
    try:
        present = _sqlite_tables(src)
        for table in tables or TABLE_ORDER:
            if table not in present:
                continue
            pg_cols = _pg_columns(pg, table)
            cols = [c for c in _sqlite_columns(src, table) if c in pg_cols]
            key = _primary_key(pg, table) or cols
            quoted = ", ".join(f'"{c}"' for c in cols)
            order = ", ".join(f'"{c}"' for c in key)
            # Keys are compared in one collation-independent order: by their
            # canonical text in Python (SQLite and PostgreSQL sort text differently).
            s_rows = sorted(src.execute(f'SELECT {quoted} FROM "{table}"').fetchall(),
                            key=lambda r: _sort_key(r, cols, key))
            p_rows = sorted((tuple(r) for r in pg.execute(f'SELECT {quoted} FROM "{table}"').fetchall()),
                            key=lambda r: _sort_key(r, cols, key))
            s_count, s_hash = _digest(s_rows)
            p_count, p_hash = _digest(p_rows)
            report[table] = {
                "sqlite_rows": s_count, "postgres_rows": p_count,
                "sqlite_sha256": s_hash, "postgres_sha256": p_hash,
                "match": s_count == p_count and s_hash == p_hash, "order_by": order,
            }
    finally:
        src.close()
    return report


def _sort_key(row, cols, key):
    out = []
    for name in key:
        value = row[cols.index(name)]
        out.append((0, value) if isinstance(value, (int, float)) and not isinstance(value, bool)
                   else (1, _canonical(value) or ""))
    return out
