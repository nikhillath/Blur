"""Map a character range of the canonical text back to source locations."""
from collections.abc import Sequence


def locate(page_map: dict | None, start: int, end: int) -> tuple[int | None, list[list[float]] | None, list[str] | None]:
    """Returns (first_page, rectangles, docx_refs) for text[start:end].

    Rectangles are in ``page_map['coord_space']``. A segment that is only partly covered is sliced
    proportionally by character position, which is exact for monospace text and an *estimate* for
    proportional fonts and OCR boxes. Rectangles on the same line are merged.
    """
    if not page_map:
        return None, None, None
    rects: list[tuple[int, list[float]]] = []
    refs: list[str] = []
    for seg in page_map.get("segments", ()):
        s, e, kind = seg[0], seg[1], seg[2]
        if e <= start or s >= end:
            continue
        if kind in ("para", "cell"):
            refs.append(seg[3])
            continue
        page, x0, y0, x1, y1 = seg[3], seg[4], seg[5], seg[6], seg[7]
        if None in (page, x0, y0, x1, y1):
            continue
        length = max(e - s, 1)
        lo, hi = max(start, s), min(end, e)
        width = x1 - x0
        rects.append((page, [x0 + width * (lo - s) / length, y0, x0 + width * (hi - s) / length, y1]))
    page = rects[0][0] if rects else None
    return page, _merge_line_rects(rects) if rects else None, refs or None


def _merge_line_rects(rects: Sequence[tuple[int, list[float]]]) -> list[list[float]]:
    merged: list[tuple[int, list[float]]] = []
    for page, r in rects:
        for mp, m in merged:
            height = max(m[3] - m[1], 1e-6)
            same_line = mp == page and abs(m[1] - r[1]) < height * 0.5 and abs(m[3] - r[3]) < height * 0.5
            if same_line and r[0] <= m[2] + height * 1.5:  # touching or one space apart
                m[0], m[1], m[2], m[3] = min(m[0], r[0]), min(m[1], r[1]), max(m[2], r[2]), max(m[3], r[3])
                break
        else:
            merged.append((page, list(r)))
    return [[round(v, 2) for v in [page, *m]] for page, m in merged]
