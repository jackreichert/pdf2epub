"""Tests for the PDF-to-EPUB conversion helpers and a small synthetic book."""

import sys
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
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

    def test_keeps_a_hyphen_inside_a_compound(self):
        pieces = p2e.join_line_pieces(
            [
                [(p2e.Style(), "the behind-")],
                [(p2e.Style(), "the-scenes plumbing")],
            ]
        )
        self.assertEqual(p2e.pieces_text(pieces), "the behind-the-scenes plumbing")

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

    def test_a_full_width_lead_stays_above_the_columns(self):
        lines = []
        for y in (80, 100, 120):
            lines.append(_line(x0=40, x1=500, y0=y, text=f"Lead {y} stays above both columns."))
        for y in range(160, 320, 16):
            lines.append(_line(x0=40, x1=250, y0=y, text=f"L{y}"))
            lines.append(_line(x0=270, x1=480, y0=y, text=f"R{y}"))
        texts = [line.text for line in p2e.order_items(lines, page_width=540)]
        self.assertLess(texts.index("Lead 120 stays above both columns."), texts.index("L160"))
        self.assertLess(texts.index("L304"), texts.index("R160"))

    def test_a_sidebar_does_not_split_the_article(self):
        lines = []
        for y in range(100, 500, 16):
            lines.append(_line(x0=40, x1=230, y0=y, text=f"L{y}"))
        for y in range(100, 280, 14):
            lines.append(_line(x0=250, x1=340, y0=y, text=f"S1-{y}"))
            lines.append(_line(x0=360, x1=450, y0=y, text=f"S2-{y}"))
        for y in range(300, 500, 16):
            lines.append(_line(x0=250, x1=460, y0=y, text=f"R{y}"))
        texts = [line.text for line in p2e.order_items(lines, page_width=520)]
        self.assertLess(texts.index("L484"), texts.index("R300"))
        self.assertLess(texts.index("R492"), texts.index("S1-100"))
        self.assertLess(texts.index("S1-268"), texts.index("S2-100"))

    def test_a_third_column_keeps_the_line_above_the_title(self):
        lines = [_line(x0=40, x1=320, y0=70, text="Title stays above the columns", size=22)]
        for y in range(180, 420, 16):
            lines.append(_line(x0=40, x1=180, y0=y, text=f"L{y}"))
            lines.append(_line(x0=200, x1=340, y0=y, text=f"M{y}"))
        # The rail starts beside the title. Its heading overlaps the first body line,
        # so the opening line cannot join that stream.
        lines.append(_line(x0=360, x1=470, y0=100, text="top of the rail"))
        lines.append(_line(x0=360, x1=450, y0=120, text="HEADING", size=18))
        for y in range(136, 420, 16):
            lines.append(_line(x0=360, x1=500, y0=y, text=f"R{y}"))
        texts = [line.text for line in p2e.order_items(lines, page_width=540)]
        self.assertLess(texts.index("Title stays above the columns"), texts.index("L180"))
        self.assertLess(texts.index("L404"), texts.index("M180"))
        self.assertLess(texts.index("M404"), texts.index("top of the rail"))
        self.assertLess(texts.index("top of the rail"), texts.index("HEADING"))
        self.assertLess(texts.index("HEADING"), texts.index("R136"))

    def test_a_column_break_does_not_end_the_sentence(self):
        style = p2e.Style()
        left = p2e.Block(
            kind="p",
            pieces=[(style, "and, as researchers recently discovered")],
            size=9,
            page=3,
            y0=1,
            y1=2,
            x0=40,
        )
        right = p2e.Block(
            kind="p",
            pieces=[(style, "that tracking continues.")],
            size=9,
            page=3,
            y0=1,
            y1=2,
            x0=260,
        )
        joined = p2e.join_broken_sentences([left, right])
        self.assertEqual(len(joined), 1)
        self.assertIn("recently discovered that tracking", joined[0].text)

    def test_a_name_split_across_columns_is_joined(self):
        style = p2e.Style()
        left = p2e.Block("p", [(style, "By law in the United")], 9, 22, 1, 2, 40)
        right = p2e.Block("p", [(style, "States, they have access.")], 9, 22, 1, 2, 200)
        joined = p2e.join_broken_sentences([left, right])
        self.assertEqual(joined[0].text, "By law in the United States, they have access.")

    def test_a_paragraph_break_does_not_join_two_capitalized_lines(self):
        style = p2e.Style()
        blocks = [
            p2e.Block("p", [(style, "– Phil Knight, founder, Nike")], 12, 3, 132, 144, 235),
            p2e.Block("p", [(style, "Finance is nothing more than watching money.")], 12, 3, 168, 180, 72),
        ]
        joined = p2e.join_broken_sentences(blocks)
        self.assertEqual(len(joined), 2)
        self.assertTrue(joined[0].text.endswith("Nike"))

    def test_wrapped_lines_join_until_the_next_paragraph(self):
        style = p2e.Style()

        def para(text, y0):
            return p2e.Block("p", [(style, text)], 12, 4, y0, y0 + 12, 72)

        joined = p2e.join_broken_sentences(
            [
                para("It makes much more sense for a business to", 100),
                para("give its existing customers more of what they want", 124),
                para("instead of starting new", 148),
                para("businesses in another city.", 210),
            ]
        )
        self.assertEqual(len(joined), 2)
        self.assertIn("business to give its existing", joined[0].text)
        self.assertTrue(joined[0].text.endswith("starting new"))
        self.assertTrue(joined[1].text.startswith("businesses"))

    def test_an_indented_quotation_stays_in_the_sentence(self):
        style = p2e.Style()
        blocks = [
            p2e.Block("p", [(style, "must be a philosophy that discerns")], 10, 160, 180, 191, 62),
            p2e.Block("p", [(style, "an organic connection among the experiences.")], 9.5, 160, 212, 222, 74),
        ]
        joined = p2e.join_broken_sentences(blocks)
        self.assertIn("discerns an organic connection", joined[0].text)

    def test_a_price_continues_the_sentence(self):
        style = p2e.Style()
        blocks = [
            p2e.Block("p", [(style, "our monthly expenses are at least")], 12, 6, 338, 350, 72),
            p2e.Block("p", [(style, "$300,000 a month.")], 12, 6, 362, 374, 72),
        ]
        joined = p2e.join_broken_sentences(blocks)
        self.assertIn("at least $300,000", joined[0].text)

    def test_an_indented_wrap_that_starts_lowercase_is_joined(self):
        style = p2e.Style()
        left = p2e.Block("p", [(style, "view themselves as")], 9, 26, 1, 2, 40)
        right = p2e.Block("p", [(style, "working in the gig economy.")], 9, 26, 1, 2, 210, indented=True)
        joined = p2e.join_broken_sentences([left, right])
        self.assertIn("themselves as working", joined[0].text)

    def test_two_pull_quotes_do_not_split_a_sentence(self):
        style = p2e.Style()
        blocks = [
            p2e.Block("p", [(style, "feedback that")], 10, 1, 0, 12, 60),
            p2e.Block("pull", [(style, "A floated sentence.")], 14, 1, 20, 40, 80),
            p2e.Block("pull", [(style, "Another floated sentence.")], 14, 1, 40, 60, 80),
            p2e.Block("p", [(style, "tempers our behavior.")], 10, 1, 70, 90, 60),
        ]
        joined = p2e.rejoin_pull_quotes(blocks)
        self.assertEqual(joined[0].text, "feedback that tempers our behavior.")
        self.assertEqual([block.kind for block in joined[1:]], ["pull", "pull"])

    def test_a_sidebar_does_not_swallow_the_next_page(self):
        style = p2e.Style()

        def block(kind, text, page, x=40):
            return p2e.Block(kind, [(style, text)], 10, page, 0, 12, x)

        pages = [p2e.Page(1, 500, 700, [], []), p2e.Page(2, 500, 700, [], [])]
        page_blocks = {
            1: [
                block("p", "people who were setting", 1),
                block("p", "THE AUTHOR", 1, 260),
                block("h1", "LESLIE K. JOHN", 1, 260),
                block("p", "She writes about its dangers.", 1, 260),
            ],
            2: [
                block("pull", "A QUOTE FROM THE ARTICLE.", 2),
                block("p", "up a digital wallet were offered a service.", 2),
            ],
        }
        p2e.merge_across_pages(pages, page_blocks, set())
        self.assertIn("setting up a digital wallet", page_blocks[1][0].text)
        self.assertEqual(page_blocks[1][2].text, "LESLIE K. JOHN")
        self.assertEqual(page_blocks[2][0].kind, "pull")
        self.assertEqual(len(page_blocks[2]), 1)

    def test_a_pull_quote_page_does_not_split_the_sentence(self):
        style = p2e.Style()

        def block(kind, text, page):
            return p2e.Block(kind, [(style, text)], 10, page, 0, 12, 40)

        pages = [p2e.Page(n, 500, 700, [], []) for n in (1, 2, 3)]
        page_blocks = {
            1: [block("p", "We often don’t get the same rich, visceral feedback that", 1)],
            2: [block("pull", "ONLINE, THE BOUNDARIES ARE BLURRED.", 2)],
            3: [
                block("pull", "IN A STUDY PEOPLE DISCLOSED EMAIL ADDRESSES.", 3),
                block("p", "tempers our behavior in the off-line world.", 3),
            ],
        }
        p2e.merge_across_pages(pages, page_blocks, set())
        self.assertIn("feedback that tempers our behavior", page_blocks[1][0].text)
        self.assertEqual(page_blocks[2][0].kind, "pull")
        self.assertEqual(page_blocks[3][0].kind, "pull")
        self.assertFalse(any(block.text.startswith("tempers") for block in page_blocks[3]))

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

    def test_a_column_gutter_is_not_one_line(self):
        spans = [
            p2e.Span("Left column text", 9, p2e.Style(), 0, 42, 247, 600),
            p2e.Span("Right column text", 9, p2e.Style(), 0, 258, 460, 600.2),
        ]
        grouped = p2e.group_spans(spans)
        self.assertEqual(len(grouped), 2)

    def test_a_display_word_does_not_glue_to_the_deck(self):
        spans = [
            p2e.Span("TRACKED", 192, p2e.Style(), 0, 40, 500, 640),
            p2e.Span("COMPANIES ARE TAKING", 33, p2e.Style(), 0, 43, 500, 642),
        ]
        texts = ["".join(span.text for span in group) for group in p2e.group_spans(spans)]
        self.assertEqual(texts, ["TRACKED", "COMPANIES ARE TAKING"])

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

    def test_a_positioned_word_gap_becomes_a_space(self):
        spans = []
        x = 72.0
        for word in ("Your", "friendly", "neighborhood"):
            width = len(word) * 6
            spans.append(
                p2e.Span(
                    word,
                    12,
                    p2e.Style(),
                    0,
                    x,
                    x + width,
                    100,
                    chars=[(ch, x + index * 6, x + index * 6 + 5, 90) for index, ch in enumerate(word)],
                    spacewidth=6.67,
                )
            )
            x += width + 3.34
        self.assertEqual(p2e.Line(spans).text, "Your friendly neighborhood")

    def test_a_split_hyphen_stays_inside_the_word(self):
        spans = [
            p2e.Span("twenty", 7.5, p2e.Style(), 0, 10, 40, 100),
            p2e.Span("-", 7.5, p2e.Style(), 0, 41.69, 45, 100),
            p2e.Span("two", 5.2, p2e.Style(), 0, 46.69, 60, 100, spacewidth=1.45),
        ]
        self.assertEqual(p2e.Line(spans).text, "twenty-two")

    def test_a_list_marker_stays_with_its_item(self):
        marker = _line(x0=90, x1=100, y0=100, text="1.", size=12)
        item = _line(x0=108, x1=400, y0=100, text="Get more customers.", size=12)
        body = _line(x0=72, x1=400, y0=130, text="After the list.", size=12)
        ordered = p2e.order_items([item, body, marker], page_width=612)
        self.assertEqual([line.text for line in ordered], ["1. Get more customers.", "After the list."])

    def test_a_repeated_fragment_stays_separate(self):
        left = _line(x0=175, x1=304, y0=100, text="school dragged on — and", size=10)
        right = _line(x0=310, x1=354, y0=100, text="on — and", size=10)
        ordered = p2e.order_items([left, right], page_width=432)
        self.assertEqual(len(ordered), 2)
        self.assertEqual(ordered[0].text, "school dragged on — and")

    def test_an_artboard_bookmark_yields_to_the_printed_title(self):
        style = p2e.Style()
        pages = [p2e.Page(1, 612, 792, [], []), p2e.Page(2, 612, 792, [], [])]
        page_blocks = {
            1: [p2e.Block("p", [], 0, 1, 0, 10, 0, poster=True)],
            2: [p2e.Block("h1", [(style, "Welcome!")], 18, 2, 80, 100, 200)],
        }
        chapters = p2e.split_chapters(
            pages,
            page_blocks,
            18,
            {1: "The-Lost-Chapter-cover", 2: "The Lost Chapter - Intro vF (1)"},
        )
        self.assertEqual([chapter.title for chapter in chapters], ["Cover", "Welcome!"])

    def test_a_long_outline_section_splits_at_its_children(self):
        class Doc:
            page_count = 200

            def get_toc(self):
                return [
                    [1, "Intro", 1],
                    [1, "The pillars of the framework", 5],
                    [2, "Security", 8],
                    [1, "Appendix: Questions and best practices", 45],
                    [2, "Operational excellence", 45],
                    [2, "Security", 90],
                    [2, "Reliability", 130],
                    [1, "Notices", 180],
                ]

        starts = p2e.outline_starts(Doc())
        self.assertNotIn(8, starts)
        self.assertEqual(starts[5], "The pillars of the framework")
        self.assertEqual(starts[45], "Appendix: Questions and best practices")
        self.assertEqual(starts[90], "Appendix: Security")
        self.assertEqual(starts[130], "Appendix: Reliability")
        self.assertEqual(starts[180], "Notices")

    def test_a_repeated_bookmark_uses_the_section_on_that_page(self):
        class Doc:
            page_count = 20

            def get_toc(self):
                return [
                    [1, "AWS Well-Architected Framework", 1],
                    [1, "AWS Well-Architected Framework", 5],
                    [2, "Introduction", 5],
                    [2, "Definitions", 6],
                    [1, "The pillars of the framework", 9],
                ]

        starts = p2e.outline_starts(Doc())
        self.assertEqual(starts[1], "AWS Well-Architected Framework")
        self.assertEqual(starts[5], "Introduction")
        self.assertNotIn(6, starts)

    def test_a_modest_paragraph_gap_starts_a_new_paragraph(self):
        # Wrapped lines are 18pt apart. The next paragraph is only a few points farther.
        texts = [
            "alpha beta gamma",
            "delta epsilon zeta",
            "eta theta iota.",
            "kappa lambda mu",
            "nu xi omicron",
            "pi rho sigma.",
        ]
        lines = []
        y = 100.0
        for index, text in enumerate(texts):
            lines.append(_line(x0=72, x1=480, y0=y, text=text, size=14.4))
            y += 23.8 if index == 2 else 18.0
        groups = p2e.group_lines(lines, margin=72, body=14.4)
        self.assertEqual(len(groups), 2)
        self.assertTrue(groups[0][-1].text.endswith("iota."))
        self.assertTrue(groups[1][0].text.startswith("kappa"))

    def test_normal_leading_stays_one_paragraph(self):
        lines = [_line(x0=72, x1=400, y0=80 + index * 15.5, text=f"wrap line {index} continues", size=10) for index in range(6)]
        groups = p2e.group_lines(lines, margin=72, body=10)
        self.assertEqual(len(groups), 1)

    def test_bullet_items_stay_separate_paragraphs(self):
        lines = [
            _line(x0=120, x1=400, y0=100, text="• First item wraps", size=9.2),
            _line(x0=129, x1=400, y0=112, text="onto the next line.", size=9.2),
            _line(x0=120, x1=400, y0=124, text="• Second item.", size=9.2),
            _line(x0=120, x1=400, y0=140, text="1. A numbered step.", size=9.2),
        ]
        groups = p2e.group_lines(lines, margin=129, body=9.2)
        self.assertEqual(
            [" ".join(line.text for line in group) for group in groups],
            ["• First item wraps onto the next line.", "• Second item.", "1. A numbered step."],
        )

    def test_a_comma_does_not_join_the_next_bullet(self):
        style = p2e.Style()
        blocks = [
            p2e.Block("p", [(style, "• AWS Well-Architected Labs,")], 9.2, 1, 100, 112, 120),
            p2e.Block("p", [(style, "• The Amazon Builders' Library")], 9.2, 1, 124, 136, 120),
        ]
        self.assertEqual(len(p2e.join_broken_sentences(blocks)), 2)

    def test_a_label_does_not_absorb_the_following_sentence(self):
        lines = [
            _line(x0=120, x1=280, y0=100, text="Implementation guidance", size=10),
            _line(x0=120, x1=420, y0=112, text="Each component needs access.", size=9.2),
        ]
        groups = p2e.group_lines(lines, margin=120, body=9.2)
        self.assertEqual([group[0].text for group in groups], ["Implementation guidance", "Each component needs access."])

    def test_a_section_label_is_not_joined_to_the_line_above(self):
        style = p2e.Style()
        blocks = [
            p2e.Block(
                "p",
                [(style, "Level of risk exposed if this best practice is not established: Low")],
                9.2,
                1,
                100,
                112,
                120,
            ),
            p2e.Block("p", [(style, "Implementation guidance")], 10, 1, 112, 124, 120),
        ]
        self.assertEqual(len(p2e.join_broken_sentences(blocks)), 2)

    def test_a_list_does_not_continue_across_the_page_break(self):
        style = p2e.Style()

        def block(text, page):
            return p2e.Block("p", [(style, text)], 9.2, page, 80, 92, 120)

        pages = [p2e.Page(1, 612, 792, [], []), p2e.Page(2, 612, 792, [], [])]
        page_blocks = {
            1: [block("• OPS01-BP02 Evaluate internal customer needs (p. 51)", 1)],
            2: [block("• OPS01-BP03 Evaluate governance requirements (p. 52)", 2)],
        }
        p2e.merge_across_pages(pages, page_blocks, set())
        self.assertTrue(page_blocks[1][0].text.startswith("• OPS01-BP02"))
        self.assertTrue(page_blocks[2][0].text.startswith("• OPS01-BP03"))

    def test_a_sentence_continues_onto_the_next_page(self):
        style = p2e.Style()

        def block(text, page):
            return p2e.Block("p", [(style, text)], 9.2, page, 80, 92, 120)

        pages = [p2e.Page(1, 612, 792, [], []), p2e.Page(2, 612, 792, [], [])]
        page_blocks = {
            1: [block("If your application", 1)],
            2: [block("relies on external dependencies like DNS.", 2)],
        }
        p2e.merge_across_pages(pages, page_blocks, set())
        self.assertIn("application relies on external", page_blocks[1][0].text)
        self.assertEqual(page_blocks[2], [])

    def test_a_page_join_skips_an_empty_page_and_a_caption_only_page(self):
        style = p2e.Style()

        def block(kind, text, page):
            return p2e.Block(kind, [(style, text)], 9.2, page, 80, 92, 120)

        pages = [p2e.Page(n, 612, 792, [], []) for n in (1, 2, 3)]
        page_blocks = {
            1: [block("p", "The sentence stops here.", 1)],
            3: [block("p", "starts later.", 3)],
        }
        p2e.merge_across_pages(pages, page_blocks, set())
        self.assertEqual(page_blocks[1][0].text, "The sentence stops here.")
        self.assertEqual(page_blocks[3][0].text, "starts later.")

        pages = [p2e.Page(1, 612, 792, [], []), p2e.Page(2, 612, 792, [], [])]
        page_blocks = {
            1: [block("caption", "Photo credit", 1), block("image", "", 1)],
            2: [block("p", "starts a new sentence.", 2)],
        }
        p2e.merge_across_pages(pages, page_blocks, set())
        self.assertEqual(page_blocks[1][0].text, "Photo credit")
        self.assertEqual(page_blocks[2][0].text, "starts a new sentence.")

    def test_capitalized_continuations_join_only_after_a_comma(self):
        style = p2e.Style()

        def pair(left, right, **kwargs):
            return [
                p2e.Block("p", [(style, left)], 10, 1, 10, 22, 40),
                p2e.Block("p", [(style, right)], 10, 1, 24, 36, 40, **kwargs),
            ]

        joined = p2e.join_broken_sentences(pair("After the comma,", "World continues."))
        self.assertEqual(joined[0].text, "After the comma, World continues.")
        indented = p2e.join_broken_sentences(pair("See the", "Report today.", indented=True))
        self.assertEqual(len(indented), 2)
        fresh = p2e.join_broken_sentences(pair("The total was", "About right now."))
        self.assertEqual(len(fresh), 2)

    def test_a_note_or_image_does_not_rejoin_the_sentence(self):
        style = p2e.Style()

        def block(kind, text):
            return p2e.Block(kind, [(style, text)] if text else [], 10, 1, 0, 12, 40)

        noted = p2e.rejoin_pull_quotes(
            [
                block("p", "feedback that"),
                block("note", "A sidebar."),
                block("p", "tempers our behavior."),
            ]
        )
        self.assertEqual(noted[0].text, "feedback that")
        self.assertEqual(noted[2].text, "tempers our behavior.")
        pictured = p2e.rejoin_pull_quotes(
            [
                block("p", "feedback that"),
                block("image", ""),
                block("p", "Tempers our behavior."),
            ]
        )
        self.assertEqual(pictured[0].text, "feedback that")
        self.assertEqual(pictured[2].text, "Tempers our behavior.")

    def test_page_joins_skip_posters_headings_and_finished_sentences(self):
        style = p2e.Style()

        def block(kind, text, page, **kwargs):
            pieces = [(style, text)] if text else []
            return p2e.Block(kind, pieces, 10, page, 0, 12, 40, **kwargs)

        def pages(*numbers):
            return [p2e.Page(number, 612, 792, [], []) for number in numbers]

        def unchanged(page_blocks, page_count):
            before = {number: [(item.kind, item.text) for item in blocks] for number, blocks in page_blocks.items()}
            p2e.merge_across_pages(pages(*range(1, page_count + 1)), page_blocks, set())
            after = {number: [(item.kind, item.text) for item in blocks] for number, blocks in page_blocks.items()}
            self.assertEqual(after, before)

        unchanged(
            {
                1: [block("p", "people who were setting", 1, poster=True)],
                2: [block("p", "up a shop were offered a service.", 2)],
            },
            2,
        )
        unchanged(
            {
                1: [block("h2", "A heading", 1)],
                2: [block("p", "starts here.", 2)],
            },
            2,
        )
        unchanged(
            {
                1: [block("p", "CHAPTER", 1, poster=True), block("p", "The sentence ends.", 1)],
                2: [block("p", "up a shop were offered a service.", 2)],
            },
            2,
        )
        unchanged(
            {
                1: [block("p", "people who were setting up", 1), block("p", "The sentence ends.", 1)],
                2: [block("p", "up a shop were offered a service.", 2)],
            },
            2,
        )
        unchanged(
            {
                1: [block("p", "people who were setting", 1)],
                2: [block("p", "up a shop were offered a service.", 2, indented=True)],
            },
            2,
        )
        unchanged(
            {
                1: [block("p", "people who were setting", 1)],
                2: [block("pull", "A QUOTE.", 2), block("p", "Up a shop were offered a service.", 2)],
            },
            2,
        )
        unchanged(
            {
                1: [block("p", "CHAPTER OPENING", 1, poster=True), block("pull", "A QUOTE.", 1)],
                2: [block("p", "up a shop were offered a service.", 2)],
            },
            2,
        )
        unchanged(
            {
                1: [block("p", "The sentence ends.", 1), block("pull", "QUOTE.", 1)],
                2: [block("h1", "Title", 2), block("pull", "ANOTHER.", 2)],
                3: [block("p", "up a shop were offered a service.", 3)],
            },
            3,
        )
        unchanged(
            {
                1: [block("note", "Credit", 1), block("image", "", 1)],
                2: [block("pull", "ONLY A QUOTE.", 2)],
                3: [block("p", "up a shop were offered a service.", 3)],
            },
            3,
        )

        joined = {
            1: [block("p", "people who were setting", 1), block("pull", "A QUOTE FROM THE PAGE.", 1)],
            2: [block("p", "up a shop were offered a service.", 2)],
        }
        p2e.merge_across_pages(pages(1, 2), joined, set())
        self.assertIn("setting up a shop", joined[1][0].text)
        self.assertEqual(joined[1][1].kind, "pull")
        self.assertEqual(joined[2], [])

    def test_a_slightly_larger_label_starts_its_own_block(self):
        lines = [
            _line(x0=120, x1=400, y0=100, text="Level of risk exposed if this best practice is not established: Low", size=9.2),
            _line(x0=120, x1=280, y0=112, text="Implementation guidance", size=10),
            _line(x0=120, x1=400, y0=124, text="• Allocate time to make improvements.", size=9.2),
        ]
        groups = p2e.group_lines(lines, margin=120, body=9.2)
        self.assertEqual(groups[0][0].text, "Level of risk exposed if this best practice is not established: Low")
        self.assertEqual(groups[1][0].text, "Implementation guidance")
        self.assertEqual(groups[2][0].text, "• Allocate time to make improvements.")

    def test_a_section_bookmark_beats_the_repeated_book_title(self):
        style = p2e.Style()
        pages = [p2e.Page(5, 612, 792, [], [])]
        page_blocks = {
            5: [
                p2e.Block("h1", [(style, "AWS Well-Architected Framework")], 28, 5, 80, 120, 72),
                p2e.Block("h1", [(style, "Introduction")], 21, 5, 200, 230, 72),
                p2e.Block(
                    "p",
                    [(style, "The AWS Well-Architected Framework helps you understand the pros and cons of the choices you make.")],
                    9.2,
                    5,
                    250,
                    280,
                    120,
                ),
            ]
        }
        chapters = p2e.split_chapters(pages, page_blocks, 18, {5: "Introduction"})
        self.assertEqual([chapter.title for chapter in chapters], ["Introduction"])

    def test_an_article_prefix_yields_to_the_printed_title(self):
        style = p2e.Style()
        pages = [p2e.Page(3, 612, 792, [], [])]
        page_blocks = {
            3: [
                p2e.Block("h2", [(style, "Article: Uninformed Consent")], 12, 3, 50, 70, 72),
                p2e.Block("h1", [(style, "Uninformed Consent")], 22, 3, 80, 110, 72),
                p2e.Block(
                    "p",
                    [(style, "A long enough opening paragraph about tracking and consent in the digital economy today.")],
                    9,
                    3,
                    140,
                    180,
                    72,
                ),
            ]
        }
        chapters = p2e.split_chapters(pages, page_blocks, 18, {3: "Article: Uninformed Consent"})
        self.assertEqual([chapter.title for chapter in chapters], ["Uninformed Consent"])

    def test_a_printed_heading_keeps_its_outline_prefix(self):
        style = p2e.Style()
        pages = [p2e.Page(10, 612, 792, [], [])]
        page_blocks = {10: [p2e.Block("h1", [(style, "Performance efficiency")], 21, 10, 80, 110, 72)]}
        chapters = p2e.split_chapters(pages, page_blocks, 18, {10: "Appendix: Performance efficiency"})
        self.assertEqual([chapter.title for chapter in chapters], ["Appendix: Performance efficiency"])

    def test_a_requested_string_is_removed(self):
        pages = [
            p2e.Page(
                1,
                612,
                792,
                [
                    _line(x0=220, x1=380, y0=400, text="OceanofPDF.com", size=14),
                    _line(x0=72, x1=460, y0=120, text="A real paragraph stays.", size=14),
                    _line(x0=72, x1=480, y0=150, text="See OceanofPDF.com for the scan.", size=14),
                ],
                [],
            )
        ]
        p2e.strip_phrases(pages, ["oceanofpdf.com"])
        self.assertEqual(
            [line.text for line in pages[0].lines],
            ["A real paragraph stays.", "See for the scan."],
        )

    def test_a_running_header_is_removed_when_the_section_name_changes(self):
        pages = []
        for number in range(1, 9):
            pages.append(
                p2e.Page(
                    number,
                    612,
                    792,
                    [
                        _line(x0=200, x1=420, y0=36, text="AWS Well-Architected Framework", size=9),
                        _line(x0=220, x1=400, y0=50, text=f"Section {number}", size=9),
                        _line(x0=72, x1=500, y0=90, text="Body text that stays on the page.", size=9.2),
                        _line(x0=300, x1=320, y0=740, text=str(number), size=9),
                    ],
                    [],
                )
            )
        p2e.strip_furniture(pages, body=9.2)
        kept = [line.text for page in pages for line in page.lines]
        self.assertEqual(kept, ["Body text that stays on the page."] * 8)

    def test_a_body_line_in_the_top_margin_stays(self):
        pages = []
        words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
        for number, word in enumerate(words, start=1):
            pages.append(
                p2e.Page(
                    number,
                    612,
                    648,
                    [
                        _line(
                            x0=72,
                            x1=420,
                            y0=50,
                            text=f"The {word} sentence stays in the chapter.",
                            size=10,
                        ),
                        _line(x0=200, x1=420, y0=600, text=f"summer 2026 | sapir {number}", size=7.5),
                    ],
                    [],
                )
            )
        p2e.strip_furniture(pages, body=10)
        kept = [line.text for page in pages for line in page.lines]
        self.assertEqual(len(kept), 8)
        self.assertTrue(all(text.startswith("The ") for text in kept))

    def test_a_title_only_first_page_becomes_a_cover_image(self):
        title = _line(x0=140, x1=400, y0=180, text="AWS Well-", size=32)
        cover = p2e.Page(1, 612, 792, [title], [])
        p2e.mark_posters([cover], body=9.2)
        self.assertTrue(cover.poster)
        article = p2e.Page(
            1,
            612,
            792,
            [_line(x0=72, x1=420, y0=100, text="This paragraph is long enough to count as body text.", size=11)],
            [],
        )
        p2e.mark_posters([article], body=11)
        self.assertFalse(article.poster)

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

    def test_a_two_column_issue_keeps_order_and_drops_furniture(self):
        folder = _out()
        pdf_path = folder / "layout.pdf"
        epub_path = folder / "layout.epub"
        _write_layout_issue(pdf_path)
        result = p2e.convert(pdf_path, output=epub_path, strip=["oceanofpdf.com"])
        self.assertEqual(result.chapters, ["Cover", "Two Columns"])
        with zipfile.ZipFile(epub_path) as archive:
            cover = archive.read("EPUB/chap_001.xhtml").decode("utf-8")
            body = archive.read("EPUB/chap_002.xhtml").decode("utf-8")
        self.assertIn('class="poster"', cover)
        self.assertNotIn("Sample Journal", body)
        self.assertNotIn("oceanofpdf", body.lower())
        self.assertLess(body.find("Alpha begins"), body.find("Omega begins"))
        self.assertLess(body.rfind("Alpha closes"), body.rfind("Omega closes"))


class EdgeTests(unittest.TestCase):
    def test_the_command_writes_an_epub_and_reports_a_missing_file(self):
        folder = _out()
        pdf_path = folder / "cli.pdf"
        epub_path = folder / "cli.epub"
        _write_sample(pdf_path)
        out = StringIO()
        err = StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = p2e.main([str(pdf_path), "-o", str(epub_path), "--title", "CLI Book", "--author", "Tester", "-v"])
        self.assertEqual(code, 0)
        self.assertIn("CLI Book", out.getvalue())
        self.assertTrue(epub_path.is_file())
        err = StringIO()
        with redirect_stderr(err):
            missing = p2e.main([str(folder / "missing.pdf")])
        self.assertEqual(missing, 1)
        self.assertIn("missing.pdf", err.getvalue())

    def test_convert_refuses_a_missing_locked_or_nearly_blank_pdf(self):
        folder = _out()
        with self.assertRaises(FileNotFoundError):
            p2e.convert(folder / "missing.pdf")

        scanned = folder / "scanned.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Hi", fontsize=12)
        doc.save(scanned)
        doc.close()
        with self.assertRaises(ValueError) as caught:
            p2e.convert(scanned)
        self.assertIn("scanned", str(caught.exception).lower())

        locked = folder / "locked.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Secret text that is long enough to extract. " * 3, fontsize=12)
        doc.save(locked, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
        doc.close()
        with self.assertRaises(ValueError) as caught:
            p2e.convert(locked)
        self.assertIn("password", str(caught.exception).lower())

    def test_metadata_dump_and_default_output(self):
        folder = _out()
        pdf_path = folder / "meta.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 80), "BIG TITLE", fontsize=28)
        page.insert_text((72, 140), "A subtitle", fontsize=16)
        page.insert_text((72, 220), "Enough body text to pass the scanned-page check here.", fontsize=11)
        doc.set_metadata({"title": "notes.pdf", "author": "Ada Lovelace"})
        doc.save(pdf_path)
        doc.close()
        dump = folder / "meta-dump"
        out = StringIO()
        with redirect_stdout(out):
            result = p2e.convert(pdf_path, dump_dir=dump, verbose=True)
        self.assertEqual(result.output, pdf_path.with_suffix(".epub"))
        self.assertEqual(result.author, "Ada Lovelace")
        self.assertEqual(result.title, "Big Title: A subtitle")
        self.assertTrue(any(dump.glob("*.html")))
        self.assertIn("body", out.getvalue())
        self.assertEqual(p2e.slug("???"), "chapter")

    def test_html_covers_images_scenes_and_posters(self):
        style = p2e.Style()
        bold = p2e.Style(bold=True)
        small = p2e.Style(smallcaps=True, italic=True)
        self.assertIn("dropcap", p2e.block_html(_block("image", "", image_href="images/a.png", image_class="dropcap", alt="T")))
        self.assertIn("<figure>", p2e.block_html(_block("image", "", image_href="images/a.png", alt="Picture")))
        self.assertEqual(p2e.block_html(_block("scene", "")), '<hr class="scene"/>')
        self.assertEqual(p2e.block_html(_block("p", "")), "")
        self.assertIn('class="indent"', p2e.block_html(_block("p", "Indented.", indented=True)))
        self.assertIn("<blockquote>", p2e.block_html(_block("pull", "A floated sentence.")))
        self.assertIn('class="note"', p2e.block_html(_block("note", "A note.")))
        self.assertIn('class="caption"', p2e.block_html(_block("caption", "A caption.")))
        self.assertIn('class="byline"', p2e.block_html(_block("byline", "By Ada", style=small)))
        self.assertIn('class="kicker"', p2e.block_html(_block("kicker", "Article", style=bold)))
        self.assertIn('class="deck"', p2e.block_html(_block("deck", "A deck.")))
        self.assertIn("<strong>", p2e.render_pieces([(bold, "Bold ") , (bold, "")], "p"))
        self.assertIn('class="sc"', p2e.render_pieces([(small, "Name")], "p"))
        self.assertNotIn("<strong>", p2e.render_pieces([(bold, "Title")], "h1"))

        poster = _block("p", "", poster=True, page=1)
        chapter = p2e.Chapter(blocks=[poster, poster, _block("p", "After the art.")])
        html = p2e.chapter_inner_html(chapter, {1: "images/page-001.jpeg"})
        self.assertEqual(html.count("poster"), 1)
        self.assertIn("After the art.", html)

        folder = _out()
        pdf_path = folder / "poster.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.save(pdf_path)
        doc.close()
        doc = fitz.open(pdf_path)
        page = p2e.Page(1, 612, 792, [], [])
        page.poster = True
        blobs = p2e.render_posters(doc, [page])
        self.assertIn("images/page-001.jpeg", blobs)
        self.assertTrue(page.poster_jpeg)
        empty = p2e.Chapter(blocks=[_block("p", "")], title="Empty")
        filled = p2e.Chapter(blocks=[_block("p", "Hello")], title="Hello")
        written = p2e.write_epub(
            doc,
            [empty, filled],
            [page],
            {"images/a.gif": (b"GIF89a", "gif"), "images/b.png": (b"png", "png"), "images/c.tif": (b"tiff", "tif")},
            folder / "poster.epub",
            "Poster",
            "",
            "en",
        )
        doc.close()
        self.assertEqual(written, ["Hello"])

    def test_body_size_furniture_and_color_edges(self):
        ornament = _line(40, 80, 100, "*", size=18)
        ornament.spans[0].ornament = True
        tiny = _line(40, 80, 120, "x", size=4)
        self.assertEqual(p2e.detect_body_size([p2e.Page(1, 612, 792, [ornament, tiny], [])]), 11.0)
        body_line = _line(40, 200, 140, "Body text stays.", size=10)
        self.assertEqual(p2e.heading_cutoff([p2e.Page(1, 612, 792, [body_line], [])], 10), 24.0)
        mid = _line(40, 200, 100, "Section", size=14)
        self.assertEqual(p2e.heading_cutoff([p2e.Page(1, 612, 792, [body_line, mid], [])], 10), 14)
        self.assertEqual(p2e.dominant_color([p2e.Page(1, 612, 792, [body_line], [])], 20), 0)

        untouched = [p2e.Page(1, 612, 792, [_line(40, 200, 40, "Header")], [])]
        before = [line.text for line in untouched[0].lines]
        p2e.strip_furniture(untouched, 11)
        self.assertEqual([line.text for line in untouched[0].lines], before)

        pages = []
        for number in range(1, 6):
            credit = _line(10, 20, 300, "getty images", size=8)
            notice = _line(40, 300, 740, "All rights reserved", size=8)
            body = _line(72, 400, 120, f"Paragraph {number} stays.", size=11)
            pages.append(p2e.Page(number, 612, 792, [credit, notice, body], []))
        p2e.strip_furniture(pages, 11)
        kept = [line.text for page in pages for line in page.lines]
        self.assertEqual(kept, [f"Paragraph {number} stays." for number in range(1, 6)])

        phrase_page = p2e.Page(1, 612, 792, [_line(40, 200, 100, "***")], [])
        p2e.strip_phrases([phrase_page], ["", "   "])
        self.assertEqual(phrase_page.lines[0].text, "***")
        p2e.strip_phrases([phrase_page], ["***"])
        self.assertEqual(phrase_page.lines, [])

        art = p2e.RawImage(1, (0, 0, 400, 500), b"x", "jpeg")
        cover = p2e.Page(2, 612, 792, [_line(40, 80, 40, "x", size=11)], [art])
        blank = p2e.Page(3, 612, 792, [], [])
        p2e.mark_posters([cover, blank], 11)
        self.assertTrue(cover.poster)
        self.assertTrue(blank.poster)

    def test_blocks_classify_images_drop_caps_and_roles(self):
        ornament = _line(40, 80, 40, "*")
        ornament.spans[0].ornament = True
        self.assertEqual(p2e.classify_group([ornament], "", 11, 22), "scene")
        title = _line(40, 300, 40, "A Real Chapter", size=24)
        self.assertEqual(p2e.classify_group([title], title.text, 11, 20), "h1")
        quote = _line(
            40, 400, 80,
            "This quotation is long enough to end as a complete sentence, so it stays a pull quote.",
            size=16,
        )
        self.assertEqual(p2e.classify_group([quote], quote.text, 11, 30), "pull")
        head = _line(40, 240, 40, "Section title", size=14)
        self.assertEqual(p2e.classify_group([head], head.text, 11, 30), "h2")
        note = _line(40, 240, 40, "A short caption", size=8)
        self.assertEqual(p2e.classify_group([note], note.text, 11, 30), "small")

        italic = _line(72, 400, 100, "An italic sentence.", size=11)
        italic.spans[0].style = p2e.Style(italic=True, smallcaps=True)
        page = p2e.Page(1, 612, 792, [italic], [])
        built = p2e.build_blocks(page, 11, 30, 0, {})
        self.assertGreater(built[0].italic_ratio, 0)
        self.assertGreater(built[0].smallcaps_ratio, 0)

        cover = p2e.Page(1, 612, 792, [], [])
        cover.poster = True
        poster_blocks = p2e.build_blocks(cover, 11, 30, 0, {})
        self.assertTrue(poster_blocks[0].poster)

        beside = _line(120, 400, 100, "Text beside a picture.", size=12)
        image = p2e.RawImage(1, (40, 100, 90, 170), b"png", "png")
        missing = p2e.RawImage(9, (40, 200, 120, 280), b"png", "png")
        photo = p2e.Page(1, 500, 700, [beside], [image, missing])
        placed = p2e.place_images(
            [_block("p", beside.text, y0=100, y1=120, x=120)],
            photo,
            {1: "images/cap.png"},
        )
        self.assertEqual(placed[0].image_class, "dropcap")
        wide = p2e.RawImage(2, (200, 300, 420, 460), b"png", "png")
        below = p2e.Page(1, 500, 700, [_line(40, 180, 100, "Above the picture.", size=12)], [wide])
        placed = p2e.place_images([_block("p", "Above the picture.", y0=100, y1=120)], below, {2: "images/wide.png"})
        self.assertEqual(placed[-1].kind, "image")
        self.assertEqual(placed[-1].image_class, "")
        poster_page = p2e.Page(1, 500, 700, [], [image])
        poster_page.poster = True
        same = [_block("p", "Stay")]
        self.assertIs(p2e.place_images(same, poster_page, {1: "images/cap.png"}), same)

        refined = p2e.refine_blocks(
            [
                _block("scene", "*"),
                _block("scene", "*"),
                _block("h1", "ARTICLE", size=22, y0=40, y1=60),
                _block("h1", "The Real Title", size=22, y0=64, y1=90),
                _block("h2", "A deck under the title", size=14, y0=100, y1=120),
                _block("h2", "By Ada Lovelace", size=12, y0=130, y1=146),
                _block("pull", "A short pull.", size=14, y0=160, y1=180, nlines=1),
                _block("image", "", y0=200, y1=260, image_href="images/a.png"),
                _block("small", "Under the picture", size=8, y0=270, y1=284),
                _block("small", "A footnote", size=8, y0=300, y1=314),
                _block("scene", "*"),
            ],
            0,
        )
        kinds = [block.kind for block in refined]
        self.assertNotIn("scene", kinds)
        self.assertIn("kicker", kinds)
        self.assertIn("deck", kinds)
        self.assertIn("byline", kinds)
        self.assertEqual(kinds.count("note"), 2)

        dropped = p2e.attach_drop_caps(
            [
                _block("p", "T", size=30, y0=40, y1=70),
                _block("p", "he story continues.", size=12, y0=40, y1=54),
                _block("p", "A", size=30, y0=80, y1=110),
                _block("p", "story starts.", size=12, y0=80, y1=94),
            ]
        )
        self.assertEqual(dropped[0].text, "The story continues.")
        self.assertEqual(dropped[1].text, "A story starts.")

    def test_sentence_joins_across_blocks_and_pages(self):
        self.assertEqual(p2e._word_token("", True), "")
        comma = p2e.join_broken_sentences(
            [
                _block("p", "After the comma,", y0=10, y1=22),
                _block("p", "the sentence goes on.", y0=24, y1=36),
            ]
        )
        self.assertEqual(len(comma), 1)
        names = p2e.join_broken_sentences(
            [
                _block("p", "Meet", y0=10, y1=22),
                _block("p", "Ada", y0=24, y1=36),
            ]
        )
        self.assertEqual(names[0].text, "Meet Ada")
        digits = p2e.join_broken_sentences(
            [
                _block("p", "The price is", y0=10, y1=22),
                _block("p", "12 dollars.", y0=24, y1=36),
            ]
        )
        self.assertEqual(len(digits), 1)
        apart = p2e.join_broken_sentences(
            [
                _block("p", "A finished thought", y0=10, y1=22),
                _block("p", "Next", y0=80, y1=92, indented=True),
            ]
        )
        self.assertEqual(len(apart), 2)
        bare = p2e.merge_text_blocks(_block("p", "Keep"), _block("p", ""))
        self.assertEqual(bare.text, "Keep")

        absorbed = p2e.absorb_split_pulls(
            [
                _block("h2", "A floated", size=14),
                _block("pull", "sentence continues here.", size=14),
            ]
        )
        self.assertEqual(absorbed[0].kind, "pull")
        self.assertIn("floated sentence", absorbed[0].text)

        rejoined = p2e.rejoin_pull_quotes(
            [
                _block("p", "feedback that"),
                _block("scene", "*"),
                _block("pull", "A floated sentence."),
                _block("p", "tempers our behavior."),
            ]
        )
        self.assertEqual(rejoined[0].text, "feedback that tempers our behavior.")

        pages = [p2e.Page(1, 500, 700, [], []), p2e.Page(2, 500, 700, [], [])]
        page_blocks = {
            1: [_block("h2", "A floated", size=14, page=1)],
            2: [_block("pull", "sentence continues.", size=14, page=2)],
        }
        p2e.merge_across_pages(pages, page_blocks, set())
        self.assertEqual(page_blocks[2][0].kind, "pull")
        self.assertIn("floated", page_blocks[2][0].text)

        page_blocks = {
            1: [_block("p", "The sentence ends.", page=1), _block("note", "Credit", size=8, page=1)],
            2: [_block("p", "starts a new page.", page=2)],
        }
        p2e.merge_across_pages(pages, page_blocks, {2})
        self.assertEqual(page_blocks[2][0].text, "starts a new page.")

    def test_titles_bookmarks_and_chapter_edges(self):
        self.assertTrue(p2e.outline_is_filename(""))
        self.assertTrue(p2e.outline_is_filename("layout.pdf"))
        self.assertTrue(p2e.outline_is_filename("Body copy"))
        self.assertTrue(p2e.outline_is_filename("The-Lost-Chapter-cover"))
        self.assertTrue(p2e.outline_is_filename("Foo-Bar"))
        self.assertFalse(p2e.outline_is_filename("Real Title"))

        class TocDoc:
            def __init__(self, toc, pages):
                self._toc = toc
                self.page_count = pages

            def get_toc(self):
                return self._toc

        shallow = p2e.outline_starts(
            TocDoc([[0], [1, "Only", 0], [1, "Only", 3], [2, "Child A", 4], [2, "Child B", 8]], 12)
        )
        self.assertEqual(shallow, {3: "Only", 4: "Child A", 8: "Child B"})
        long = p2e.outline_starts(
            TocDoc(
                [
                    [1, "Intro", 1],
                    [1, "Intro", 5],
                    [2, "Real Intro", 5],
                    [1, "Appendix: Questions", 10],
                    [2, "Security", 80],
                    [2, "Security", 90],
                ],
                200,
            )
        )
        self.assertEqual(long[1], "Intro")
        self.assertEqual(long[5], "Real Intro")
        self.assertEqual(long[10], "Appendix: Questions")
        self.assertEqual(long[80], "Appendix: Security")
        self.assertIn("Security", long[90])

        style = p2e.Style()
        pages = [p2e.Page(1, 612, 792, [], []), p2e.Page(2, 612, 792, [], [])]
        logo = p2e.Block("h1", [(style, "Sapir")], 20, 2, 40, 60, 40)
        body = "A paragraph with enough letters to count as chapter body text for the split."
        page_blocks = {
            1: [p2e.Block("h1", [(style, "Opening")], 22, 1, 40, 70, 40), p2e.Block("p", [(style, body)], 11, 1, 80, 120, 40)],
            2: [logo],
        }
        chapters = p2e.split_chapters(pages, page_blocks, 18, {})
        self.assertEqual(len(chapters), 1)
        self.assertIn("Sapir", " ".join(block.text for block in chapters[0].blocks))

        scene_only = {1: [_block("scene", "*")]}
        self.assertEqual(p2e.split_chapters([p2e.Page(1, 612, 792, [], [])], scene_only, 18, {}), [])

        cover_blocks = [_block("p", "", poster=True, page=1), _block("h1", "Hi", size=20, page=1)]
        self.assertEqual(p2e.choose_title(cover_blocks, 1, True), "Cover")
        self.assertEqual(p2e.choose_title([_block("h2", "Only a heading")], 4, False), "Only a heading")
        self.assertEqual(p2e.choose_title([], 4, False), "Chapter 4")
        self.assertEqual(p2e.infer_book_title([], "  Notes  "), "Notes")
        self.assertEqual(p2e.infer_book_title([], None), "Untitled")
        short = p2e.Page(1, 612, 792, [_line(40, 80, 40, "Hi", size=20)], [])
        self.assertEqual(p2e.infer_book_title([short], "file.pdf"), "Untitled")
        title = _line(40, 300, 40, "BIG TITLE", size=28)
        subtitle = _line(40, 240, 80, "A subtitle", size=16)
        self.assertEqual(p2e.infer_book_title([p2e.Page(1, 612, 792, [title, subtitle, title], [])], ""), "Big Title: A subtitle")

    def test_glyph_helpers_and_paragraph_breaks(self):
        oblique = p2e.style_from_font_name("Face-Oblique")
        self.assertTrue(oblique.italic)
        self.assertTrue(p2e.style_from_font_name("Face-itali").italic)
        self.assertTrue(p2e.style_from_font_name("Face-Demi").bold)
        self.assertTrue(p2e.style_from_font_name("Face-Black").bold)
        self.assertTrue(p2e.style_from_font_name("Face-Heavy").bold)
        self.assertTrue(p2e.style_from_font_name("Face_SC700").smallcaps)
        self.assertEqual(p2e.clean_chars("o\ufb03ce\u00a0\u200b"), "office ")
        self.assertFalse(p2e.looks_tracked("AB CD"))
        self.assertEqual(p2e.collapse_tracked("A B C D E", [], 12), "ABCDE")
        self.assertEqual(p2e.smart_case("Already Mixed"), "Already Mixed")
        self.assertEqual(p2e.smart_case("fish & chips"), "Fish & Chips")
        self.assertEqual(p2e.smart_case("123"), "123")
        self.assertTrue(p2e.junk_metadata("draft.docx"))
        self.assertTrue(p2e.junk_metadata("Untitled"))
        self.assertTrue(p2e.junk_metadata("Microsoft Word - notes"))
        self.assertEqual(p2e.as_int_color(3), 3)
        self.assertEqual(p2e.as_int_color((1.0, 0.0, 0.0)), 255 << 16)
        self.assertEqual(p2e.as_int_color((10, 20, 30)), (10 << 16) + (20 << 8) + 30)
        self.assertEqual(p2e.as_int_color("red"), 0)
        self.assertEqual(p2e.escape_text("a<b>\ufb01"), "a&lt;b&gt;fi")
        self.assertEqual(p2e.merge_pieces([(p2e.Style(), ""), (p2e.Style(), "A"), (p2e.Style(), "B")]), [(p2e.Style(), "AB")])
        self.assertEqual(p2e.rstrip_pieces([]), [])
        self.assertEqual(p2e.lstrip_pieces([]), [])
        self.assertEqual(p2e.join_line_pieces([[(p2e.Style(), "   ")], [(p2e.Style(), "next")]])[0][1].strip(), "next")
        span = p2e.Span("a\u00a0b", 12, p2e.Style(), 0, 0, 10, 20, chars=[("\u00ad", 0, 4, 10), ("\n", 4, 8, 10), ("\ue000", 8, 12, 10), ("c", 12, 16)])
        self.assertEqual(p2e.chars_to_text(span), "- c")
        self.assertEqual(p2e.Line([span]).y0, 10)
        self.assertEqual(p2e._baseline_groups([], 12), [])
        self.assertEqual(p2e._first_letter("..."), "")
        self.assertFalse(p2e._mostly_rtl([("a", 0, 1, 0)]))
        self.assertEqual(p2e._order_rtl_line([("\u05b7", 0, 1, 0)])[0][0], "\u05b7")
        wide = _line(10, 40, 10, "Left", size=11)
        indented_prev = _line(90, 200, 10, "Already in", size=11)
        nxt = _line(90, 200, 28, "Not an indent", size=11)
        self.assertFalse(p2e.is_first_line_indent(nxt, indented_prev, 40, 11))
        self.assertFalse(p2e.is_first_line_indent(_line(42, 80, 28, "Too small", size=11), wide, 40, 11))

        lines = [
            _line(72, 400, 100, "Same leading one.", size=12),
            _line(72, 400, 118, "Same leading two.", size=20),
            _line(200, 500, 140, "Far across.", size=12),
            _line(72, 400, 120, "Back up.", size=12),
            _line(72, 400, 160, "Body one.", size=12),
            _line(72, 400, 178, "Body two.", size=12),
            _line(72, 400, 196, "Body three.", size=12),
            _line(72, 400, 214, "Body four.", size=12),
            _line(72, 400, 232, "By the editor", size=12),
            _line(72, 400, 250, "Article", size=12),
            _line(72, 400, 268, "continues after.", size=12),
            _line(72, 400, 286, "1. A step", size=12),
        ]
        groups = p2e.group_lines(lines, 72, 12)
        self.assertGreater(len(groups), 4)
        self.assertEqual(p2e.body_margin([], 12), 0.0)

    def test_extraction_accepts_synthetic_traces_and_images(self):
        catalog = p2e.load_font_catalog(_FontDoc())
        self.assertEqual(len(catalog), 2)
        chosen = p2e.resolve_style(
            "Times-Roman",
            [("A", 0.5), ("B", 0.5)],
            [
                p2e.FontRec("Times-Roman", p2e.Style(), {"A": 0.5, "B": 0.5}, None),
                p2e.FontRec("Times-Roman-Extra", p2e.Style(italic=True), {"A": 0.9, "B": 0.9}, None),
            ],
        )
        self.assertFalse(chosen.italic)
        self.assertFalse(p2e.resolve_style("Unknown", [], []).italic)
        self.assertFalse(p2e._vertical_direction({"dir": "nope"}))
        self.assertEqual(p2e._space_vertical([("A", 0, 0, 0, 0)]), [("A", 0, 0, 0, 0)])
        self.assertEqual(p2e._char_baseline((65, 0), 10, 12), 10 + 12 * 0.8)

        trace = _TracePage(
            [
                {"size": 0, "font": "Times", "chars": []},
                {"size": 12, "font": "Times", "color": (1.0, 0, 0), "chars": [(None,)]},
                {
                    "size": 12,
                    "font": "Wingdings",
                    "color": 0,
                    "chars": [(0xE100, 1, (10, 40), (10, 30, 20, 42))],
                },
                {
                    "size": 12,
                    "font": "Times-Roman",
                    "color": 0,
                    "dir": (0, -1),
                    "chars": [
                        (ord("A"), 1, "bad", (10, 90, 20, 102)),
                        (ord("B"), 1, (10, 80), (10, 70, 20, 82)),
                        (ord("C"), 1, (10, 40), (10, 30, 20, 42)),
                        (ord("\u00ad"), 1, (10, 20), (10, 10, 20, 22)),
                        (10, 1, (10, 16), (10, 8, 20, 18)),
                    ],
                },
            ]
        )
        lines = p2e.extract_lines(trace, catalog)
        self.assertTrue(any(span.ornament for line in lines for span in line.spans))
        self.assertTrue(any("A" in line.text for line in lines))

        self.assertEqual(p2e.extract_images(_BrokenImages(), object(), {}), [])
        images = p2e.extract_images(_ImagePage(), _ImageDoc(), {})
        self.assertEqual([image.ext for image in images], ["png"])

    def test_extraction_covers_font_advances_ornaments_and_odd_images(self):
        import pdf2epub_lib.extract as extract

        self.assertEqual(p2e.group_spans([]), [])
        bare = extract._ornament_span(12, p2e.Style(), 0, 3.0, None, None)
        self.assertEqual((bare.x0, bare.x1, bare.origin_y), (0.0, 0.0, 0.0))
        self.assertTrue(bare.ornament)

        ornament = _TracePage(
            [
                {
                    "size": 12,
                    "font": "Ornaments",
                    "color": 0,
                    "chars": [
                        (0xE100, 1, (10, 40), (10, 30, 20, 42)),
                        (0xE101, 1, (30, 40), (30, 20, 50, 48)),
                    ],
                }
            ]
        )
        lines = p2e.extract_lines(ornament, [])
        span = lines[0].spans[0]
        self.assertTrue(span.ornament)
        self.assertEqual((span.x0, span.x1), (10.0, 50.0))

        font_path = next(
            (
                path
                for path in (
                    Path("/System/Library/Fonts/Supplemental/Andale Mono.ttf"),
                    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
                    Path("/Library/Fonts/Arial.ttf"),
                )
                if path.is_file()
            ),
            None,
        )
        if font_path is not None:
            doc = fitz.open()
            page = doc.new_page()
            page.insert_font(fontname="F0", fontfile=str(font_path))
            page.insert_text((72, 72), "Hello", fontname="F0", fontsize=12)
            catalog = p2e.load_font_catalog(doc)
            embedded = [rec for rec in catalog if rec.advances]
            self.assertGreater(embedded[0].advances["A"], 0)
            doc.close()

        class _OddPage:
            def get_image_info(self, xrefs=True):
                return [
                    {"xref": 1, "bbox": (0, 0, 200, 17)},
                    {"xref": 2, "bbox": (10, 10, 90, 90)},
                    {"xref": 3, "bbox": (10, 100, 90, 180)},
                    {"xref": 4, "bbox": (10, 200, 90, 280)},
                ]

        class _OddDoc:
            def extract_image(self, xref):
                if xref == 2:
                    return {}
                if xref == 3:
                    return {"image": b"jpeg-bytes", "ext": "jpg"}
                if xref == 4:
                    return {"image": b"tiff-bytes", "ext": "tiff"}
                return {"image": b"other", "ext": "png"}

        class _Pix:
            n = 4

            def __init__(self, *args):
                pass

            def tobytes(self, kind):
                return b"png-bytes"

        original = extract.fitz.Pixmap
        extract.fitz.Pixmap = _Pix
        try:
            images = p2e.extract_images(_OddPage(), _OddDoc(), {})
        finally:
            extract.fitz.Pixmap = original
        self.assertEqual([(image.ext, image.data) for image in images], [("jpeg", b"jpeg-bytes"), ("png", b"png-bytes")])


class _FontPage:
    def get_fonts(self, full=True):
        return [
            (1, 0, 0, "ABCDEF+Times-Bold"),
            (1, 0, 0, "ABCDEF+Times-Bold"),
            (2, 0, 0, "NoPlus"),
        ]


class _FontDoc:
    page_count = 1

    def __getitem__(self, index):
        return _FontPage()

    def extract_font(self, xref):
        if xref == 1:
            raise RuntimeError("missing font")
        return ("NoPlus", "ttf", "Type1", b"")


class _TracePage:
    def __init__(self, traces):
        self.traces = traces

    def get_texttrace(self):
        return self.traces


class _BrokenImages:
    def get_image_info(self, xrefs=True):
        raise RuntimeError("no images")


class _ImagePage:
    def get_image_info(self, xrefs=True):
        return [
            {"xref": 0, "bbox": (0, 0, 10, 10)},
            {"xref": 1, "bbox": (0, 0, 10, 40)},
            {"xref": 2, "bbox": (0, 0, 200, 10)},
            {"xref": 3, "bbox": (10, 10, 80, 80)},
            {"xref": 3, "bbox": (10, 10, 80, 80)},
            {"xref": 4, "bbox": (10, 100, 80, 180)},
            {"xref": 5, "bbox": (10, 200, 90, 280)},
        ]


class _ImageDoc:
    def extract_image(self, xref):
        if xref == 3:
            return {"image": b"\x89PNG\r\n", "ext": "png"}
        if xref == 4:
            raise RuntimeError("missing")
        if xref == 5:
            return {"image": b"abc", "ext": "jp2"}
        return {}


def _out() -> Path:
    folder = Path(__file__).resolve().parents[1] / ".test-out"
    folder.mkdir(exist_ok=True)
    return folder


def _block(kind, text, size=12, page=1, y0=0, y1=None, x=40, **kwargs):
    if y1 is None:
        y1 = y0 + size
    style = kwargs.pop("style", p2e.Style())
    pieces = [(style, text)] if text else []
    return p2e.Block(kind, pieces, size, page, y0, y1, x, **kwargs)


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


def _write_layout_issue(path: Path) -> None:
    """A cover, then three article pages with two columns, a header, and a watermark."""
    doc = fitz.open()
    cover = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 400, 500), 0)
    pix.set_rect(pix.irect, (30, 60, 90))
    cover.insert_image(fitz.Rect(80, 120, 500, 640), pixmap=pix)
    cover.insert_text((180, 80), "Cover", fontsize=28)
    left = [
        "Alpha begins the left column.",
        "Alpha continues down the left.",
        "Alpha stays on the left side.",
        "Alpha closes the left column.",
    ]
    right = [
        "Omega begins the right column.",
        "Omega continues down the right.",
        "Omega stays on the right side.",
        "Omega closes the right column.",
    ]

    def article(page, number, heading):
        page.insert_text((220, 36), "Sample Journal", fontsize=9)
        page.insert_text((300, 770), str(number), fontsize=9)
        if heading:
            page.insert_text((72, 100), "Two Columns", fontsize=22)
        y = 130
        for line in left:
            page.insert_text((72, y), line, fontsize=11)
            y += 22
        y = 130
        for line in right:
            page.insert_text((360, y), line, fontsize=11)
            y += 22
        if number == 3:
            page.insert_text((200, 460), "oceanofpdf.com", fontsize=12)

    article(doc.new_page(), 2, True)
    article(doc.new_page(), 3, False)
    article(doc.new_page(), 4, False)
    doc.set_toc([[1, "Cover", 1], [1, "Two Columns", 2]])
    doc.save(path)
    doc.close()


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
