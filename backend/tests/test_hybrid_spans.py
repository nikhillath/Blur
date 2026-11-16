from app.document_processing.locate import locate
from app.ml.hybrid import resolve
from app.ml.spans import WordPred, bio_to_spans, merge_window_words
from app.ml.taxonomy import Detection


def D(t, s, e, score, method="rule", det=None):
    return Detection(t, s, e, score, method, det or f"{method}:{t}")


# ---------------------------------------------------------------- resolution policy
def test_empty_and_below_threshold():
    assert resolve([]) == []
    assert resolve([D("EMAIL", 0, 5, 0.1)], 0.3) == []


def test_identical_spans_merge_with_max_score_and_provenance():
    out = resolve([D("PERSON", 0, 11, 0.7, "label-rule"), D("PERSON", 0, 11, 0.93, "transformer", "transformer:m")])
    assert len(out) == 1
    d = out[0]
    assert d.score == 0.93 and d.method == "hybrid" and d.detector == "transformer:m"
    assert len(d.provenance) == 2  # both original predictions preserved
    assert 0.93 <= d.score <= 1  # not inflated by agreement


def test_same_type_overlap_becomes_union():
    out = resolve([D("PERSON", 0, 4, 0.9, "transformer"), D("PERSON", 2, 11, 0.7, "label-rule")])
    assert [(d.start, d.end) for d in out] == [(0, 11)] and out[0].score == 0.9


def test_adjacent_same_type_stay_separate():
    out = resolve([D("PERSON", 0, 4, 0.9), D("PERSON", 4, 9, 0.9)])
    assert len(out) == 2


def test_cross_type_conflict_keeps_highest_score_and_records_loser():
    out = resolve([D("PHONE", 5, 15, 0.55), D("BANK_ACCOUNT", 5, 15, 0.8)])
    assert len(out) == 1 and out[0].entity_type == "BANK_ACCOUNT"
    assert any("suppressed" in p and p["suppressed"]["type"] == "PHONE" for p in out[0].provenance)


def test_tie_prefers_longer_span_then_rule():
    out = resolve([D("PERSON", 0, 5, 0.8, "transformer"), D("ADDRESS", 0, 9, 0.8, "label-rule")])
    assert out[0].entity_type == "ADDRESS"
    out = resolve([D("PERSON", 0, 5, 0.8, "transformer"), D("ADDRESS", 0, 5, 0.8, "rule")])
    assert out[0].entity_type == "ADDRESS"


def test_non_overlapping_all_kept_and_sorted():
    out = resolve([D("EMAIL", 20, 30, 0.9), D("PHONE", 0, 10, 0.9), D("PAN", 40, 50, 0.9)])
    assert [d.entity_type for d in out] == ["PHONE", "EMAIL", "PAN"]


def test_resolution_never_leaves_overlaps():
    import random

    rnd = random.Random(7)
    types = ["PERSON", "EMAIL", "PHONE", "ADDRESS"]
    for _ in range(200):
        ds = []
        for _ in range(rnd.randint(1, 12)):
            s = rnd.randint(0, 50)
            ds.append(D(rnd.choice(types), s, s + rnd.randint(1, 15), rnd.random(), rnd.choice(["rule", "transformer"])))
        out = resolve(ds, 0.0)
        for a, b in zip(out, out[1:]):
            assert a.end <= b.start


# ---------------------------------------------------------------- BIO decoding
def W(text, s, label, score=0.9):
    return WordPred(s, s + len(text), label, score)


def test_bio_basic_sequence():
    text = "Riya Sharma lives in Pune"
    words = [W("Riya", 0, "B-PER"), W("Sharma", 5, "I-PER", 0.8), W("lives", 12, "O"), W("in", 18, "O"), W("Pune", 21, "B-LOC")]
    spans = bio_to_spans(words, text)
    assert [(s.raw_type, text[s.start : s.end]) for s in spans] == [("PER", "Riya Sharma"), ("LOC", "Pune")]
    assert abs(spans[0].score - 0.85) < 1e-9
    assert abs(spans[1].score - 0.9 * 0.6) < 1e-9  # LOC is down-weighted


def test_bio_edge_cases():
    text = "a b c d e f"
    # I- without a B- opens a span; type change closes; unmapped ORG ignored; two B- are two spans
    words = [W("a", 0, "I-PER"), W("b", 2, "I-PER"), W("c", 4, "I-LOC"), W("d", 6, "B-ORG"), W("e", 8, "B-PER"), W("f", 10, "B-PER")]
    spans = bio_to_spans(words, text)
    assert [(s.raw_type, text[s.start : s.end]) for s in spans] == [("PER", "a b"), ("LOC", "c"), ("PER", "e"), ("PER", "f")]


def test_bio_span_does_not_cross_newline():
    text = "Riya\nSharma"
    spans = bio_to_spans([W("Riya", 0, "B-PER"), W("Sharma", 5, "I-PER")], text)
    assert len(spans) == 2


def test_bio_bare_labels_and_empty():
    assert bio_to_spans([], "") == []
    text = "Riya Sharma"
    spans = bio_to_spans([W("Riya", 0, "PER"), W("Sharma", 5, "PER")], text)
    assert len(spans) == 1 and text[spans[0].start : spans[0].end] == "Riya Sharma"


def test_merge_window_words_keeps_best():
    a = [WordPred(0, 4, "O", 0.6), WordPred(5, 9, "B-PER", 0.9)]
    b = [WordPred(5, 9, "O", 0.7), WordPred(10, 14, "O", 0.99)]
    merged = merge_window_words([a, b])
    assert [(w.start, w.label) for w in merged] == [(0, "O"), (5, "B-PER"), (10, "O")]


# ---------------------------------------------------------------- locate
PAGE_MAP = {
    "coord_space": "pdf_points",
    "segments": [
        [0, 5, "word", 1, 10, 100, 60, 112],     # "Email"
        [6, 11, "word", 1, 70, 100, 120, 112],   # "a@b.c"
        [12, 16, "word", 1, 10, 130, 50, 142],   # next line
        [20, 24, "para", "body.p.1"],
    ],
}


def test_locate_merges_words_on_one_line():
    page, rects, refs = locate(PAGE_MAP, 0, 11)
    assert page == 1 and len(rects) == 1 and refs is None
    assert rects[0] == [1, 10, 100, 120, 112]


def test_locate_multiline_and_partial_segment():
    page, rects, _ = locate(PAGE_MAP, 6, 16)
    assert len(rects) == 2
    page, rects, _ = locate(PAGE_MAP, 7, 9)  # 2 of 5 chars of the second word
    assert rects == [[1, 70 + 50 * 1 / 5, 100, 70 + 50 * 3 / 5, 112]]


def test_locate_docx_refs_and_misses():
    assert locate(PAGE_MAP, 21, 22)[2] == ["body.p.1"]
    assert locate(PAGE_MAP, 100, 110) == (None, None, None)
    assert locate(None, 0, 5) == (None, None, None)
