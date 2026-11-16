import io
import zipfile

import pytest

from app.services.file_validation import ValidationError, sanitize_filename, validate_upload
from tests import samples


@pytest.mark.parametrize(
    "name,maker,mime,expected",
    [
        ("a.pdf", samples.make_pdf, "application/pdf", "pdf"),
        ("a.TXT", samples.make_txt, "text/plain", "txt"),
        ("a.docx", samples.make_docx, None, "docx"),
        ("a.png", samples.make_png, "image/png", "png"),
        ("a.jpg", samples.make_jpg, "image/jpeg", "jpg"),
        ("a.jpeg", samples.make_jpg, "application/octet-stream", "jpg"),
    ],
)
def test_valid_files(name, maker, mime, expected):
    v = validate_upload(name, mime, maker())
    assert v.file_type == expected and v.size > 0


def test_mime_is_server_determined_not_client_supplied():
    assert validate_upload("a.txt", "text/plain; charset=utf-8", b"hello").mime_type == "text/plain"


@pytest.mark.parametrize("name", ["a.exe", "a.html", "a.svg", "a", "a.pdf.exe", ".pdf.sh", ""])
def test_unsupported_extension(name):
    with pytest.raises(ValidationError) as e:
        validate_upload(name, None, b"data")
    assert e.value.status_code in (415, 422)


def test_content_must_match_extension():
    for name, data in [
        ("a.pdf", b"MZ\x90\x00 not a pdf"),
        ("a.png", samples.make_jpg()),
        ("a.jpg", samples.make_png()),
        ("a.docx", b"plain text pretending to be docx"),
        ("a.txt", b"\x00\x01\x02binary\x00"),
    ]:
        with pytest.raises(ValidationError):
            validate_upload(name, None, data)


def test_zip_that_is_not_docx_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("evil.txt", "x")
    with pytest.raises(ValidationError):
        validate_upload("a.docx", None, buf.getvalue())


def test_docx_zip_bomb_rejected(monkeypatch):
    monkeypatch.setattr("app.services.file_validation.MAX_DOCX_UNCOMPRESSED", 1000)
    with pytest.raises(ValidationError):
        validate_upload("a.docx", None, samples.make_docx("x" * 5000))


def test_declared_mime_mismatch_rejected():
    with pytest.raises(ValidationError) as e:
        validate_upload("a.pdf", "text/html", samples.make_pdf())
    assert e.value.status_code == 415


def test_empty_file_rejected():
    with pytest.raises(ValidationError):
        validate_upload("a.txt", "text/plain", b"")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\cmd.txt", "cmd.txt"),
        ("/abs/path/file.pdf", "file.pdf"),
        ("file\x00.pdf", "file.pdf"),
        ("fi\nle\t.txt", "file.txt"),
        ("  ..hidden.txt  ", "hidden.txt"),
        (None, ""),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_truncates_but_keeps_extension():
    out = sanitize_filename("a" * 500 + ".pdf")
    assert len(out) <= 200 and out.endswith(".pdf")
