"""The command line: the only entry point the Makefile and CI use.

Every command is a *step* of the same daily run, so they compose: `world` lands
files, `ingest` makes them trustworthy, `build` runs the dbt DAG, `forecast` and
`optimise` make decisions, `report` writes the artifact, and `run` does all of it
in order with one manifest.

All of them accept the same clock overrides (`--as-of`, `--days`) and the same
scale knob (`--stores`), which is what makes the pipeline replayable: a backfill
is `--as-of` a past date, and CI is `--stores 2 --days 90`.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

import click
import pandas as pd

from .config import Config, load_config
from .db import connect, ensure_schemas, inventory, query_df, query_scalar

log = logging.getLogger("fresh_shelf")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


class RunLog:
    """A step-by-step record of a pipeline run, written as JSON and Markdown.

    The point is that "did last night's run work?" is answerable from the
    warehouse, not from someone's terminal scrollback: what ran, how long it
    took, what it changed, and what it refused to load.
    """

    def __init__(self, cfg: Config, name: str) -> None:
        self.cfg = cfg
        self.name = name
        self.started = datetime.now(UTC)
        self.steps: list[dict] = []

    def step(self, name: str) -> _StepContext:
        return _StepContext(self, name)

    def add(self, name: str, seconds: float, status: str, detail: str = "") -> None:
        self.steps.append(
            {"step": name, "seconds": round(seconds, 2), "status": status, "detail": detail}
        )

    def write(self) -> Path:
        cfg = self.cfg
        record = {
            "run_name": self.name,
            "as_of_date": cfg.clock.as_of_date.isoformat(),
            "trading_window": [
                cfg.clock.first_trading_day.isoformat(),
                cfg.clock.last_trading_day.isoformat(),
            ],
            "seed": cfg.seed,
            "stores": cfg.stores,
            "started_utc": self.started.isoformat(),
            "finished_utc": datetime.now(UTC).isoformat(),
            "duration_seconds": round((datetime.now(UTC) - self.started).total_seconds(), 1),
            "steps": self.steps,
        }
        cfg.paths.runs_dir.mkdir(parents=True, exist_ok=True)
        target = cfg.paths.runs_dir / f"{self.name}_{self.started:%Y%m%dT%H%M%SZ}.json"
        target.write_text(json.dumps(record, indent=2))
        (cfg.paths.runs_dir / "latest.json").write_text(json.dumps(record, indent=2))
        return target


class _StepContext:
    def __init__(self, runlog: RunLog, name: str) -> None:
        self.runlog = runlog
        self.name = name

    def __enter__(self) -> _StepContext:
        log.info("=== %s", self.name)
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        seconds = time.perf_counter() - self.t0
        status = "ok" if exc_type is None else "failed"
        detail = "" if exc_type is None else f"{exc_type.__name__}: {exc}"
        self.runlog.add(self.name, seconds, status, detail)
        if exc_type is None:
            log.info("--- %s ok in %.1fs", self.name, seconds)
        return False

    def ok(self, detail: str) -> None:
        if self.runlog.steps:
            self.runlog.steps[-1]["detail"] = detail


# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------
def common_options(func):
    func = click.option(
        "--as-of", "as_of", default=None, help="Override the as-of date (YYYY-MM-DD)."
    )(func)
    func = click.option(
        "--days", "days", type=int, default=None, help="Override the history window length."
    )(func)
    func = click.option(
        "--stores",
        default=None,
        help="'all', an integer count, or a comma-separated list of store codes.",
    )(func)
    func = click.option("--seed", type=int, default=None, help="Override the simulation seed.")(
        func
    )
    return click.option("--verbose", is_flag=True, default=False, help="Debug logging.")(func)


def _parse_stores(value: str | None) -> str | int | list[str] | None:
    if value is None:
        return None
    text = value.strip()
    if text.isdigit():
        return int(text)
    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]
    return text


def _cfg(as_of, days, stores, seed) -> Config:
    return load_config().override(
        as_of=as_of, history_days=days, stores=_parse_stores(stores), seed=seed
    )


def _dbt(cfg: Config, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run dbt with the project clock and warehouse exported to it.

    dbt is where the transform DAG lives, so the CLI shells out to it rather
    than reimplementing it: `dbt build` runs models and tests in dependency
    order, and a failing test fails the step.
    """
    env = {**os.environ, **cfg.dbt_env(), "DBT_PROFILES_DIR": str(cfg.paths.root)}
    cmd = [str(_dbt_bin()), *args]
    log.debug("running: %s", " ".join(cmd))
    return subprocess.run(cmd, cwd=cfg.paths.root, env=env, check=check, text=True)


def _dbt_bin() -> Path:
    candidate = Path(sys.executable).parent / "dbt"
    return candidate if candidate.exists() else Path(shutil.which("dbt") or "dbt")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
@click.group()
@click.version_option(package_name="fresh-shelf-ops")
def cli() -> None:
    """Fresh-department waste vs revenue optimisation."""


@cli.command()
@common_options
def world(as_of, days, stores, seed, verbose) -> None:
    """Simulate the source systems into the landing zone (dirty files + truth)."""
    _configure_logging(verbose)
    from .landing.writer import write_landing
    from .world import simulate

    cfg = _cfg(as_of, days, stores, seed)
    runlog = RunLog(cfg, "world")
    cfg.paths.ensure()
    log.info("clock: %s", cfg.clock.describe())

    if cfg.paths.landing_dir.exists():
        shutil.rmtree(cfg.paths.landing_dir)

    with runlog.step("simulate"):
        simulation = simulate(cfg)
        log.info(
            "simulated %d stores, %d items",
            len(simulation.masters.stores),
            len(simulation.masters.items),
        )
    with runlog.step("write_truth"):
        for name, frame in simulation.truth.items():
            frame.to_parquet(cfg.paths.truth_dir / f"{name}.parquet", index=False)
    with runlog.step("write_landing"):
        manifest = write_landing(cfg, simulation)

    target = runlog.write()
    duplicates = (
        int((manifest["is_duplicate_of"].notna()).sum()) if "is_duplicate_of" in manifest else 0
    )
    log.info(
        "landed %d files (%d redeliveries) into %s",
        len(manifest),
        duplicates,
        cfg.paths.landing_dir,
    )
    click.echo(f"world written | files={len(manifest)} duplicates={duplicates} | {target.name}")


@cli.command()
@click.option(
    "--from", "date_from", default=None, help="Only load files landing on/after this date."
)
@click.option("--to", "date_to", default=None, help="Only load files landing on/before this date.")
@click.option("--force", is_flag=True, default=False, help="Re-process files already loaded.")
@common_options
def ingest(as_of, days, stores, seed, verbose, date_from, date_to, force) -> None:
    """Load the landing zone into the raw schema."""
    _configure_logging(verbose)
    from .ingest import Ingestor

    cfg = _cfg(as_of, days, stores, seed)
    runlog = RunLog(cfg, "ingest")
    cfg.paths.ensure()

    with connect(cfg) as con:
        ensure_schemas(con)
        with runlog.step("load_files"):
            ingestor = Ingestor(cfg, con)
            manifest = ingestor.run(
                date_from=date.fromisoformat(date_from) if date_from else None,
                date_to=date.fromisoformat(date_to) if date_to else None,
                force=force,
            )
        with runlog.step("summarise"):
            loaded = int((manifest["status"] == "loaded").sum()) if not manifest.empty else 0
            duplicates = int((manifest["status"] == "duplicate").sum()) if not manifest.empty else 0
            quarantined = int(manifest["rows_quarantined"].sum()) if not manifest.empty else 0
            late = int((manifest["lag_days"].fillna(0) > 0).sum()) if not manifest.empty else 0
            detail = (
                f"files={len(manifest)} loaded={loaded} duplicate={duplicates} "
                f"late={late} quarantined_rows={quarantined}"
            )
            log.info(detail)
    target = runlog.write()
    click.echo(f"ingest complete | {detail} | {target.name}")


@cli.command()
@common_options
def build(as_of, days, stores, seed, verbose) -> None:
    """dbt seed + build (models and tests, in dependency order)."""
    _configure_logging(verbose)
    cfg = _cfg(as_of, days, stores, seed)
    runlog = RunLog(cfg, "build")
    with runlog.step("dbt_deps"):
        _dbt(cfg, "deps")
    with runlog.step("dbt_seed"):
        _dbt(cfg, "seed")
    with runlog.step("dbt_build"):
        _dbt(cfg, "build")
    target = runlog.write()
    click.echo(f"dbt build complete | {target.name}")


@cli.command()
@common_options
def forecast(as_of, days, stores, seed, verbose) -> None:
    """Backtest the demand models and write the forward forecast."""
    _configure_logging(verbose)
    from .model.forecast import run_forecast

    cfg = _cfg(as_of, days, stores, seed)
    runlog = RunLog(cfg, "forecast")
    with connect(cfg, read_only=False) as con, runlog.step("backtest_and_forecast"):
        summary = run_forecast(cfg, con)
        log.info("forecast: %s", summary)
    target = runlog.write()
    click.echo(f"forecast complete | {target.name}")


@cli.command()
@common_options
def optimise(as_of, days, stores, seed, verbose) -> None:
    """Replenishment and markdown plans, the frontier, and the dollar impact."""
    _configure_logging(verbose)
    from .model.optimise import run_optimiser

    cfg = _cfg(as_of, days, stores, seed)
    runlog = RunLog(cfg, "optimise")
    with connect(cfg, read_only=False) as con, runlog.step("optimise"):
        summary = run_optimiser(cfg, con)
        log.info("optimiser: %s", summary)
    target = runlog.write()
    click.echo(f"optimise complete | {target.name}")


@cli.command()
@common_options
def report(as_of, days, stores, seed, verbose) -> None:
    """Render the static HTML report, its Markdown mirror and metrics.json."""
    _configure_logging(verbose)
    from .report.build import build_report

    cfg = _cfg(as_of, days, stores, seed)
    runlog = RunLog(cfg, "report")
    with connect(cfg, read_only=True) as con, runlog.step("render"):
        paths = build_report(cfg, con)
        detail = ", ".join(str(p.relative_to(cfg.paths.root)) for p in paths)
    target = runlog.write()
    click.echo(f"report written | {detail} | {target.name}")


@cli.command()
@common_options
def run(as_of, days, stores, seed, verbose) -> None:
    """The recurring run: land, load, build, forecast, optimise, report."""
    _configure_logging(verbose)
    ctx = click.get_current_context()
    cfg = _cfg(as_of, days, stores, seed)
    runlog = RunLog(cfg, "run")
    steps = [
        (
            "world",
            lambda: ctx.invoke(
                world, as_of=as_of, days=days, stores=stores, seed=seed, verbose=verbose
            ),
        ),
        (
            "ingest",
            lambda: ctx.invoke(
                ingest,
                as_of=as_of,
                days=days,
                stores=stores,
                seed=seed,
                verbose=verbose,
                date_from=None,
                date_to=None,
                force=False,
            ),
        ),
        (
            "build",
            lambda: ctx.invoke(
                build, as_of=as_of, days=days, stores=stores, seed=seed, verbose=verbose
            ),
        ),
        (
            "forecast",
            lambda: ctx.invoke(
                forecast, as_of=as_of, days=days, stores=stores, seed=seed, verbose=verbose
            ),
        ),
        (
            "optimise",
            lambda: ctx.invoke(
                optimise, as_of=as_of, days=days, stores=stores, seed=seed, verbose=verbose
            ),
        ),
        (
            "report",
            lambda: ctx.invoke(
                report, as_of=as_of, days=days, stores=stores, seed=seed, verbose=verbose
            ),
        ),
    ]
    for name, fn in steps:
        with runlog.step(name):
            fn()
    target = runlog.write()
    metrics = _latest_metrics(cfg)
    click.echo(f"run complete | {len(steps)} steps | {target.name}")
    if metrics:
        click.echo(
            "  annualised: sales ${sales:,.0f} | recorded waste ${waste:,.0f} | "
            "unrecorded shrink ${shrink:,.0f} | markdown erosion ${md:,.0f}".format(**metrics)
        )


@cli.command()
@click.option(
    "--assert-idempotent", is_flag=True, default=False, help="Fail if the last two loads differ."
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
@common_options
def status(as_of, days, stores, seed, verbose, assert_idempotent, as_json) -> None:
    """Run history, table inventory and defect tallies."""
    _configure_logging(verbose)
    cfg = _cfg(as_of, days, stores, seed)
    with connect(cfg, read_only=True) as con:
        report: dict = {"as_of": cfg.clock.as_of_date.isoformat()}
        runs = (
            query_df(
                con,
                "SELECT * FROM read_json_auto(?)",
                [str(cfg.paths.runs_dir / "latest.json")],
            )
            if (cfg.paths.runs_dir / "latest.json").exists()
            else pd.DataFrame()
        )
        report["latest_run"] = runs.to_dict("records")[0] if not runs.empty else None

        tables = inventory(con)
        report["tables"] = tables.to_dict("records")

        if _table_exists(con, "ops", "ingest_files"):
            tallies = query_df(
                con,
                """
                SELECT source,
                       count(*) FILTER (status = 'loaded')    AS loaded,
                       count(*) FILTER (status = 'duplicate') AS duplicate,
                       sum(rows_quarantined)                  AS quarantined_rows,
                       max(lag_days)                          AS max_lag_days
                FROM ops.ingest_files
                GROUP BY source ORDER BY source
                """,
            )
            report["ingest_by_source"] = tallies.to_dict("records")

        if assert_idempotent:
            # Genuine collisions are a source-day-store loaded from two different
            # FILES. Keying on (source, business_date) alone is wrong and produced
            # a false alarm on 360 healthy days: the POS feed legitimately lands
            # one file per store per day, so a day with several stores is normal.
            #
            # The real invariant is that no two distinct files claimed the same
            # store-day, which is what a duplicate or an append-instead-of-replace
            # would break.
            collisions = query_df(
                con,
                """
                SELECT source, file_name, count(*) AS ingest_rows
                FROM ops.ingest_files
                WHERE status = 'loaded'
                GROUP BY 1, 2
                HAVING count(*) > 1
                """,
            )
            dupes = query_scalar(
                con, "SELECT count(*) FROM ops.ingest_files WHERE status = 'duplicate'", default=0
            )
            # The checksum table is the authoritative idempotency evidence: a
            # checksum that loaded twice means the same bytes were processed
            # twice, whatever they were called.
            reused_checksums = query_scalar(
                con,
                """
                SELECT count(*) FROM (
                    SELECT checksum FROM ops.ingest_files
                    WHERE status = 'loaded'
                    GROUP BY 1 HAVING count(*) > 1
                )
                """,
                default=0,
            )
            report["idempotency"] = {
                "files_loaded_twice": len(collisions),
                "checksums_loaded_twice": reused_checksums,
                "redeliveries_deduplicated": dupes,
            }
            if len(collisions) or reused_checksums:
                raise click.ClickException(
                    f"{len(collisions)} file(s) and {reused_checksums} checksum(s) were "
                    "loaded more than once — the loader is not idempotent"
                )

    if as_json:
        click.echo(json.dumps(report, indent=2, default=str))
        return

    latest = report.get("latest_run")
    if latest:
        click.echo(
            f"latest run: {latest['run_name']} at {latest['started_utc']} "
            f"({latest['duration_seconds']}s, {len(latest['steps'])} steps)"
        )
        for step in latest["steps"]:
            flag = "ok" if step["status"] == "ok" else "FAILED"
            click.echo(f"  {step['step']:<16} {step['seconds']:>7.1f}s  {flag}  {step['detail']}")
    for row in report.get("ingest_by_source", []):
        click.echo(
            "  ingest {source:<15} loaded={loaded:<4} duplicate={duplicate:<3} "
            "quarantined={quarantined_rows:<5} max_lag={max_lag_days}".format(**row)
        )
    if "idempotency" in report:
        click.echo(f"  idempotency: {report['idempotency']}")


@cli.command(
    name="dbt", context_settings={"ignore_unknown_options": True, "allow_extra_args": True}
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@common_options
def dbt_cmd(as_of, days, stores, seed, verbose, args) -> None:
    """Run dbt with the project's clock and warehouse exported.

    Exists so the Makefile never has to remember the environment. Calling `dbt`
    directly works right up until it does not: `profiles.yml` has a default path,
    so a bare `dbt build` after `make ingest` will cheerfully build whichever
    database the default names. That is how a small world produced empty staging
    tables while every command reported success.
    """
    _configure_logging(verbose)
    cfg = _cfg(as_of, days, stores, seed)
    result = _dbt(cfg, *args, check=False)
    raise SystemExit(result.returncode)


def _table_exists(con, schema: str, table: str) -> bool:
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


def _latest_metrics(cfg: Config) -> dict | None:
    path = cfg.paths.report_dir / "metrics.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    headline = payload.get("headline", {})
    return {
        "sales": headline.get("sales_value", 0.0),
        "waste": headline.get("recorded_waste_value", 0.0),
        "shrink": headline.get("unrecorded_shrink_value", 0.0),
        "md": headline.get("markdown_erosion_value", 0.0),
    }


def main() -> None:  # pragma: no cover
    cli()


if __name__ == "__main__":  # pragma: no cover
    main()
