import io
import time

import pymupdf
import pytest
from PIL import Image

from app.document_processing import extractors
from app.document_processing.extractors import extract
from app.document_processing.ocr import OcrLine, ocr_available, recognize_with_deadline
from app.document_processing.types import Deadline, ExtractionError
from tests.test_extraction import EMAIL, _text_image

needs_ocr = pytest.mark.skipif(not ocr_available(), reason="OCR engine unavailable")


class SlowEngine:
    name = "slow"

    def recognize(self, image):
        time.sleep(2)
        return []


class FixedEngine:
    name = "fixed"

    def recognize(self, image):
        return [OcrLine("Phone: 98765 43210  ok", (10, 20, 230, 40), 0.9)]


def test_ocr_hard_timeout_does_not_hang():
    t0 = time.monotonic()
    with pytest.raises(ExtractionError, match="time limit"):
        recognize_with_deadline(SlowEngine(), Image.new("RGB", (10, 10)), Deadline(0.3))
    assert time.monotonic() - t0 < 1.5  # returned long before the 2s OCR job finished
    time.sleep(2)  # let the abandoned job drain so it cannot delay later tests


def test_ocr_words_get_proportional_boxes(monkeypatch):
    monkeypatch.setattr(extractors, "get_ocr_engine", lambda: FixedEngine())
    buf = io.BytesIO()
    Image.new("RGB", (300, 60), "white").save(buf, "PNG")
    r = extract("png", buf.getvalue())
    words = [s for s in r.segments if s.kind == "ocr_word"]
    assert [r.text[s.start : s.end] for s in words] == ["Phone:", "98765", "43210", "ok"]
    assert all(s.bbox[1] == 20 and s.bbox[3] == 40 for s in words)
    xs = [s.bbox for s in words]
    assert xs[0][0] == 10 and xs[-1][2] <= 230
    assert all(a[2] <= b[0] + 1e-6 for a, b in zip(xs, xs[1:]))  # monotonic, non-overlapping
    n = len("Phone: 98765 43210  ok")
    assert abs(xs[1][0] - (10 + 220 * 7 / n)) < 1e-6  # character 7 starts the second word


@needs_ocr
def test_ocr_entity_box_covers_the_matched_words_in_image():
    from app.ml.pipeline import run_detection

    res = extract("png", _text_image([f"Email: {EMAIL}"], size=(1100, 120), font_size=44))
    out = run_detection(res.text, res.page_map())
    email = next(e for e in out.entities if e.detection.entity_type == "EMAIL")
    _, x0, y0, x1, y1 = email.bbox[0]
    label, line_end = res.segments[0].bbox, max(sg.bbox[2] for sg in res.segments)
    assert x0 >= label[2] - 1 and x1 <= line_end + 1 and y1 > y0  # starts after the "Email:" label, inside the line


def test_rotated_pdf_pages_extract_and_record_rotation():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Contact riya.sharma@example.com about the account")
    page.set_rotation(90)
    r = extract("pdf", doc.tobytes())
    assert EMAIL in r.text and r.pages[0].rotation == 90
    seg = next(s for s in r.segments if r.text[s.start : s.end] == EMAIL)
    assert r.pages[0].width == pytest.approx(842, abs=1) and 0 <= seg.bbox[2] <= 595 and 0 <= seg.bbox[3] <= 842  # unrotated space
    for deg in (0, 180, 270):
        page.set_rotation(deg)
        assert EMAIL in extract("pdf", doc.tobytes()).text


def test_rotated_page_bbox_matches_the_page_content():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Contact riya.sharma@example.com now")
    page.set_rotation(90)
    data = doc.tobytes()
    r = extract("pdf", data)
    seg = next(s for s in r.segments if r.text[s.start : s.end] == EMAIL)
    reopened = pymupdf.open(stream=data, filetype="pdf")[0]
    clip = pymupdf.Rect(*seg.bbox) + (-1, -1, 1, 1)
    assert EMAIL in reopened.get_text("text", clip=clip).replace("\n", "")
