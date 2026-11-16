"""Transformer token-classification detector (Hugging Face).

Works with any ``AutoModelForTokenClassification`` checkpoint whose labels are BIO-style
(``B-PER`` ...) or bare (``PER``). A checkpoint fine-tuned on the PII taxonomy (Phase 6) plugs in by
changing ``NER_MODEL``; labels are mapped through ``taxonomy.MODEL_LABEL_MAP``.
"""
import json
import logging
import threading
import time
from pathlib import Path

from app.core.config import get_settings
from app.document_processing.types import Deadline
from app.ml.spans import WordPred, bio_to_spans, merge_window_words
from app.ml.taxonomy import MODEL_LABEL_MAP, Detection

logger = logging.getLogger("pii_sentinel.transformer")

_EDGE_PUNCT = frozenset(".,;:!?()[]{}<>\"'")


class TransformerDetector:
    def __init__(
        self,
        model_name: str,
        *,
        max_length: int = 256,
        stride: int = 64,
        batch_size: int = 8,
        min_score: float = 0.5,
        local_files_only: bool = False,
        model=None,
        tokenizer=None,
        granularity: str | None = None,
    ):
        import torch  # lazy: keeps rules-only deployments light
        from transformers import AutoModelForTokenClassification, AutoTokenizer

        self._torch = torch
        self.model_name = model_name
        self.max_length, self.stride, self.batch_size, self.min_score = max_length, stride, batch_size, min_score
        meta = _read_meta(model_name)
        # "word": first sub-token decides a whole word (generic NER checkpoints).
        # "token": every sub-token carries a label, giving character-exact spans (our fine-tuned models).
        self.granularity = granularity or meta.get("granularity", "word")
        self.tokenizer = tokenizer or AutoTokenizer.from_pretrained(model_name, use_fast=True, local_files_only=local_files_only)
        if not self.tokenizer.is_fast:
            raise RuntimeError("a fast tokenizer is required for character offsets")
        self.model = model or AutoModelForTokenClassification.from_pretrained(model_name, local_files_only=local_files_only)
        self.model.eval()
        self.id2label = {int(k): v for k, v in self.model.config.id2label.items()}
        self._lock = threading.Lock()
        self.version = f"transformer:{meta.get('name', model_name)}"

    def predict_words(self, text: str, deadline: Deadline | None = None) -> list[WordPred]:
        """Word-level predictions (first sub-token decides a word's label), merged across windows."""
        if not text.strip():
            return []
        torch = self._torch
        enc = self.tokenizer(
            text, return_offsets_mapping=True, truncation=True, max_length=self.max_length,
            stride=self.stride, return_overflowing_tokens=True, padding=False,
        )
        n_windows = len(enc["input_ids"])
        windows: list[list[WordPred]] = []
        for b0 in range(0, n_windows, self.batch_size):
            if deadline:
                deadline.check()
            idx = range(b0, min(b0 + self.batch_size, n_windows))
            feats = [{"input_ids": enc["input_ids"][i], "attention_mask": enc["attention_mask"][i]} for i in idx]
            batch = self.tokenizer.pad(feats, return_tensors="pt")
            with self._lock, torch.inference_mode():
                probs = torch.softmax(self.model(**batch).logits, dim=-1)
            conf, pred = probs.max(dim=-1)
            for row, i in enumerate(idx):
                windows.append(self._window_words(enc, i, pred[row].tolist(), conf[row].tolist(), n_windows))
        return merge_window_words(windows)

    def _window_words(self, enc, i: int, pred: list[int], conf: list[float], n_windows: int) -> list[WordPred]:
        word_ids = enc.word_ids(batch_index=i)
        offsets = enc["offset_mapping"][i]
        if self.granularity == "token":
            return [
                WordPred(offsets[t][0], offsets[t][1], self.id2label[pred[t]], float(conf[t]))
                for t, w in enumerate(word_ids)
                if w is not None and offsets[t][1] > offsets[t][0]
            ]
        first: dict[int, int] = {}  # word id -> first token index
        last: dict[int, int] = {}
        for t, w in enumerate(word_ids):
            if w is None:
                continue
            first.setdefault(w, t)
            last[w] = t
        ordered = sorted(first)
        words: list[WordPred] = []
        for pos, w in enumerate(ordered):
            # Words touching a window edge may be cut in half; the neighbouring window covers them.
            if n_windows > 1 and ((i > 0 and pos == 0) or (i < n_windows - 1 and pos == len(ordered) - 1)):
                continue
            t0, t1 = first[w], last[w]
            start, end = offsets[t0][0], offsets[t1][1]
            if end > start:
                words.append(WordPred(start, end, self.id2label[pred[t0]], float(conf[t0])))
        return words

    def detect(self, text: str, deadline: Deadline | None = None) -> list[Detection]:
        out: list[Detection] = []
        for sp in bio_to_spans(self.predict_words(text, deadline), text):
            start, end = sp.start, sp.end
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            if self.granularity == "word":  # a word label covers the whole word: drop edge punctuation
                while start < end and text[start] in _EDGE_PUNCT:
                    start += 1
                while end > start and text[end - 1] in _EDGE_PUNCT:
                    end -= 1
            if end - start >= 2 and any(c.isalnum() for c in text[start:end]) and sp.score >= self.min_score:
                out.append(Detection(MODEL_LABEL_MAP[sp.raw_type], start, end, sp.score, "transformer", self.version))
        return out


def _read_meta(model_name: str) -> dict:
    """Metadata written by ml/scripts/train.py next to a fine-tuned checkpoint (absent for hub models)."""
    path = Path(model_name) / "pii_ner_meta.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return {}


_load_lock = threading.Lock()
_detector: "TransformerDetector | None" = None
_failed_at: float | None = None
RETRY_AFTER_SECONDS = 60


def get_transformer_detector() -> "TransformerDetector | None":
    """Returns the configured detector, or None (rules-only mode) if disabled or it cannot load.

    Thread-safe: the startup warm-up and the first scan may call this at the same moment, and importing
    ``transformers`` from two threads at once can fail. Whoever arrives second waits for the first to
    finish. A successful load is kept; a failure is retried after a short back-off instead of being
    remembered forever (so one unlucky start can not leave the app silently rules-only).
    """
    global _detector, _failed_at
    s = get_settings()
    if not s.ner_model:
        return None
    with _load_lock:
        if _detector is not None:
            return _detector
        if _failed_at is not None and time.monotonic() - _failed_at < RETRY_AFTER_SECONDS:
            return None
        try:
            _detector = TransformerDetector(
                s.ner_model, max_length=s.ner_max_length, stride=s.ner_stride,
                batch_size=s.ner_batch_size, min_score=s.ner_min_score, local_files_only=s.ner_local_only,
            )
            _failed_at = None
            logger.info("loaded NER model %s", s.ner_model)
            return _detector
        except Exception:
            _failed_at = time.monotonic()
            logger.exception("could not load NER model %s; falling back to rules-only", s.ner_model)
            return None


def reset_transformer_detector() -> None:
    """For tests and hot-reload: forget the loaded model and any recorded failure."""
    global _detector, _failed_at
    with _load_lock:
        _detector, _failed_at = None, None
