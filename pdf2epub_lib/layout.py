"""Reading order and paragraph grouping for positioned lines."""

from __future__ import annotations

import re
import statistics

from pdf2epub_lib.model import Block, Line
from pdf2epub_lib.text import _first_letter, is_first_line_indent

class _Stream:
    """Lines that share a column: similar left and right edges, stacked down the page."""

    def __init__(self, line):
        self.lines = [line]

    @property
    def x0(self) -> float:
        return statistics.median(line.x0 for line in self.lines)

    @property
    def x1(self) -> float:
        return statistics.median(line.x1 for line in self.lines)

    @property
    def y0(self) -> float:
        return min(line.y0 for line in self.lines)

    @property
    def y1(self) -> float:
        return max(line.y1 for line in self.lines)

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    def sorted_lines(self):
        return sorted(self.lines, key=lambda line: (line.y0, line.x0))


def _can_join_stream(stream: _Stream, line) -> bool:
    sx0 = stream.x0
    sx1 = stream.x1
    overlap = min(stream.y1, line.y1) - max(stream.y0, line.y0)
    if overlap > 6:
        return False
    x0_ok = abs(line.x0 - sx0) <= 18 or (
        0 <= line.x0 - sx0 <= 30 and abs(line.x1 - sx1) <= 16
    )
    # A drop-cap wrap is indented a couple of ems, not a whole column away.
    if line.x0 < sx0 - 10:
        x0_ok = abs(line.x1 - sx1) <= 16 and sx0 - line.x0 <= 40
    if not x0_ok:
        return False
    # A short line far below a kicker belongs to the column underneath, not the kicker.
    if line.y0 - stream.y1 > 40:
        return False
    # A short first line is followed by the rest of the same column. A line that
    # reaches into a wider column beside it starts a new stream.
    if line.x1 > sx1 + 24:
        same_column = abs(line.x0 - sx0) <= 8 and line.x1 - sx1 <= 80
        if not same_column:
            return False
    # A narrower column stacked under a wider one is not a short last line.
    if line.x1 < sx1 - 30 and line.y0 - stream.y1 > 14:
        return False
    return True


def _merge_indented_wraps(streams: list[_Stream]) -> list[_Stream]:
    """Lines beside a drop cap share the column below them."""
    changed = True
    while changed:
        changed = False
        for upper in list(streams):
            for lower in streams:
                if upper is lower or upper not in streams:
                    continue
                if upper.y1 > lower.y0 + 4:
                    continue
                if lower.y0 - upper.y1 > 22:
                    continue
                if abs(upper.x1 - lower.x1) > 18:
                    continue
                if upper.x0 + 4 < lower.x0 or upper.x0 - lower.x0 > 40:
                    continue
                lower.lines.extend(upper.lines)
                streams.remove(upper)
                changed = True
                break
            if changed:
                break
    return streams


def _build_streams(lines, page_width: float) -> list[_Stream]:
    streams: list[_Stream] = []
    for line in sorted(lines, key=lambda item: (item.y0, item.x0)):
        if getattr(line, "is_image", False):
            continue
        best = None
        best_score = None
        for stream in streams:
            if not _can_join_stream(stream, line):
                continue
            score = (line.y0 - stream.y1, abs(line.x0 - stream.x0))
            if best_score is None or score < best_score:
                best = stream
                best_score = score
        if best is None:
            streams.append(_Stream(line))
        else:
            best.lines.append(line)
    return _merge_indented_wraps(streams)


def _y_overlap(group, other) -> float:
    if not group or not other:
        return 0.0
    top = max(min(item.y0 for item in group), min(item.y0 for item in other))
    bottom = min(max(item.y1 for item in group), max(item.y1 for item in other))
    return bottom - top


def _blocks_gutter(stream: _Stream, gap_lo: float, gap_hi: float) -> bool:
    """A column sitting in the whitespace is not a line that crosses the gutter."""
    if stream.x1 <= gap_lo + 1 or stream.x0 >= gap_hi - 1:
        return False
    if stream.x0 <= gap_lo + 1 and stream.x1 >= gap_hi - 1:
        return False
    return True


def _vertical_split(streams: list[_Stream]):
    """Split into left and right column groups. Wide lines may cross the gutter."""
    if len(streams) < 2:
        return None
    best = None
    for left_stream in streams:
        for right_stream in streams:
            gap_lo = left_stream.x1
            gap_hi = right_stream.x0
            gap = gap_hi - gap_lo
            if gap < 6:
                continue
            if any(
                _blocks_gutter(stream, gap_lo, gap_hi)
                for stream in streams
                if stream is not left_stream and stream is not right_stream
            ):
                continue
            gutter = (gap_lo + gap_hi) / 2
            left = [stream for stream in streams if stream.x1 <= gutter + 1]
            right = [stream for stream in streams if stream.x0 >= gutter - 1]
            if not left or not right:
                continue
            cross = [stream for stream in streams if stream not in left and stream not in right]
            overlap = _y_overlap(left, right)
            shorter = min(
                max(stream.y1 for stream in left) - min(stream.y0 for stream in left),
                max(stream.y1 for stream in right) - min(stream.y0 for stream in right),
            )
            if shorter <= 0 or overlap < 0.4 * shorter:
                continue
            score = (overlap, gap)
            if best is None or score > best[0]:
                best = (score, left, right, cross)
    if best is None:
        return None
    _score, left, right, cross = best
    return left, right, cross


def _flatten(streams: list[_Stream]):
    """One column's streams overlap in x, so their lines read from top to bottom."""
    lines = []
    for stream in streams:
        lines.extend(stream.sorted_lines())
    return sorted(lines, key=lambda line: (line.y0, line.x0))


def _order_column_streams(streams: list[_Stream]):
    if len(streams) <= 1:
        return _flatten(streams)
    split = _vertical_split(streams)
    if split:
        left, right, cross = split
        return _insert_wide_lines(_read_column_pair(left, right), cross)
    return _flatten(streams)


def _max_x1(stream: _Stream) -> float:
    return max(line.x1 for line in stream.lines)


def _stacked_on_column(stream: _Stream, mate: _Stream) -> bool:
    """A fragment above or below a column, not a narrower sidebar beside it."""
    if _y_overlap(stream.lines, mate.lines) > 8:
        return False
    if abs(stream.x0 - mate.x0) > 18:
        return False
    stream_right = _max_x1(stream)
    mate_right = _max_x1(mate)
    if abs(stream_right - mate_right) <= 42:
        return True
    gap = stream.y0 - mate.y1 if stream.y0 >= mate.y0 else mate.y0 - stream.y1
    return gap <= 16 and stream_right <= mate_right + 8


def _read_column_pair(left: list[_Stream], right: list[_Stream]):
    """Read the left column, then a matching right column, then a sidebar that sat beside them."""
    dominant = max(left, key=lambda stream: (stream.y1 - stream.y0) * max(stream.width, 1))
    mates = []
    others = []
    for stream in right:
        ratio = stream.width / dominant.width if dominant.width else 0
        overlap = _y_overlap([stream], [dominant])
        height = stream.y1 - stream.y0
        if 0.72 <= ratio <= 1.4 and height > 0 and overlap >= 0.55 * height:
            mates.append(stream)
        else:
            others.append(stream)
    # The top of a third column can sit beside the title, so it misses the
    # left column and would otherwise be read after the article.
    attached = []
    sidebar = []
    for stream in others:
        if mates and any(_stacked_on_column(stream, mate) for mate in mates):
            attached.append(stream)
        else:
            sidebar.append(stream)
    ordered = _order_column_streams(left) + _order_column_streams(mates + attached)
    return ordered + _order_column_streams(sidebar)


def _insert_wide_lines(lines, wide_streams: list[_Stream]):
    ordered = list(lines)
    for stream in sorted(wide_streams, key=lambda item: (item.y0, item.x0)):
        block = stream.sorted_lines()
        index = len(ordered)
        for at, line in enumerate(ordered):
            overlaps = line.x0 < stream.x1 - 4 and stream.x0 < line.x1 - 4
            if overlaps and line.y0 >= stream.y0 - 1:
                index = at
                break
        ordered[index:index] = block
    return ordered


def _order_body(lines, page_width: float):
    if len(lines) < 6:
        return sorted(lines, key=lambda line: (line.y0, line.x0))
    streams = _build_streams(lines, page_width)
    wide_cut = page_width * 0.46
    columns = [stream for stream in streams if stream.width <= wide_cut]
    wide = [stream for stream in streams if stream.width > wide_cut]
    if len(columns) < 2:
        return sorted(lines, key=lambda line: (line.y0, line.x0))
    return _insert_wide_lines(_order_column_streams(columns), wide)



def _same_baseline(left: Line, right: Line) -> bool:
    tolerance = max(1.6, max(left.size, right.size) * 0.35)
    return abs(left.origin_y - right.origin_y) <= tolerance


def _duplicate_fragment(left: Line, right: Line) -> bool:
    """A second drawing of the same words, shifted a few points to the right."""
    right_text = re.sub(r"\s+", " ", right.text).strip().lower()
    left_text = re.sub(r"\s+", " ", left.text).strip().lower()
    if len(right_text) < 4:
        return False
    return right_text in left_text


def merge_baseline_fragments(lines: list[Line]) -> list[Line]:
    """Join a marker or a word that sits on the same baseline as the line beside it.

    A list number or a bullet can be its own text object, a fraction of an em away
    from the item. A column gutter is wider than that. A repeated fragment, such as
    a word drawn twice, stays separate.
    """
    ordered = sorted(lines, key=lambda line: (round(line.origin_y, 1), line.x0))
    merged: list[Line] = []
    for line in ordered:
        if (
            merged
            and _same_baseline(merged[-1], line)
            and not merged[-1].ornament
            and not line.ornament
        ):
            gap = line.x0 - merged[-1].x1
            size = max(merged[-1].size, line.size, 1.0)
            if 0 < gap <= size * 0.95 and not _duplicate_fragment(merged[-1], line):
                merged[-1].spans.extend(line.spans)
                continue
        merged.append(line)
    return merged


def order_items(items, page_width: float):
    margin = [
        item
        for item in items
        if not getattr(item, "is_image", False)
        and (
            (item.x0 > page_width * 0.82 and (item.x1 - item.x0) < page_width * 0.14)
            or ((item.x1 - item.x0) < 16 and len(getattr(item, "text", "") or "") > 3)
        )
    ]
    body = merge_baseline_fragments([item for item in items if item not in margin])
    return _order_body(body, page_width) + sorted(margin, key=lambda item: (item.y0, item.x0))


def _starts_list_item(text: str) -> bool:
    """A bullet or a short numbered step. A wrapped continuation does not match."""
    return bool(re.match(r"^(?:[•●◦▪‣]|\d{1,2}[.)])(?:\s+\S|\s*$)", text.strip()))


def _is_section_label(current: Block, nxt: Block) -> bool:
    """A slightly larger label such as 'Implementation guidance' is not the next words."""
    return (
        nxt.size > current.size + 0.4
        and len(nxt.text.split()) <= 4
        and _first_letter(nxt.text).isupper()
    )


def paragraph_gap_limit(lines: list[Line], body: float) -> float:
    """How far the next line can sit and still be the same paragraph.

    Wrapped lines share one leading. A new paragraph is only a little farther
    down in some books, closer than a fixed multiple of the font size.
    """
    fallback = body * 1.35 * 1.45
    gaps = []
    for prev, line in zip(lines, lines[1:]):
        if abs(line.size - prev.size) > max(1.25, body * 0.12):
            continue
        if abs(line.x0 - prev.x0) > max(36.0, body * 3.2):
            continue
        if line.y0 + 2 < prev.y0:
            continue
        gap = line.y0 - prev.y0
        if 0 < gap <= body * 3:
            gaps.append(gap)
    if len(gaps) < 4:
        return fallback
    typical = statistics.median(gaps)
    # Do not go past the old ceiling. A book whose normal leading is already
    # that loose keeps joining lines the way it does today.
    return min(fallback, typical * 1.25)


def same_paragraph(prev: Line, line: Line, margin: float, body: float, gap_limit: float | None = None) -> bool:
    # A small-caps name on a body line can shift that line's size by about a point.
    if abs(line.size - prev.size) > max(1.25, body * 0.12):
        return False
    if line.ornament or prev.ornament:
        return False
    if re.match(r"by\b", line.text.strip(), re.I) and not re.match(r"by\b", prev.text.strip(), re.I):
        return False
    if prev.text.strip().lower() in {"article", "q&a"}:
        return False
    # A bullet or a numbered step is its own item. The wrapped lines under it are not.
    if _starts_list_item(line.text):
        return False
    # A slightly larger label, such as "Implementation guidance" or "Resources", stands alone.
    if line.size > prev.size + 0.4 and len(line.text.split()) <= 4 and _first_letter(line.text).isupper():
        return False
    if prev.size > line.size + 0.4 and len(prev.text.split()) <= 4 and _first_letter(prev.text).isupper():
        return False
    # A jump across a gutter starts a new paragraph. Indents stay well under this.
    if abs(line.x0 - prev.x0) > max(36.0, body * 3.2):
        return False
    if line.y0 + 2 < prev.y0:
        return False
    limit = gap_limit if gap_limit is not None else max(prev.size, line.size) * 1.35 * 1.45
    if line.y0 - prev.y0 > limit:
        return False
    if is_first_line_indent(line, prev, margin, body):
        return False
    return True


def body_margin(lines: list[Line], body: float) -> float:
    edges = [round(line.x0, 0) for line in lines if abs(line.size - body) <= max(0.8, body * 0.12)]
    if not edges:
        edges = [round(line.x0, 0) for line in lines]
    if not edges:
        return 0.0
    return statistics.median(edges)


def group_lines(lines: list[Line], margin: float, body: float) -> list[list[Line]]:
    gap_limit = paragraph_gap_limit(lines, body)
    groups: list[list[Line]] = []
    for line in lines:
        if not line.text and not line.ornament:
            continue
        if not groups or not same_paragraph(groups[-1][-1], line, margin, body, gap_limit):
            groups.append([line])
        else:
            groups[-1].append(line)
    return groups

