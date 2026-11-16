"""Writes fictional sample documents to tests/fixtures (no real personal data). Run: python -m scripts.make_fixtures"""
from pathlib import Path

from tests import samples

OUT = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
TEXT = (
    samples.SAMPLE_TEXT
    + "Customer ID: CUST-DEMO-001\nDate of Birth: 15 March 2001\nAddress: 24 Lake View Road, Bhubaneswar, Odisha\n"
    + "PAN-like test value: ABCDE1234F\nAadhaar-like test value: 4521 7834 9123\n"
)

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sample.txt").write_text(TEXT, encoding="utf-8")
    (OUT / "sample.pdf").write_bytes(samples.make_pdf(TEXT))
    (OUT / "sample.docx").write_bytes(samples.make_docx(TEXT))
    (OUT / "no_pii.txt").write_text("The quarterly report shows revenue grew steadily across all regions.\n", encoding="utf-8")
    (OUT / "invalid_identifiers.txt").write_text("Order 12345678 ref ABCDE1234 batch 1234 5678 serial 0000 0000 0000\n", encoding="utf-8")
    (OUT / "corrupt.pdf").write_bytes(b"%PDF-1.4\n" + b"this is not a real pdf body " * 10)
    (OUT / "malware.exe").write_bytes(b"MZ\x90\x00 not a real executable")
    print("wrote", sorted(p.name for p in OUT.iterdir()))
