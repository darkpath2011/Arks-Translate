"""Export a translated PDF page while retaining its original visual layout."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

import pymupdf

from pdf_extract import block_text


log = logging.getLogger(__name__)

CHINESE_FONT = Path(r"C:\Windows\Fonts\simfang.ttf")
CJK_FONT = "arks-cjk"
# Built-in serif, used to redraw English that was too long to translate in
# place. SimFang's Latin glyphs are wider and would no longer fit the block.
LATIN_FONT = "tiro"
# SimFang's CJK glyphs stop being legible below this. A translation that needs
# a smaller size keeps its English instead of shrinking into unreadable specks.
MIN_FONT_SIZE = 6
# Comfortable spacing first; tighten only to rescue a block that would not fit.
LINE_HEIGHTS = (1.15, 1.05)
# Extracted boxes include leading, so splitting two overlapping boxes strictly
# on their edges leaves each a little short of the text it has to hold.
OVERLAP_SLACK = 1.5


class Placement(NamedTuple):
    """Text chosen for one block, at a size and spacing known to fit its box."""

    rect: pymupdf.Rect
    text: str
    size: int
    lineheight: float
    slack: float
    fontname: str
    is_source: bool


def _fit(
    ruler: pymupdf.Page, rect: pymupdf.Rect, text: str, base_size: float, fontname: str
) -> tuple[int, float, float] | None:
    """Largest legible size and spacing that fit text in rect, plus the height
    left over, or None if the text never fits.

    Measured by trial insertion on a throwaway page so the answer matches what
    the real page will do. Blocks must be sized before anything is erased:
    insert_textbox draws nothing at all when the text does not fit, so redacting
    first would leave a blank gap where the original used to be.
    """
    for lineheight in LINE_HEIGHTS:
        for size in range(max(MIN_FONT_SIZE, round(base_size)), MIN_FONT_SIZE - 1, -1):
            remaining = ruler.insert_textbox(
                rect, text, fontname=fontname, fontsize=size, lineheight=lineheight
            )
            if remaining >= 0:
                return size, lineheight, remaining
    return None


def _resolve_overlaps(rects: list[pymupdf.Rect]) -> list[pymupdf.Rect]:
    """Shrink a block that swallows a neighbour so the two do not print on top
    of each other.

    Extraction sometimes gives a block a box tall enough to cover the line above
    or below it. Both boxes then claim the same strip, and whichever is drawn
    second lands on top of the first. Give the contested strip to the smaller
    box and leave the larger one the tallest band that remains.
    """
    out = list(rects)
    for i, outer in enumerate(out):
        for inner in rects:
            overlap = outer & inner
            area = inner.get_area()
            if overlap.is_empty or area <= 0 or area > outer.get_area():
                continue
            if overlap.get_area() / area < 0.7 or overlap.get_area() >= outer.get_area():
                continue
            above = pymupdf.Rect(
                outer.x0, outer.y0, outer.x1, min(inner.y0 + OVERLAP_SLACK, outer.y1)
            )
            below = pymupdf.Rect(
                outer.x0, max(inner.y1 - OVERLAP_SLACK, outer.y0), outer.x1, outer.y1
            )
            keep = above if above.height >= below.height else below
            if keep.height >= 3:
                out[i] = outer = keep
    return out


def _place(ruler: pymupdf.Page, rect: pymupdf.Rect, block: dict, zh: str) -> Placement | None:
    """Choose what to draw in a block: its translation, else its English source.

    Falling back to the source keeps the block readable instead of shrinking the
    translation into specks, and it has to be redrawn rather than left in place —
    extracted boxes overlap, so a neighbour's redaction can erase an untouched
    block.
    """
    base_size = float(block.get("style", {}).get("font_size", 9))
    fitted = _fit(ruler, rect, zh, base_size, CJK_FONT)
    if fitted is not None:
        return Placement(rect, zh, *fitted, CJK_FONT, False)
    source = block_text(block)
    if source:
        fontname = LATIN_FONT if source.isascii() else CJK_FONT
        fitted = _fit(ruler, rect, source, base_size, fontname)
        if fitted is not None:
            return Placement(rect, source, *fitted, fontname, True)
    return None


def export_translated_pages(
    source_pdf: Path,
    pages: list[tuple[int, list[dict], list[str]]],
    output_path: Path,
) -> Path:
    """Write all pages that have saved translations into one ordered PDF."""
    if not CHINESE_FONT.is_file():
        raise RuntimeError("SimFang font is unavailable for Chinese PDF export")
    source = pymupdf.open(source_pdf)
    output = pymupdf.open()
    scratch = pymupdf.open()
    kept_source = 0
    unplaced = 0
    try:
        for page_num, blocks, translations in pages:
            if page_num < 1 or page_num > len(source):
                continue
            output.insert_pdf(source, from_page=page_num - 1, to_page=page_num - 1)
            page = output[-1]
            ruler = scratch.new_page(width=page.rect.width, height=page.rect.height)
            ruler.insert_font(fontname=CJK_FONT, fontfile=str(CHINESE_FONT))

            candidates = []
            for block, zh in zip(blocks, translations):
                text = (zh or "").strip()
                bbox = block.get("bbox")
                if not text or block.get("kind") == "figure" or not bbox or len(bbox) != 4:
                    continue
                rect = pymupdf.Rect(bbox)
                if rect.is_empty or rect.width < 3 or rect.height < 3:
                    continue
                candidates.append((block, text, rect))

            placements: list[Placement] = []
            boxes = _resolve_overlaps([rect for _, _, rect in candidates])
            for (block, text, bbox), text_rect in zip(candidates, boxes):
                placement = _place(ruler, text_rect, block, text)
                if placement is None:
                    unplaced += 1
                    log.warning(
                        "neither translation nor source fits its block on page %s: %.40s",
                        page_num,
                        text,
                    )
                    continue
                if placement.is_source:
                    kept_source += 1
                    log.warning(
                        "translation too long for its block on page %s, keeping source: %.40s",
                        page_num,
                        text,
                    )
                # Erase the block's full original box, not the trimmed one, so no
                # scan pixels of the English survive beside the new text.
                # A slight inset preserves rules and surrounding layout details.
                page.add_redact_annot(bbox + (-0.5, -0.5, 0.5, 0.5), fill=(1, 1, 1))
                placements.append(placement)

            # Erase the English rather than covering it: a white overlay leaves
            # the original selectable, searchable and read aloud underneath.
            # Line art is kept so table rules and borders survive.
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )

            page.insert_font(fontname=CJK_FONT, fontfile=str(CHINESE_FONT))
            for placement in placements:
                # Chinese needs fewer lines than the English it replaces, which
                # would leave the text stranded at the top of a part-empty box.
                page.insert_textbox(
                    placement.rect + (0, placement.slack / 2, 0, 0),
                    placement.text,
                    fontname=placement.fontname,
                    fontsize=placement.size,
                    color=(0, 0, 0),
                    lineheight=placement.lineheight,
                    overlay=True,
                )

        if len(output) == 0:
            raise ValueError("No translated pages available")
        if kept_source:
            log.warning("%s block(s) kept their English source in %s", kept_source, output_path.name)
        if unplaced:
            log.error("%s block(s) could not be placed at all in %s", unplaced, output_path.name)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output.save(output_path, garbage=4, deflate=True)
        return output_path
    finally:
        scratch.close()
        source.close()
        output.close()
