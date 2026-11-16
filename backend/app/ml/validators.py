"""Format validators used by the rule detector.

Only publicly documented structures are implemented. A passing validator means "well-formed",
never "genuine/issued": no registry lookups are possible here.
"""
import re
from datetime import date

# --- Verhoeff checksum (public-domain algorithm; Aadhaar numbers carry a Verhoeff check digit)
_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5), (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7), (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3), (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4), (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7), (9, 4, 5, 3, 1, 2, 6, 8, 7, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
_INV = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def verhoeff_valid(number: str) -> bool:
    if not number.isdigit():
        return False
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def verhoeff_check_digit(number: str) -> str:
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _D[c][_P[(i + 1) % 8][int(ch)]]
    return str(_INV[c])


def aadhaar_format_ok(digits: str) -> bool:
    """12 digits, first digit 2-9 (UIDAI does not issue numbers starting with 0 or 1)."""
    return len(digits) == 12 and digits.isdigit() and digits[0] in "23456789"


# --- PAN: AAAAA9999A; 4th letter encodes the holder type (Income Tax Department structure).
PAN_HOLDER_TYPES = frozenset("ABCFGHLJPTK")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def pan_format_ok(pan: str) -> bool:
    return bool(PAN_RE.match(pan))


def pan_holder_type_ok(pan: str) -> bool:
    return pan_format_ok(pan) and pan[3] in PAN_HOLDER_TYPES


# --- Indian vehicle registration: state/UT code + RTO digits + series + number
RTO_STATE_CODES = frozenset(
    "AN AP AR AS BR CG CH DD DL DN GA GJ HP HR JH JK KA KL LA LD MH ML MN MP MZ NL OD OR PB PY RJ SK TN TR TS UK UP WB".split()
)


def vehicle_state_ok(code: str) -> bool:
    return code in RTO_STATE_CODES


# --- Dates
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTHS["sept"] = 9


def month_number(name: str) -> int | None:
    return _MONTHS.get(name.lower().rstrip("."))


def valid_birth_date(year: int, month: int, day: int, today: date | None = None) -> bool:
    today = today or date.today()
    try:
        d = date(year, month, day)
    except ValueError:
        return False
    return date(1900, 1, 1) <= d <= today
