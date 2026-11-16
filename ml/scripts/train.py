"""Fine-tune a multilingual Transformer for PII token classification.

Usage (from backend/, venv active):
    python ../ml/scripts/train.py --config ../ml/configs/train_xlmr.yaml            # full run
    python ../ml/scripts/train.py --config ../ml/configs/train_xlmr.yaml --benchmark # time 6 steps only

Design notes
* Token-level BIO labels over the application taxonomy (pii_ml.labelmap.LABELS); special tokens and padding
  carry -100 so the loss ignores them.
* Model selection uses ONLY the validation split (strict entity-level micro-F1). Test splits are never read here.
* Early stopping (patience) plus an optional wall-clock budget make CPU-only training predictable.
* The encoder can be initialised from any HF checkpoint; the classification head is always re-initialised.
"""
import argparse
import json
import math
import random
import sys
import time
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pii_ml.bio import align_labels  # noqa: E402
from pii_ml.dataio import read_jsonl  # noqa: E402
from pii_ml.labelmap import ID2LABEL, LABEL2ID, LABELS  # noqa: E402
from pii_ml.metrics import evaluate  # noqa: E402


def set_seed(seed: int) -> None:
    import numpy as np
    import torch

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def select_training_docs(rows: list[dict], cfg: dict, rng: random.Random) -> list[dict]:
    """Keep every document from small, type-rich sources (oversampled) and sample the large source."""
    small = [r for r in rows if r["source"] != "maskara"]
    large = [r for r in rows if r["source"] == "maskara"]
    budget = max(cfg["max_examples"] - len(small) * cfg["small_source_oversample"], 0)
    by_stratum: dict[tuple, list[dict]] = {}
    for r in large:
        by_stratum.setdefault((r["domain"], r.get("difficulty")), []).append(r)
    chosen: list[dict] = []
    if budget:
        frac = min(budget / max(len(large), 1), 1.0)
        for stratum in sorted(by_stratum, key=str):
            items = by_stratum[stratum]
            rng.shuffle(items)
            chosen += items[: max(1, round(len(items) * frac))]
        rng.shuffle(chosen)
        chosen = chosen[:budget]
    return chosen + small * cfg["small_source_oversample"]


def tokenise(rows: list[dict], tok, max_length: int, stride: int) -> tuple[list[dict], dict]:
    feats, lost, total_ents, windows = [], 0, 0, 0
    for r in rows:
        enc = tok(r["text"], return_offsets_mapping=True, truncation=True, max_length=max_length,
                  stride=stride, return_overflowing_tokens=True, padding=False)
        total_ents += len(r["entities"])
        for i in range(len(enc["input_ids"])):
            labels, miss = align_labels([tuple(o) for o in enc["offset_mapping"][i]], r["entities"], LABEL2ID)
            lost += miss
            windows += 1
            feats.append({"input_ids": enc["input_ids"][i], "labels": labels})
    return feats, {"windows": windows, "entities": total_ents, "entities_lost_in_alignment": lost}


def batches(feats: list[dict], batch_size: int, rng: random.Random, pad_id: int):
    import torch

    order = list(range(len(feats)))
    rng.shuffle(order)
    chunk = batch_size * 50  # sort by length inside chunks: less padding, still random batches
    groups = [sorted(order[i : i + chunk], key=lambda j: len(feats[j]["input_ids"])) for i in range(0, len(order), chunk)]
    all_batches = [g[i : i + batch_size] for g in groups for i in range(0, len(g), batch_size)]
    rng.shuffle(all_batches)
    for idxs in all_batches:
        width = max(len(feats[j]["input_ids"]) for j in idxs)
        ids = torch.full((len(idxs), width), pad_id, dtype=torch.long)
        lab = torch.full((len(idxs), width), -100, dtype=torch.long)
        att = torch.zeros((len(idxs), width), dtype=torch.long)
        for row, j in enumerate(idxs):
            n = len(feats[j]["input_ids"])
            ids[row, :n] = torch.tensor(feats[j]["input_ids"])
            lab[row, :n] = torch.tensor(feats[j]["labels"])
            att[row, :n] = 1
        yield {"input_ids": ids, "attention_mask": att, "labels": lab}


def validate(model, tok, val_docs: list[dict], cfg: dict) -> dict:
    from app.ml.transformer import TransformerDetector

    det = TransformerDetector("in-memory", model=model, tokenizer=tok, granularity="token",
                              max_length=256, stride=64, batch_size=8, min_score=cfg["eval_min_score"])
    preds = {d["id"]: [{"start": x.start, "end": x.end, "type": x.entity_type} for x in det.detect(d["text"])]
             for d in val_docs}
    model.train()
    return evaluate(val_docs, preds, max_examples=5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--benchmark", action="store_true", help="time a few optimisation steps and exit")
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    t = cfg["train"]

    import torch
    from transformers import AutoModelForTokenClassification, AutoTokenizer, get_linear_schedule_with_warmup

    torch.set_num_threads(cfg.get("threads", 4))
    set_seed(cfg["seed"])
    rng = random.Random(cfg["seed"])
    run_dir = ROOT / "artifacts" / cfg["run_name"]
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "train_config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    tok = AutoTokenizer.from_pretrained(cfg.get("tokenizer", cfg["base_model"]), use_fast=True, local_files_only=cfg.get("local_files_only", False))
    processed = ROOT / "data" / "processed"
    train_docs = select_training_docs(read_jsonl(processed / "train.jsonl"), t, rng)
    val_all = read_jsonl(processed / "validation.jsonl")
    random.Random(cfg["seed"] + 1).shuffle(val_all)
    val_docs = val_all[: t["eval_max_examples"]]
    feats, tstats = tokenise(train_docs, tok, cfg["max_length"], cfg["stride"])
    print(f"train docs={len(train_docs)} windows={tstats['windows']} "
          f"entities={tstats['entities']} lost_in_alignment={tstats['entities_lost_in_alignment']}", flush=True)
    type_counts = Counter(e["type"] for d in train_docs for e in d["entities"])

    model = AutoModelForTokenClassification.from_pretrained(
        cfg["base_model"], num_labels=len(LABELS), id2label=ID2LABEL, label2id=LABEL2ID,
        ignore_mismatched_sizes=True, local_files_only=cfg.get("local_files_only", False),
    )
    if t.get("freeze_embeddings"):  # 192M of XLM-R's 278M parameters are the word embeddings: freezing them
        for p in model.base_model.embeddings.word_embeddings.parameters():  # slashes optimiser cost on CPU
            p.requires_grad = False
    n_train = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"trainable parameters: {n_train / 1e6:.1f}M of {sum(p.numel() for p in model.parameters()) / 1e6:.1f}M", flush=True)
    model.train()
    no_decay = ("bias", "LayerNorm.weight", "layer_norm.weight")
    head_lr = t.get("head_lr", t["lr"])  # the new classification head starts from scratch: give it a larger step
    groups = []
    for is_head in (False, True):
        for decay in (True, False):
            params = [p for n, p in model.named_parameters() if p.requires_grad and n.startswith("classifier") == is_head
                      and (not any(k in n for k in no_decay)) == decay]
            if params:
                groups.append({"params": params, "weight_decay": t["weight_decay"] if decay else 0.0,
                               "lr": head_lr if is_head else t["lr"]})
    opt = torch.optim.AdamW(groups, lr=t["lr"])
    steps_per_epoch = math.ceil(len(feats) / t["batch_size"])
    total_steps = steps_per_epoch * t["epochs"]
    sched = get_linear_schedule_with_warmup(opt, int(total_steps * t["warmup_ratio"]), total_steps)
    pad_id = tok.pad_token_id

    log_path = run_dir / "train_log.jsonl"
    log = log_path.open("w", encoding="utf-8")
    best_f1, best_step, bad_evals, step = -1.0, 0, 0, 0
    history, losses = [], []
    started = time.time()
    stop_reason = "completed"

    def save_best() -> None:
        best = run_dir / "best"
        model.save_pretrained(best)
        tok.save_pretrained(best)
        (best / "pii_ner_meta.json").write_text(json.dumps({
            "name": cfg["run_name"], "granularity": "token", "base_model": cfg["base_model"], "tokenizer": cfg.get("tokenizer", cfg["base_model"]),
            "labels": LABELS, "max_length": cfg["max_length"], "stride": cfg["stride"],
            "best_step": step, "validation_micro_f1": best_f1, "seed": cfg["seed"],
            "data": json.loads((ROOT / "data" / "raw" / "manifest.json").read_text(encoding="utf-8")),
            "train_docs": len(train_docs), "train_entities_by_type": dict(type_counts),
            "note": "Trained on synthetic data only; see ml/evaluation for held-out results.",
        }, indent=2), encoding="utf-8")

    done = False
    for epoch in range(t["epochs"]):
        for batch in batches(feats, t["batch_size"], rng, pad_id):
            loss = model(**batch).loss
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), t["grad_clip"])
            opt.step(); sched.step(); opt.zero_grad(set_to_none=True)
            step += 1
            losses.append(loss.item())
            elapsed = time.time() - started
            if args.benchmark and step == 2:
                bench_start = time.time()  # steps 1-2 include one-off warm-up cost
            if args.benchmark and step >= 8:
                per = (time.time() - bench_start) / (step - 2)
                print(f"BENCHMARK: {per:.2f} s/step at batch={t['batch_size']} len<={cfg['max_length']}; "
                      f"{total_steps} steps would take {per * total_steps / 3600:.2f} h", flush=True)
                return 0
            if step % t["log_every"] == 0:
                rec = {"step": step, "epoch": epoch, "loss": round(sum(losses[-t["log_every"]:]) / t["log_every"], 4),
                       "lr": sched.get_last_lr()[0], "elapsed_s": round(elapsed), "eta_s": round(elapsed / step * (total_steps - step))}
                print(json.dumps(rec), flush=True); log.write(json.dumps(rec) + "\n"); log.flush()
            if step % t["eval_every_steps"] == 0 or step == total_steps:
                m = validate(model, tok, val_docs, cfg)
                f1 = m["strict"]["micro"]["f1"]
                rec = {"step": step, "eval": {"strict_micro": m["strict"]["micro"], "relaxed_micro_f1": m["relaxed"]["micro"]["f1"]}}
                print(json.dumps(rec), flush=True); log.write(json.dumps(rec) + "\n"); log.flush()
                history.append({"step": step, "val_strict_micro_f1": f1})
                if f1 > best_f1:
                    best_f1, best_step, bad_evals = f1, step, 0
                    save_best()
                else:
                    bad_evals += 1
                    if bad_evals >= t["patience"]:
                        stop_reason, done = "early_stopping", True
            if time.time() - started > t["max_minutes"] * 60:
                stop_reason, done = "time_budget", True
            if done:
                break
        if done:
            break

    if best_step == 0:  # budget ended before the first evaluation: evaluate once so a checkpoint exists
        m = validate(model, tok, val_docs, cfg)
        best_f1, best_step = m["strict"]["micro"]["f1"], step
        history.append({"step": step, "val_strict_micro_f1": best_f1})
        save_best()
    result = {"run_name": cfg["run_name"], "stop_reason": stop_reason, "steps_done": step, "planned_steps": total_steps,
              "best_step": best_step, "best_validation_strict_micro_f1": best_f1, "train_minutes": round((time.time() - started) / 60, 1),
              "tokenisation": tstats, "history": history}
    (run_dir / "train_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
