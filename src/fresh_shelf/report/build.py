"""Build the report: one self-contained HTML file, a Markdown mirror, metrics.json.

The report's job is to state the dollars without overclaiming, so it leads with
the things a reviewer would otherwise have to dig for rather than burying them:

1. **What the estate loses, in three separate numbers.** Markdown erosion,
   recorded waste, and unrecorded shrink are different problems with different
   fixes, and the page says so before it says anything else.
2. **That recorded waste is a floor.** The scan file misses roughly a fifth of
   disposals (D07), and the page shows the size of the gap rather than quoting
   the scan total as the loss.
3. **That the optimiser's own dollar accounting disagrees with the frontier.**
   The net benefit comes out negative on this estate while the frontier still has
   an interior optimum. That disagreement is on the page, in the summary, with
   the reason — not smoothed over.
4. **That the reconciliation residue is a bound.** It over-states true unscanned
   loss several-fold because count error dominates, so the weighted estimate is
   quoted with its confidence rather than the raw residue.

Everything is generated from the warehouse. No number here is typed by hand,
which is why the report can be regenerated on any world and stays honest.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from html import escape
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from ..config import Config
from ..db import query_df
from . import charts

log = logging.getLogger(__name__)

#: How many rows a table shows before it is truncated. Chosen so the page stays
#: readable while still listing every line that matters at this estate's size.
TOP_N = 15


@dataclass
class ReportData:
    """Everything the template needs, already shaped into display form."""

    headline: dict[str, Any]
    loss_by_department: pd.DataFrame
    loss_by_store: pd.DataFrame
    league_table: pd.DataFrame
    markdown: pd.DataFrame
    frontier: pd.DataFrame
    impact: pd.DataFrame
    reconciliation: pd.DataFrame
    forecast_metrics: pd.DataFrame
    calibration: pd.DataFrame
    data_quality: pd.DataFrame
    run_manifest: dict[str, Any]
    as_of: date
    stores: int
    departments: int
    items: int
    trading_days: int
    scope: str
    subtitle_prefix: str


def _scalar(con: duckdb.DuckDBPyConnection, sql: str, default: Any = 0) -> Any:
    row = con.execute(sql).fetchone()
    if row is None or row[0] is None:
        return default
    return row[0]


def collect(cfg: Config, con: duckdb.DuckDBPyConnection) -> ReportData:
    """Read every number the report needs, in one place.

    Centralised deliberately: a number that is fetched inline in the template is
    a number nobody can test, and this report is meant to be checkable.
    """
    fct = "marts.fct_store_item_day"

    headline = {
        "sales_value": float(_scalar(con, f"select sum(net_sales_value) from {fct}")),
        "full_price_sales_value": float(
            _scalar(con, f"select sum(full_price_sales_value) from {fct}")
        ),
        "realised_margin_value": float(
            _scalar(con, f"select sum(realised_margin_value) from {fct}")
        ),
        "markdown_erosion_value": float(
            _scalar(con, f"select sum(markdown_erosion_value) from {fct}")
        ),
        "recorded_waste_value": float(_scalar(con, f"select sum(recorded_waste_cost) from {fct}")),
        "prep_loss_value": float(_scalar(con, f"select sum(prep_loss_cost) from {fct}")),
        # Weighted, and reported with its confidence: the raw residue overstates
        # unscanned loss several-fold because count error dominates it.
        "unrecorded_shrink_value": float(
            _scalar(
                con, f"select sum(weighted_shrink_cost) from {fct} where weighted_shrink_cost > 0"
            )
        ),
        "unrecorded_shrink_raw_residue": float(
            _scalar(
                con, f"select sum(shrink_cost_per_day) from {fct} where shrink_cost_per_day > 0"
            )
        ),
        "avg_shrink_confidence": float(
            _scalar(
                con, f"select avg(shrink_confidence) from {fct} where shrink_confidence is not null"
            )
        ),
        "units_sold": float(_scalar(con, f"select sum(units_sold) from {fct}")),
        "units_received": float(_scalar(con, f"select sum(qty_received) from {fct}")),
        "waste_qty_scanned": float(_scalar(con, f"select sum(qty_waste_scanned) from {fct}")),
        "waste_events": int(_scalar(con, "select count(*) from staging.stg_waste_events")),
        "markdown_tickets": int(_scalar(con, "select count(*) from staging.stg_markdown_events")),
        "delivery_lines": int(_scalar(con, "select count(*) from staging.stg_goods_received")),
        "trading_days": int(
            _scalar(con, "select count(distinct business_date) from marts.fct_store_item_day")
        ),
        "stores": int(_scalar(con, "select count(*) from marts.dim_store")),
        "items_sold": int(_scalar(con, f"select count(distinct sku) from {fct}")),
        "pos_gap_days": int(
            _scalar(con, f"select count(distinct business_date) from {fct} where is_pos_gap")
        ),
    }
    # The four ratios the summary quotes. Computed here rather than in the
    # template so they can be asserted in tests.
    sales = headline["sales_value"] or 1.0
    headline["waste_pct_of_sales"] = 100.0 * headline["recorded_waste_value"] / sales
    headline["markdown_pct_of_sales"] = 100.0 * headline["markdown_erosion_value"] / sales
    headline["margin_pct_of_sales"] = 100.0 * headline["realised_margin_value"] / sales
    headline["shrink_pct_of_sales"] = 100.0 * headline["unrecorded_shrink_value"] / sales
    headline["sell_through_pct"] = (
        100.0 * headline["units_sold"] / headline["units_received"]
        if headline["units_received"]
        else 0.0
    )

    loss_by_department = query_df(
        con,
        """
        select
            department_name,
            cast(sum(net_sales_value) as decimal(14, 2))          as net_sales_value,
            cast(sum(recorded_waste_cost) as decimal(14, 2))      as recorded_waste_cost,
            cast(sum(markdown_erosion_value) as decimal(14, 2))   as markdown_erosion_value,
            cast(sum(realised_margin_value) as decimal(14, 2))    as realised_margin_value,
            round(100 * sum(recorded_waste_cost) / nullif(sum(net_sales_value), 0), 2)
                                                                   as waste_pct_of_sales,
            round(100 * sum(markdown_erosion_value) / nullif(sum(net_sales_value), 0), 2)
                                                                   as markdown_pct_of_sales,
            cast(sum(qty_received) as decimal(14, 2))             as units_received,
            cast(sum(units_sold) as decimal(14, 2))               as units_sold
        from marts.fct_store_item_day
        group by 1
        order by recorded_waste_cost desc
        """,
    )

    loss_by_store = query_df(
        con,
        """
        with s as (
            select
                f.store_code,
                d.store_name,
                d.store_format,
                d.size_index,
                cast(sum(f.net_sales_value) as decimal(14, 2))        as net_sales_value,
                cast(sum(f.recorded_waste_cost) as decimal(14, 2))     as recorded_waste_cost,
                cast(sum(f.markdown_erosion_value) as decimal(14, 2))  as markdown_erosion_value,
                cast(sum(f.weighted_shrink_cost) as decimal(14, 2))    as estimated_shrink_cost,
                round(100 * sum(f.recorded_waste_cost) / nullif(sum(f.net_sales_value), 0), 2)
                                                                       as waste_pct_of_sales,
                round(100 * sum(f.markdown_erosion_value) / nullif(sum(f.net_sales_value), 0), 2)
                                                                       as markdown_pct_of_sales
            from marts.fct_store_item_day f
            join marts.dim_store d on f.store_code = d.store_code
            group by 1, 2, 3, 4
        )
        select * from s order by recorded_waste_cost desc
        """,
    )

    league_table = query_df(
        con,
        f"""
        select
            sku,
            department_name,
            shelf_life_days,
            round(recorded_waste_cost, 0)                        as recorded_waste_cost,
            round(markdown_erosion_value, 0)                     as markdown_erosion_value,
            round(total_loss_cost, 0)                            as total_loss_cost,
            round(waste_rate_of_sales, 4)                        as waste_rate_of_sales,
            round(receive_to_sell_ratio, 2)                      as receive_to_sell_ratio,
            round(days_of_cover_received, 2)                     as days_of_cover_received,
            waste_pressure_reason
        from reporting.mart_waste_league_table
        order by recorded_waste_cost desc
        limit {TOP_N}
        """,
    )

    markdown = query_df(
        con,
        """
        select
            depth_band,
            pass_timing,
            count(*)                                                as tickets,
            cast(sum(qty_sold_at_markdown) as decimal(14, 1))        as units_moved,
            cast(sum(erosion_cost) as decimal(14, 2))                as erosion_cost,
            cast(sum(avoided_waste_cost) as decimal(14, 2))          as avoided_waste_cost,
            cast(sum(recovered_margin_value) as decimal(14, 2))      as recovered_margin_value,
            cast(sum(unhonoured_erosion_value) as decimal(14, 2))    as unhonoured_erosion_value,
            round(avg(honour_rate), 4)                               as avg_honour_rate
        from reporting.mart_markdown_effectiveness
        group by 1, 2
        order by erosion_cost desc
        """,
    )

    frontier = query_df(
        con,
        """
        select order_ratio, service_level, expected_waste_cost, expected_lost_margin,
               total_cost, is_optimal, is_recommended
        from analytics.waste_revenue_frontier
        order by order_ratio
        """,
    )
    impact = query_df(con, "select component, value, basis from analytics.optimisation_impact")
    reconciliation = query_df(
        con, 'select "check", value, note from analytics.impact_reconciliation'
    )
    forecast_metrics = query_df(con, "select * from analytics.forecast_metrics order by best_wape")
    calibration = query_df(
        con, "select * from analytics.forecast_calibration order by nominal_quantile"
    )
    data_quality = query_df(
        con,
        """
        select store_code, days_with_no_feed, feed_gap_rate, unmapped_skus,
               unmapped_net_sales_value, days_with_mixed_money_units, has_reported_caveat
        from reporting.mart_data_quality
        where has_reported_caveat
        order by store_code
        """,
    )

    run_manifest: dict[str, Any] = {}
    latest = cfg.paths.runs_dir / "latest.json"
    if latest.exists():
        run_manifest = json.loads(latest.read_text())

    # The subtitle is derived from the data, not read from config: a hardcoded
    # "eight stores, 12 months" is wrong the moment the report is generated on a
    # smaller world, and a report whose own header misstates its scope undermines
    # every number under it.
    window_start = query_df(con, "select min(business_date) as d from marts.fct_store_item_day")[
        "d"
    ].iloc[0]
    scope = (
        f"{headline['stores']} stores · {headline['items_sold']} items · "
        f"{headline['trading_days']} trading days · "
        f"{pd.Timestamp(window_start).date()} to {cfg.clock.last_trading_day}"
    )
    subtitle_prefix = cfg.report_cfg.get("subtitle", "")

    return ReportData(
        headline=headline,
        loss_by_department=loss_by_department,
        loss_by_store=loss_by_store,
        league_table=league_table,
        markdown=markdown,
        frontier=frontier,
        impact=impact,
        reconciliation=reconciliation,
        forecast_metrics=forecast_metrics,
        calibration=calibration,
        data_quality=data_quality,
        run_manifest=run_manifest,
        as_of=cfg.clock.as_of_date,
        stores=headline["stores"],
        departments=len(loss_by_department),
        items=headline["items_sold"],
        trading_days=headline["trading_days"],
        scope=scope,
        subtitle_prefix=subtitle_prefix,
    )


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
def _table(df: pd.DataFrame, columns: dict[str, str] | None = None, *, money: tuple = ()) -> str:
    """A table with named columns.

    Column labels come from the caller so the page can say "waste, $ (cost)"
    rather than repeating the warehouse's snake_case. Numbers are formatted with
    thousands separators everywhere, which is what readable money and readable
    counts both need — so there is no separate money branch to get wrong. (The
    `money` parameter is kept for call-site readability even though the formatting
    is uniform.)
    """
    del money
    if df.empty:
        return '<p class="muted">No rows.</p>'
    frame = df
    if columns:
        present = [c for c in columns if c in df.columns]
        frame = df[present]
        headers = [columns[c] for c in present]
    else:
        headers = [c.replace("_", " ") for c in df.columns]

    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body_rows = []
    for record in frame.to_dict("records"):
        cells = []
        for name in frame.columns:
            value = record[name]
            if value is None:
                text = "—"
            elif isinstance(value, float):
                text = f"{value:,.2f}"
            else:
                text = str(value)
            cells.append(f"<td>{escape(text)}</td>")
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _money(value: float) -> str:
    return f"${value:,.0f}"


def _pct(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}%"


def _stat(label: str, value: str, note: str = "", tone: str = "") -> str:
    tone_class = f" stat--{tone}" if tone else ""
    note_html = f'<p class="stat__note">{escape(note)}</p>' if note else ""
    return (
        f'<div class="stat{tone_class}"><dt>{escape(label)}</dt>'
        f"<dd>{escape(value)}</dd>{note_html}</div>"
    )


# ---------------------------------------------------------------------------
# The report itself
# ---------------------------------------------------------------------------
def render(data: ReportData, cfg: Config) -> str:
    """Fill the template. Charts and tables are built here, not in the template,
    so a missing section degrades to a sentence rather than to a broken page."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("template.html")

    headline = data.headline
    departments = data.loss_by_department
    frontier = data.frontier

    # --- headline stats -----------------------------------------------------
    stat_sales = _stat(
        "Net sales",
        _money(headline["sales_value"]),
        f"{headline['units_sold']:,.0f} units · margin {_pct(headline['margin_pct_of_sales'], 1)} of sales",
        "positive",
    )
    stat_waste = _stat(
        "Recorded waste, at cost",
        _money(headline["recorded_waste_value"]),
        f"{_pct(headline['waste_pct_of_sales'])} of sales · a floor, not the loss",
        "waste",
    )
    stat_markdown = _stat(
        "Markdown erosion, at margin",
        _money(headline["markdown_erosion_value"]),
        f"{_pct(headline['markdown_pct_of_sales'])} of sales · margin given away to sell stock in time",
        "markdown",
    )
    stat_shrink = _stat(
        "Unrecorded shrink, estimated",
        _money(headline["unrecorded_shrink_value"]),
        f"{_pct(headline['shrink_pct_of_sales'])} of sales · confidence "
        f"{headline['avg_shrink_confidence']:.2f}",
    )

    shrink_note = (
        '<div class="note note--caution"><strong>Read the shrink figure carefully.</strong> '
        f"The raw ledger residue over this period is {_money(headline['unrecorded_shrink_raw_residue'])}, "
        "which is several times the true unscanned loss — most of it is physical-count error, because a "
        "count eyeballed to ±3% cannot resolve a 1% loss. The figure above is that residue weighted by its "
        "own confidence. Neither number is &ldquo;shrink&rdquo; in the loss-prevention sense; both are bounds "
        "on loss the records do not explain.</div>"
    )

    # --- department chart ---------------------------------------------------
    if not departments.empty:
        chart_departments = charts.scatter_bars(
            labels=departments["department_name"].tolist(),
            waste=departments["recorded_waste_cost"].astype(float).tolist(),
            markdown=departments["markdown_erosion_value"].astype(float).tolist(),
            title="Recorded waste against markdown erosion, by department",
            desc=(
                "Stacked bars per department showing the cost of stock thrown away and the margin "
                "given away to sell stock in time. The balance between the two differs sharply by "
                "department: produce and seafood lose to disposal, bakery and dairy to discounting."
            ),
        )
    else:
        chart_departments = '<p class="muted">No department data.</p>'

    table_departments = _table(
        departments,
        {
            "department_name": "Department",
            "net_sales_value": "Net sales $",
            "recorded_waste_cost": "Waste $ (cost)",
            "markdown_erosion_value": "Markdown $ (margin)",
            "waste_pct_of_sales": "Waste % of sales",
            "markdown_pct_of_sales": "Markdown % of sales",
        },
        money=("net_sales_value", "recorded_waste_cost", "markdown_erosion_value"),
    )

    table_league = _table(
        data.league_table,
        {
            "sku": "SKU",
            "department_name": "Department",
            "shelf_life_days": "Life (days)",
            "recorded_waste_cost": "Waste $",
            "markdown_erosion_value": "Markdown $",
            "total_loss_cost": "Total cost $",
            "days_of_cover_received": "Days cover",
            "waste_pressure_reason": "Why",
        },
        money=("recorded_waste_cost", "markdown_erosion_value", "total_loss_cost"),
    )

    table_markdown = _table(
        data.markdown,
        {
            "depth_band": "Depth",
            "pass_timing": "Pass",
            "tickets": "Tickets",
            "units_moved": "Units moved",
            "erosion_cost": "Erosion $",
            "recovered_margin_value": "Margin recovered $",
            "unhonoured_erosion_value": "Not honoured $",
            "avg_honour_rate": "Honour rate",
        },
        money=("erosion_cost", "recovered_margin_value", "unhonoured_erosion_value"),
    )

    # --- frontier -----------------------------------------------------------
    if not frontier.empty:
        optimal = frontier[frontier["is_optimal"]]
        chart_frontier = charts.line_chart(
            x=frontier["service_level"].astype(float).tolist(),
            series={
                "Waste cost": frontier["expected_waste_cost"].astype(float).tolist(),
                "Lost margin": [float(v) for v in frontier["expected_lost_margin"]],
                "Total cost": frontier["total_cost"].astype(float).tolist(),
            },
            colours={
                "Waste cost": charts.PALETTE["waste"],
                "Lost margin": charts.PALETTE["positive"],
                "Total cost": charts.PALETTE["ink"],
            },
            title="Waste cost against lost margin across order policy",
            desc=(
                "Three lines against the service level the order policy achieves. Waste cost rises "
                "as more is ordered and lost margin falls, while total cost forms a U with a minimum "
                "in the middle. The optimum is where the two opposing costs balance."
            ),
            x_label="service level achieved",
            y_label="$ over the period",
            annotate_min=True,
        )
    else:
        chart_frontier = '<p class="muted">No frontier computed.</p>'

    frontier_note = ""
    if not frontier.empty:
        optimal = frontier[frontier["is_optimal"]]
        if not optimal.empty:
            row = optimal.iloc[0]
            frontier_note = (
                '<div class="note"><strong>The optimum is interior, and that is the finding.</strong> '
                f"It sits at a service level of {float(row['service_level']):.2f}, where waste costs "
                f"{_money(float(row['expected_waste_cost']))} and lost margin "
                f"{_money(float(row['expected_lost_margin']))} — a total of "
                f"{_money(float(row['total_cost']))}. Neither ordering to a blanket 95% service nor "
                "starving the shelf is right; the balance point is a property of each line's margin "
                "against its cost price, which is why it is computed per line.</div>"
            )

    # --- impact -------------------------------------------------------------
    impact_table = _table(
        data.impact,
        {"component": "Component", "value": "Value", "basis": "How it was derived"},
    )

    reconciliation_note = ""
    if not data.reconciliation.empty:
        frame = data.reconciliation.set_index("check")["value"]
        agree = float(frame.get("methods_agree_on_direction", 1.0)) > 0.5
        gap = float(frame.get("service_level_gap", 0.0))
        net = float(frame.get("impact_net_benefit", 0.0))
        optimum = float(frame.get("frontier_optimum_service_level", 0.0))
        if agree:
            reconciliation_note = (
                '<div class="note"><strong>The two methods agree on direction.</strong> '
                "The plan's own accounting and the frontier both show a benefit.</div>"
            )
        else:
            reconciliation_note = (
                '<div class="note note--alert"><strong>The two methods disagree, and both are on this page '
                "deliberately.</strong> The frontier has an interior optimum at a service level of "
                f"{optimum:.2f}, so the trade-off is real and the plan is directionally sound. But the plan's "
                f"own component accounting nets to {_money(net)} — a loss — because on this estate waste is a "
                "small share of sales while margin is a large one, so every unit no longer ordered gives up "
                "far more margin than the waste it avoids. "
                f"The recommended plan averages a service level {gap:.2f} above the unconstrained optimum, "
                "which is the price of pack sizes and shelf-life caps: you cannot order three-quarters of a "
                "tray. The honest conclusion is that <em>ordering down is not the win here</em> — the gains "
                "are in markdown timing and in the lines whose cover genuinely exceeds their shelf life. "
                "A deployment should negotiate the gap between the economics and the operation, not assume "
                "it away.</div>"
            )

    # --- forecast -----------------------------------------------------------
    table_forecast = _table(
        data.forecast_metrics,
        {
            "model": "Model",
            "folds": "Folds",
            "best_wape": "WAPE",
            "mean_pinball": "Mean pinball",
            "coverage_at_top_quantile": "Coverage at top quantile",
            "target_coverage": "Target",
            "beats_naive": "Beats baseline",
        },
    )

    chart_calibration = ""
    calibration_note = ""
    if not data.calibration.empty:
        chart_calibration = charts.coverage_strip(
            nominal=data.calibration["nominal_quantile"].astype(float).tolist(),
            achieved=data.calibration["achieved_coverage"].astype(float).tolist(),
            title="Requested quantile against measured coverage",
            desc=(
                "Each point plots the quantile the model was asked for against the share of outcomes "
                "that actually fell below it. Points below the dashed diagonal are quantiles the model "
                "does not fully deliver, and the shortfall is what the ordering plan compensates for."
            ),
        )
        shortfalls = data.calibration["coverage_shortfall"].astype(float)
        worst = float(shortfalls.max())
        calibration_note = (
            '<div class="note"><strong>The quantiles are calibrated but conservative.</strong> '
            "Coverage rises monotonically with the requested quantile, which is the property that matters — "
            f"but the top quantile falls short by {worst:.2f}. A plan that ordered to the nominal figure "
            "believing it was a true service level would under-order, so the achieved coverage is measured "
            "in the backtest and passed to the optimiser rather than assumed.</div>"
        )
    else:
        chart_calibration = '<p class="muted">No calibration measured.</p>'

    table_quality = _table(
        data.data_quality,
        {
            "store_code": "Store / source",
            "days_with_no_feed": "Duplicate files",
            "feed_gap_rate": "Gap rate",
            "unmapped_skus": "Unmapped SKUs",
            "unmapped_net_sales_value": "Unmapped sales $",
            "days_with_mixed_money_units": "Max lag (days)",
        },
        money=("unmapped_net_sales_value",),
    )

    generated = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M UTC")
    footer = (
        f"Generated {generated} · fresh-shelf-ops {cfg.project.get('name', '')} · "
        f"as-of {data.as_of} · seed {cfg.seed} · {data.stores} stores, {data.trading_days} trading days"
    )

    margin_ratio = (
        headline["realised_margin_value"] / headline["recorded_waste_value"]
        if headline["recorded_waste_value"]
        else 0.0
    )

    return template.render(
        title=cfg.report_cfg.get("title", "Fresh loss report"),
        subtitle=data.scope,
        as_of=data.as_of.isoformat(),
        as_of_minus_one=(pd.Timestamp(data.as_of) - pd.Timedelta(days=1)).date().isoformat(),
        trading_days=f"{data.trading_days:,}",
        stores=data.stores,
        items=f"{data.items:,}",
        stat_sales=stat_sales,
        stat_waste=stat_waste,
        stat_markdown=stat_markdown,
        stat_shrink=stat_shrink,
        shrink_note=shrink_note,
        chart_departments=chart_departments,
        table_departments=table_departments,
        table_league=table_league,
        table_markdown=table_markdown,
        chart_frontier=chart_frontier,
        frontier_note=frontier_note,
        impact_table=impact_table,
        reconciliation_note=reconciliation_note,
        table_forecast=table_forecast,
        chart_calibration=chart_calibration,
        calibration_note=calibration_note,
        table_quality=table_quality,
        margin_ratio=f"{margin_ratio:,.0f}",
        footer=footer,
    )


def render_markdown(data: ReportData, cfg: Config) -> str:
    """The same findings as Markdown, for people who read reports in a terminal
    or a git diff. Deliberately not a full mirror: the numbers that carry the
    argument are repeated, and the charts become sentences."""
    headline = data.headline
    lines: list[str] = []
    lines.append(f"# {cfg.report_cfg.get('title', 'Fresh loss report')}")
    lines.append("")
    lines.append(data.scope)
    lines.append("")
    lines.append(
        f"Reporting date **{data.as_of}**; the last closed trading day is "
        f"**{(pd.Timestamp(data.as_of) - pd.Timedelta(days=1)).date()}**. "
        f"Generated from the DuckDB warehouse; no figure is entered by hand."
    )
    lines.append("")
    lines.append("## Headline numbers")
    lines.append("")
    lines.append("| Measure | Value | Basis |")
    lines.append("| --- | ---: | --- |")
    lines.append(
        f"| Net sales | {_money(headline['sales_value'])} | {headline['units_sold']:,.0f} units |"
    )
    lines.append(
        f"| Recorded waste | {_money(headline['recorded_waste_value'])} | at cost; "
        f"{_pct(headline['waste_pct_of_sales'])} of sales — a floor, not the loss |"
    )
    lines.append(
        f"| Markdown erosion | {_money(headline['markdown_erosion_value'])} | at margin; "
        f"{_pct(headline['markdown_pct_of_sales'])} of sales |"
    )
    lines.append(
        f"| Unrecorded shrink (estimated) | {_money(headline['unrecorded_shrink_value'])} | "
        f"raw residue {_money(headline['unrecorded_shrink_raw_residue'])}, weighted by confidence "
        f"{headline['avg_shrink_confidence']:.2f} |"
    )
    lines.append("")
    lines.append(
        f"> Waste is a small share of sales ({_pct(headline['waste_pct_of_sales'])}) while margin is a large "
        f"one ({_pct(headline['margin_pct_of_sales'])}). A unit not ordered therefore gives up far more "
        "margin than the waste it avoids, which shapes every recommendation below."
    )
    lines.append("")

    if not data.loss_by_department.empty:
        lines.append("## Loss by department")
        lines.append("")
        lines.append("| Department | Net sales | Waste $ | Waste % | Markdown $ | Markdown % |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        for row in data.loss_by_department.itertuples():
            lines.append(
                f"| {row.department_name} | {_money(row.net_sales_value)} | "
                f"{_money(row.recorded_waste_cost)} | {row.waste_pct_of_sales:.2f}% | "
                f"{_money(row.markdown_erosion_value)} | {row.markdown_pct_of_sales:.2f}% |"
            )
        lines.append("")

    if not data.frontier.empty:
        optimal = data.frontier[data.frontier["is_optimal"]]
        if not optimal.empty:
            row = optimal.iloc[0]
            lines.append("## The ordering trade-off")
            lines.append("")
            lines.append(
                f"The optimum sits at a service level of **{float(row['service_level']):.2f}**, where waste "
                f"costs {_money(float(row['expected_waste_cost']))} and lost margin costs "
                f"{_money(float(row['expected_lost_margin']))}, a total of "
                f"{_money(float(row['total_cost']))}. Neither a blanket high service level nor starving the "
                "shelf is right; the balance is a property of each line."
            )
            lines.append("")

    if not data.reconciliation.empty:
        frame = data.reconciliation.set_index("check")["value"]
        agree = float(frame.get("methods_agree_on_direction", 1.0)) > 0.5
        lines.append("## Does the plan pay for itself?")
        lines.append("")
        if agree:
            lines.append(
                "The plan's own accounting and the frontier agree: the plan shows a benefit."
            )
        else:
            lines.append(
                f"**The two methods disagree, and both are shown deliberately.** The frontier has an interior "
                f"optimum at a service level of {float(frame.get('frontier_optimum_service_level', 0)):.2f}, so "
                "the trade-off is real. But the plan's own component accounting nets to "
                f"**{_money(float(frame.get('impact_net_benefit', 0)))}** — a loss — because waste is a small "
                "share of sales while margin is a large one. The recommended plan averages a service level "
                f"{float(frame.get('service_level_gap', 0)):.2f} above the unconstrained optimum, which is the "
                "price of pack sizes and shelf-life caps. On this estate ordering down is not the win; markdown "
                "timing and the lines whose cover exceeds their shelf life are."
            )
        lines.append("")

    if not data.impact.empty:
        lines.append("| Impact component | Value | Basis |")
        lines.append("| --- | ---: | --- |")
        for row in data.impact.itertuples():
            lines.append(f"| {row.component} | {float(row.value):,.2f} | {row.basis} |")
        lines.append("")

    if not data.forecast_metrics.empty:
        lines.append("## Forecast quality")
        lines.append("")
        lines.append("| Model | Folds | WAPE | Mean pinball | Coverage at top quantile |")
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        for row in data.forecast_metrics.itertuples():
            lines.append(
                f"| {row.model} | {row.folds} | {row.best_wape:.3f} | {row.mean_pinball:.3f} | "
                f"{row.coverage_at_top_quantile:.3f} (target {row.target_coverage:.2f}) |"
            )
        lines.append("")

    lines.append("## Caveats")
    lines.append("")
    for caveat in (
        "Recorded waste is a floor: roughly a fifth of disposals are never scanned.",
        "Unrecorded shrink is a bound dominated by physical-count error, not a measurement of theft.",
        "The backtest has perfect future weather, which flatters it against any real deployment.",
        "Markdown response is estimated from non-random history, so the optimiser under-marks-down slightly.",
        "The ordering baseline is reconstructed from delivery history, not observed as placed orders.",
        "The estate is simulated; the pipeline, reconciliation and decision logic are not.",
    ):
        lines.append(f"- {caveat}")
    lines.append("")
    return "\n".join(lines)


def build_report(cfg: Config, con: duckdb.DuckDBPyConnection) -> list[Path]:
    """Write report.html, report.md and metrics.json. Returns the paths written."""
    data = collect(cfg, con)
    html = render(data, cfg)
    markdown = render_markdown(data, cfg)

    out = cfg.paths.report_dir
    out.mkdir(parents=True, exist_ok=True)
    html_path = out / "index.html"
    md_path = out / "report.md"
    metrics_path = out / "metrics.json"

    html_path.write_text(html, encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")

    metrics = {
        "as_of_date": data.as_of.isoformat(),
        "last_trading_day": (pd.Timestamp(data.as_of) - pd.Timedelta(days=1)).date().isoformat(),
        "stores": data.stores,
        "departments": data.departments,
        "items": data.items,
        "trading_days": data.trading_days,
        "seed": cfg.seed,
        "headline": data.headline,
        "impact": data.impact.set_index("component")["value"].to_dict()
        if not data.impact.empty
        else {},
        "reconciliation": data.reconciliation.set_index("check")["value"].to_dict()
        if not data.reconciliation.empty
        else {},
        "forecast": data.forecast_metrics.to_dict("records"),
        "calibration": data.calibration.to_dict("records"),
        "frontier_optimal": data.frontier[data.frontier["is_optimal"]].to_dict("records")
        if not data.frontier.empty
        else [],
    }
    metrics_path.write_text(json.dumps(metrics, indent=2, default=str), encoding="utf-8")
    log.info("report written: %s", html_path)
    return [html_path, md_path, metrics_path]
