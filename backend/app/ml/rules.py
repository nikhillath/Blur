"""Deterministic detector for structured identifiers.

A regex match is only a *candidate*. Each rule validates structure, looks at nearby context
keywords, and assigns a heuristic score. Scores express rule strength and are NOT calibrated
probabilities. Anything weak is still emitted (with a low score) so a human can decide.
"""
import re
from collections.abc import Callable, Iterator

from app.ml import validators as v
from app.ml.taxonomy import Detection

RULES_VERSION = "rules-1.0"
CONTEXT_WINDOW = 40


def _kw(*words: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(words) + r")(?![A-Za-z0-9])", re.IGNORECASE)


CTX = {
    "AADHAAR": _kw("aadhaar", "aadhar", "adhaar", "uidai", "uid", "आधार"),
    "PAN": _kw("pan", "permanent account(?: number)?"),
    "PASSPORT": _kw("passport"),
    "VOTER_ID": _kw("voter", "epic", "election"),
    "DRIVING_LICENSE": _kw("driving", "licen[cs]e", "d\\.?l\\.?\\s*(?:no|number)"),
    "PHONE": _kw("phone", "mobile", "mob", "tel", "telephone", "contact", "cell", "whatsapp", "call"),
    "BANK_ACCOUNT": _kw("account", "a/c", "acct", "acc\\.?\\s*no", "bank"),
    "UPI_ID": _kw("upi", "vpa"),
    "IFSC": _kw("ifsc"),
    "EMAIL_WORD": _kw("e-?mail", "mail"),
    "VEHICLE_REG": _kw("vehicle", "registration", "reg\\.?\\s*no", "rc\\s*no", "number plate"),
    "DOB": _kw("dob", "d\\.o\\.b\\.?", "date of birth", "birth\\s*date", "birthday", "born", "जन्म"),
}


def _context(text: str, start: int) -> str:
    """Text shortly before ``start`` on the same line."""
    window = text[max(0, start - CONTEXT_WINDOW) : start]
    return window.rsplit("\n", 1)[-1]


def has_ctx(text: str, start: int, key: str) -> bool:
    return bool(CTX[key].search(_context(text, start)))


def _det(etype: str, start: int, end: int, score: float, method: str = "rule") -> Detection:
    return Detection(etype, start, end, score, method, f"rule:{etype}")


# ------------------------------------------------------------------ structured rules
EMAIL_RE = re.compile(
    r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])"
)


def rule_email(text: str) -> Iterator[Detection]:
    for m in EMAIL_RE.finditer(text):
        yield _det("EMAIL", m.start(), m.end(), 0.97)


PHONE_RE = re.compile(
    r"(?<!\d)(?<!\d[ -])((?:\+|00)?91[ -]?|0)?[6-9]\d{4}[ -]?\d{5}(?![ -]?\d)"
)


def rule_phone(text: str) -> Iterator[Detection]:
    for m in PHONE_RE.finditer(text):
        prefix = m.group(1)
        if has_ctx(text, m.start(), "PHONE") or (prefix and prefix.strip("- ").lstrip("+0") in {"91"}):
            score = 0.92
        elif prefix:
            score = 0.7
        else:
            score = 0.55
        yield _det("PHONE", m.start(), m.end(), score)


AADHAAR_RE = re.compile(r"(?<!\d)(?<!\d[ -])(?<![A-Za-z]-)([2-9]\d{3})([ -]?)(\d{4})\2(\d{4})(?![ -]?\d)")


def rule_aadhaar(text: str) -> Iterator[Detection]:
    for m in AADHAAR_RE.finditer(text):
        digits = m.group(1) + m.group(3) + m.group(4)
        if not v.aadhaar_format_ok(digits):
            continue
        ctx = has_ctx(text, m.start(), "AADHAAR")
        grouped = bool(m.group(2))
        if v.verhoeff_valid(digits):
            score = 0.97 if ctx else 0.9
        elif ctx:
            score = 0.7  # right shape + keyword, but checksum fails: "Aadhaar-like"
        elif grouped:
            score = 0.4
        else:
            continue  # bare 12 digits with a failed checksum and no context: too ambiguous
        yield _det("AADHAAR", m.start(), m.end(), score)


PAN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{5}[0-9]{4}[A-Z](?![A-Za-z0-9])")


def rule_pan(text: str) -> Iterator[Detection]:
    for m in PAN_RE.finditer(text):
        ctx = has_ctx(text, m.start(), "PAN")
        if v.pan_holder_type_ok(m.group()):
            score = 0.95 if ctx else 0.85
        elif ctx:
            score = 0.7  # PAN-shaped but the holder-type letter is not a known one
        else:
            continue
        yield _det("PAN", m.start(), m.end(), score)


PASSPORT_RE = re.compile(r"(?<![A-Za-z0-9])[A-PR-WY][1-9]\d\s?\d{4}[1-9](?![A-Za-z0-9])")


def rule_passport(text: str) -> Iterator[Detection]:
    for m in PASSPORT_RE.finditer(text):
        if has_ctx(text, m.start(), "PASSPORT"):  # shape alone is far too common
            yield _det("PASSPORT", m.start(), m.end(), 0.85)


VOTER_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{3}\d{7}(?![A-Za-z0-9])")


def rule_voter_id(text: str) -> Iterator[Detection]:
    for m in VOTER_RE.finditer(text):
        if has_ctx(text, m.start(), "VOTER_ID"):
            yield _det("VOTER_ID", m.start(), m.end(), 0.85)


DL_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}[ -]?\d{2}[ -]?(?:19|20)\d{2}[ -]?\d{7}(?![A-Za-z0-9])")


def rule_driving_license(text: str) -> Iterator[Detection]:
    for m in DL_RE.finditer(text):
        yield _det("DRIVING_LICENSE", m.start(), m.end(), 0.9 if has_ctx(text, m.start(), "DRIVING_LICENSE") else 0.7)


IFSC_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{4}0[A-Z0-9]{6}(?![A-Za-z0-9])")


def rule_ifsc(text: str) -> Iterator[Detection]:
    for m in IFSC_RE.finditer(text):
        yield _det("IFSC", m.start(), m.end(), 0.95 if has_ctx(text, m.start(), "IFSC") else 0.8)


UPI_RE = re.compile(r"(?<![\w.@-])[A-Za-z0-9._-]{2,64}@[A-Za-z]{2,25}(?![\w@-]|\.[A-Za-z0-9])")


def rule_upi(text: str) -> Iterator[Detection]:
    for m in UPI_RE.finditer(text):
        if has_ctx(text, m.start(), "EMAIL_WORD") and not has_ctx(text, m.start(), "UPI_ID"):
            continue
        yield _det("UPI_ID", m.start(), m.end(), 0.85 if has_ctx(text, m.start(), "UPI_ID") else 0.5)


BANK_RE = re.compile(r"(?<!\d)(?<!\d[ -])\d{9,18}(?![ -]?\d)")


def rule_bank_account(text: str) -> Iterator[Detection]:
    for m in BANK_RE.finditer(text):
        if has_ctx(text, m.start(), "BANK_ACCOUNT"):  # a long number alone is not an account number
            yield _det("BANK_ACCOUNT", m.start(), m.end(), 0.8)


VEHICLE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2})[ -]?(\d{2})[ -]?[A-Z]{1,3}[ -]?\d{4}(?![A-Za-z0-9])")


def rule_vehicle(text: str) -> Iterator[Detection]:
    for m in VEHICLE_RE.finditer(text):
        if v.vehicle_state_ok(m.group(1)):
            yield _det("VEHICLE_REG", m.start(), m.end(), 0.9 if has_ctx(text, m.start(), "VEHICLE_REG") else 0.7)


# ------------------------------------------------------------------------ dates of birth
def _month(name: str) -> int | None:
    return v.month_number(name)


def _dmy(m: re.Match) -> tuple[int, int, int] | None:
    a, b, y = int(m[1]), int(m[2]), int(m[3])
    for d, mo in ((a, b), (b, a)):  # prefer day-first (Indian convention)
        if v.valid_birth_date(y, mo, d):
            return y, mo, d
    return None


def _d_mon_y(m: re.Match) -> tuple[int, int, int] | None:
    mo = _month(m[2])
    return (int(m[3]), mo, int(m[1])) if mo and v.valid_birth_date(int(m[3]), mo, int(m[1])) else None


def _mon_d_y(m: re.Match) -> tuple[int, int, int] | None:
    mo = _month(m[1])
    return (int(m[3]), mo, int(m[2])) if mo and v.valid_birth_date(int(m[3]), mo, int(m[2])) else None


def _iso(m: re.Match) -> tuple[int, int, int] | None:
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    return (y, mo, d) if v.valid_birth_date(y, mo, d) else None


_DATE_PATTERNS: list[tuple[re.Pattern, Callable[[re.Match], tuple[int, int, int] | None]]] = [
    (re.compile(r"(?<!\d)(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})(?!\d)"), _dmy),
    (re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?[ \-]+([A-Za-z]{3,9})\.?,?[ \-]+(\d{4})(?!\d)"), _d_mon_y),
    (re.compile(r"(?<![A-Za-z])([A-Za-z]{3,9})\.?[ \-]+(\d{1,2})(?:st|nd|rd|th)?,?[ \-]+(\d{4})(?!\d)"), _mon_d_y),
    (re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)"), _iso),
]


def rule_dob(text: str) -> Iterator[Detection]:
    """Dates are only PII as a birth date, so a DOB keyword must precede the date."""
    seen: set[tuple[int, int]] = set()
    for pattern, parse in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            if (m.start(), m.end()) in seen or parse(m) is None:
                continue
            if has_ctx(text, m.start(), "DOB"):
                seen.add((m.start(), m.end()))
                yield _det("DATE_OF_BIRTH", m.start(), m.end(), 0.92)


# ------------------------------------------------------------- labelled business identifiers
_VALUE = r"[A-Za-z0-9][A-Za-z0-9\-_/]{2,30}"
CUSTOMER_RE = re.compile(
    rf"(?<![A-Za-z])(?:customer|cust|client)\.?[ \t]*(?:id|no|number|#)[ \t]*[:#\-]?[ \t]*({_VALUE})", re.IGNORECASE
)
EMPLOYEE_RE = re.compile(
    rf"(?<![A-Za-z])(?:employee|emp|staff)\.?[ \t]*(?:id|no|number|code)[ \t]*[:#\-]?[ \t]*({_VALUE})", re.IGNORECASE
)


def rule_customer_id(text: str) -> Iterator[Detection]:
    for m in CUSTOMER_RE.finditer(text):
        yield _det("CUSTOMER_ID", m.start(1), m.end(1), 0.85, "label-rule")


def rule_employee_id(text: str) -> Iterator[Detection]:
    for m in EMPLOYEE_RE.finditer(text):
        yield _det("EMPLOYEE_ID", m.start(1), m.end(1), 0.85, "label-rule")


# ----------------------------------------------------- labelled free-text fields (names/addresses)
NAME_RE = re.compile(
    r"(?im)^[ \t]*(?:(?:customer|full|applicant|patient|employee|account holder|beneficiary|"
    r"father'?s|mother'?s|nominee|cardholder|holder)[ \t]+)?name[ \t]*(?::|-|\t)[ \t]*([^\n]+?)[ \t]*$"
)
ADDRESS_RE = re.compile(
    r"(?im)^[ \t]*(?:(?:residential|permanent|current|postal|mailing|billing|home|office|correspondence)[ \t]+)?"
    r"address[ \t]*(?::|-|\t)[ \t]*([^\n]+?)[ \t]*$"
)


def _clean_span(text: str, m: re.Match, group: int = 1) -> tuple[int, int]:
    start, end = m.start(group), m.end(group)
    while end > start and text[end - 1] in ".,;:":
        end -= 1
    return start, end


def rule_name_label(text: str) -> Iterator[Detection]:
    for m in NAME_RE.finditer(text):
        s, e = _clean_span(text, m)
        value = text[s:e]
        if 2 <= len(value) <= 60 and re.search(r"[^\W\d_]{2}", value) and "@" not in value and not re.search(r"\d{3,}", value):
            yield _det("PERSON", s, e, 0.7, "label-rule")


def rule_address_label(text: str) -> Iterator[Detection]:
    for m in ADDRESS_RE.finditer(text):
        s, e = _clean_span(text, m)
        if 8 <= e - s <= 200:
            yield _det("ADDRESS", s, e, 0.7, "label-rule")


ALL_RULES: tuple[Callable[[str], Iterator[Detection]], ...] = (
    rule_email, rule_phone, rule_aadhaar, rule_pan, rule_passport, rule_voter_id, rule_driving_license,
    rule_ifsc, rule_upi, rule_bank_account, rule_vehicle, rule_dob, rule_customer_id, rule_employee_id,
    rule_name_label, rule_address_label,
)


def detect_rules(text: str) -> list[Detection]:
    out: list[Detection] = []
    for rule in ALL_RULES:
        out.extend(rule(text))
    return out

