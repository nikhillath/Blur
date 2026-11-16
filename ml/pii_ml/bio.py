"""Character spans <-> token-level BIO labels (training representation)."""
from pii_ml.labelmap import LABEL2ID


def align_labels(
    offsets: list[tuple[int, int]], entities: list[dict], label2id: dict[str, int] = LABEL2ID,
    min_overlap: float = 0.5,
) -> tuple[list[int], int]:
    """Token labels for one (possibly windowed) tokenisation.

    * special/padding tokens (offset ``(0, 0)``) get -100 so the loss ignores them;
    * a token belongs to the entity covering at least ``min_overlap`` of its characters;
    * the first token of an entity is ``B-``, following tokens of the same entity ``I-``.

    Returns (labels, number_of_entities_that_no_token_could_carry). The second value surfaces
    tokenisation/annotation boundary mismatches instead of hiding them.
    """
    labels = []
    first_seen: set[int] = set()
    for s, e in offsets:
        if e <= s:
            labels.append(-100)
            continue
        best, best_cov = None, 0.0
        for i, ent in enumerate(entities):
            ov = min(e, ent["end"]) - max(s, ent["start"])
            if ov > 0 and ov / (e - s) > best_cov:
                best, best_cov = i, ov / (e - s)
        if best is None or best_cov < min_overlap:
            labels.append(label2id["O"])
        else:
            prefix = "I" if best in first_seen else "B"
            first_seen.add(best)
            labels.append(label2id[f"{prefix}-{entities[best]['type']}"])
    in_window = [i for i, ent in enumerate(entities) if any(
        min(e, ent["end"]) - max(s, ent["start"]) > 0 for s, e in offsets if e > s)]
    lost = sum(1 for i in in_window if i not in first_seen)
    return labels, lost
