"""The boundary between the warehouse and the answer key.

The most important property in this project is negative: **the pipeline must not
read `data/truth`.** That directory holds the simulator's own record of what
happened to the shelf, and it exists so the tests can check the pipeline against
known answers. If any part of the pipeline read it, every downstream claim would
be circular. The warehouse would be validating itself against a file only it can
see, and a bug in the loader would be invisible because both sides would share
it.

So this file enforces the boundary by reading the source. That is unfashionable
for a test, and it is the right tool here: the failure mode is a single line added
during a debugging session and forgotten, which would not raise, would not fail
any other test, and would quietly make every number in the report meaningless.

Two distinctions the enforcement has to respect, or it becomes noise nobody reads:

* **Writing is fine; reading the sealed tables is not.** The world generator and
  the CLI write the truth; that is its whole purpose.
* **`landing_gaps.csv` and `landing_manifest.csv` are pipeline metadata, not the
  answer key.** They record what the *source systems* sent, which is exactly what
  a real pipeline would know. Only the `truth_*.parquet` files are answers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fresh_shelf.config import REPO_ROOT, load_config

SRC = REPO_ROOT / "src" / "fresh_shelf"

#: The sealed answer-key tables. Named with a `truth_` prefix precisely so that a
#: stray reference is obvious both to a reader and to this scan.
SEALED_TABLES = (
    "truth_store_item_day",
    "truth_disposals",
    "truth_expiry_buckets",
    "truth_markdown_lift",
    "truth_calendar",
    "truth_weather",
)

#: Pipeline metadata that legitimately lives beside the truth because it records
#: what the sources did, not what the answer is.
METADATA_FILES = ("landing_gaps.csv", "landing_manifest.csv")


def _python_sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _sql_files() -> list[Path]:
    return sorted((REPO_ROOT / "models").rglob("*.sql")) + sorted(
        (REPO_ROOT / "dbt_tests").rglob("*.sql")
    )


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Source lines that are code, not prose.

    Docstrings and comments are stripped: a module explaining that it must *not*
    read the truth is not a violation, and treating it as one would train a
    reviewer to ignore this test.
    """
    text = path.read_text()
    if path.suffix == ".py":
        # Remove triple-quoted blocks, then line comments.
        text = re.sub(r'"""(?:.|\n)*?"""', "", text)
        text = re.sub(r"'''(?:.|\n)*?'''", "", text)
    lines = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        if path.suffix == ".sql" and stripped.startswith("--"):
            continue
        lines.append((lineno, raw))
    return lines


def test_no_module_reads_a_sealed_truth_table():
    """Nothing under `src/fresh_shelf` may read a `truth_*` table.

    Checked against the table names rather than the directory, so that writing
    the truth and reading pipeline metadata stay legal while reading an answer
    does not.
    """
    offenders: list[str] = []
    for path in _python_sources():
        for lineno, line in _code_lines(path):
            if path.parent.name == "world" or path.name == "cli.py":
                continue  # the writers
            for table in SEALED_TABLES:
                if table in line and "parquet" in line and "to_parquet" not in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}  {line.strip()}")
    assert not offenders, (
        "the pipeline reads its own answer key; these lines must go:\n"
        + "\n".join(f"  {o}" for o in offenders)
    )


def test_no_module_reads_the_truth_directory_for_parquet():
    offenders: list[str] = []
    for path in _python_sources():
        if path.parent.name == "world" or path.name == "cli.py" or path.name == "config.py":
            continue
        for lineno, line in _code_lines(path):
            if "truth_dir" in line and "read" in line.lower() and "parquet" in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}  {line.strip()}")
    assert not offenders, "the pipeline reads parquet from the truth directory:\n" + "\n".join(
        f"  {o}" for o in offenders
    )


def test_dbt_models_never_read_the_truth():
    """The warehouse must be buildable from `raw` alone.

    A `read_parquet('data/truth/...')` in a model would make the warehouse depend
    on a file the pipeline produces for testing, and would break the moment the
    truth directory was cleaned.
    """
    offenders: list[str] = []
    for path in _sql_files():
        for lineno, line in _code_lines(path):
            lowered = line.lower()
            if "truth" in lowered and (
                "read_parquet" in lowered or "read_csv" in lowered or "source(" in lowered
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}  {line.strip()}")
    assert not offenders, "a dbt model or test reads the sealed truth:\n" + "\n".join(
        f"  {o}" for o in offenders
    )


def test_the_world_generator_owns_the_truth():
    """One writer, so the answer key has one owner.

    `db.py` offers a `write_parquet` helper that the world generator and the CLI
    use; that is a utility, not a second producer. What must not appear is a
    third module inventing its own answers.
    """
    producers = set()
    for path in _python_sources():
        text = path.read_text()
        if re.search(r"to_parquet\(|write_parquet\(", text):
            rel = str(path.relative_to(REPO_ROOT))
            if rel.endswith("db.py"):
                continue  # the shared helper
            producers.add(rel)
    assert producers, "nothing writes the truth, so the tests have no answer key"
    unexpected = {
        p for p in producers if "world" not in p and not p.endswith("cli.py") and "report" not in p
    }
    assert not unexpected, f"the truth is written outside the world generator: {sorted(unexpected)}"


def test_landing_dir_and_truth_dir_are_separate_trees():
    """The dirty files and the answer key must not share a directory."""
    cfg = load_config()
    landing = cfg.paths.landing_dir.resolve()
    truth = cfg.paths.truth_dir.resolve()
    assert landing != truth
    assert truth not in landing.parents
    assert landing not in truth.parents


def test_metadata_files_are_not_mistaken_for_answers():
    """`landing_gaps.csv` records what the sources sent. A real pipeline knows
    that, so reading it is legitimate. It lives beside the answers only for
    convenience, and this test documents the distinction so the next person does
    not "clean up" by deleting it."""
    for name in METADATA_FILES:
        assert not name.startswith("truth_"), (
            f"{name} is pipeline metadata and must not carry the truth prefix, or it "
            "will be caught by the sealed-table scan"
        )


@pytest.mark.parametrize("name", ["truth_store_item_day", "truth_disposals", "truth_calendar"])
def test_truth_tables_are_named_for_what_they_are(name: str):
    """A `truth_` prefix, so a stray reference is obvious in review."""
    cfg = load_config()
    path = cfg.paths.truth_dir / f"{name}.parquet"
    if not path.exists():
        pytest.skip("no world generated yet")
    assert path.name.startswith("truth_")
