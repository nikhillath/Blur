import io
import random

import pymupdf
import pytest
from docx import Document as DocxDocument
from PIL import Image, ImageDraw, ImageFont

from app.core.config import get_settings
from app.document_processing import extractors
from app.document_processing.extractors import extract
from app.document_processing.ocr import ocr_available
from app.document_processing.types import Deadline, ExtractionError
from tests import samples

needs_ocr = pytest.mark.skipif(not ocr_available(), reason="OCR engine unavailable")

EMAIL = "riya.sharma@example.com"


def slice_ok(result, needle):
    """Offset contract: text[start:end] must equal the matched string."""
    i = result.text.index(needle)
    assert result.text[i : i + len(needle)] == needle
    return i


# ---------------------------------------------------------------- TXT
def test_txt_newline_normalisation():
    r = extract("txt", b"line1\r\nline2\rline3\n")
    assert r.text == "line1\nline2\nline3\n"
    assert r.pages[0].end == len(r.text)


def test_txt_bom_and_unicode():
    assert extract("txt", "﻿hello".encode("utf-8")).text == "hello"
    assert "नमस्ते" in extract("txt", "नमस्ते Riya".encode("utf-8")).text


@pytest.mark.parametrize("blob", [b"", b"   \n\t  \n"])
def test_txt_empty_or_blank_is_failure_not_silent(blob):
    with pytest.raises(ExtractionError):
        extract("txt", blob)


# ---------------------------------------------------------------- PDF
def test_text_pdf_extracts_text_with_word_boxes():
    r = extract("pdf", samples.make_pdf())
    assert EMAIL in r.text and "98765 43210" in r.text
    assert r.coord_space == "pdf_points" and r.pages[0].method == "text"
    i = slice_ok(r, EMAIL)
    seg = [s for s in r.segments if s.start <= i < s.end][0]
    assert seg.page == 1 and seg.bbox is not None
    x0, y0, x1, y1 = seg.bbox
    assert 0 < x0 < x1 and 0 < y0 < y1
    assert r.text[seg.start : seg.end] == EMAIL


def test_pdf_segments_are_consistent_with_text():
    r = extract("pdf", samples.make_pdf())
    for s in r.segments:
        assert 0 <= s.start < s.end <= len(r.text)
        assert r.text[s.start : s.end].strip() == r.text[s.start : s.end]


def test_multipage_pdf_page_ranges():
    doc = pymupdf.open()
    for t in ("Page one Alpha", "Page two Beta contact beta@example.com"):
        doc.new_page().insert_text((72, 72), t)
    r = extract("pdf", doc.tobytes())
    assert [p.page for p in r.pages] == [1, 2]
    assert "Alpha" in r.text[r.pages[0].start : r.pages[0].end]
    assert "Beta" in r.text[r.pages[1].start : r.pages[1].end]
    assert r.quality["page_count"] == 2


def test_pdf_page_limit(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_pdf_pages", 1)
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page().insert_text((72, 72), "some text on the page here")
    with pytest.raises(ExtractionError, match="page limit"):
        extract("pdf", doc.tobytes())


def test_encrypted_pdf_rejected():
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret secret secret secret")
    blob = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    with pytest.raises(ExtractionError, match="password"):
        extract("pdf", blob)


def test_corrupt_pdf_rejected():
    with pytest.raises(ExtractionError):
        extract("pdf", b"%PDF-1.4\ngarbage that is not a pdf")


def test_blank_pdf_without_text_is_failure(monkeypatch):
    def no_ocr():
        raise ExtractionError("OCR engine is not available")

    monkeypatch.setattr(extractors, "get_ocr_engine", no_ocr)
    doc = pymupdf.open()
    doc.new_page()
    with pytest.raises(ExtractionError, match="no extractable text"):
        extract("pdf", doc.tobytes())


def test_deadline_enforced():
    with pytest.raises(ExtractionError, match="time limit"):
        extract("pdf", samples.make_pdf(), Deadline(-1))


# ---------------------------------------------------------------- DOCX
def test_docx_paragraphs_tables_headers_footers():
    d = DocxDocument()
    d.add_paragraph("Customer Name: Riya Sharma")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "Email"
    t.cell(0, 1).text = EMAIL
    t.cell(1, 0).text = "Phone"
    t.cell(1, 1).text = "+91 98765 43210"
    d.sections[0].header.paragraphs[0].text = "HEADER-ONLY-TOKEN"
    d.sections[0].footer.paragraphs[0].text = "FOOTER-ONLY-TOKEN"
    buf = io.BytesIO()
    d.save(buf)
    r = extract("docx", buf.getvalue())
    for needle in ("Riya Sharma", EMAIL, "+91 98765 43210", "HEADER-ONLY-TOKEN", "FOOTER-ONLY-TOKEN"):
        assert needle in r.text, needle
    assert f"Email\t{EMAIL}" in r.text  # cells tab-separated, rows newline-separated
    i = slice_ok(r, EMAIL)
    seg = [s for s in r.segments if s.start <= i < s.end][0]
    assert seg.ref.startswith("body.t.0.r.0.c.1") and r.text[seg.start : seg.end] == EMAIL


def test_docx_order_and_merged_cells():
    d = DocxDocument()
    d.add_paragraph("BEFORE")
    t = d.add_table(rows=1, cols=2)
    t.cell(0, 0).merge(t.cell(0, 1)).text = "MERGED"
    d.add_paragraph("AFTER")
    buf = io.BytesIO()
    d.save(buf)
    text = extract("docx", buf.getvalue()).text
    assert text.index("BEFORE") < text.index("MERGED") < text.index("AFTER")
    assert text.count("MERGED") == 1


def test_docx_corrupt_rejected():
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<broken")
        z.writestr("[Content_Types].xml", "<x/>")
    with pytest.raises(ExtractionError):
        extract("docx", buf.getvalue())


# ---------------------------------------------------------------- OCR
def _text_image(lines, size=(900, 220), font_size=36, rotate=0, noise=False):
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except OSError:
        font = ImageFont.load_default(size=font_size)
    for i, line in enumerate(lines):
        draw.text((20, 20 + i * (font_size + 16)), line, fill="black", font=font)
    if noise:
        rnd = random.Random(1)
        for _ in range(400):
            draw.point((rnd.randrange(size[0]), rnd.randrange(size[1])), fill=(120, 120, 120))
    if rotate:
        img = img.rotate(rotate, expand=True, fillcolor="white")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@needs_ocr
def test_image_ocr_recovers_text_with_boxes():
    r = extract("png", _text_image(["Customer Name: Riya Sharma", f"Email: {EMAIL}"]))
    assert r.coord_space == "image_pixels" and r.pages[0].method == "ocr"
    assert "Riya" in r.text and "example.com" in r.text
    assert r.quality["mean_ocr_confidence"] > 0.6
    for s in r.segments:
        assert s.bbox and s.bbox[2] > s.bbox[0] and s.bbox[3] > s.bbox[1]
        assert 0 <= s.bbox[0] and s.bbox[2] <= 905


@needs_ocr
def test_image_ocr_noisy_and_small():
    r = extract("png", _text_image(["Phone: 9876543210"], size=(500, 80), font_size=20, noise=True))
    assert "9876543210" in r.text.replace(" ", "")


@needs_ocr
def test_image_ocr_upside_down_is_handled():
    r = extract("png", _text_image(["Email: " + EMAIL], rotate=180))
    assert "example" in r.text.lower()


@needs_ocr
def test_blank_image_is_failure():
    buf = io.BytesIO()
    Image.new("RGB", (400, 200), "white").save(buf, "PNG")
    with pytest.raises(ExtractionError, match="no extractable text"):
        extract("png", buf.getvalue())


@needs_ocr
def test_scanned_pdf_uses_ocr_and_maps_to_pdf_points():
    png = _text_image(["Customer Name: Riya Sharma", f"Email: {EMAIL}"], size=(1200, 300), font_size=48)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(pymupdf.Rect(30, 30, 565, 30 + 535 * 300 / 1200), stream=png)
    r = extract("pdf", doc.tobytes())  # image-only page: no text layer
    assert r.pages[0].method == "ocr" and r.quality["ocr_pages"] == 1
    assert "example.com" in r.text
    assert r.coord_space == "pdf_points"
    for s in r.segments:  # boxes lie inside the page, so pixels were converted to points
        assert 0 <= s.bbox[0] < s.bbox[2] <= 595 and 0 <= s.bbox[1] < s.bbox[3] <= 842


def test_corrupt_image_rejected():
    with pytest.raises(ExtractionError):
        extract("png", b"\x89PNG\r\n\x1a\nnot really a png")


def test_quality_flags_garbled_text():
    r = extract("txt", ("#### ---- ++++ " * 10 + "a").encode())
    assert any("alphanumeric" in w for w in r.quality["warnings"])
