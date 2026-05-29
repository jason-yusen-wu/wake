"""
probe/oracle/schema.py — data types for the Rung 2 Wizard-of-Oz oracle.

An OracleFeedback record stores the hand-crafted "perfect analyzer output"
for one failure instance.  The oracle harness injects this feedback into
the agent loop in place of the wake daemon's output.

The feedback is written as the human would expect a perfect static analyzer
to phrase it — i.e., in the same format as wake's shaped feedback, but
reflecting exactly what would catch the actual bug.  See record.py for the
guided entry UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OracleFeedback:
    """
    Hand-crafted "perfect wake output" for one failure instance.

    The natural-language feedback is what the oracle loop injects into the
    agent's message stream.  It should be as specific as possible:
      - Name the variable that can be None
      - Name the line(s) where the dereference occurs
      - Describe the value-flow path from source to consumer
      - Suggest a fix locus

    The is_analyzable flag is set by the labeler to confirm this case was
    correctly selected from the analyzable subset of Rung 1.
    """
    instance_id: str
    # Natural language feedback text the oracle loop will inject.
    feedback_text: str
    # Category and property from the Rung 1 label (for stratified analysis).
    category: str = ""
    which_property: str = ""
    # How confident the labeler is that this feedback is correct.
    # high = would definitely fire; medium = probably; low = uncertain
    confidence: str = "high"
    # True = labeler confirmed this is in the analyzable subset.
    is_analyzable: bool = True
    # Optional: the gold patch (for reference during recording).
    gold_patch: str = ""
    recorded_by: str = "human"
    record_timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "feedback_text": self.feedback_text,
            "category": self.category,
            "which_property": self.which_property,
            "confidence": self.confidence,
            "is_analyzable": self.is_analyzable,
            "gold_patch": self.gold_patch,
            "recorded_by": self.recorded_by,
            "record_timestamp": self.record_timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OracleFeedback":
        return cls(
            instance_id=d["instance_id"],
            feedback_text=d.get("feedback_text", ""),
            category=d.get("category", ""),
            which_property=d.get("which_property", ""),
            confidence=d.get("confidence", "high"),
            is_analyzable=d.get("is_analyzable", True),
            gold_patch=d.get("gold_patch", ""),
            recorded_by=d.get("recorded_by", "human"),
            record_timestamp=d.get("record_timestamp", ""),
        )
