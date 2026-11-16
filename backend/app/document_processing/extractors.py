import io
import logging
import re
from collections.abc import Callable

import pymupdf
from charset_normalizer import from_bytes
from docx import Document as DocxDocument
from docx.table import Table
from docx.text.paragraph import Paragraph
from PIL import Image, UnidentifiedImageError

from app.core.config import get_settings
from app.document_processing.ocr import OcrLine, get_ocr_engine, recognize_with_deadline
from app.document_processing.types import (
    Deadline,
    ExtractionError,
    ExtractionResult,
    PageInfo,
    Segment,
    TextBuilder,
)

logger = logging.getLogger("pii_sentinel.extraction")

PAGE_SEPARATOR = "\n\n"


# ----------------------------------------------------------------------------- TXT
def extract_txt(data: bytes, deadline: Deadline) -> ExtractionResult:
    match = from_bytes(data).best()
    if match is None:
        raise ExtractionError("could not determine the text encoding")
    text = str(match)
    if text.startswith("﻿"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")  # canonical newlines
    if len(text) > get_settings().max_extracted_chars:
        raise ExtractionError("document text exceeds the size limit")
    return ExtractionResult(
        text=text, file_type="txt", coord_space=None,
        pages=[PageInfo(page=1, start=0, end=len(text), method="txt")],
        segments=[],
        quality={"encoding": match.encoding},
    )


# ---------------------------------------------------------------------------- DOCX
def _iter_blocks(parent_element, parent):
    """Yield Paragraph/Table objects in document order."""
    for child in parent_element.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield Paragraph(child, parent)
        elif tag == "tbl":
            yield Table(child, parent)


def extract_docx(data: bytes, deadline: Deadline) -> ExtractionResult:
    settings = get_settings()
    try:
        doc = DocxDocument(io.BytesIO(data))
    except Exception as exc:
        raise ExtractionError("the DOCX file could not be read") from exc

    tb = TextBuilder(settings.max_extracted_chars)
    segments: list[Segment] = []

    def add_paragraph(p: Paragraph, ref: str, trailer: str = "\n") -> None:
        start, end = tb.add(p.text)
        if end > start:
            segments.append(Segment(start, end, kind="para", ref=ref))
        tb.add(trailer)

    def add_table(t: Table, ref: str) -> None:
        for ri, row in enumerate(t.rows):
            seen: set[int] = set()
            cells = []
            for cell in row.cells:  # merged cells repeat; emit each underlying cell once
                if id(cell._tc) in seen:
                    continue
                seen.add(id(cell._tc))
                cells.append(cell)
            for ci, cell in enumerate(cells):
                cref = f"{ref}.r.{ri}.c.{ci}"
                for pi, block in enumerate(_iter_blocks(cell._tc, cell)):
                    if isinstance(block, Paragraph):
                        start, end = tb.add(block.text)
                        if end > start:
                            segments.append(Segment(start, end, kind="cell", ref=f"{cref}.p.{pi}"))
                        if pi + 1 < len(cell.paragraphs) + len(cell.tables):
                            tb.add(" ")
                    else:
                        add_table(block, f"{cref}.t.{pi}")
                if ci + 1 < len(cells):
                    tb.add("\t")
            tb.add("\n")

    def add_container(parent_element, parent, prefix: str) -> None:
        pi = ti = 0
        for block in _iter_blocks(parent_element, parent):
            deadline.check()
            if isinstance(block, Paragraph):
                add_paragraph(block, f"{prefix}.p.{pi}")
                pi += 1
            else:
                add_table(block, f"{prefix}.t.{ti}")
                ti += 1

    add_container(doc.element.body, doc, "body")
    for si, section in enumerate(doc.sections):
        for kind, part in (("header", section.header), ("footer", section.footer)):
            if part.is_linked_to_previous and si > 0:
                continue
            add_container(part._element, part, f"{kind}.{si}")

    text = tb.build()
    return ExtractionResult(
        text=text, file_type="docx", coord_space=None,
        pages=[PageInfo(page=1, start=0, end=len(text), method="docx")],
        segments=segments,
        quality={"sections": len(doc.sections)},
    )


# ------------------------------------------------------------------------ OCR helper
def _ocr_to_text(
    tb: TextBuilder, lines: list[OcrLine], page_no: int, to_space: Callable[[tuple], tuple]
) -> tuple[list[Segment], float | None]:
    segments = []
    for ln in lines:
        line_start, _ = tb.add(ln.text)
        tb.add("\n")
        x0, y0, x1, y1 = to_space(ln.bbox)
        n = max(len(ln.text), 1)
        # The engine gives one box per line; split it per word proportionally to character position.
        # These word boxes are estimates (exact only for monospace text).
        for w in re.finditer(r"\S+", ln.text):
            segments.append(Segment(
                line_start + w.start(), line_start + w.end(), page=page_no,
                bbox=(x0 + (x1 - x0) * w.start() / n, y0, x0 + (x1 - x0) * w.end() / n, y1), kind="ocr_word",
            ))
    conf = sum(ln.confidence for ln in lines) / len(lines) if lines else None
    return segments, conf


# ----------------------------------------------------------------------------- PDF
def extract_pdf(data: bytes, deadline: Deadline) -> ExtractionResult:
    settings = get_settings()
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ExtractionError("the PDF could not be read") from exc
    try:
        if doc.needs_pass or doc.is_encrypted:
            raise ExtractionError("password-protected PDFs are not supported")
        if doc.page_count == 0:
            raise ExtractionError("the PDF has no pages")
        if doc.page_count > settings.max_pdf_pages:
            raise ExtractionError(f"the PDF exceeds the {settings.max_pdf_pages}-page limit")

        tb = TextBuilder(settings.max_extracted_chars)
        segments: list[Segment] = []
        pages: list[PageInfo] = []
        ocr_pages = 0
        warnings: list[str] = []

        for index in range(doc.page_count):
            deadline.check()
            page = doc[index]
            page_no = index + 1
            if page_no > 1:
                tb.add(PAGE_SEPARATOR)
            words = page.get_text("words")
            n_chars = sum(len(w[4].strip()) for w in words)
            page_start = tb.length

            if n_chars >= settings.ocr_min_text_chars:
                _add_text_layer(tb, segments, words, page_no)
                method, conf = "text", None
            else:
                try:
                    engine = get_ocr_engine()
                except ExtractionError:
                    if n_chars == 0:
                        warnings.append(f"page {page_no}: no text layer and OCR is unavailable")
                        method, conf = "text", None
                        pages.append(PageInfo(page_no, page_start, tb.length, method, conf, page.rect.width, page.rect.height, page.rotation))
                        continue
                    _add_text_layer(tb, segments, words, page_no)
                    method, conf = "text", None
                else:
                    zoom = settings.ocr_dpi / 72.0
                    pix = page.get_pixmap(dpi=settings.ocr_dpi, colorspace=pymupdf.csRGB, alpha=False)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    lines = recognize_with_deadline(engine, img, deadline)
                    # OCR pixels -> PDF points, so every PDF segment shares one coordinate space
                    # The pixmap shows the page as DISPLAYED (rotated); PDF text coordinates use the unrotated page.
                    # Convert so every PDF segment shares the unrotated space.
                    segs, conf = _ocr_to_text(
                        tb, lines, page_no,
                        lambda b, z=zoom, m=page.derotation_matrix: tuple(pymupdf.Rect([v / z for v in b]) * m),
                    )
                    segments.extend(segs)
                    method = "ocr"
                    ocr_pages += 1
            pages.append(PageInfo(page_no, page_start, tb.length, method, conf, page.rect.width, page.rect.height, page.rotation))

        text = tb.build()
        ocr_confs = [p.ocr_confidence for p in pages if p.ocr_confidence is not None]
        quality = {"page_count": len(pages), "ocr_pages": ocr_pages, "warnings": warnings}
        if ocr_confs:
            quality["mean_ocr_confidence"] = round(sum(ocr_confs) / len(ocr_confs), 3)
        return ExtractionResult(text=text, file_type="pdf", pages=pages, segments=segments,
                                coord_space="pdf_points", quality=quality)
    finally:
        doc.close()


def _add_text_layer(tb: TextBuilder, segments: list[Segment], words: list, page_no: int) -> None:
    """Rebuild reading-order text from word boxes: words -> lines -> blocks."""
    prev_block = prev_line = None
    for x0, y0, x1, y1, word, block_no, line_no, _ in words:
        if prev_block is not None:
            if block_no != prev_block:
                tb.add("\n\n")
            elif line_no != prev_line:
                tb.add("\n")
            else:
                tb.add(" ")
        start, end = tb.add(word)
        segments.append(Segment(start, end, page=page_no, bbox=(x0, y0, x1, y1), kind="word"))
        prev_block, prev_line = block_no, line_no


# --------------------------------------------------------------------------- IMAGE
def extract_image(data: bytes, deadline: Deadline) -> ExtractionResult:
    settings = get_settings()
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise ExtractionError("the image could not be read") from exc
    engine = get_ocr_engine()  # ExtractionError("OCR engine is not available") if missing
    tb = TextBuilder(settings.max_extracted_chars)
    lines = recognize_with_deadline(engine, img, deadline)
    segs, conf = _ocr_to_text(tb, lines, 1, lambda b: b)
    text = tb.build()
    return ExtractionResult(
        text=text, file_type="image", coord_space="image_pixels",
        pages=[PageInfo(1, 0, len(text), "ocr", conf, float(img.width), float(img.height))],
        segments=segs,
        quality={"page_count": 1, "ocr_pages": 1,
                 **({"mean_ocr_confidence": round(conf, 3)} if conf is not None else {})},
    )


# ------------------------------------------------------------------------ DISPATCH
EXTRACTORS = {
    "pdf": extract_pdf, "txt": extract_txt, "docx": extract_docx,
    "png": extract_image, "jpg": extract_image,
}


def assess_quality(result: ExtractionResult) -> dict:
    text = result.text
    stripped = text.strip()
    if not stripped:
        raise ExtractionError("no extractable text was found in the document")
    q = dict(result.quality)
    warnings = list(q.get("warnings", []))
    alnum = sum(c.isalnum() for c in stripped)
    q["char_count"] = len(text)
    q["alnum_ratio"] = round(alnum / len(stripped), 3)
    if "�" in text:
        warnings.append("text contains undecodable replacement characters")
    if q["alnum_ratio"] < 0.3:
        warnings.append("very little alphanumeric text; extraction may be poor")
    conf = q.get("mean_ocr_confidence")
    if conf is not None and conf < 0.6:
        warnings.append("low OCR confidence; detections may be unreliable")
    q["warnings"] = warnings
    return q


def extract(file_type: str, data: bytes, deadline: Deadline | None = None) -> ExtractionResult:
    fn = EXTRACTORS.get(file_type)
    if fn is None:
        raise ExtractionError("unsupported file type")
    deadline = deadline or Deadline(get_settings().scan_timeout_seconds)
    result = fn(data, deadline)
    result.quality = assess_quality(result)
    return result
