# pdf2epub

Convert a PDF into an EPUB you can read in Apple Books, Kindle, or any other e-reader.

The script reads the PDF's text layer and rebuilds a reflowable book:

- chapters, from the PDF's bookmarks when it has them, otherwise from heading size
- paragraphs, including words that were hyphenated across line breaks
- italics, bold, and small caps
- pictures where they appear, including illustrated drop caps
- running headers and page numbers removed

A cover or a section page that is mostly artwork is kept as a picture, so the design survives. Article pages reflow.

## Setup

```bash
cd projects/pdf2epub
uv sync
```

## Use

```bash
uv run python pdf2epub.py "Some File.pdf"
uv run python pdf2epub.py "Some File.pdf" -o "Some File.epub" --title "Sapir: Influence" --author "Sapir"
uv run python pdf2epub.py "Some File.pdf" --strip "oceanofpdf.com"
uv run python -m unittest tests.test_pdf2epub -q
```

With no `-o`, the EPUB is written next to the PDF.

`--strip TEXT` removes that text on every page. Repeat the flag to remove more than one string. Matching ignores case. A line that is only the watermark is dropped. A longer line keeps the rest of the sentence.

Page numbers and running headers are removed without a flag. The converter looks at each page's lines, not a plain text dump. A line of digits in the top or bottom margin is a page number. A short line that repeats in that margin is a running header. A watermark in the middle of the page is left alone until you pass `--strip`.

`--dump-dir DIR` writes each chapter as HTML so you can check the conversion without opening a reader.

## Limits

A scanned PDF has no text to extract. The script stops and says so. It does not OCR.

Tables come out as ordinary paragraphs. Pages with two clear text columns are read left column first, then right. Very decorative pages can still need a quick look in a reader.

## License

Apache-2.0. See [LICENSE](LICENSE).
