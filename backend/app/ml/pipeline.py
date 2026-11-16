"""Detection pipeline: rules + optional Transformer -> conflict resolution -> located, masked entities."""
import logging
from dataclasses import dataclass

from app.core.config import get_settings
from app.document_processing.locate import locate
from app.document_processing.types import Deadline
from app.ml.hybrid import resolve
from app.ml.rules import RULES_VERSION, detect_rules
from app.ml.taxonomy import Detection, mask_value
from app.ml.transformer import get_transformer_detector

logger = logging.getLogger("pii_sentinel.detection")


@dataclass
class EntityDraft:
    detection: Detection
    value: str
    masked: str
    page: int | None
    bbox: list | None


@dataclass
class DetectionOutput:
    entities: list[EntityDraft]
    mode: str  # "rules-only" | "hybrid"
    version: str
    config: dict


def run_detection(text: str, page_map: dict | None, deadline: Deadline | None = None) -> DetectionOutput:
    s = get_settings()
    detections = detect_rules(text)
    n_rule = len(detections)
    config: dict = {"rules": RULES_VERSION, "min_confidence": s.min_confidence}

    ner = get_transformer_detector()
    n_ner = 0
    if ner is not None:
        window = text[: s.ner_max_chars]
        if len(window) < len(text):
            config["ner_truncated_at_chars"] = len(window)  # cost bound: be explicit, never silent
        found = ner.detect(window, deadline)
        n_ner = len(found)
        detections.extend(found)
        config["ner_model"] = ner.model_name
    elif s.ner_model:
        config["ner_error"] = "configured model could not be loaded; ran rules-only"

    resolved = resolve(detections, s.min_confidence)
    dropped_cap = max(len(resolved) - s.max_entities_per_scan, 0)
    if dropped_cap:
        resolved = sorted(resolved, key=lambda d: -d.score)[: s.max_entities_per_scan]
        resolved.sort(key=lambda d: d.start)
        config["entities_dropped_over_cap"] = dropped_cap

    drafts = []
    for d in resolved:
        value = text[d.start : d.end]
        page, rects, _refs = locate(page_map, d.start, d.end)
        drafts.append(EntityDraft(d, value, mask_value(d.entity_type, value), page, rects))
    config["candidates"] = {"rule": n_rule, "transformer": n_ner, "after_resolution": len(drafts)}
    version = RULES_VERSION + (f"+{ner.version}" if ner else "")
    return DetectionOutput(drafts, "hybrid" if ner else "rules-only", version, config)
