"""Normalise the raw datasets, build leakage-safe splits, and write processed JSONL + statistics.

Usage:  python ../ml/scripts/prepare.py          (from backend/, venv active)

Split design
  train       : Maskara `train` minus validation template families  +  IndiaPII documents in buckets 0-69
  validation  : Maskara `train` held-out template families          +  IndiaPII documents in buckets 70-79
                (the ONLY data used for model selection / early stopping)
  test_*      : never used for training or selection
                  maskara_template_disjoint, maskara_real_world  (official held-out splits)
                  indiapii                                        (buckets 80-99)
                  ylemis                                          (whole benchmark; evaluation-only licence intent)
Groups (Maskara template family / IndiaPII document) never straddle splits; exact-duplicate texts that
appear in any test set are removed from train/validation.
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pii_ml.dataio import (  # noqa: E402
    Report, bucket, load_indiapii, load_maskara, load_ylemis, text_key, write_jsonl,
)

RAW, OUT = ROOT / "data" / "raw", ROOT / "data" / "processed"
VAL_FAMILY_PERCENT = 8  # share of Maskara template families held out for validation
INDIAPII_TRAIN_BELOW, INDIAPII_VAL_BELOW = 70, 80


def stats(rows: list[dict]) -> dict:
    ents = Counter(e["type"] for r in rows for e in r["entities"])
    return {
        "documents": len(rows),
        "documents_without_entities": sum(1 for r in rows if not r["entities"]),
        "entities": sum(ents.values()),
        "entities_by_type": dict(sorted(ents.items(), key=lambda kv: -kv[1])),
        "languages": dict(Counter(r["lang"] for r in rows).most_common()),
        "domains": dict(Counter(r["domain"] for r in rows).most_common(12)),
        "mean_chars": round(sum(len(r["text"]) for r in rows) / max(len(rows), 1), 1),
    }


def main() -> int:
    reports: dict[str, Report] = {}

    def load(name, fn, path):
        reports[name] = Report()
        return fn(path, reports[name])

    maskara_train = load("maskara_train", load_maskara, RAW / "maskara" / "train-00000-of-00001.parquet")
    maskara_td = load("maskara_template_disjoint", load_maskara, RAW / "maskara" / "template_disjoint_eval-00000-of-00001.parquet")
    maskara_rw = load("maskara_real_world", load_maskara, RAW / "maskara" / "real_world_eval-00000-of-00001.parquet")
    indiapii = load("indiapii", load_indiapii, RAW / "indiapii" / "indiapii-v1.0.jsonl")
    ylemis = load("ylemis", load_ylemis, RAW / "ylemis" / "benchmark.jsonl")

    # --- group-aware splits ----------------------------------------------------------
    families = sorted({r["group"] for r in maskara_train})
    val_families = {f for f in families if bucket("family:" + f) < VAL_FAMILY_PERCENT}
    train, val = [], []
    for r in maskara_train:
        (val if r["group"] in val_families else train).append(r)
    ip_train, ip_val, ip_test = [], [], []
    for r in indiapii:
        b = bucket("doc:" + r["id"])
        (ip_train if b < INDIAPII_TRAIN_BELOW else ip_val if b < INDIAPII_VAL_BELOW else ip_test).append(r)
    train += ip_train
    val += ip_val
    tests = {
        "maskara_template_disjoint": maskara_td, "maskara_real_world": maskara_rw,
        "indiapii": ip_test, "ylemis": ylemis,
    }

    # --- leakage control ---------------------------------------------------------------
    test_texts = {text_key(r["text"]) for rows in tests.values() for r in rows}
    removed = Counter()
    for name, rows in (("train", train), ("validation", val)):
        keep = [r for r in rows if text_key(r["text"]) not in test_texts]
        removed[name] = len(rows) - len(keep)
        rows[:] = keep
    train_texts = {text_key(r["text"]) for r in train}
    val_dupes = sum(1 for r in val if text_key(r["text"]) in train_texts)
    val[:] = [r for r in val if text_key(r["text"]) not in train_texts]

    leakage = {
        "train_val_group_overlap": len({r["group"] for r in train} & {r["group"] for r in val}),
        "train_test_id_overlap": len({r["id"] for r in train} & {r["id"] for rows in tests.values() for r in rows}),
        "train_test_text_overlap": len(train_texts & test_texts),
        "val_test_text_overlap": len({text_key(r["text"]) for r in val} & test_texts),
        "removed_from_train_for_test_duplicates": removed["train"],
        "removed_from_validation_for_test_duplicates": removed["validation"],
        "removed_from_validation_for_train_duplicates": val_dupes,
    }
    if any(leakage[k] for k in ("train_val_group_overlap", "train_test_id_overlap", "train_test_text_overlap", "val_test_text_overlap")):
        print(json.dumps(leakage, indent=2))
        raise SystemExit("LEAKAGE DETECTED - refusing to write splits")

    # --- write ---------------------------------------------------------------------------
    write_jsonl(OUT / "train.jsonl", train)
    write_jsonl(OUT / "validation.jsonl", val)
    for name, rows in tests.items():
        write_jsonl(OUT / f"test_{name}.jsonl", rows)
    summary = {
        "split_policy": {
            "validation_template_family_percent": VAL_FAMILY_PERCENT,
            "indiapii_buckets": {"train": f"0-{INDIAPII_TRAIN_BELOW - 1}", "validation": f"{INDIAPII_TRAIN_BELOW}-{INDIAPII_VAL_BELOW - 1}", "test": f"{INDIAPII_VAL_BELOW}-99"},
            "validation_families": sorted(val_families),
        },
        "leakage_checks": leakage,
        "normalisation_reports": {k: dict(v) for k, v in reports.items()},
        "splits": {"train": stats(train), "validation": stats(val), **{f"test_{k}": stats(v) for k, v in tests.items()}},
    }
    (OUT / "stats.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    for name, s in summary["splits"].items():
        print(f"{name:34} docs={s['documents']:>7}  entities={s['entities']:>7}  no-entity docs={s['documents_without_entities']:>6}")
    print("leakage:", leakage)
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
