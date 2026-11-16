"""Builders for fictional sample files. No real personal data."""
import io

import pymupdf
from docx import Document as DocxDocument
from PIL import Image

SAMPLE_TEXT = (
    "CUSTOMER INFORMATION RECORD\n"
    "Customer Name: Riya Sharma\n"
    "Email: riya.sharma@example.com\n"
    "Phone: +91 98765 43210\n"
    "This information is fictional and intended solely for local demonstration.\n"
)


def make_txt() -> bytes:
    return SAMPLE_TEXT.encode()


def make_pdf(text: str = SAMPLE_TEXT) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def make_docx(text: str = SAMPLE_TEXT) -> bytes:
    d = DocxDocument()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def make_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(buf, "PNG")
    return buf.getvalue()


def make_jpg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(buf, "JPEG")
    return buf.getvalue()
