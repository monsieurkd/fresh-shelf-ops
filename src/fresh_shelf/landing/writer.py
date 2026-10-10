"""Writing the simulated sources to disk, as the source systems really drop them.

The simulation produces clean frames. This module is what makes them a *data
engineering* problem: it writes each feed as the file a source system would
actually have put in a shared folder — one file per store-day, named by whoever
wrote it, arriving on the date the integration ran rather than the date it
describes, with the defects in `config/defect_catalogue.yml` applied en route.

Two deliberate choices:

* Files are dropped into ``data/landing/<source>/<landing_date>/`` where
  ``landing_date`` is when the file *arrived*, not what it contains. That is the
  whole reason a late file (D02) is a real problem in this pipeline: the
  partition tells you nothing about the business date inside, so the loader has
  to read the file.
* Every file carries a header marker the loader can use where the source system
  does publish one — the money unit (D05) is in the CSV header comment, because
  that is how the till actually marks a cents export.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..config import Config, load_defect_catalogue
from ..world.simulate import WorldData

#: The .NET integration's own idempotency key: deterministic per (store, business
#: day), so a redelivery of the same extract is recognisable even if the file is
#: renamed. It is how a real integration protects itself, and it is what makes
#: D01 detectable without trusting filenames.
BATCH_TEMPLATE = "B{code}{day:%Y%m%d}"

MONEY_UNIT_CENTS_FROM = 0
MONEY_UNIT_CENTS_TO = 61  # the two months in which the old till firmware runs


@dataclass(frozen=True)
class LandingFile:
    """A file that was written to the landing zone."""

    source: str
    business_date: date | None
    landing_date: date
    path: Path
    rows: int
    checksum: str
    size_bytes: int
    is_duplicate_of: str | None = None

    def as_record(self) -> dict:
        return {
            "source": self.source,
            "business_date": self.business_date,
            "landing_date": self.landing_date,
            "file_name": self.path.name,
            "file_path": str(self.path.relative_to(self.path.parents[2])),
            "rows": self.rows,
            "checksum": self.checksum,
            "size_bytes": self.size_bytes,
            "is_duplicate_of": self.is_duplicate_of,
        }


class LandingWriter:
    def __init__(self, cfg: Config, defects: list[dict] | None = None) -> None:
        self.cfg = cfg
        self.defects = {d["code"]: d for d in (defects or load_defect_catalogue())}
        self.written: list[LandingFile] = []
        self._seen_checksums: dict[str, str] = {}
        # Deliberate gaps: (source, store, business day) the source never sends.
        self.dropped_store_days: set[tuple[str, str, date]] = set()
        self.late_store_days: set[tuple[str, str, date]] = set()

    # -- helpers ---------------------------------------------------------------
    def _write(
        self,
        source: str,
        business_date: date | None,
        frame: pd.DataFrame,
        *,
        file_name: str,
        landing_date: date,
        suffix: str = ".csv",
        header_comment: str | None = None,
    ) -> LandingFile:
        if not file_name.endswith(suffix):
            file_name = f"{file_name}{suffix}"
        directory = self.cfg.paths.landing_day_dir(source, landing_date)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / file_name

        buffer = io.StringIO()
        if header_comment:
            buffer.write(f"# {header_comment}\n")
        frame.to_csv(buffer, index=False, lineterminator="\n")
        payload = buffer.getvalue().encode()
        checksum = hashlib.sha256(payload).hexdigest()

        duplicate_of = None
        if checksum in self._seen_checksums:
            duplicate_of = self._seen_checksums[checksum]
        else:
            self._seen_checksums[checksum] = str(path.relative_to(self.cfg.paths.landing_dir))

        path.write_bytes(payload)
        record = LandingFile(
            source=source,
            business_date=business_date,
            landing_date=landing_date,
            path=path,
            rows=len(frame),
            checksum=checksum,
            size_bytes=len(payload),
            is_duplicate_of=duplicate_of,
        )
        self.written.append(record)
        return record

    def _rng(self, *parts: object) -> np.random.Generator:
        key = ":".join(str(p) for p in parts)
        return np.random.default_rng(
            [self.cfg.seed, int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)]
        )

    def _landing_date_for(self, store: str, business_date: date) -> date:
        """The morning this store's extract is expected to land."""
        return business_date + timedelta(days=1 + self._landing_lag(store, business_date))

    def _outage_store_days(self) -> set[tuple[str, date]]:
        """D03: the store-days whose extract is never sent at all.

        Precomputed as an explicit set of (store, day) pairs rather than decided
        per store-day. The first version resolved the outage day correctly and
        then dropped *every* store trading that day, which silently deleted the
        rest of the estate from the landing zone — a bug that made five other
        tests fail for the wrong reason.
        """
        count = int(self.defects["D03"]["injection"]["days"])
        if count <= 0:
            return set()
        window = self.cfg.clock.trading_days()
        candidates = sorted({window[len(window) // 3], window[(2 * len(window)) // 3]})[:count]
        stores = sorted(set(self._store_codes))
        return {(stores[day.toordinal() % len(stores)], day) for day in candidates}

    def _landing_lag(self, store: str, business_date: date) -> int:
        """D02: the two regional stores land a day after next."""
        rule = self.defects["D02"]["injection"]
        if store not in set(rule["stores"]):
            return 0
        rng = self._rng("late", store, business_date)
        return int(rule["lag_days"]) if rng.random() < float(rule["probability_per_file"]) else 0

    def _is_cents_store(self, store: str, business_date: date) -> bool:
        rule = self.defects["D05"]["injection"]
        if store not in set(rule["stores"]):
            return False
        window = self.cfg.clock.trading_days()
        offset = (business_date - window[0]).days
        return int(rule.get("from_offset_days", 0)) <= offset <= int(rule.get("to_offset_days", 61))

    # -- sources ---------------------------------------------------------------
    def write_all(self, world: WorldData) -> list[LandingFile]:
        self._store_codes = [s.code for s in world.masters.stores]
        self._items = world.feeds["item_master"].set_index("sku").to_dict("index")
        self._outages = self._outage_store_days()

        self.write_master_data(world)
        self.write_pos_sales(world)
        self.write_goods_received(world)
        self.write_waste_events(world)
        self.write_stock_counts(world)
        self.write_markdown_events(world)
        self.write_promotions(world)
        self.write_weather(world)
        self.write_trading_calendar(world)
        return list(self.written)

    def write_trading_calendar(self, world: WorldData) -> None:
        """The trading calendar as a source feed.

        The store system owns this: it is what decides when the doors open, and
        the roster and the till both read it. It has to land like any other feed,
        because the alternative is dbt reading the sealed truth in `data/truth`
        — which would make the warehouse's own answers unauditable, since the
        pipeline would be checking itself against a file only it can see.
        """
        cal = world.calendar.copy()
        frame = cal.rename(columns={"day": "trading_date"})[
            [
                "trading_date",
                "weekday",
                "month",
                "is_weekend",
                "holiday_name",
                "is_closed",
                "is_school_holiday",
                "school_break",
                "special_day",
                "footfall_index",
                "day_type",
            ]
        ]
        self._write(
            "trading_calendar",
            None,
            frame,
            file_name="trading_calendar_full",
            landing_date=self.cfg.clock.last_trading_day,
            header_comment="source=STORE-OPS, encoding=UTF-8, tz=Australia/Adelaide",
        )

    # -- reference data (published once, upstream of everything) ---------------
    def write_master_data(self, world: WorldData) -> None:
        drop = self.cfg.clock.last_trading_day
        for source, feed in (
            ("item_master", "item_master"),
            ("store_master", "store_master"),
            ("supplier_master", "supplier_master"),
        ):
            frame = world.feeds[feed]
            # D09: two seasonal lines are not in the master yet, and one retired
            # line is still on the till's own file. The warehouse has to cope
            # with SKUs it has never seen rather than dropping the sales.
            if source == "item_master":
                frame = frame.iloc[:-2]  # the last two lines were added mid-season
            self._write(
                source,
                None,
                frame,
                file_name=f"{source}_full_extract",
                landing_date=drop,
            )

    # -- POS (the big one) -----------------------------------------------------
    def write_pos_sales(self, world: WorldData) -> None:
        pos = world.feeds["pos_sales"].copy()
        pos["business_date"] = pd.to_datetime(pos["business_date"]).dt.date
        unknown_sku_rate: float = self.defects["D09"]["injection"]["unknown_sku_rows"]

        for (business_date, store), group in pos.groupby(
            ["business_date", "store_code"], sort=True
        ):
            rng = self._rng("pos", store, business_date)
            group = group.copy()
            group.insert(
                2, "extract_batch_id", BATCH_TEMPLATE.format(code=store, day=business_date)
            )

            # D10: voided and refunded lines arrive in the same extract.
            if rng.random() < float(self.defects["D10"]["injection"]["probability_per_store_day"]):
                group = self._inject_voids(group, rng)

            # D09: rows for SKUs the item master does not carry (seasonal lines).
            group = self._inject_unknown_sku(group, rng, unknown_sku_rate)

            # D03: a link outage — this store-day's extract is never sent.
            if (store, business_date) in self._outages:
                self.dropped_store_days.add(("pos_sales", store, business_date))
                continue

            # D05: one store's old firmware exports money in integer cents.
            money_unit = "dollars"
            if self._is_cents_store(store, business_date):
                money_unit = "cents"
                for col in ("gross_sales", "discount_amount"):
                    group[col] = (group[col] * 100).round(0).astype("int64")

            # D04: another store's locale writes units as text, and the firmware
            # uses a decimal comma. The column must stay `object` dtype — pandas
            # silently re-coerces a mixed list back to float64, which would write
            # clean numbers to disk and make the defect invisible.
            if store in set(self.defects["D04"]["injection"]["stores"]):
                group["units"] = pd.Series(
                    [
                        self._as_decimal_comma(rng, float(value))
                        for value in group["units"].to_numpy()
                    ],
                    index=group.index,
                    dtype=object,
                )

            lag = self._landing_lag(store, business_date)
            landing_date = self._landing_date_for(store, business_date)
            if landing_date > self.cfg.clock.as_of_date:
                # Lands after the snapshot: the file simply is not there, and the
                # warehouse reports the store-day as missing rather than as zero.
                self.dropped_store_days.add(("pos_sales", store, business_date))
                continue
            if lag:
                self.late_store_days.add(("pos_sales", store, business_date))

            comment = (
                "source=POS-NET, encoding=UTF-8, delimiter=, "
                f"money_unit={money_unit}, tz=Australia/Adelaide"
            )
            self._write(
                "pos_sales",
                business_date,
                group,
                file_name=f"pos_{store}_{business_date:%Y%m%d}",
                landing_date=landing_date,
                header_comment=comment,
            )

            # D01: a gateway timeout makes the integration retry, and the retry
            # lands under a different name with identical bytes.
            if rng.random() < float(self.defects["D01"]["injection"]["probability_per_file"]):
                self._write(
                    "pos_sales",
                    business_date,
                    group,
                    file_name=f"pos_{store}_{business_date:%Y%m%d}_retry",
                    landing_date=landing_date,
                    header_comment=comment,
                )

    def _inject_voids(self, group: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
        """D10: a void or a refund arrives as a negative line in the extract.

        A void is the *reversal of part of* a line already in the file — the
        till refunds the two loaves a customer brought back, not the whole day's
        bread. So each affected line is reduced by the amount that was voided
        and a matching negative line carries it back out. The reduction is
        bounded by the line's own units, because reversing more than a line
        holds would drive the net below zero — which no till can produce and
        which the warehouse would (and should) reject as corrupt data.
        """
        if group.empty:
            return group
        out = group.copy()
        victims = out.sample(n=min(3, len(out)), random_state=int(rng.integers(0, 2**31 - 1)))
        reversed_rows = []
        for index, row in victims.iterrows():
            units = float(row.get("units", 0) or 0)
            if units == 0:
                continue
            voided = units * float(rng.uniform(0.05, 0.33))
            out.at[index, "units"] = units - voided
            reversal = row.copy()
            reversal["units"] = -voided
            for col in ("gross_sales", "discount_amount"):
                value = float(row.get(col, 0) or 0)
                if value:
                    reversal[col] = -(value * (voided / units))
                    out.at[index, col] = value - float(reversal[col])
            reversal["transaction_count"] = 0
            reversal["promo_name"] = "VOID/REFUND"
            reversed_rows.append(reversal)
        if not reversed_rows:
            return out
        return pd.concat([out, pd.DataFrame(reversed_rows)], ignore_index=True)

    def _inject_unknown_sku(
        self, group: pd.DataFrame, rng: np.random.Generator, rate: float
    ) -> pd.DataFrame:
        """D09: the till knows a seasonal line the item master does not.

        `unknown_sku_rows` is a share of rows, so the per-store-day probability
        is scaled by the average lines a store sells in a day.
        """
        if rng.random() > rate * 25:
            return group
        sku = ["P9001", "P9002", "B9001", "D9001", "S9001"][int(rng.integers(0, 5))]
        template = group.iloc[[0]].copy()
        template["sku"] = sku
        template["units"] = round(float(rng.uniform(2, 40)), 3)
        template["gross_sales"] = round(
            float(template["units"].iloc[0]) * float(rng.uniform(3, 9)), 2
        )
        template["discount_amount"] = 0.0
        template["promo_name"] = ""
        template["extract_batch_id"] = group["extract_batch_id"].iloc[0]
        return pd.concat([group, template], ignore_index=True)

    def _as_decimal_comma(self, rng: np.random.Generator, value: float) -> object:
        """D04: the old firmware writes units as text, some with a decimal comma."""
        if rng.random() < 0.18:
            return f"{value:.3f}".replace(".", ",")
        return value

    # -- goods received --------------------------------------------------------
    def write_goods_received(self, world: WorldData) -> None:
        grn = world.feeds["goods_received"].copy()
        grn["delivery_date"] = pd.to_datetime(grn["delivery_date"]).dt.date
        for business_date, group in grn.groupby("delivery_date", sort=True):
            landing_date = business_date + timedelta(days=1)
            if landing_date > self.cfg.clock.as_of_date:
                continue
            self._write(
                "goods_received",
                business_date,
                group,
                file_name=f"goods_received_{business_date:%Y%m%d}",
                landing_date=landing_date,
                header_comment="source=WMS-GRN, encoding=UTF-8, money_unit=dollars",
            )

    # -- loss scans (the UTC one) ---------------------------------------------
    def write_waste_events(self, world: WorldData) -> None:
        """D06: the handhelds write UTC. One file per device-day, named by the
        *device's* local day, which is why the loader cannot trust the filename
        or the raw timestamp's date part."""
        scans = world.feeds["waste_events"].copy()
        if scans.empty:
            return
        scans["event_ts_utc"] = pd.to_datetime(scans["event_ts_utc"])
        adelaide = ZoneInfo(self.cfg.timezone)
        local_days = scans["event_ts_utc"].dt.tz_localize("UTC").dt.tz_convert(adelaide).dt.date
        scans["_landing_day"] = local_days
        variants = _reason_variants()

        for landing_day, group in scans.groupby("_landing_day", sort=True):
            out = group.drop(columns=["_landing_day"]).copy()
            salt = int(landing_day.toordinal())
            # D08: the reason code the handheld shows differs by firmware.
            out["reason_code"] = [
                _reason_variant(str(code), salt + i, variants)
                for i, code in enumerate(out["reason_code"])
            ]
            landing_date = landing_day + timedelta(days=1)
            if landing_date > self.cfg.clock.as_of_date:
                landing_date = self.cfg.clock.as_of_date
            self._write(
                "waste_events",
                landing_day,
                out,
                file_name=f"loss_scans_{landing_day:%Y%m%d}",
                landing_date=landing_date,
                header_comment="source=HANDHELD-SCAN, encoding=UTF-8, tz=UTC",
            )

    # -- counts ----------------------------------------------------------------
    def write_stock_counts(self, world: WorldData) -> None:
        counts = world.feeds["stock_counts"].copy()
        if counts.empty:
            return
        counts["count_date"] = pd.to_datetime(counts["count_date"]).dt.date
        for count_date, group in counts.groupby("count_date", sort=True):
            self._write(
                "stock_counts",
                count_date,
                group,
                file_name=f"stock_counts_{count_date:%Y%m%d}",
                landing_date=count_date,
                header_comment="source=CYCLE-COUNT, encoding=UTF-8",
            )

    # -- markdown tickets -----------------------------------------------------
    def write_markdown_events(self, world: WorldData) -> None:
        marks = world.feeds["markdown_events"].copy()
        if marks.empty:
            return
        marks["printed_ts_local"] = pd.to_datetime(marks["printed_ts_local"])
        marks["_day"] = marks["printed_ts_local"].dt.date
        for day, group in marks.groupby("_day", sort=True):
            landing_date = day + timedelta(days=1)
            if landing_date > self.cfg.clock.as_of_date:
                landing_date = self.cfg.clock.as_of_date
            self._write(
                "markdown_events",
                day,
                group.drop(columns=["_day"]),
                file_name=f"markdowns_{day:%Y%m%d}",
                landing_date=landing_date,
                header_comment="source=SCALE-TICKET, encoding=UTF-8, tz=Australia/Adelaide, money_unit=dollars",
            )

    # -- promotions ------------------------------------------------------------
    def write_promotions(self, world: WorldData) -> None:
        frame = world.feeds["promotions"]
        window = self.cfg.clock.trading_days()
        # Planning publishes promotions one month ahead; the extract therefore
        # covers future dates the trading data cannot yet show.
        horizon = min(window[-1] + timedelta(days=28), self.cfg.clock.as_of_date)
        frame = frame[
            (pd.to_datetime(frame["start_date"]).dt.date <= horizon)
            & (pd.to_datetime(frame["end_date"]).dt.date >= window[0])
        ]
        self._write(
            "promotions",
            None,
            frame,
            file_name="promo_plan_full",
            landing_date=self.cfg.clock.last_trading_day,
            header_comment="source=MERCH-PLAN, encoding=UTF-8",
        )

    # -- weather ---------------------------------------------------------------
    def write_weather(self, world: WorldData) -> None:
        wx = world.feeds["weather"].copy()
        wx["weather_date"] = pd.to_datetime(wx["weather_date"]).dt.date
        # The BOM drop includes the as-of day itself (a forecast), which is a
        # useful trap: the pipeline must not report trading performance for a day
        # that has not finished.
        frame = wx[wx["weather_date"] <= self.cfg.clock.as_of_date]
        self._write(
            "weather",
            None,
            frame,
            file_name="bom_daily_observations",
            landing_date=self.cfg.clock.as_of_date,
            header_comment="source=BOM, station=SADelaide(023000), encoding=UTF-8",
        )


def _reason_variants() -> dict[str, list[str]]:
    """D08: firmware-specific reason code variants, applied per row.

    Four generations of handheld map the same physical event to a different
    label, and one store's Android app writes free text. Anything not in this
    map still has to survive the loader.
    """
    return {
        "EXPIRED": ["EXPIRED", "EXP", "expired", "EXPIRY", "BEST BEFORE", "1"],
        "EXP": ["EXP", "EXPIRED", "exp", "1"],
        "EXPIRY": ["EXPIRY", "EXPIRED", "1"],
        "BEST BEFORE": ["BEST BEFORE", "PAST BEST BEFORE", "EXPIRED", "1"],
        "QUALITY": ["QUALITY", "QUAL", "Poor Quality", "MOULD", "3"],
        "POOR QUALITY": ["POOR QUALITY", "QUALITY", "MOULD"],
        "MOULD": ["MOULD", "MOULDY", "QUALITY"],
        "SOFT": ["SOFT", "QUALITY"],
        "SPOIL": ["SPOIL", "SPOILAGE", "QUALITY"],
        "TEMPERATURE": ["TEMPERATURE", "TEMP", "Cold Chain", "4"],
        "COLD CHAIN": ["COLD CHAIN", "TEMPERATURE", "4"],
        "TRIM": ["TRIM", "TRIMMINGS", "Prep", "5"],
        "TRIMMINGS": ["TRIMMINGS", "TRIM", "5"],
        "PREP": ["PREP", "TRIM", "Prep", "5"],
        "BUTCHER TRIM": ["BUTCHER TRIM", "TRIM", "5"],
        "DAMAGED": ["DAMAGED", "DMG", "damaged", "2"],
    }


def _reason_variant(code: str, salt: int, variants: dict[str, list[str]]) -> str:
    options = variants.get(code.upper(), [code, code.strip(), code.title()])
    # Deterministic per (code, salt): the same store-day always recovers the
    # same label, so a re-run of the pipeline compares like with like.
    return str(options[salt % len(options)])


def reason_code_variants() -> list[str]:
    """Every raw code the sources can emit — pinned by a test against the seed
    map, so a new firmware code cannot enter the landing zone unmapped."""
    seen: set[str] = set()
    for options in _reason_variants().values():
        seen.update(options)
    return sorted(seen)


def file_manifest(files: list[LandingFile]) -> pd.DataFrame:
    """One row per landed file: the ingest run manifest's left-hand side."""
    records = [f.as_record() for f in files]
    df = pd.DataFrame(records)
    if df.empty:
        df = pd.DataFrame(
            columns=[
                "source",
                "business_date",
                "landing_date",
                "file_name",
                "file_path",
                "rows",
                "checksum",
                "size_bytes",
                "is_duplicate_of",
            ]
        )
    return df.sort_values(["source", "landing_date", "file_name"]).reset_index(drop=True)


def write_landing(cfg: Config, world: WorldData) -> pd.DataFrame:
    """Write the whole landing zone and return the file manifest."""
    writer = LandingWriter(cfg)
    files = writer.write_all(world)
    manifest = file_manifest(files)
    manifest["dropped_store_day"] = False
    for source, store, day in writer.dropped_store_days:
        mask = (
            (manifest["source"] == source)
            & (manifest["business_date"] == day)
            & manifest["file_name"].str.contains(store, regex=False)
        )
        manifest.loc[mask, "dropped_store_day"] = True
    # Record the deliberate gaps so the loader's coverage test means something.
    gaps = pd.DataFrame(
        [
            {"source": source, "store_code": store, "business_date": day}
            for source, store, day in sorted(writer.dropped_store_days)
        ],
        columns=["source", "store_code", "business_date"],
    )
    gaps.to_csv(cfg.paths.truth_dir / "landing_gaps.csv", index=False)
    manifest.to_csv(cfg.paths.truth_dir / "landing_manifest.csv", index=False)
    return manifest
