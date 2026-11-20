"""Run one PDF path through to an EPUB."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import fitz

from pdf2epub_lib.blocks import build_blocks
from pdf2epub_lib.chapters import infer_book_title, outline_starts, page_has_h1, split_chapters
from pdf2epub_lib.extract import extract_images, extract_lines, load_font_catalog
from pdf2epub_lib.joins import join_broken_sentences, merge_across_pages
from pdf2epub_lib.model import Page, Result
from pdf2epub_lib.prepare import (
    detect_body_size,
    dominant_color,
    heading_cutoff,
    mark_posters,
    strip_furniture,
    strip_phrases,
)
from pdf2epub_lib.publish import chapter_inner_html, render_posters, write_epub, xhtml_document
from pdf2epub_lib.text import junk_metadata

def _open_document(pdf_path: Path):
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)
    doc = fitz.open(pdf_path)
    if doc.needs_pass:
        raise ValueError(f"{pdf_path.name} is password-protected.")
    if doc.page_count == 0:
        raise ValueError(f"{pdf_path.name} has no pages.")
    return doc


def _read_pages(doc) -> tuple[list[Page], dict[int, tuple[bytes, str]]]:
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
    return pages, image_cache


def _require_text(pdf_path: Path, pages: list[Page]) -> None:
    text_chars = sum(len(line.text) for page in pages for line in page.lines)
    if text_chars < 40:
        raise ValueError(
            f"{pdf_path.name} has almost no extractable text. It looks scanned. "
            "This script does not OCR pages."
        )


def _stored_images(image_cache: dict[int, tuple[bytes, str]]):
    image_names: dict[int, str] = {}
    image_blobs: dict[str, tuple[bytes, str]] = {}
    for xref, (data, ext) in image_cache.items():
        digest = hashlib.sha1(data).hexdigest()[:12]
        suffix = "jpg" if ext == "jpeg" else ext
        href = f"images/img-{digest}.{suffix}"
        image_names[xref] = href
        image_blobs[href] = (data, "jpeg" if ext == "jpeg" else ext)
    return image_names, image_blobs


def _build_chapters(doc, pages, body, cutoff, body_color, image_names, image_blobs):
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
    for number in page_blocks:
        page_blocks[number] = join_broken_sentences(page_blocks[number])
    merge_across_pages(pages, page_blocks, h1_pages)
    chapters = split_chapters(pages, page_blocks, cutoff, outline)
    image_blobs.update(render_posters(doc, pages))
    return chapters, image_blobs, outline


def _book_names(doc, pages, title, author) -> tuple[str, str]:
    meta = doc.metadata or {}
    book_title = title.strip() if title else infer_book_title(pages, meta.get("title"))
    book_author = author.strip() if author else ""
    if not book_author and not junk_metadata(meta.get("author")):
        book_author = meta["author"].strip()
    return book_title, book_author


def _dump_chapters(dump_dir: Path, chapters, pages) -> None:
    dump_dir.mkdir(parents=True, exist_ok=True)
    poster_hrefs = {page.number: f"images/page-{page.number:03d}.jpeg" for page in pages if page.poster_jpeg}
    for index, chapter in enumerate(chapters, start=1):
        body_html = chapter_inner_html(chapter, poster_hrefs)
        (dump_dir / f"{index:02d}-{slug(chapter.title)}.html").write_text(
            xhtml_document(chapter.title, body_html),
            encoding="utf-8",
        )


def convert(
    pdf_path: Path,
    output: Path | None = None,
    title: str | None = None,
    author: str | None = None,
    language: str = "en",
    dump_dir: Path | None = None,
    verbose: bool = False,
    strip: list[str] | None = None,
) -> Result:
    pdf_path = Path(pdf_path)
    doc = _open_document(pdf_path)
    pages, image_cache = _read_pages(doc)
    _require_text(pdf_path, pages)
    body = detect_body_size(pages)
    cutoff = heading_cutoff(pages, body)
    strip_phrases(pages, strip or [])
    strip_furniture(pages, body)
    body_color = dominant_color(pages, body)
    mark_posters(pages, body)
    image_names, image_blobs = _stored_images(image_cache)
    chapters, image_blobs, outline = _build_chapters(
        doc, pages, body, cutoff, body_color, image_names, image_blobs
    )
    book_title, book_author = _book_names(doc, pages, title, author)
    if verbose:
        print(f"pages {doc.page_count}  body {body:.1f}pt  heading cutoff {cutoff:.1f}pt  color #{body_color:06x}")
        print(f"outline entries used: {len(outline)}")
    destination = output or pdf_path.with_suffix(".epub")
    written_titles = write_epub(doc, chapters, pages, image_blobs, destination, book_title, book_author, language)
    if dump_dir:
        _dump_chapters(dump_dir, chapters, pages)
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
