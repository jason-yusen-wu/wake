"""
probe/audit/dataset.py — load, save, and query the labeled failure dataset.

The dataset is a JSONL file: one JSON object per line, one LabeledFailure
per object.  JSONL is append-friendly and git-diffable.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterator

from schema import FailureCategory, AnalysisVerdict, LabeledFailure, PatchSource

DEFAULT_DATASET = Path(__file__).parent / "corpus" / "labeled_failures.jsonl"


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load(path: Path = DEFAULT_DATASET) -> list[LabeledFailure]:
    """Load all records from the dataset file (empty list if file absent)."""
    if not path.exists():
        return []
    with open(path) as f:
        return [LabeledFailure.from_dict(json.loads(line)) for line in f if line.strip()]


def save(failures: list[LabeledFailure], path: Path = DEFAULT_DATASET) -> None:
    """Overwrite the dataset file with the given records."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for failure in failures:
            f.write(json.dumps(failure.to_dict()) + "\n")


def upsert(failure: LabeledFailure, path: Path = DEFAULT_DATASET) -> None:
    """Add or replace the record for failure.instance_id."""
    existing = load(path)
    updated = [f for f in existing if f.instance_id != failure.instance_id]
    updated.append(failure)
    save(updated, path)


# ---------------------------------------------------------------------------
# Filtering / querying
# ---------------------------------------------------------------------------

def filter_failures(
    failures: list[LabeledFailure],
    *,
    labeled_only: bool = False,
    analyzable_only: bool = False,
    category: FailureCategory | None = None,
    verdict: AnalysisVerdict | None = None,
    source: PatchSource | None = None,
) -> list[LabeledFailure]:
    result = failures
    if labeled_only:
        result = [f for f in result if f.is_labeled]
    if analyzable_only:
        result = [f for f in result if f.is_analyzable]
    if category is not None:
        result = [f for f in result if f.category == category]
    if verdict is not None:
        result = [f for f in result if f.would_catch == verdict]
    if source is not None:
        result = [f for f in result if f.patch_source == source]
    return result


def ids(failures: list[LabeledFailure]) -> set[str]:
    return {f.instance_id for f in failures}
