"""Rebuild the document layout for a RAG stack that reads a DLA hierarchy.

The RAG stack chunks by the outline tree it reads off the printed page, so this
module's job is to make that tree **impossible to misread**:

  1. A section name always stands alone on its line, in bold, at a size that
     shrinks with depth and stays clearly apart from the body size — those are
     the cues that make a DLA model label the line `title` rather than `text`
     or `list-item`.
  2. The document title comes first, at the largest size: the RAG stack uses it
     as the root of every chunk title.
  3. No heading line is invented that is not in the original — every extra line
     is a phantom branch in the outline tree.
  4. A heading is never left stranded at the foot of a page, cut off from the
     content it introduces.

Content flows continuously by default, like an ordinary document. The *one page
per section* mode (`page_per_section=True`) is kept for cases where chunk
boundaries should coincide with page boundaries.

This module only computes the layout (pagination, font sizes). Writing the
.docx or .pdf file itself lives in `render.py`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .chunker import (
    CHARS_PER_TOKEN, MAX_TOKENS, _split_long_text, _split_table, est_tokens,
)
from .models import Section, continues_sentence

# Heading sizes by level: "1." largest, "1.1" smaller, "1.1.1" smallest.
# Levels deeper than this list reuse the last entry.
#
# Even the deepest level must stay visibly larger than the body: a DLA model
# tells a heading from a bullet mainly by size and weight, and half a point of
# difference gets it labelled list-item.
HEADING_PT = [20.0, 17.0, 15.0, 13.5, 12.5, 11.5]
TITLE_PT = 24.0                # document title — level 0, largest on page one
BODY_PT = 10.5                 # body text, always smaller than every heading

# The RAG stack reads an outline at most 6 levels deep. Deeper sections are
# still drawn, but reuse the level-6 size — one size further down would fall
# below the body size and a DLA model would read it as an ordinary paragraph.
MAX_TREE_DEPTH = len(HEADING_PT)

# Estimates for A4, 2cm margins, 1.15 line spacing
LINES_PER_PAGE = 42
CHARS_PER_LINE = 88            # at the body font size

# Continuous-flow mode applies no per-page token cap: the RAG stack chunks by
# the outline tree, not by page, so capping tokens per page only leaves pages
# half empty.
_NO_TOKEN_CAP = 10 ** 9


# The suffix that reopens a section split across several parts. It is written in
# the language of the documents being processed (Vietnamese), because it is
# printed into the rebuilt document itself, not shown in the tool's own UI.
CONTINUED_SUFFIX = "(tiếp)"


def heading_pt(level: int) -> float:
    """Font size of a level-`level` heading (0 = document title, 1 = topmost)."""
    if level <= 0:
        return TITLE_PT
    return HEADING_PT[min(level, MAX_TREE_DEPTH) - 1]


@dataclass
class PageItem:
    """One block of content on a page: heading, paragraph, table or figure."""

    kind: str                  # heading | para | table | figure
    text: str
    level: int = 0             # used for headings only
    size: float = BODY_PT
    meta: dict = field(default_factory=dict)

    @property
    def lines(self) -> int:
        """Estimated number of lines this block takes on the page."""
        return _lines_of(self)

    @property
    def tokens(self) -> int:
        """Tokens this block contributes when the RAG stack reads the page back."""
        if self.kind == "figure":
            # an image yields no tokens; only its caption becomes text
            return est_tokens(self.meta.get("caption") or "")
        return est_tokens(self.text)


@dataclass
class LayoutPage:
    """One page of the rebuilt document — always belonging to a single section."""

    section: Section
    items: list[PageItem]
    part_index: int = 1
    part_total: int = 1

    @property
    def lines(self) -> int:
        return sum(i.lines for i in self.items)

    @property
    def tokens(self) -> int:
        return sum(i.tokens for i in self.items)


def _lines_of(item: PageItem) -> int:
    if item.kind == "figure":
        height = item.meta.get("height") or 0
        # an image occupies as many text lines as its height allows, plus a caption line
        return max(2, math.ceil(float(height) / 14) + 1) if height else 3
    if item.kind == "table":
        rows = [r for r in item.text.split("\n") if r.strip()]
        # at least one line per row; text-heavy rows wrap inside the cell
        return sum(max(1, math.ceil(len(r) / CHARS_PER_LINE)) for r in rows) + 1
    per_line = CHARS_PER_LINE * BODY_PT / item.size
    used = 0
    for line in item.text.split("\n"):
        used += max(1, math.ceil(len(line) / per_line))
    # spacing above and below the block
    return used + (1 if item.kind == "heading" else 0)


def _items_of(section: Section, drop_cover: bool = True) -> list[PageItem]:
    """Turn a section's blocks into layout items ready for pagination."""
    items: list[PageItem] = []
    for block in section.blocks:
        if drop_cover and block.kind == "figure" and block.page <= 1:
            continue                       # cover art: decoration, not content
        if block.is_table or block.kind == "table":
            items.append(PageItem("table", block.text, meta=dict(block.meta)))
        elif block.kind == "figure":
            items.append(PageItem("figure", block.text, meta=dict(block.meta)))
        elif block.text.strip():
            # A PDF stores rendered lines, so a sentence often spans two
            # adjacent blocks. Joining them keeps the paragraph whole and lets
            # sentence-boundary splitting land in the right place.
            if (items and items[-1].kind == "para"
                    and continues_sentence(items[-1].text, block.text)):
                items[-1] = PageItem("para", f"{items[-1].text} {block.text}",
                                     meta=items[-1].meta)
            else:
                items.append(PageItem("para", block.text, meta=dict(block.meta)))
    return items


def _explode(item: PageItem, cap_lines: int, cap_tokens: int) -> list[PageItem]:
    """Split a block that exceeds one page's capacity or the token ceiling."""
    if item.lines <= cap_lines and item.tokens <= cap_tokens:
        return [item]

    if item.kind == "figure":
        # a figure cannot be split -> scale it down to fit a page
        height = float(item.meta.get("height") or 0)
        if not height:
            return [item]
        scale = max(0.1, (cap_lines - 1) / item.lines)
        meta = dict(item.meta)
        meta["height"] = height * scale
        meta["width"] = float(item.meta.get("width") or 0) * scale
        return [PageItem("figure", item.text, meta=meta)]

    # Split in token space: that is the unit of the RAG ceiling, and the page
    # capacity is converted to tokens so both are measured on one scale.
    by_page = math.ceil(cap_lines * CHARS_PER_LINE / CHARS_PER_TOKEN)
    limit = max(40, min(cap_tokens, by_page))
    parts = [item]
    for _ in range(5):
        if item.kind == "table":
            # Tables split by row, repeating the header line in each part. A row
            # longer than the ceiling is spread into prose, and that piece is no
            # longer a table — label it "table" and the renderer finds no rows
            # and leaves the whole block blank.
            parts = [PageItem("table" if p.lstrip().startswith("|") else "para",
                              p, size=item.size, meta=item.meta)
                     for p in _split_table(item.text, limit, est_tokens)]
        else:
            parts = [PageItem("para", p, size=item.size, meta=item.meta)
                     for p in _split_long_text(item.text, limit, est_tokens)]
        if all(p.lines <= cap_lines and p.tokens <= cap_tokens for p in parts):
            break
        limit //= 2
    return parts


def _fits_in(units: list[PageItem], cap_lines: int, cap_tokens: int) -> int:
    """How many parts are needed if no part may exceed either ceiling."""
    parts, lines, tokens = 1, 0, 0
    for unit in units:
        if lines and (lines + unit.lines > cap_lines
                      or tokens + unit.tokens > cap_tokens):
            parts += 1
            lines, tokens = unit.lines, unit.tokens
        else:
            lines += unit.lines
            tokens += unit.tokens
    return parts


def _split_even(units: list[PageItem], cap_lines: int,
                cap_tokens: int) -> list[list[PageItem]]:
    """Split blocks into the fewest pages, filled to roughly equal depth.

    Filling greedily leaves the last page nearly empty. Instead, *both* ceilings
    are lowered by the same ratio until the smallest fill level is found that
    still fits in the same number of pages, and the blocks are laid out at that
    level — which leaves the pages about equally long.
    """
    if (sum(u.lines for u in units) <= cap_lines
            and sum(u.tokens for u in units) <= cap_tokens):
        return [units]

    need = _fits_in(units, cap_lines, cap_tokens)
    low, high = 10, 100                   # fill level, as a percentage of both ceilings
    while low < high:
        mid = (low + high) // 2
        if _fits_in(units, cap_lines * mid // 100,
                    cap_tokens * mid // 100) <= need:
            high = mid
        else:
            low = mid + 1
    fill_lines = max(1, cap_lines * low // 100)
    fill_tokens = max(1, cap_tokens * low // 100)

    parts: list[list[PageItem]] = [[]]
    lines = tokens = 0
    for unit in units:
        if parts[-1] and (lines + unit.lines > fill_lines
                          or tokens + unit.tokens > fill_tokens):
            parts.append([])
            lines = tokens = 0
        parts[-1].append(unit)
        lines += unit.lines
        tokens += unit.tokens
    return parts


def _without_ellipsis(name: str) -> str:
    """Drop the "…" that marks a shortened section name (and a trailing "+")."""
    return name[:-1].strip(" +") if name.endswith("…") else name


def _heading_and_rest(section: Section) -> tuple[str, str]:
    """Separate the section name from body text written on the same line.

    Administrative documents often run the content straight into the heading
    line ("14. KOC: Là những người tiêu dùng chủ chốt…"). Left alone, the whole
    passage is set in bold at heading size, so only the section name is kept and
    the rest moves down into the body.

    The name is taken at its *full* length rather than the shortened form used
    for the outline path: the shortened form is cut at a hard character mark, so
    it often breaks mid-phrase and the printed heading reads as missing words.
    """
    full = section.heading_text.strip()
    probe = _without_ellipsis(section.display_heading.strip())
    if probe and full.startswith(probe):
        return probe, full[len(probe):].lstrip(" :.-–\n")

    line, _sep, rest = full.partition("\n")
    line = line.strip()
    if not probe:
        return line, rest.strip()

    # The section name does not open the original line. Two cases: a flattened
    # table row ("STT: 3.1 | Nội dung: Đối tượng khách hàng | …") buries the name
    # mid-line, and a section with merged numbers ("1.1 + 1.2") has a name that
    # appears in no line at all. Both are handled the same way: the heading line
    # carries only the name and the original line moves down into the body —
    # printing the whole line as a heading makes a DLA model read one
    # interminable section name.
    number = section.number.split(" + ")[0].strip()
    body = (line[len(number):].lstrip(" :.-–")
            if number and line.startswith(number) else line)
    # For a section with merged numbers, its own name has just been printed on
    # the heading line. In sections where *the content is the section name* — a
    # glossary entry like "13 Doanh nghiệp bán hàng đa cấp: Là doanh nghiệp…" —
    # leaving it alone makes the line under the heading repeat it word for word.
    own = _without_ellipsis(section.own_title.strip())
    if own and body.startswith(own):
        body = body[len(own):].lstrip(" :.-–")
    return probe, "\n".join(p for p in (body, rest.strip()) if p)


def _is_descendant(child: Section, parent: Section) -> bool:
    return (len(child.path) > len(parent.path)
            and child.path[:len(parent.path)] == parent.path)


def _heading_item(section: Section) -> PageItem | None:
    """A section's heading block, or None when the section has no real heading."""
    if section.is_preamble or not section.heading_text:
        return None
    title, _rest = _heading_and_rest(section)
    return PageItem("heading", title, level=section.level,
                    size=heading_pt(section.level))


def _content_items(section: Section, drop_cover: bool) -> list[PageItem]:
    """A section's content, with any text run into the heading line split off."""
    _title, rest = _heading_and_rest(section) if section.heading_text else ("", "")
    units = _items_of(section, drop_cover=drop_cover)
    if not rest or section.is_preamble:
        return units

    # The text run into the heading line is usually the *first half* of a
    # sentence whose second half sits in the next block (a PDF stores rendered
    # lines). Split the name off and leave it there, and the paragraph breaks in
    # two right below every heading.
    if (units and units[0].kind == "para"
            and continues_sentence(rest, units[0].text)):
        units[0] = PageItem("para", f"{rest} {units[0].text}",
                            meta=units[0].meta)
        return units
    units.insert(0, PageItem("para", rest))
    return units


def build_pages(sections: list[Section],
                lines_per_page: int = LINES_PER_PAGE,
                max_tokens: int = MAX_TOKENS,
                drop_cover: bool = True,
                page_per_section: bool = False,
                doc_title: str = "") -> list[LayoutPage]:
    """Lay the whole document out into pages.

    Content flows continuously by default; `page_per_section=True` returns to
    the older mode where each section gets its own page and every page fits
    inside `max_tokens` tokens.
    """
    if not page_per_section:
        return _flow_pages(sections, lines_per_page, drop_cover, doc_title,
                           max_tokens)
    return _section_pages(sections, lines_per_page, max_tokens, drop_cover,
                          doc_title)


def _continuation(head: PageItem) -> PageItem:
    """The heading line that reopens a section split into several parts."""
    return PageItem("heading", f"{head.text} {CONTINUED_SUFFIX}", level=head.level,
                    size=head.size)


def _flow_pages(sections: list[Section], lines_per_page: int,
                drop_cover: bool, doc_title: str,
                max_tokens: int = MAX_TOKENS) -> list[LayoutPage]:
    """Build one continuous flow and let the renderer decide the page breaks.

    The line counts in this module are estimates and never match a real layout
    engine exactly — least of all for tables. Paginate on the estimate and force
    the renderer to follow, and any page that was undercounted overflows, spills
    onto the next page and leaves one nearly blank. Word and MuPDF paginate
    accurately, so let them; in exchange the whole document becomes a single
    "layout page".

    The token ceiling still has to be enforced, flow or no flow. The RAG stack
    chunks by the outline tree rather than by page, so a four-thousand-token
    section becomes exactly one four-thousand-token chunk and gets truncated at
    embedding time. A section over the ceiling is split into parts, each opening
    with a "(tiếp)" line — that is the node the RAG stack splits on — and the
    parts are made even rather than front-loaded.
    """
    if not sections:
        return []
    items: list[PageItem] = []
    if doc_title:
        items.append(PageItem("heading", doc_title, level=0, size=TITLE_PT))
    for section in sections:
        head = _heading_item(section)
        # The heading line is read as chunk tokens too, so subtract it up front.
        # The "(tiếp)" line is longer than the original heading, so budget for
        # whichever of the two costs more.
        budget = max_tokens if max_tokens > 0 else _NO_TOKEN_CAP
        if head is not None:
            budget = max(40, budget - _continuation(head).tokens)

        units: list[PageItem] = []
        for unit in _content_items(section, drop_cover):
            units.extend(_explode(unit, lines_per_page, budget))

        if head is None:
            items.extend(units)
            continue
        # Page capacity is no longer a constraint in continuous-flow mode; only
        # the token ceiling is.
        for index, part in enumerate(_split_even(units, _NO_TOKEN_CAP, budget)):
            items.append(head if index == 0 else _continuation(head))
            items.extend(part)
    return [LayoutPage(sections[0], items)] if items else []


def _section_pages(sections: list[Section],
                   lines_per_page: int,
                   max_tokens: int,
                   drop_cover: bool,
                   doc_title: str) -> list[LayoutPage]:
    """The older mode: one page per section, each page within the token ceiling."""
    pages: list[LayoutPage] = []
    # A parent section usually holds nothing but its heading line. Giving it a
    # whole blank page is wasteful, so it is held back and placed at the top of
    # its first child's page — which breaks no constraint, since the parent has
    # no content of its own.
    pending: list[tuple[Section, PageItem]] = []

    for section in sections:
        # The section name stands alone on its line; anything run into it is body.
        head = _heading_item(section)
        units = _content_items(section, drop_cover)

        if not units:
            if head is not None:
                pending.append((section, head))
            continue

        # Only pending headings that come *immediately before* this section and
        # are its ancestors may sit at the top of its page; the rest are genuinely
        # empty sections and get their own page to preserve document order.
        keep = len(pending)
        while keep > 0 and _is_descendant(section, pending[keep - 1][0]):
            keep -= 1
        for owner, item in pending[:keep]:
            pages.append(LayoutPage(owner, [item]))
        lead: list[PageItem] = [item for _owner, item in pending[keep:]]
        pending = []
        if head is not None:
            lead.append(head)

        # A heading carried to the top of a page takes space and is read as
        # tokens too, so subtract it before laying out the content. The next page
        # carries the "(tiếp)" line, which is longer than the original heading, so
        # budget for whichever costs more.
        cont_text = f"{head.text} {CONTINUED_SUFFIX}" if head is not None else ""
        cont_head = PageItem("heading", cont_text, level=section.level,
                             size=heading_pt(section.level))
        reserve_lines = max(sum(i.lines for i in lead), cont_head.lines)
        reserve_tokens = max(sum(i.tokens for i in lead), cont_head.tokens)
        cap = max(6, lines_per_page - reserve_lines)
        cap_tokens = max(40, max_tokens - reserve_tokens)
        exploded: list[PageItem] = []
        for unit in units:
            exploded.extend(_explode(unit, cap, cap_tokens))

        parts = _split_even(exploded, cap, cap_tokens)
        for index, part in enumerate(parts):
            if index == 0:
                items = lead + part
            elif cont_text:
                items = [PageItem("heading", cont_text, level=section.level,
                                  size=heading_pt(section.level))] + part
            else:
                items = list(part)
            pages.append(LayoutPage(section, items, index + 1, len(parts)))

    for owner, item in pending:
        pages.append(LayoutPage(owner, [item]))
    if doc_title and pages:
        pages[0].items.insert(0, PageItem("heading", doc_title, level=0,
                                          size=TITLE_PT))
    return pages
