import io
import re
import zipfile

import pymupdf
import pytest
from docx import Document as DocxDocument
from docx.shared import Pt
from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.document_processing.extractors import extract
from app.document_processing.ocr import ocr_available
from app.models import AuditLog, PiiEntity
from app.redaction import docx as docx_red
from app.redaction.plan import Target, check_absent, merge_targets, replacement
from tests import samples
from tests.test_detection_pipeline import FULL
from tests.test_extraction import EMAIL, _text_image

DOCS = "/api/v1/documents"
PHONE = "+91 98765 43210"
PAN = "ABCPD1234F"
needs_ocr = pytest.mark.skipif(not ocr_available(), reason="OCR engine unavailable")


@pytest.fixture
def alice(make_user, auth_headers):
    make_user("alice@example.com")
    return auth_headers("alice@example.com")


@pytest.fixture
def bob(make_user, auth_headers):
    make_user("bob@example.com")
    return auth_headers("bob@example.com")


def flow(client, hdr, name, data, mime, decide="all", style=None):
    """upload -> scan -> mark entities -> redact. decide: 'all' (REDACT every entity) or 'none'."""
    did = client.post(DOCS, headers=hdr, files={"file": (name, data, mime)}).json()["id"]
    sid = client.post(f"{DOCS}/{did}/scan", headers=hdr).json()["id"]
    if decide == "all":
        client.post(f"/api/v1/scans/{sid}/reviews/bulk", json={"action": "REDACT", "min_confidence": 0.0}, headers=hdr)
    body = {"mask_style": style} if style else None
    r = client.post(f"/api/v1/scans/{sid}/redact", json=body, headers=hdr)
    return did, sid, r


def download(client, hdr, rid):
    r = client.get(f"/api/v1/redacted-documents/{rid}/download", headers=hdr)
    assert r.status_code == 200, r.text
    return r.content


def entity_id(client, hdr, sid, value_type):
    return next(e["id"] for e in client.get(f"/api/v1/scans/{sid}/entities?page_size=200", headers=hdr).json()["items"] if e["entity_type"] == value_type)


# ------------------------------------------------------------------------------------ TXT
def test_txt_redaction_end_to_end(client, alice):
    original = FULL.encode()
    did, sid, r = flow(client, alice, "d.txt", original, "text/plain")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["validation_status"] == "PASSED" and body["redaction_count"] >= 8
    assert "storage_key" not in str(body)
    out = download(client, alice, body["id"]).decode()
    for secret in (EMAIL, PHONE, PAN, "4521 7834 9123", "Riya Sharma", "CUST-DEMO-001"):
        assert secret not in out
    assert out.count("[REDACTED]") >= 8
    assert "CUSTOMER INFORMATION RECORD" in out and "Customer Name:" in out  # surrounding text preserved
    assert client.get(f"{DOCS}/{did}/download", headers=alice).content == original  # original untouched


def test_txt_keep_decisions_are_left_unchanged(client, alice):
    did = client.post(DOCS, headers=alice, files={"file": ("d.txt", FULL.encode(), "text/plain")}).json()["id"]
    sid = client.post(f"{DOCS}/{did}/scan", headers=alice).json()["id"]
    client.post(f"/api/v1/scans/{sid}/reviews/bulk", json={"action": "REDACT", "min_confidence": 0.0}, headers=alice)
    client.post(f"/api/v1/entities/{entity_id(client, alice, sid, 'EMAIL')}/review", json={"action": "KEEP"}, headers=alice)
    r = client.post(f"/api/v1/scans/{sid}/redact", headers=alice)
    out = download(client, alice, r.json()["id"]).decode()
    assert EMAIL in out and PHONE not in out  # the KEEP decision survived


def test_repeated_value_redacting_one_occurrence_only(client, alice):
    text = f"Primary {EMAIL}\nBackup {EMAIL}\n"
    did = client.post(DOCS, headers=alice, files={"file": ("d.txt", text.encode(), "text/plain")}).json()["id"]
    sid = client.post(f"{DOCS}/{did}/scan", headers=alice).json()["id"]
    first = client.get(f"/api/v1/scans/{sid}/entities", headers=alice).json()["items"][0]
    client.post(f"/api/v1/entities/{first['id']}/review", json={"action": "REDACT"}, headers=alice)
    r = client.post(f"/api/v1/scans/{sid}/redact", headers=alice)
    assert r.json()["validation_status"] == "PASSED"  # one remaining occurrence is expected, not a leak
    assert download(client, alice, r.json()["id"]).decode().count(EMAIL) == 1


def test_partial_style_masks_numbers_but_keeps_last_four(client, alice):
    _, _, r = flow(client, alice, "d.txt", FULL.encode(), "text/plain", style="partial")
    out = download(client, alice, r.json()["id"]).decode()
    assert "4521 7834 9123" not in out and "XXXXXXXX9123" in out
    assert r.json()["validation_details"]["mask_style_applied"] == "partial"
    assert "[REDACTED]" in out  # non-numeric types are still fully masked


def test_overlapping_entities_are_merged_cleanly(client, alice):
    text = "Contact: Riya Sharma lives here\n"
    did = client.post(DOCS, headers=alice, files={"file": ("d.txt", text.encode(), "text/plain")}).json()["id"]
    sid = client.post(f"{DOCS}/{did}/scan", headers=alice).json()["id"]
    i = text.index("Riya")
    client.post(f"/api/v1/scans/{sid}/entities", json={"start": i, "end": i + 4, "entity_type": "PERSON"}, headers=alice)
    client.post(f"/api/v1/scans/{sid}/entities", json={"start": i + 2, "end": i + 11, "entity_type": "OTHER_IDENTIFIER"}, headers=alice)
    r = client.post(f"/api/v1/scans/{sid}/redact", headers=alice)
    out = download(client, alice, r.json()["id"]).decode()
    assert out == "Contact: [REDACTED] lives here\n"


# ------------------------------------------------------------------------------------ PDF
def test_pdf_text_is_really_removed_not_covered(client, alice):
    data = samples.make_pdf(FULL)
    did, sid, r = flow(client, alice, "d.pdf", data, "application/pdf")
    assert r.status_code == 201 and r.json()["validation_status"] == "PASSED", r.text
    out = download(client, alice, r.json()["id"])
    doc = pymupdf.open(stream=out, filetype="pdf")
    text = "".join(p.get_text() for p in doc)
    for secret in (EMAIL, PHONE, PAN, "4521", "Riya"):
        assert secret not in text, secret          # not recoverable by text extraction
        assert secret.encode() not in out
    assert "CUSTOMER INFORMATION RECORD" in text and doc.page_count == 1  # rest of the page intact
    assert not any((doc.metadata or {}).get(k) for k in ("author", "title", "creator"))
    orig = "".join(p.get_text() for p in pymupdf.open(stream=client.get(f"{DOCS}/{did}/download", headers=alice).content, filetype="pdf"))
    assert EMAIL in orig  # original document unchanged
    pix = doc[0].get_pixmap(dpi=72)
    assert any(px != 255 for px in pix.samples[::3])   # black boxes were actually drawn


def test_pdf_redaction_on_rotated_pages(client, alice):
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), f"Contact {EMAIL} for details")
    page.set_rotation(90)
    _, _, r = flow(client, alice, "rot.pdf", pdf.tobytes(), "application/pdf")
    assert r.json()["validation_status"] == "PASSED"
    out = pymupdf.open(stream=download(client, alice, r.json()["id"]), filetype="pdf")
    assert EMAIL not in out[0].get_text() and "Contact" in out[0].get_text()


def test_multipage_pdf_only_touches_pages_with_targets(client, alice):
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "Page one has nothing sensitive at all")
    pdf.new_page().insert_text((72, 72), f"Page two mail {EMAIL}")
    _, _, r = flow(client, alice, "m.pdf", pdf.tobytes(), "application/pdf")
    d = r.json()["validation_details"]
    assert d["pages_redacted"] == 1 and r.json()["validation_status"] == "PASSED"
    out = pymupdf.open(stream=download(client, alice, r.json()["id"]), filetype="pdf")
    assert "nothing sensitive" in out[0].get_text() and EMAIL not in out[1].get_text()


def test_pdf_falls_back_to_text_search_without_locations(client, alice, monkeypatch):
    from app.redaction import pdf as pdf_mod

    monkeypatch.setattr(pdf_mod, "rects_for", lambda *a, **k: {})
    _, _, r = flow(client, alice, "d.pdf", samples.make_pdf(FULL), "application/pdf")
    assert r.json()["validation_status"] == "PASSED" and r.json()["validation_details"]["targets_via_search"] > 0


def test_verification_catches_a_broken_engine_and_keeps_no_file(client, alice, monkeypatch, storage):
    from app.redaction import pdf as pdf_mod

    monkeypatch.setattr(pdf_mod, "redact_pdf", lambda data, pm, targets, text: (data, {}))  # "redacts" nothing
    did, sid, r = flow(client, alice, "d.pdf", samples.make_pdf(FULL), "application/pdf")
    body = r.json()
    assert r.status_code == 201 and body["validation_status"] == "FAILED"
    assert body["validation_details"]["verification"]["leaks"] > 0
    assert client.get(f"/api/v1/redacted-documents/{body['id']}/download", headers=alice).status_code == 409
    assert not any(k.startswith("redacted/") for k in storage.iter_keys())  # failed output is never retained
    with SessionLocal() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "REDACTION_FAILED_VALIDATION"))


@needs_ocr
def test_scanned_pdf_pixels_are_removed(client, alice):
    png = _text_image(["Customer Name: Riya Sharma", f"Email: {EMAIL}"], size=(1200, 300), font_size=48)
    pdf = pymupdf.open()
    page = pdf.new_page(width=595, height=842)
    page.insert_image(pymupdf.Rect(30, 30, 565, 30 + 535 * 300 / 1200), stream=png)
    _, _, r = flow(client, alice, "scan.pdf", pdf.tobytes(), "application/pdf")
    assert r.status_code == 201 and r.json()["validation_status"] == "PASSED", r.text
    out = pymupdf.open(stream=download(client, alice, r.json()["id"]), filetype="pdf")
    pix = out[0].get_pixmap(dpi=200, colorspace=pymupdf.csRGB, alpha=False)
    from PIL import Image

    from app.document_processing.ocr import get_ocr_engine
    lines = get_ocr_engine().recognize(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    assert "example" not in " ".join(ln.text for ln in lines).lower()  # re-OCR of the output finds nothing


# ------------------------------------------------------------------------------------ images
@needs_ocr
def test_image_redaction_removes_text_and_exif(client, alice):
    from PIL import Image

    from app.document_processing.ocr import get_ocr_engine

    data = _text_image(["Customer Name: Riya Sharma", f"Email: {EMAIL}"])
    _, _, r = flow(client, alice, "scan.png", data, "image/png")
    assert r.status_code == 201 and r.json()["validation_status"] == "PASSED", r.text
    out = download(client, alice, r.json()["id"])
    img = Image.open(io.BytesIO(out))
    assert img.size == Image.open(io.BytesIO(data)).size and not img.info.get("exif")
    assert "example" not in " ".join(ln.text for ln in get_ocr_engine().recognize(img)).lower()


# ------------------------------------------------------------------------------------ DOCX
def make_docx():
    d = DocxDocument()
    p = d.add_paragraph()
    p.add_run("Name: ").bold = True
    p.add_run("Riya ").font.size = Pt(14)
    p.add_run("Sharma")              # one name split across two runs
    q = d.add_paragraph()
    q.add_run("Contact ")
    q.add_run("riya.sharma@")        # one email split across two runs
    q.add_run("example.com now.")
    d.add_paragraph("Untouched paragraph about renewals.")
    t = d.add_table(rows=1, cols=2)
    t.cell(0, 0).text = "Phone"
    t.cell(0, 1).text = PHONE
    d.sections[0].header.paragraphs[0].text = f"Header: {PAN}"
    d.core_properties.author = "Riya Sharma"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_docx_redaction_covers_body_table_header_and_split_runs(client, alice):
    data = make_docx()
    did, sid, r = flow(client, alice, "d.docx", data, "application/octet-stream")
    assert r.status_code == 201 and r.json()["validation_status"] == "PASSED", r.text
    out = download(client, alice, r.json()["id"])
    doc = DocxDocument(io.BytesIO(out))
    all_text = "\n".join(p.text for p in doc.paragraphs) + doc.tables[0].cell(0, 1).text + doc.sections[0].header.paragraphs[0].text
    for secret in ("riya.sharma", "example.com", PHONE, PAN, "Sharma"):
        assert secret not in all_text, secret
    assert "Untouched paragraph about renewals." in all_text
    assert doc.paragraphs[0].runs[0].bold and doc.paragraphs[0].runs[0].text == "Name: "  # formatting kept
    assert doc.core_properties.author == ""
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        raw = b"".join(z.read(n) for n in z.namelist())
    assert b"example.com" not in raw and b"Riya" not in raw
    assert DocxDocument(io.BytesIO(client.get(f"{DOCS}/{did}/download", headers=alice).content)).paragraphs[0].text.startswith("Name: Riya")


def test_docx_refs_match_between_extractor_and_redactor():
    data = make_docx()
    d = DocxDocument(io.BytesIO(data))
    redactor_refs = {ref for ref, _ in docx_red.iter_paragraphs(d)}
    extractor_refs = {s[3] for s in extract("docx", data).page_map()["segments"]}
    assert extractor_refs <= redactor_refs and extractor_refs


def docx_with_footnote(text: str) -> bytes:
    """A DOCX whose footnotes part (not edited by the redactor) holds sensitive text, linked by a real relationship."""
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part

    d = DocxDocument(io.BytesIO(make_docx()))
    blob = f'<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:footnote><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:footnote></w:footnotes>'.encode()
    part = Part(PackURI("/word/footnotes.xml"), "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml", blob, d.part.package)
    d.part.relate_to(part, RT.FOOTNOTES)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_docx_value_in_unhandled_part_fails_verification(client, alice, storage):
    """The footnotes part is not edited; the all-parts verifier must notice the value is still there and refuse."""
    _, _, r = flow(client, alice, "d.docx", docx_with_footnote(EMAIL), "application/octet-stream")
    assert r.status_code == 201 and r.json()["validation_status"] == "FAILED", r.text
    assert r.json()["validation_details"]["verification"]["leaks"] >= 1
    assert client.get(f"/api/v1/redacted-documents/{r.json()['id']}/download", headers=alice).status_code == 409
    assert not any(k.startswith("redacted/") for k in storage.iter_keys())


def test_docx_with_clean_footnote_still_passes(client, alice):
    _, _, r = flow(client, alice, "d.docx", docx_with_footnote("see the appendix"), "application/octet-stream")
    assert r.json()["validation_status"] == "PASSED"


# ------------------------------------------------------------------------------------ rules / unit
def test_check_absent_semantics():
    ok = check_absent(f"a {EMAIL} b {EMAIL}", f"a [X] b {EMAIL}", [EMAIL])
    assert ok["passed"]                                   # one of two occurrences redacted
    leak = check_absent(f"a {EMAIL}", f"a {EMAIL}", [EMAIL])
    assert not leak["passed"] and leak["leaks"] == 1
    assert check_absent("x", "x", ["ab"])["checked_values"] == 0   # too short to verify
    assert check_absent("Ri ya", "[X]", ["Riya"])["passed"]        # whitespace-insensitive


def test_merge_targets_unions_overlaps_but_not_adjacent():
    class E:
        def __init__(s, a, b, t="PERSON"):
            s.start_position, s.end_position, s.entity_type, s.id = a, b, t, object()

    ts = merge_targets([E(10, 20), E(0, 5), E(4, 8, "PHONE"), E(20, 25)])
    assert [(t.start, t.end) for t in ts] == [(0, 8), (10, 20), (20, 25)]
    assert ts[0].types == {"PERSON", "PHONE"} and len(ts[0].entity_ids) == 2


def test_replacement_policies():
    assert replacement("full", "4521 7834 9123", {"AADHAAR"}) == "[REDACTED]"
    assert replacement("partial", "4521 7834 9123", {"AADHAAR"}) == "XXXXXXXX9123"
    assert replacement("partial", "Riya Sharma", {"PERSON"}) == "[REDACTED]"
    assert replacement("partial", "12345", {"PHONE"}) == "[REDACTED]"


# ------------------------------------------------------------------------------------ API / security
def test_nothing_selected_and_invalid_input(client, alice):
    did = client.post(DOCS, headers=alice, files={"file": ("d.txt", FULL.encode(), "text/plain")}).json()["id"]
    sid = client.post(f"{DOCS}/{did}/scan", headers=alice).json()["id"]
    assert client.post(f"/api/v1/scans/{sid}/redact", headers=alice).status_code == 409
    assert client.post(f"/api/v1/scans/{sid}/redact", json={"mask_style": "blur"}, headers=alice).status_code == 422
    assert client.get("/api/v1/redacted-documents/not-a-uuid", headers=alice).status_code == 422


def test_pending_entities_are_reported(client, alice):
    did = client.post(DOCS, headers=alice, files={"file": ("d.txt", FULL.encode(), "text/plain")}).json()["id"]
    sid = client.post(f"{DOCS}/{did}/scan", headers=alice).json()["id"]
    client.post(f"/api/v1/entities/{entity_id(client, alice, sid, 'EMAIL')}/review", json={"action": "REDACT"}, headers=alice)
    r = client.post(f"/api/v1/scans/{sid}/redact", headers=alice)
    assert r.json()["validation_details"]["pending_entities_at_redaction"] > 0
    assert r.json()["redaction_count"] == 1


def test_access_control(client, alice, bob, make_user, auth_headers):
    did, sid, r = flow(client, alice, "d.txt", FULL.encode(), "text/plain")
    rid = r.json()["id"]
    base = f"/api/v1/redacted-documents/{rid}"
    for method, url in (("get", base), ("get", base + "/download"), ("delete", base), ("get", f"{DOCS}/{did}/redacted")):
        assert getattr(client, method)(url, headers=bob).status_code == 404, url
        assert getattr(client, method)(url).status_code == 401, url
    assert client.post(f"/api/v1/scans/{sid}/redact", headers=bob).status_code == 404
    make_user("rita@example.com", "REVIEWER")
    rita = auth_headers("rita@example.com")
    assert client.get(base + "/download", headers=rita).status_code == 404
    client.post(f"{DOCS}/{did}/reviewers", json={"email": "rita@example.com"}, headers=alice)
    assert client.get(base + "/download", headers=rita).status_code == 200
    again = client.post(f"/api/v1/scans/{sid}/redact", headers=rita)         # reviewers may trigger redaction
    assert again.status_code == 201 and again.json()["id"] != rid
    assert client.delete(base, headers=rita).status_code == 404 or client.delete(f"/api/v1/redacted-documents/{again.json()['id']}", headers=rita).status_code == 404
    assert len(client.get(f"{DOCS}/{did}/redacted", headers=alice).json()) == 2


def test_download_headers_listing_delete_and_audit(client, alice, storage):
    did, sid, r = flow(client, alice, "my report.txt", FULL.encode(), "text/plain")
    rid = r.json()["id"]
    resp = client.get(f"/api/v1/redacted-documents/{rid}/download", headers=alice)
    assert resp.headers["content-disposition"].startswith("attachment") and "redacted_my%20report.txt" in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff" and "no-store" in resp.headers["cache-control"]
    assert client.get(f"/api/v1/redacted-documents/{rid}", headers=alice).json()["id"] == rid
    assert any(k.startswith("redacted/") for k in storage.iter_keys())
    assert client.delete(f"/api/v1/redacted-documents/{rid}", headers=alice).status_code == 204
    assert not any(k.startswith("redacted/") for k in storage.iter_keys())
    with SessionLocal() as db:
        actions = {a for (a,) in db.execute(select(AuditLog.action))}
        assert {"REDACTION_CREATED", "REDACTED_DOWNLOADED", "REDACTED_DELETED"} <= actions
        blob = str([a.details for a in db.scalars(select(AuditLog))])
        assert "riya.sharma" not in blob.lower() and "98765" not in blob


def test_deleting_the_document_removes_redacted_files(client, alice, storage):
    did, _, _ = flow(client, alice, "d.txt", FULL.encode(), "text/plain")
    assert any(k.startswith("redacted/") for k in storage.iter_keys())
    assert client.delete(f"{DOCS}/{did}", headers=alice).status_code == 204
    assert list(storage.iter_keys()) == []
    with SessionLocal() as db:
        from app.models import RedactedDocument

        assert db.scalar(select(func.count()).select_from(RedactedDocument)) == 0


def test_redaction_never_changes_stored_entities_or_original(client, alice, storage):
    did = client.post(DOCS, headers=alice, files={"file": ("d.txt", FULL.encode(), "text/plain")}).json()["id"]
    sid = client.post(f"{DOCS}/{did}/scan", headers=alice).json()["id"]
    client.post(f"/api/v1/scans/{sid}/reviews/bulk", json={"action": "REDACT", "min_confidence": 0.0}, headers=alice)
    with SessionLocal() as db:
        before = [(e.id, e.detected_value, e.review_status) for e in db.scalars(select(PiiEntity).order_by(PiiEntity.id))]
    client.post(f"/api/v1/scans/{sid}/redact", headers=alice)
    with SessionLocal() as db:
        after = [(e.id, e.detected_value, e.review_status) for e in db.scalars(select(PiiEntity).order_by(PiiEntity.id))]
    assert before == after
    assert client.get(f"{DOCS}/{did}/download", headers=alice).content == FULL.encode()


def test_text_regex_sanity_no_redaction_markers_leak_values(client, alice):
    _, _, r = flow(client, alice, "d.txt", FULL.encode(), "text/plain")
    out = download(client, alice, r.json()["id"]).decode()
    assert not re.search(r"[\w.]+@[\w.]+\.\w+", out) and not re.search(r"\d{5}\s?\d{5}", out)


@needs_ocr
@pytest.mark.parametrize("rotation", [90, 180])
def test_rotated_scanned_pdf_redaction(client, alice, rotation):
    from PIL import Image

    from app.document_processing.ocr import get_ocr_engine

    png = _text_image(["Customer Name: Riya Sharma", f"Email: {EMAIL}"], size=(1200, 300), font_size=48)
    pdf = pymupdf.open()
    page = pdf.new_page(width=595, height=842)
    page.insert_image(pymupdf.Rect(30, 30, 565, 30 + 535 * 300 / 1200), stream=png)
    page.set_rotation(rotation)
    _, _, r = flow(client, alice, "rot-scan.pdf", pdf.tobytes(), "application/pdf")
    assert r.status_code == 201 and r.json()["validation_status"] == "PASSED", r.text
    out = pymupdf.open(stream=download(client, alice, r.json()["id"]), filetype="pdf")
    pix = out[0].get_pixmap(dpi=200, colorspace=pymupdf.csRGB, alpha=False)
    lines = get_ocr_engine().recognize(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    assert "example" not in " ".join(ln.text for ln in lines).lower()
