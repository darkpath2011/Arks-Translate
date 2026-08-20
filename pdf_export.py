"""Export a translated PDF page while retaining its original visual layout."""

from __future__ import annotations

from pathlib import Path

import pymupdf


CHINESE_FONT = Path(r"C:\Windows\Fonts\simfang.ttf")


def _textbox(page: pymupdf.Page, rect: pymupdf.Rect, text: str, base_size: float) -> None:
    """Fit translated text into its original text-block rectangle."""
    for size in range(max(6, round(base_size)), 4, -1):
        remaining = page.insert_textbox(
            rect,
            text,
            fontname="arks-cjk",
            fontsize=size,
            color=(0, 0, 0),
            lineheight=1.15,
            overlay=True,
        )
        if remaining >= 0:
            return
    # Keep a readable result for unusually dense blocks rather than failing.
    page.insert_textbox(
        rect,
        text,
        fontname="arks-cjk",
        fontsize=5,
        color=(0, 0, 0),
        lineheight=1.05,
        overlay=True,
    )


def export_translated_page(
    source_pdf: Path,
    page_num: int,
    blocks: list[dict],
    translations: list[str],
    output_path: Path,
) -> Path:
    """Write one source page, replacing only blocks that have Chinese text."""
    if not CHINESE_FONT.is_file():
        raise RuntimeError("SimFang font is unavailable for Chinese PDF export")

    source = pymupdf.open(source_pdf)
    try:
        if page_num < 1 or page_num > len(source):
            raise ValueError("Page number is outside the source PDF")
        output = pymupdf.open()
        output.insert_pdf(source, from_page=page_num - 1, to_page=page_num - 1)
    finally:
        source.close()

    try:
        page = output[0]
        page.insert_font(fontname="arks-cjk", fontfile=str(CHINESE_FONT))
        for block, zh in zip(blocks, translations):
            text = (zh or "").strip()
            if not text or block.get("kind") == "figure":
                continue
            bbox = block.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            rect = pymupdf.Rect(bbox)
            if rect.is_empty or rect.width < 3 or rect.height < 3:
                continue
            # A slight inset preserves rules and surrounding layout details.
            cover = rect + (-0.5, -0.5, 0.5, 0.5)
            page.draw_rect(cover, color=None, fill=(1, 1, 1), overlay=True)
            base_size = float(block.get("style", {}).get("font_size", 9))
            _textbox(page, rect, text, base_size)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output.save(output_path, garbage=4, deflate=True)
        return output_path
    finally:
        output.close()


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
    try:
        for page_num, blocks, translations in pages:
            if page_num < 1 or page_num > len(source):
                continue
            output.insert_pdf(source, from_page=page_num - 1, to_page=page_num - 1)
            page = output[-1]
            page.insert_font(fontname="arks-cjk", fontfile=str(CHINESE_FONT))
            for block, zh in zip(blocks, translations):
                text = (zh or "").strip()
                bbox = block.get("bbox")
                if not text or block.get("kind") == "figure" or not bbox or len(bbox) != 4:
                    continue
                rect = pymupdf.Rect(bbox)
                if rect.is_empty or rect.width < 3 or rect.height < 3:
                    continue
                page.draw_rect(rect + (-0.5, -0.5, 0.5, 0.5), color=None, fill=(1, 1, 1), overlay=True)
                base_size = float(block.get("style", {}).get("font_size", 9))
                _textbox(page, rect, text, base_size)
        if len(output) == 0:
            raise ValueError("No translated pages available")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output.save(output_path, garbage=4, deflate=True)
        return output_path
    finally:
        source.close()
        output.close()
