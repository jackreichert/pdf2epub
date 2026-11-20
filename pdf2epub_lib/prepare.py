"""Body size, requested phrases, running headers, and poster pages."""

from __future__ import annotations

import re
from collections import Counter

from pdf2epub_lib.model import Line, Page
from pdf2epub_lib.text import normalize_furniture

def detect_body_size(pages: list[Page]) -> float:
    weights: Counter[float] = Counter()
    for page in pages:
        for line in page.lines:
            if line.size < 6 or line.ornament:
                continue
            weights[round(line.size, 1)] += max(1, len(line.text))
    if not weights:
        return 11.0
    return weights.most_common(1)[0][0]


def heading_cutoff(pages: list[Page], body: float) -> float:
    short = [
        line.size
        for page in pages
        for line in page.lines
        if 2 <= len(line.text.strip()) <= 140 and line.size >= body * 1.25
    ]
    if any(size >= body * 1.9 for size in short):
        return body * 1.9
    if not short:
        return body * 2.4
    short.sort()
    return short[len(short) // 2]


def strip_phrases(pages: list[Page], phrases: list[str]) -> None:
    """Drop a watermark line, or cut the phrase out of a longer line."""
    needles = [phrase.strip() for phrase in phrases if phrase and phrase.strip()]
    if not needles:
        return
    pattern = re.compile("|".join(re.escape(needle) for needle in needles), re.IGNORECASE)
    for page in pages:
        kept: list[Line] = []
        for line in page.lines:
            if not pattern.search(line.text):
                kept.append(line)
                continue
            cleaned = pattern.sub("", line.text)
            cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
            if not re.search(r"[A-Za-z0-9]", cleaned):
                continue
            span = line.spans[0]
            span.text = cleaned
            span.chars = []
            line.spans = [span]
            kept.append(line)
        page.lines = kept


_BOILERPLATE = ("all rights reserved", "article reprint", "this document is authorized", "for additional copies")


def _margin_candidates(pages: list[Page], body: float, counts: Counter[str]):
    zoned: list[tuple[Page, Line, str]] = []
    for page in pages:
        for line in page.lines:
            in_zone = line.y1 > page.height * 0.90 or line.y0 < page.height * 0.09
            # Running heads in technical manuals sit within a point of the body size.
            if not in_zone or line.size > body * 1.05:
                continue
            key = normalize_furniture(line.text)
            if len(key) < 3 and not line.text.strip().isdigit():
                continue
            zoned.append((page, line, key))
            if key:
                counts[key] += 1
    return zoned


def _narrow_credits(pages: list[Page], body: float, counts: Counter[str]):
    """A vertical photo credit is only a few points wide and repeats down the issue."""
    narrow: list[tuple[Page, Line, str]] = []
    for page in pages:
        for line in page.lines:
            if (line.x1 - line.x0) >= 16 or line.size >= body * 0.92:
                continue
            key = normalize_furniture(line.text)
            if len(key) < 3:
                continue
            narrow.append((page, line, key))
            counts[key] += 1
    return narrow


def _sibling_header_ids(pages: list[Page], body: float, zoned, repeated: set[str], drop_ids: set[int]) -> None:
    """A section title under a repeated book header changes too often to match on its own."""
    header_pages = {
        page.number
        for page, line, key in zoned
        if key in repeated and line.y0 < page.height * 0.09
    }
    for page in pages:
        if page.number not in header_pages:
            continue
        pending = []
        others = []
        for line in page.lines:
            if id(line) in drop_ids:
                continue
            short = (
                line.y0 < page.height * 0.09
                and line.size <= body * 1.05
                and len(line.text) <= 60
                and len(line.text.split()) <= 8
            )
            if short:
                pending.append(line)
            else:
                others.append(line)
        if not pending:
            continue
        next_y = min((line.y0 for line in others), default=page.height)
        if next_y - max(line.y1 for line in pending) >= 12:
            for line in pending:
                drop_ids.add(id(line))


def strip_furniture(pages: list[Page], body: float) -> None:
    if len(pages) < 3:
        return
    counts: Counter[str] = Counter()
    zoned = _margin_candidates(pages, body, counts)
    narrow = _narrow_credits(pages, body, counts)
    repeated = {key for key, count in counts.items() if count >= max(3, int(0.12 * len(pages)))}
    drop_ids: set[int] = set()
    for _page, line, key in zoned:
        if key in repeated or line.text.strip().isdigit() or any(phrase in key for phrase in _BOILERPLATE):
            drop_ids.add(id(line))
    for _page, line, key in narrow:
        if key in repeated:
            drop_ids.add(id(line))
    _sibling_header_ids(pages, body, zoned, repeated, drop_ids)
    for page in pages:
        page.lines = [line for line in page.lines if id(line) not in drop_ids]


def dominant_color(pages: list[Page], body: float) -> int:
    counts: Counter[int] = Counter()
    for page in pages:
        for line in page.lines:
            if abs(line.size - body) > 0.6:
                continue
            for span in line.spans:
                if span.text.strip():
                    counts[span.color] += len(span.text)
    if not counts:
        return 0
    return counts.most_common(1)[0][0]


def mark_posters(pages: list[Page], body: float) -> None:
    for page in pages:
        body_chars = sum(
            len(line.text)
            for line in page.lines
            if abs(line.size - body) <= body * 0.15 and not line.ornament
        )
        total_chars = sum(len(line.text) for line in page.lines)
        page_area = page.width * page.height
        large_art = any(img.width * img.height > page_area * 0.18 for img in page.images)
        title_only = page.number == 1 and body_chars < 40 and total_chars < 80
        page.poster = (body_chars < 140 and large_art) or (total_chars < 28 and (large_art or not page.lines)) or title_only
        if page.poster:
            page.poster_jpeg = b""
