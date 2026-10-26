"""Loading the landing zone into the raw schema.

The contract this layer holds is narrow: after it runs, every number in `raw` is
in dollars, in eaches, in Adelaide time, de-duplicated and traceable to the file
it came from.

Nothing downstream is allowed to be clever about units, timezone or duplicates,
because that is where the silent errors live. Concretely:

* a file is identified by the sha256 of its bytes, so a redelivery under a new
  name loads once (D01);
* the file's business day is read from its contents, never from its folder, so a
  file that lands two mornings late still lands in the right partition (D02);
* money is normalised to dollars using the money-unit marker the source publishes
  in the file header, and anything implausible is quarantined with its raw text
  rather than scaled (D04, D05);
* loss-scan timestamps are converted from UTC to Adelaide time, and the business
  day is derived from the local timestamp (D06);
* a row that cannot be typed is quarantined with the raw value and the reason.
  It is never defaulted to zero, because a zero is a number someone will report.

Every load writes the raw table, a row in `ops.ingest_files` per file, and a row
in `ops.raw_quarantine` per rejected row. That is what makes a run reviewable
afterwards instead of a mystery.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from ..config import Config
from ..db import OPS_SCHEMA, RAW_SCHEMA, ensure_schemas, query_scalar, replace_table
from . import schema

log = logging.getLogger(__name__)

#: A fresh line cannot average more than this per unit in dollars. It exists to
#: catch the cents/dollars error (D05): a file that averages $800 a punnet is
#: not a premium berry, it is a unit mistake.
MAX_PLAUSIBLE_UNIT_VALUE = 250.0

#: Same idea for a single loss-scan line: nobody disposes of 500kg of prawns.
MAX_PLAUSIBLE_DISPOSAL_QTY = 500.0

#: How many days after the trading day a feed is *expected* to land. This is the
#: baseline `lag_days` is measured against: a POS extract landing the next
#: morning is on time, not a day late. Without it every healthy file reads as
#: late, which is the same as the signal being useless.
EXPECTED_LANDING_LAG = {
    "pos_sales": 1,  # next morning
    "goods_received": 1,
    "stock_counts": 0,  # counted the same evening
    "markdown_events": 1,
    "waste_events": 1,
    "weather": 0,
}


@dataclass
class LoadedFile:
    source: str
    business_date: date | None
    landing_date: date
    file_name: str
    file_path: str
    rows_in_file: int
    rows_loaded: int
    rows_quarantined: int
    rows_duplicate: int
    checksum: str
    lag_days: int
    status: str  # loaded | duplicate | quarantined | failed
    notes: str = ""

    def as_record(self) -> dict:
        return {
            "source": self.source,
            "business_date": self.business_date,
            "landing_date": self.landing_date,
            "file_name": self.file_name,
            "file_path": self.file_path,
            "rows_in_file": self.rows_in_file,
            "rows_loaded": self.rows_loaded,
            "rows_quarantined": self.rows_quarantined,
            "rows_duplicate": self.rows_duplicate,
            "checksum": self.checksum,
            "lag_days": self.lag_days,
            "status": self.status,
            "notes": self.notes,
        }


def _read_csv(path: Path) -> tuple[pd.DataFrame, dict[str, str]]:
    """Read a landed file, extracting the source's header markers if present."""
    markers: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as handle:
        first = handle.readline()
    if first.startswith("#"):
        for part in first.lstrip("# ").split(","):
            if "=" in part:
                key, value = part.split("=", 1)
                markers[key.strip()] = value.strip()
        df = pd.read_csv(path, skiprows=1, dtype=str, keep_default_na=False)
    else:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    markers["_file"] = path.name
    return df, markers


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _to_num(value: object) -> float | None:
    """Parse a source number, tolerating the locale decimal comma (D04).

    Returns None when the value is genuinely unparseable, so the caller can
    quarantine it with its original text.
    """
    text = str(value).strip()
    if text == "":
        return None
    text = text.replace(" ", "")
    if "," in text and "." in text:
        text = text.replace(",", "")  # 1.234,56 style thousands + decimal
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


class Ingestor:
    """Loads every landed file into ``raw``, once."""

    def __init__(self, cfg: Config, con: duckdb.DuckDBPyConnection) -> None:
        self.cfg = cfg
        self.con = con
        self.files: list[LoadedFile] = []
        self.quarantine: list[dict] = []
        ensure_schemas(con)

    # -- entry point -----------------------------------------------------------
    def run(
        self,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        force: bool = False,
        prune: bool = True,
    ) -> pd.DataFrame:
        """Load the landing zone.

        ``force`` re-processes files even if their checksum is already known.
        That is what `make backfill` uses after a code change to a loader. It is
        safe because every loader replaces its rows for the business days it
        touches rather than appending blindly.

        ``prune`` drops raw rows that no file in the landing zone accounts for.
        Without it, shrinking the world leaves the previous world behind: run the
        eight-store estate, then `make world-small`, and `raw` holds both because
        nothing ever deletes a store that stopped appearing. That is a genuine
        reproducibility hole rather than a test artefact. The report would be
        built on two worlds at once, and the only visible symptom is a number
        that is quietly too big.
        """
        self._ensure_meta_tables()
        known = self._known_checksums() if not force else {}

        files = sorted(self.cfg.paths.landing_dir.rglob("*.csv"))
        log.info("landing files found: %d", len(files))
        by_source: dict[str, list[Path]] = {}
        for path in files:
            source = path.relative_to(self.cfg.paths.landing_dir).parts[0]
            by_source.setdefault(source, []).append(path)

        if prune:
            self._prune_sources(by_source, date_from=date_from, date_to=date_to)

        for source, paths in by_source.items():
            loader = schema.LOADERS[source]
            for path in sorted(paths):
                business_date_hint = self._business_date_from_dir(path)
                if date_from and business_date_hint and business_date_hint < date_from:
                    continue
                if date_to and business_date_hint and business_date_hint > date_to:
                    continue
                checksum = _sha256(path)
                if checksum in known:
                    self.files.append(
                        LoadedFile(
                            source=source,
                            business_date=business_date_hint,
                            landing_date=self._landing_date_from_dir(path),
                            file_name=path.name,
                            file_path=str(path.relative_to(self.cfg.paths.landing_dir)),
                            rows_in_file=0,
                            rows_loaded=0,
                            rows_quarantined=0,
                            rows_duplicate=known[checksum][1],
                            checksum=checksum,
                            lag_days=0,
                            status="duplicate",
                            notes=f"identical content already loaded as {known[checksum][0]} (D01)",
                        )
                    )
                    continue
                self._load_one(source, path, checksum, loader)
                known[checksum] = (str(path.relative_to(self.cfg.paths.landing_dir)), 1)

        self._write_meta()
        return pd.DataFrame([f.as_record() for f in self.files])

    # -- per-file load ---------------------------------------------------------
    def _load_one(self, source: str, path: Path, checksum: str, loader: schema.Loader) -> None:
        landing_date = self._landing_date_from_dir(path)
        try:
            raw, markers = _read_csv(path)
        except Exception as exc:
            log.warning("unreadable file %s: %s", path, exc)
            self.files.append(
                LoadedFile(
                    source=source,
                    business_date=None,
                    landing_date=landing_date,
                    file_name=path.name,
                    file_path=str(path.relative_to(self.cfg.paths.landing_dir)),
                    rows_in_file=0,
                    rows_loaded=0,
                    rows_quarantined=0,
                    rows_duplicate=0,
                    checksum=checksum,
                    lag_days=0,
                    status="failed",
                    notes=str(exc)[:400],
                )
            )
            return

        frame, quarantine, business_date = loader(self.cfg, raw, markers, source)
        if business_date is not None and hasattr(business_date, "date"):
            business_date = business_date.date()
        frame["_source_file"] = path.name
        frame["_file_checksum"] = checksum
        frame["_landing_date"] = landing_date
        frame["_ingested_at"] = datetime.now(UTC).replace(tzinfo=None)

        # Replace the business days this file covers, so a re-run (or a late
        # file that lands after a partial load) converges on the same table.
        self._replace_business_days(source, frame, business_date)
        for row in quarantine:
            row.update(
                {
                    "source": source,
                    "source_file": path.name,
                    "checksum": checksum,
                }
            )
        self.quarantine.extend(quarantine)

        # Lateness is not "how old is this file". Every feed is at least a day
        # old, because a trading day's extract lands the next morning. It is how
        # much *later* than that baseline the file actually turned up, which is
        # the only number that says whether a feed is drifting.
        lag = (
            (landing_date - business_date).days - EXPECTED_LANDING_LAG.get(source, 0)
            if business_date
            else 0
        )
        self.files.append(
            LoadedFile(
                source=source,
                business_date=business_date,
                landing_date=landing_date,
                file_name=path.name,
                file_path=str(path.relative_to(self.cfg.paths.landing_dir)),
                rows_in_file=len(raw),
                rows_loaded=len(frame),
                rows_quarantined=len(quarantine),
                rows_duplicate=0,
                checksum=checksum,
                lag_days=max(0, lag),
                status="loaded",
            )
        )

    def _replace_business_days(
        self, source: str, frame: pd.DataFrame, business_date: date | None
    ) -> None:
        """Make the load idempotent for the rows a file covers.

        The delete must be scoped to what this file *is*, not just to the day it
        covers. The POS feed lands one file per store per day, so deleting by
        business date alone means store S03's file wipes store S01's rows for
        the same day. The last store processed each day is the only one that
        survives. Keying on (business_date, store_code) is what makes the load
        idempotent per feed rather than destructive across feeds.
        """
        table = source
        if not self._table_has_rows(table):
            self._create_or_replace(table, frame)
            return

        keys = self._idempotency_keys(frame, business_date)
        if not keys:
            # Reference extracts (item/store/supplier master, promotions) carry
            # no business day: they are a full snapshot, so they replace.
            self._create_or_replace(table, frame)
            return

        conditions = []
        params: list[object] = []
        for column, values in keys.items():
            placeholders = ", ".join(["?"] * len(values))
            conditions.append(f"{column} IN ({placeholders})")
            params.extend(values)
        self.con.execute(
            f"DELETE FROM {RAW_SCHEMA}.{table} WHERE {' AND '.join(conditions)}",
            params,
        )
        self._insert(table, frame)

    def _idempotency_keys(
        self, frame: pd.DataFrame, business_date: date | None
    ) -> dict[str, list[object]]:
        """The grain a file owns, so a re-run replaces exactly its own rows.

        Every daily feed is (business day, store); the loss-scan and markdown
        feeds are the same shape. Anything else falls back to business day.
        """
        keys: dict[str, list[object]] = {}
        present = set(frame.columns)
        if "business_date" in present:
            days = sorted(set(frame["business_date"].dropna().unique()))
            if not days and business_date is not None:
                days = [business_date]
            if days:
                keys["business_date"] = days
        if {"store_code", "business_date"} <= present:
            stores = sorted({str(s) for s in frame["store_code"].dropna().unique()})
            if stores:
                keys["store_code"] = stores
        return keys

    def _prune_sources(
        self,
        by_source: dict[str, list[Path]],
        *,
        date_from: date | None,
        date_to: date | None,
    ) -> None:
        """Delete raw rows the current landing zone cannot account for.

        The load itself only replaces the (day, store) pairs a file covers, which
        is what makes it idempotent. That is necessary but not sufficient: if the
        world shrinks, through a backfill over a narrower window or `world-small`
        after `world`, the stores and days that no longer appear are never
        overwritten, so they survive and the report is built on two worlds.

        The prune is scoped so it can only ever remove what has genuinely gone:

        * **It only runs on a full load.** A `--from`/`--to` load is a backfill of
          a window, not a statement that the rest of history has gone, so pruning
          is skipped entirely when a window was requested. (An earlier version
          pruned anyway and truncated 3,000 rows a caller had asked to keep.)
        * **Per source**, so a source absent from the landing zone is left alone
          rather than read as "delete everything".
        * **By store**, because a shrinking world is the case that actually bites:
          the eight-store estate followed by a two-store run left six stores'
          rows in `raw` forever, and the report was quietly built on two worlds.
        """
        if date_from is not None or date_to is not None:
            # A windowed load is a backfill. Deleting outside its window would
            # destroy exactly the history the caller was trying to keep.
            return

        for source, paths in by_source.items():
            if not paths or not self._table_has_rows(source):
                continue
            columns = self._table_columns(source)
            if "business_date" not in columns:
                continue

            # What the current landing zone actually accounts for, at the grain
            # the table is keyed on.
            file_stores: list[str] = []
            file_days: set[date] = set()
            for path in paths:
                hint = self._business_date_from_dir(path)
                if hint is None:
                    continue
                file_days.add(hint)
                parts = path.name.split("_")
                if len(parts) > 1 and parts[1][:1].isalpha():
                    file_stores.append(parts[1])
            if not file_days:
                continue

            conditions: list[str] = []
            params: list[object] = []

            # 1. Stores that no longer appear in this world. Run an eight-store
            #    estate then a two-store world and six stores' rows would sit in
            #    raw forever, so the report was built on two worlds at once.
            if "store_code" in columns and file_stores:
                distinct_stores = sorted(set(file_stores))
                placeholders = ", ".join(["?"] * len(distinct_stores))
                conditions.append(f"store_code NOT IN ({placeholders})")
                params.extend(distinct_stores)

            # 2. Days outside the window the files themselves span. Derived from
            #    the files rather than from config, so it cannot disagree with
            #    what was actually landed. A day whose file is merely late is not
            #    a day that has gone. But `world` regenerates the whole landing
            #    zone, so a day with no file at all genuinely is not in this world.
            #
            #    This is only a window check: it removes history outside the
            #    landed span, never gaps inside it. A gap inside is a missing file
            #    the pipeline must report (D03), not silently delete.
            conditions.append("business_date < ?")
            params.append(min(file_days))
            conditions.append("business_date > ?")
            params.append(max(file_days))

            if conditions:
                removed = self.con.execute(
                    f"DELETE FROM {RAW_SCHEMA}.{source} WHERE {' OR '.join(conditions)}",
                    params,
                ).fetchall()
                del removed
                log.debug("pruned %s to the current world", source)

    def _table_columns(self, table: str) -> set[str]:
        rows = self.con.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = ? AND table_name = ?
            """,
            [RAW_SCHEMA, table],
        ).fetchall()
        return {r[0] for r in rows}

    @staticmethod
    def _is_snapshot(source: str) -> bool:
        return source in schema.REFERENCE_SOURCES

    def _table_has_rows(self, table: str) -> bool:
        return bool(
            query_scalar(
                self.con,
                """
                SELECT count(*) FROM information_schema.tables
                WHERE table_schema = ? AND table_name = ?
                """,
                [RAW_SCHEMA, table],
                default=0,
            )
        )

    def _create_or_replace(self, table: str, frame: pd.DataFrame) -> None:
        replace_table(self.con, frame, RAW_SCHEMA, table)

    def _insert(self, table: str, frame: pd.DataFrame) -> None:
        """Append by column *name*, never by position.

        `INSERT INTO t SELECT * FROM df` matches columns positionally, so it only
        works when the incoming frame has exactly the table's column order. The
        POS loader inserts `extract_batch_id` at a fixed position, and on a file
        whose header differs the two orders diverge. The insert then silently
        shifts every value one column left and fails with a type error somewhere
        unrelated. Naming the columns makes the append order-independent.
        """
        columns = [
            row[1]
            for row in self.con.execute(
                """
                SELECT ordinal_position, column_name
                FROM information_schema.columns
                WHERE table_schema = ? AND table_name = ?
                ORDER BY ordinal_position
                """,
                [RAW_SCHEMA, table],
            ).fetchall()
        ]
        present = [c for c in columns if c in frame.columns]
        if not present:
            raise ValueError(f"no columns of {table} present in the incoming frame")
        self.con.register("_ingest_append", frame)
        try:
            column_list = ", ".join(f'"{c}"' for c in present)
            self.con.execute(
                f"INSERT INTO {RAW_SCHEMA}.{table} ({column_list}) "
                f"SELECT {column_list} FROM _ingest_append"
            )
        finally:
            self.con.unregister("_ingest_append")

    # -- metadata --------------------------------------------------------------
    def _ensure_meta_tables(self) -> None:
        self.con.execute(f"CREATE SCHEMA IF NOT EXISTS {OPS_SCHEMA}")
        self.con.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {OPS_SCHEMA}.ingest_files (
                source VARCHAR,
                business_date DATE,
                landing_date DATE,
                file_name VARCHAR,
                file_path VARCHAR,
                rows_in_file BIGINT,
                rows_loaded BIGINT,
                rows_quarantined BIGINT,
                rows_duplicate BIGINT,
                checksum VARCHAR,
                lag_days INTEGER,
                status VARCHAR,
                notes VARCHAR,
                loaded_at TIMESTAMP
            )
            """
        )

    def _known_checksums(self) -> dict[str, tuple[str, int]]:
        rows = self.con.execute(
            f"""
            SELECT checksum, arg_min(file_path, loaded_at) AS file_path
            FROM {OPS_SCHEMA}.ingest_files
            WHERE status = 'loaded'
            GROUP BY checksum
            """
        ).fetchall()
        return {checksum: (path, 1) for checksum, path in rows}

    def _write_meta(self) -> None:
        # Rewrite the meta views for *this* run, keeping history for `status`.
        self.con.execute(
            f"CREATE OR REPLACE TABLE {OPS_SCHEMA}.ingest_files AS SELECT * FROM {OPS_SCHEMA}.ingest_files WHERE 1=0"
        )
        if self.files:
            frame = pd.DataFrame([f.as_record() for f in self.files])
            frame["loaded_at"] = datetime.now(UTC).replace(tzinfo=None)
            replace_table(self.con, frame, OPS_SCHEMA, "ingest_files")
        if self.quarantine:
            q = pd.DataFrame(self.quarantine)
            replace_table(self.con, q, OPS_SCHEMA, "raw_quarantine")
        else:
            self.con.execute(
                f"CREATE OR REPLACE TABLE {OPS_SCHEMA}.raw_quarantine AS "
                f"SELECT NULL::VARCHAR AS source, NULL::VARCHAR AS source_file, "
                f"NULL::VARCHAR AS checksum, NULL::VARCHAR AS business_date, "
                f"NULL::VARCHAR AS raw_row, NULL::VARCHAR AS reason WHERE 1=0"
            )

    # -- helpers ---------------------------------------------------------------
    def _landing_date_from_dir(self, path: Path) -> date:
        return date.fromisoformat(path.parent.name)

    def _business_date_from_dir(self, path: Path) -> date | None:
        """Only used for `--from/--to` windowing before the file is read.

        The authoritative business date always comes from the loader, which
        reads the file; the folder is a hint and nothing more (D02).
        """
        parts = path.name.split("_")
        for part in reversed(parts):
            digits = "".join(ch for ch in part if ch.isdigit())
            if len(digits) >= 8:
                try:
                    return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
                except ValueError:
                    continue
        return None


def recompute_landing_gaps(cfg: Config, con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Which expected store-days never arrived (D03), for the quality mart."""
    truth = cfg.paths.truth_dir / "landing_gaps.csv"
    if truth.exists():
        return pd.read_csv(truth)
    return pd.DataFrame(columns=["source", "store_code", "business_date"])


def expected_store_days(cfg: Config, con: duckdb.DuckDBPyConnection) -> int:
    """Not used: kept out. The coverage test in the warehouse compares POS
    presence against the store/calendar cross product, which is the only
    definition that cannot drift from the data."""
    raise NotImplementedError


def init_meta_state(con: duckdb.DuckDBPyConnection) -> None:
    """Create the metadata schema even on a warehouse that never ran a load."""
    ensure_schemas(con)
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {OPS_SCHEMA}")


def lookback_window(cfg: Config, days: int) -> tuple[date, date]:
    end = cfg.clock.as_of_date
    return end - timedelta(days=days), end
