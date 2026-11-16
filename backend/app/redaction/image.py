import io

from PIL import Image, ImageDraw

from app.redaction.plan import Target


def redact_image(data: bytes, page_map: dict, targets: list[Target], file_type: str) -> tuple[bytes, dict]:
    """Paint opaque boxes over the OCR word boxes. Re-encoding drops EXIF and other metadata."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    draw = ImageDraw.Draw(img)
    n = 0
    for t in targets:
        for seg in page_map.get("segments", ()):
            s, e, kind = seg[0], seg[1], seg[2]
            if kind in {"word", "ocr_word"} and s < t.end and e > t.start and None not in seg[4:8]:
                x0, y0, x1, y1 = seg[4:8]
                px = (x1 - x0) * 0.2 + 3
                draw.rectangle([x0 - px, y0 - 3, x1 + px, y1 + 3], fill=(0, 0, 0))
                n += 1
    buf = io.BytesIO()
    img.save(buf, "PNG" if file_type == "png" else "JPEG", **({} if file_type == "png" else {"quality": 92}))
    return buf.getvalue(), {"rectangles": n}
