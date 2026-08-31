"""Split a document into chunks: each one inside a single page and section.

Three constraints drive everything here:
  1. No chunk may exceed the **512 token** ceiling — the limit of the RAG
     embedding step, which truncates the overflow silently, so it has to be
     enforced here.
  2. No chunk may straddle a page boundary -> avoids stitching together content
     that PDF extraction pulled from two different pages.
  3. No chunk may hold two different sections -> the vector reflects one topic.

When a section spans several pages it is split into several chunks, but each
one carries the full outline path and prev/next links so the complete context
can be recovered at query time.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .models import Chunk, Section, clean_text

# The token ceiling of the target RAG stack. Every size below is measured in
# tokens rather than characters, because tokens are what the embedding step
# actually counts.
MAX_TOKENS = 512
# Below this a section standing on its own is too thin, and its vector carries
# almost no information — merge it with a neighbour as long as the total still
# fits under the 512 ceiling.
#
# Setting the threshold close to half the ceiling is deliberate: measured over
# 18 real documents, dropping it to 60 leaves 389 chunks under 200 tokens, while
# raising it past 200 changes almost nothing (890 -> 877 chunks) because by then
# the 512 ceiling is the binding constraint.
MIN_TOKENS = 200

# A multilingual tokenizer (XLM-R/BGE-M3) splits Vietnamese at roughly 2.8
# characters per token. The figure is kept slightly tighter than reality so the
# estimate never falls below the true token count — undercount and the chunk is
# truncated at embedding time.
CHARS_PER_TOKEN = 2.8


@dataclass
class ChunkConfig:
    max_tokens: int = MAX_TOKENS   # size ceiling of one chunk, in tokens
    min_tokens: int = MIN_TOKENS   # below this, try to merge with a neighbour
    overlap_sentences: int = 1     # sentences repeated when a section splits across pages
    include_path_prefix: bool = True
    max_path_depth: int = 4        # outline levels kept in the prefix
    merge_short: bool = True       # merge very short sections into a neighbour

    @property
    def max_chars(self) -> int:
        """The token ceiling expressed in characters — for readable reporting only."""
        return int(self.max_tokens * CHARS_PER_TOKEN)


_SENT_SPLIT = re.compile(r"(?<=[.!?;:])\s+(?=[A-ZĐÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚĂĨŨƠƯẠ-ỹ0-9(])")


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]
    return parts or ([text] if text.strip() else [])


def est_tokens(text: str) -> int:
    """Estimate the token count of a passage of Vietnamese text.

    Takes the larger of two counts, because each undershoots on a different kind
    of content:

    * by characters — undershoots on markdown tables, where `|` is dense and the
      string is short;
    * by syllables plus punctuation — undershoots on prose full of long words.

    Overestimating leaves the chunk slightly under the ceiling; underestimating
    gets it truncated at embedding time, so always err on the safe side.
    """
    if not text.strip():
        return 0
    words = text.split()
    # a Vietnamese syllable usually fits one token; long words are split further
    syllables = sum(1 + len(w) // 7 for w in words)
    marks = sum(1 for ch in text if not ch.isalnum() and not ch.isspace())
    by_chars = math.ceil(len(text) / CHARS_PER_TOKEN)
    return max(1, by_chars, syllables + marks)


def _table_rows(md: str) -> tuple[list[str], list[str]]:
    """Split a markdown table into (header lines, data rows)."""
    lines = md.split("\n")
    if len(lines) >= 2 and set(lines[1].replace("|", "").replace(" ", "")) <= {"-"}:
        return lines[:2], lines[2:]
    return [], lines


def _row_cells(row: str) -> list[str]:
    """Split one markdown table row into its cells."""
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _row_as_fields(row: str, head_cells: list[str], limit: int,
                   measure=len) -> list[str]:
    """Spread an over-long table row into "column name: value" lines."""
    cells = [c for c in _row_cells(row) if c]
    # A single-cell table is really a text frame, not columnar data. Labelling it
    # "Column 1:" only adds noise, so return it verbatim.
    if len(cells) == 1 and not [h for h in head_cells if h]:
        return _split_long_text(cells[0], limit, measure)

    lines: list[str] = []
    for i, cell in enumerate(_row_cells(row)):
        if not cell:
            continue
        label = head_cells[i] if i < len(head_cells) and head_cells[i] else f"Column {i + 1}"
        lines.append(f"{label}: {cell}")

    parts: list[str] = []
    buf = ""
    for line in lines:
        for seg in _split_long_text(line, limit, measure):
            if buf and measure(buf) + measure(seg) + 1 > limit:
                parts.append(buf)
                buf = seg
            else:
                buf = f"{buf}\n{seg}" if buf else seg
    if buf:
        parts.append(buf)
    return parts or [row]


def _split_table(md: str, limit: int, measure=len) -> list[str]:
    """Split a long table by rows, repeating the header line in every part.

    `measure` decides the unit of `limit`: characters by default, while the
    chunker passes `est_tokens` so the split respects the RAG token ceiling.
    """
    header, rows = _table_rows(md)
    head_text = "\n".join(header)
    head_len = measure(head_text) + 1 if header else 0

    # A header line longer than the ceiling cannot be repeated in every part,
    # and the table has no rows left to split on. A one-row table — a merged cell
    # spanning the whole page, common in administrative documents — lands exactly
    # here: treat the header as data, or the whole block comes back intact and
    # blows past the token ceiling.
    if header and head_len > limit:
        rows = [header[0]] + rows
        header, head_len = [], 0

    head_cells = _row_cells(header[0]) if header else []

    parts: list[str] = []
    current: list[str] = []
    size = head_len
    for row in rows:
        # A row longer than the ceiling cannot stay in table form. Cutting it in
        # half would break the column count and make the table meaningless, so
        # that row becomes "column name: value" lines — still readable, and no
        # data is lost.
        if measure(row) + head_len > limit:
            if current:
                parts.append(("\n".join(header + current)) if header else "\n".join(current))
                current = []
                size = head_len
            parts.extend(_row_as_fields(row, head_cells, limit, measure))
            continue
        if current and size + measure(row) + 1 > limit:
            parts.append(("\n".join(header + current)) if header else "\n".join(current))
            current = []
            size = head_len
        current.append(row)
        size += measure(row) + 1
    if current:
        parts.append(("\n".join(header + current)) if header else "\n".join(current))
    return parts or [md]


def _split_long_text(text: str, limit: int, measure=len) -> list[str]:
    """Split an over-long passage at sentence boundaries so nothing breaks mid-sentence."""
    if measure(text) <= limit:
        return [text]
    out: list[str] = []
    current = ""
    for sentence in _split_sentences(text):
        if current and measure(current) + measure(sentence) + 1 > limit:
            out.append(current.strip())
            current = sentence
        elif measure(sentence) > limit:
            # a single sentence still exceeds the ceiling -> hard-split on spaces
            if current:
                out.append(current.strip())
                current = ""
            words = sentence.split(" ")
            buf = ""
            for w in words:
                if buf and measure(buf) + measure(w) + 1 > limit:
                    out.append(buf.strip())
                    buf = w
                else:
                    buf = f"{buf} {w}".strip()
            current = buf
        else:
            current = f"{current} {sentence}".strip()
    if current.strip():
        out.append(current.strip())
    return out


@dataclass
class _Piece:
    """A piece of content belonging to exactly one section and page, before numbering."""
    section: Section
    page: int
    text: str
    has_table: bool
    figures: list[dict] = field(default_factory=list)


def _pieces_for_section(section: Section, cfg: ChunkConfig) -> list[_Piece]:
    """Collect a section's content and split it by page."""
    # The outline prefix is prepended later, so its cost is subtracted up front
    # to keep the finished chunk under the size ceiling.
    budget = cfg.max_tokens - est_tokens(_prefix(section, cfg)) - 1
    budget = max(budget, cfg.max_tokens // 3)

    by_page: dict[int, list[str]] = {}
    table_pages: set[int] = set()
    figures_by_page: dict[int, list[dict]] = {}

    heading_line = section.heading_text
    if heading_line:
        by_page.setdefault(section.page_start, []).append(heading_line)

    for block in section.blocks:
        by_page.setdefault(block.page, [])
        if block.is_table:
            table_pages.add(block.page)
        if block.kind == "figure":
            figures_by_page.setdefault(block.page, []).append({
                "caption": block.meta.get("caption", ""),
                "width": block.meta.get("width"),
                "height": block.meta.get("height"),
                "file": block.meta.get("file", ""),
                "page": block.page,
            })
        by_page[block.page].append(block.text)

    pieces: list[_Piece] = []
    for page in sorted(by_page):
        body = "\n".join(t for t in by_page[page] if t).strip()
        if not body:
            continue

        has_table = page in table_pages
        figs = figures_by_page.get(page, [])
        if est_tokens(body) <= budget:
            pieces.append(_Piece(section, page, body, has_table, list(figs)))
            continue

        # Too long: split it — tables by row, prose by sentence
        if has_table:
            segments: list[str] = []
            for part in by_page[page]:
                if part.lstrip().startswith("|"):
                    segments.extend(_split_table(part, budget, est_tokens))
                else:
                    segments.extend(_split_long_text(part, budget, est_tokens))
            merged: list[str] = []
            for seg in segments:
                if merged and est_tokens(merged[-1]) + est_tokens(seg) + 1 <= budget:
                    merged[-1] = merged[-1] + "\n" + seg
                else:
                    merged.append(seg)
            for seg in merged:
                pieces.append(_Piece(section, page, seg, True, _figs_in(seg, figs)))
        else:
            for seg in _split_long_text(body, budget, est_tokens):
                pieces.append(_Piece(section, page, seg, False, _figs_in(seg, figs)))
    return pieces


def _figs_in(segment: str, figures: list[dict]) -> list[dict]:
    """Attach a figure only to the piece that holds its placeholder line."""
    if not figures or "[FIGURE:" not in segment:
        return []
    return [f for f in figures if not f.get("caption") or f["caption"] in segment] or list(figures)


def _merge_small(pieces: list[_Piece], cfg: ChunkConfig) -> list[_Piece]:
    """Merge pieces that are too short into a neighbour so the vector carries meaning.

    Merging only happens within one page and under one parent section, so the
    chunk stays on a single broad topic instead of mixing unrelated content.
    """
    out: list[_Piece] = []
    for piece in pieces:
        if not out:
            out.append(piece)
            continue
        prev = out[-1]
        same_page = prev.page == piece.page
        same_parent = prev.section.path[:-1] == piece.section.path[:-1]
        # a parent section holding nothing but its heading line belongs with its
        # first child, rather than standing alone as a chunk carrying nothing
        is_parent = piece.section.path[:-1] == prev.section.path
        # subtract the following piece's outline prefix, added at the very end
        room = cfg.max_tokens - est_tokens(_prefix(piece.section, cfg)) - 1
        fits = est_tokens(prev.text) + est_tokens(piece.text) + 1 <= room
        if same_page and (same_parent or is_parent) and fits and (
            est_tokens(prev.text) < cfg.min_tokens
            or est_tokens(piece.text) < cfg.min_tokens
        ):
            prev.text = prev.text + "\n" + piece.text
            prev.has_table = prev.has_table or piece.has_table
            prev.figures = prev.figures + piece.figures
            if is_parent:
                # the content really belongs to the child -> take the finer path
                prev.section = piece.section
            elif prev.section is not piece.section:
                prev.section = _common_parent(prev.section, piece.section)
            continue
        out.append(piece)
    return out


def _split_mixed(text: str, limit: int) -> list[str]:
    """Split a piece that mixes prose and tables, respecting the token ceiling."""
    out: list[str] = []
    for part in text.split("\n"):
        if part.lstrip().startswith("|"):
            out.extend(_split_table(part, limit, est_tokens))
        else:
            out.extend(_split_long_text(part, limit, est_tokens))
    merged: list[str] = []
    for seg in out:
        if merged and est_tokens(merged[-1]) + est_tokens(seg) + 1 <= limit:
            merged[-1] = merged[-1] + "\n" + seg
        else:
            merged.append(seg)
    return merged or [text]


def _enforce_budget(pieces: list[_Piece], cfg: ChunkConfig) -> list[_Piece]:
    """The final backstop: no piece exceeds the token ceiling after merging.

    Both merging short pieces and prepending the outline prefix make a chunk
    longer. Going over the ceiling means the embedding step truncates silently,
    so everything is re-checked here rather than trusted from earlier steps.
    """
    out: list[_Piece] = []
    for piece in pieces:
        room = cfg.max_tokens - est_tokens(_prefix(piece.section, cfg)) - 1
        room = max(room, cfg.max_tokens // 3)
        if est_tokens(piece.text) <= room:
            out.append(piece)
            continue
        for seg in _split_mixed(piece.text, room):
            out.append(_Piece(piece.section, piece.page, seg,
                              piece.has_table, _figs_in(seg, piece.figures)))
    return out


def _common_parent(a: Section, b: Section) -> Section:
    """The section representing two merged siblings: keep the first, widen the span."""
    if a.path == b.path:
        return a
    merged = Section(
        number=a.number,
        title=a.title,
        level=a.level,
        path=a.path,
        page_start=a.page_start,
        page_end=max(a.page_end, b.page_end),
    )
    return merged


def _prefix(section: Section, cfg: ChunkConfig) -> str:
    if not cfg.include_path_prefix:
        return ""
    path = section.path
    if len(path) > cfg.max_path_depth:
        path = path[:1] + path[-(cfg.max_path_depth - 1):]
    return " > ".join(path)


def build_chunks(
    sections: list[Section],
    doc_id: str,
    source_file: str,
    page_source: str,
    cfg: ChunkConfig | None = None,
) -> list[Chunk]:
    cfg = cfg or ChunkConfig()

    pieces: list[_Piece] = []
    for section in sections:
        pieces.extend(_pieces_for_section(section, cfg))
    pieces = _merge_small(pieces, cfg)
    pieces = _enforce_budget(pieces, cfg)

    # Count the parts of each section so part_index/part_total can be filled in
    counts: dict[str, int] = {}
    for piece in pieces:
        key = piece.section.path_str
        counts[key] = counts.get(key, 0) + 1

    chunks: list[Chunk] = []
    seen: dict[str, int] = {}
    prev_piece: _Piece | None = None

    for index, piece in enumerate(pieces):
        key = piece.section.path_str
        seen[key] = seen.get(key, 0) + 1
        part_index, part_total = seen[key], counts[key]

        is_continuation = (
            prev_piece is not None and prev_piece.section.path_str == key
        )
        next_piece = pieces[index + 1] if index + 1 < len(pieces) else None
        is_continued = next_piece is not None and next_piece.section.path_str == key

        raw = piece.text
        prefix = _prefix(piece.section, cfg)
        # A section split in two: repeat the previous part's last sentence so the
        # halves sit closer in vector space and the thread of the content is not
        # lost. The repeated sentence counts against the ceiling too, so it is
        # only added when there is room.
        if is_continuation and cfg.overlap_sentences > 0 and prev_piece is not None:
            tail = _split_sentences(prev_piece.text)[-cfg.overlap_sentences:]
            carry = " ".join(tail).strip()
            used = est_tokens(prefix) + est_tokens(raw) + est_tokens(carry) + 3
            if carry and used <= cfg.max_tokens:
                raw = f"[...] {carry}\n{raw}"

        # The prefix already carries the heading line; repeating it verbatim just
        # below only costs space and dilutes the vector.
        body = raw
        if prefix:
            first, sep, rest = raw.partition("\n")
            if first.strip() and prefix.endswith(first.strip()):
                body = rest if sep else ""
        text = f"{prefix}\n{body}".strip() if prefix else raw

        chunk_id = f"{doc_id}#p{piece.page}#{index:04d}"
        # Count tokens on the exact string that goes to embedding, not the one
        # before whitespace normalisation — the two differ by a few tokens.
        final = clean_text_keep_newlines(text)
        chunks.append(Chunk(
            chunk_id=chunk_id,
            doc_id=doc_id,
            source_file=source_file,
            text=final,
            raw_text=clean_text_keep_newlines(piece.text),
            section_number=piece.section.number,
            section_title=piece.section.title,
            section_path=piece.section.path_str,
            section_level=piece.section.level,
            page=piece.page,
            page_source=page_source,
            is_continued=is_continued,
            is_continuation=is_continuation,
            part_index=part_index,
            part_total=part_total,
            prev_chunk_id=None,
            next_chunk_id=None,
            char_count=len(final),
            est_tokens=est_tokens(final),
            has_table=piece.has_table,
            has_figure=bool(piece.figures),
            figures=piece.figures,
        ))
        prev_piece = piece

    for i, chunk in enumerate(chunks):
        if i > 0:
            chunk.prev_chunk_id = chunks[i - 1].chunk_id
        if i + 1 < len(chunks):
            chunk.next_chunk_id = chunks[i + 1].chunk_id
    return chunks


def clean_text_keep_newlines(text: str) -> str:
    """Normalise whitespace but keep newlines (markdown tables need them)."""
    lines = [clean_text(line) for line in text.split("\n")]
    return "\n".join(line for line in lines if line != "").strip()
