"""Turn ordered lines and images into typed blocks."""

from __future__ import annotations

import re

from pdf2epub_lib.joins import absorb_split_pulls, merge_text_blocks, rejoin_pull_quotes
from pdf2epub_lib.layout import body_margin, group_lines, order_items
from pdf2epub_lib.model import Block, Line, Page
from pdf2epub_lib.text import _first_letter, color_distance, ends_sentence, join_line_pieces, pieces_text

def _emphasis_ratios(group: list[Line]) -> tuple[float, float]:
    italic_letters = 0
    small_letters = 0
    letter_count = 0
    for line in group:
        for span in line.spans:
            count = sum(1 for ch in span.text if ch.isalpha())
            letter_count += count
            if span.style.italic:
                italic_letters += count
            if span.style.smallcaps:
                small_letters += count
    if not letter_count:
        return 0.0, 0.0
    return italic_letters / letter_count, small_letters / letter_count


def build_blocks(page: Page, body: float, h1_cut: float, body_color: int, image_names: dict[int, str]) -> list[Block]:
    margin = body_margin(page.lines, body)
    ordered = order_items(page.lines, page.width)
    text_lines = [item for item in ordered if not getattr(item, "is_image", False)]
    groups = group_lines(text_lines, margin, body)
    blocks: list[Block] = []
    for group in groups:
        pieces = join_line_pieces([line.pieces for line in group])
        text = pieces_text(pieces)
        if not text and not any(line.ornament for line in group):
            continue
        italic_ratio, smallcaps_ratio = _emphasis_ratios(group)
        kind = classify_group(group, text, body, h1_cut)
        color = group[0].spans[0].color if group[0].spans else 0
        delta = group[0].x0 - margin
        blocks.append(
            Block(
                kind=kind,
                pieces=pieces if text else [],
                size=group[0].size,
                page=page.number,
                y0=min(line.y0 for line in group),
                y1=max(line.y1 for line in group),
                x0=min(line.x0 for line in group),
                indented=body * 0.45 <= delta <= body * 2.3,
                color=color,
                nlines=len(group),
                italic_ratio=italic_ratio,
                smallcaps_ratio=smallcaps_ratio,
                poster=page.poster,
            )
        )

    placed = place_images(blocks, page, image_names)
    refined = refine_blocks(placed, body_color)
    # A cover drawn as vectors has no text blocks. Keep a poster block so the page
    # still becomes a chapter and the rendered page is written into the book.
    if page.poster and not any(block.poster for block in refined):
        refined.append(
            Block(
                kind="p",
                pieces=[],
                size=0,
                page=page.number,
                y0=0,
                y1=page.height,
                x0=0,
                poster=True,
            )
        )
    return refined


def classify_group(group: list[Line], text: str, body: float, h1_cut: float) -> str:
    if any(line.ornament for line in group) and len(text) < 2:
        return "scene"
    size = group[0].size
    length = len(text)
    # A long deck can be larger than body text. Titles stay within a handful of words.
    if size >= h1_cut and length <= 180 and len(group) <= 6 and len(text.split()) <= 16:
        return "h1"
    if size >= body * 1.18 and length <= 320:
        # A one-line quotation ends a sentence. A section heading does not.
        sentence_quote = ends_sentence(text) and length > 80
        if len(group) >= 3 or length > 120 or sentence_quote:
            return "pull"
        return "h2"
    if size <= body * 0.86 and length <= 260:
        return "small"
    return "p"


def place_images(blocks: list[Block], page: Page, image_names: dict[int, str]) -> list[Block]:
    if page.poster:
        return blocks
    text_lines = [line for line in page.lines if not line.ornament and line.text]
    for image in sorted(page.images, key=lambda img: (img.y0, img.x0)):
        href = image_names.get(image.xref)
        if not href:
            continue
        beside = []
        for line in text_lines:
            overlap = min(image.y1, line.y1) - max(image.y0, line.y0)
            if overlap > 4 and line.x0 >= image.x0 + image.width * 0.45:
                beside.append(line)
        dropcap = False
        target_y = image.y0
        if beside and image.width < page.width * 0.5:
            text_left = min(line.x0 for line in beside)
            if image.x1 <= text_left + 10:
                dropcap = True
                target_y = min(line.y0 for line in beside)
        target = len(blocks)
        for index, block in enumerate(blocks):
            if block.kind == "image":
                continue
            if dropcap and block.y0 - 4 <= target_y <= block.y1 + 4:
                target = index
                break
            if not dropcap and block.y0 >= image.y0 - 6:
                target = index
                break
        blocks.insert(
            target,
            Block(
                kind="image",
                pieces=[],
                size=0,
                page=page.number,
                y0=image.y0,
                y1=image.y1,
                x0=image.x0,
                poster=False,
                image_href=href,
                image_class="dropcap" if dropcap else "",
                alt="",
            ),
        )
    return blocks


def _stack_headings(blocks: list[Block]) -> list[Block]:
    merged: list[Block] = []
    for block in blocks:
        if (
            block.kind in {"h1", "h2"}
            and re.match(r"by\b", block.text.strip(), re.I)
            and len(block.text.split()) <= 8
            and len(block.text) < 80
        ):
            block.kind = "byline"
        if (
            merged
            and block.kind == "h1"
            and merged[-1].kind == "h1"
            and block.page == merged[-1].page
            and abs(block.size - merged[-1].size) < 1.2
            and block.y0 - merged[-1].y1 < max(block.size * 0.85, 8)
        ):
            # "ARTICLE" above the real title is a kicker, not the first line of the title.
            if len(merged[-1].text.split()) <= 2 and len(merged[-1].text) < 18 and len(block.text.split()) >= 3:
                merged[-1].kind = "kicker"
            else:
                merged[-1] = merge_text_blocks(merged[-1], block, " ")
                continue
        merged.append(block)
    return merged


def _text_neighbor(blocks: list[Block], start: int, step: int) -> Block | None:
    index = start + step
    while 0 <= index < len(blocks):
        if blocks[index].kind not in {"image", "scene", "empty"}:
            return blocks[index]
        index += step
    return None


def _label_roles(blocks: list[Block], body_color: int) -> None:
    for index, block in enumerate(blocks):
        nxt = _text_neighbor(blocks, index, 1)
        prev = _text_neighbor(blocks, index, -1)
        colored = color_distance(block.color, body_color) > 90 and body_color != 0
        if block.kind == "h2" and nxt and nxt.kind == "h1" and len(block.text) < 90:
            block.kind = "byline" if (block.smallcaps_ratio > 0.5 or colored) else "kicker"
        elif block.kind == "h2" and prev and prev.kind == "h1" and len(block.text) < 180 and block.nlines <= 4:
            block.kind = "deck"
        elif block.kind == "pull" and prev and prev.kind == "h1" and block.nlines <= 5:
            block.kind = "deck"
        elif (
            block.kind == "h2"
            and prev
            and prev.kind in {"h1", "deck"}
            and re.match(r"by\b", block.text.strip(), re.I)
            and len(block.text) < 90
        ):
            block.kind = "byline"
        elif block.kind == "small":
            # Neighbors skip image blocks, so a credit under a picture stays a note.
            block.kind = "note"


def _drop_edge_scenes(blocks: list[Block]) -> list[Block]:
    collapsed: list[Block] = []
    for block in blocks:
        if block.kind == "scene" and (not collapsed or collapsed[-1].kind == "scene"):
            if collapsed and collapsed[-1].kind == "scene":
                continue
            if not collapsed:
                continue
        collapsed.append(block)
    while collapsed and collapsed[-1].kind == "scene":
        collapsed.pop()
    return collapsed


def refine_blocks(blocks: list[Block], body_color: int) -> list[Block]:
    stacked = _stack_headings(blocks)
    _label_roles(stacked, body_color)
    trimmed = _drop_edge_scenes(stacked)
    return rejoin_pull_quotes(absorb_split_pulls(attach_drop_caps(trimmed)))


def attach_drop_caps(blocks: list[Block]) -> list[Block]:
    """A one-letter display initial belongs to the paragraph beside it."""
    index = 0
    while index < len(blocks) - 1:
        block = blocks[index]
        nxt = blocks[index + 1]
        letter = block.text.strip()
        if (
            len(letter) == 1
            and letter.isalpha()
            and nxt.kind == "p"
            and nxt.pieces
            and block.size >= max(nxt.size * 2, 1)
        ):
            head = _first_letter(nxt.text)
            if head.islower() and letter.upper() not in {"A", "I"}:
                style, text = nxt.pieces[0]
                nxt.pieces = [(style, letter + text.lstrip())] + list(nxt.pieces[1:])
            else:
                nxt.pieces = join_line_pieces([block.pieces, nxt.pieces])
            del blocks[index]
            continue
        index += 1
    return blocks
