import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from charset_normalizer import from_bytes

# file_type -> (canonical MIME, acceptable client-declared MIME types)
TYPES: dict[str, tuple[str, set[str]]] = {
    "pdf": ("application/pdf", {"application/pdf"}),
    "txt": ("text/plain", {"text/plain"}),
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ),
    "png": ("image/png", {"image/png"}),
    "jpg": ("image/jpeg", {"image/jpeg"}),
}
EXT_TO_TYPE = {"pdf": "pdf", "txt": "txt", "docx": "docx", "png": "png", "jpg": "jpg", "jpeg": "jpg"}
GENERIC_MIME = {"application/octet-stream", ""}
MAX_DOCX_UNCOMPRESSED = 200 * 1024 * 1024  # zip-bomb guard
MAX_DOCX_ENTRIES = 5000


class ValidationError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class ValidatedFile:
    safe_name: str
    file_type: str
    mime_type: str
    size: int


def sanitize_filename(raw: str | None) -> str:
    """Display-only name: no path components, control chars, or excessive length.
    It is never used to build a filesystem path."""
    name = (raw or "").replace("\x00", "")
    name = PureWindowsPath(PurePosixPath(name).name).name
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    if len(name) > 200:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 200 - len(ext) - 1] + dot + ext) if dot and len(ext) <= 10 else name[:200]
    return name


def _is_docx(data: bytes) -> bool:
    if not data.startswith(b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) > MAX_DOCX_ENTRIES or sum(i.file_size for i in infos) > MAX_DOCX_UNCOMPRESSED:
                raise ValidationError("document archive is too large when unpacked")
            names = {i.filename for i in infos}
            return "word/document.xml" in names and "[Content_Types].xml" in names
    except zipfile.BadZipFile:
        return False


def _is_text(data: bytes) -> bool:
    if b"\x00" in data[:8192]:
        return False
    if not data:
        return True
    return from_bytes(data[:65536]).best() is not None


def _detect(data: bytes, declared_type: str) -> bool:
    if declared_type == "pdf":
        return data[:1024].lstrip().startswith(b"%PDF-")
    if declared_type == "png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if declared_type == "jpg":
        return data.startswith(b"\xff\xd8\xff")
    if declared_type == "docx":
        return _is_docx(data)
    if declared_type == "txt":
        return _is_text(data)
    return False


def validate_upload(filename: str | None, client_mime: str | None, data: bytes) -> ValidatedFile:
    safe = sanitize_filename(filename)
    if not safe:
        raise ValidationError("filename is required")
    ext = safe.rpartition(".")[2].lower() if "." in safe else ""
    file_type = EXT_TO_TYPE.get(ext)
    if file_type is None:
        raise ValidationError(f"unsupported file type; allowed: {', '.join(sorted(EXT_TO_TYPE))}", 415)
    if not data:
        raise ValidationError("file is empty")
    canonical, accepted = TYPES[file_type]
    mime = (client_mime or "").split(";")[0].strip().lower()
    if mime not in accepted and mime not in GENERIC_MIME:
        raise ValidationError("declared content type does not match file extension", 415)
    if not _detect(data, file_type):
        raise ValidationError("file content does not match its extension", 415)
    return ValidatedFile(safe_name=safe, file_type=file_type, mime_type=canonical, size=len(data))
