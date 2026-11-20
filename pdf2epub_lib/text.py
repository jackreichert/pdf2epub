"""Glyph order, word spaces, hyphen joins, and emphasis."""

from __future__ import annotations

import html
import re
import statistics
import unicodedata

from pdf2epub_lib.model import HYPHENS, LIGATURES, Line, Span, Style, _flat_chars

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
    if not letters or not (all(ch.isupper() for ch in letters) or all(ch.islower() for ch in letters)):
        return text

    def cap(word: str) -> str:
        if "&" in word:
            return word.upper()
        chars = list(word)
        for index, ch in enumerate(chars):
            if ch.isalpha():
                chars[index] = ch.upper()
                for later in range(index + 1, len(chars)):
                    chars[later] = chars[later].lower()
                break
        return "".join(chars)

    return " ".join(cap(word) for word in text.split(" "))


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
        next_word = group[0][1].split(" ", 1)[0]
        # "behind-" + "the-scenes" keeps its hyphen. "disappoint-" + "ments" does not.
        compound = "-" in next_word or "\u2010" in next_word
        if text.endswith(HYPHENS) and head.islower() and not compound:
            accumulated[-1] = (style, text[:-1])
        elif text.endswith(HYPHENS):
            pass
        elif text and group[0][1] and not text.endswith(" ") and not group[0][1].startswith(" "):
            accumulated[-1] = (style, text + " ")
        accumulated.extend(group)
    return merge_pieces(accumulated)


def _needs_word_space(previous: Span, span: Span, previous_text: str, text: str) -> bool:
    """True when two spans are separate words whose space glyph was never written.

    Illustrator often emits one text object per word and positions it closer than
    the font's nominal space width. A gap of about a quarter of an em is that
    space. A lone hyphen stays attached, so "twenty" + "-" + "two" stays one word.
    """
    if not text or not previous_text:
        return False
    if text[:1].isspace() or previous_text[-1:].isspace():
        return False
    if previous_text.strip() in {"-", "‐"} or text.strip() in {"-", "‐"}:
        return False
    gap = span.x0 - previous.x1
    if gap <= 0.4:
        return False
    size = max(previous.size, span.size, 1.0)
    return gap > size * 0.25


def _squeeze_spaces(pieces: list[tuple[Style, str]]) -> list[tuple[Style, str]]:
    squeezed: list[tuple[Style, str]] = []
    for style, text in pieces:
        text = re.sub(r" {2,}", " ", text)
        if squeezed and squeezed[-1][1].endswith(" ") and text.startswith(" "):
            text = text[1:]
        if text:
            squeezed.append((style, text))
    return merge_pieces(squeezed)


def line_pieces(line: Line) -> list[tuple[Style, str]]:
    ordered = sorted(line.spans, key=lambda span: span.x0)
    pieces: list[tuple[Style, str]] = []
    previous: Span | None = None
    all_chars: list[tuple[str, float, float]] = []
    for span in ordered:
        text = chars_to_text(span)
        all_chars.extend((ch, x0, x1) for ch, x0, x1, _y in _flat_chars(span) if ch.strip() or ch == " ")
        if previous is not None and pieces and _needs_word_space(previous, span, pieces[-1][1], text):
            style, previous_text = pieces[-1]
            pieces[-1] = (style, previous_text + " ")
        if text:
            pieces.append((span.style, text))
            previous = span
    pieces = _squeeze_spaces(pieces)
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


def _first_letter(text: str) -> str:
    for char in text.lstrip():
        if char.isalpha():
            return char
    return ""

