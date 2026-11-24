#!/usr/bin/env python3
"""Convert a PDF into a reflowable EPUB.

Reads the text layer (fonts, size, position) and rebuilds chapters,
paragraphs, emphasis, and images. Bookmarks are used as chapter breaks
when the PDF has them. Otherwise chapters come from heading size.
Pass --ocr to read scanned pages with OCRia-nano-1.

The steps live in pdf2epub_lib. This module is the command and the import
surface the tests use.

Usage:
    python pdf2epub.py INPUT.pdf
    python pdf2epub.py INPUT.pdf -o OUTPUT.epub --title "Title" --author "Name"
    python pdf2epub.py SCAN.pdf --ocr
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pdf2epub_lib.model import (
    HYPHENS,
    LIGATURES,
    FONT_NAME_LIMIT,
    CSS,
    Style,
    Span,
    Line,
    _span_boxes,
    _flat_chars,
    RawImage,
    Block,
    Page,
    Chapter,
    Result,
)

from pdf2epub_lib.text import (
    style_from_font_name,
    clean_chars,
    looks_tracked,
    collapse_tracked,
    ends_sentence,
    is_first_line_indent,
    normalize_furniture,
    junk_metadata,
    smart_case,
    color_distance,
    as_int_color,
    escape_text,
    render_pieces,
    merge_pieces,
    pieces_text,
    rstrip_pieces,
    lstrip_pieces,
    join_line_pieces,
    _needs_word_space,
    _squeeze_spaces,
    line_pieces,
    chars_to_text,
    _is_combining_mark,
    _is_rtl_letter,
    _mostly_rtl,
    _order_rtl_line,
    _baseline_groups,
    _first_letter,
)

from pdf2epub_lib.layout import (
    _Stream,
    _can_join_stream,
    _merge_indented_wraps,
    _build_streams,
    _y_overlap,
    _blocks_gutter,
    _vertical_split,
    _flatten,
    _order_column_streams,
    _max_x1,
    _stacked_on_column,
    _read_column_pair,
    _insert_wide_lines,
    _order_body,
    _same_baseline,
    _duplicate_fragment,
    merge_baseline_fragments,
    order_items,
    _starts_list_item,
    _is_section_label,
    paragraph_gap_limit,
    same_paragraph,
    body_margin,
    group_lines,
)

from pdf2epub_lib.extract import (
    FontRec,
    load_font_catalog,
    resolve_style,
    _vertical_direction,
    _space_vertical,
    _char_baseline,
    group_spans,
    extract_lines,
    extract_images,
)

from pdf2epub_lib.prepare import (
    detect_body_size,
    heading_cutoff,
    strip_phrases,
    strip_furniture,
    dominant_color,
    mark_posters,
)

from pdf2epub_lib.blocks import (
    build_blocks,
    classify_group,
    place_images,
    refine_blocks,
    attach_drop_caps,
)

from pdf2epub_lib.joins import (
    _word_token,
    _continues_sentence,
    join_broken_sentences,
    absorb_split_pulls,
    rejoin_pull_quotes,
    merge_text_blocks,
    merge_across_pages,
)

from pdf2epub_lib.chapters import (
    page_has_h1,
    chapter_body_chars,
    _outline_adds_a_qualifier,
    _generic_outline_prefix,
    outline_is_filename,
    _LONG_OUTLINE_SPAN,
    outline_starts,
    split_chapters,
    choose_title,
    chapter_visible,
    infer_book_title,
)

from pdf2epub_lib.publish import (
    block_html,
    chapter_inner_html,
    xhtml_document,
    write_epub,
    render_posters,
)

from pdf2epub_lib.convert import (
    convert,
    slug,
)
from pdf2epub_lib.ocr import (
    fill_sparse_pages,
    lines_from_items,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert a PDF into an EPUB with chapters, paragraphs, images, and formatting.")
    parser.add_argument("pdf", type=Path, help="PDF to convert")
    parser.add_argument("-o", "--output", type=Path, help="EPUB path. Defaults to the PDF name with an .epub suffix.")
    parser.add_argument("--title", help="Book title. Defaults to the PDF metadata or the cover.")
    parser.add_argument("--author", help="Author. Defaults to the PDF metadata.")
    parser.add_argument("--language", default="en", help="Language code. Default: en")
    parser.add_argument("--dump-dir", type=Path, help="Also write each chapter as an HTML file in this directory.")
    parser.add_argument(
        "--strip",
        action="append",
        default=[],
        metavar="TEXT",
        help="Remove this text wherever it appears, such as a watermark. Repeat for more than one string.",
    )
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="Read every page with OCRia-nano-1, including a page that already has a text layer. The first run downloads the model.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = convert(
            args.pdf,
            output=args.output,
            title=args.title,
            author=args.author,
            language=args.language,
            dump_dir=args.dump_dir,
            verbose=args.verbose,
            strip=args.strip,
            ocr=args.ocr,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {result.output}")
    print(f"{result.title}" + (f" — {result.author}" if result.author else ""))
    print(f"{result.page_count} pages, {len(result.chapters)} chapters, {result.word_count} words, {result.image_count} images")
    for title in result.chapters:
        print(f"  {title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
