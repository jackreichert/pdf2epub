"""Read scanned pages with OCRia-nano-1 through LiteParse.

Digital pages keep the PDF text layer. A page with almost no text is
replaced by OCRia-nano-1 lines, in the same point space the layout
code already uses. The model weights download on the first run.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pdf2epub_lib.extract import group_spans
from pdf2epub_lib.model import Line, Page, Span, Style
from pdf2epub_lib.text import style_from_font_name

# Same bar as the scanned-page check in convert._require_text.
SPARSE_CHARS = 40
# Matches the "large artwork" test in mark_posters.
_SCAN_AREA = 0.18


def page_text_chars(page: Page) -> int:
    return sum(len(line.text) for line in page.lines)


def lines_from_items(items) -> list[Line]:
    """Turn LiteParse text items into lines. Boxes are top-left, in points."""
    spans: list[Span] = []
    for item in items:
        span = _span_from_item(item)
        if span is not None:
            spans.append(span)
    return [Line(spans=group) for group in group_spans(spans)]


def fill_sparse_pages(pdf_path: Path, pages: list[Page], parser=None, all_pages: bool = False) -> int:
    """Replace pages with OCRia-nano-1 text. Returns how many changed.

    By default only a page with almost no text is replaced, and only when
    the new text is longer. ``all_pages`` is the ``--ocr`` path: every page
    is read again, including one whose text layer is present but unusable.
    """
    chosen = list(pages) if all_pages else [page for page in pages if page_text_chars(page) < SPARSE_CHARS]
    if not chosen:
        return 0
    parsed = _parse(pdf_path, parser, [page.number for page in chosen])
    by_number = {int(item.page_num): item for item in parsed.pages}
    filled = 0
    for page in chosen:
        source = by_number.get(page.number)
        if source is None:
            continue
        lines = lines_from_items(source.text_items)
        gained = sum(len(line.text) for line in lines)
        if all_pages:
            if gained < SPARSE_CHARS:
                continue
        elif gained <= page_text_chars(page):
            continue
        page.lines = lines
        if page_text_chars(page) >= SPARSE_CHARS:
            _drop_page_scans(page)
        filled += 1
    return filled


def _parse(pdf_path: Path, parser, page_numbers: list[int]):
    if parser is not None:
        return parser.parse(str(pdf_path))
    try:
        from liteparse import LiteParse, ParseError
    except ImportError as exc:
        raise ValueError(
            "OCRia-nano-1 needs LiteParse. From the project directory, run: uv sync"
        ) from exc
    # Pull first. A parse that starts before the weights exist remembers that
    # miss for the rest of the process, so a retry in this process cannot see them.
    _ensure_models()
    target = ",".join(str(number) for number in page_numbers)
    try:
        return LiteParse(ocr_backend="ocria-nano", quiet=True, target_pages=target).parse(str(pdf_path))
    except ParseError as exc:
        raise ValueError(f"OCRia-nano-1 could not read {pdf_path.name}: {exc}") from exc


def _ensure_models() -> None:
    from liteparse import models

    installed = models.list()
    ready = any(
        getattr(item, "name", None) == "ocria-nano" and getattr(item, "complete", True)
        for item in installed
    )
    if ready:
        return
    print("Downloading OCRia-nano-1 (about 75 MB). This happens once.", file=sys.stderr)
    try:
        models.pull("ocria-nano")
    except Exception as exc:
        raise ValueError(
            "OCRia-nano-1 models are not installed. Run: uv run lit models pull ocria-nano"
        ) from exc


def _drop_page_scans(page: Page) -> None:
    """Drop a page-sized scan once its text has been read."""
    area = page.width * page.height
    if area <= 0:
        return
    page.images = [image for image in page.images if image.width * image.height <= area * _SCAN_AREA]


def _span_from_item(item) -> Span | None:
    text = str(_field(item, "text") or "")
    if _field(item, "trailing_space_generated") and text and not text.endswith(" "):
        text += " "
    if not text.strip():
        return None
    try:
        x = float(_field(item, "x"))
        y = float(_field(item, "y"))
        width = float(_field(item, "width"))
        height = float(_field(item, "height"))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    size = _font_size(item, height)
    origin_y = y + size * 0.9
    step = width / len(text)
    chars = [(ch, x + index * step, x + (index + 1) * step, y) for index, ch in enumerate(text)]
    return Span(
        text=text,
        size=size,
        style=_style(item),
        color=0,
        x0=x,
        x1=x + width,
        origin_y=origin_y,
        chars=chars,
    )


def _font_size(item, height: float) -> float:
    raw = _field(item, "font_size")
    try:
        size = float(raw)
    except (TypeError, ValueError):
        size = 0.0
    if size <= 0:
        size = height * 0.9
    return max(size, 1.0)


def _style(item) -> Style:
    name = _field(item, "font_name") or ""
    return style_from_font_name(str(name))


def _field(item, name: str):
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)
