"""Tests for the report.

A report is code that makes claims, so it gets tested like code. Three properties
matter, and all three have failed silently in real projects:

* It must be self-contained. A static HTML report is meant to render offline in
  ten years. A CDN reference or a `<script>` tag breaks that promise invisibly.
  The page looks perfect on the machine that built it.
* It must be well-formed. Broken HTML renders as a plausible page with a section
  missing, which is worse than a visible error.
* It must not overstate. The headline is where a reader stops reading, so the
  caveats belong in the page itself rather than in a docstring.

The charts are pure functions of their inputs, so they are tested directly
rather than through the rendered page.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

import pytest

from fresh_shelf.config import load_config
from fresh_shelf.report import charts

# The chart tests are pure functions and always run; the page tests need a built
# warehouse, so the module is in the warehouse tier.
pytestmark = pytest.mark.warehouse


class _WellFormed(HTMLParser):
    """Minimal nesting checker. Not a validator. It catches the mistakes a
    template makes: an unclosed tag, or an end tag with nothing to close."""

    VOID = frozenset(
        {
            "meta",
            "br",
            "hr",
            "img",
            "input",
            "link",
            "source",
            "path",
            "rect",
            "line",
            "circle",
            "polyline",
            "text",
            "use",
        }
    )

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.stray: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.VOID:
            return
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass
        else:
            self.stray.append(tag)


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------
def test_bar_chart_escapes_its_labels():
    """A label is data, so it must not be able to inject markup."""
    svg = charts.bar_chart(
        labels=["<script>alert(1)</script>"],
        values=[1.0],
        title="t",
        desc="d",
    )
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


def test_every_chart_is_accessible():
    """Each SVG carries a title and a description, so a screen reader gets the
    finding rather than the word 'image'."""
    svgs = [
        charts.bar_chart(["a"], [1.0], title="T", desc="D"),
        charts.line_chart([0.0, 1.0], {"s": [1.0, 2.0]}, title="T", desc="D"),
        charts.scatter_bars(["a"], [1.0], [2.0], title="T", desc="D"),
        charts.coverage_strip([0.5, 0.9], [0.5, 0.85], title="T", desc="D"),
    ]
    for svg in svgs:
        assert "<title>T</title>" in svg
        assert "<desc>D</desc>" in svg
        assert 'role="img"' in svg


def test_line_chart_survives_a_flat_series():
    """A single distinct value makes the y-range zero, which would divide by it."""
    svg = charts.line_chart([0.5, 0.6, 0.7], {"only": [5.0, 5.0, 5.0]}, title="T", desc="D")
    assert "<svg" in svg
    assert "nan" not in svg.lower()


def test_line_chart_marks_the_minimum_when_asked():
    svg = charts.line_chart(
        [0.4, 0.5, 0.6],
        {"total": [30.0, 10.0, 20.0]},
        title="T",
        desc="D",
        annotate_min=True,
    )
    assert "Optimum 0.50" in svg


def test_charts_degrade_to_a_message_when_empty():
    """An empty frame must produce a sentence, not a broken chart."""
    assert "No data" in charts.bar_chart([], [], title="T", desc="D")
    assert "No data" in charts.line_chart([], {}, title="T", desc="D")
    assert "No data" in charts.scatter_bars([], [], [], title="T", desc="D")


def test_coverage_strip_draws_the_truth_diagonal():
    """Without the diagonal a reader cannot see the shortfall's direction."""
    svg = charts.coverage_strip([0.5, 0.9], [0.45, 0.85], title="T", desc="D")
    assert "perfect calibration" in svg
    assert "stroke-dasharray" in svg


# ---------------------------------------------------------------------------
# Rendered page
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """Render the report from the warehouse if one has been built.

    If there is no warehouse this fails rather than skipping. A skip reads as
    coverage that is not there, and these tests exist to catch a report that has
    stopped rendering its charts or stopped stating its caveats. CI builds the
    world before running tests so this cannot fire.
    """
    cfg = load_config()
    warehouse = cfg.paths.warehouse_file
    if not warehouse.exists():
        pytest.fail(
            f"no warehouse at {warehouse}; these tests need one. "
            "Run `make world && make daily` first. They do not skip, "
            "skip, because a skipped report test looks like passing coverage."
        )
    import duckdb

    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        tables = {
            r[0]
            for r in con.execute(
                "select table_name from information_schema.tables where table_schema = 'analytics'"
            ).fetchall()
        }
    except Exception as exc:
        pytest.fail(f"warehouse has no analytics schema: {exc}")
    if "waste_revenue_frontier" not in tables:
        pytest.fail(
            "the optimiser has not run, so there is no frontier to report on. Run `make daily`."
        )

    from fresh_shelf.report.build import build_report

    out = tmp_path_factory.mktemp("report")
    cfg = cfg.override(build_dir=out)
    paths = build_report(cfg, con)
    html = (out / "report" / "index.html").read_text()
    markdown = (out / "report" / "report.md").read_text()
    metrics = json.loads((out / "report" / "metrics.json").read_text())
    return html, markdown, metrics, paths


def test_report_is_well_formed_html(rendered):
    html, _, _, _ = rendered
    checker = _WellFormed()
    checker.feed(html)
    assert checker.stack == [], f"unclosed tags: {checker.stack}"
    assert checker.stray == [], f"stray end tags: {checker.stray}"
    assert html.lstrip().startswith("<!DOCTYPE html>")


def test_report_is_self_contained(rendered):
    """No CDN, no script, no external stylesheet. The file has to work offline.

    The only permitted absolute URI is the SVG namespace declaration, which is an
    identifier rather than a fetch.
    """
    html, _, _, _ = rendered
    assert "<script" not in html.lower()
    assert "<link" not in html.lower()
    external = [
        uri
        for uri in re.findall(r"https?://[^\s\"'<>]+", html)
        if not uri.startswith("http://www.w3.org/")
    ]
    assert external == [], f"external references found: {external}"


def test_report_renders_its_charts_as_svg(rendered):
    html, _, _, _ = rendered
    assert html.count("<svg") >= 3
    assert "&lt;svg" not in html, "chart markup was escaped into visible text"


def test_report_states_the_caveats_in_the_page_itself(rendered):
    """A caveat in a docstring is a caveat nobody reads.

    Matched against whitespace-normalised text: the template wraps its prose, so
    a multi-word phrase can straddle a line break and a naive substring check
    fails on a caveat that is plainly there. That would be a bug in the test, not
    the report, and it would read as "the caveat is missing".
    """
    html, _, _, _ = rendered
    normalised = " ".join(html.lower().split())
    for phrase in (
        "recorded waste is a floor",
        "physical-count error",
        "perfect knowledge",
        "simulated estate",
        "markdown response is estimated from non-random history",
    ):
        assert phrase in normalised, f"missing caveat: {phrase}"


def test_report_shows_the_shrink_bound_rather_than_a_single_number(rendered):
    """The raw residue overstates loss, so both numbers must appear together."""
    _, _, metrics, _ = rendered
    headline = metrics["headline"]
    assert headline["unrecorded_shrink_raw_residue"] > headline["unrecorded_shrink_value"]
    assert 0 < headline["avg_shrink_confidence"] <= 1


def test_report_publishes_the_impact_disagreement(rendered):
    """The methods disagree on this estate, and the page has to say so."""
    _, markdown, metrics, _ = rendered
    reconciliation = metrics.get("reconciliation", {})
    assert "service_level_gap" in reconciliation
    if float(reconciliation.get("methods_agree_on_direction", 1)) < 0.5:
        assert "disagree" in markdown.lower()


def test_report_scopes_itself_from_the_data(rendered):
    """The scope line is derived, so it cannot misstate the world it describes."""
    _, markdown, metrics, _ = rendered
    assert f"{metrics['stores']} stores" in markdown
    assert f"{metrics['trading_days']} trading days" in markdown


def test_markdown_mirror_carries_the_headline_numbers(rendered):
    _, markdown, metrics, _ = rendered
    assert metrics["as_of_date"] in markdown
    assert "WAPE" in markdown
    assert "Recorded waste" in markdown


def test_metrics_json_is_machine_readable(rendered):
    _, _, metrics, paths = rendered
    assert len(paths) == 3
    assert set(metrics) >= {
        "as_of_date",
        "stores",
        "headline",
        "impact",
        "forecast",
        "calibration",
    }
    assert metrics["headline"]["sales_value"] > 0
    # Every impact component must carry its own derivation.
    assert metrics["frontier_optimal"], "the frontier optimum was not published"


def test_report_never_sums_the_three_losses_into_one(rendered):
    """Waste, markdown and shrink are different problems and must stay apart.

    A single "total loss" figure would average three different fixes into one
    number nobody can act on, so the page is checked for the temptation.
    """
    html, _, _, _ = rendered
    assert "total loss" not in html.lower()
    assert "Recorded waste" in html
    assert "Markdown erosion" in html
    assert "Unrecorded shrink" in html
