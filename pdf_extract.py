"""Extract a paper into the database as text + table blocks.

Output structure per page:
  blocks = [
    {kind: "h2", text: "Method"},
    {kind: "p", sents: ["...", "..."]},
    {kind: "table", rows: [[{text, x, w, y}, ...], ...]},
    {kind: "caption", text: "Table 1"},
    ...
  ]
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

import pymupdf

import db

SECTION_NAMES = {
    "abstract", "introduction", "background",
    "method", "methods", "methodology",
    "sample", "procedure", "measures", "analysis",
    "results", "discussion",
    "conclusion", "conclusions",
    "implications", "limitations",
    "references", "appendix",
    "acknowledgments", "acknowledgements",
}

SKIP_HEADING_RE = re.compile(
    r"^(?:journal|volume|vol\.?|copyright|©|\d{4}.*vol|page \d+|pp\.? \d+|doi:)",
    re.IGNORECASE,
)

# Section heading on its own line (one-line), exactly the heading word(s)
TABLE_LABEL_RE = re.compile(r"^(Table|Figure|Fig\.)\s+\d+", re.IGNORECASE)


def _paper_id(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()[:16]


def _detect_section_title(line: str) -> str | None:
    s = line.strip().rstrip(":").strip()
    if not s or len(s) > 50:
        return None
    if SKIP_HEADING_RE.match(s):
        return None
    if s.lower() in SECTION_NAMES:
        return s.capitalize()
    if s.lower().rstrip("s") in SECTION_NAMES:
        return s.capitalize()
    return None


def _is_table_label(text: str) -> bool:
    """A short line like 'Table 1' or 'Figure 2'."""
    s = text.strip()
    return bool(TABLE_LABEL_RE.match(s)) and len(s) < 50


def _spans_of_line(line: dict) -> list[dict]:
    """Return spans with text and bbox."""
    spans = []
    for sp in line["spans"]:
        bbox = sp["bbox"]
        text = sp["text"]
        if not text.strip():
            continue
        spans.append({"text": text, "x": bbox[0], "y": bbox[1], "w": bbox[2] - bbox[0], "h": bbox[3] - bbox[1]})
    return spans


def _line_text(line: dict) -> str:
    return "".join(sp["text"] for sp in line["spans"])


def _line_y(line: dict) -> float:
    return line["bbox"][1]


def _line_h(line: dict) -> float:
    return line["bbox"][3] - line["bbox"][1]


def _group_lines_into_rows(page_dict: dict, y_tolerance: float = 4.0) -> list[list[dict]]:
    """Group lines by overlapping y-ranges into visual rows."""
    # Collect all lines with their y/h
    all_lines = []
    for block in page_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = _line_text(line)
            if not text.strip():
                continue
            all_lines.append({"line": line, "y": _line_y(line), "y_end": line["bbox"][3]})
    all_lines.sort(key=lambda r: r["y"])

    rows: list[list[dict]] = []
    for item in all_lines:
        placed = False
        for row in rows:
            row_top = row[0]["y"]
            row_bot = max(r["y_end"] for r in row)
            # Same row if line y is within tolerance of any existing line
            if abs(item["y"] - row_top) < y_tolerance or abs(item["y"] - row_bot) < y_tolerance:
                row.append(item)
                placed = True
                break
        if not placed:
            rows.append([item])
    return rows


def _is_table_row(spans: list[dict]) -> bool:
    """Detect if a row of spans looks like a table row (multiple separated cells)."""
    if len(spans) < 2:
        return False
    # Sort by x
    sorted_spans = sorted(spans, key=lambda s: s["x"])
    # Compute gaps between consecutive spans
    gaps = []
    for i in range(len(sorted_spans) - 1):
        gap = sorted_spans[i + 1]["x"] - (sorted_spans[i]["x"] + sorted_spans[i]["w"])
        gaps.append(gap)
    # A table row has at least one gap > some threshold
    return max(gaps) > 8


def _row_to_table_row(spans: list[dict]) -> list[dict]:
    """Convert spans to table row cells."""
    sorted_spans = sorted(spans, key=lambda s: s["x"])
    return [{"text": s["text"].strip(), "x": round(s["x"]), "w": round(s["w"])} for s in sorted_spans]


def _row_to_paragraph(spans: list[dict]) -> str:
    """Convert spans to plain text by joining with spaces (handling gaps)."""
    sorted_spans = sorted(spans, key=lambda s: s["x"])
    parts = []
    for i, s in enumerate(sorted_spans):
        if i == 0:
            parts.append(s["text"])
            continue
        prev = sorted_spans[i - 1]
        prev_end = prev["x"] + prev["w"]
        gap = s["x"] - prev_end
        # If gap is significant, insert space(s)
        if gap > 6:
            parts.append(" ")
        elif gap > 2:
            parts.append(" ")
        parts.append(s["text"])
    return "".join(parts).strip()


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [s.strip() for s in parts if s.strip()]

def _bbox_union(boxes):
    return [round(min(b[0] for b in boxes), 2), round(min(b[1] for b in boxes), 2),
            round(max(b[2] for b in boxes), 2), round(max(b[3] for b in boxes), 2)] if boxes else [0, 0, 0, 0]


def _sentence_word_groups(words: list[dict]) -> list[tuple[str, list[dict]]]:
    """Split native PDF words into sentence-sized, selectable groups."""
    groups: list[tuple[str, list[dict]]] = []
    current: list[dict] = []
    for word in words:
        current.append(word)
        # This deliberately mirrors the lightweight sentence splitting used for
        # translation, but keeps the original word rectangles intact.
        if re.search(r"[.!?][\"')\]]*$", word["text"]):
            groups.append((" ".join(w["text"] for w in current), current))
            current = []
    if current:
        groups.append((" ".join(w["text"] for w in current), current))
    return groups

def _extract_page_ir(page: pymupdf.Page, page_num: int, image_path: str | None = None) -> tuple[list[dict], dict]:
    """Build lossless-ish page IR from native PyMuPDF geometry; heuristics stay explicit."""
    raw = page.get_text("dict")
    # get_text("words") supplies glyph-derived rectangles.  Do not infer word
    # positions from span text length: proportional fonts make that inaccurate.
    words_by_block: dict[int, list[dict]] = defaultdict(list)
    for x0, y0, x1, y1, text, block_no, line_no, word_no in page.get_text("words", sort=False):
        if not text.strip():
            continue
        words_by_block[block_no].append({
            "text": text,
            "bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
            "line_no": line_no,
            "word_no": word_no,
        })
    for block_words in words_by_block.values():
        block_words.sort(key=lambda w: (w["line_no"], w["word_no"]))
    candidates = []
    sizes = []
    text_block_no = -1
    for rb in raw.get("blocks", []):
        if rb.get("type") == 1:
            candidates.append({"raw": rb, "lines": [], "words": [], "text": "", "bbox": [round(v,2) for v in rb.get("bbox", [0,0,0,0])], "size": 0, "figure": True})
            continue
        if rb.get("type") != 0 or not rb.get("lines"): continue
        # PyMuPDF's word block index counts text blocks only, unlike the
        # dictionary block array which also includes image blocks.
        text_block_no += 1
        lines = []
        words = words_by_block.get(text_block_no, [])
        for ln in rb["lines"]:
            txt = "".join(sp.get("text", "") for sp in ln.get("spans", [])).strip()
            if not txt: continue
            lb = list(ln["bbox"]); lines.append({"text": txt, "bbox": [round(v,2) for v in lb]})
            for sp in ln.get("spans", []):
                st = sp.get("text", "")
                if not st.strip(): continue
                sizes.append(float(sp.get("size", 10)))
        if lines:
            candidates.append({"raw": rb, "lines": lines, "words": words, "text": " ".join(x["text"] for x in lines), "bbox": _bbox_union([x["bbox"] for x in lines]), "size": max((sp.get("size",10) for ln in rb["lines"] for sp in ln.get("spans", [])), default=10)})
    median = sorted(sizes)[len(sizes)//2] if sizes else 10
    page_h = float(page.rect.height); page_w = float(page.rect.width)
    candidates.sort(key=lambda c: (c["bbox"][1], c["bbox"][0]))
    blocks=[]; word_boxes=[]; sentences=[]
    for idx, c in enumerate(candidates, 1):
        text=c["text"].strip(); y0,y1=c["bbox"][1],c["bbox"][3]
        low=text.lower(); kind="figure" if c.get("figure") else "paragraph"
        if c.get("figure"):
            blocks.append({"id":f"p{page_num}-b{idx}","kind":"figure","type":"figure","bbox":c["bbox"],"reading_order":idx,"text":"","lines":[],"sentences":[],"style":{},"children":[]})
            continue
        if y0 < page_h*0.08: kind="header"
        elif y1 > page_h*0.92: kind="footer"
        elif _is_table_label(text): kind="caption"
        elif c["size"] >= median*1.35 or _detect_section_title(text): kind="heading"
        elif re.match(r"^(?:[•●▪◦]|\d+[.)]|\([a-z]\))\s+", text, re.I): kind="list"
        sent_objs=[]
        for si, (sent_text, sw) in enumerate(_sentence_word_groups(c["words"])):
            sb = _bbox_union([w["bbox"] for w in sw])
            sid=f"p{page_num}-b{idx}-s{si+1}"; sent_objs.append({"id":sid,"text":sent_text,"bbox":sb})
            sentences.append({"id":sid,"hash":hashlib.sha1(sent_text.encode()).hexdigest()[:12],"text":sent_text,"x":sb[0],"y":sb[1],"w":sb[2]-sb[0],"h":sb[3]-sb[1]})
            for w in sw:
                word_boxes.append({"text":w["text"],"bbox":w["bbox"],"x":w["bbox"][0],"y":w["bbox"][1],"w":w["bbox"][2]-w["bbox"][0],"h":w["bbox"][3]-w["bbox"][1],"sent_id":sid,"lemma":re.sub(r"[^A-Za-z']", "", w["text"].lower())})
        blocks.append({"id":f"p{page_num}-b{idx}","kind":kind,"type":kind,"bbox":c["bbox"],"reading_order":idx,"text":text,"lines":c["lines"],"sentences":sent_objs,"style":{"font_size":round(c["size"],2),"bold":c["size"]>=median*1.2},"children":[]})
    return blocks, {"coordinate_version": 2, "page_num":page_num,"page_w":page_w,"page_h":page_h,"image_path":image_path,"image_w":page_w,"image_h":page_h,"word_boxes":word_boxes,"sentences":sentences,"blocks":blocks}


def _build_blocks_for_page(page: pymupdf.Page) -> list[dict]:
    """Walk through the page rows and emit h2/p/table/caption blocks."""
    page_dict = page.get_text("dict")
    rows = _group_lines_into_rows(page_dict)

    blocks: list[dict] = []
    current_para_lines: list[str] = []
    table_rows_buffer: list[list[dict]] = []
    table_caption: str | None = None

    def flush_paragraph():
        nonlocal current_para_lines
        if current_para_lines:
            text = " ".join(current_para_lines)
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                sents = _split_sentences(text)
                if sents:
                    blocks.append({"kind": "p", "sents": sents})
            current_para_lines = []

    def flush_table():
        nonlocal table_rows_buffer, table_caption
        if table_rows_buffer:
            blocks.append({
                "kind": "table",
                "rows": [_row_to_table_row(r) for r in table_rows_buffer],
            })
            table_rows_buffer = []
        if table_caption:
            blocks.append({"kind": "caption", "text": table_caption})
            table_caption = None

    i = 0
    while i < len(rows):
        row = rows[i]
        spans = []
        for item in row:
            spans.extend(_spans_of_line(item["line"]))
        # Detect heading (entire line equals a known section title)
        row_text = " ".join(s["text"] for s in spans).strip()
        # A heading is a single line that is the section name
        if len(row) == 1 and (title := _detect_section_title(row_text)):
            flush_paragraph()
            flush_table()
            blocks.append({"kind": "h2", "text": title})
            i += 1
            continue

        # Detect table label
        if _is_table_label(row_text):
            # The label could either introduce a table that follows, or stand alone.
            flush_paragraph()
            flush_table()
            table_caption = row_text
            i += 1
            continue

        # Detect table row (multi-column)
        if _is_table_row(spans):
            flush_paragraph()
            table_rows_buffer.append(spans)
            i += 1
            continue

        # Default: paragraph line
        flush_table()
        current_para_lines.append(_row_to_paragraph(spans))
        i += 1

    flush_paragraph()
    flush_table()
    return blocks


def extract_pdf(pdf_path: Path) -> str:
    paper_id = _paper_id(pdf_path)
    doc = pymupdf.open(pdf_path)

    title = None
    if doc.metadata:
        title = doc.metadata.get("title")
    if not title:
        title = pdf_path.stem.replace("_", " ").title()
    db.upsert_paper(paper_id, title, str(pdf_path))

    all_blocks: list[tuple[int, list[dict]]] = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        page_no = page_num + 1
        image_dir = Path(__file__).parent / ".cache" / "pages"; image_dir.mkdir(parents=True, exist_ok=True)
        image_name = f"{paper_id}-{page_no}.png"
        page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).save(image_dir / image_name)
        blocks, page_data = _extract_page_ir(page, page_no, image_name)
        page_data["image_w"] = int(page.rect.width * 1.5); page_data["image_h"] = int(page.rect.height * 1.5)
        db.save_page_blocks(paper_id, page_no, blocks, page_data)
        if blocks:
            all_blocks.append((page_num + 1, blocks))

    # Section identification: h2 across pages
    sections: list[tuple[int, int, str]] = []
    current_start: int | None = None
    current_title: str | None = None
    last_page = len(doc)

    for page_num, blocks in all_blocks:
        for b in blocks:
            if b["kind"] in {"h2", "heading"}:
                if current_title is not None and current_start is not None:
                    end = page_num if page_num >= current_start else page_num - 1
                    if end < current_start:
                        end = current_start
                    sections.append((current_start, end, current_title))
                current_title = b["text"]
                current_start = page_num

    if current_title is not None and current_start is not None:
        sections.append((current_start, last_page, current_title))
    sections = [s for s in sections if len(s[2]) >= 3]

    for idx, (start, end, title) in enumerate(sections):
        db.save_section(paper_id, idx, title, start, end)

    doc.close()
    return paper_id


if __name__ == "__main__":
    import sys
    p = Path(sys.argv[1])
    pid = extract_pdf(p)
    print(f"paper_id={pid}")
    print("sections:")
    for s in db.list_sections(pid):
        print(f"  [{s['section_id']}] {s['title']} pp.{s['start_page']}-{s['end_page']}")
    print("\npage 4 blocks:")
    for b in db.load_page_blocks(pid, 4):
        if b["kind"] == "table":
            print(f"  [table] {len(b['rows'])} rows")
            for r in b["rows"][:3]:
                print(f"    {[c['text'][:25] for c in r]}")
        else:
            t = (b.get("text") or " ".join(b.get("sents", [])))[:60]
            print(f"  [{b['kind']}] {t}")
