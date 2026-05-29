"""
partition — extract visible/held-out test split from SWE-bench instances.

SWE-bench Verified instances already carry this information:
  FAIL_TO_PASS  — tests that fail before the gold patch, pass after.
                  These are the "held-out" tests: the ones the agent must fix.
                  A correct patch makes all of these pass.
  PASS_TO_PASS  — tests that pass both before and after the gold patch.
                  These must not regress. Visible to the agent via the
                  existing test suite.

The visible/held-out framing from the design doc (§8):
  "caught what tests did not" — wake catching a held-out break means it
  found a regression the visible test suite would have missed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class InstancePartition:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    # Tests that must transition fail→pass (the target, "held-out" from agent)
    fail_to_pass: list[str]
    # Tests that must stay passing ("visible" — agent can run these)
    pass_to_pass: list[str]
    # Gold patch (for reference, not given to agent)
    gold_patch: str = ""


def load_dataset(dataset_path: str | Path) -> list[InstancePartition]:
    """
    Load SWE-bench Verified instances from a JSONL file or HuggingFace.

    Accepts either a local .jsonl path or the string "swebench_verified"
    to download from HuggingFace datasets.
    """
    path = Path(dataset_path)
    if path.exists() and path.suffix in (".jsonl", ".json"):
        return _load_jsonl(path)
    return _load_huggingface(str(dataset_path))


def _load_jsonl(path: Path) -> list[InstancePartition]:
    instances = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            instances.append(_parse_instance(d))
    return instances


def _load_huggingface(name: str) -> list[InstancePartition]:
    from datasets import load_dataset as hf_load
    split = "test"
    if name == "swebench_verified":
        ds = hf_load("princeton-nlp/SWE-bench_Verified", split=split)
    else:
        ds = hf_load(name, split=split)
    return [_parse_instance(dict(row)) for row in ds]


def _parse_instance(d: dict) -> InstancePartition:
    ftp = d.get("FAIL_TO_PASS", [])
    ptp = d.get("PASS_TO_PASS", [])
    # Some dataset versions store these as JSON strings
    if isinstance(ftp, str):
        ftp = json.loads(ftp)
    if isinstance(ptp, str):
        ptp = json.loads(ptp)
    return InstancePartition(
        instance_id=d.get("instance_id", ""),
        repo=d.get("repo", ""),
        base_commit=d.get("base_commit", ""),
        problem_statement=d.get("problem_statement", ""),
        fail_to_pass=ftp,
        pass_to_pass=ptp,
        gold_patch=d.get("patch", ""),
    )


def select_instances(
    instances: list[InstancePartition],
    n: int | None = None,
    instance_ids: list[str] | None = None,
    repo_filter: str | None = None,
) -> list[InstancePartition]:
    """
    Select a subset of instances for evaluation.

    n:            take the first n instances
    instance_ids: explicit list of instance_ids to include
    repo_filter:  only include instances from repos matching this substring
    """
    result = instances
    if instance_ids:
        id_set = set(instance_ids)
        result = [i for i in result if i.instance_id in id_set]
    if repo_filter:
        result = [i for i in result if repo_filter in i.repo]
    if n is not None:
        result = result[:n]
    return result
