# ML pipeline

PII Sentinel detects PII with a **hybrid** of (1) deterministic rules and (2) a fine-tuned multilingual Transformer,
merged by a documented conflict-resolution policy. Detection never decides redaction: a human reviews every candidate.

## Status (honest summary)

| Component | Status |
|---|---|
| Rule detector (16 rules, validators, context scoring) | Implemented, unit-tested, evaluated |
| Fine-tuned Transformer (`pii-minilm-quick`) | **Trained and evaluated** on CPU, synthetic data only |
| Hybrid + conflict resolution | Implemented, evaluated |
| Pretrained generic NER (`Davlan/xlm-roberta-base-ner-hrl`) | Supported as a fallback model (`NER_MODEL`); not the default |
| LLM-assisted analysis | **Not implemented** (planned, optional) |
| Calibrated confidence | **No.** Scores are model/rule strengths, not calibrated probabilities |

Everything below is reproducible with the scripts in `ml/`. All numbers come from `ml/evaluation/final-minilm/results.json`.

## Data

| Dataset | Licence | Role | Notes |
|---|---|---|---|
| Maskara Indian PII 200K (`somukandula/maskara-indian-pii-200k`) | MIT | train / validation; two held-out eval splits | ~250k synthetic English + Hinglish documents, 17 labels |
| IndiaPII-Bench (`maskflow-ai/indiapii-bench`) | CC-BY-4.0 | split by document: 70% train / 10% validation / 20% test | 2,000 synthetic documents, labelled hard negatives |
| Ylemis India-PII Benchmark (`Pranshurs/ylemis-india-pii-benchmark`) | CC0-1.0 | **final held-out test only** | 10,000 synthetic cases, 14 language/format slices; its card says evaluation-only |

All three are fully synthetic. `ml/data/` is git-ignored; `python ml/scripts/download_data.py` fetches them and writes a
manifest with SHA-256 hashes.

### Splits and leakage control (`ml/scripts/prepare.py`)

* Maskara train is split **by template family** (8% of families held out for validation), not by row.
* IndiaPII is split by document (stable SHA-1 bucket).
* Exact-duplicate texts that appear in any test set are removed from train/validation (1,462 rows removed).
* The script refuses to write splits if group, id or text overlap is detected. The recorded result is zero overlap
  (`ml/data/processed/stats.json`).
* Test sets are never read by `train.py`. Model selection uses the validation split only.

### Label mapping (`ml/pii_ml/labelmap.py`)

Each source label maps to exactly one application type, to "drop", or to "ignore". Unknown labels raise an error.
* Hard negatives in IndiaPII (order ids, PAN-shaped invoice numbers, VPA-shaped emails, non-checksum Aadhaar-shaped
  numbers) become `O`: they train and test false-positive behaviour.
* Out-of-taxonomy gold spans (Ylemis `location`, `organization`) are **ignored**: predictions overlapping them are
  counted as neither right nor wrong.
* Government identifiers (Aadhaar, PAN, passport, voter id, driving licence) stay distinct from business identifiers.
* **Post-hoc correction, disclosed:** the first evaluation mapped Ylemis `account_id` to `BANK_ACCOUNT` and scored 0% for
  that type. Inspecting the false negatives showed those values are internal ids such as `policy_5628203_824` and
  `cust_1795603_166`, not bank account numbers, so the mapping was wrong; it now maps to `OTHER_IDENTIFIER`. The test
  documents were not changed. The original report is kept in `ml/evaluation/initial-minilm-original-mapping/`.

## Model and training

* Encoder: `microsoft/Multilingual-MiniLM-L12-H384` (MIT; 12 layers, hidden 384), XLM-R sentencepiece tokenizer.
  Chosen because the work runs on a CPU-only laptop: XLM-R base measured 11.5 s/step, 4.9 s/step with frozen
  embeddings, 0.91 s/step for MiniLM.
* Token classification head over 35 BIO labels (`O` + `B-`/`I-` for 17 types), character-exact spans.
* Config: `ml/configs/train_minilm_quick.yaml` (12,000 documents sampled from the Maskara train split + 3x oversampled
  IndiaPII train documents, 1 epoch, batch 16, lr 2e-4, head lr 1e-3, word embeddings frozen, seed 13). 19 minutes on CPU.
* Best validation strict micro-F1: 0.757 (step 750). Training logs: `ml/artifacts/pii-minilm-quick/train_log.jsonl`.
* **A longer run did not help.** Continuing for 30 minutes on 28,000 further documents (`pii-minilm-v1`) scored lower on
  identical validation documents (0.721 vs 0.748). Maskara is template-generated; more of the same templates mostly
  memorises them. By the selection rule (validation only) `pii-minilm-quick` is the model used.
* Caveat on selection: validation holds only 4 template families, so model selection is noisy.

Reproduce: `python ml/scripts/download_data.py && python ml/scripts/prepare.py &&
python ml/scripts/train.py --config ml/configs/train_minilm_quick.yaml` (run from `backend/` with the venv active).

## Hybrid detection and conflict resolution

`backend/app/ml/hybrid.py`: drop detections below `MIN_CONFIDENCE` (0.3); merge identical spans (score = max, agreement
recorded in provenance, never added); union overlapping spans of the same type; for overlapping different types keep the
highest score (then longer span, then rule over model) and record the loser under `provenance`. Every original
prediction is preserved. Known limit: a higher-scoring identifier inside a longer address line removes that address; a
reviewer can add it back as a manual span.

## Evaluation (held-out, full test sets, CPU)

Strict = exact start, end and type. Relaxed = same type and any overlap. `ml/scripts/evaluate.py --model <dir> --name <n>`.

| Test set | System | Precision | Recall | F1 strict | F1 relaxed |
|---|---|---|---|---|---|
| **Ylemis** (10k, 14 slices) | rules | 97.8% | 43.6% | 60.3% | 60.3% |
| | transformer | 66.8% | 70.4% | 68.6% | 77.2% |
| | **hybrid** | 72.3% | 76.3% | **74.3%** | 81.5% |
| **Maskara template-disjoint** (15k) | rules | 75.1% | 28.6% | 41.4% | 54.5% |
| | transformer | 76.4% | 83.9% | 80.0% | 90.8% |
| | **hybrid** | 77.1% | 84.5% | **80.6%** | 91.0% |
| **Maskara real-world** (2.6k) | rules | 74.7% | 39.1% | 51.4% | 64.3% |
| | transformer | 80.8% | 91.7% | 85.9% | 93.3% |
| | **hybrid** | 80.8% | 92.0% | **86.0%** | 93.2% |
| IndiaPII held-out (393) | hybrid | 98.8% | 100% | 99.4% | 99.4% |

**How to read these numbers**
* **Ylemis and Maskara template-disjoint are the meaningful out-of-distribution results.** The hybrid beats either part
  alone on both. Ylemis's published tool reports about 96% on this benchmark; this project's model is clearly weaker.
* **The IndiaPII number is not evidence of generalisation.** Train and test documents come from the same generator and
  templates, so it measures in-distribution memorisation. It is reported for completeness only.
* Latency (CPU, per document, single thread): rules < 1 ms; transformer p50 about 21-25 ms, p95 about 28-55 ms (up to
  72 ms on the longer IndiaPII documents). Model resident memory about 1.1 GB including PyTorch.

### Known weaknesses (from the per-type and per-language tables in the report)

* **False positives on documents with no PII are high on Ylemis:** 53.9% of PII-free documents get at least one
  prediction (rules alone: 15.4%). The model over-fires on PII-shaped ids. Low-confidence detections are shown to reviewers
  with their scores; the review step, not the model, makes the redaction decision.
* **Weak types:** `OTHER_IDENTIFIER` (17% F1 on Ylemis: GSTIN, medical and account ids are heterogeneous), `VEHICLE_REG`
  (40%), `DRIVING_LICENSE` and `VOTER_ID` (few training examples; voter id is carried by the rules).
* **Languages:** Hindi 61%, `llm-prompt-IN` 41%, OCR-style text 64%, Bengali 68%, Tamil 73%. The training data contains
  English and Hinglish only. Telugu/Gujarati/Kannada/Marathi/Punjabi scored 97-100% but each slice has just 135 documents
  with simple templates, so **no Indian language other than English/Hinglish is claimed as supported.**
* Boundary errors on addresses (a person's name absorbed into the address span) are the main reason strict F1 trails
  relaxed F1.

### Bugs found by this evaluation and fixed

Aadhaar rule matched invoice numbers (`INV-20240218-0366`): separators must now be consistent and not glued to a letter
prefix. UPI rule matched text the document calls an e-mail: now skipped. One-character junk spans from the token decoder
(`,`, `U`) are filtered. Regression tests were added for each. The model was **not** retrained after these fixes.

## Limitations

* Synthetic training and test data only; expect lower scores on real documents.
* Small encoder, CPU training, 12k training documents.
* Raw detected values are stored in the database unencrypted (documented in `docs/security.md`).
* Scores are not calibrated; do not read 0.9 as "90% likely".
