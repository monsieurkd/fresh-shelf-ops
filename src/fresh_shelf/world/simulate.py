"""The store-day simulation: what actually happened on the shelf.

One day at a time, per store, per ranged line:

    deliver -> sell at full price (FEFO) -> mark down what is about to expire
            -> sell some of that -> dispose of the rest -> count what is left

Everything the warehouse will later have to infer is produced here as truth
first: which batch expired on which day, what was thrown out, which disposals
were never scanned, and how much margin the markdown pass gave away. The
simulation is the ground truth the pipeline is tested against. The synthetic
world does not need to be realistic in every detail. It needs known answers, so
the pipeline's answers can be checked against them.

Three behaviours here are the problem the project optimises:

* Ordering follows the estate's habit: a naive average-sales forecast plus a
  per-store over-order bias, rounded up to the pack. On a short-life line, that
  bias becomes waste.
* Markdown is ad hoc. A store either remembers to do a pass or it does not, and
  the depth is a habit (30% on the last day, 50% on the day of expiry).
* Disposal scanning is incomplete, which is why recorded waste is a floor rather
  than the truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from math import ceil
from typing import Any
from zlib import crc32
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..config import Config
from . import calendar as calendar_module
from . import weather as weather_module
from .master import Item, Masters, Store, StoreItem, build_masters

#: Days simulated before the window opens, not emitted anywhere. Without this
#: the first fortnight reads as a store that teleported onto an empty shelf.
WARMUP_DAYS = 21

#: Share of the *remaining* stock that sells at a given markdown depth, before
#: the line's own responsiveness is applied. The optimiser never sees this
#: table: it has to estimate the response from observed markdown history, and
#: `truth_markdown_lift` exists so the estimate can be checked against it.
LIFT_BY_DEPTH: dict[float, float] = {
    0.10: 0.22,
    0.20: 0.36,
    0.30: 0.48,
    0.40: 0.59,
    0.50: 0.68,
    0.70: 0.82,
}

#: Preparation loss: the trim a department loses converting stock into the
#: product it sells (slicing hams, filleting whole fish, trimming leaves). It is
#: a loss, but it is not waste. The warehouse keeps the two apart.
PREP_LOSS_RATE = {"P": 0.012, "B": 0.0, "D": 0.025, "Y": 0.0, "S": 0.035}

#: Not every department peaks with footfall: milk is milk, bread is Saturday.
DEPT_FOOTFALL_ELASTICITY = {"P": 1.15, "B": 1.25, "D": 1.20, "S": 1.30, "Y": 0.75}

#: How much more a heating day spoils (chilled chain degrades over 35C).
COLD_CHAIN_LOSS_RATE = 0.030
RANDOM_QUALITY_RATE = 0.005
DAMAGE_RATE = 0.004

PREP_REASONS = {"P": ("TRIMMINGS", "PREP"), "D": ("TRIM", "PREP"), "S": ("TRIM", "BUTCHER TRIM")}
EXPIRY_REASONS = ("EXPIRED", "EXP", "EXPIRY", "BEST BEFORE")
QUALITY_REASONS = ("QUALITY", "MOULD", "SOFT", "SPOIL")
WEIGHED_DP = 3


def _round_qty(item: Item, qty: float) -> float:
    if item.is_weighed:
        return round(float(qty), WEIGHED_DP)
    return float(round(qty))


def _stable(name: str) -> int:
    return crc32(name.encode()) % 1_000_000


def markdown_lift(item: Item, depth: float) -> float:
    """The world's true markdown response for a line (impacted by habit)."""
    lift = LIFT_BY_DEPTH[depth] * item.markdown_responsiveness
    return float(min(0.95, lift))


@dataclass
class Batch:
    """One delivery line's remaining stock. FEFO consumes the earliest expiry."""

    line_id: str
    received_date: date
    expiry_date: date
    unit_cost: float
    qty_received: float
    qty_remaining: float
    source: str = "delivery"


@dataclass
class WorldData:
    """Everything the simulation produced: clean feeds, truth, and the context."""

    feeds: dict[str, pd.DataFrame]
    truth: dict[str, pd.DataFrame]
    masters: Masters
    calendar: pd.DataFrame
    weather: pd.DataFrame
    params: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [f"{name}: {len(df):,} rows" for name, df in sorted(self.feeds.items())]
        return "\n".join(parts)


class _Sink:
    """Append-only row buffers for one simulation run."""

    def __init__(self) -> None:
        self.pos: list[tuple] = []
        self.receipts: list[tuple] = []
        self.counts: list[tuple] = []
        self.disposals: list[tuple] = []  # every physical disposal/dispatch
        self.scans: list[tuple] = []  # the subset a handheld actually scanned
        self.markdowns: list[tuple] = []
        self.daily: list[tuple] = []
        self.expiry: list[tuple] = []


def _extend_calendar(cfg: Config, cal: pd.DataFrame, warmup_start: date) -> pd.DataFrame:
    """Add calendar rows for the warm-up days, so they are simulated too.

    The warm-up exists to carry shelf state (batches, expiry dates, the ordering
    habit's backlog) into the window. If those days have no calendar row the loop
    skips them and the window opens on a shelf that has never held stock, which
    shows up as a first day with a fifth of the usual stock on it.
    """
    warmup = calendar_module.build_calendar(
        cfg.override(
            as_of=cfg.clock.first_trading_day,
            history_days=(cfg.clock.first_trading_day - warmup_start).days + 1,
        )
    )
    if warmup.empty:
        return cal
    return (
        pd.concat([warmup, cal], ignore_index=True)
        .drop_duplicates(subset=["day"])
        .sort_values("day")
    )


def _extend_weather(cfg: Config, weather: pd.DataFrame, warmup_start: date) -> pd.DataFrame:
    """Add weather rows for the warm-up days, keyed the same way as the window."""
    warmup = weather_module.build_weather(
        cfg.override(
            as_of=cfg.clock.first_trading_day,
            history_days=(cfg.clock.first_trading_day - warmup_start).days + 1,
        ),
        sorted(weather["store_code"].unique().tolist()),
    )
    if warmup.empty:
        return weather
    combined = pd.concat([warmup, weather], ignore_index=True)
    return combined.drop_duplicates(subset=["store_code", "weather_date"]).sort_values(
        ["store_code", "weather_date"]
    )


def simulate(cfg: Config, masters: Masters | None = None) -> WorldData:
    masters = masters or build_masters(cfg)
    cal = calendar_module.build_calendar(cfg)
    store_codes = [s.code for s in masters.stores]
    weather = weather_module.build_weather(cfg, store_codes)

    # The warm-up days need calendar and weather rows too. Without them the
    # simulation skips them (`cal_row is None` -> continue), so the window opens
    # on a cold shelf: day one holds a Monday's deliveries against no carried
    # stock, which is not what a store in its second year of trading looks like.
    warmup_start = cfg.clock.first_trading_day - timedelta(days=WARMUP_DAYS)
    cal = _extend_calendar(cfg, cal, warmup_start)
    weather = _extend_weather(cfg, weather, warmup_start)

    rng = np.random.default_rng(cfg.seed)
    sink = _Sink()

    promos = _build_promotions(cfg, masters, rng)
    over_order_bias = {
        s.code: float(rng.uniform(0.06, 0.22) * (1.15 if s.format == "Express" else 1.0))
        for s in masters.stores
    }
    unhonoured_rate = 0.09

    cal_by_day = {row.day: row for row in cal.itertuples()}
    reliability_by_supplier = {s.code: s.reliability for s in masters.suppliers}

    for store in masters.stores:
        store_weather = {
            row.weather_date: (row.max_temp_c, row.min_temp_c, row.rainfall_mm)
            for row in weather[weather.store_code == store.code].itertuples()
        }
        ranged = sorted(masters.items_for_store(store.code), key=lambda i: i.sku)
        for item in ranged:
            si = masters.assortment[(store.code, item.sku)]
            item_rng = np.random.default_rng([cfg.seed, _stable(store.code), _stable(item.sku)])
            _simulate_store_item(
                cfg=cfg,
                store=store,
                item=item,
                si=si,
                warmup_start=warmup_start,
                cal_by_day=cal_by_day,
                store_weather=store_weather,
                item_rng=item_rng,
                sink=sink,
                promo_days=promos.get(item.sku, {}),
                over_order_bias=over_order_bias[store.code],
                unhonoured_rate=unhonoured_rate,
                reliability_by_supplier=reliability_by_supplier,
            )

    feeds = _build_feeds(cfg, masters, cal, weather, sink, promos)
    truth = _build_truth(sink, masters, cal, weather)
    return WorldData(
        feeds=feeds,
        truth=truth,
        masters=masters,
        calendar=cal,
        weather=weather,
        params={
            "seed": cfg.seed,
            "warmup_days": WARMUP_DAYS,
            "first_trading_day": cfg.clock.first_trading_day.isoformat(),
            "last_trading_day": cfg.clock.last_trading_day.isoformat(),
        },
    )


def _build_promotions(
    cfg: Config, masters: Masters, rng: np.random.Generator
) -> dict[str, dict[date, tuple[str, float, float]]]:
    """Catalogue promotions are estate-wide: one plan, every store that ranges it."""
    out: dict[str, dict[date, tuple[str, float, float]]] = {}
    window = cfg.clock.trading_days()
    for item in masters.items:
        if item.promos_per_year <= 0:
            continue
        days: dict[date, tuple[str, float, float]] = {}
        for _ in range(item.promos_per_year):
            start_idx = int(rng.integers(0, max(1, len(window) - 21)))
            length = int(rng.integers(4, 8))
            discount = float(rng.choice([0.10, 0.15, 0.20, 0.25, 0.30]))
            # A catalogue discount lifts units far more than a clear-out
            # markdown does. The feature exists for volume.
            uplift = 1.25 + 0.9 * discount
            name_map = {
                0.10: "Weekly Special",
                0.15: "Catalogue Special",
                0.20: "Big Deal",
                0.25: "Half Price Event",
                0.30: "Clearance Feature",
            }
            name = name_map[discount]
            for offset in range(length):
                idx = start_idx + offset
                if idx < len(window):
                    days[window[idx]] = (name, discount, uplift)
        out[item.sku] = days
    return out


def _day_demand_mean(
    item: Item,
    store: Store,
    si: StoreItem,
    day: date,
    cal_row: Any,
    max_temp: float,
    rain_mm: float,
    day_index: int,
    total_days: int,
    promo: tuple[str, float, float] | None,
) -> float:
    base_daily = item.weekly_units / 7.0 * store.size_index * si.variety_index
    season = item.season_factor(day.month)
    heat = 1.0 + item.heat_sensitivity * max(0.0, (max_temp - 25.0)) / 5.0
    wet = 1.0 + item.rain_sensitivity * (rain_mm / 10.0)
    footfall = 1.0 + (cal_row.footfall_index - 1.0) * DEPT_FOOTFALL_ELASTICITY[item.department]
    school = 1.0
    if cal_row.is_school_holiday:
        school = 1.06 if store.format == "Express" else 1.02
    trend = 1.0 + 0.05 * (day_index / max(1, total_days))  # a group that is growing slowly
    promo_mult = promo[2] if promo else 1.0
    return max(0.0, base_daily * season * heat * wet * footfall * school * trend * promo_mult)


def _sell(batches: list[Batch], qty: float, day: date) -> float:
    """Consume ``qty`` FEFO (earliest expiry first). Returns what was sold."""
    if qty <= 0:
        return 0.0
    order = sorted(
        (b for b in batches if b.qty_remaining > 0 and b.expiry_date >= day),
        key=lambda b: (b.expiry_date, b.received_date, b.line_id),
    )
    sold = 0.0
    for batch in order:
        if qty <= 0:
            break
        take = min(batch.qty_remaining, qty)
        batch.qty_remaining -= take
        qty -= take
        sold += take
    return sold


def _simulate_store_item(
    *,
    cfg: Config,
    store: Store,
    item: Item,
    si: StoreItem,
    warmup_start: date,
    cal_by_day: dict[date, Any],
    store_weather: dict[date, tuple[float, float, float]],
    item_rng: np.random.Generator,
    sink: _Sink,
    promo_days: dict[date, tuple[str, float, float]],
    over_order_bias: float,
    unhonoured_rate: float,
    reliability_by_supplier: dict[str, float],
) -> None:
    window = cfg.clock.trading_days()
    days = [warmup_start + timedelta(days=i) for i in range(WARMUP_DAYS)] + window
    total_days = len(window)
    emit_from = cfg.clock.first_trading_day
    batches: list[Batch] = []
    line_seq = 0
    staff = [f"{store.code}-T{i:02d}" for i in range(1, 6)]
    device = f"{store.code}-HH{int(item_rng.integers(1, 4)):02d}"
    prep_rate = PREP_LOSS_RATE[item.department]

    # Opening shelf: what was already in the store when the window opened.
    if item.shelf_life_days > 1:
        base_daily = item.weekly_units / 7.0 * store.size_index * si.variety_index
        for chunk in range(2):
            qty = _round_qty(item, base_daily * float(item_rng.uniform(0.5, 1.6)))
            if qty <= 0:
                continue
            batches.append(
                Batch(
                    line_id=f"{store.code}-{item.sku}-OPEN{chunk}",
                    received_date=days[0],
                    expiry_date=days[0]
                    + timedelta(
                        days=int(item_rng.integers(1, max(2, item.life_at_delivery_days + 1)))
                    ),
                    unit_cost=round(item.cost_price * float(item_rng.uniform(0.97, 1.03)), 4),
                    qty_received=qty,
                    qty_remaining=qty,
                    source="opening",
                )
            )

    for idx, day in enumerate(days):
        cal_row = cal_by_day.get(day)
        if cal_row is None:
            continue
        wx = store_weather.get(day)
        max_temp, _min_temp, rain_mm = wx if wx is not None else (22.0, 12.0, 0.0)
        closed = bool(cal_row.is_closed)
        emitting = day >= emit_from
        promo = promo_days.get(day)
        promo_active = promo is not None and item.department != "Y"

        delivered_qty = 0.0
        delivered_value = 0.0
        rejected_qty = 0.0

        # -- deliveries ----------------------------------------------------
        if not closed and day.isoweekday() in si.delivery_weekdays:
            gap = _next_delivery_gap(si.delivery_weekdays, day.isoweekday())
            soh_now = sum(b.qty_remaining for b in batches)
            ordered = _legacy_order_qty(item, store, si, day, gap, over_order_bias, soh_now)
            received = ordered
            unit_cost = _unit_cost_at(cfg, item, day, item_rng)
            life = item.life_at_delivery_days
            reliability = reliability_by_supplier.get(item.supplier_code, 0.95)
            if item_rng.random() > reliability:
                received = _round_qty(item, ordered * float(item_rng.uniform(0.6, 0.95)))
            substituted = False
            if item_rng.random() < 0.03:
                received = _round_qty(item, ordered * float(item_rng.uniform(1.3, 1.6)))
                unit_cost = round(unit_cost * float(item_rng.uniform(0.88, 0.95)), 4)
                life = max(1, life - 1)
                substituted = True
            # A rejected line is a real event in the GRN (a pallet that arrived
            # warm, a punnet that was already mouldy). It is recorded, not
            # subtracted from the delivery.
            if item_rng.random() < 0.02:
                rejected_qty = _round_qty(item, received * float(item_rng.uniform(0.05, 0.18)))
            if received > 0:
                line_seq += 1
                line_id = f"{store.code}-{item.sku}-L{line_seq:05d}"
                expiry = day + timedelta(days=max(1, life + int(item_rng.integers(-1, 1))))
                batches.append(
                    Batch(
                        line_id=line_id,
                        received_date=day,
                        expiry_date=expiry,
                        unit_cost=unit_cost,
                        qty_received=received,
                        qty_remaining=received,
                    )
                )
                delivered_qty = received
                delivered_value = received * unit_cost
                if emitting:
                    sink.receipts.append(
                        (
                            day,
                            store.code,
                            item.sku,
                            item.supplier_code,
                            f"INV-{day:%y%m}-{store.code}-{line_seq:05d}",
                            f"PO-{day:%y%m%d}-{store.code}",
                            _round_qty(item, ordered),
                            received,
                            rejected_qty,
                            unit_cost,
                            expiry,
                            item.unit,
                            substituted,
                            int(idx),
                        )
                    )

        # -- sales at full price -------------------------------------------
        demand = _draw_demand(
            item,
            _day_demand_mean(
                item,
                store,
                si,
                day,
                cal_row,
                max_temp,
                rain_mm,
                idx,
                total_days,
                promo if promo_active else None,
            ),
            item_rng,
        )
        if closed:
            demand = 0.0
            promo_active = False
        available_start = sum(b.qty_remaining for b in batches)
        sold_full = _round_qty(item, _sell(batches, demand, day)) if not closed else 0.0
        discount_value = 0.0

        # -- the markdown pass (ad hoc, at the evening pass) ----------------
        ticketed_qty = 0.0
        ticketed_depth = 0.0
        honoured_qty = 0.0
        if not closed and item.is_markdown_eligible:
            ticketed_qty, ticketed_depth, honoured_qty, discount_value = _markdown_pass(
                item=item,
                store=store,
                day=day,
                batches=batches,
                demand=demand,
                sold_full=sold_full,
                item_rng=item_rng,
                unhonoured_rate=unhonoured_rate,
                emit=emitting,
                sink=sink,
            )

        # -- what leaves the shelf without being sold ----------------------
        disposed_qty = 0.0
        prep_removed = 0.0
        prep_qty = 0.0
        throughput = sold_full + honoured_qty
        if not closed:
            # preparation loss: a trimmings yield on what the department handled
            if prep_rate > 0 and throughput > 0:
                prep_qty = _round_qty(
                    item, throughput * prep_rate * float(item_rng.uniform(0.6, 1.4))
                )
                prep_removed = _remove_from_shelf(batches, prep_qty, day)
                if prep_qty > 0 and emitting:
                    _record_disposal(
                        sink,
                        store,
                        item,
                        day,
                        _round_qty(item, prep_removed),
                        item_rng.choice(PREP_REASONS.get(item.department, ("PREP",))),
                        staff,
                        device,
                        rng=item_rng,
                        local_hour=float(item_rng.uniform(7.0, 15.0)),
                        # Preparation trims go in the prep bin, which is scanned
                        # on a bin-by-bin routine rather than per item. Hence
                        # the flat 50%, independent of the store's discipline.
                        scan_probability=0.5,
                        loss_class="preparation",
                    )
            # random quality losses, and a cold-chain hit on a hot day
            if throughput > 0 or available_start > 0:
                random_rate = RANDOM_QUALITY_RATE + (DAMAGE_RATE if item.is_weighed else 0.0)
                if max_temp >= 35.0 and item.department in {"P", "D", "S", "Y"}:
                    random_rate += COLD_CHAIN_LOSS_RATE * (1.0 if item.department != "Y" else 0.4)
                if item_rng.random() < random_rate * 4:
                    qty = _round_qty(
                        item, max(0.05, available_start * float(item_rng.uniform(0.005, 0.04)))
                    )
                    if qty > 0:
                        removed = _remove_from_shelf(batches, qty, day)
                        # Account for it: this stock left the shelf, so it is
                        # waste. Forgetting to add it here made the daily ledger
                        # disagree with the disposal records by exactly the
                        # random-quality losses, which is the kind of gap that
                        # silently becomes "unrecorded shrink" downstream.
                        disposed_qty += _round_qty(item, removed)
                        reason = str(
                            item_rng.choice(
                                ("TEMPERATURE", "COLD CHAIN")
                                if max_temp >= 35.0
                                else QUALITY_REASONS
                            )
                        )
                        if emitting:
                            _record_disposal(
                                sink,
                                store,
                                item,
                                day,
                                qty,
                                reason,
                                staff,
                                device,
                                rng=item_rng,
                                local_hour=float(item_rng.uniform(8.0, 17.0)),
                                scan_probability=store.scan_compliance,
                                loss_class="waste",
                            )

            # end of day: everything that has reached its expiry is disposed
            expired_qty = sum(b.qty_remaining for b in batches if b.expiry_date <= day)
            if expired_qty > 0:
                _remove_from_shelf(batches, expired_qty, day, only_expired=True)
                disposed_qty = _round_qty(item, expired_qty)
                if disposed_qty > 0 and emitting:
                    _record_disposal(
                        sink,
                        store,
                        item,
                        day,
                        disposed_qty,
                        item_rng.choice(EXPIRY_REASONS),
                        staff,
                        device,
                        rng=item_rng,
                        local_hour=float(item_rng.uniform(18.0, 21.5)),
                        scan_probability=store.scan_compliance
                        * (0.94 if cal_row.is_weekend else 1.0),
                        loss_class="waste",
                    )

        # Keep the batch list bounded: depleted deliveries would otherwise
        # accumulate for a year of trading days and slow every sort below.
        if len(batches) > 12:
            batches = [b for b in batches if b.qty_remaining > 0]

        soh_end = sum(b.qty_remaining for b in batches)
        soh_value = sum(b.qty_remaining * b.unit_cost for b in batches)
        sold_units = sold_full + honoured_qty
        shortfall = max(0.0, demand - sold_units) if not closed else 0.0
        net_sales = (
            sold_full * item.retail_price + honoured_qty * item.retail_price - discount_value
        )

        # -- physical counts ------------------------------------------------
        if not closed and emitting and _is_count_day(store, item, day, item_rng):
            counted_qty, counted_in_packs = _apply_counting_error(soh_end, item, rng=item_rng)
            sink.counts.append(
                (
                    day,
                    store.code,
                    item.sku,
                    counted_qty,
                    _count_type(store, item, day),
                    str(item_rng.choice(staff)),
                    "pack" if counted_in_packs else "each",
                    f"C{day:%y%m%d}-{store.code}-{item.sku}",
                )
            )

        if emitting:
            if sold_units > 0 or demand > 0:
                sink.pos.append(
                    (
                        day,
                        store.code,
                        item.sku,
                        _round_qty(item, sold_units),
                        round(sold_full * item.retail_price + honoured_qty * item.retail_price, 2),
                        round(discount_value, 2),
                        int(max(1, ceil(sold_units / 1.4))) if sold_units > 0 else 0,
                        0.0,
                        item.unit,
                        promo[0] if promo_active else None,
                    )
                )
            sink.daily.append(
                (
                    store.code,
                    item.sku,
                    day,
                    item.department,
                    item.category,
                    _round_qty(item, sold_units),
                    _round_qty(item, sold_full),
                    _round_qty(item, honoured_qty),
                    round(discount_value, 2),
                    round(net_sales, 2),
                    round(disposed_qty, 3),
                    round(prep_removed, 3),
                    round(ticketed_qty, 3),
                    round(honoured_qty, 3),
                    round(soh_end, 3),
                    round(soh_value, 2),
                    round(shortfall, 3),
                    round(delivered_qty, 3),
                    round(delivered_value, 2),
                    round(rejected_qty, 3),
                    1 if closed else 0,
                    bool(cal_row.is_school_holiday),
                    float(max_temp),
                    float(rain_mm),
                    int(ticketed_depth * 100),
                )
            )
            for bucket in _expiry_buckets(batches, day):
                sink.expiry.append((store.code, item.sku, day, bucket[0], bucket[1], bucket[2]))


def _next_delivery_gap(weekdays: tuple[int, ...], today_iso: int) -> int:
    """Days of cover to order for: up to the day before the next delivery."""
    if not weekdays:
        return 3
    for offset in range(1, 9):
        if ((today_iso - 1 + offset) % 7) + 1 in weekdays:
            return offset
    return 3


def _legacy_order_qty(
    item: Item,
    store: Store,
    si: StoreItem,
    day: date,
    gap: int,
    over_order_bias: float,
    current_soh: float,
) -> float:
    """The ordering habit that causes the waste.

    An order-up-to rule, which is what a store system actually runs: take the
    average daily sales, multiply by the days until the next delivery plus a
    safety day, add the store's over-order bias, subtract what is already on the
    shelf, round up to the pack.

    Two things make it the problem rather than the solution. It prices cover from
    an average that knows nothing about tomorrow's weather, footfall or holiday.
    And it never looks at how long the stock on the shelf actually lives, so a
    line with two days of life gets ordered to cover five days of trading.
    """
    base_daily = item.weekly_units / 7.0 * store.size_index * si.variety_index
    cover = gap + 1.0
    target = base_daily * cover * (1.0 + over_order_bias) * item.season_factor(day.month)
    shortfall = target - current_soh
    if shortfall <= 0:
        # Nothing needed, unless the shelf is thinner than a day's trade. In
        # that case even a disciplined store tops up rather than risk a gap.
        return float(item.pack_size) if current_soh < base_daily else 0.0
    packs = max(1.0, ceil(shortfall / item.pack_size))
    return packs * item.pack_size


def _unit_cost_at(cfg: Config, item: Item, day: date, rng: np.random.Generator) -> float:
    """Cost moves with the season: tomatoes are dear in July, prawns in December."""
    season = item.season_factor(day.month)
    if item.department in {"P", "S", "B"}:
        seasonal_index = 1.0 + 0.14 * (1.0 / max(0.6, season) - 1.0)
    else:
        seasonal_index = 1.0
    drift = 1.0 + 0.03 * ((day.month - 1) / 12.0)  # gentle inflation across the year
    return round(item.cost_price * seasonal_index * drift * float(rng.uniform(0.97, 1.03)), 4)


def _draw_demand(item: Item, mean: float, rng: np.random.Generator) -> float:
    if mean <= 0:
        return 0.0
    if item.is_weighed:
        sd = 0.32 * mean
        return max(0.0, float(rng.normal(mean, sd)))
    # Negative binomial: fast movers are steady, slow movers are lumpy (0/1/2),
    # which is what makes a slow fresh line so hard to order and so easy to waste.
    dispersion = 8.0 if mean >= 8 else 3.0
    p = dispersion / (dispersion + mean)
    return float(rng.negative_binomial(dispersion, p))


def _markdown_pass(
    *,
    item: Item,
    store: Store,
    day: date,
    batches: list[Batch],
    demand: float,
    sold_full: float,
    item_rng: np.random.Generator,
    unhonoured_rate: float,
    emit: bool,
    sink: _Sink,
) -> tuple[float, float, float, float]:
    """The evening markdown pass, as it is actually run: by habit, not by model.

    Returns (ticketed qty, depth, units that sold at the marked price, margin
    given away). Some of what is ticketed rings through at full price anyway.
    That is the print-and-not-honour problem (D13).
    """
    expiring_today = sum(b.qty_remaining for b in batches if b.expiry_date <= day)
    expiring_tomorrow = sum(
        b.qty_remaining for b in batches if b.expiry_date == day + timedelta(days=1)
    )
    remaining = sum(b.qty_remaining for b in batches)

    ticketed = 0.0
    depth = 0.0
    if expiring_today > 0 and item_rng.random() < 0.75 * store.markdown_discipline:
        depth = 0.70 if item_rng.random() < 0.25 else 0.50
        ticketed = min(
            remaining, expiring_today + (expiring_tomorrow * 0.5 if depth >= 0.5 else 0.0)
        )
    elif expiring_tomorrow > 0 and item_rng.random() < 0.55 * store.markdown_discipline:
        depth = 0.50 if item_rng.random() < 0.2 else 0.30
        ticketed = min(remaining, expiring_tomorrow + expiring_today)
    if ticketed <= 0 or depth <= 0:
        return 0.0, 0.0, 0.0, 0.0

    lift = markdown_lift(item, depth) * float(item_rng.uniform(0.85, 1.15))
    moved = _round_qty(item, ticketed * min(0.95, lift))
    if moved <= 0:
        return 0.0, 0.0, 0.0, 0.0
    # `ticketed` is what the sticker went on and actually moved. `honoured` is
    # the share the till rang at the marked price. The rest of the ticket was
    # rung at full price by mistake (D13).
    ticketed = moved
    honoured = _round_qty(item, moved * (1.0 - unhonoured_rate))
    # Erosion is what the *customer* was given off, so it includes the units the
    # markdown moved even where a till failed to honour the ticket. If erosion
    # tracked only the honoured share it would understate the margin given away,
    # and D13 exists because printed and realised differ.
    discount = moved * item.retail_price * depth

    # Sell what the markdown moved (the rest of the ticket rings at full price).
    _sell(batches, moved, day)

    if emit:
        pass_name = "18:00 evening pass" if item_rng.random() < 0.8 else "14:00 afternoon pass"
        hour = 18.0 if "18:00" in pass_name else 14.0
        sink.markdowns.append(
            (
                datetime.combine(day, time(int(hour), int(item_rng.integers(0, 60)))),
                store.code,
                item.sku,
                depth,
                item.retail_price,
                round(item.retail_price * (1 - depth), 2),
                _round_qty(item, ticketed),
                _round_qty(item, honoured),
                _round_qty(item, moved),
                pass_name,
                "manual" if item_rng.random() < 0.85 else "auto_suggest",
                "each" if not item.is_weighed else "kg",
            )
        )
    return _round_qty(item, ticketed), depth, honoured, discount


def _remove_from_shelf(
    batches: list[Batch], qty: float, day: date, *, only_expired: bool = False
) -> float:
    """Take stock off the shelf for a loss, oldest expiry first.

    FEFO again: whether it is a trim, a damaged punnet or an expired tub, the
    stock that leaves is the stock closest to its best-before.
    """
    if qty <= 0:
        return 0.0
    candidates = [
        b for b in batches if b.qty_remaining > 0 and (b.expiry_date <= day or not only_expired)
    ]
    order = sorted(candidates, key=lambda b: (b.expiry_date, b.received_date, b.line_id))
    removed = 0.0
    for batch in order:
        if qty <= 0:
            break
        take = min(batch.qty_remaining, qty)
        batch.qty_remaining -= take
        qty -= take
        removed += take
    return removed


def _record_disposal(
    sink: _Sink,
    store: Store,
    item: Item,
    day: date,
    qty: float,
    reason: Any,
    staff: list[str],
    device: str,
    *,
    rng: np.random.Generator,
    local_hour: float,
    scan_probability: float,
    loss_class: str,
) -> None:
    """A physical disposal happens; whether it gets scanned is a separate event.

    The scanned subset is what the loss-scan feed contains. The rest is real
    stock loss that no feed records. That is why the warehouse reconciles the
    ledger against counts instead of trusting the scan file.
    """
    scanned = bool(rng.random() < scan_probability)
    staff_id = staff[abs(_stable(item.sku + str(day.toordinal()))) % len(staff)]
    local_dt = datetime.combine(day, time(int(local_hour), int((local_hour % 1) * 60)))
    utc_ts = (
        local_dt.replace(tzinfo=ZoneInfo("Australia/Adelaide")).astimezone(UTC).replace(tzinfo=None)
    )
    reason_code = str(reason)
    sink.disposals.append(
        (store.code, item.sku, day, local_dt, qty, reason_code, loss_class, scanned, item.unit)
    )
    if scanned:
        sink.scans.append(
            (
                utc_ts,
                store.code,
                item.sku,
                qty,
                reason_code,
                item.unit,
                staff_id,
                device,
                "waste stream" if loss_class == "waste" else "prep bin",
            )
        )


def _expiry_buckets(batches: list[Batch], day: date) -> list[tuple[date, float, float]]:
    buckets: dict[date, tuple[float, float]] = {}
    for batch in batches:
        if batch.qty_remaining <= 0:
            continue
        qty, value = buckets.get(batch.expiry_date, (0.0, 0.0))
        buckets[batch.expiry_date] = (
            qty + batch.qty_remaining,
            value + batch.qty_remaining * batch.unit_cost,
        )
    return [
        (expiry, round(qty, 4), round(value, 4))
        for expiry, (qty, value) in sorted(buckets.items())
        if qty > 0
    ]


def _count_type(store: Store, item: Item, day: date) -> str:
    if day.isoweekday() == 7:
        return "weekly_full"
    if item.shelf_life_days <= 3 and store.markdown_discipline >= 1.0:
        return "cycle_high_risk"
    if day.isoweekday() == 3 and store.code in {"S01", "S03", "S04"}:
        return "midweek_full"
    return "cycle_high_risk"


def _is_count_day(store: Store, item: Item, day: date, rng: np.random.Generator) -> bool:
    """Which store-item-days get a physical count.

    Sunday close is a full estate count; the genuinely high-risk short-life
    lines at disciplined stores are counted daily; three metro stores run a
    midweek full count. Everything else waits for Sunday.
    """
    if day.isoweekday() == 7:
        return True
    if item.shelf_life_days <= 3 and store.markdown_discipline >= 1.0:
        return True
    return day.isoweekday() == 3 and store.code in {"S01", "S03", "S04"}


def _apply_counting_error(
    qty: float, item: Item, *, rng: np.random.Generator
) -> tuple[float, bool]:
    """Counts are eyeballed, and pack items get counted in packs (D11)."""
    value = qty * float(rng.normal(1.0, 0.03))
    if item.pack_size > 1 and rng.random() < 0.05:
        return _round_qty(item, round(value / item.pack_size)), True
    return _round_qty(item, value), False


# ---------------------------------------------------------------------------
# Assembling the feeds and the sealed truth
# ---------------------------------------------------------------------------
def _build_feeds(
    cfg: Config,
    masters: Masters,
    cal: pd.DataFrame,
    weather: pd.DataFrame,
    sink: _Sink,
    promos: dict[str, dict[date, tuple[str, float, float]]],
) -> dict[str, pd.DataFrame]:
    """The source-system extracts, before anything is done to them.

    These frames are *clean*. `fresh_shelf.landing.writer` is what breaks them
    on the way to disk: duplicated, late, mis-unitised, mistimed.
    """
    feeds: dict[str, pd.DataFrame] = {}

    feeds["pos_sales"] = _frame(
        sink.pos,
        [
            "business_date",
            "store_code",
            "sku",
            "units",
            "gross_sales",
            "discount_amount",
            "transaction_count",
            "void_units",
            "unit_of_measure",
            "promo_name",
        ],
    )
    feeds["goods_received"] = _frame(
        sink.receipts,
        [
            "delivery_date",
            "store_code",
            "sku",
            "supplier_code",
            "invoice_no",
            "purchase_order",
            "qty_ordered",
            "qty_received",
            "qty_rejected",
            "unit_cost",
            "expiry_date",
            "unit_of_measure",
            "was_substituted",
            "delivery_sequence",
        ],
    )
    feeds["stock_counts"] = _frame(
        sink.counts,
        [
            "count_date",
            "store_code",
            "sku",
            "counted_qty",
            "count_type",
            "counted_by",
            "unit_of_measure",
            "count_reference",
        ],
    )
    feeds["waste_events"] = _frame(
        sink.scans,
        [
            "event_ts_utc",
            "store_code",
            "sku",
            "qty",
            "reason_code",
            "unit_of_measure",
            "staff_id",
            "device_id",
            "disposal_method",
        ],
    )
    feeds["markdown_events"] = _frame(
        sink.markdowns,
        [
            "printed_ts_local",
            "store_code",
            "sku",
            "markdown_pct",
            "base_price",
            "markdown_price",
            "qty_ticketed",
            "qty_honoured",
            "qty_sold_at_markdown",
            "pass_name",
            "trigger",
            "unit_of_measure",
        ],
    )
    feeds["promotions"] = _promotion_frame(masters, promos)
    feeds["weather"] = weather.copy()
    feeds["item_master"] = _item_master_frame(masters)
    feeds["store_master"] = _store_master_frame(masters)
    feeds["supplier_master"] = _supplier_master_frame(masters)
    return feeds


def _frame(rows: list[tuple], columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns)


def _promotion_frame(
    masters: Masters, promos: dict[str, dict[date, tuple[str, float, float]]]
) -> pd.DataFrame:
    rows: list[tuple] = []
    week = 0
    for sku, days in sorted(promos.items()):
        by_name: dict[str, list[date]] = {}
        for day, (name, _discount, _uplift) in sorted(days.items()):
            by_name.setdefault(name, []).append(day)
        for name, day_list in by_name.items():
            # contiguous runs of the same promotion are one planned event
            run: list[date] = []
            for day in day_list:
                if run and (day - run[-1]).days > 1:
                    week += 1
                    rows.append(_promo_row(sku, name, run, promos[sku], week))
                    run = []
                run.append(day)
            if run:
                week += 1
                rows.append(_promo_row(sku, name, run, promos[sku], week))
    return _frame(
        rows,
        [
            "sku",
            "promo_name",
            "start_date",
            "end_date",
            "promo_type",
            "discount_pct",
            "demand_uplift_factor",
            "catalogue_week",
        ],
    )


def _promo_row(
    sku: str, name: str, run: list[date], days: dict[date, tuple[str, float, float]], week: int
) -> tuple:
    discount = days[run[0]][1]
    uplift = days[run[0]][2]
    promo_type = "catalogue feature" if discount >= 0.2 else "weekly special"
    return (sku, name, run[0], run[-1], promo_type, discount, round(uplift, 3), f"CW{week:02d}")


def _item_master_frame(masters: Masters) -> pd.DataFrame:
    rows = [
        (
            item.sku,
            item.name,
            item.department,
            item.department_name,
            item.category,
            item.unit,
            item.pack_size,
            item.retail_price,
            item.cost_price,
            item.shelf_life_days,
            item.life_at_delivery_days,
            item.supplier_code,
            "Chilled 0-4C"
            if item.department != "P" or item.category == "Herbs"
            else "Chilled 4-8C",
            item.is_markdown_eligible,
            item.shelf_life_days <= 21,
            round(item.gross_margin_rate, 4),
            True,
        )
        for item in masters.items
    ]
    return _frame(
        rows,
        [
            "sku",
            "item_name",
            "department_code",
            "department_name",
            "category",
            "unit_of_measure",
            "pack_size",
            "retail_price",
            "standard_cost",
            "shelf_life_days",
            "life_at_delivery_days",
            "supplier_code",
            "storage_condition",
            "is_markdown_eligible",
            "is_short_life",
            "standard_margin_rate",
            "is_active",
        ],
    )


def _store_master_frame(masters: Masters) -> pd.DataFrame:
    rows = [
        (
            s.code,
            s.name,
            s.suburb,
            s.postcode,
            "SA",
            s.latitude,
            s.longitude,
            s.format,
            s.size_index,
            s.opened,
            s.region,
            "Australia/Adelaide",
        )
        for s in masters.stores
    ]
    return _frame(
        rows,
        [
            "store_code",
            "store_name",
            "suburb",
            "postcode",
            "state",
            "latitude",
            "longitude",
            "store_format",
            "size_index",
            "opened_date",
            "region",
            "timezone",
        ],
    )


def _supplier_master_frame(masters: Masters) -> pd.DataFrame:
    rows = [
        (
            s.code,
            s.name,
            s.type,
            s.region,
            s.lead_time_days,
            ",".join(str(d) for d in s.delivery_weekdays),
            s.min_order_value,
            s.reliability,
        )
        for s in masters.suppliers
    ]
    return _frame(
        rows,
        [
            "supplier_code",
            "supplier_name",
            "supplier_type",
            "region",
            "lead_time_days",
            "delivery_weekdays",
            "min_order_value",
            "reliability",
        ],
    )


def _build_truth(
    sink: _Sink, masters: Masters, cal: pd.DataFrame, weather: pd.DataFrame
) -> dict[str, pd.DataFrame]:
    """Sealed answers, written outside the landing zone.

    The pipeline never reads these; the test suite does. They are what makes
    "the loader recovers the truth" and "the FEFO model agrees with reality"
    checkable statements rather than claims.
    """
    truth: dict[str, pd.DataFrame] = {}
    truth["truth_disposals"] = _frame(
        sink.disposals,
        [
            "store_code",
            "sku",
            "business_date",
            "disposed_at_local",
            "qty",
            "reason_code",
            "loss_class",
            "was_scanned",
            "unit_of_measure",
        ],
    )
    truth["truth_store_item_day"] = _frame(
        sink.daily,
        [
            "store_code",
            "sku",
            "business_date",
            "department_code",
            "category",
            "units_sold",
            "units_sold_full_price",
            "units_sold_markdown",
            "markdown_erosion_value",
            "net_sales_value",
            "waste_qty",
            "prep_loss_qty",
            "markdown_ticketed_qty",
            "markdown_honoured_qty",
            "soh_eod_qty",
            "soh_eod_value",
            "lost_sales_qty",
            "delivered_qty",
            "delivered_value",
            "rejected_qty",
            "is_closed",
            "is_school_holiday",
            "max_temp_c",
            "rainfall_mm",
            "markdown_depth_pct",
        ],
    )
    truth["truth_expiry_buckets"] = _frame(
        sink.expiry,
        ["store_code", "sku", "business_date", "expiry_date", "qty_remaining", "value_remaining"],
    )
    truth["truth_markdown_lift"] = pd.DataFrame(
        [
            {
                "sku": item.sku,
                "department_code": item.department,
                "markdown_pct": depth,
                "true_lift_share": round(markdown_lift(item, depth), 4),
                "responsiveness": item.markdown_responsiveness,
                "shelf_life_days": item.shelf_life_days,
            }
            for item in masters.items
            for depth in sorted(LIFT_BY_DEPTH)
        ]
    )
    truth["truth_calendar"] = cal.copy()
    truth["truth_weather"] = weather.copy()
    return truth
