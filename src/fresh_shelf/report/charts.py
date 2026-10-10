"""Inline SVG charts.

No charting library and no CDN. The report has to be a single file that renders
in ten years, offline, on a machine that has never heard of this project — so
every chart is hand-built SVG embedded in the HTML. That also means the charts
are accessible: each one carries a `<title>` and a `<desc>`, so a screen reader
reads the finding rather than announcing "image".

The palette is defined once here. It uses a diverging scale for the
waste-versus-margin charts, because the whole point of those is which of two
opposing costs dominates, and a single-hue ramp would hide exactly that.
"""

from __future__ import annotations

from html import escape

#: Costs and losses. Red for waste, amber for markdown erosion, slate for
#: context that is neither a cost nor a saving.
PALETTE = {
    "waste": "#b3261e",
    "markdown": "#c77700",
    "sales": "#1a5f7a",
    "positive": "#2e7d32",
    "negative": "#b3261e",
    "muted": "#5f6368",
    "grid": "#dadce0",
    "ink": "#202124",
    "band": "#f1f3f4",
}

#: A sequential ramp for depth/severity, ordered light to dark.
RAMP = ["#e8f0f4", "#b9d3e0", "#7faec6", "#4a86a6", "#1a5f7a"]


def _svg(width: int, height: int, title: str, desc: str, body: str) -> str:
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">'
        f"<title>{escape(title)}</title><desc>{escape(desc)}</desc>"
        f"{body}</svg>"
    )


def _empty(width: int, height: int, message: str) -> str:
    return (
        f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
        f'fill="{PALETTE["muted"]}" font-size="13" font-family="system-ui, sans-serif">'
        f"{escape(message)}</text>"
    )


def bar_chart(
    labels: list[str],
    values: list[float],
    *,
    title: str,
    desc: str,
    colour: str | None = None,
    value_format: str = "{:,.0f}",
    width: int = 640,
    row_height: int = 26,
    unit: str = "",
) -> str:
    """Horizontal bars. One row per category, longest first by convention."""
    if not labels:
        return _svg(width, 80, title, desc, _empty(width, 80, "No data"))
    colour = colour or PALETTE["waste"]
    label_width = 190
    value_width = 110
    height = len(labels) * row_height + 24
    max_value = max(values) if values else 1.0
    max_value = max_value if max_value > 0 else 1.0
    bar_space = width - label_width - value_width

    parts: list[str] = []
    for i, (label, value) in enumerate(zip(labels, values, strict=False)):
        y = i * row_height + 14
        bar_width = max(1.0, (max(value, 0) / max_value) * bar_space)
        parts.append(
            f'<text x="0" y="{y + 13}" font-size="12" fill="{PALETTE["ink"]}" '
            f'font-family="system-ui, sans-serif">{escape(str(label))}</text>'
        )
        parts.append(
            f'<rect x="{label_width}" y="{y}" width="{bar_width:.1f}" '
            f'height="{row_height - 8}" rx="2" fill="{colour}"/>'
        )
        parts.append(
            f'<text x="{label_width + bar_width + 6}" y="{y + 13}" font-size="12" '
            f'fill="{PALETTE["muted"]}" font-family="system-ui, sans-serif">'
            f"{escape(value_format.format(value))}{escape(unit)}</text>"
        )
    return _svg(width, height, title, desc, "".join(parts))


def line_chart(
    x: list[float],
    series: dict[str, list[float]],
    *,
    title: str,
    desc: str,
    colours: dict[str, str] | None = None,
    x_label: str = "",
    y_label: str = "",
    width: int = 680,
    height: int = 300,
    annotate_min: bool = False,
) -> str:
    """Multi-series line chart, used for the waste-vs-revenue frontier."""
    if not x or not series:
        return _svg(width, height, title, desc, _empty(width, height, "No data"))
    colours = colours or {}
    pad_l, pad_r, pad_t, pad_b = 64, 130, 18, 44
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    all_y = [v for values in series.values() for v in values if v is not None]
    if not all_y:
        return _svg(width, height, title, desc, _empty(width, height, "No data"))
    y_min, y_max = min(0.0, min(all_y)), max(all_y)
    if y_max <= y_min:
        y_max = y_min + 1
    x_min, x_max = min(x), max(x)
    if x_max <= x_min:
        x_max = x_min + 1

    def sx(v: float) -> float:
        return pad_l + (v - x_min) / (x_max - x_min) * plot_w

    def sy(v: float) -> float:
        return pad_t + plot_h - (v - y_min) / (y_max - y_min) * plot_h

    parts: list[str] = []
    # Axes and horizontal gridlines with value labels.
    for i in range(5):
        value = y_min + (y_max - y_min) * i / 4
        tick_y = sy(value)
        parts.append(
            f'<line x1="{pad_l}" y1="{tick_y:.1f}" x2="{pad_l + plot_w}" y2="{tick_y:.1f}" '
            f'stroke="{PALETTE["grid"]}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{tick_y + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="{PALETTE["muted"]}" font-family="system-ui, sans-serif">'
            f"{value:,.0f}</text>"
        )
    for i in range(5):
        value = x_min + (x_max - x_min) * i / 4
        # NB: not `x` — that would rebind the function's `x` parameter and break
        # the series loop below, which is how this chart first shipped.
        tick_x = sx(value)
        parts.append(
            f'<text x="{tick_x:.1f}" y="{pad_t + plot_h + 18}" text-anchor="middle" '
            f'font-size="11" fill="{PALETTE["muted"]}" '
            f'font-family="system-ui, sans-serif">{value:.2f}</text>'
        )
    parts.append(
        f'<text x="{pad_l + plot_w / 2:.1f}" y="{height - 6}" text-anchor="middle" '
        f'font-size="11" fill="{PALETTE["muted"]}" '
        f'font-family="system-ui, sans-serif">{escape(x_label)}</text>'
    )
    parts.append(
        f'<text x="14" y="{pad_t + plot_h / 2:.1f}" text-anchor="middle" font-size="11" '
        f'fill="{PALETTE["muted"]}" transform="rotate(-90 14 {pad_t + plot_h / 2:.1f})" '
        f'font-family="system-ui, sans-serif">{escape(y_label)}</text>'
    )

    legend_y = pad_t
    for name, values in series.items():
        colour = colours.get(name, PALETTE["sales"])
        points = " ".join(
            f"{sx(xv):.1f},{sy(yv):.1f}"
            for xv, yv in zip(x, values, strict=False)
            if yv is not None
        )
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="{colour}" stroke-width="2.2"/>'
        )
        parts.append(
            f'<line x1="{pad_l + plot_w + 12}" y1="{legend_y + 4}" '
            f'x2="{pad_l + plot_w + 32}" y2="{legend_y + 4}" stroke="{colour}" stroke-width="2.5"/>'
        )
        parts.append(
            f'<text x="{pad_l + plot_w + 37}" y="{legend_y + 8}" font-size="11" '
            f'fill="{PALETTE["ink"]}" font-family="system-ui, sans-serif">{escape(name)}</text>'
        )
        legend_y += 20

    if annotate_min:
        first_name = next(iter(series))
        first = series[first_name]
        best = min(
            range(len(first)),
            key=lambda i: first[i] if first[i] is not None else float("inf"),
        )
        parts.append(
            f'<circle cx="{sx(x[best]):.1f}" cy="{sy(first[best]):.1f}" r="4.5" '
            f'fill="{PALETTE["ink"]}"/>'
        )
        parts.append(
            f'<text x="{sx(x[best]):.1f}" y="{sy(first[best]) - 10:.1f}" text-anchor="middle" '
            f'font-size="11" fill="{PALETTE["ink"]}" '
            f'font-family="system-ui, sans-serif">Optimum {x[best]:.2f}</text>'
        )
    return _svg(width, height, title, desc, "".join(parts))


def scatter_bars(
    labels: list[str],
    waste: list[float],
    markdown: list[float],
    *,
    title: str,
    desc: str,
    width: int = 680,
    row_height: int = 30,
) -> str:
    """Stacked horizontal bars: waste cost against markdown erosion per group.

    Stacked rather than grouped because the question is the *total* cost of
    getting a department wrong, and which of the two dominates — which grouped
    bars make you compute mentally.
    """
    if not labels:
        return _svg(width, 100, title, desc, _empty(width, 100, "No data"))
    label_width, value_width = 170, 120
    height = len(labels) * row_height + 34
    totals = [w + m for w, m in zip(waste, markdown, strict=False)]
    max_total = max(totals) if totals else 1.0
    max_total = max_total if max_total > 0 else 1.0
    bar_space = width - label_width - value_width

    parts: list[str] = []
    # Legend.
    parts.append(
        f'<rect x="0" y="0" width="10" height="10" fill="{PALETTE["waste"]}"/>'
        f'<text x="15" y="9" font-size="11" fill="{PALETTE["ink"]}" '
        f'font-family="system-ui, sans-serif">Recorded waste (cost)</text>'
        f'<rect x="180" y="0" width="10" height="10" fill="{PALETTE["markdown"]}"/>'
        f'<text x="195" y="9" font-size="11" fill="{PALETTE["ink"]}" '
        f'font-family="system-ui, sans-serif">Markdown erosion (margin)</text>'
    )
    for i, label in enumerate(labels):
        y = i * row_height + 26
        w_width = max(0.0, waste[i] / max_total * bar_space)
        m_width = max(0.0, markdown[i] / max_total * bar_space)
        parts.append(
            f'<text x="0" y="{y + 13}" font-size="12" fill="{PALETTE["ink"]}" '
            f'font-family="system-ui, sans-serif">{escape(str(label))}</text>'
        )
        parts.append(
            f'<rect x="{label_width}" y="{y}" width="{w_width:.1f}" '
            f'height="{row_height - 10}" fill="{PALETTE["waste"]}"/>'
        )
        parts.append(
            f'<rect x="{label_width + w_width:.1f}" y="{y}" width="{m_width:.1f}" '
            f'height="{row_height - 10}" fill="{PALETTE["markdown"]}"/>'
        )
        parts.append(
            f'<text x="{label_width + w_width + m_width + 6:.1f}" y="{y + 13}" font-size="11" '
            f'fill="{PALETTE["muted"]}" font-family="system-ui, sans-serif">'
            f"${totals[i]:,.0f}</text>"
        )
    return _svg(width, height, title, desc, "".join(parts))


def coverage_strip(
    nominal: list[float],
    achieved: list[float],
    *,
    title: str,
    desc: str,
    width: int = 420,
    height: int = 180,
) -> str:
    """Nominal quantile against measured coverage, with the diagonal as truth.

    Points below the diagonal are quantiles the model does not actually deliver,
    which is the number the optimiser has to be told about. Showing the diagonal
    means a reader can see the shortfall without reading the axis labels.
    """
    if not nominal:
        return _svg(width, height, title, desc, _empty(width, height, "No data"))
    pad_l, pad_r, pad_t, pad_b = 46, 16, 16, 40
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def sx(v: float) -> float:
        return pad_l + (v - 0.4) / 0.6 * plot_w

    def sy(v: float) -> float:
        return pad_t + plot_h - (v - 0.4) / 0.6 * plot_h

    parts: list[str] = []
    parts.append(
        f'<line x1="{sx(0.4):.1f}" y1="{sy(0.4):.1f}" x2="{sx(1.0):.1f}" y2="{sy(1.0):.1f}" '
        f'stroke="{PALETTE["muted"]}" stroke-dasharray="4 3" stroke-width="1.4"/>'
    )
    parts.append(
        f'<text x="{sx(0.86):.1f}" y="{sy(0.86) - 6:.1f}" font-size="10" '
        f'fill="{PALETTE["muted"]}" font-family="system-ui, sans-serif">perfect calibration</text>'
    )
    points = " ".join(f"{sx(n):.1f},{sy(a):.1f}" for n, a in zip(nominal, achieved, strict=False))
    parts.append(
        f'<polyline points="{points}" fill="none" stroke="{PALETTE["sales"]}" stroke-width="2.2"/>'
    )
    for n, a in zip(nominal, achieved, strict=False):
        parts.append(
            f'<circle cx="{sx(n):.1f}" cy="{sy(a):.1f}" r="3.5" fill="{PALETTE["sales"]}"/>'
        )
    for tick in (0.4, 0.6, 0.8, 1.0):
        parts.append(
            f'<text x="{sx(tick):.1f}" y="{pad_t + plot_h + 16}" text-anchor="middle" '
            f'font-size="10" fill="{PALETTE["muted"]}" '
            f'font-family="system-ui, sans-serif">{tick:.1f}</text>'
        )
        parts.append(
            f'<text x="{pad_l - 6}" y="{sy(tick) + 3:.1f}" text-anchor="end" font-size="10" '
            f'fill="{PALETTE["muted"]}" font-family="system-ui, sans-serif">{tick:.1f}</text>'
        )
    parts.append(
        f'<text x="{pad_l + plot_w / 2:.1f}" y="{height - 4}" text-anchor="middle" font-size="10" '
        f'fill="{PALETTE["muted"]}" font-family="system-ui, sans-serif">quantile requested</text>'
    )
    return _svg(width, height, title, desc, "".join(parts))
