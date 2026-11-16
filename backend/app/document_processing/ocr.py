"""Pluggable OCR. The default engine is RapidOCR (pip-installable ONNX models, no system install)."""
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

import numpy as np
from PIL import Image, ImageOps

from app.document_processing.types import Deadline, ExtractionError

Image.MAX_IMAGE_PIXELS = 60_000_000  # decompression-bomb guard

MIN_SIDE = 1000  # upscale smaller images so small glyphs are recognisable
MAX_SIDE = 4000  # downscale huge images to bound memory/time


@dataclass
class OcrLine:
    text: str
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1 in the *input* image's pixel space
    confidence: float  # engine score in [0, 1]; not a calibrated probability


class OcrEngine(Protocol):
    name: str

    def recognize(self, image: Image.Image) -> list[OcrLine]: ...


def preprocess(image: Image.Image) -> tuple[Image.Image, float]:
    """Returns (prepared RGB image, scale applied). Coordinates from the prepared image
    must be divided by ``scale`` to return to the original pixel space."""
    image = ImageOps.exif_transpose(image) or image
    image = image.convert("L")
    image = ImageOps.autocontrast(image)
    longest = max(image.size)
    scale = 1.0
    if longest < MIN_SIDE:
        scale = MIN_SIDE / longest
    elif longest > MAX_SIDE:
        scale = MAX_SIDE / longest
    if scale != 1.0:
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
    return image.convert("RGB"), scale


class RapidOcrEngine:
    name = "rapidocr"

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR  # imported lazily: heavy

        self._engine = RapidOCR()
        self._lock = threading.Lock()

    def recognize(self, image: Image.Image) -> list[OcrLine]:
        prepared, scale = preprocess(image)
        with self._lock:
            result, _ = self._engine(np.asarray(prepared))
        lines: list[OcrLine] = []
        for box, text, score in result or []:
            text = (text or "").strip()
            if not text:
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            bbox = (min(xs) / scale, min(ys) / scale, max(xs) / scale, max(ys) / scale)
            lines.append(OcrLine(text=text, bbox=bbox, confidence=float(score)))
        # reading order: top to bottom, then left to right
        lines.sort(key=lambda ln: (round(ln.bbox[1] / 10), ln.bbox[0]))
        return lines


@lru_cache
def get_ocr_engine() -> OcrEngine:
    try:
        return RapidOcrEngine()
    except Exception as exc:  # missing package or model load failure
        raise ExtractionError("OCR engine is not available") from exc


# One worker: ONNX sessions are not worth running concurrently on CPU, and a single slot makes the
# hard timeout below easy to reason about.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr")


def recognize_with_deadline(engine: OcrEngine, image: Image.Image, deadline: Deadline) -> list[OcrLine]:
    """Runs OCR but stops waiting when the deadline passes, so one slow page cannot hang a scan.
    Python cannot kill a running thread: an abandoned job finishes in the background."""
    deadline.check()
    future = _executor.submit(engine.recognize, image)
    try:
        return future.result(timeout=max(deadline.remaining(), 0.001))
    except FutureTimeout:
        future.cancel()
        raise ExtractionError("processing time limit exceeded") from None


def ocr_available() -> bool:
    try:
        get_ocr_engine()
        return True
    except ExtractionError:
        return False
