"""Configuration, paths and the pinned clock.

Every time-related decision in this project flows from one value,
``clock.as_of_date`` in ``config/project.yml``. A pipeline
that calls ``date.today()`` cannot be replayed: "the last 12 months" means
something different every time it runs, a backfill cannot be re-run, and a
reviewer cannot check a number in the report against the data that produced it.
Here "today" is **2026-09-10**, one month before the snapshot was assembled,
so every run of every layer reproduces the same numbers, and the same code still
accepts ``--as-of`` to run any other day.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = REPO_ROOT / "config" / "project.yml"
DEFECT_FILE = REPO_ROOT / "config" / "defect_catalogue.yml"

#: Overrides the as-of date without editing the file (CI, a what-if run).
AS_OF_ENV = "FRESH_SHELF_AS_OF"
#: Overrides the warehouse file (read by profiles.yml too).
DB_ENV = "FRESH_SHELF_DB"

DEFAULT_AS_OF = date(2026, 9, 10)


def to_date(value: date | datetime | str | None) -> date | None:
    """Accept a date, a datetime or an ISO string; return a date or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()


@dataclass(frozen=True)
class Clock:
    """The project's notion of "now".

    The trading window is ``history_days`` days ending on the last *closed*
    trading day, which is the day before ``as_of_date``: a trading day's feed
    lands the next morning, so on 2026-09-10 the warehouse can be complete for
    2026-09-09 and no further.
    """

    as_of_date: date
    history_days: int = 364
    timezone: str = "Australia/Adelaide"

    @property
    def last_trading_day(self) -> date:
        return self.as_of_date - timedelta(days=1)

    @property
    def first_trading_day(self) -> date:
        return self.last_trading_day - timedelta(days=self.history_days - 1)

    def trading_days(self) -> list[date]:
        """Every trading day in the window, oldest first."""
        span = (self.last_trading_day - self.first_trading_day).days + 1
        return [self.first_trading_day + timedelta(days=i) for i in range(span)]

    def is_in_window(self, day: date | str) -> bool:
        d = to_date(day)
        return self.first_trading_day <= d <= self.last_trading_day

    def describe(self) -> str:
        return (
            f"as-of {self.as_of_date:%Y-%m-%d} | trading window "
            f"{self.first_trading_day:%Y-%m-%d}..{self.last_trading_day:%Y-%m-%d} "
            f"({self.history_days} days) | {self.timezone}"
        )


@dataclass(frozen=True)
class Paths:
    """Resolved, absolute locations. Relative values resolve against the repo."""

    landing_dir: Path
    warehouse_file: Path
    truth_dir: Path
    artifacts_dir: Path
    build_dir: Path
    root: Path = REPO_ROOT

    def source_dir(self, source: str) -> Path:
        return self.landing_dir / source

    def landing_day_dir(self, source: str, landing_date: date) -> Path:
        return self.landing_dir / source / landing_date.strftime("%Y-%m-%d")

    @property
    def runs_dir(self) -> Path:
        return self.artifacts_dir / "runs"

    @property
    def logs_dir(self) -> Path:
        return self.artifacts_dir / "logs"

    @property
    def report_dir(self) -> Path:
        return self.build_dir / "report"

    def ensure(self) -> None:
        for path in (
            self.landing_dir,
            self.warehouse_file.parent,
            self.truth_dir,
            self.runs_dir,
            self.logs_dir,
            self.report_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Config:
    """Typed view over ``config/project.yml``."""

    project: dict[str, Any]
    world: dict[str, Any]
    economics: dict[str, Any]
    forecast: dict[str, Any]
    optimise: dict[str, Any]
    report_cfg: dict[str, Any]
    clock: Clock
    paths: Paths
    source_file: Path = CONFIG_FILE

    # -- convenience accessors -------------------------------------------------
    @property
    def timezone(self) -> str:
        return self.clock.timezone

    @property
    def seed(self) -> int:
        return int(self.world.get("seed", 20260910))

    @property
    def stores(self) -> str | list[str]:
        return self.world.get("stores", "all")

    @property
    def currency(self) -> str:
        return self.project.get("currency", "AUD")

    @property
    def trading_name(self) -> str:
        return self.project.get("trading_name", "the estate")

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = {
            "project": self.project,
            "world": self.world,
            "economics": self.economics,
            "forecast": self.forecast,
            "optimise": self.optimise,
            "report": self.report_cfg,
        }
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def dbt_env(self) -> dict[str, str]:
        """Environment for a dbt subprocess: the clock and the warehouse file."""
        return {
            AS_OF_ENV: self.clock.as_of_date.isoformat(),
            DB_ENV: str(self.paths.warehouse_file),
        }

    def override(
        self,
        *,
        as_of: date | str | None = None,
        history_days: int | None = None,
        stores: str | int | list[str] | None = None,
        seed: int | None = None,
        landing_dir: Path | str | None = None,
        warehouse_file: Path | str | None = None,
        truth_dir: Path | str | None = None,
        artifacts_dir: Path | str | None = None,
        build_dir: Path | str | None = None,
    ) -> Config:
        """A copy with a different clock, scale or set of directories.

        Used by `--as-of`, `--stores`, `--days` and by the test suite, which
        builds whole miniature worlds in tmp directories rather than sharing one
        mutable warehouse.
        """
        clock = self.clock
        if as_of is not None or history_days is not None:
            clock = Clock(
                as_of_date=to_date(as_of) or clock.as_of_date,
                history_days=history_days if history_days is not None else clock.history_days,
                timezone=clock.timezone,
            )

        world = dict(self.world)
        if stores is not None:
            world["stores"] = stores
        if seed is not None:
            world["seed"] = seed

        paths = self.paths
        path_updates: dict[str, Any] = {
            "landing_dir": landing_dir,
            "warehouse_file": warehouse_file,
            "truth_dir": truth_dir,
            "artifacts_dir": artifacts_dir,
            "build_dir": build_dir,
        }
        if any(v is not None for v in path_updates.values()):
            resolved = {}
            for name, value in path_updates.items():
                if value is None:
                    resolved[name] = getattr(paths, name)
                else:
                    p = Path(value).expanduser()
                    resolved[name] = p if p.is_absolute() else (paths.root / p)
            paths = Paths(root=paths.root, **resolved)

        return dataclasses.replace(self, clock=clock, world=world, paths=paths)


def _resolve(p: Path, root: Path) -> Path:
    p = Path(p).expanduser()
    return p if p.is_absolute() else root / p


def load_config(path: Path | str | None = None, *, as_of: date | str | None = None) -> Config:
    """Read the project config, applying env/argument overrides to the clock."""
    config_file = Path(path) if path else CONFIG_FILE
    raw = yaml.safe_load(config_file.read_text()) or {}

    clock_cfg = raw.get("clock", {})
    project_cfg = raw.get("project", {})

    resolved_as_of = (
        to_date(as_of)
        or to_date(os.environ.get(AS_OF_ENV))
        or to_date(clock_cfg.get("as_of_date"))
        or DEFAULT_AS_OF
    )

    clock = Clock(
        as_of_date=resolved_as_of,
        history_days=int(clock_cfg.get("history_days", 364)),
        timezone=project_cfg.get("timezone", "Australia/Adelaide"),
    )

    paths_cfg = raw.get("paths", {})
    warehouse_override = os.environ.get(DB_ENV)
    paths = Paths(
        landing_dir=_resolve(paths_cfg.get("landing_dir", "data/landing"), REPO_ROOT),
        warehouse_file=_resolve(
            warehouse_override
            or paths_cfg.get("warehouse_file", "data/warehouse/fresh_shelf.duckdb"),
            REPO_ROOT,
        ),
        truth_dir=_resolve(paths_cfg.get("truth_dir", "data/truth"), REPO_ROOT),
        artifacts_dir=_resolve(paths_cfg.get("artifacts_dir", "data/artifacts"), REPO_ROOT),
        build_dir=_resolve(paths_cfg.get("build_dir", "build"), REPO_ROOT),
    )

    return Config(
        project=project_cfg,
        world=raw.get("world", {}),
        economics=raw.get("economics", {}),
        forecast=raw.get("forecast", {}),
        optimise=raw.get("optimise", {}),
        report_cfg=raw.get("report", {}),
        clock=clock,
        paths=paths,
        source_file=config_file,
    )


def load_defect_catalogue(path: Path | str | None = None) -> list[dict[str, Any]]:
    """The injected-defect catalogue: what the sources do to the data, and how
    the loader is contracted to handle it (docs/DECISIONS.md, AD-02)."""
    catalogue_file = Path(path) if path else DEFECT_FILE
    raw = yaml.safe_load(catalogue_file.read_text()) or {}
    return raw.get("defects", [])


def defect_by_code(code: str, *, catalogue: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    for defect in catalogue if catalogue is not None else load_defect_catalogue():
        if defect["code"] == code:
            return defect
    raise KeyError(f"unknown defect code {code!r}")
