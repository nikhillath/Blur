"""Load, normalise and validate the source datasets into one unified record format.

Unified record::

    {"id", "text", "entities": [{"start","end","type"}], "ignore": [{"start","end"}],
     "source", "lang", "domain", "group"}

``group`` is the unit that must never straddle splits (template family / document).
"""
import hashlib
import json
from collections import Counter
from pathlib import Path

from pii_ml.labelmap import IGNORE, map_label


class Report(Counter):
    """Counts of everything that was dropped or repaired, so nothing disappears silently."""


def _clean_spans(text: str, spans: list[dict], report: Report) -> list[dict]:
    """Validate spans; trim whitespace; resolve overlaps (keep the longest, then the earliest)."""
    ok = []
    for sp in spans:
        s, e = int(sp["start"]), int(sp["end"])
        if not (0 <= s < e <= len(text)):
            report["malformed_span"] += 1
            continue
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if s >= e:
            report["blank_span"] += 1
            continue
        ok.append({"start": s, "end": e, "type": sp["type"]})
    ok.sort(key=lambda x: (-(x["end"] - x["start"]), x["start"]))
    kept: list[dict] = []
    for sp in ok:
        if any(sp["start"] < k["end"] and k["start"] < sp["end"] for k in kept):
            report["overlap_dropped"] += 1
            continue
        kept.append(sp)
    kept.sort(key=lambda x: x["start"])
    return kept


def normalise(source: str, raw: dict, report: Report, *, id_: str, text: str, group: str,
              lang: str | None, domain: str | None, label_key: str) -> dict | None:
    if not isinstance(text, str) or not text.strip():
        report["empty_text"] += 1
        return None
    gold, ignore = [], []
    for ent in raw.get("entities") or []:
        if source == "indiapii" and ent.get("value_class") == "hard_negative":
            report["hard_negative_spans"] += 1
            continue
        target = map_label(source, ent[label_key])
        if target is None:
            report["dropped_by_mapping"] += 1
        elif target == IGNORE:
            ignore.append({"start": int(ent["start"]), "end": int(ent["end"]), "type": ent[label_key]})
        else:
            gold.append({"start": ent["start"], "end": ent["end"], "type": target})
    return {
        "id": id_, "text": text, "entities": _clean_spans(text, gold, report),
        "ignore": [s for s in ignore if 0 <= s["start"] < s["end"] <= len(text)],
        "source": source, "lang": lang or "unknown", "domain": domain or "unknown", "group": group,
    }


def load_maskara(path: Path, report: Report) -> list[dict]:
    import pyarrow.parquet as pq

    out = []
    for r in pq.read_table(path).to_pylist():
        rec = normalise("maskara", r, report, id_=r["id"], text=r["text"], group=r["template_family"],
                        lang=r.get("locale"), domain=r.get("domain"), label_key="label")
        if rec:
            rec["difficulty"] = r.get("difficulty")
            out.append(rec)
    return out


def load_indiapii(path: Path, report: Report) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        rec = normalise("indiapii", r, report, id_=r["id"], text=r["text"], group=r["id"],
                        lang=r.get("lang"), domain=r.get("domain"), label_key="label")
        if rec:
            out.append(rec)
    return out


def load_ylemis(path: Path, report: Report) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        rec = normalise("ylemis", r, report, id_=r["id"], text=r["text"], group=r["id"],
                        lang=r.get("language"), domain=r.get("category"), label_key="type")
        if rec:
            out.append(rec)
    return out


def bucket(key: str, mod: int = 100) -> int:
    """Stable, process-independent hash bucket (never Python's randomised hash())."""
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16) % mod


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def text_key(text: str) -> str:
    return hashlib.sha1(" ".join(text.split()).lower().encode("utf-8")).hexdigest()
