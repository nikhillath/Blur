"""Shared extraction types.

Offset contract: every character offset anywhere in the system (entities, highlights,
redaction) is a 0-based index into ``ExtractionResult.text`` -- the canonical text.
``segments`` map ranges of that text back to source locations.
"""
import time
from dataclasses import dataclass, field


class ExtractionError(Exception):
    """Raised with a message that is safe to show to the user."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class Segment:
    start: int
    end: int
    page: int | None = None  # 1-based
    bbox: tuple[float, float, float, float] | None = None  # x0, y0, x1, y1 in ExtractionResult.coord_space
    kind: str = "word"  # word | line (OCR) | para | cell
    ref: str | None = None  # DOCX location, e.g. "body.p.3", "body.t.0.r.1.c.2"

    def to_json(self) -> list:
        if self.ref is not None:
            return [self.start, self.end, self.kind, self.ref]
        x = list(self.bbox) if self.bbox else [None] * 4
        return [self.start, self.end, self.kind, self.page, *[None if v is None else round(v, 2) for v in x]]


@dataclass
class PageInfo:
    page: int
    start: int
    end: int
    method: str  # text | ocr | txt | docx | image
    ocr_confidence: float | None = None
    width: float | None = None
    height: float | None = None
    rotation: int = 0  # PDF page /Rotate in degrees; segment coordinates use the UNROTATED page space; width/height are as displayed

    def to_json(self) -> dict:
        return {
            "page": self.page, "start": self.start, "end": self.end, "method": self.method,
            "ocr_confidence": None if self.ocr_confidence is None else round(self.ocr_confidence, 3),
            "width": self.width, "height": self.height, "rotation": self.rotation,
        }


@dataclass
class ExtractionResult:
    text: str
    file_type: str
    pages: list[PageInfo]
    segments: list[Segment]
    coord_space: str | None  # "pdf_points" | "image_pixels" | None
    quality: dict = field(default_factory=dict)

    def page_map(self) -> dict:
        return {
            "coord_space": self.coord_space,
            "pages": [p.to_json() for p in self.pages],
            "segments": [s.to_json() for s in self.segments],
        }


class Deadline:
    def __init__(self, seconds: float):
        self._end = time.monotonic() + seconds

    def remaining(self) -> float:
        return self._end - time.monotonic()

    def check(self) -> None:
        if self.remaining() < 0:
            raise ExtractionError("processing time limit exceeded")


class TextBuilder:
    """Accumulates canonical text while tracking offsets."""

    def __init__(self, max_chars: int):
        self._parts: list[str] = []
        self.length = 0
        self._max = max_chars

    def add(self, s: str) -> tuple[int, int]:
        start = self.length
        self._parts.append(s)
        self.length += len(s)
        if self.length > self._max:
            raise ExtractionError("document text exceeds the size limit")
        return start, self.length

    def build(self) -> str:
        return "".join(self._parts)
