"""Explicit label mappings from each source dataset to the application taxonomy.

Rules (documented in docs/ml-pipeline.md):
* A source label maps to exactly one taxonomy type, or to ``None`` = "not PII for this project".
* ``IGNORE`` source labels are real entities of a kind the taxonomy does not cover (e.g. organisation).
  They are NOT turned into negatives: during evaluation, predictions that overlap them are ignored,
  so a correct-but-out-of-scope prediction is never counted as a false positive.
* Hard-negative labels (PII-shaped values that are not PII) are dropped from the gold entities, so they
  become ``O`` for training and act as false-positive traps for evaluation.
"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.ml.taxonomy import ENTITY_TYPES  # noqa: E402

IGNORE = "__IGNORE__"

MASKARA = {
    "PERSON_NAME": "PERSON", "ADDRESS": "ADDRESS", "DATE_OF_BIRTH": "DATE_OF_BIRTH",
    "DRIVER_LICENSE": "DRIVING_LICENSE", "EMAIL": "EMAIL", "PHONE": "PHONE", "AADHAAR": "AADHAAR",
    "PAN_CARD": "PAN", "PASSPORT": "PASSPORT", "UPI_ID": "UPI_ID", "VEHICLE_REG": "VEHICLE_REG",
    # secrets / financial / network identifiers have no dedicated type in this taxonomy
    "API_KEY": "OTHER_IDENTIFIER", "CREDIT_CARD": "OTHER_IDENTIFIER", "IP_ADDRESS": "OTHER_IDENTIFIER",
    "PASSWORD": "OTHER_IDENTIFIER", "SSN": "OTHER_IDENTIFIER", "USERNAME": "OTHER_IDENTIFIER",
}

INDIAPII = {
    "PERSON_NAME": "PERSON", "VEHICLE_REG": "VEHICLE_REG", "DRIVING_LICENCE": "DRIVING_LICENSE",
    "BANK_ACCOUNT_IN": "BANK_ACCOUNT", "IFSC": "IFSC", "INDIAN_MOBILE": "PHONE", "PAN": "PAN",
    "AADHAAR": "AADHAAR", "AADHAAR_MASKED": "AADHAAR", "INDIAN_ADDRESS": "ADDRESS", "PIN_CODE": "ADDRESS",
    "UPI_VPA": "UPI_ID", "VOTER_ID": "VOTER_ID", "INDIAN_PASSPORT": "PASSPORT",
    "GSTIN": "OTHER_IDENTIFIER", "ABHA_NUMBER": "OTHER_IDENTIFIER", "ABHA_ADDRESS": "OTHER_IDENTIFIER",
    # PII-shaped but NOT PII (value_class == hard_negative): dropped so they stay "O"
    "TIMESTAMP_SHAPED": None, "ORDER_ID_SHAPED": None, "PAN_SHAPED_INVOICE_NO": None,
    "VPA_SHAPED_EMAIL": None, "NON_VERHOEFF_AADHAAR_SHAPED": None,
}

YLEMIS = {
    "person": "PERSON", "address": "ADDRESS", "email": "EMAIL", "indian_phone": "PHONE", "pan": "PAN",
    "aadhaar": "AADHAAR", "ifsc": "IFSC", "upi_id": "UPI_ID", "voter_id": "VOTER_ID",
    "vehicle_registration": "VEHICLE_REG", "indian_passport": "PASSPORT",
    # POST-HOC CORRECTION (disclosed in docs/ml-pipeline.md): the first evaluation mapped account_id to BANK_ACCOUNT
    # and scored 0%. Inspecting false negatives showed account_id values are alphanumeric internal ids such as
    # "policy_5628203_824" / "cust_1795603_166", not bank account numbers, so the mapping was semantically wrong.
    "account_id": "OTHER_IDENTIFIER", "gstin": "OTHER_IDENTIFIER", "medical_id": "OTHER_IDENTIFIER",
    "location": IGNORE, "organization": IGNORE,
}

SOURCE_MAPS = {"maskara": MASKARA, "indiapii": INDIAPII, "ylemis": YLEMIS}

# Model label inventory: BIO over the full taxonomy (stable ordering => stable label ids).
LABELS = ["O"] + [f"{p}-{t}" for t in ENTITY_TYPES for p in ("B", "I")]
LABEL2ID = {l: i for i, l in enumerate(LABELS)}
ID2LABEL = {i: l for l, i in LABEL2ID.items()}


def map_label(source: str, label: str) -> str | None:
    """Taxonomy type, IGNORE, or None (drop). Unknown labels raise: never guess a mapping."""
    table = SOURCE_MAPS[source]
    if label not in table:
        raise KeyError(f"{source}: unmapped label {label!r}")
    return table[label]
