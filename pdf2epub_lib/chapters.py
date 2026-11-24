"""Choose chapter breaks and titles from bookmarks and headings."""

from __future__ import annotations

import re
from collections import Counter

from pdf2epub_lib.model import Block, Chapter, Page
from pdf2epub_lib.text import junk_metadata, smart_case

def page_has_h1(blocks: list[Block], h1_cut: float) -> bool:
    return any(block.kind == "h1" and block.size >= h1_cut - 0.2 for block in blocks)


def chapter_body_chars(blocks: list[Block]) -> int:
    return sum(len(block.text) for block in blocks if block.kind in {"p", "pull", "note"})


def _outline_adds_a_qualifier(outline: str, visual: str) -> bool:
    return bool(outline and visual and visual.casefold() in outline.casefold() and len(visual) + 1 < len(outline))


def _generic_outline_prefix(outline: str) -> bool:
    return outline.split(":", 1)[0].strip().casefold() in {"article", "chapter"}


def _scan_page_name(title: str) -> bool:
    """A bookmark that is only a scan file stem, such as independent71newy_0889."""
    text = title.strip()
    if not text or " " in text:
        return False
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*[_-][A-Za-z0-9_-]+", text):
        return False
    return any(ch.isdigit() for ch in text)


def outline_is_filename(title: str) -> bool:
    """Artboard names exported as bookmarks, not titles a reader would see."""
    text = title.strip()
    if not text or junk_metadata(text):
        return True
    if re.search(r"\bv\.?\s*f\b|\bbody\b|\(\d+\)", text, re.I):
        return True
    if re.search(r"[-_]cover\b", text, re.I):
        return True
    if " " not in text and ("-" in text or "_" in text):
        return True
    return False


# A level-1 bookmark longer than this is split at its level-2 children.
_LONG_OUTLINE_SPAN = 60


def outline_starts(doc) -> dict[int, str]:
    toc = doc.get_toc() or []
    entries: list[tuple[int, str, int]] = []
    for item in toc:
        if len(item) < 3 or not item[1] or not item[2]:
            continue
        entries.append((int(item[0]), str(item[1]).strip(), int(item[2])))
    level1 = [(title, page) for level, title, page in entries if level == 1]
    if level1 and all(_scan_page_name(title) for title, _page in level1):
        return {}
    distinct = {page for _title, page in level1}
    if len(distinct) < 2:
        chosen = [(title, page) for level, title, page in entries if level <= 2]
        distinct = {page for _title, page in chosen}
        if len(distinct) < 2:
            return {}
        starts: dict[int, str] = {}
        for title, page in chosen:
            starts.setdefault(page, title)
        return starts

    ordered = sorted(level1, key=lambda item: item[1])
    page_count = int(getattr(doc, "page_count", 0) or 0)
    chosen_pages: list[tuple[str, int]] = []
    used: set[str] = set()
    for title, page in ordered:
        label = title
        if label.casefold() in used:
            children_here = [child for level, child, child_page in entries if level == 2 and child_page == page]
            if children_here:
                label = children_here[0]
        used.add(label.casefold())
        chosen_pages.append((label, page))
    _split_long_outline_spans(entries, ordered, page_count, used, chosen_pages)
    starts = {}
    for title, page in chosen_pages:
        starts.setdefault(int(page), title)
    return starts


def _split_long_outline_spans(
    entries: list[tuple[int, str, int]],
    ordered: list[tuple[str, int]],
    page_count: int,
    used: set[str],
    chosen_pages: list[tuple[str, int]],
) -> None:
    """A level-1 bookmark longer than 60 pages starts chapters at its level-2 children."""
    for index, (title, start) in enumerate(ordered):
        end = ordered[index + 1][1] if index + 1 < len(ordered) else (page_count + 1 if page_count else start)
        if end - start <= _LONG_OUTLINE_SPAN:
            continue
        parent_head = title.split(":", 1)[0].strip()
        for level, child, child_page in entries:
            if level != 2 or not (start < child_page < end):
                continue
            label = child
            # A parent that already has a qualifier, or a repeated child, keeps the parent name.
            if parent_head and (":" in title or label.casefold() in used):
                prefixed = f"{parent_head}: {child}"
                if prefixed.casefold() != title.casefold():
                    label = prefixed
            if label.casefold() in used:
                label = f"{title}: {child}"
            used.add(label.casefold())
            chosen_pages.append((label, child_page))


def _page_groups(pages: list[Page], h1_pages: set[int]) -> list[list[Page]]:
    raw: list[list[Page]] = []
    current: list[Page] = []
    for page in pages:
        if page.number in h1_pages and current:
            raw.append(current)
            current = []
        current.append(page)
    if current:
        raw.append(current)
    return raw


def _heading_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _fold_logo_pages(
    raw_chapters: list[Chapter],
    h1_counts: Counter[str],
    pages: list[Page],
    page_blocks: dict[int, list[Block]],
) -> list[Chapter]:
    assembled: list[Chapter] = []
    held: list[Block] = []
    for chapter in raw_chapters:
        titles = [_heading_key(block.text) for block in chapter.blocks if block.kind == "h1"]
        logo = bool(titles) and all(h1_counts[title] >= 2 and len(title.split()) <= 3 for title in titles)
        has_body = chapter_body_chars(chapter.blocks) >= 60
        if has_body or chapter.outline_title:
            chapter.blocks = held + chapter.blocks
            held = []
            assembled.append(chapter)
            continue
        if logo and assembled:
            assembled[-1].blocks.extend(chapter.blocks)
        else:
            held.extend(chapter.blocks)
    if held:
        if assembled:
            # A trailing section opener with nothing after it stays with the previous chapter.
            if chapter_body_chars(held) >= 60:
                assembled.append(Chapter(blocks=held))
            else:
                assembled[-1].blocks.extend(held)
        else:
            assembled.append(Chapter(blocks=held))
    if not assembled:
        everything = [block for page in pages for block in page_blocks.get(page.number, [])]
        if everything:
            assembled.append(Chapter(blocks=everything))
    return assembled


def _assign_chapter_title(chapter: Chapter, index: int, pages: list[Page]) -> None:
    starts = index == 1 and bool(pages and chapter.blocks and chapter.blocks[0].page == 1)
    visual = choose_title(chapter.blocks, index, starts_at_front=starts)
    bookmark = chapter.outline_title or ""
    headings = {
        re.sub(r"\s+", " ", block.text).strip().casefold()
        for block in chapter.blocks
        if block.kind in {"h1", "h2"} and not block.poster and block.text.strip()
    }
    # Bookmarks often prefix a label ("Article:") or contain a typo the page title does not.
    if bookmark.lower() in {"cover", "contents", "back cover"}:
        chapter.title = bookmark
    elif (
        bookmark
        and not outline_is_filename(bookmark)
        and bookmark.casefold() in headings
        and not _outline_adds_a_qualifier(bookmark, visual)
    ):
        chapter.title = smart_case(bookmark)
    elif (
        bookmark
        and not outline_is_filename(bookmark)
        and _outline_adds_a_qualifier(bookmark, visual)
        and not _generic_outline_prefix(bookmark)
    ):
        # "Appendix: Performance efficiency" stays qualified. "Article: Title" does not.
        chapter.title = smart_case(bookmark)
    elif visual and (len(visual.split()) >= 2 or outline_is_filename(bookmark)):
        chapter.title = smart_case(visual)
    else:
        chapter.title = smart_case(bookmark) if bookmark else visual


def split_chapters(pages: list[Page], page_blocks: dict[int, list[Block]], h1_cut: float, outline: dict[int, str]) -> list[Chapter]:
    h1_pages = set(outline) if outline else {
        page.number for page in pages if page_has_h1(page_blocks.get(page.number, []), h1_cut)
    }
    h1_counts: Counter[str] = Counter()
    raw_chapters: list[Chapter] = []
    for group in _page_groups(pages, h1_pages):
        blocks: list[Block] = []
        for page in group:
            blocks.extend(page_blocks.get(page.number, []))
        for block in blocks:
            if block.kind == "h1":
                h1_counts[_heading_key(block.text)] += 1
        raw_chapters.append(Chapter(blocks=blocks, outline_title=outline.get(group[0].number, "")))
    assembled = _fold_logo_pages(raw_chapters, h1_counts, pages, page_blocks)
    for index, chapter in enumerate(assembled, start=1):
        _assign_chapter_title(chapter, index, pages)
    return [chapter for chapter in assembled if chapter_visible(chapter)]


def choose_title(blocks: list[Block], index: int, starts_at_front: bool) -> str:
    if starts_at_front and any(block.poster for block in blocks):
        article = [
            block
            for block in blocks
            if block.kind == "h1" and not block.poster and len(block.text.split()) > 3
        ]
        if not article:
            return "Cover"
    seen: list[Block] = []
    for block in blocks:
        if block.kind == "h1" and block.text.strip():
            seen.append(block)
        elif block.kind in {"p", "pull"} and len(block.text) > 80 and seen:
            break
    if seen:
        titled = [block for block in seen if len(block.text.split()) >= 2] or seen
        best = max(titled, key=lambda block: (block.size, -len(block.text)))
        return re.sub(r"\s+", " ", best.text).strip()[:180]
    for block in blocks:
        if block.kind in {"h1", "h2"} and block.text.strip():
            return re.sub(r"\s+", " ", block.text).strip()[:180]
    return "Cover" if starts_at_front else f"Chapter {index}"


def chapter_visible(chapter: Chapter) -> bool:
    return any(block.kind != "scene" and (block.text.strip() or block.image_href or block.poster) for block in chapter.blocks)


def infer_book_title(pages: list[Page], meta_title: str | None) -> str:
    if not junk_metadata(meta_title):
        return meta_title.strip()
    first = pages[0] if pages else None
    if first is None:
        return "Untitled"
    ranked = sorted(
        (line for line in first.lines if len(line.text.strip()) >= 3 and not line.ornament),
        key=lambda line: line.size,
        reverse=True,
    )
    chosen: list[str] = []
    for line in ranked:
        text = smart_case(re.sub(r"\s+", " ", line.text).strip())
        if not text or text in chosen:
            continue
        chosen.append(text)
        if len(chosen) == 2:
            break
    if not chosen:
        return "Untitled"
    if len(chosen) == 1:
        return chosen[0]
    if len(chosen[1].split()) <= 6:
        return f"{chosen[0]}: {chosen[1]}"
    return chosen[0]
