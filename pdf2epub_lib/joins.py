"""Join sentences that a column or a page break split apart."""

from __future__ import annotations

from pdf2epub_lib.layout import _is_section_label, _starts_list_item
from pdf2epub_lib.model import Block, Page
from pdf2epub_lib.text import _first_letter, ends_sentence, join_line_pieces

def _word_token(text: str, last: bool) -> str:
    parts = text.split()
    if not parts:
        return ""
    token = parts[-1] if last else parts[0]
    return token.strip("\"'”’“‘()[] ,;:")


def _continues_sentence(current: str, nxt: str, indented: bool, y_gap: float | None = None, size: float = 12.0) -> bool:
    """A wrapped line can continue a sentence. A new paragraph further down the page does not."""
    if _starts_list_item(nxt):
        return False
    head = _first_letter(nxt)
    # An indented quotation can sit a little below the line it continues.
    # A dateline or a cover line sits further down and starts its own paragraph.
    if head.islower():
        return y_gap is None or y_gap <= 28
    # A capitalized name split across columns is close. The sentence after an
    # attribution is not.
    if y_gap is not None and y_gap > max(14.0, size * 1.25):
        return False
    if indented:
        return False
    if current.rstrip().endswith(","):
        return True
    last = _word_token(current, True)
    first = _word_token(nxt, False)
    if last and last[:1].isupper() and last.isalpha() and first and first[:1].isupper() and first.isalpha():
        return True
    return head in "$£€¥0123456789"


def join_broken_sentences(blocks: list[Block]) -> list[Block]:
    """A column or size change can split one sentence into two paragraphs."""
    index = 0
    while index < len(blocks) - 1:
        current = blocks[index]
        nxt = blocks[index + 1]
        if (
            current.kind == "p"
            and nxt.kind == "p"
            and not current.poster
            and not nxt.poster
            and not ends_sentence(current.text)
            and not _is_section_label(current, nxt)
            and _continues_sentence(
                current.text,
                nxt.text,
                nxt.indented,
                nxt.y0 - current.y1,
                max(current.size, nxt.size, 1.0),
            )
        ):
            blocks[index] = merge_text_blocks(current, nxt)
            del blocks[index + 1]
            continue
        index += 1
    return blocks


def absorb_split_pulls(blocks: list[Block]) -> list[Block]:
    """A pull quote's first line can be tagged as a heading when it stands alone."""
    index = 0
    while index < len(blocks) - 1:
        heading = blocks[index]
        follow = blocks[index + 1]
        if (
            heading.kind == "h2"
            and follow.kind == "pull"
            and not ends_sentence(heading.text)
            and _first_letter(follow.text).islower()
        ):
            merged = merge_text_blocks(heading, follow)
            merged.kind = "pull"
            blocks[index] = merged
            del blocks[index + 1]
            continue
        index += 1
    return blocks


def rejoin_pull_quotes(blocks: list[Block]) -> list[Block]:
    """Move pull quotes out of the sentence they were floated into."""
    index = 0
    while index < len(blocks) - 2:
        if blocks[index].kind != "p" or ends_sentence(blocks[index].text):
            index += 1
            continue
        nxt = index + 1
        if blocks[nxt].kind not in {"pull", "image", "scene"}:
            index += 1
            continue
        end = nxt
        saw_pull = False
        while end < len(blocks) and blocks[end].kind in {"pull", "image", "scene"}:
            saw_pull = saw_pull or blocks[end].kind == "pull"
            end += 1
        if (
            saw_pull
            and end < len(blocks)
            and blocks[end].kind == "p"
            and _first_letter(blocks[end].text).islower()
        ):
            merged = merge_text_blocks(blocks[index], blocks[end])
            blocks[index : end + 1] = [merged, *blocks[nxt:end]]
            continue
        index += 1
    return blocks


def merge_text_blocks(left: Block, right: Block, sep: str = " ") -> Block:
    del sep  # join_line_pieces inserts the space, or removes a line-end hyphen.
    if right.pieces:
        left.pieces = join_line_pieces([left.pieces, right.pieces])
    left.y1 = max(left.y1, right.y1)
    left.nlines += right.nlines
    return left


def _join_heading_into_pull(left_blocks: list[Block], right_blocks: list[Block]) -> None:
    if (
        left_blocks[-1].kind == "h2"
        and right_blocks[0].kind == "pull"
        and not ends_sentence(left_blocks[-1].text)
        and _first_letter(right_blocks[0].text).islower()
    ):
        merged = merge_text_blocks(left_blocks.pop(), right_blocks[0])
        merged.kind = "pull"
        right_blocks[0] = merged


def _content_tail(blocks: list[Block]) -> int | None:
    """A photo credit appended after the columns is not the end of the sentence."""
    tail_at = len(blocks) - 1
    while tail_at >= 0 and blocks[tail_at].kind in {"note", "caption", "image", "scene"}:
        tail_at -= 1
    if tail_at < 0:
        return None
    return tail_at


def _nearest_unfinished_paragraph(blocks: list[Block]) -> int | None:
    for at in range(len(blocks) - 1, -1, -1):
        block = blocks[at]
        if block.kind in {"pull", "image", "scene", "note", "caption"}:
            continue
        if block.kind == "p" and not block.poster and not ends_sentence(block.text):
            return at
        return None
    return None


def _cross_page_source(
    left_blocks: list[Block],
    tail: Block,
    tail_at: int,
    pair_index: int,
    numbers: list[int],
    page_blocks: dict[int, list[Block]],
) -> tuple[list[Block], int, Block] | None:
    if tail.kind == "pull" and tail_at >= 1 and left_blocks[tail_at - 1].kind == "p":
        return left_blocks, tail_at - 1, left_blocks[tail_at - 1]
    if tail.kind == "pull" and pair_index > 0:
        # The sentence was joined onto the previous page, and this page is the quote.
        earlier = page_blocks.get(numbers[pair_index - 1]) or []
        source_at = _nearest_unfinished_paragraph(earlier)
        if source_at is None:
            return None
        return earlier, source_at, earlier[source_at]
    if tail.kind == "p":
        return left_blocks, tail_at, tail
    return None


def _source_before_author_box(left_blocks: list[Block], tail_at: int) -> tuple[int, Block] | None:
    """An author box after the columns leaves the unfinished sentence behind it.

    The search stays on this page. The caller still writes through the block
    list that first located the sentence, which may be an earlier page.
    """
    for at in range(tail_at - 1, -1, -1):
        block = left_blocks[at]
        if block.kind in {"note", "caption", "image", "scene", "pull", "h1", "h2", "kicker", "byline", "deck"}:
            continue
        if block.kind != "p" or block.poster:
            break
        if ends_sentence(block.text) or len(block.text.split()) <= 3:
            continue
        between = left_blocks[at + 1 :]
        if any(item.kind in {"h1", "h2", "kicker"} for item in between):
            return at, block
        break
    return None


def _destination_index(right_blocks: list[Block]) -> int | None:
    """A pull quote floated to the top of the next page sits between the two halves."""
    dest_at = 0
    while dest_at < len(right_blocks) and right_blocks[dest_at].kind in {"image", "scene", "pull", "note", "caption"}:
        dest_at += 1
    if dest_at >= len(right_blocks):
        return None
    return dest_at


def _joins_across_page(source: Block, dest: Block, dest_at: int) -> bool:
    if dest.kind != "p" or dest.poster or dest.indented:
        return False
    if dest_at > 0 and not _first_letter(dest.text).islower():
        return False
    # A new bullet, or a new sentence, is not the previous page's unfinished line.
    if _starts_list_item(dest.text) or _is_section_label(source, dest):
        return False
    return _continues_sentence(
        source.text,
        dest.text,
        False,
        None,
        max(source.size, dest.size, 1.0),
    )


def merge_across_pages(pages: list[Page], page_blocks: dict[int, list[Block]], h1_pages: set[int]) -> None:
    numbers = [page.number for page in pages]
    for pair_index, (previous, nxt) in enumerate(zip(numbers, numbers[1:])):
        if nxt in h1_pages:
            continue
        left_blocks = page_blocks.get(previous) or []
        right_blocks = page_blocks.get(nxt) or []
        if not left_blocks or not right_blocks:
            continue
        _join_heading_into_pull(left_blocks, right_blocks)
        if not left_blocks or not right_blocks:
            continue
        tail_at = _content_tail(left_blocks)
        if tail_at is None:
            continue
        tail = left_blocks[tail_at]
        if tail.poster or right_blocks[0].poster:
            continue
        located = _cross_page_source(left_blocks, tail, tail_at, pair_index, numbers, page_blocks)
        if located is None:
            continue
        source_blocks, source_at, source = located
        if source.poster:
            continue
        if ends_sentence(source.text):
            found = _source_before_author_box(left_blocks, tail_at)
            if found is None:
                continue
            source_at, source = found
        dest_at = _destination_index(right_blocks)
        if dest_at is None:
            continue
        if not _joins_across_page(source, right_blocks[dest_at], dest_at):
            continue
        source_blocks[source_at] = merge_text_blocks(source, right_blocks[dest_at])
        del right_blocks[dest_at]

