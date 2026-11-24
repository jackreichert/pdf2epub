"""Write chapter HTML and the EPUB package."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pymupdf as fitz

from pdf2epub_lib.model import CSS, Block, Chapter, Page
from pdf2epub_lib.text import escape_text, render_pieces

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
