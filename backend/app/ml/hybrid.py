"""Conflict resolution between detectors.

Policy (applied in order):
1. Drop detections below ``min_confidence``.
2. Identical (type, span) detections from different detectors merge; score = max of the inputs.
   Scores are deliberately NOT combined (noisy-OR etc.): the detectors read the same text and
   are correlated, so agreement is recorded in provenance rather than inflating the score.
3. Overlapping detections of the same type merge into their union span; score = max.
4. Overlapping detections of different types: greedy by (score, span length, rule over model);
   the winner is kept and each loser is recorded under the winner's provenance as ``suppressed``.
Original predictions always survive in ``provenance``.
"""
from app.ml.taxonomy import Detection

_METHOD_RANK = {"rule": 0, "label-rule": 1, "transformer": 2}


def _merge(group: list[Detection]) -> Detection:
    best = max(group, key=lambda d: (d.score, -_METHOD_RANK.get(d.method, 9)))
    start = min(d.start for d in group)
    end = max(d.end for d in group)
    prov: list[dict] = []
    for d in group:
        prov.extend(d.provenance or [d.record()])
    methods = {d.method for d in group}
    det = Detection(best.entity_type, start, end, best.score, best.method, best.detector, prov)
    if len(methods) > 1:
        det.method = "hybrid"
    return det


def _merge_same_type(dets: list[Detection]) -> list[Detection]:
    by_type: dict[str, list[Detection]] = {}
    for d in dets:
        by_type.setdefault(d.entity_type, []).append(d)
    merged: list[Detection] = []
    for items in by_type.values():
        items.sort(key=lambda d: (d.start, d.end))
        group = [items[0]]
        group_end = items[0].end
        for d in items[1:]:
            if d.start < group_end:  # overlap (adjacent spans stay separate)
                group.append(d)
                group_end = max(group_end, d.end)
            else:
                merged.append(_merge(group) if len(group) > 1 else _single(group[0]))
                group, group_end = [d], d.end
        merged.append(_merge(group) if len(group) > 1 else _single(group[0]))
    return merged


def _single(d: Detection) -> Detection:
    if not d.provenance:
        d.provenance = [d.record()]
    return d


def resolve(detections: list[Detection], min_confidence: float = 0.3) -> list[Detection]:
    kept = [d for d in detections if d.score >= min_confidence]
    merged = _merge_same_type(kept)
    merged.sort(key=lambda d: (-d.score, -(d.end - d.start), _METHOD_RANK.get(d.method, 9), d.start))
    selected: list[Detection] = []
    for cand in merged:
        clash = next((s for s in selected if cand.start < s.end and s.start < cand.end), None)
        if clash is None:
            selected.append(cand)
        else:
            clash.provenance.append({"suppressed": cand.record()})
    selected.sort(key=lambda d: (d.start, d.end))
    return selected
