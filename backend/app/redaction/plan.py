"""Turns reviewed entities into redaction targets and defines how redactions are verified."""
import re
from collections import Counter
from dataclasses import dataclass, field

NUMERIC_TYPES = {"AADHAAR", "PHONE", "BANK_ACCOUNT", "PAN", "PASSPORT", "VOTER_ID", "DRIVING_LICENSE", "VEHICLE_REG"}
MIN_VALIDATE_LEN = 3  # shorter needles produce meaningless matches


class RedactionError(Exception):
    """Message is safe to show to the user."""


@dataclass
class Target:
    start: int
    end: int
    types: set[str] = field(default_factory=set)
    entity_ids: list = field(default_factory=list)


def merge_targets(entities) -> list[Target]:
    """Union overlapping spans (adjacent spans stay separate). Each entity keeps its id for the audit trail."""
    out: list[Target] = []
    for e in sorted(entities, key=lambda e: (e.start_position, e.end_position)):
        if out and e.start_position < out[-1].end:
            t = out[-1]
            t.end = max(t.end, e.end_position)
        else:
            t = Target(e.start_position, e.end_position)
            out.append(t)
        t.types.add(e.entity_type)
        t.entity_ids.append(e.id)
    return out


def replacement(style: str, value: str, types: set[str]) -> str:
    """Text substituted for a span (TXT/DOCX). PDFs and images are always fully blacked out."""
    alnum = [c for c in value if c.isalnum()]
    if style == "partial" and types <= NUMERIC_TYPES and len(alnum) >= 8:
        return "X" * (len(alnum) - 4) + "".join(alnum[-4:])
    return "[REDACTED]"


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s).casefold()


def check_absent(original_text: str, output_text: str, redacted_values: list[str]) -> dict:
    """Every redacted value must occur fewer times in the output than in the original, by at least the number
    of entities redacted. Occurrences that were never selected (KEEP, undetected) may legitimately remain."""
    before_n, after_n = norm(original_text), norm(output_text)
    needles = Counter(norm(v) for v in redacted_values if len(norm(v)) >= MIN_VALIDATE_LEN)
    leaks = []
    for needle, removed in needles.items():
        allowed = max(before_n.count(needle) - removed, 0)
        if after_n.count(needle) > allowed:
            leaks.append({"length": len(needle), "remaining": after_n.count(needle), "allowed": allowed})
    return {"checked_values": len(needles), "leaks": len(leaks), "passed": not leaks, "leak_details": leaks[:5]}
