from app.redaction.plan import Target, replacement


def redact_text(text: str, targets: list[Target], style: str) -> str:
    """Splice replacements into the canonical text (newlines are normalised to LF, a BOM is dropped)."""
    out, pos = [], 0
    for t in targets:
        out.append(text[pos : t.start])
        out.append(replacement(style, text[t.start : t.end], t.types))
        pos = t.end
    out.append(text[pos:])
    return "".join(out)
