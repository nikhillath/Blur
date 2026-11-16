from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import Response

from app.storage.local import StorageError, get_storage


def attachment(storage_key: str, media_type: str, filename: str) -> Response:
    """Reads a stored (possibly encrypted) file and returns it decrypted as a download. Files are capped by
    MAX_UPLOAD_MB, so holding one in memory is fine."""
    try:
        data = get_storage().read(storage_key)
    except StorageError:
        raise HTTPException(status_code=404, detail="file not found")
    ascii_name = "".join(c if c.isascii() and c not in '"\\r\n' else "_" for c in filename) or "download"
    return Response(
        data, media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=utf-8''{quote(filename)}",
            "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
        },
    )
