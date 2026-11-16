"""Entity-level evaluation.

Primary metric (strict): a prediction is correct only if (start, end, type) matches a gold span exactly.
Secondary (relaxed): same type and any character overlap, matched one-to-one.
Gold spans of out-of-scope kinds (``ignore``) neutralise overlapping predictions: they count as neither
TP nor FP. Documents with no gold entities feed the false-positive rate.
"""
from collections import defaultdict


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4), "tp": tp, "fp": fp, "fn": fn}


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def match_doc(gold: list[dict], pred: list[dict], ignore: list[dict]) -> dict:
    """Returns strict/relaxed match results for a single document."""
    g = [(x["start"], x["end"], x["type"]) for x in gold]
    p = [(x["start"], x["end"], x["type"]) for x in pred]
    ign = [(x["start"], x["end"]) for x in ignore]

    strict_tp = set(g) & set(p)
    p_scored = [x for x in p if x in strict_tp or not any(_overlaps((x[0], x[1]), i) for i in ign)]

    # relaxed: greedy one-to-one, same type, any overlap
    used: set[int] = set()
    relaxed_tp = []
    for pi, pr in enumerate(p_scored):
        for gi, gd in enumerate(g):
            if gi not in used and pr[2] == gd[2] and _overlaps(pr[:2], gd[:2]):
                used.add(gi)
                relaxed_tp.append((pr, gd))
                break
    return {
        "strict_tp": sorted(strict_tp),
        "strict_fp": sorted(set(p_scored) - strict_tp),
        "strict_fn": sorted(set(g) - strict_tp),
        "relaxed_tp": relaxed_tp,
        "relaxed_fp": [x for x in p_scored if not any(x == r[0] for r in relaxed_tp)],
        "relaxed_fn": [g[i] for i in range(len(g)) if i not in used],
        "n_pred_scored": len(p_scored),
    }


def evaluate(docs: list[dict], predictions: dict[str, list[dict]], max_examples: int = 25) -> dict:
    """``docs``: unified records; ``predictions``: id -> [{"start","end","type"}]."""
    totals = {m: defaultdict(int) for m in ("strict", "relaxed")}
    per_type = {m: defaultdict(lambda: defaultdict(int)) for m in ("strict", "relaxed")}
    per_slice: dict[str, dict] = {}
    fp_ex, fn_ex = [], []
    clean_docs = clean_with_pred = 0

    for d in docs:
        res = match_doc(d["entities"], predictions.get(d["id"], []), d.get("ignore", []))
        counts = {
            "strict": (len(res["strict_tp"]), len(res["strict_fp"]), len(res["strict_fn"])),
            "relaxed": (len(res["relaxed_tp"]), len(res["relaxed_fp"]), len(res["relaxed_fn"])),
        }
        for mode, (tp, fp, fn) in counts.items():
            for k, v in zip(("tp", "fp", "fn"), (tp, fp, fn)):
                totals[mode][k] += v
        for mode, tp_list, fp_list, fn_list in (
            ("strict", res["strict_tp"], res["strict_fp"], res["strict_fn"]),
            ("relaxed", [r[1] for r in res["relaxed_tp"]], res["relaxed_fp"], res["relaxed_fn"]),
        ):
            for x in tp_list:
                per_type[mode][x[2]]["tp"] += 1
            for x in fp_list:
                per_type[mode][x[2]]["fp"] += 1
            for x in fn_list:
                per_type[mode][x[2]]["fn"] += 1
        for key in (f"lang={d['lang']}", f"domain={d['domain']}"):
            s = per_slice.setdefault(key, {"tp": 0, "fp": 0, "fn": 0, "docs": 0})
            s["tp"] += counts["strict"][0]; s["fp"] += counts["strict"][1]; s["fn"] += counts["strict"][2]; s["docs"] += 1
        if not d["entities"]:
            clean_docs += 1
            clean_with_pred += 1 if res["n_pred_scored"] else 0
        for x in res["strict_fp"]:
            if len(fp_ex) < max_examples:
                fp_ex.append({"id": d["id"], "type": x[2], "text": d["text"][x[0]:x[1]], "context": d["text"][max(0, x[0] - 30):x[1] + 30]})
        for x in res["strict_fn"]:
            if len(fn_ex) < max_examples:
                fn_ex.append({"id": d["id"], "type": x[2], "text": d["text"][x[0]:x[1]], "context": d["text"][max(0, x[0] - 30):x[1] + 30]})

    report: dict = {"documents": len(docs)}
    for mode in ("strict", "relaxed"):
        types = per_type[mode]
        rows = {t: _prf(c["tp"], c["fp"], c["fn"]) for t, c in sorted(types.items())}
        supported = [r for t, r in rows.items() if r["tp"] + r["fn"] > 0]  # macro over types present in gold
        report[mode] = {
            "micro": _prf(totals[mode]["tp"], totals[mode]["fp"], totals[mode]["fn"]),
            "macro_f1": round(sum(r["f1"] for r in supported) / len(supported), 4) if supported else 0.0,
            "per_type": rows,
        }
    report["slices"] = {k: {**_prf(v["tp"], v["fp"], v["fn"]), "docs": v["docs"]} for k, v in sorted(per_slice.items())}
    report["clean_document_false_positive_rate"] = {
        "clean_documents": clean_docs, "with_any_prediction": clean_with_pred,
        "rate": round(clean_with_pred / clean_docs, 4) if clean_docs else None,
    }
    report["examples"] = {"false_positives": fp_ex, "false_negatives": fn_ex}
    return report
