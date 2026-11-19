#!/usr/bin/env python3
"""Convert a PDF into a reflowable EPUB.

Reads the text layer (fonts, size, position) and rebuilds chapters,
paragraphs, emphasis, and images. Bookmarks are used as chapter breaks
when the PDF has them. Otherwise chapters come from heading size.

Usage:
    python pdf2epub.py INPUT.pdf
    python pdf2epub.py INPUT.pdf -o OUTPUT.epub --title "Title" --author "Name"
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import statistics
import sys
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import fitz

HYPHENS = ("-", "\u00ad", "\u2010")
LIGATURES = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "ft",
    "\ufb06": "st",
}
FONT_NAME_LIMIT = 24

CSS = """
@namespace epub "http://www.idpf.org/2007/ops";
html, body {
  margin: 0;
  padding: 0;
}
body {
  font-family: Palatino, "Palatino Linotype", "Iowan Old Style", Georgia, serif;
  line-height: 1.5;
  margin: 5% 6% 8%;
  hyphens: auto;
  -webkit-hyphens: auto;
}
h1 {
  font-weight: 600;
  font-size: 1.65em;
  line-height: 1.15;
  margin: 0.4em 0 0.45em;
  text-indent: 0;
}
h2 {
  font-weight: 500;
  font-size: 1.2em;
  line-height: 1.25;
  margin: 1.5em 0 0.4em;
  text-indent: 0;
}
p {
  margin: 0 0 0.15em;
  text-indent: 0;
}
p.indent { text-indent: 1.25em; }
p.byline {
  font-variant: small-caps;
  letter-spacing: 0.14em;
  margin: 1.8em 0 0.15em;
  color: #8d3f38;
  text-indent: 0;
}
p.kicker {
  font-variant: small-caps;
  letter-spacing: 0.16em;
  font-size: 0.82em;
  color: #555;
  margin: 1.6em 0 0.2em;
  text-indent: 0;
}
p.deck {
  font-size: 1.08em;
  margin: 0.15em 0 1.15em;
  text-indent: 0;
}
p.note, p.caption, figcaption {
  font-size: 0.86em;
  line-height: 1.35;
  color: #444;
  margin: 0.25em 0 1em;
  text-indent: 0;
}
blockquote {
  margin: 1.35em 0.2em;
  padding: 0.55em 0;
  border-top: 1px solid #c9b4a6;
  border-bottom: 1px solid #c9b4a6;
  color: #8d3f38;
}
blockquote p { margin: 0.25em 0; text-indent: 0; }
span.sc {
  font-variant: small-caps;
  letter-spacing: 0.03em;
}
figure {
  margin: 1em auto 1.1em;
  text-align: center;
}
img { max-width: 100%; height: auto; }
img.dropcap {
  float: left;
  width: 4.4em;
  margin: 0.1em 0.55em 0.2em 0;
}
hr.scene {
  border: 0;
  height: 1.4em;
  margin: 1.1em auto;
  text-align: center;
}
hr.scene::after {
  content: "*  *  *";
  letter-spacing: 0.35em;
  color: #8d3f38;
  font-size: 0.8em;
}
img.poster { width: 100%; }
""".strip()


@dataclass(frozen=True)
class Style:
    italic: bool = False
    bold: bool = False
    smallcaps: bool = False


@dataclass
class Span:
    text: str
    size: float
    style: Style
    color: int
    x0: float
    x1: float
    origin_y: float
    chars: list[tuple[str, float, float]] = field(default_factory=list)
    ornament: bool = False
    spacewidth: float = 0.0


@dataclass
class Line:
    spans: list[Span]
    is_image: bool = False

    @property
    def x0(self) -> float:
        return min(s.x0 for s in self.spans)

    @property
    def x1(self) -> float:
        return max(s.x1 for s in self.spans)

    @property
    def y0(self) -> float:
        return min(c[1] for s in self.spans for c in _span_boxes(s))

    @property
    def y1(self) -> float:
        return max(s.origin_y for s in self.spans) + max(s.size for s in self.spans) * 0.25

    @property
    def origin_y(self) -> float:
        return self.spans[0].origin_y

    @property
    def size(self) -> float:
        weights: Counter[float] = Counter()
        for span in self.spans:
            weights[round(span.size, 1)] += max(1, sum(1 for ch in span.text if not ch.isspace()))
        return weights.most_common(1)[0][0]

    @property
    def pieces(self) -> list[tuple[Style, str]]:
        return line_pieces(self)

    @property
    def text(self) -> str:
        return "".join(text for _, text in self.pieces).strip()

    @property
    def ornament(self) -> bool:
        return bool(self.spans) and all(s.ornament or not s.text.strip() for s in self.spans) and any(
            s.ornament for s in self.spans
        )


def _span_boxes(span: Span) -> list[tuple[str, float]]:
    if span.chars:
        return [(ch, y) for ch, _x0, _x1, y in _flat_chars(span)]
    return [("", span.origin_y - span.size)]


def _flat_chars(span: Span) -> list[tuple[str, float, float, float]]:
    """Return (char, x0, x1, y0) tuples. y0 falls back to the baseline."""
    out = []
    for item in span.chars:
        if len(item) >= 4:
            ch, x0, x1, y0 = item[0], item[1], item[2], item[3]
            out.append((ch, x0, x1, y0))
        else:
            ch, x0, x1 = item
            out.append((ch, x0, x1, span.origin_y))
    return out


@dataclass
class RawImage:
    xref: int
    bbox: tuple[float, float, float, float]
    data: bytes
    ext: str
    is_image: bool = True

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass
class Block:
    kind: str
    pieces: list[tuple[Style, str]]
    size: float
    page: int
    y0: float
    y1: float
    x0: float
    indented: bool = False
    color: int = 0
    nlines: int = 1
    italic_ratio: float = 0.0
    smallcaps_ratio: float = 0.0
    poster: bool = False
    image_href: str | None = None
    image_class: str = ""
    alt: str = ""

    @property
    def text(self) -> str:
        return "".join(part for _, part in self.pieces).strip()


@dataclass
class Page:
    number: int
    width: float
    height: float
    lines: list[Line]
    images: list[RawImage]
    poster_jpeg: bytes | None = None
    poster: bool = False


@dataclass
class Chapter:
    blocks: list[Block]
    title: str = ""
    outline_title: str = ""


@dataclass
class Result:
    output: Path
    title: str
    author: str
    chapters: list[str]
    page_count: int
    image_count: int
    word_count: int


# --- text helpers -----------------------------------------------------------

def style_from_font_name(name: str) -> Style:
    cleaned = name.split("+", 1)[-1].lower()
    italic = "italic" in cleaned or "oblique" in cleaned or cleaned.endswith("itali")
    bold = any(token in cleaned for token in ("bold", "semibold", "demi", "black", "heavy"))
    smallcaps = bool(re.search(r"small.?caps?|(?:^|[-_])sc\d", cleaned))
    return Style(italic=italic, bold=bold, smallcaps=smallcaps)


def clean_chars(text: str) -> str:
    text = "".join(LIGATURES.get(ch, ch) for ch in text)
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    return text


def looks_tracked(text: str) -> bool:
    parts = [part for part in text.split(" ") if part]
    if len(parts) < 5:
        return False
    singles = sum(1 for part in parts if len(part) == 1 and part.isalpha())
    return singles / len(parts) >= 0.75


def collapse_tracked(text: str, chars: list[tuple[str, float, float]], size: float) -> str:
    """Join letterspaced words. Wider gaps between letters become word spaces."""
    if not looks_tracked(text):
        return text
    letters = [(ch, x0, x1) for ch, x0, x1 in chars if not ch.isspace() and ch.isprintable()]
    if len(letters) < 4:
        return "".join(part for part in text.split(" ") if part)
    gaps = [letters[i][1] - letters[i - 1][1] for i in range(1, len(letters))]
    typical = statistics.median(gaps)
    threshold = typical + max(size * 0.22, 1.0)
    out = [letters[0][0]]
    for index, gap in enumerate(gaps):
        if gap > threshold:
            out.append(" ")
        out.append(letters[index + 1][0])
    return "".join(out)


def ends_sentence(text: str) -> bool:
    stripped = text.rstrip()
    stripped = stripped.rstrip("\"'”’»)]")
    return stripped.endswith((".", "!", "?", "…"))


def is_first_line_indent(line: Line, prev: Line, margin: float, body: float) -> bool:
    """True for a classic first-line indent, not for text wrapped around a picture."""
    delta = line.x0 - margin
    if not (body * 0.4 <= delta <= body * 2.3):
        return False
    if prev.x0 > margin + body * 0.35:
        return False
    return True


def normalize_furniture(text: str) -> str:
    lowered = re.sub(r"[^a-z0-9]+", " ", text.lower())
    lowered = re.sub(r"\d+", "", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def junk_metadata(value: str | None) -> bool:
    if not value or not value.strip():
        return True
    text = value.strip()
    if re.search(r"\.(indd|pdf|docx?|pages|qxd)$", text, re.I):
        return True
    if re.match(r"(untitled|microsoft word|string[_ ])", text, re.I):
        return True
    return False


def smart_case(text: str) -> str:
    letters = [ch for ch in text if ch.isalpha()]
    if letters and (all(ch.isupper() for ch in letters) or all(ch.islower() for ch in letters)):
        return text.title()
    return text


def color_distance(left: int, right: int) -> int:
    return (
        abs(((left >> 16) & 255) - ((right >> 16) & 255))
        + abs(((left >> 8) & 255) - ((right >> 8) & 255))
        + abs((left & 255) - (right & 255))
    )


def as_int_color(color) -> int:
    if isinstance(color, int):
        return color
    if isinstance(color, (list, tuple)) and len(color) >= 3:
        channels = []
        for channel in color[:3]:
            if isinstance(channel, float) and channel <= 1:
                channels.append(int(round(channel * 255)))
            else:
                channels.append(int(channel))
        red, green, blue = channels
        return (red << 16) + (green << 8) + blue
    return 0


def escape_text(text: str) -> str:
    return html.escape(clean_chars(text), quote=False)


def render_pieces(pieces: list[tuple[Style, str]], kind: str) -> str:
    merged = merge_pieces(pieces)
    parts: list[str] = []
    all_bold = bool(merged) and all(style.bold for style, text in merged if text.strip())
    for style, text in merged:
        if not text:
            continue
        rendered = escape_text(text)
        if style.smallcaps:
            rendered = f'<span class="sc">{rendered}</span>'
        if style.italic:
            rendered = f"<em>{rendered}</em>"
        if style.bold and not (all_bold and kind in {"h1", "h2", "kicker", "byline"}):
            rendered = f"<strong>{rendered}</strong>"
        parts.append(rendered)
    return "".join(parts)


def merge_pieces(pieces: list[tuple[Style, str]]) -> list[tuple[Style, str]]:
    merged: list[tuple[Style, str]] = []
    for style, text in pieces:
        if not text:
            continue
        if merged and merged[-1][0] == style:
            merged[-1] = (style, merged[-1][1] + text)
        else:
            merged.append((style, text))
    return merged


def pieces_text(pieces: list[tuple[Style, str]]) -> str:
    return "".join(text for _, text in pieces).strip()


def rstrip_pieces(pieces: list[tuple[Style, str]]) -> list[tuple[Style, str]]:
    if not pieces:
        return pieces
    style, text = pieces[-1]
    return pieces[:-1] + [(style, text.rstrip(" \t"))]


def lstrip_pieces(pieces: list[tuple[Style, str]]) -> list[tuple[Style, str]]:
    if not pieces:
        return pieces
    style, text = pieces[0]
    return [(style, text.lstrip(" \t"))] + pieces[1:]


def join_line_pieces(groups: list[list[tuple[Style, str]]]) -> list[tuple[Style, str]]:
    """Join wrapped lines. A hyphen at the end of a line is removed when the next line continues the word."""
    accumulated: list[tuple[Style, str]] = []
    for group in groups:
        group = rstrip_pieces(group)
        group = [item for item in group if item[1]]
        if not group:
            continue
        if not accumulated:
            accumulated = lstrip_pieces(group)
            continue
        group = lstrip_pieces(group)
        style, text = accumulated[-1]
        head = group[0][1][:1]
        if text.endswith(HYPHENS) and head.islower():
            accumulated[-1] = (style, text[:-1])
        elif text.endswith(HYPHENS):
            pass
        elif text and group[0][1] and not text.endswith(" ") and not group[0][1].startswith(" "):
            accumulated[-1] = (style, text + " ")
        accumulated.extend(group)
    return merge_pieces(accumulated)


def line_pieces(line: Line) -> list[tuple[Style, str]]:
    ordered = sorted(line.spans, key=lambda span: span.x0)
    pieces: list[tuple[Style, str]] = []
    previous_x1 = None
    all_chars: list[tuple[str, float, float]] = []
    for span in ordered:
        text = chars_to_text(span)
        all_chars.extend((ch, x0, x1) for ch, x0, x1, _y in _flat_chars(span) if ch.strip() or ch == " ")
        previous_text = pieces[-1][1] if pieces else ""
        if (
            previous_x1 is not None
            and text
            and not text[:1].isspace()
            and not previous_text[-1:].isspace()
        ):
            gap = span.x0 - previous_x1
            word_gap = span.spacewidth or span.size * 0.33
            if gap > max(word_gap * 1.15, span.size * 0.3):
                text = " " + text
        if text:
            pieces.append((span.style, text))
            previous_x1 = span.x1
    pieces = merge_pieces(pieces)
    raw = "".join(text for _, text in pieces)
    collapsed = collapse_tracked(raw, all_chars, line.size)
    if collapsed != raw and pieces:
        pieces = [(pieces[0][0], collapsed)]
    return pieces


def chars_to_text(span: Span) -> str:
    """Join glyphs in stored order. Word spaces come from space glyphs, not ink gaps."""
    chars = _flat_chars(span)
    if not chars:
        return clean_chars(span.text)
    out: list[str] = []
    for ch, _x0, _x1, _y0 in chars:
        if ch == "\u00ad":
            ch = "-"
        if ch in "\n\r":
            ch = " "
        elif ord(ch) < 32 or 0xE000 <= ord(ch) <= 0xF8FF:
            continue
        out.append(ch)
    return clean_chars("".join(out))


def _is_combining_mark(ch: str) -> bool:
    return bool(ch) and unicodedata.category(ch).startswith("M")


def _is_rtl_letter(ch: str) -> bool:
    code = ord(ch)
    return 0x0590 <= code <= 0x08FF or 0xFB1D <= code <= 0xFB4F


def _mostly_rtl(chars) -> bool:
    letters = []
    rtl = 0
    for item in chars:
        ch = item[0]
        if _is_combining_mark(ch):
            continue
        if _is_rtl_letter(ch):
            letters.append(ch)
            rtl += 1
        elif ch.isalpha():
            letters.append(ch)
    if len(letters) < 2:
        return False
    return rtl / len(letters) >= 0.4


def _order_rtl_line(chars):
    """Turn a visually left-to-right Hebrew run into logical order."""
    bases = []
    marks = []
    for item in chars:
        if _is_combining_mark(item[0]):
            marks.append(item)
        else:
            bases.append(item)
    if not bases:
        return list(chars)
    buckets: list[list] = [[] for _ in bases]
    for mark in marks:
        center = (mark[1] + mark[2]) / 2
        nearest = min(
            range(len(bases)),
            key=lambda index: abs(((bases[index][1] + bases[index][2]) / 2) - center),
        )
        buckets[nearest].append(mark)
    ordered = []
    for index in sorted(range(len(bases)), key=lambda index: -bases[index][1]):
        ordered.append(bases[index])
        ordered.extend(buckets[index])
    return ordered


def _baseline_groups(chars, size: float):
    """Split one PDF text object into visual lines.

    InDesign often emits a whole text frame as one span. Characters stay in
    content-stream order on Latin lines. A jump in the glyph top starts a new
    line. Hebrew lines are stored in logical order so the reader’s bidi
    algorithm can display them.
    """
    if not chars:
        return []
    tolerance = max(1.5, size * 0.35)
    groups: list[list] = []
    current: list = [chars[0]]
    anchor = chars[0][3]
    for item in chars[1:]:
        y0 = item[3]
        if abs(y0 - anchor) > tolerance:
            groups.append(current)
            current = [item]
            anchor = y0
        else:
            current.append(item)
    groups.append(current)
    return [_order_rtl_line(group) if _mostly_rtl(group) else group for group in groups]


# --- layout -----------------------------------------------------------------

def column_midpoint(items, page_width: float) -> float | None:
    lines = [
        item
        for item in items
        if not getattr(item, "is_image", False)
        and not getattr(item, "ornament", False)
        and (item.x1 - item.x0) > 8
    ]
    if len(lines) < 8:
        return None
    anchors = sorted({round(line.x0, 0) for line in lines})
    best: tuple[float, float] | None = None
    for left, right in zip(anchors, anchors[1:]):
        gap = right - left
        if gap < page_width * 0.10:
            continue
        mid = (left + right) / 2
        left_lines = [line for line in lines if (line.x0 + line.x1) / 2 < mid]
        right_lines = [line for line in lines if (line.x0 + line.x1) / 2 >= mid]
        if len(left_lines) < 4 or len(right_lines) < 4:
            continue
        left_top = min(line.y0 for line in left_lines)
        left_bottom = max(line.y1 for line in left_lines)
        right_top = min(line.y0 for line in right_lines)
        right_bottom = max(line.y1 for line in right_lines)
        overlap = min(left_bottom, right_bottom) - max(left_top, right_top)
        span = max(left_bottom, right_bottom) - min(left_top, right_top)
        if span <= 0 or overlap < 0.6 * span:
            continue
        if abs(left_top - right_top) > 36:
            continue
        if best is None or gap > best[0]:
            best = (gap, mid)
    return None if best is None else best[1]


def order_items(items, page_width: float):
    mid = column_midpoint(items, page_width)
    if mid is None:
        return sorted(items, key=lambda item: (item.y0, item.x0))
    full, left, right = [], [], []
    for item in items:
        if (item.x1 - item.x0) > page_width * 0.62:
            full.append(item)
        elif (item.x0 + item.x1) / 2 < mid:
            left.append(item)
        else:
            right.append(item)
    if not left or not right:
        return sorted(items, key=lambda item: (item.y0, item.x0))
    top = min(min(item.y0 for item in left), min(item.y0 for item in right))
    bottom = max(max(item.y1 for item in left), max(item.y1 for item in right))
    above = [item for item in full if item.y1 <= top + 4]
    below = [item for item in full if item.y0 >= bottom - 4]
    middle = [item for item in full if item not in above and item not in below]
    return (
        sorted(above, key=lambda item: item.y0)
        + sorted(middle, key=lambda item: item.y0)
        + sorted(left, key=lambda item: (item.y0, item.x0))
        + sorted(right, key=lambda item: (item.y0, item.x0))
        + sorted(below, key=lambda item: item.y0)
    )


def same_paragraph(prev: Line, line: Line, margin: float, body: float) -> bool:
    # A small-caps name on a body line can shift that line's size by about a point.
    if abs(line.size - prev.size) > max(1.25, body * 0.12):
        return False
    if line.ornament or prev.ornament:
        return False
    expected = max(prev.size, line.size) * 1.35
    if line.y0 - prev.y0 > expected * 1.45:
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
    groups: list[list[Line]] = []
    for line in lines:
        if not line.text and not line.ornament:
            continue
        if not groups or not same_paragraph(groups[-1][-1], line, margin, body):
            groups.append([line])
        else:
            groups[-1].append(line)
    return groups


# --- fonts ------------------------------------------------------------------

@dataclass
class FontRec:
    full: str
    style: Style
    advances: dict[str, float]
    font: object | None


def load_font_catalog(doc) -> list[FontRec]:
    catalog: list[FontRec] = []
    seen: set[int] = set()
    for index in range(doc.page_count):
        for entry in doc[index].get_fonts(full=True):
            xref = entry[0]
            if xref in seen:
                continue
            seen.add(xref)
            base = entry[3].split("+", 1)[-1]
            face = None
            advances: dict[str, float] = {}
            try:
                _name, _ext, _subtype, buffer = doc.extract_font(xref)
                if buffer:
                    face = fitz.Font(fontbuffer=buffer)
                    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz":
                        advances[ch] = face.glyph_advance(ord(ch))
            except Exception:
                face = None
            catalog.append(FontRec(base, style_from_font_name(base), advances, face))
    return catalog


def resolve_style(truncated: str, samples: list[tuple[str, float]], catalog: list[FontRec]) -> Style:
    key = truncated[:FONT_NAME_LIMIT]
    candidates = [rec for rec in catalog if rec.full[:FONT_NAME_LIMIT] == key or rec.full.startswith(truncated)]
    if not candidates:
        return style_from_font_name(truncated)
    if len(candidates) == 1:
        return candidates[0].style
    scored = []
    for rec in candidates:
        errors = []
        for ch, advance in samples:
            expected = rec.advances.get(ch)
            if expected and expected > 0 and advance > 0:
                errors.append(abs(expected - advance))
        scored.append((sum(errors) / len(errors) if errors else 9.0, rec))
    scored.sort(key=lambda item: item[0])
    return scored[0][1].style


# --- extraction -------------------------------------------------------------

def _char_baseline(item, y0: float, size: float) -> float:
    origin = item[2] if len(item) > 2 else None
    if isinstance(origin, (tuple, list)) and len(origin) >= 2:
        return float(origin[1])
    return y0 + size * 0.8


def group_spans(spans: list[Span]) -> list[list[Span]]:
    """Join glyphs that share a baseline. A wide gap stays a second column."""
    if not spans:
        return []
    ordered = sorted(spans, key=lambda span: span.origin_y)
    clusters: list[list[Span]] = []
    for span in ordered:
        if clusters:
            anchor = clusters[-1][0]
            tolerance = max(1.6, max(span.size, anchor.size) * 0.35)
            if abs(span.origin_y - anchor.origin_y) <= tolerance:
                clusters[-1].append(span)
                continue
        clusters.append([span])
    grouped: list[list[Span]] = []
    for cluster in clusters:
        cluster.sort(key=lambda span: (span.x0, span.x1))
        current: list[Span] = []
        right_edge = None
        for span in cluster:
            limit = max(span.size * 1.6, 14)
            if current and right_edge is not None and span.x0 <= right_edge + limit:
                current.append(span)
                right_edge = max(right_edge, span.x1)
                continue
            if current:
                grouped.append(current)
            current = [span]
            right_edge = span.x1
        if current:
            grouped.append(current)
    return grouped


def extract_lines(page, catalog: list[FontRec]) -> list[Line]:
    collected: list[Span] = []
    for raw in page.get_texttrace():
        size = float(raw.get("size") or 0)
        if size <= 0:
            continue
        font_name = raw.get("font") or ""
        color = as_int_color(raw.get("color"))
        ornament_font = any(token in font_name.lower() for token in ("ornament", "dingbat", "wingding"))
        chars = []
        samples = []
        private = 0
        kept = 0
        ornament_box = None
        ornament_baseline = None
        for item in raw.get("chars") or []:
            if len(item) < 4 or not isinstance(item[0], int):
                continue
            ch = chr(item[0])
            bbox = item[3]
            x0, y0, x1, y1 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
            baseline = _char_baseline(item, y0, size)
            if 0xE000 <= ord(ch) <= 0xF8FF:
                private += 1
                if ornament_box is None:
                    ornament_box = (x0, y0, x1, y1)
                    ornament_baseline = baseline
                else:
                    ornament_box = (
                        min(ornament_box[0], x0),
                        min(ornament_box[1], y0),
                        max(ornament_box[2], x1),
                        max(ornament_box[3], y1),
                    )
                continue
            if ch == "\u00ad":
                chars.append(("-", x0, x1, y0, baseline))
                continue
            if ord(ch) < 32:
                continue
            chars.append((ch, x0, x1, y0, baseline))
            if ch.isalpha() and x1 > x0:
                samples.append((ch, (x1 - x0) / size))
            kept += 1
        if not chars and private == 0:
            continue
        style = resolve_style(font_name, samples[:8], catalog)
        spacewidth = float(raw.get("spacewidth") or size * 0.25)
        if not chars:
            if ornament_box is None:
                ox0 = ox1 = origin_y = 0.0
            else:
                ox0, oy0, ox1, _oy1 = ornament_box
                origin_y = ornament_baseline if ornament_baseline is not None else oy0 + size * 0.8
            collected.append(
                Span(
                    text="",
                    size=size,
                    style=style,
                    color=color,
                    x0=ox0,
                    x1=ox1,
                    origin_y=origin_y,
                    ornament=True,
                    spacewidth=spacewidth,
                )
            )
            continue
        # One PDF text object can hold several visual lines. Split on baseline.
        for group in _baseline_groups(chars, size):
            baselines = [item[4] for item in group if len(item) > 4]
            collected.append(
                Span(
                    text="".join(item[0] for item in group),
                    size=size,
                    style=style,
                    color=color,
                    x0=min(item[1] for item in group),
                    x1=max(item[2] for item in group),
                    origin_y=statistics.median(baselines) if baselines else group[0][3] + size * 0.8,
                    chars=group,
                    ornament=ornament_font or (private > 0 and kept == 0),
                    spacewidth=spacewidth,
                )
            )
    lines = []
    for spans in group_spans(collected):
        line = Line(spans)
        if line.text or line.ornament:
            lines.append(line)
    return lines


def extract_images(page, doc, cache: dict[int, tuple[bytes, str]]) -> list[RawImage]:
    images: list[RawImage] = []
    try:
        info = page.get_image_info(xrefs=True)
    except Exception:
        return images
    seen: set[tuple[int, int, int, int, int]] = set()
    for item in info:
        xref = int(item.get("xref") or 0)
        bbox = tuple(float(v) for v in item.get("bbox") or (0, 0, 0, 0))
        if xref <= 0 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        display_w = bbox[2] - bbox[0]
        display_h = bbox[3] - bbox[1]
        if display_w < 28 or display_h < 16:
            continue
        if display_h < 18 and display_w > display_h * 6:
            continue
        key = (xref, round(bbox[0]), round(bbox[1]), round(bbox[2]), round(bbox[3]))
        if key in seen:
            continue
        seen.add(key)
        if xref not in cache:
            try:
                extracted = doc.extract_image(xref)
            except Exception:
                continue
            if not extracted or not extracted.get("image"):
                continue
            data = extracted["image"]
            ext = (extracted.get("ext") or "png").lower()
            if ext == "jpg":
                ext = "jpeg"
            if ext not in {"jpeg", "png", "gif"}:
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if pix.n >= 4:
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    data = pix.tobytes("png")
                    ext = "png"
                except Exception:
                    continue
            cache[xref] = (data, ext)
        data, ext = cache[xref]
        images.append(RawImage(xref=xref, bbox=bbox, data=data, ext=ext))
    return images


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


def strip_furniture(pages: list[Page], body: float) -> None:
    if len(pages) < 3:
        return
    counts: Counter[str] = Counter()
    zoned: list[tuple[Page, Line, str]] = []
    for page in pages:
        for line in page.lines:
            in_zone = line.y1 > page.height * 0.90 or line.y0 < page.height * 0.055
            if not in_zone or line.size >= body * 0.92:
                continue
            key = normalize_furniture(line.text)
            if len(key) < 3 and not line.text.strip().isdigit():
                continue
            zoned.append((page, line, key))
            if key:
                counts[key] += 1
    repeated = {key for key, count in counts.items() if count >= max(3, int(0.12 * len(pages)))}
    drop_ids = set()
    for _page, line, key in zoned:
        if key in repeated or line.text.strip().isdigit():
            drop_ids.add(id(line))
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
        page.poster = (body_chars < 140 and large_art) or (total_chars < 28 and (large_art or not page.lines))
        if page.poster:
            page.poster_jpeg = b""


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
                italic_ratio=(italic_letters / letter_count) if letter_count else 0.0,
                smallcaps_ratio=(small_letters / letter_count) if letter_count else 0.0,
                poster=page.poster,
            )
        )

    placed = place_images(blocks, page, image_names)
    return refine_blocks(placed, body_color)


def classify_group(group: list[Line], text: str, body: float, h1_cut: float) -> str:
    if any(line.ornament for line in group) and len(text) < 2:
        return "scene"
    size = group[0].size
    length = len(text)
    if size >= h1_cut and length <= 180 and len(group) <= 6:
        return "h1"
    if size >= body * 1.18 and length <= 320:
        if len(group) >= 3 or length > 120:
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


def refine_blocks(blocks: list[Block], body_color: int) -> list[Block]:
    merged: list[Block] = []
    for block in blocks:
        if (
            merged
            and block.kind == "h1"
            and merged[-1].kind == "h1"
            and block.page == merged[-1].page
            and abs(block.size - merged[-1].size) < 1.2
            and block.y0 - merged[-1].y1 < max(block.size * 0.85, 8)
        ):
            merged[-1] = merge_text_blocks(merged[-1], block, " ")
            continue
        merged.append(block)

    def neighbor(start: int, step: int) -> Block | None:
        index = start + step
        while 0 <= index < len(merged):
            if merged[index].kind not in {"image", "scene", "empty"}:
                return merged[index]
            index += step
        return None

    for index, block in enumerate(merged):
        nxt = neighbor(index, 1)
        prev = neighbor(index, -1)
        colored = color_distance(block.color, body_color) > 90 and body_color != 0
        if block.kind == "h2" and nxt and nxt.kind == "h1" and len(block.text) < 90:
            block.kind = "byline" if (block.smallcaps_ratio > 0.5 or colored) else "kicker"
        elif block.kind == "h2" and prev and prev.kind == "h1" and len(block.text) < 180 and block.nlines <= 4:
            block.kind = "deck"
        elif block.kind == "small":
            block.kind = "caption" if prev and prev.kind == "image" else "note"
    collapsed: list[Block] = []
    for block in merged:
        if block.kind == "scene" and (not collapsed or collapsed[-1].kind == "scene"):
            if collapsed and collapsed[-1].kind == "scene":
                continue
            if not collapsed:
                continue
        collapsed.append(block)
    while collapsed and collapsed[-1].kind == "scene":
        collapsed.pop()
    return rejoin_pull_quotes(absorb_split_pulls(collapsed))


def _first_letter(text: str) -> str:
    for char in text.lstrip():
        if char.isalpha():
            return char
    return ""


def absorb_split_pulls(blocks: list[Block]) -> list[Block]:
    """A pull quote's first line can be tagged as a heading when it stands alone."""
    index = 0
    while index < len(blocks) - 1:
        heading = blocks[index]
        follow = blocks[index + 1]
        if (
            heading.kind == "h2"
            and follow.kind == "pull"
            and not ends_sentence(heading.text)
            and _first_letter(follow.text).islower()
        ):
            merged = merge_text_blocks(heading, follow)
            merged.kind = "pull"
            blocks[index] = merged
            del blocks[index + 1]
            continue
        index += 1
    return blocks


def rejoin_pull_quotes(blocks: list[Block]) -> list[Block]:
    """Move a pull quote out of the sentence it was floated into."""
    index = 0
    while index < len(blocks):
        if blocks[index].kind != "pull":
            index += 1
            continue
        previous = index - 1
        while previous >= 0 and blocks[previous].kind in {"image", "scene"}:
            previous -= 1
        nxt = index + 1
        while nxt < len(blocks) and blocks[nxt].kind in {"image", "scene"}:
            nxt += 1
        before = blocks[previous] if previous >= 0 else None
        after = blocks[nxt] if nxt < len(blocks) else None
        if (
            before
            and after
            and before.kind == "p"
            and after.kind == "p"
            and not ends_sentence(before.text)
            and _first_letter(after.text).islower()
        ):
            blocks[previous] = merge_text_blocks(before, after)
            pull = blocks[index]
            del blocks[nxt]
            blocks.pop(index)
            blocks.insert(previous + 1, pull)
            continue
        index += 1
    return blocks


def merge_text_blocks(left: Block, right: Block, sep: str = " ") -> Block:
    del sep  # join_line_pieces inserts the space, or removes a line-end hyphen.
    if right.pieces:
        left.pieces = join_line_pieces([left.pieces, right.pieces])
    left.y1 = max(left.y1, right.y1)
    left.nlines += right.nlines
    return left


def merge_across_pages(pages: list[Page], page_blocks: dict[int, list[Block]], h1_pages: set[int]) -> None:
    numbers = [page.number for page in pages]
    for previous, nxt in zip(numbers, numbers[1:]):
        if nxt in h1_pages:
            continue
        left_blocks = page_blocks.get(previous) or []
        right_blocks = page_blocks.get(nxt) or []
        if not left_blocks or not right_blocks:
            continue
        if (
            left_blocks[-1].kind == "h2"
            and right_blocks[0].kind == "pull"
            and not ends_sentence(left_blocks[-1].text)
            and _first_letter(right_blocks[0].text).islower()
        ):
            merged = merge_text_blocks(left_blocks.pop(), right_blocks[0])
            merged.kind = "pull"
            right_blocks[0] = merged
        if not left_blocks or not right_blocks:
            continue
        tail = left_blocks[-1]
        head = right_blocks[0]
        if tail.poster or head.poster:
            continue
        if tail.kind == "pull" and len(left_blocks) >= 2 and left_blocks[-2].kind == "p":
            source = left_blocks[-2]
            source_at = len(left_blocks) - 2
        elif tail.kind == "p":
            source = tail
            source_at = len(left_blocks) - 1
        else:
            continue
        if source.poster or ends_sentence(source.text):
            continue
        # A pull quote floated to the top of the next page sits between the two halves.
        dest_at = 0
        while dest_at < len(right_blocks) and right_blocks[dest_at].kind in {"image", "scene", "pull"}:
            dest_at += 1
        if dest_at >= len(right_blocks):
            continue
        dest = right_blocks[dest_at]
        if dest.kind != "p" or dest.poster or dest.indented:
            continue
        if dest_at > 0 and not _first_letter(dest.text).islower():
            continue
        left_blocks[source_at] = merge_text_blocks(source, dest)
        del right_blocks[dest_at]


# --- chapters ---------------------------------------------------------------

def page_has_h1(blocks: list[Block], h1_cut: float) -> bool:
    return any(block.kind == "h1" and block.size >= h1_cut - 0.2 for block in blocks)


def chapter_body_chars(blocks: list[Block]) -> int:
    return sum(len(block.text) for block in blocks if block.kind in {"p", "pull", "note"})


def outline_starts(doc) -> dict[int, str]:
    toc = doc.get_toc() or []
    level1 = [(title.strip(), page) for level, title, page in toc if level == 1 and title and page]
    pages = {page for _title, page in level1}
    chosen = level1
    if len(pages) < 2:
        chosen = [(title.strip(), page) for level, title, page in toc if level <= 2 and title and page]
        pages = {page for _title, page in chosen}
    if len(pages) < 2:
        return {}
    starts: dict[int, str] = {}
    for title, page in chosen:
        starts.setdefault(int(page), title)
    return starts


def split_chapters(pages: list[Page], page_blocks: dict[int, list[Block]], h1_cut: float, outline: dict[int, str]) -> list[Chapter]:
    h1_pages = set(outline) if outline else {
        page.number for page in pages if page_has_h1(page_blocks.get(page.number, []), h1_cut)
    }
    raw: list[list[Page]] = []
    current: list[Page] = []
    for page in pages:
        if page.number in h1_pages and current:
            raw.append(current)
            current = []
        current.append(page)
    if current:
        raw.append(current)

    def blocks_of(group: list[Page]) -> list[Block]:
        out: list[Block] = []
        for page in group:
            out.extend(page_blocks.get(page.number, []))
        return out

    h1_text_counts: Counter[str] = Counter()
    raw_chapters = []
    for group in raw:
        blocks = blocks_of(group)
        for block in blocks:
            if block.kind == "h1":
                h1_text_counts[re.sub(r"\s+", " ", block.text.strip().lower())] += 1
        title = outline.get(group[0].number, "")
        raw_chapters.append(Chapter(blocks=blocks, outline_title=title))

    assembled: list[Chapter] = []
    held: list[Block] = []
    for chapter in raw_chapters:
        titles = [re.sub(r"\s+", " ", block.text.strip().lower()) for block in chapter.blocks if block.kind == "h1"]
        logo = bool(titles) and all(h1_text_counts[title] >= 2 and len(title.split()) <= 3 for title in titles)
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

    for index, chapter in enumerate(assembled, start=1):
        chapter.title = chapter.outline_title or choose_title(chapter.blocks, index, starts_at_front=(index == 1 and pages and chapter.blocks and chapter.blocks[0].page == 1))
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
    seen: list[str] = []
    for block in blocks:
        if block.kind == "h1" and block.text.strip():
            seen.append(re.sub(r"\s+", " ", block.text).strip())
        elif block.kind in {"p", "pull"} and len(block.text) > 80 and seen:
            break
    if seen:
        return seen[-1][:180]
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


# --- epub -------------------------------------------------------------------

def block_html(block: Block) -> str:
    if block.kind == "image" and block.image_href:
        alt = escape_text(block.alt)
        if block.image_class == "dropcap":
            return f'<img class="dropcap" src="{block.image_href}" alt="{alt}"/>'
        return f'<figure><img src="{block.image_href}" alt="{alt}"/></figure>'
    if block.kind == "scene":
        return '<hr class="scene"/>'
    inner = render_pieces(block.pieces, block.kind)
    if not inner.strip():
        return ""
    tag = {
        "h1": "h1",
        "h2": "h2",
        "byline": "p",
        "kicker": "p",
        "deck": "p",
        "pull": "blockquote",
        "caption": "p",
        "note": "p",
        "p": "p",
    }.get(block.kind, "p")
    css = {
        "byline": "byline",
        "kicker": "kicker",
        "deck": "deck",
        "caption": "caption",
        "note": "note",
    }.get(block.kind, "")
    if block.kind == "p" and block.indented:
        css = "indent"
    class_attr = f' class="{css}"' if css else ""
    if tag == "blockquote":
        return f"<blockquote><p>{inner}</p></blockquote>"
    return f"<{tag}{class_attr}>{inner}</{tag}>"


def chapter_inner_html(chapter: Chapter, poster_hrefs: dict[int, str]) -> str:
    parts: list[str] = []
    seen_posters: set[int] = set()
    for block in chapter.blocks:
        if block.poster:
            if block.page not in seen_posters and poster_hrefs.get(block.page):
                seen_posters.add(block.page)
                href = poster_hrefs[block.page]
                parts.append(f'<figure><img class="poster" src="{href}" alt=""/></figure>')
            continue
        rendered = block_html(block)
        if rendered:
            parts.append(rendered)
    return "\n".join(parts)


def xhtml_document(title: str, body: str) -> str:
    return (
        '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
        f"<title>{escape_text(title)}</title>"
        "</head><body>\n"
        f"{body}\n"
        "</body></html>"
    )


def write_epub(
    doc,
    chapters: list[Chapter],
    pages: list[Page],
    image_blobs: dict[str, tuple[bytes, str]],
    output: Path,
    title: str,
    author: str,
    language: str,
) -> list[str]:
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier(f"urn:uuid:{uuid.uuid4()}")
    book.set_title(title)
    book.set_language(language)
    if author:
        book.add_author(author)

    cover = doc[0].get_pixmap(matrix=fitz.Matrix(1.8, 1.8), alpha=False)
    book.set_cover("cover.jpeg", cover.tobytes("jpeg", jpg_quality=82))

    style = epub.EpubItem(uid="style", file_name="style.css", media_type="text/css", content=CSS.encode("utf-8"))
    book.add_item(style)

    for href, (data, ext) in image_blobs.items():
        media = {"jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif"}.get(ext, "image/jpeg")
        book.add_item(epub.EpubItem(uid="img-" + hashlib.sha1(href.encode()).hexdigest()[:12], file_name=href, media_type=media, content=data))

    poster_hrefs = {
        page.number: f"images/page-{page.number:03d}.jpeg" for page in pages if page.poster_jpeg
    }
    documents = []
    for index, chapter in enumerate(chapters, start=1):
        body = chapter_inner_html(chapter, poster_hrefs)
        if not body.strip():
            continue
        document = epub.EpubHtml(
            title=chapter.title,
            file_name=f"chap_{index:03d}.xhtml",
            lang=language,
        )
        document.content = xhtml_document(chapter.title, body)
        document.add_item(style)
        book.add_item(document)
        documents.append(document)

    book.toc = tuple(documents)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *documents]
    output.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(output), book)
    return [document.title for document in documents]


def render_posters(doc, pages: list[Page]) -> dict[str, tuple[bytes, str]]:
    blobs: dict[str, tuple[bytes, str]] = {}
    for page in pages:
        if not page.poster:
            continue
        pix = doc[page.number - 1].get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
        data = pix.tobytes("jpeg", jpg_quality=80)
        href = f"images/page-{page.number:03d}.jpeg"
        page.poster_jpeg = data
        blobs[href] = (data, "jpeg")
    return blobs


def convert(
    pdf_path: Path,
    output: Path | None = None,
    title: str | None = None,
    author: str | None = None,
    language: str = "en",
    dump_dir: Path | None = None,
    verbose: bool = False,
) -> Result:
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)
    doc = fitz.open(pdf_path)
    if doc.needs_pass:
        raise ValueError(f"{pdf_path.name} is password-protected.")
    if doc.page_count == 0:
        raise ValueError(f"{pdf_path.name} has no pages.")

    catalog = load_font_catalog(doc)
    image_cache: dict[int, tuple[bytes, str]] = {}
    pages: list[Page] = []
    for index in range(doc.page_count):
        fitz_page = doc[index]
        pages.append(
            Page(
                number=index + 1,
                width=float(fitz_page.rect.width),
                height=float(fitz_page.rect.height),
                lines=extract_lines(fitz_page, catalog),
                images=extract_images(fitz_page, doc, image_cache),
            )
        )
    text_chars = sum(len(line.text) for page in pages for line in page.lines)
    if text_chars < 40:
        raise ValueError(
            f"{pdf_path.name} has almost no extractable text. It looks scanned. "
            "This script does not OCR pages."
        )

    body = detect_body_size(pages)
    cutoff = heading_cutoff(pages, body)
    strip_furniture(pages, body)
    body_color = dominant_color(pages, body)
    mark_posters(pages, body)

    image_names: dict[int, str] = {}
    image_blobs: dict[str, tuple[bytes, str]] = {}
    for xref, (data, ext) in image_cache.items():
        digest = hashlib.sha1(data).hexdigest()[:12]
        suffix = "jpg" if ext == "jpeg" else ext
        href = f"images/img-{digest}.{suffix}"
        image_names[xref] = href
        image_blobs[href] = (data, "jpeg" if ext == "jpeg" else ext)

    page_blocks = {
        page.number: build_blocks(page, body, cutoff, body_color, image_names) for page in pages
    }
    used_hrefs = {
        block.image_href
        for blocks in page_blocks.values()
        for block in blocks
        if block.image_href and not block.poster
    }
    image_blobs = {href: blob for href, blob in image_blobs.items() if href in used_hrefs}
    outline = outline_starts(doc)
    h1_pages = set(outline) if outline else {
        page.number for page in pages if page_has_h1(page_blocks.get(page.number, []), cutoff)
    }
    merge_across_pages(pages, page_blocks, h1_pages)
    chapters = split_chapters(pages, page_blocks, cutoff, outline)
    poster_blobs = render_posters(doc, pages)
    image_blobs.update(poster_blobs)

    meta = doc.metadata or {}
    book_title = title.strip() if title else infer_book_title(pages, meta.get("title"))
    book_author = author.strip() if author else ""
    if not book_author and not junk_metadata(meta.get("author")):
        book_author = meta["author"].strip()

    if verbose:
        print(f"pages {doc.page_count}  body {body:.1f}pt  heading cutoff {cutoff:.1f}pt  color #{body_color:06x}")
        print(f"outline entries used: {len(outline)}")

    destination = output or pdf_path.with_suffix(".epub")
    written_titles = write_epub(doc, chapters, pages, image_blobs, destination, book_title, book_author, language)
    if dump_dir:
        dump_dir.mkdir(parents=True, exist_ok=True)
        poster_hrefs = {page.number: f"images/page-{page.number:03d}.jpeg" for page in pages if page.poster_jpeg}
        for index, chapter in enumerate(chapters, start=1):
            body_html = chapter_inner_html(chapter, poster_hrefs)
            (dump_dir / f"{index:02d}-{slug(chapter.title)}.html").write_text(
                xhtml_document(chapter.title, body_html),
                encoding="utf-8",
            )
    words = sum(len(block.text.split()) for chapter in chapters for block in chapter.blocks if not block.poster)
    doc.close()
    return Result(
        output=destination,
        title=book_title,
        author=book_author,
        chapters=written_titles,
        page_count=len(pages),
        image_count=len(image_blobs),
        word_count=words,
    )


def slug(text: str) -> str:
    slug_text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug_text[:48] or "chapter"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert a PDF into an EPUB with chapters, paragraphs, images, and formatting.")
    parser.add_argument("pdf", type=Path, help="PDF to convert")
    parser.add_argument("-o", "--output", type=Path, help="EPUB path. Defaults to the PDF name with an .epub suffix.")
    parser.add_argument("--title", help="Book title. Defaults to the PDF metadata or the cover.")
    parser.add_argument("--author", help="Author. Defaults to the PDF metadata.")
    parser.add_argument("--language", default="en", help="Language code. Default: en")
    parser.add_argument("--dump-dir", type=Path, help="Also write each chapter as an HTML file in this directory.")
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
