"""DuckDB access.

The warehouse is a single file (``data/warehouse/fresh_shelf.duckdb``). DuckDB
allows one writer at a time, so the pipeline is a *sequence*, not a swarm:
landing files -> raw (Python) -> transforms (dbt) -> analytics (Python) ->
report. Every command in ``fresh_shelf.cli`` opens a connection, does its work
and closes it, which is why `make daily` never deadlocks on a lock file — and
why nothing here holds a connection open while a subprocess runs.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from .config import Config, load_config

#: Landed source tables, exactly as they arrived (plus ingest provenance).
RAW_SCHEMA = "raw"
#: Pipeline metadata: runs, files, quarantined rows. Auditable, never dropped.
OPS_SCHEMA = "ops"
#: Written by the Python decision layer (forecast, plans, frontier).
ANALYTICS_SCHEMA = "analytics"
#: dbt seeds and model schemas.
SEEDS_SCHEMA = "seeds"
DBT_SCHEMAS = ("staging", "intermediate", "marts", "reporting", "analytics", "seeds")

ALL_SCHEMAS = (RAW_SCHEMA, OPS_SCHEMA, ANALYTICS_SCHEMA, *DBT_SCHEMAS)


def connect(
    cfg: Config | None = None,
    *,
    read_only: bool = False,
    warehouse_file: Path | str | None = None,
) -> duckdb.DuckDBPyConnection:
    """Open the warehouse, creating its directory if needed."""
    target = Path(warehouse_file) if warehouse_file else (cfg or load_config()).paths.warehouse_file
    target.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(target), read_only=read_only)
    con.execute("SET timezone = 'UTC'")  # raw timestamps are read as UTC, converted explicitly
    return con


def ensure_schemas(con: duckdb.DuckDBPyConnection, schemas: Iterable[str] = ALL_SCHEMAS) -> None:
    for schema in schemas:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")


def query_df(
    con: duckdb.DuckDBPyConnection, sql: str, params: Sequence[Any] | None = None
) -> pd.DataFrame:
    return con.execute(sql, params or []).fetch_df()


def query_scalar(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    params: Sequence[Any] | None = None,
    default: Any = None,
) -> Any:
    row = con.execute(sql, params or []).fetchone()
    return row[0] if row else default


def table_exists(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    return bool(
        query_scalar(
            con,
            """
            SELECT count(*) FROM information_schema.tables
            WHERE table_schema = ? AND table_name = ?
            """,
            [schema, table],
            default=0,
        )
    )


def list_tables(con: duckdb.DuckDBPyConnection, schema: str) -> list[str]:
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = ? ORDER BY 1",
        [schema],
    ).fetchall()
    return [r[0] for r in rows]


def row_count(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> int:
    if not table_exists(con, schema, table):
        return 0
    return int(query_scalar(con, f"SELECT count(*) FROM {schema}.{table}", default=0) or 0)


def replace_table(
    con: duckdb.DuckDBPyConnection, df: pd.DataFrame, schema: str, table: str
) -> None:
    """Publish a DataFrame as a table, atomically enough for this pipeline.

    DuckDB creates the table inside the same transaction as the data write, so
    a downstream reader sees either the old table or the new one — and a re-run
    of the same step produces the same table, which is what makes `make daily`
    safe to repeat.
    """
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    con.register("_replace_table_df", df)
    try:
        con.execute(f"CREATE OR REPLACE TABLE {schema}.{table} AS SELECT * FROM _replace_table_df")
    finally:
        con.unregister("_replace_table_df")


def describe(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> pd.DataFrame:
    return query_df(con, f"DESCRIBE {schema}.{table}")


def write_parquet(df: pd.DataFrame, path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(target, index=False)
    return target


def read_parquet(path: Path | str) -> pd.DataFrame:
    return pd.read_parquet(path)


def inventory(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Every table with its exact row count, for `status`.

    Reads `information_schema` rather than `duckdb_tables()`. DuckDB's table
    function has renamed its columns across versions (this project hit
    `table_schema` becoming `schema_name`, then `estimated_size` disappearing),
    and `information_schema` is the portable spelling.

    Counts exactly rather than estimating: the estate's largest table is under a
    million rows, so `count(*)` costs milliseconds and an *estimate* would make
    `status` useless for spotting a table that should have grown.
    """
    tables = query_df(
        con,
        """
        SELECT table_schema AS schema, table_name AS name
        FROM information_schema.tables
        WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
        ORDER BY table_schema, table_name
        """,
    )
    counts = []
    for row in tables.itertuples():
        try:
            value = con.execute(f'SELECT count(*) FROM "{row.schema}"."{row.name}"').fetchone()[0]
        except Exception:  # an unreadable table must not break status
            value = None
        counts.append(value)
    tables["rows"] = counts
    return tables
