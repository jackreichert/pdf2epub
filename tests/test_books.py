"""End-to-end checks for PDFs that stay on this machine.

Each test skips when its PDF is absent. The files are not copied into the repo.
"""

import re
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pdf2epub as p2e

BOOKS = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs"
SAPIR = BOOKS / "Downloads/Sapir_Volume22.pdf"
HBR = BOOKS / "Documents/eBooks/HBR-Tracked-Jaron-Lanier-BG1805-PDF-ENG.PDF"
MDW = BOOKS / "Downloads/million-dollar-weekend-the-lost-chapter.pdf"
WAF = BOOKS / "Downloads/wellarchitected-framework.pdf"


def _chapter_text(epub_path: Path) -> str:
    with zipfile.ZipFile(epub_path) as archive:
        html = "\n".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.endswith(".xhtml") and "chap_" in name
        )
    return re.sub(r"<[^>]+>", " ", html)


class BookTests(unittest.TestCase):
    def _convert(self, pdf_path: Path):
        if not pdf_path.is_file():
            self.skipTest(f"missing {pdf_path.name}")
        folder = Path(__file__).resolve().parents[1] / ".test-out" / "books"
        folder.mkdir(parents=True, exist_ok=True)
        epub_path = folder / f"{pdf_path.stem}.epub"
        return p2e.convert(pdf_path, output=epub_path), epub_path

    def test_sapir_chapters_and_a_footer_line(self):
        result, epub_path = self._convert(SAPIR)
        self.assertEqual(
            result.chapters,
            [
                "Cover",
                "Contents",
                "The Anxiety of (Waning) Jewish Influence",
                "Not Be Giants",
                "How AI Is Being Weaponized Against Jews",
                "‘People Who Are Involved Have Influence’",
                "The Unspoken Language of Israeli Politics",
                "On Reluctant Leadership",
                "Bad Influence",
                "British Jews Made",
                "The Outsider Advantage",
                "A New Battle of the Books",
                "Where the Soviets Won",
                "Change a School?",
                "The Mobilization Ladder",
                "What Donors Should Know About Universities",
                "Jewish Masterpiece: Judaism as a Civilization",
                "Letters",
                "Contributors",
            ],
        )
        text = _chapter_text(epub_path)
        self.assertIn("Ideas for a Thriving Jewish Future", text)
        self.assertIn("They Might", text)

    def test_hbr_chapters_drop_the_reprint_line(self):
        result, epub_path = self._convert(HBR)
        self.assertEqual(
            result.chapters,
            [
                "Cover",
                "Contents",
                "Uninformed Consent",
                "How To Exercise The Power You Didn’t Ask For",
                "The Happy Tracked Employee",
                "Q&A: Helen Nissenbaum “Stop Thinking About Consent: It Isn’t Possible And It Isn’t Right”",
                "A Blueprint For A Better Digital Society",
                "Back Cover",
            ],
        )
        text = _chapter_text(epub_path)
        self.assertIn("TrackMeNot", text)
        self.assertNotIn("GETTY IMAGES", text.upper())
        self.assertNotIn("FOR ARTICLE REPRINTS", text.upper())

    def test_lost_chapter_keeps_its_three_parts(self):
        result, epub_path = self._convert(MDW)
        self.assertEqual(
            result.chapters,
            ["Cover", "Welcome!", "The Lost Chapter: Growth Rocket Fuel"],
        )
        self.assertIn("Phil Knight", _chapter_text(epub_path))

    def test_well_architected_splits_the_long_appendices(self):
        result, epub_path = self._convert(WAF)
        self.assertEqual(
            result.chapters,
            [
                "AWS Well-Architected Framework",
                "Table of Contents",
                "Introduction",
                "The pillars of the framework",
                "The review process",
                "Conclusion",
                "Contributors",
                "Further reading",
                "Document revisions",
                "Appendix: Questions and best practices",
                "Appendix: Security",
                "Appendix: Reliability",
                "Appendix: Performance efficiency",
                "Appendix: Cost optimization",
                "Appendix: Sustainability",
                "Notices",
                "AWS glossary",
            ],
        )
        self.assertIn("external dependencies", _chapter_text(epub_path).lower())
