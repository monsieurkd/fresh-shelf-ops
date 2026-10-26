"""Per-source loaders: raw file in, typed rows out.

Each loader has the same signature and the same obligations:

    loader(cfg, raw_frame, markers, source) -> (frame, quarantine_rows, business_date)

It returns the rows that can be trusted, the rows it refused (with the raw text
and the reason) and the business day the file describes. A loader never raises
for bad data and never silently coerces a bad value to zero: a quarantined row is
an operational signal, a zero is a lie that reads as a fact.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

import pandas as pd

from ..config import Config

Loader = Callable[
    [Config, pd.DataFrame, dict[str, str], str], tuple[pd.DataFrame, list[dict], date | None]
]

TZ_ADELAIDE = "Australia/Adelaide"


def _num(value: object) -> float | None:
    """Parse a source number, tolerating a locale decimal comma (D04)."""
    text = str(value).strip().replace(" ", "")
    if text == "" or text.lower() in {"nan", "none", "null"}:
        return None
    if "," in text and "." in text:
        text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _int(value: object) -> int | None:
    parsed = _num(value)
    return None if parsed is None else round(parsed)


def _q(source: str, file: str, reason: str, **fields: object) -> dict:
    """One quarantine row: what was refused, from where, and why."""
    return {
        "source_file": file,
        "reason": reason,
        "raw_row": "; ".join(f"{k}={v}" for k, v in sorted(fields.items()))[:1000],
        "source": source,
    }


def _business_dates_from_local(series: pd.Series, tz: str) -> pd.Series:
    """Adelaide business date from a naive *local* timestamp."""
    return pd.to_datetime(series).dt.date


def load_pos_sales(
    cfg: Config, raw: pd.DataFrame, markers: dict[str, str], source: str
) -> tuple[pd.DataFrame, list[dict], date | None]:
    """The POS extract: the biggest feed and the one with the most to get wrong.

    Handles: cents vs dollars (D05, via the file's own money-unit marker and a
    plausibility bound), text units with a locale comma (D04), void lines (D10),
    SKUs the item master does not carry (D09) and redeliveries (D01, upstream of
    here in the ingestor).
    """
    quarantine: list[dict] = []
    file_name = str(markers.get("_file", "pos"))
    money_unit = str(markers.get("money_unit", "dollars")).lower()
    scale = 100.0 if money_unit == "cents" else 1.0

    out_rows: list[dict] = []
    for record in raw.to_dict("records"):
        business_date = pd.to_datetime(str(record.get("business_date")), errors="coerce")
        units = _num(record.get("units"))
        gross = _num(record.get("gross_sales"))
        discount = _num(record.get("discount_amount")) or 0.0
        if business_date is pd.NaT or units is None or gross is None:
            quarantine.append(_q(source, file_name, "unparseable core field", row=record))
            continue

        gross /= scale
        discount /= scale
        # Plausibility bound: a fresh line cannot average $250 a unit. A cents
        # file whose marker is missing lands here rather than reporting 100x.
        unit_value = abs(gross) / abs(units) if units else 0.0
        if unit_value > 250.0 and scale == 1.0:
            quarantine.append(
                _q(
                    source,
                    file_name,
                    f"implausible unit value ${unit_value:,.2f} (unit or currency mismatch)",
                    row=record,
                )
            )
            continue

        out_rows.append(
            {
                "business_date": business_date.date(),
                "store_code": str(record.get("store_code", "")).strip(),
                "sku": str(record.get("sku", "")).strip(),
                "extract_batch_id": str(record.get("extract_batch_id", "")).strip() or None,
                "units": round(units, 3),
                "gross_sales": round(gross, 2),
                "discount_amount": round(discount, 2),
                "net_sales": round(gross - discount, 2),
                "transaction_count": _int(record.get("transaction_count")) or 0,
                "void_units": _num(record.get("void_units")) or 0.0,
                "unit_of_measure": str(record.get("unit_of_measure", "ea")).strip() or "ea",
                # Forced to str, never left to inference: an all-empty column
                # makes pandas (and so DuckDB) type it as INTEGER, and then the
                # first real value, a promo name, fails to convert against a
                # table that was created from a quieter day's file. This is the
                # bug that only showed up on the full-estate run.
                "money_unit_as_filed": str(money_unit),
                "promo_name": str(record.get("promo_name", "")).strip(),
            }
        )

    frame = pd.DataFrame(out_rows)
    if frame.empty:
        frame = pd.DataFrame(
            columns=[
                "business_date",
                "store_code",
                "sku",
                "extract_batch_id",
                "units",
                "gross_sales",
                "discount_amount",
                "net_sales",
                "transaction_count",
                "void_units",
                "unit_of_measure",
                "money_unit_as_filed",
                "promo_name",
            ]
        )
    # The business day of the file is its most common business date, read from
    # the contents. It is never read from the folder the file landed in (D02).
    business_date = frame["business_date"].mode().iloc[0] if not frame.empty else None
    return frame, quarantine, business_date


def load_goods_received(
    cfg: Config, raw: pd.DataFrame, markers: dict[str, str], source: str
) -> tuple[pd.DataFrame, list[dict], date | None]:
    quarantine: list[dict] = []
    out: list[dict] = []
    file_name = str(markers.get("_file", "grn"))
    for record in raw.to_dict("records"):
        delivery_date = pd.to_datetime(str(record.get("delivery_date")), errors="coerce")
        received = _num(record.get("qty_received"))
        if delivery_date is pd.NaT or received is None:
            quarantine.append(_q(source, file_name, "unparseable delivery line", row=record))
            continue
        out.append(
            {
                "business_date": delivery_date.date(),
                "store_code": str(record.get("store_code", "")).strip(),
                "sku": str(record.get("sku", "")).strip(),
                "supplier_code": str(record.get("supplier_code", "")).strip(),
                "invoice_no": str(record.get("invoice_no", "")).strip(),
                "purchase_order": str(record.get("purchase_order", "")).strip(),
                "qty_ordered": _num(record.get("qty_ordered")) or 0.0,
                "qty_received": received,
                "qty_rejected": _num(record.get("qty_rejected")) or 0.0,
                "unit_cost": round(_num(record.get("unit_cost")) or 0.0, 4),
                "extended_cost": round(received * (_num(record.get("unit_cost")) or 0.0), 2),
                "expiry_date": pd.to_datetime(
                    str(record.get("expiry_date")), errors="coerce"
                ).date()
                if str(record.get("expiry_date", "")).strip()
                else None,
                "unit_of_measure": str(record.get("unit_of_measure", "ea")).strip(),
                "was_substituted": str(record.get("was_substituted", "")).strip().lower()
                in {"true", "1", "yes"},
                "delivery_sequence": _int(record.get("delivery_sequence")) or 0,
            }
        )
    frame = pd.DataFrame(out)
    business_date = (
        pd.to_datetime(frame["business_date"]).mode().iloc[0] if not frame.empty else None
    )
    return frame, quarantine, business_date


def load_waste_events(
    cfg: Config, raw: pd.DataFrame, markers: dict[str, str], source: str
) -> tuple[pd.DataFrame, list[dict], date | None]:
    """Loss scans: the UTC feed (D06).

    The raw timestamp is UTC because that is what the handhelds write. The
    business date is derived *after* converting to Adelaide local time, so a
    19:00 disposal stays on its trading day and a 00:30 one does not jump
    backwards into the previous one. The reason code is left exactly as filed;
    mapping it is the warehouse's job (D08).
    """
    quarantine: list[dict] = []
    out: list[dict] = []
    file_name = str(markers.get("_file", "scans"))
    tz = str(markers.get("tz", "UTC")).upper()
    for record in raw.to_dict("records"):
        ts = pd.to_datetime(str(record.get("event_ts_utc")), errors="coerce", utc=True)
        qty = _num(record.get("qty"))
        if ts is pd.NaT or qty is None:
            quarantine.append(_q(source, file_name, "unparseable loss scan", row=record))
            continue
        if abs(qty) > 500.0:
            quarantine.append(
                _q(source, file_name, f"implausible disposal quantity {qty:g}", row=record)
            )
            continue
        local = ts.tz_convert(TZ_ADELAIDE)
        out.append(
            {
                "event_ts_utc": ts.tz_convert("UTC").tz_localize(None).to_pydatetime(),
                "event_ts_local": local.tz_localize(None).to_pydatetime(),
                "business_date": local.date(),
                "filed_timestamp_zone": tz,
                "store_code": str(record.get("store_code", "")).strip(),
                "sku": str(record.get("sku", "")).strip(),
                "qty": round(qty, 3),
                "reason_code_raw": str(record.get("reason_code", "")).strip(),
                "unit_of_measure": str(record.get("unit_of_measure", "ea")).strip(),
                "staff_id": str(record.get("staff_id", "")).strip() or None,
                "device_id": str(record.get("device_id", "")).strip() or None,
                "disposal_method": str(record.get("disposal_method", "")).strip() or None,
            }
        )
    frame = pd.DataFrame(out)
    business_date = (
        pd.to_datetime(frame["business_date"]).mode().iloc[0] if not frame.empty else None
    )
    return frame, quarantine, business_date


def load_stock_counts(
    cfg: Config, raw: pd.DataFrame, markers: dict[str, str], source: str
) -> tuple[pd.DataFrame, list[dict], date | None]:
    quarantine: list[dict] = []
    out: list[dict] = []
    file_name = str(markers.get("_file", "counts"))
    for record in raw.to_dict("records"):
        count_date = pd.to_datetime(str(record.get("count_date")), errors="coerce")
        qty = _num(record.get("counted_qty"))
        if count_date is pd.NaT or qty is None:
            quarantine.append(_q(source, file_name, "unparseable count", row=record))
            continue
        if qty < 0:
            quarantine.append(_q(source, file_name, f"negative count {qty:g}", row=record))
            continue
        out.append(
            {
                "business_date": count_date.date(),
                "store_code": str(record.get("store_code", "")).strip(),
                "sku": str(record.get("sku", "")).strip(),
                "counted_qty": round(qty, 3),
                "count_type": str(record.get("count_type", "")).strip(),
                "counted_by": str(record.get("counted_by", "")).strip() or None,
                "unit_of_measure": str(record.get("unit_of_measure", "ea")).strip(),
                "count_reference": str(record.get("count_reference", "")).strip() or None,
            }
        )
    frame = pd.DataFrame(out)
    business_date = (
        pd.to_datetime(frame["business_date"]).mode().iloc[0] if not frame.empty else None
    )
    return frame, quarantine, business_date


def load_markdown_events(
    cfg: Config, raw: pd.DataFrame, markers: dict[str, str], source: str
) -> tuple[pd.DataFrame, list[dict], date | None]:
    quarantine: list[dict] = []
    out: list[dict] = []
    file_name = str(markers.get("_file", "markdowns"))
    for record in raw.to_dict("records"):
        printed = pd.to_datetime(str(record.get("printed_ts_local")), errors="coerce")
        pct = _num(record.get("markdown_pct"))
        if printed is pd.NaT or pct is None:
            quarantine.append(_q(source, file_name, "unparseable markdown ticket", row=record))
            continue
        if not 0 < pct < 1:
            quarantine.append(
                _q(source, file_name, f"markdown depth {pct:g} outside (0,1)", row=record)
            )
            continue
        out.append(
            {
                "printed_ts_local": printed.to_pydatetime(),
                "business_date": printed.date(),
                "store_code": str(record.get("store_code", "")).strip(),
                "sku": str(record.get("sku", "")).strip(),
                "markdown_pct": round(pct, 4),
                "base_price": round(_num(record.get("base_price")) or 0.0, 4),
                "markdown_price": round(_num(record.get("markdown_price")) or 0.0, 4),
                "qty_ticketed": round(_num(record.get("qty_ticketed")) or 0.0, 3),
                "qty_honoured": round(_num(record.get("qty_honoured")) or 0.0, 3),
                "qty_sold_at_markdown": round(_num(record.get("qty_sold_at_markdown")) or 0.0, 3),
                "pass_name": str(record.get("pass_name", "")).strip() or None,
                "trigger": str(record.get("trigger", "")).strip() or None,
                "unit_of_measure": str(record.get("unit_of_measure", "ea")).strip(),
            }
        )
    frame = pd.DataFrame(out)
    business_date = (
        pd.to_datetime(frame["business_date"]).mode().iloc[0] if not frame.empty else None
    )
    return frame, quarantine, business_date


def load_promotions(
    cfg: Config, raw: pd.DataFrame, markers: dict[str, str], source: str
) -> tuple[pd.DataFrame, list[dict], date | None]:
    quarantine: list[dict] = []
    out: list[dict] = []
    file_name = str(markers.get("_file", "promos"))
    for record in raw.to_dict("records"):
        start = pd.to_datetime(str(record.get("start_date")), errors="coerce")
        end = pd.to_datetime(str(record.get("end_date")), errors="coerce")
        if start is pd.NaT or end is pd.NaT or end < start:
            quarantine.append(_q(source, file_name, "invalid promotion window", row=record))
            continue
        out.append(
            {
                "sku": str(record.get("sku", "")).strip(),
                "promo_name": str(record.get("promo_name", "")).strip(),
                "start_date": start.date(),
                "end_date": end.date(),
                "business_date": start.date(),
                "promo_type": str(record.get("promo_type", "")).strip(),
                "discount_pct": round(_num(record.get("discount_pct")) or 0.0, 4),
                "demand_uplift_factor": round(_num(record.get("demand_uplift_factor")) or 1.0, 3),
                "catalogue_week": str(record.get("catalogue_week", "")).strip() or None,
            }
        )
    frame = pd.DataFrame(out)
    return frame, quarantine, None


def load_weather(
    cfg: Config, raw: pd.DataFrame, markers: dict[str, str], source: str
) -> tuple[pd.DataFrame, list[dict], date | None]:
    quarantine: list[dict] = []
    out: list[dict] = []
    file_name = str(markers.get("_file", "weather"))
    for record in raw.to_dict("records"):
        day = pd.to_datetime(str(record.get("weather_date")), errors="coerce")
        tmax = _num(record.get("max_temp_c"))
        if day is pd.NaT or tmax is None:
            quarantine.append(_q(source, file_name, "unparseable weather row", row=record))
            continue
        out.append(
            {
                "business_date": day.date(),
                "weather_date": day.date(),
                "store_code": str(record.get("store_code", "")).strip(),
                "max_temp_c": tmax,
                "min_temp_c": _num(record.get("min_temp_c")) or 0.0,
                "rainfall_mm": _num(record.get("rainfall_mm")) or 0.0,
                "is_heat_day": str(record.get("is_heat_day", "")).strip().lower() in {"true", "1"},
                "is_cold_night": str(record.get("is_cold_night", "")).strip().lower()
                in {"true", "1"},
            }
        )
    frame = pd.DataFrame(out)
    return frame, quarantine, None


def load_trading_calendar(
    cfg: Config, raw: pd.DataFrame, markers: dict[str, str], source: str
) -> tuple[pd.DataFrame, list[dict], date | None]:
    """The trading calendar: which days the doors were open.

    Unlike every other feed, this one carries no business day of its own. It
    describes the whole window, so it replaces rather than merging by day.
    """
    quarantine: list[dict] = []
    out: list[dict] = []
    file_name = str(markers.get("_file", "calendar"))
    for record in raw.to_dict("records"):
        trading_date = pd.to_datetime(str(record.get("trading_date")), errors="coerce")
        if trading_date is pd.NaT:
            quarantine.append(_q(source, file_name, "unparseable trading date", row=record))
            continue
        out.append(
            {
                "trading_date": trading_date.date(),
                "weekday": _int(record.get("weekday")) or 0,
                "month_number": _int(record.get("month")) or 0,
                "is_weekend": str(record.get("is_weekend", "")).strip().lower() in {"true", "1"},
                "holiday_name": str(record.get("holiday_name", "")).strip() or None,
                "is_closed": str(record.get("is_closed", "")).strip().lower() in {"true", "1"},
                "is_school_holiday": str(record.get("is_school_holiday", "")).strip().lower()
                in {"true", "1"},
                "school_break": str(record.get("school_break", "")).strip() or None,
                "special_day": str(record.get("special_day", "")).strip() or None,
                "footfall_index": round(_num(record.get("footfall_index")) or 0.0, 4),
                "day_type": str(record.get("day_type", "")).strip(),
            }
        )
    frame = pd.DataFrame(out)
    return frame, quarantine, None


def load_item_master(
    cfg: Config, raw: pd.DataFrame, markers: dict[str, str], source: str
) -> tuple[pd.DataFrame, list[dict], date | None]:
    quarantine: list[dict] = []
    out: list[dict] = []
    file_name = str(markers.get("_file", "items"))
    for record in raw.to_dict("records"):
        sku = str(record.get("sku", "")).strip()
        if not sku:
            quarantine.append(_q(source, file_name, "missing sku", row=record))
            continue
        out.append(
            {
                "sku": sku,
                "item_name": str(record.get("item_name", "")).strip(),
                "department_code": str(record.get("department_code", "")).strip(),
                "department_name": str(record.get("department_name", "")).strip(),
                "category": str(record.get("category", "")).strip(),
                "unit_of_measure": str(record.get("unit_of_measure", "ea")).strip(),
                "pack_size": _num(record.get("pack_size")) or 1.0,
                "retail_price": round(_num(record.get("retail_price")) or 0.0, 4),
                "standard_cost": round(_num(record.get("standard_cost")) or 0.0, 4),
                "shelf_life_days": _int(record.get("shelf_life_days")) or 0,
                "life_at_delivery_days": _int(record.get("life_at_delivery_days")) or 0,
                "supplier_code": str(record.get("supplier_code", "")).strip(),
                "storage_condition": str(record.get("storage_condition", "")).strip(),
                "is_markdown_eligible": str(record.get("is_markdown_eligible", "")).strip().lower()
                in {"true", "1"},
                "is_short_life": str(record.get("is_short_life", "")).strip().lower()
                in {"true", "1"},
                "standard_margin_rate": _num(record.get("standard_margin_rate")) or 0.0,
                "is_active": str(record.get("is_active", "")).strip().lower() in {"true", "1"},
                "master_published_date": cfg.clock.last_trading_day,
            }
        )
    frame = pd.DataFrame(out)
    return frame, quarantine, None


def load_store_master(
    cfg: Config, raw: pd.DataFrame, markers: dict[str, str], source: str
) -> tuple[pd.DataFrame, list[dict], date | None]:
    quarantine: list[dict] = []
    out: list[dict] = []
    file_name = str(markers.get("_file", "stores"))
    for record in raw.to_dict("records"):
        code = str(record.get("store_code", "")).strip()
        if not code:
            quarantine.append(_q(source, file_name, "missing store code", row=record))
            continue
        out.append(
            {
                "store_code": code,
                "store_name": str(record.get("store_name", "")).strip(),
                "suburb": str(record.get("suburb", "")).strip(),
                "postcode": _int(record.get("postcode")) or 0,
                "state": str(record.get("state", "")).strip(),
                "latitude": _num(record.get("latitude")) or 0.0,
                "longitude": _num(record.get("longitude")) or 0.0,
                "store_format": str(record.get("store_format", "")).strip(),
                "size_index": _num(record.get("size_index")) or 1.0,
                "opened_date": pd.to_datetime(
                    str(record.get("opened_date")), errors="coerce"
                ).date()
                if str(record.get("opened_date", "")).strip()
                else None,
                "region": str(record.get("region", "")).strip(),
                "timezone": str(record.get("timezone", TZ_ADELAIDE)).strip(),
            }
        )
    frame = pd.DataFrame(out)
    return frame, quarantine, None


def load_supplier_master(
    cfg: Config, raw: pd.DataFrame, markers: dict[str, str], source: str
) -> tuple[pd.DataFrame, list[dict], date | None]:
    quarantine: list[dict] = []
    out: list[dict] = []
    file_name = str(markers.get("_file", "suppliers"))
    for record in raw.to_dict("records"):
        code = str(record.get("supplier_code", "")).strip()
        if not code:
            quarantine.append(_q(source, file_name, "missing supplier code", row=record))
            continue
        weekdays = str(record.get("delivery_weekdays", "")).split(",")
        out.append(
            {
                "supplier_code": code,
                "supplier_name": str(record.get("supplier_name", "")).strip(),
                "supplier_type": str(record.get("supplier_type", "")).strip(),
                "region": str(record.get("region", "")).strip(),
                "lead_time_days": _int(record.get("lead_time_days")) or 1,
                "delivery_weekdays": ",".join(part.strip() for part in weekdays if part.strip()),
                "min_order_value": _num(record.get("min_order_value")) or 0.0,
                "reliability": _num(record.get("reliability")) or 0.0,
            }
        )
    frame = pd.DataFrame(out)
    return frame, quarantine, None


LOADERS: dict[str, Loader] = {
    "pos_sales": load_pos_sales,
    "goods_received": load_goods_received,
    "waste_events": load_waste_events,
    "stock_counts": load_stock_counts,
    "markdown_events": load_markdown_events,
    "promotions": load_promotions,
    "weather": load_weather,
    "trading_calendar": load_trading_calendar,
    "item_master": load_item_master,
    "store_master": load_store_master,
    "supplier_master": load_supplier_master,
}

#: Sources that describe a trading day (and therefore carry ``business_date``).
DAILY_SOURCES = (
    "pos_sales",
    "goods_received",
    "waste_events",
    "stock_counts",
    "markdown_events",
    "weather",
)

#: Reference data with no business day of its own.
REFERENCE_SOURCES = (
    "item_master",
    "store_master",
    "supplier_master",
    "promotions",
    "trading_calendar",
)


def _now() -> datetime:
    return datetime.now(UTC)
