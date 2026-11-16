import pymupdf
import pytest

from app.document_processing.extractors import extract
from app.ml import pipeline
from app.ml.taxonomy import Detection
from tests import samples

FULL = (
    samples.SAMPLE_TEXT
    + "Customer ID: CUST-DEMO-001\nDate of Birth: 15 March 2001\n"
    + "Address: 24 Lake View Road, Bhubaneswar, Odisha\nPAN-like test value: ABCDE1234F\n"
    + "Aadhaar-like test value: 4521 7834 9123\n"
)


def types_of(out):
    return {e.detection.entity_type for e in out.entities}


def test_rules_only_mode_on_demo_document():
    out = pipeline.run_detection(FULL, None)
    assert out.mode == "rules-only" and out.version == "rules-1.0"
    assert {"PERSON", "EMAIL", "PHONE", "CUSTOMER_ID", "DATE_OF_BIRTH", "ADDRESS", "PAN", "AADHAAR"} <= types_of(out)
    for e in out.entities:
        d = e.detection
        assert FULL[d.start : d.end] == e.value
        assert e.value not in e.masked and d.provenance
    ordered = [(e.detection.start, e.detection.end) for e in out.entities]
    assert ordered == sorted(ordered) and all(a[1] <= b[0] for a, b in zip(ordered, ordered[1:]))


def test_no_pii_document():
    assert pipeline.run_detection("Quarterly revenue grew 12% across all regions.", None).entities == []


def test_invalid_identifiers_are_not_reported_as_valid():
    out = pipeline.run_detection("Ref 12345678 and code ABCDE1234 and 1234 5678", None)
    assert out.entities == []


def test_repeated_entities_all_found():
    text = "Mail a@example.com or a@example.com. Also b@example.com"
    emails = [e for e in pipeline.run_detection(text, None).entities if e.detection.entity_type == "EMAIL"]
    assert len(emails) == 3


def test_ocr_like_noise_still_detected():
    text = "Email : riya.sharma@example.com\nPhone:+919876543210\nPAN:ABCPD1234F"
    got = types_of(pipeline.run_detection(text, None))
    assert {"EMAIL", "PHONE", "PAN"} <= got


def test_entities_located_on_pdf_pages():
    page_map = extract("pdf", samples.make_pdf()).page_map()
    text = extract("pdf", samples.make_pdf()).text
    out = pipeline.run_detection(text, page_map)
    email = next(e for e in out.entities if e.detection.entity_type == "EMAIL")
    assert email.page == 1 and email.bbox
    page, x0, y0, x1, y1 = email.bbox[0]
    assert page == 1 and 0 < x0 < x1 and 0 < y0 < y1


def test_located_box_actually_contains_the_text_on_the_page():
    """Cross-check the bbox against the PDF itself, not just against our own segments."""
    data = samples.make_pdf()
    res = extract("pdf", data)
    out = pipeline.run_detection(res.text, res.page_map())
    email = next(e for e in out.entities if e.detection.entity_type == "EMAIL")
    _, x0, y0, x1, y1 = email.bbox[0]
    doc = pymupdf.open(stream=data, filetype="pdf")
    inside = doc[0].get_text("text", clip=pymupdf.Rect(x0 - 1, y0 - 1, x1 + 1, y1 + 1))
    assert "riya.sharma@example.com" in inside.replace("\n", "")


class FakeNer:
    model_name = "fake/ner"
    version = "transformer:fake/ner"

    def __init__(self, found):
        self._found = found

    def detect(self, text, deadline=None):
        return [Detection("PERSON", s, e, sc, "transformer", self.version) for s, e, sc in self._found]


def test_hybrid_mode_merges_model_and_rule_results(monkeypatch):
    text = "Customer Name: Riya Sharma\nNotes: met Anita Rao yesterday\n"
    a = text.index("Anita Rao")
    r = text.index("Riya Sharma")
    fake = FakeNer([(r, r + 11, 0.97), (a, a + 9, 0.88)])
    monkeypatch.setattr(pipeline, "get_transformer_detector", lambda: fake)
    out = pipeline.run_detection(text, None)
    assert out.mode == "hybrid" and out.version.endswith("+transformer:fake/ner")
    people = {e.value: e for e in out.entities if e.detection.entity_type == "PERSON"}
    assert set(people) == {"Riya Sharma", "Anita Rao"}  # NER found one the rules could not
    both = people["Riya Sharma"].detection
    assert both.method == "hybrid" and both.score == 0.97 and len(both.provenance) == 2
    assert out.config["ner_model"] == "fake/ner"
    assert out.config["candidates"]["transformer"] == 2


def test_ner_truncation_is_reported(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "ner_max_chars", 10)
    monkeypatch.setattr(pipeline, "get_transformer_detector", lambda: FakeNer([]))
    assert pipeline.run_detection("x" * 50, None).config["ner_truncated_at_chars"] == 10


def test_ner_configured_but_unavailable_falls_back_and_says_so(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "ner_model", "does/not-exist")
    monkeypatch.setattr(pipeline, "get_transformer_detector", lambda: None)
    out = pipeline.run_detection("Mail a@example.com", None)
    assert out.mode == "rules-only" and "ner_error" in out.config


def test_entity_cap(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "max_entities_per_scan", 2)
    text = " ".join(f"u{i}@example.com" for i in range(5))
    out = pipeline.run_detection(text, None)
    assert len(out.entities) == 2 and out.config["entities_dropped_over_cap"] == 3


@pytest.mark.parametrize("n", [2000])
def test_performance_on_large_text(n):
    import time

    text = FULL * n
    t0 = time.perf_counter()
    out = pipeline.run_detection(text, None)
    assert time.perf_counter() - t0 < 30
    assert len(out.entities) > n
