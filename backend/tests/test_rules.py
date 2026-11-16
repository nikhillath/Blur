import pytest

from app.ml import validators as v
from app.ml.rules import detect_rules
from app.ml.taxonomy import Detection, mask_value


def found(text, etype=None):
    return [d for d in detect_rules(text) if etype is None or d.entity_type == etype]


def values(text, etype):
    return sorted(text[d.start : d.end] for d in found(text, etype))


def make_aadhaar(first11: str) -> str:
    return first11 + v.verhoeff_check_digit(first11)


# ---------------------------------------------------------------- checksum
def test_verhoeff_known_vector():
    assert v.verhoeff_check_digit("236") == "3"  # published worked example
    assert v.verhoeff_valid("2363")
    assert not v.verhoeff_valid("2364")
    assert not v.verhoeff_valid("23a3") and not v.verhoeff_valid("")


def test_verhoeff_detects_all_single_digit_errors():
    good = make_aadhaar("40123456789")
    assert v.verhoeff_valid(good)
    for i in range(len(good)):
        for d in "0123456789":
            if d != good[i]:
                assert not v.verhoeff_valid(good[:i] + d + good[i + 1 :])


# ---------------------------------------------------------------- email
@pytest.mark.parametrize("text,expected", [
    ("Mail riya.sharma@example.com now", "riya.sharma@example.com"),
    ("a.b+tag@sub.example.co.in.", "a.b+tag@sub.example.co.in"),
    ("(x_y@ex-ample.org)", "x_y@ex-ample.org"),
])
def test_email_positive(text, expected):
    assert values(text, "EMAIL") == [expected]


@pytest.mark.parametrize("text", ["user@", "@example.com", "a@b", "no email here", "a@@b.com", "a@b..com"])
def test_email_negative(text):
    assert found(text, "EMAIL") == []


# ---------------------------------------------------------------- phone
@pytest.mark.parametrize("text,expected", [
    ("Phone: +91 98765 43210", "+91 98765 43210"),
    ("call 98765-43210 today", "98765-43210"),
    ("Mobile 09876543210", "09876543210"),
    ("Phone:+919876543210", "+919876543210"),  # OCR dropped the space after the colon
    ("tel 9876543210.", "9876543210"),
])
def test_phone_positive(text, expected):
    assert values(text, "PHONE") == [expected]


@pytest.mark.parametrize("text", [
    "1234567890", "98765432101", "5876543210", "order 12345 67890", "4521 7834 9123", "2026-10-22",
    "987654321",
])
def test_phone_negative(text):
    assert found(text, "PHONE") == []


def test_phone_context_raises_score():
    bare = found("see 9876543210", "PHONE")[0].score
    ctx = found("Mobile: 9876543210", "PHONE")[0].score
    assert ctx > bare


# ---------------------------------------------------------------- aadhaar
def test_aadhaar_valid_checksum_high_score():
    num = make_aadhaar("40123456789")
    grouped = f"{num[:4]} {num[4:8]} {num[8:]}"
    for text in (f"Aadhaar: {grouped}", f"UID {num}"):
        d = found(text, "AADHAAR")[0]
        assert d.score >= 0.95
    assert found(f"ref {num}", "AADHAAR")[0].score == 0.9


def test_aadhaar_demo_value_is_flagged_but_low():
    # The documented demo value has a failing checksum: it must be "Aadhaar-like", not "valid".
    assert not v.verhoeff_valid("452178349123")
    assert found("Aadhaar-like test value: 4521 7834 9123", "AADHAAR")[0].score == 0.7
    assert found("ref 4521 7834 9123", "AADHAAR")[0].score == 0.4  # grouped, no keyword
    assert found("ref 452178349123", "AADHAAR") == []  # bare digits, failed checksum, no keyword


@pytest.mark.parametrize("text", ["Aadhaar 0123 4567 8901", "Aadhaar 1234 5678 9012", "Aadhaar 4521 7834 912",
                                  "Aadhaar 4521 7834 91234"])
def test_aadhaar_negative(text):
    assert found(text, "AADHAAR") == []


# ---------------------------------------------------------------- PAN
def test_pan():
    assert found("PAN: ABCPD1234F", "PAN")[0].score == 0.95
    assert found("ref ABCPD1234F", "PAN")[0].score == 0.85
    assert found("PAN: ABCDE1234F", "PAN")[0].score == 0.7  # 4th letter 'D' is not a known holder type
    assert found("ref ABCDE1234F", "PAN") == []
    for bad in ("ABCPD12345", "abcpd1234f", "ABCP1234FF", "XABCPD1234F", "PAN: ABCPD1234FX"):
        assert found(bad, "PAN") == [], bad


# ---------------------------------------------------------------- other IDs
def test_passport_requires_context():
    assert values("Passport No: K1234567", "PASSPORT") == ["K1234567"]
    assert found("code K1234567", "PASSPORT") == []
    assert found("Passport Q1234567", "PASSPORT") == []  # Q is not used as a series letter


def test_voter_id_requires_context():
    assert values("Voter ID: ABC1234567", "VOTER_ID") == ["ABC1234567"]
    assert found("code ABC1234567", "VOTER_ID") == []


def test_ifsc():
    assert values("IFSC: SBIN0001234", "IFSC") == ["SBIN0001234"]
    assert found("SBIN1001234", "IFSC") == []  # 5th character must be 0
    assert found("SBIN000123", "IFSC") == []


def test_upi_is_not_email():
    assert values("UPI ID: riya@okaxis", "UPI_ID") == ["riya@okaxis"]
    assert found("riya@example.com", "UPI_ID") == []
    assert found("riya@okaxis", "UPI_ID")[0].score < found("UPI: riya@okaxis", "UPI_ID")[0].score


def test_bank_account_requires_context():
    assert values("Account No: 123456789012", "BANK_ACCOUNT") == ["123456789012"]
    assert values("A/c 0123456789", "BANK_ACCOUNT") == ["0123456789"]
    assert found("Order 123456789012", "BANK_ACCOUNT") == []
    assert found("Account 12345678", "BANK_ACCOUNT") == []  # too short


def test_vehicle_registration():
    assert values("Vehicle: MH12AB1234", "VEHICLE_REG") == ["MH12AB1234"]
    assert values("KA-01-AB-1234", "VEHICLE_REG") == ["KA-01-AB-1234"]
    assert found("XX12AB1234", "VEHICLE_REG") == []


def test_driving_license():
    assert values("DL: MH-14-2011-0062821", "DRIVING_LICENSE") == ["MH-14-2011-0062821"]
    assert found("MH-14-1811-0062821", "DRIVING_LICENSE") == []


# ---------------------------------------------------------------- dates of birth
@pytest.mark.parametrize("text,expected", [
    ("DOB: 15/03/2001", "15/03/2001"),
    ("Date of Birth: 15 March 2001", "15 March 2001"),
    ("born March 15, 2001", "March 15, 2001"),
    ("D.O.B 2001-03-15", "2001-03-15"),
    ("Birth date: 15-Mar-2001", "15-Mar-2001"),
])
def test_dob_positive(text, expected):
    assert values(text, "DATE_OF_BIRTH") == [expected]


@pytest.mark.parametrize("text", [
    "Invoice date: 15/03/2001", "DOB: 31/02/2001", "DOB: 15/03/2999", "DOB: 15/15/2001", "15 March 2001",
])
def test_dob_negative(text):
    assert found(text, "DATE_OF_BIRTH") == []


# ---------------------------------------------------------------- labelled fields
def test_business_ids_are_not_government_ids():
    t = "Customer ID: CUST-DEMO-001\nEmployee No: EMP-4421"
    assert values(t, "CUSTOMER_ID") == ["CUST-DEMO-001"]
    assert values(t, "EMPLOYEE_ID") == ["EMP-4421"]
    assert not found(t, "PAN") and not found(t, "AADHAAR")


def test_name_and_address_labels():
    t = "Customer Name: Riya Sharma\nAddress: 24 Lake View Road, Bhubaneswar, Odisha\n"
    assert values(t, "PERSON") == ["Riya Sharma"]
    assert values(t, "ADDRESS") == ["24 Lake View Road, Bhubaneswar, Odisha"]
    assert values("Name:\tRiya Sharma.", "PERSON") == ["Riya Sharma"]


@pytest.mark.parametrize("text", ["File name: report.pdf", "Name: 12345", "Name: a@b.com", "username: bob", "Name: X"])
def test_name_label_negative(text):
    assert found(text, "PERSON") == []


# ---------------------------------------------------------------- general properties
def test_no_pii_text_has_no_detections():
    assert detect_rules("The quarterly report shows revenue grew 12% across all regions in 2024.") == []
    assert detect_rules("") == []


def test_offsets_always_slice_back_to_text_in_noisy_input():
    text = (
        "CUSTOMER RECORD\r\n  Name :  Riya  Sharma \n Phone:+919876543210 | e-mail riya@example.com\n"
        "\tPAN:ABCPD1234F  Aadhaar-like 4521-7834-9123 IFSC SBIN0001234 DOB:15/03/2001\n"
    )
    for d in detect_rules(text):
        assert 0 <= d.start < d.end <= len(text)
        assert text[d.start : d.end] == text[d.start : d.end].strip()
        assert 0 <= d.score <= 1


def test_unicode_text_offsets():
    text = "नाम: राहुल | Email: rahul@example.com | फ़ोन: 9876543210"
    for d in detect_rules(text):
        assert text[d.start : d.end] in ("rahul@example.com", "9876543210")


def test_detection_validation():
    with pytest.raises(ValueError):
        Detection("NOT_A_TYPE", 0, 3, 0.5, "rule", "x")
    with pytest.raises(ValueError):
        Detection("EMAIL", 5, 5, 0.5, "rule", "x")
    assert Detection("EMAIL", 0, 3, 7.0, "rule", "x").score == 1.0


# ---------------------------------------------------------------- masking
@pytest.mark.parametrize("etype,value", [
    ("EMAIL", "riya.sharma@example.com"), ("PHONE", "+91 98765 43210"), ("AADHAAR", "4521 7834 9123"),
    ("PAN", "ABCPD1234F"), ("PERSON", "Riya Sharma"), ("ADDRESS", "24 Lake View Road"), ("IFSC", "SBIN0001234"),
])
def test_mask_never_reveals_full_value(etype, value):
    m = mask_value(etype, value)
    assert m != value and "*" in m
    assert value.replace(" ", "") not in m.replace(" ", "")


def test_mask_keeps_only_last_digits_for_numbers():
    assert mask_value("AADHAAR", "4521 7834 9123").endswith("9123")
    assert "4521" not in mask_value("AADHAAR", "4521 7834 9123")
    assert mask_value("EMAIL", "riya@example.com") == "r***@example.com"
    assert mask_value("PHONE", "") == "***"


# ---------------------------------------------------------------- regressions found by the held-out evaluation
@pytest.mark.parametrize("text", [
    "Order reference: INV-20240218-0366 please resolve", "REF-20230428-8939", "INV-2024-0218-0366", "ref 2024 0218-0366",
])
def test_aadhaar_not_matched_inside_reference_numbers(text):
    assert found(text, "AADHAAR") == []


def test_aadhaar_requires_consistent_separators():
    assert len(found("Aadhaar 4521-7834-9123", "AADHAAR")) == 1
    assert len(found("Aadhaar 4521 7834 9123", "AADHAAR")) == 1
    assert found("Aadhaar 4521 7834-9123", "AADHAAR") == []


def test_upi_skipped_when_text_calls_it_an_email():
    assert found("payment id ke liye email diya tha horrwwsj@bluepeak us par", "UPI_ID") == []
    assert len(found("UPI: horrwwsj@bluepeak", "UPI_ID")) == 1
    assert len(found("pay riya@okaxis now", "UPI_ID")) == 1
