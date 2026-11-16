"""Download the approved datasets into ml/data/raw and write a manifest (sha256, size, license).

Usage (from backend/ with the venv active):  python ../ml/scripts/download_data.py

Nothing here is committed to git (ml/data is ignored). Licenses are copied from each dataset card at
download time; re-check them before redistributing anything.
"""
import hashlib
import json
import shutil
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"

DATASETS = {
    "maskara": {
        "repo": "somukandula/maskara-indian-pii-200k",
        "files": [
            "data/train-00000-of-00001.parquet",
            "data/template_disjoint_eval-00000-of-00001.parquet",
            "data/real_world_eval-00000-of-00001.parquet",
        ],
        "license": "MIT",
        "role": "training (train split); evaluation (two eval splits)",
    },
    "indiapii": {
        "repo": "maskflow-ai/indiapii-bench",
        "files": ["indiapii-v1.0.jsonl"],
        "license": "CC-BY-4.0",
        "role": "split by document: train / validation / test",
    },
    "ylemis": {
        "repo": "Pranshurs/ylemis-india-pii-benchmark",
        "files": ["benchmark.jsonl", "manifest.json"],
        "license": "CC0-1.0",
        "role": "final held-out evaluation ONLY (never trained on, never used for model selection)",
    },
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    manifest = {}
    for name, spec in DATASETS.items():
        out_dir = RAW / name
        out_dir.mkdir(parents=True, exist_ok=True)
        files = {}
        for rel in spec["files"]:
            cached = Path(hf_hub_download(spec["repo"], rel, repo_type="dataset"))
            dest = out_dir / Path(rel).name
            shutil.copyfile(cached, dest)
            files[dest.name] = {"sha256": sha256(dest), "bytes": dest.stat().st_size}
            print(f"{name}: {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
        manifest[name] = {k: spec[k] for k in ("repo", "license", "role")} | {"files": files}
    (RAW / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("wrote", RAW / "manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
