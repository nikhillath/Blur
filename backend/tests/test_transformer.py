"""Transformer detector mechanics, tested with a tiny randomly-initialised model.

These tests prove offsets, windowing and span reconstruction -- NOT model quality. Real-model
behaviour is covered by test_real_model.py when the weights are available locally.
"""
import string

import pytest

torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")

from app.ml.transformer import TransformerDetector  # noqa: E402

LABELS = ["O", "B-PER", "I-PER"]


@pytest.fixture(scope="module")
def tiny_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("tiny_ner")
    chars = list(string.ascii_letters + string.digits + ".,:;@-_+/'")
    vocab = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]"] + chars + ["##" + c for c in chars]
    (d / "vocab.txt").write_text("\n".join(vocab), encoding="utf-8")
    tok = transformers.BertTokenizerFast(vocab_file=str(d / "vocab.txt"), do_lower_case=False)
    cfg = transformers.BertConfig(
        vocab_size=len(vocab), hidden_size=32, num_hidden_layers=2, num_attention_heads=2,
        intermediate_size=64, max_position_embeddings=512, num_labels=3,
        id2label=dict(enumerate(LABELS)), label2id={l: i for i, l in enumerate(LABELS)},
    )
    model = transformers.BertForTokenClassification(cfg)
    model.save_pretrained(d)
    tok.save_pretrained(d)
    return str(d)


def build(tiny_dir, label_bias=None, **kw):
    det = TransformerDetector(tiny_dir, min_score=0.0, **kw)
    if label_bias is not None:  # force the (otherwise random) prediction so spans are deterministic
        with torch.no_grad():
            det.model.classifier.weight.zero_()
            det.model.classifier.bias.copy_(torch.tensor(label_bias))
    return det


def test_words_have_valid_offsets(tiny_dir):
    det = build(tiny_dir)
    text = "Customer Name: Riya Sharma, phone 9876543210\nSecond line here"
    words = det.predict_words(text)
    assert words == sorted(words, key=lambda w: w.start)
    covered = []
    for w in words:
        piece = text[w.start : w.end]
        assert piece and piece == piece.strip() and w.label in LABELS and 0 <= w.score <= 1
        covered.append(piece)
    assert "Riya" in covered and "Sharma," in covered or "Sharma" in covered


def test_forced_labels_reconstruct_exact_spans(tiny_dir):
    det = build(tiny_dir, label_bias=[0.0, 0.0, 12.0])  # every word -> I-PER
    text = "Riya Sharma\nAnita Rao"
    out = det.detect(text)
    assert [text[d.start : d.end] for d in out] == ["Riya Sharma", "Anita Rao"]  # split at the newline
    assert all(d.entity_type == "PERSON" and d.method == "transformer" and d.score > 0.9 for d in out)
    assert all(d.detector.startswith("transformer:") for d in out)


def test_all_outside_gives_no_detections_and_empty_text(tiny_dir):
    det = build(tiny_dir, label_bias=[12.0, 0.0, 0.0])
    assert det.detect("Riya Sharma") == []
    assert det.detect("") == [] and det.detect("   \n ") == []


def test_long_text_windows_cover_every_word_once(tiny_dir):
    det = build(tiny_dir, label_bias=[0.0, 12.0, 0.0], max_length=24, stride=8, batch_size=3)
    words = [f"w{i:03d}" for i in range(120)]
    text = " ".join(words)
    preds = det.predict_words(text)
    got = [text[p.start : p.end] for p in preds]
    assert got == words  # no word lost, none duplicated, none cut in half at window edges


def test_deadline_is_honoured(tiny_dir):
    from app.document_processing.types import Deadline, ExtractionError

    det = build(tiny_dir)
    with pytest.raises(ExtractionError):
        det.detect("word " * 200, Deadline(-1))


def test_min_score_filters(tiny_dir):
    det = build(tiny_dir, label_bias=[0.0, 12.0, 0.0])
    det.min_score = 1.01
    assert det.detect("Riya Sharma") == []
