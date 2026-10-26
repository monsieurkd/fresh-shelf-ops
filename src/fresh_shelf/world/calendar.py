"""The trading calendar.

Fresh waste is a calendar problem before it is anything else: the estate closes
on Christmas Day and Good Friday, trades late on Thursdays, empties the seafood
counter in the week before Christmas and sells salads in a heatwave. This module
turns the pinned window into one row per day with the attributes demand hangs
off, and reads the SA public holidays and school term breaks from the curated
seeds. These are the same files dbt seeds into the warehouse, so the simulation
and the report can never disagree about whether 2026-04-03 was a trading day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from ..config import Config

#: Footfall by ISO weekday (1=Mon). Adelaide's late-night shopping Thursday and
#: the Saturday morning peak are the two shapes every fresh department lives on.
WEEKDAY_FOOTFALL = {1: 0.92, 2: 0.90, 3: 1.00, 4: 1.12, 5: 1.18, 6: 1.35, 7: 1.02}

#: Named retail days that are not public holidays but move the whole estate.
SPECIAL_DAYS: dict[tuple[int, int], tuple[str, float]] = {
    (12, 24): ("Christmas Eve", 1.90),
    (12, 23): ("Christmas Eve eve", 1.45),
    (12, 31): ("New Year's Eve", 1.40),
    (12, 27): ("Post-Christmas", 1.15),
    (1, 2): ("Post-New-Year", 1.05),
}

#: Black Friday (the last Friday in November). It is an imported event, but SA
#: fresh departments have felt it since about 2019.
BLACK_FRIDAY_FOOTFALL = 1.45

#: Public-holiday footfall. A closed store trades zero units (see `is_closed`).
HOLIDAY_FOOTFALL = {
    "Christmas Day": 0.0,
    "Good Friday": 0.0,
    "Proclamation Day": 0.55,
    "New Year's Day": 0.70,
    "Australia Day": 0.85,
    "Adelaide Cup Day": 0.78,
    "Easter Saturday": 1.15,
    "Easter Sunday": 0.60,
    "Easter Monday": 0.90,
    "Anzac Day": 0.55,
    "King's Birthday": 0.95,
    "Labour Day": 0.95,
}


@dataclass(frozen=True)
class TradingDay:
    day: date
    weekday: int
    month: int
    is_weekend: bool
    holiday_name: str | None
    is_closed: bool
    is_school_holiday: bool
    school_break: str | None
    special_day: str | None
    footfall_index: float

    @property
    def day_type(self) -> str:
        if self.is_closed:
            return "closed"
        if self.holiday_name:
            return "public_holiday"
        if self.special_day:
            return "special"
        if self.is_school_holiday:
            return "school_holiday"
        return "normal"


def _seeds_dir(cfg: Config) -> Path:
    return cfg.paths.root / "seeds"


def load_public_holidays(cfg: Config) -> pd.DataFrame:
    df = pd.read_csv(_seeds_dir(cfg) / "au_sa_public_holidays.csv")
    df["holiday_date"] = pd.to_datetime(df["holiday_date"]).dt.date
    df["closed_major_stores"] = df["closed_major_stores"].astype(str).str.lower() == "true"
    return df


def load_school_holidays(cfg: Config) -> pd.DataFrame:
    df = pd.read_csv(_seeds_dir(cfg) / "au_sa_school_holidays.csv")
    for col in ("start_date", "end_date"):
        df[col] = pd.to_datetime(df[col]).dt.date
    return df


def _black_friday(year: int) -> date:
    """Last Friday in November."""
    d = date(year, 11, 30)
    while d.weekday() != 4:  # 4 = Friday
        d -= timedelta(days=1)
    return d


def build_calendar(cfg: Config, *, only_day: date | None = None) -> pd.DataFrame:
    """One row per trading-window day, oldest first."""
    holidays = load_public_holidays(cfg)
    holiday_map: dict[date, tuple[str, bool]] = {
        row.holiday_date: (row.name, bool(row.closed_major_stores)) for row in holidays.itertuples()
    }
    breaks = load_school_holidays(cfg)

    rows: list[TradingDay] = []
    for day in cfg.clock.trading_days() if only_day is None else [only_day]:
        weekday = day.isoweekday()
        holiday = holiday_map.get(day)
        holiday_name = holiday[0] if holiday else None
        # The seed is the single source of truth for whether the estate trades:
        # closed means no sales, no deliveries and no counting that day, and the
        # stock on the shelf still ages.
        closed = bool(holiday[1]) if holiday else False
        special = SPECIAL_DAYS.get((day.month, day.day))
        footfall = WEEKDAY_FOOTFALL[weekday]
        if holiday_name:
            footfall = HOLIDAY_FOOTFALL.get(holiday_name, 0.9)
        elif special:
            footfall = special[1]
        if day == _black_friday(day.year):
            footfall *= BLACK_FRIDAY_FOOTFALL
            special = ("Black Friday", BLACK_FRIDAY_FOOTFALL)

        school_break = None
        for row in breaks.itertuples():
            if row.start_date <= day <= row.end_date:
                school_break = row.term_break
                break

        if holiday_name == "Easter Sunday":
            closed = True  # restricted trading: the estate does not open

        rows.append(
            TradingDay(
                day=day,
                weekday=weekday,
                month=day.month,
                is_weekend=weekday >= 6,
                holiday_name=holiday_name,
                is_closed=closed,
                is_school_holiday=school_break is not None,
                school_break=school_break,
                special_day=special[0] if special else None,
                footfall_index=round(0.0 if closed else footfall, 4),
            )
        )

    df = pd.DataFrame([r.__dict__ for r in rows])
    df["day_type"] = [r.day_type for r in rows]
    return df
