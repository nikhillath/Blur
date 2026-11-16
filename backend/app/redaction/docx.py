"""DOCX redaction by editing text nodes in place (formatting of untouched text is preserved).

Covers paragraphs, tables (including nested), headers and footers: exactly the parts the extractor reads.
Anything else in the package (comments, footnotes, text boxes) is NOT edited; the output validator scans every
XML part and fails the redaction if a redacted value is still present there.
"""
import io
import re
import zipfile

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.document_processing.extractors import _iter_blocks
from app.redaction.plan import RedactionError, Target, replacement

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def iter_paragraphs(doc):
    """(ref, Paragraph) using the same ref scheme as document_processing.extractors.extract_docx."""

    def container(el, parent, prefix):
        pi = ti = 0
        for block in _iter_blocks(el, parent):
            if isinstance(block, Paragraph):
                yield f"{prefix}.p.{pi}", block
                pi += 1
            else:
                yield from table(block, f"{prefix}.t.{ti}")
                ti += 1

    def table(t: Table, ref):
        for ri, row in enumerate(t.rows):
            seen, cells = set(), []
            for cell in row.cells:
                if id(cell._tc) not in seen:
                    seen.add(id(cell._tc))
                    cells.append(cell)
            for ci, cell in enumerate(cells):
                cref = f"{ref}.r.{ri}.c.{ci}"
                for pi, block in enumerate(_iter_blocks(cell._tc, cell)):
                    if isinstance(block, Paragraph):
                        yield f"{cref}.p.{pi}", block
                    else:
                        yield from table(block, f"{cref}.t.{pi}")

    yield from container(doc.element.body, doc, "body")
    for si, section in enumerate(doc.sections):
        for kind, part in (("header", section.header), ("footer", section.footer)):
            if part.is_linked_to_previous and si > 0:
                continue
            yield from container(part._element, part, f"{kind}.{si}")


def _text_nodes(p) -> list[tuple[object, int, int]]:
    """w:t elements with their [start, end) offsets in the paragraph text, plus a check against python-docx."""
    nodes, pos, joined = [], 0, []
    for el in p._p.iter():
        if el.tag == W + "t":
            s = el.text or ""
            nodes.append((el, pos, pos + len(s)))
            pos += len(s)
            joined.append(s)
        elif el.tag == W + "tab":
            pos += 1
            joined.append("\t")
        elif el.tag in (W + "br", W + "cr"):
            if el.get(W + "type") in (None, "textWrapping") or el.tag == W + "cr":
                pos += 1
                joined.append("\n")
    if "".join(joined) != p.text:
        raise RedactionError("a DOCX paragraph has a structure that cannot be edited safely")
    return nodes


def _edit_paragraph(p, edits: list[tuple[int, int, str]]) -> None:
    nodes = _text_nodes(p)
    owner: dict[int, object] = {}
    for k, (s, e, _) in enumerate(edits):
        owner[k] = next((n for n, ns, ne in nodes if ne > s and ns < e), None)
        if owner[k] is None:
            raise RedactionError("could not locate text to redact in a DOCX paragraph")
    for el, ns, ne in nodes:
        text, out, cursor = el.text or "", [], ns
        changed = False
        for k, (s, e, rep) in enumerate(edits):
            if e <= ns or s >= ne:
                continue
            changed = True
            out.append(text[cursor - ns : max(s, ns) - ns])
            if owner[k] is el:
                out.append(rep)
            cursor = min(e, ne)
        if changed:
            out.append(text[cursor - ns :])
            el.text = "".join(out)
            el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def redact_docx(data: bytes, page_map: dict, targets: list[Target], text: str, style: str) -> tuple[bytes, dict]:
    doc = Document(io.BytesIO(data))
    segs = [s for s in page_map.get("segments", ()) if s[2] in ("para", "cell")]
    paras = dict(iter_paragraphs(doc))
    edits: dict[str, list[tuple[int, int, str]]] = {}
    for t in targets:
        placed = False
        for s_, e_, _, ref in segs:
            lo, hi = max(s_, t.start), min(e_, t.end)
            if lo < hi and ref in paras:
                # the replacement text goes into the first paragraph the span touches; later ones are only cleared
                rep = "" if placed else replacement(style, text[t.start : t.end], t.types)
                placed = True
                edits.setdefault(ref, []).append((lo - s_, hi - s_, rep))
        if not placed:
            raise RedactionError("a selected span could not be located in the DOCX structure")
    for ref, items in edits.items():
        _edit_paragraph(paras[ref], sorted(items))
    cp = doc.core_properties
    for field in ("author", "last_modified_by", "title", "subject", "comments", "keywords", "category", "content_status"):
        setattr(cp, field, "")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue(), {"paragraphs_edited": len(edits)}


def all_parts_text(data: bytes) -> str:
    """Visible text of EVERY xml part (body, headers, comments, footnotes, text boxes...), tags stripped."""
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.endswith(".xml") or name.endswith(".rels"):
                raw = z.read(name).decode("utf-8", "ignore")
                # drop tags WITHOUT inserting spaces so a value split across runs is still seen whole
                out.append(re.sub(r"<[^>]+>", "", raw.replace("</w:p>", "\n")))
    return "\n".join(out)
