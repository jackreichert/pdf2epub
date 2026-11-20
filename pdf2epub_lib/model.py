"""Shared page, line, and block records.

Line.text asks the text module for styled pieces. That import stays inside
the property so this module can load before the text module.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

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
  margin: 0 0 0.85em;
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
        # line_pieces needs Line, and Line.text needs line_pieces.
        from pdf2epub_lib.text import line_pieces
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

