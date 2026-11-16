# ml/

Training and evaluation for the PII Transformer. Full write-up: `docs/ml-pipeline.md`.

```
configs/      train_minilm.yaml (30-min warm-start run), train_minilm_quick.yaml (the selected model's config)
pii_ml/       label maps, dataset normalisation, BIO alignment, metrics (unit-tested in backend/tests)
scripts/      download_data.py -> prepare.py -> train.py -> evaluate.py
data/         raw/ and processed/ (git-ignored; recreate with the scripts)
artifacts/    checkpoints and logs (git-ignored; model.safetensors is ~470 MB)
evaluation/   results.json + report.md per evaluation run (kept in git)
```

Run from `backend/` with its venv active:

```
python ../ml/scripts/download_data.py
python ../ml/scripts/prepare.py
python ../ml/scripts/train.py --config ../ml/configs/train_minilm_quick.yaml   # ~20 min on a laptop CPU
python ../ml/scripts/evaluate.py --model ../ml/artifacts/pii-minilm-quick/best --name my-run
```

To use a checkpoint in the app set `NER_MODEL=<path to .../best>` in `.env`. Leave it empty for rules-only mode.
