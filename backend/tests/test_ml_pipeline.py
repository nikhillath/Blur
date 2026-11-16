"""Tests for the training/evaluation library in ml/pii_ml (no model weights needed)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ml"))

from app.ml.spans import WordPred, bio_to_spans  # noqa: E402
from app.ml.taxonomy import ENTITY_TYPES  # noqa: E402
from pii_ml import labelmap  # noqa: E402
from pii_ml.bio import align_labels  # noqa: E402
from pii_ml.dataio import Report, bucket, normalise, text_key  # noqa: E402
from pii_ml.labelmap import ID2LABEL, IGNORE, LABEL2ID, LABELS, map_label  # noqa: E402
from pii_ml.metrics import evaluate, match_doc  # noqa: E402


# ------------------------------------------------------------------ label maps
def test_label_inventory_is_bio_over_taxonomy_in_stable_order():
    assert LABELS[0] == "O" and len(LABELS) == 1 + 2 * len(ENTITY_TYPES)
    assert LABELS[1:3] == [f"B-{ENTITY_TYPES[0]}", f"I-{ENTITY_TYPES[0]}"]
    assert all(ID2LABEL[LABEL2ID[l]] == l for l in LABELS)


@pytest.mark.parametrize("source", ["maskara", "indiapii", "ylemis"])
def test_every_mapping_target_is_a_taxonomy_type(source):
    for label, target in labelmap.SOURCE_MAPS[source].items():
        assert target is None or target == IGNORE or target in ENTITY_TYPES, (source, label)


def test_unknown_source_label_is_an_error_not_a_guess():
    with pytest.raises(KeyError):
        map_label("maskara", "SOMETHING_NEW")


def test_government_and_business_ids_stay_distinct():
    assert map_label("indiapii", "AADHAAR_MASKED") == "AADHAAR"
    # account_id values are internal ids (policy_/cust_...), not bank account numbers: post-hoc corrected mapping
    assert map_label("ylemis", "account_id") == "OTHER_IDENTIFIER"
    assert map_label("ylemis", "organization") == IGNORE


# ------------------------------------------------------------------ normalisation
def rec(source, text, ents, label_key="label"):
    rep = Report()
    out = normalise(source, {"entities": ents}, rep, id_="x", text=text, group="g", lang="en", domain="d", label_key=label_key)
    return out, rep


def test_normalise_maps_trims_and_counts():
    text = "Name: Riya  Mail: a@b.co"
    out, rep = rec("maskara", text, [
        {"start": 6, "end": 12, "label": "PERSON_NAME"},  # "Riya  " is trimmed
        {"start": 19, "end": 24, "label": "EMAIL"},
        {"start": 3, "end": 999, "label": "PHONE"},       # out of range
        {"start": 5, "end": 5, "label": "PHONE"},         # empty
    ])
    assert [(e["start"], e["end"], e["type"]) for e in out["entities"]] == [(6, 10, "PERSON"), (19, 24, "EMAIL")]
    assert text[6:10] == "Riya" and rep["malformed_span"] == 2


def test_overlap_policy_keeps_longest_span():
    text = "0123456789"
    out, rep = rec("maskara", text, [{"start": 0, "end": 4, "label": "PHONE"}, {"start": 2, "end": 9, "label": "AADHAAR"}])
    assert [(e["start"], e["end"], e["type"]) for e in out["entities"]] == [(2, 9, "AADHAAR")]
    assert rep["overlap_dropped"] == 1


def test_hard_negatives_and_ignored_kinds():
    text = "ORD-12345 Acme Corp riya@x.co"
    out, rep = rec("indiapii", text, [
        {"start": 0, "end": 9, "label": "ORDER_ID_SHAPED", "value_class": "hard_negative"},
        {"start": 20, "end": 29, "label": "UPI_VPA", "value_class": "positive"},
    ])
    assert [e["type"] for e in out["entities"]] == ["UPI_ID"] and rep["hard_negative_spans"] == 1
    out, _ = rec("ylemis", text, [{"type": "organization", "start": 10, "end": 19}], label_key="type")
    assert out["entities"] == [] and out["ignore"] == [{"start": 10, "end": 19, "type": "organization"}]


def test_empty_text_is_dropped():
    out, rep = rec("maskara", "   ", [])
    assert out is None and rep["empty_text"] == 1


def test_bucket_is_stable_and_text_key_normalises():
    assert bucket("doc:abc") == bucket("doc:abc") and 0 <= bucket("x", 100) < 100
    assert text_key("Hello   World") == text_key("hello world")


# ------------------------------------------------------------------ BIO alignment
def test_align_labels_basic_and_special_tokens():
    #           <s>      "Name"  ":"     "Riya"   "Sharma"   </s>
    offsets = [(0, 0), (0, 4), (4, 5), (6, 10), (11, 17), (0, 0)]
    ents = [{"start": 6, "end": 17, "type": "PERSON"}]
    labels, lost = align_labels(offsets, ents)
    assert labels == [-100, LABEL2ID["O"], LABEL2ID["O"], LABEL2ID["B-PERSON"], LABEL2ID["I-PERSON"], -100]
    assert lost == 0


def test_align_labels_two_adjacent_entities_get_two_begins():
    offsets = [(0, 3), (4, 7)]
    ents = [{"start": 0, "end": 3, "type": "PERSON"}, {"start": 4, "end": 7, "type": "PERSON"}]
    labels, _ = align_labels(offsets, ents)
    assert labels == [LABEL2ID["B-PERSON"], LABEL2ID["B-PERSON"]]


def test_align_labels_boundary_straddling_token_and_lost_entity():
    # the token spans 10 characters but only 3 belong to the entity: below the 50% rule -> O, entity "lost"
    labels, lost = align_labels([(0, 10)], [{"start": 7, "end": 10, "type": "PHONE"}])
    assert labels == [LABEL2ID["O"]] and lost == 1


def test_align_then_decode_roundtrip_recovers_exact_spans():
    text = "Mail riya.sharma@example.com or call +91 98765 43210 now"
    ents = [{"start": 5, "end": 28, "type": "EMAIL"}, {"start": 37, "end": 52, "type": "PHONE"}]
    offsets, i = [], 0
    for w in text.split(" "):
        offsets.append((text.index(w, i), text.index(w, i) + len(w)))
        i = offsets[-1][1]
    labels, _ = align_labels(offsets, ents)
    words = [WordPred(s, e, ID2LABEL[l], 0.9) for (s, e), l in zip(offsets, labels)]
    got = [(text[s.start : s.end], s.raw_type) for s in bio_to_spans(words, text)]
    assert got == [("riya.sharma@example.com", "EMAIL"), ("+91 98765 43210", "PHONE")]


# ------------------------------------------------------------------ metrics
def E(s, e, t):
    return {"start": s, "end": e, "type": t}


def doc(id_, entities, ignore=(), lang="en", domain="d"):
    return {"id": id_, "text": "x" * 100, "entities": entities, "ignore": list(ignore), "lang": lang, "domain": domain}


def test_strict_vs_relaxed_matching():
    gold = [E(0, 10, "PHONE"), E(20, 30, "EMAIL"), E(40, 50, "PAN")]
    pred = [E(0, 10, "PHONE"), E(21, 30, "EMAIL"), E(60, 70, "PAN")]  # exact, boundary-off, spurious
    r = match_doc(gold, pred, [])
    assert len(r["strict_tp"]) == 1 and len(r["strict_fp"]) == 2 and len(r["strict_fn"]) == 2
    assert len(r["relaxed_tp"]) == 2 and len(r["relaxed_fp"]) == 1 and len(r["relaxed_fn"]) == 1


def test_wrong_type_is_never_a_match():
    r = match_doc([E(0, 10, "PHONE")], [E(0, 10, "BANK_ACCOUNT")], [])
    assert not r["strict_tp"] and not r["relaxed_tp"]


def test_ignore_regions_neutralise_overlapping_predictions():
    r = match_doc([], [E(5, 15, "ADDRESS")], [{"start": 0, "end": 20}])
    assert r["strict_fp"] == [] and r["n_pred_scored"] == 0
    r = match_doc([E(5, 15, "ADDRESS")], [E(5, 15, "ADDRESS")], [{"start": 0, "end": 20}])
    assert len(r["strict_tp"]) == 1  # a correct prediction is still credited


def test_evaluate_aggregates_micro_macro_slices_and_false_positive_rate():
    docs = [
        doc("a", [E(0, 10, "PHONE"), E(20, 30, "EMAIL")], lang="hi"),
        doc("b", [E(0, 5, "PAN")], lang="en"),
        doc("c", [], lang="en"),  # clean document
        doc("d", [], lang="en"),  # clean document
    ]
    preds = {"a": [E(0, 10, "PHONE")], "b": [E(0, 5, "PAN"), E(50, 60, "PHONE")], "c": [E(1, 4, "PERSON")], "d": []}
    rep = evaluate(docs, preds)
    m = rep["strict"]["micro"]
    assert (m["tp"], m["fp"], m["fn"]) == (2, 2, 1)
    assert m["precision"] == 0.5 and round(m["recall"], 4) == 0.6667
    assert rep["strict"]["per_type"]["PHONE"]["fp"] == 1 and rep["strict"]["per_type"]["EMAIL"]["fn"] == 1
    assert rep["clean_document_false_positive_rate"] == {"clean_documents": 2, "with_any_prediction": 1, "rate": 0.5}
    assert rep["slices"]["lang=hi"]["tp"] == 1 and rep["slices"]["lang=en"]["fp"] == 2
    # macro F1 averages only types present in gold (PERSON, a pure false positive, is excluded)
    assert rep["strict"]["macro_f1"] == round((2 / 3 + 0.0 + 1.0) / 3, 4)  # PHONE has 1 FP -> F1 2/3
    assert rep["examples"]["false_negatives"][0]["type"] == "EMAIL"


def test_evaluate_with_no_predictions_and_no_gold():
    rep = evaluate([doc("a", [])], {})
    assert rep["strict"]["micro"]["f1"] == 0.0 and rep["clean_document_false_positive_rate"]["rate"] == 0.0
