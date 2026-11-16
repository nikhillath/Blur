"""Entity taxonomy, shared detection types, and value masking."""
from dataclasses import dataclass, field

ENTITY_TYPES = (
    "PERSON", "AADHAAR", "PAN", "PASSPORT", "VOTER_ID", "DRIVING_LICENSE", "PHONE", "EMAIL",
    "ADDRESS", "DATE_OF_BIRTH", "BANK_ACCOUNT", "IFSC", "UPI_ID", "VEHICLE_REG",
    "CUSTOMER_ID", "EMPLOYEE_ID", "OTHER_IDENTIFIER",
)
ENTITY_SET = frozenset(ENTITY_TYPES)

# Government-issued identifiers (kept distinct from ordinary business identifiers).
GOVERNMENT_IDS = frozenset({"AADHAAR", "PAN", "PASSPORT", "VOTER_ID", "DRIVING_LICENSE"})

# Maps labels emitted by external/fine-tuned token-classification models to the taxonomy.
# Anything not listed (e.g. ORG, MISC, DATE) is intentionally ignored: it is not PII by itself.
MODEL_LABEL_MAP = {
    "PER": "PERSON", "PERSON": "PERSON", "PER_NAME": "PERSON",
    "LOC": "ADDRESS", "LOCATION": "ADDRESS", "ADDRESS": "ADDRESS",
    **{t: t for t in ENTITY_TYPES},
}
# Generic NER location tags are coarse (a city is not a street address), so they are down-weighted.
LABEL_SCORE_FACTOR = {"LOC": 0.6, "LOCATION": 0.6}


@dataclass
class Detection:
    entity_type: str
    start: int
    end: int
    score: float
    method: str  # rule | transformer | label-rule | llm-mock
    detector: str  # e.g. "rule:PAN", "transformer:Davlan/xlm-roberta-base-ner-hrl"
    provenance: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.entity_type not in ENTITY_SET:
            raise ValueError(f"unknown entity type {self.entity_type!r}")
        if not (0 <= self.start < self.end):
            raise ValueError("invalid span")
        self.score = max(0.0, min(1.0, float(self.score)))

    def record(self) -> dict:
        """Snapshot of the original prediction, preserved through conflict resolution."""
        return {
            "detector": self.detector, "method": self.method, "type": self.entity_type,
            "start": self.start, "end": self.end, "score": round(self.score, 4),
        }


def mask_value(entity_type: str, value: str) -> str:
    """Display-safe rendering. Never returns the full value."""
    v = value.strip()
    if not v:
        return "***"
    if entity_type == "EMAIL" and "@" in v:
        local, _, domain = v.partition("@")
        return f"{local[:1]}***@{domain}"
    if entity_type == "PERSON":
        return " ".join(w[:1] + "*" * min(max(len(w) - 1, 2), 6) for w in v.split())
    digits = [c for c in v if c.isalnum()]
    if entity_type in {"AADHAAR", "PHONE", "BANK_ACCOUNT", "PAN", "PASSPORT", "VOTER_ID",
                       "DRIVING_LICENSE", "VEHICLE_REG", "CUSTOMER_ID", "EMPLOYEE_ID", "OTHER_IDENTIFIER"}:
        keep = 4 if len(digits) >= 8 else 2 if len(digits) >= 5 else 0
        return "*" * min(max(len(digits) - keep, 3), 12) + ("".join(digits[-keep:]) if keep else "")
    return v[:1] + "*" * min(max(len(v) - 1, 3), 10)
