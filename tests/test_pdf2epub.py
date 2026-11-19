"""Tests for the PDF-to-EPUB conversion helpers and a small synthetic book."""

import sys
import unittest
import zipfile
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pdf2epub as p2e


class TextTests(unittest.TestCase):
    def test_dehyphenates_a_wrapped_word(self):
        pieces = p2e.join_line_pieces(
            [
                [(p2e.Style(), "The disappoint-")],
                [(p2e.Style(), "ments continued.")],
            ]
        )
        self.assertEqual(p2e.pieces_text(pieces), "The disappointments continued.")

    def test_keeps_a_hyphen_before_a_capital(self):
        pieces = p2e.join_line_pieces(
            [
                [(p2e.Style(), "pro-")],
                [(p2e.Style(), "Israel politics")],
            ]
        )
        self.assertEqual(p2e.pieces_text(pieces), "pro-Israel politics")

    def test_ends_sentence(self):
        self.assertFalse(p2e.ends_sentence("Paul Samuelson"))
        self.assertTrue(p2e.ends_sentence("ebbs of our influence."))
        self.assertTrue(p2e.ends_sentence("time.”"))

    def test_letterspacing_uses_wider_gaps_as_word_spaces(self):
        # Letters are 10 units apart inside a word and 22 apart between words.
        chars = []
        x = 0
        for word in ("sapir", "ideas"):
            for ch in word:
                chars.append((ch, float(x), float(x + 6)))
                x += 10
            x += 12
        text = "s a p i r i d e a s"
        collapsed = p2e.collapse_tracked(text, chars, size=20)
        self.assertEqual(collapsed, "sapir ideas")

    def test_font_name_styles(self):
        italic = p2e.style_from_font_name("PYOCNO+PSFournierStd-Italic")
        self.assertTrue(italic.italic)
        self.assertFalse(italic.bold)
        small = p2e.style_from_font_name("PSFournierStdPetit-Regular-SC700")
        self.assertTrue(small.smallcaps)
        bold = p2e.style_from_font_name("BrandonGrotesque-Bold-SC700")
        self.assertTrue(bold.bold)
        self.assertTrue(bold.smallcaps)
        roman = p2e.style_from_font_name("PSFournierStdGrand-Light")
        self.assertFalse(roman.italic)

    def test_two_columns_read_left_then_right(self):
        lines = []
        for y in range(100, 400, 20):
            lines.append(_line(x0=50, x1=200, y0=y, text=f"L{y}"))
            lines.append(_line(x0=300, x1=450, y0=y, text=f"R{y}"))
        ordered = p2e.order_items(lines, page_width=500)
        texts = [line.text for line in ordered]
        self.assertLess(texts.index("L380"), texts.index("R100"))

    def test_a_drop_cap_wrap_is_not_treated_as_two_columns(self):
        lines = []
        for y in range(80, 180, 16):
            lines.append(_line(x0=180, x1=400, y0=y, text=f"wrap {y}"))
        for y in range(180, 500, 16):
            lines.append(_line(x0=60, x1=400, y0=y, text=f"full {y}"))
        ordered = p2e.order_items(lines, page_width=460)
        texts = [line.text for line in ordered]
        self.assertEqual(texts[0], "wrap 80")
        self.assertLess(texts.index("wrap 160"), texts.index("full 180"))

    def test_modest_indent_is_not_an_image_wrap(self):
        prev = _line(x0=72, x1=400, y0=100, text="Previous sentence.")
        indented = _line(x0=86, x1=400, y0=116, text="A new paragraph.")
        wrapped = _line(x0=190, x1=400, y0=116, text="Beside a picture.")
        self.assertTrue(p2e.is_first_line_indent(indented, prev, margin=72, body=11))
        self.assertFalse(p2e.is_first_line_indent(wrapped, prev, margin=72, body=11))

    def test_a_pull_quote_does_not_split_a_sentence(self):
        style = p2e.Style()
        blocks = [
            p2e.Block("p", [(style, "Yet")], 10, 1, 0, 12, 60),
            p2e.Block("pull", [(style, "A sentence repeated from later.")], 14, 1, 20, 50, 80),
            p2e.Block("p", [(style, "when the story continues.")], 10, 1, 60, 80, 60),
        ]
        joined = p2e.rejoin_pull_quotes(blocks)
        self.assertEqual(joined[0].text, "Yet when the story continues.")
        self.assertEqual(joined[1].kind, "pull")
        self.assertEqual(len(joined), 2)

    def test_junk_metadata(self):
        self.assertTrue(p2e.junk_metadata("String_22_USE.indd"))
        self.assertTrue(p2e.junk_metadata(""))
        self.assertFalse(p2e.junk_metadata("Sapir: Influence"))

    def test_ink_gap_is_not_a_word_space(self):
        span = p2e.Span(
            text="",
            size=10,
            style=p2e.Style(),
            color=0,
            x0=0,
            x1=30,
            origin_y=20,
            chars=[("l", 0, 2, 10), ("u", 8, 14, 10)],
        )
        self.assertEqual(p2e.chars_to_text(span), "lu")

    def test_baseline_groups_split_a_wrapped_text_object(self):
        chars = [
            ("T", 10, 22, 128.2),
            ("h", 22, 34, 128.4),
            ("e", 34, 44, 128.2),
            (" ", 360, 368, 128.2),
            ("J", 10, 24, 162.2),
            ("e", 24, 36, 162.4),
        ]
        groups = p2e._baseline_groups(chars, 30)
        self.assertEqual(["".join(item[0] for item in group) for group in groups], ["The ", "Je"])

    def test_same_baseline_keeps_a_closing_paren(self):
        spans = [
            p2e.Span("The Anxiety of ", 30, p2e.Style(), 0, 60.4, 253.7, 152.17),
            p2e.Span("Waning", 30, p2e.Style(), 0, 262.3, 355.4, 152.17),
            p2e.Span("(", 30, p2e.Style(), 0, 253.4, 262.6, 152.47),
            p2e.Span(")", 30, p2e.Style(), 0, 355.1, 364.3, 152.47),
        ]
        grouped = p2e.group_spans(spans)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(p2e.Line(grouped[0]).text, "The Anxiety of (Waning)")

    def test_a_wide_gap_on_one_baseline_stays_two_lines(self):
        spans = [
            p2e.Span("Left words", 11, p2e.Style(), 0, 50, 180, 100),
            p2e.Span("Right words", 11, p2e.Style(), 0, 300, 430, 100),
        ]
        grouped = p2e.group_spans(spans)
        self.assertEqual(len(grouped), 2)

    def test_a_space_glyph_is_not_doubled(self):
        spans = [
            p2e.Span("ow", 8.4, p2e.Style(), 0, 175.5, 190.8, 296, chars=[("o", 175.5, 183, 289), ("w", 183, 190.8, 289)], spacewidth=2.3),
            p2e.Span(" ", 12, p2e.Style(), 0, 191.8, 195.2, 296, chars=[(" ", 191.8, 195.2, 289)], spacewidth=3),
            p2e.Span("has", 8.4, p2e.Style(), 0, 199.5, 219.0, 296, chars=[("h", 199.5, 205, 289), ("a", 205, 212, 289), ("s", 212, 219, 289)], spacewidth=2.3),
        ]
        self.assertEqual(p2e.Line(spans).text, "ow has")

    def test_hebrew_line_is_stored_in_logical_order(self):
        # Drawn left to right. The vowel sits on the leftmost letter.
        chars = [
            ("א", 10, 18, 50),
            ("\u05b7", 12, 16, 48),
            ("ב", 20, 28, 50),
            ("ג", 30, 38, 50),
        ]
        groups = p2e._baseline_groups(chars, 18)
        self.assertEqual("".join(item[0] for item in groups[0]), "גבא\u05b7")


class ConvertTests(unittest.TestCase):
    def test_synthetic_pdf_becomes_chapters(self):
        folder = Path(__file__).resolve().parents[1] / ".test-out"
        folder.mkdir(exist_ok=True)
        pdf_path = folder / "sample.pdf"
        epub_path = folder / "sample.epub"
        _write_sample(pdf_path)
        result = p2e.convert(pdf_path, output=epub_path, title="Sample Book", author="Tester")
        self.assertEqual(result.title, "Sample Book")
        self.assertEqual(result.chapters, ["The Sample Chapter", "Second Thoughts"])
        with zipfile.ZipFile(epub_path) as archive:
            names = archive.namelist()
            html = "\n".join(
                archive.read(name).decode("utf-8")
                for name in names
                if name.endswith(".xhtml") and "chap_" in name
            )
        self.assertIn("hyphenated across", html)
        self.assertIn("test document", html)
        self.assertIn("should stay", html)
        self.assertIn("<em>", html)
        self.assertTrue(any(name.startswith("EPUB/images/") or name.startswith("images/") for name in names))


def _line(x0, x1, y0, text, size=11):
    span = p2e.Span(
        text=text,
        size=size,
        style=p2e.Style(),
        color=0,
        x0=x0,
        x1=x1,
        origin_y=y0 + size,
        chars=[],
    )
    return p2e.Line([span])


def _write_sample(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 80), "The Sample Chapter", fontsize=24, fontname="times-bold")
    rect = fitz.Rect(72, 110, 400, 250)
    page.insert_textbox(
        rect,
        "This paragraph is long enough to wrap across several lines in the test document so the extractor must join them.",
        fontsize=11,
        fontname="times-roman",
    )
    page.insert_text((72, 300), "Words are hyphen-", fontsize=11, fontname="times-roman")
    page.insert_text((72, 314), "ated across a line.", fontsize=11, fontname="times-roman")
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 48, 48), 0)
    pix.set_rect(pix.irect, (180, 40, 40))
    page.insert_image(fitz.Rect(72, 340, 140, 408), pixmap=pix)

    second = doc.new_page()
    second.insert_text((72, 80), "Second Thoughts", fontsize=24, fontname="times-bold")
    second.insert_textbox(
        fitz.Rect(72, 110, 420, 360),
        "An italic follow up that should stay emphasized. " * 8,
        fontsize=11,
        fontname="times-italic",
    )
    doc.save(path)
    doc.close()


if __name__ == "__main__":
    unittest.main()
