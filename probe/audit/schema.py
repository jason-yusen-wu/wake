"""
probe/audit/schema.py — data types for the Rung 1 failure audit.

A LabeledFailure records one SWE-bench instance together with the
human-applied taxonomy label.  The labeling answers the decisive
question: *would a precise static analysis have caught this, and which
property?*

Taxonomy categories (mutually exclusive, exhaustive):
  null_type        — None-deref, wrong type, Optional not handled.
                     This is what wake-prop-null catches today.
  incomplete_edit  — Changed a function but missed callers/dependents.
                     This is what wake's blast-radius (change-consistency)
                     catches.
  wrong_logic      — Locally plausible but globally incorrect logic;
                     no type or None issue.
  missing_edge     — A missing branch, boundary condition, or off-by-one.
  integration      — Interaction between components, serialization, ORM.
  api_misuse       — Calling an API with wrong args, wrong order, wrong
                     semantics.
  misunderstood_intent — The patch fixes something but not what the tests
                     require; analysis can't help here.
  other            — Doesn't fit any bucket.

AnalysisVerdict answers "would wake have caught this?":
  yes       — the failure mode is within wake's current scope and
              precision level; analysis would have fired.
  partial   — analysis would flag something related but not the exact
              root cause (e.g., a downstream symptom of a logic error).
  no        — definitively outside what static nullability/change-
              consistency analysis can catch.
  unknown   — not enough information to decide.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class FailureCategory(str, Enum):
    NULL_TYPE          = "null_type"
    INCOMPLETE_EDIT    = "incomplete_edit"
    WRONG_LOGIC        = "wrong_logic"
    MISSING_EDGE       = "missing_edge"
    INTEGRATION        = "integration"
    API_MISUSE         = "api_misuse"
    MISUNDERSTOOD_INTENT = "misunderstood_intent"
    OTHER              = "other"
    UNLABELED          = "unlabeled"

    @classmethod
    def choices(cls) -> list[str]:
        return [c.value for c in cls if c != cls.UNLABELED]

    @classmethod
    def from_str(cls, s: str) -> "FailureCategory":
        for c in cls:
            if c.value == s.lower():
                return c
        raise ValueError(f"Unknown category: {s!r}. Choose from: {cls.choices()}")


class AnalysisVerdict(str, Enum):
    YES     = "yes"
    PARTIAL = "partial"
    NO      = "no"
    UNKNOWN = "unknown"

    @classmethod
    def choices(cls) -> list[str]:
        return [v.value for v in cls]

    @classmethod
    def from_str(cls, s: str) -> "AnalysisVerdict":
        for v in cls:
            if v.value == s.lower():
                return v
        raise ValueError(f"Unknown verdict: {s!r}. Choose from: {cls.choices()}")


class AnalysisProperty(str, Enum):
    """Which analysis property would catch this failure, if any."""
    NULLABILITY         = "nullability"
    CHANGE_CONSISTENCY  = "change_consistency"   # blast-radius / missed callers
    TYPE_SAFETY         = "type_safety"
    RESOURCE_LIFETIME   = "resource_lifetime"
    OTHER               = "other"
    NONE                = "none"

    @classmethod
    def choices(cls) -> list[str]:
        return [p.value for p in cls]

    @classmethod
    def from_str(cls, s: str) -> "AnalysisProperty":
        for p in cls:
            if p.value == s.lower():
                return p
        raise ValueError(f"Unknown property: {s!r}. Choose from: {cls.choices()}")


class PatchSource(str, Enum):
    """How the patch being labeled was obtained."""
    GOLD              = "gold"               # the gold patch from SWE-bench
    AGENT_FAILED      = "agent_failed"       # agent patch that failed all held-out tests
    AGENT_PARTIAL     = "agent_partial"      # agent patch that fixed some but not all
    AGENT_REGRESSED   = "agent_regressed"    # agent patch that broke a pass_to_pass test


@dataclass
class LabeledFailure:
    """One labeled SWE-bench failure instance."""
    # Identity
    instance_id: str
    repo: str
    base_commit: str

    # Problem context
    problem_statement: str

    # The patch being analyzed
    patch: str
    patch_source: PatchSource

    # Test outcome (from SWE-bench evaluation)
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)
    # test_name → "PASSED" | "FAILED" | "ERROR"
    test_results: dict[str, str] = field(default_factory=dict)

    # Taxonomy label (filled in by label.py)
    category: FailureCategory = FailureCategory.UNLABELED
    would_catch: AnalysisVerdict = AnalysisVerdict.UNKNOWN
    which_property: AnalysisProperty = AnalysisProperty.NONE
    analysis_note: str = ""       # free text: how/why analysis would or wouldn't catch it

    # Metadata
    labeled_by: str = "human"
    label_timestamp: str = ""

    @property
    def is_labeled(self) -> bool:
        return self.category != FailureCategory.UNLABELED

    @property
    def is_analyzable(self) -> bool:
        """True if wake-style analysis would catch this (yes or partial)."""
        return self.would_catch in (AnalysisVerdict.YES, AnalysisVerdict.PARTIAL)

    @property
    def failed_held_out_tests(self) -> list[str]:
        """FAIL_TO_PASS tests that actually failed (held-out breaks)."""
        return [t for t in self.fail_to_pass if self.test_results.get(t) != "PASSED"]

    @property
    def broke_passing_tests(self) -> list[str]:
        """PASS_TO_PASS tests that now fail (regressions introduced)."""
        return [t for t in self.pass_to_pass if self.test_results.get(t) == "FAILED"]

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "repo": self.repo,
            "base_commit": self.base_commit,
            "problem_statement": self.problem_statement,
            "patch": self.patch,
            "patch_source": self.patch_source.value,
            "fail_to_pass": self.fail_to_pass,
            "pass_to_pass": self.pass_to_pass,
            "test_results": self.test_results,
            "category": self.category.value,
            "would_catch": self.would_catch.value,
            "which_property": self.which_property.value,
            "analysis_note": self.analysis_note,
            "labeled_by": self.labeled_by,
            "label_timestamp": self.label_timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LabeledFailure":
        return cls(
            instance_id=d["instance_id"],
            repo=d.get("repo", ""),
            base_commit=d.get("base_commit", ""),
            problem_statement=d.get("problem_statement", ""),
            patch=d.get("patch", ""),
            patch_source=PatchSource(d.get("patch_source", PatchSource.GOLD.value)),
            fail_to_pass=d.get("fail_to_pass", []),
            pass_to_pass=d.get("pass_to_pass", []),
            test_results=d.get("test_results", {}),
            category=FailureCategory(d.get("category", FailureCategory.UNLABELED.value)),
            would_catch=AnalysisVerdict(d.get("would_catch", AnalysisVerdict.UNKNOWN.value)),
            which_property=AnalysisProperty(d.get("which_property", AnalysisProperty.NONE.value)),
            analysis_note=d.get("analysis_note", ""),
            labeled_by=d.get("labeled_by", "human"),
            label_timestamp=d.get("label_timestamp", ""),
        )
