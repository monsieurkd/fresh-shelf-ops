"""Weather as a source feed.

Every fresh department is a weather derivative: salad and berries in a
heatwave, soup vegetables and roasts in a cold snap, and — the part that costs
money — a 43C day shortens the life of everything on the chilled shelf, so the
week after a heatwave is a waste week. The simulation therefore uses weather
twice: as a demand driver (``heat_sensitivity``/``rain_sensitivity``) and as a
spoilage driver.

The series is Adelaide-shaped rather than random: a proper seasonal cycle, an
autoregressive day-to-day carry-over, a wet winter, and a January heatwave —
the kind of event an optimiser has to survive rather than an outlier to be
smoothed away.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from ..config import Config

#: Long-run mean daily maximum by month for Adelaide (BOM-style climate normals).
ADELAIDE_MEAN_MAX = [29.0, 29.0, 26.0, 22.0, 18.0, 15.0, 15.0, 16.0, 19.0, 22.0, 25.0, 27.0]
ADELAIDE_RAIN_PROB = [0.10, 0.10, 0.14, 0.22, 0.32, 0.40, 0.42, 0.40, 0.32, 0.26, 0.20, 0.14]
ADELAIDE_RAIN_MM = [18.0, 15.0, 22.0, 38.0, 60.0, 72.0, 75.0, 68.0, 55.0, 45.0, 32.0, 24.0]

#: Store-level climate offsets (deg C) — the hills are cooler, the north warmer.
STORE_TEMP_OFFSET = {
    "S01": 0.3,
    "S02": -0.6,
    "S03": 0.2,
    "S04": 0.4,
    "S05": -1.6,
    "S06": 0.9,
    "S07": 1.9,
    "S08": -0.5,
}
STORE_RAIN_MULTIPLIER = {
    "S01": 1.0,
    "S02": 0.9,
    "S03": 0.95,
    "S04": 1.0,
    "S05": 1.45,  # Mount Barker: wettest part of the estate
    "S06": 0.95,
    "S07": 0.70,  # Whyalla: dry
    "S08": 0.95,
}

#: Heatwave events: a run of days that pushes the maximum well above normal.
#: The January 2026 event is the one that shows up in the February waste.
HEATWAVES: list[tuple[date, date, float]] = [
    (date(2025, 12, 18), date(2025, 12, 21), 9.0),
    (date(2026, 1, 22), date(2026, 1, 27), 13.0),
    (date(2026, 3, 12), date(2026, 3, 14), 7.5),
]


def _heatwave_boost(day: date) -> float:
    for start, end, boost in HEATWAVES:
        if start <= day <= end:
            return boost
    return 0.0


def build_weather(cfg: Config, store_codes: list[str]) -> pd.DataFrame:
    """One row per store per day: maximum, minimum, rainfall, heat flag."""
    rng = np.random.default_rng(cfg.seed + 7)
    days = cfg.clock.trading_days()
    n = len(days)

    # Estate-level series first (Adelaide normals), then a per-store offset, so
    # every store experiences the same heatwave on the same day.
    anomaly = np.zeros(n)
    rain_amount = np.zeros(n)
    for i, day in enumerate(days):
        month = day.month
        carry = anomaly[i - 1] * 0.62 if i else 0.0
        anomaly[i] = 0.62 * carry + rng.normal(0, 2.6) + _heatwave_boost(day)
        wet = rng.random() < ADELAIDE_RAIN_PROB[month - 1]
        if wet:
            mean_mm = ADELAIDE_RAIN_MM[month - 1] / (ADELAIDE_RAIN_PROB[month - 1] * 10)
            rain_amount[i] = float(rng.gamma(1.4, mean_mm / 1.4))

    rows = []
    for code in store_codes:
        temp_offset = STORE_TEMP_OFFSET.get(code, 0.0)
        rain_mult = STORE_RAIN_MULTIPLIER.get(code, 1.0)
        store_rng = np.random.default_rng(cfg.seed + 1000 + int(code[1:]))
        for i, day in enumerate(days):
            month = day.month
            max_temp = (
                ADELAIDE_MEAN_MAX[month - 1] + temp_offset + anomaly[i] + store_rng.normal(0, 1.1)
            )
            diurnal = 8.5 + 1.8 * (month in (0, 1, 2, 10, 11)) + store_rng.normal(0, 1.2)
            min_temp = max_temp - diurnal
            rain_mm = round(rain_amount[i] * rain_mult * float(store_rng.uniform(0.7, 1.3)), 1)
            rows.append(
                (
                    code,
                    day,
                    round(float(max_temp), 1),
                    round(float(min_temp), 1),
                    rain_mm,
                    bool(max_temp >= 35.0),
                    bool(min_temp <= 4.0),
                )
            )

    return pd.DataFrame(
        rows,
        columns=[
            "store_code",
            "weather_date",
            "max_temp_c",
            "min_temp_c",
            "rainfall_mm",
            "is_heat_day",
            "is_cold_night",
        ],
    )
