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
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Use

```bash
python pdf2epub.py "Some File.pdf"
python pdf2epub.py "Some File.pdf" -o "Some File.epub" --title "Sapir: Influence" --author "Sapir"
```

With no `-o`, the EPUB is written next to the PDF.

`--dump-dir DIR` writes each chapter as HTML so you can check the conversion without opening a reader.

## Limits

A scanned PDF has no text to extract. The script stops and says so. It does not OCR.

Tables come out as ordinary paragraphs. Pages with two clear text columns are read left column first, then right. Very decorative pages can still need a quick look in a reader.
