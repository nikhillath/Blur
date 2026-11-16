"""Behaviour of the real pretrained NER model. Skipped when the weights are not in the local cache
(download once: see docs/ml-pipeline.md). Qualitative smoke checks only -- NOT an accuracy evaluation;
that is the job of the Phase 6 evaluation pipeline on held-out data.
"""
import pytest

pytest.importorskip("torch")
pytest.importorskip("transformers")

from app.ml.transformer import TransformerDetector  # noqa: E402

MODEL = "Davlan/xlm-roberta-base-ner-hrl"


@pytest.fixture(scope="module")
def det():
    try:
        return TransformerDetector(MODEL, local_files_only=True)
    except Exception as exc:  # weights not downloaded
        pytest.skip(f"{MODEL} not available locally: {type(exc).__name__}")


def spans(det, text, etype):
    return sorted(text[d.start : d.end] for d in det.detect(text) if d.entity_type == etype)


def test_finds_person_names_with_exact_offsets(det):
    text = "Customer Name: Riya Sharma\nMet Anita Rao in Pune. Contact Satya Nadella of Microsoft.\n"
    assert spans(det, text, "PERSON") == ["Anita Rao", "Riya Sharma", "Satya Nadella"]


def test_edge_punctuation_is_trimmed(det):
    text = "Le client Jean Dupont habite a Paris."
    assert "Jean Dupont" in spans(det, text, "PERSON")
    assert all(not s.endswith(".") for s in spans(det, text, "ADDRESS"))


def test_locations_are_down_weighted(det):
    out = det.detect("She moved to Bhubaneswar last year.")
    loc = [d for d in out if d.entity_type == "ADDRESS"]
    assert loc and all(d.score <= 0.6 for d in loc)  # a city is not a street address


def test_no_names_in_plain_text(det):
    assert det.detect("The quarterly report shows revenue grew 12% across regions.") == []


def test_hybrid_pipeline_with_real_model(det, monkeypatch):
    from app.ml import pipeline

    monkeypatch.setattr(pipeline, "get_transformer_detector", lambda: det)
    text = (
        "Customer Name: Riya Sharma\nEmail: riya.sharma@example.com\nPhone: +91 98765 43210\n"
        "Notes: spoke with Anita Rao about the renewal.\n"
    )
    out = pipeline.run_detection(text, None)
    assert out.mode == "hybrid" and MODEL in out.version
    by = {(e.detection.entity_type, e.value): e.detection for e in out.entities}
    riya = by[("PERSON", "Riya Sharma")]
    assert riya.method == "hybrid" and len(riya.provenance) == 2  # rule and model agreed
    assert ("PERSON", "Anita Rao") in by  # the model found what no rule could
    assert ("EMAIL", "riya.sharma@example.com") in by and ("PHONE", "+91 98765 43210") in by
    ordered = sorted((e.detection.start, e.detection.end) for e in out.entities)
    assert all(a[1] <= b[0] for a, b in zip(ordered, ordered[1:]))
