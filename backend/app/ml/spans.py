"""Reconstruct entity spans from word-level token-classification output (framework independent)."""
from dataclasses import dataclass

from app.ml.taxonomy import LABEL_SCORE_FACTOR, MODEL_LABEL_MAP


@dataclass
class WordPred:
    start: int
    end: int
    label: str  # raw model label, e.g. "B-PER", "I-PER", "O"
    score: float  # softmax probability of ``label`` (not calibrated)


@dataclass
class Span:
    raw_type: str
    start: int
    end: int
    score: float


def split_label(label: str) -> tuple[str, str]:
    """'B-PER' -> ('B', 'PER'); 'PER' -> ('I', 'PER'); 'O' -> ('O', '')."""
    if label == "O":
        return "O", ""
    if len(label) > 2 and label[1] == "-" and label[0] in "BIES":
        prefix = "B" if label[0] in "BS" else "I"
        return prefix, label[2:]
    return "I", label


def bio_to_spans(words: list[WordPred], text: str) -> list[Span]:
    """Greedy BIO decoding over words sorted by position.

    * ``B-X`` opens a span; ``I-X`` continues one of the same type (or opens one if none is open).
    * ``O``, an unmapped type, or a newline between words closes the open span.
    * Span score = mean word probability, scaled by the label's reliability factor.
    """
    spans: list[Span] = []
    cur_type: str | None = None
    cur_start = cur_end = 0
    scores: list[float] = []

    def close() -> None:
        nonlocal cur_type, scores
        if cur_type is not None and scores:
            factor = LABEL_SCORE_FACTOR.get(cur_type, 1.0)
            spans.append(Span(cur_type, cur_start, cur_end, sum(scores) / len(scores) * factor))
        cur_type, scores = None, []

    for w in sorted(words, key=lambda w: w.start):
        prefix, raw = split_label(w.label)
        if prefix == "O" or raw not in MODEL_LABEL_MAP:
            close()
            continue
        continues = (
            prefix == "I" and cur_type == raw and "\n" not in text[cur_end : w.start]
        )
        if continues:
            cur_end = w.end
            scores.append(w.score)
        else:
            close()
            cur_type, cur_start, cur_end, scores = raw, w.start, w.end, [w.score]
    close()
    return spans


def merge_window_words(windows: list[list[WordPred]]) -> list[WordPred]:
    """Merge words predicted by overlapping windows; the highest-probability prediction wins."""
    best: dict[tuple[int, int], WordPred] = {}
    for words in windows:
        for w in words:
            key = (w.start, w.end)
            if key not in best or w.score > best[key].score:
                best[key] = w
    return sorted(best.values(), key=lambda w: w.start)
