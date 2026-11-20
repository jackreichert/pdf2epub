"""Read fonts, text lines, and images from a PDF page."""

from __future__ import annotations

import statistics
from dataclasses import dataclass

import fitz

from pdf2epub_lib.model import FONT_NAME_LIMIT, Line, RawImage, Span, Style
from pdf2epub_lib.text import _baseline_groups, as_int_color, style_from_font_name

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

def _vertical_direction(raw) -> bool:
    direction = raw.get("dir") or (1, 0)
    try:
        return abs(float(direction[1])) > abs(float(direction[0]))
    except (TypeError, ValueError, IndexError):
        return False


def _space_vertical(chars):
    """Keep upright-reading order for text set on a vertical baseline, and split words."""
    if len(chars) < 2:
        return list(chars)
    positions = [item[4] if len(item) > 4 else item[3] for item in chars]
    steps = [abs(positions[index] - positions[index - 1]) for index in range(1, len(positions))]
    typical = statistics.median(steps) if steps else 0
    spaced = [chars[0]]
    for index, item in enumerate(chars[1:], start=1):
        if typical and steps[index - 1] > typical * 1.75:
            spaced.append((" ", item[1], item[1], item[3], positions[index]))
        spaced.append(item)
    return spaced


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
            limit = max(span.size * 0.5, 3.5)
            same_size = True
            if current:
                sizes = [item.size for item in current] + [span.size]
                # A display word can share a baseline with the deck it sits on.
                same_size = max(sizes) <= min(sizes) * 1.8 or max(sizes) - min(sizes) <= 8
            overlaps_previous = (
                current
                and right_edge is not None
                and span.x1 <= right_edge - 4
                and (span.x1 - span.x0) < (right_edge - current[0].x0) * 0.35
            )
            if current and right_edge is not None and same_size and span.x0 <= right_edge + limit and not overlaps_previous:
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


def _glyphs_from_trace(raw, size: float):
    """Return kept glyphs, font samples, and a private-use ornament box."""
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
    return chars, samples, private, kept, ornament_box, ornament_baseline


def _ornament_span(size, style, color, spacewidth, ornament_box, ornament_baseline) -> Span:
    if ornament_box is None:
        ox0 = ox1 = origin_y = 0.0
    else:
        ox0, oy0, ox1, _oy1 = ornament_box
        origin_y = ornament_baseline if ornament_baseline is not None else oy0 + size * 0.8
    return Span(
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


def _spans_from_trace(raw, catalog: list[FontRec]) -> list[Span]:
    size = float(raw.get("size") or 0)
    if size <= 0:
        return []
    font_name = raw.get("font") or ""
    chars, samples, private, kept, ornament_box, ornament_baseline = _glyphs_from_trace(raw, size)
    if not chars and private == 0:
        return []
    style = resolve_style(font_name, samples[:8], catalog)
    spacewidth = float(raw.get("spacewidth") or size * 0.25)
    if not chars:
        return [_ornament_span(size, style, as_int_color(raw.get("color")), spacewidth, ornament_box, ornament_baseline)]
    # One PDF text object can hold several visual lines. Split on baseline.
    # Vertical credits stay in stream order; a y-sort would reverse them.
    groups = [_space_vertical(chars)] if _vertical_direction(raw) else _baseline_groups(chars, size)
    ornament_font = any(token in font_name.lower() for token in ("ornament", "dingbat", "wingding"))
    color = as_int_color(raw.get("color"))
    spans = []
    for group in groups:
        baselines = [item[4] for item in group if len(item) > 4]
        spans.append(
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
    return spans


def extract_lines(page, catalog: list[FontRec]) -> list[Line]:
    collected: list[Span] = []
    for raw in page.get_texttrace():
        collected.extend(_spans_from_trace(raw, catalog))
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

