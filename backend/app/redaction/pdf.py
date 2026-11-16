"""True PDF redaction: the text and pixels under each rectangle are removed from the file, not covered."""
import pymupdf
from PIL import Image

from app.document_processing.ocr import get_ocr_engine, recognize_with_deadline
from app.document_processing.types import Deadline, ExtractionError
from app.redaction.plan import RedactionError, Target

WORD_KINDS = {"word", "ocr_word"}


def rects_for(page_map: dict, targets: list[Target]) -> dict[int, dict[int, list]]:
    """target index -> {page: [Rect in unrotated page space]}. Whole words that touch the span are redacted
    (a safe superset); OCR word boxes are estimates, so they get a wider margin."""
    found: dict[int, dict[int, list]] = {}
    for i, t in enumerate(targets):
        for seg in page_map.get("segments", ()):
            s, e, kind = seg[0], seg[1], seg[2]
            if kind not in WORD_KINDS or e <= t.start or s >= t.end or None in seg[3:8]:
                continue
            x0, y0, x1, y1 = seg[4:8]
            padx = (x1 - x0) * 0.2 + 2 if kind == "ocr_word" else 1
            pady = 1 if kind == "ocr_word" else 0.5
            found.setdefault(i, {}).setdefault(seg[3], []).append(pymupdf.Rect(x0 - padx, y0 - pady, x1 + padx, y1 + pady))
    return found


def redact_pdf(data: bytes, page_map: dict, targets: list[Target], text: str) -> tuple[bytes, dict]:
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        if doc.needs_pass or doc.is_encrypted:
            raise RedactionError("password-protected PDFs are not supported")
        located = rects_for(page_map, targets)
        per_page: dict[int, list] = {}
        searched = 0
        for i, t in enumerate(targets):
            if i in located:
                for page_no, rects in located[i].items():
                    per_page.setdefault(page_no, []).extend(rects)
                continue
            value = text[t.start : t.end]  # no recorded location: fall back to a text search of the page content
            for p in doc:
                for r in p.search_for(value):
                    per_page.setdefault(p.number + 1, []).append(r)
                    searched += 1
        for page_no, rects in per_page.items():
            page = doc[page_no - 1]
            for r in rects:  # stored coordinates are already in the page's unrotated space (see extractors)
                page.add_redact_annot(r, fill=(0, 0, 0))
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS)
        doc.set_metadata({})
        doc.del_xml_metadata()
        doc.scrub(metadata=True, xml_metadata=True, attached_files=True, embedded_files=True, javascript=True,
                  thumbnails=True, reset_fields=True, reset_responses=True)
        out = doc.tobytes(garbage=4, deflate=True, clean=True)
        return out, {"pages_redacted": len(per_page), "rectangles": sum(map(len, per_page.values())), "targets_via_search": searched}
    finally:
        doc.close()


def output_text(data: bytes, page_methods: dict[int, str], deadline: Deadline) -> str:
    """Everything a reader could still recover: the text layer, plus OCR of rendered scanned pages."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        parts = []
        for p in doc:
            deadline.check()
            parts.append(p.get_text())
            if page_methods.get(p.number + 1) == "ocr":
                try:
                    pix = p.get_pixmap(dpi=200, colorspace=pymupdf.csRGB, alpha=False)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    parts += [ln.text for ln in recognize_with_deadline(get_ocr_engine(), img, deadline)]
                except ExtractionError:
                    raise RedactionError("could not verify a scanned page (OCR unavailable or timed out)")
        return "\n".join(parts)
    finally:
        doc.close()


def metadata_clean(data: bytes) -> bool:
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return not any((doc.metadata or {}).get(k) for k in ("author", "title", "subject", "keywords", "creator"))
    finally:
        doc.close()
