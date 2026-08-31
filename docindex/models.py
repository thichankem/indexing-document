"""Data types shared across the whole pipeline."""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Any


# Invisible characters that leak into PDFs exported from Word
# (zero-width space, BOM, soft hyphen)
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍﻿­"), None)


# Mathematical symbols and typographic punctuation that a RAG stack reads back
# as unknown glyphs. They are folded into words or ASCII equivalents: read as
# "Tổng", "∑" still makes sense in the sentence; left as-is, tokenisation
# returns a character that is in no vocabulary. The replacement words are in
# Vietnamese because the source documents are.
_SYMBOL_WORDS = {
    "∑": " Tổng ", "∏": " Tích ", "√": " căn ", "∞": " vô cùng ",
    "≤": "<=", "≥": ">=", "≠": "!=", "≈": "~=", "±": "+/-",
    "×": "x", "÷": "/", "∗": "*", "→": "->", "⇒": "=>",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-", "―": "-",
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "‘": "'", "’": "'", "‚": "'", "′": "'", "″": '"',
    "•": "-", "▪": "-", "●": "-", "◦": "-", "○": "-",
}
_SYMBOL_MAP = {ord(k): v for k, v in _SYMBOL_WORDS.items()}

# The Symbol/Wingdings fonts embedded by Word push their glyphs into the
# private use area U+F0xx while keeping their ASCII code positions — U+F02B is
# therefore really a "+". Only punctuation and digits are mapped back: the
# letter range holds the Greek alphabet or Wingdings ornaments, and folding
# those to Latin would invent a word that is nowhere in the document.
_PUA_EXTRA = {0xF0B7: "-", 0xF0A7: "-", 0xF0D8: "->", 0xF0E0: "->"}


def _from_symbol_font(code: int) -> str:
    if code in _PUA_EXTRA:
        return _PUA_EXTRA[code]
    if 0xF020 <= code <= 0xF07E:
        ch = chr(code - 0xF000)
        return ch if not ch.isalpha() else " "
    return " "


def normalize_symbols(s: str) -> str:
    """Fold special characters down to plain, readable letters.

    Formulas in banking documents are typed in Equation Editor, so their
    variables live in the Mathematical Alphanumeric Symbols block ("𝐌𝐢", not
    "Mi"). A RAG stack has no font for that block, so it reads them back as
    "$Mi$" or drops them entirely — they have to be lowered to plain Latin
    during cleaning.

    `NFKC` handles most of it (mathematical letters, fullwidth digits,
    ligatures, non-breaking spaces); what is left are the maths symbols and
    typographic punctuation, translated through the table above.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    if any(0xE000 <= ord(ch) <= 0xF8FF for ch in s):
        s = "".join(_from_symbol_font(ord(ch))
                    if 0xE000 <= ord(ch) <= 0xF8FF else ch for ch in s)
    return s.translate(_SYMBOL_MAP)


def clean_text(s: str) -> str:
    """Normalise text: drop invisible characters, fold symbols, collapse whitespace."""
    if not s:
        return ""
    s = s.translate(_INVISIBLE)
    s = normalize_symbols(s)
    s = s.replace(" ", " ").replace("\t", " ")
    s = re.sub(r"[ ]{2,}", " ", s)
    return s.strip()


def rows_to_markdown(rows: list[list[str]]) -> str:
    """Build a markdown table from rows, dropping columns that are entirely empty.

    Merged cells in Word/PDF extract as runs of empty adjacent columns
    ("| | Contract year | | |"), which makes the table hard to read and wastes
    the chunk's token budget.
    """
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return ""

    width = max(len(r) for r in rows)
    rows = [list(r) + [""] * (width - len(r)) for r in rows]
    keep = [i for i in range(width) if any(r[i].strip() for r in rows)]
    if not keep:
        return ""
    rows = [[r[i] for i in keep] for r in rows]

    out = ["| " + " | ".join(rows[0]) + " |",
           "| " + " | ".join(["---"] * len(keep)) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


@dataclass
class Block:
    """The smallest unit of content pulled out of a file: a paragraph, a line or a table."""

    text: str
    page: int                      # page number (1-based)
    kind: str = "para"             # para | heading | table | caption
    number: str | None = None      # rendered section number, e.g. "1.", "2.1.", "a)"
    level: int | None = None       # heading level (1 = topmost)
    bold: bool = False
    size: float = 0.0
    is_table: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


PREAMBLE_TITLE = "Phần mở đầu"

_ENDS_SENTENCE = re.compile(r"[.:;!?]\s*$")
# A line that opens a new item: a bullet, "a)", "(i)", "1.2."
_STARTS_ITEM = re.compile(
    r"^\s*([-–—•+*]|\(?[a-zA-Zđ][.)]\s|\(?[ivx]+[.)]\s|\d+(\.\d+)*[.)]?\s)")


def continues_sentence(lead: str, follow: str) -> bool:
    """Is `follow` the second half of a sentence left unfinished in `lead`?

    A PDF stores rendered lines, so one sentence often spans two blocks. Miss
    that and the paragraph breaks in mid-sentence. The leading capital is not
    used as a signal: the second half very often starts with a proper noun
    ("…Dai-ichi Life Việt" / "Nam được…").
    """
    if not lead or not follow:
        return False
    if _ENDS_SENTENCE.search(lead):
        return False
    return not _STARTS_ITEM.match(follow)


def document_title(path: str) -> str:
    """Document title derived from the file name; the root of the outline tree.

    A RAG stack uses the document title as the top level of every chunk title,
    so the rebuilt document has to carry that line. Converted files often keep
    a double extension ("quy-dinh.docx.pdf"); both have to be stripped to get
    at the real name.
    """
    name = os.path.basename(path)
    for _ in range(2):
        stem, ext = os.path.splitext(name)
        if ext.lower() not in (".pdf", ".docx", ".doc"):
            break
        name = stem
    return clean_text(name.replace("_", " ")).strip()


@dataclass
class Section:
    """One section of the document tree."""

    number: str                    # "2.1."
    title: str                     # section name, shortened to fit the path
    level: int
    path: list[str]                # outline path from the root down to here
    kind: str = ""                 # heading kind: decimal | article | banner | …
    blocks: list[Block] = field(default_factory=list)
    page_start: int = 0
    page_end: int = 0
    # The heading line verbatim. Long headings are shortened for the path, but
    # the chunk body has to keep every word or data is lost.
    full_heading: str = ""
    # The section name used for the heading line *on the page*: it can be
    # longer than `title` because, unlike the outline prefix, it does not share
    # the token budget with the body.
    display_title: str = ""
    # This section's own name, unchanged when it absorbs a sibling. Used to
    # spot text that already sits on the heading line and avoid repeating it.
    own_title: str = ""

    @property
    def path_str(self) -> str:
        return " > ".join(self.path)

    @property
    def heading_str(self) -> str:
        return f"{self.number} {self.title}".strip()

    @property
    def display_heading(self) -> str:
        """The heading line as printed: section number plus the full-length name."""
        return f"{self.number} {self.display_title or self.title}".strip()

    @property
    def heading_text(self) -> str:
        """The complete heading line to place in the chunk body."""
        return self.full_heading or self.heading_str

    @property
    def is_merged(self) -> bool:
        """This section absorbed the number of the sibling after it ("1.1 + 1.2").

        When it did, `heading_str` collects several names while `full_heading`
        stays the section's own original heading line — the absorbed section's
        text lives in the body, not on the heading line.
        """
        return " + " in self.number

    @property
    def is_banner(self) -> bool:
        """An unnumbered banner heading — a divider between parts of the document.

        It is not a topic of its own, only the frame around the real sections
        below it, so it must never absorb another section's content (or be
        absorbed): doing so files numbered content under a node that carries no
        number to look it up by.
        """
        return self.kind == "banner"

    @property
    def is_preamble(self) -> bool:
        """Content that sits before the first heading — not a real section.

        The name PREAMBLE_TITLE is invented by the tool to collect the cover
        page and the opening remarks. It does not exist in the document, so it
        must not be drawn as a heading, or the RAG outline would grow a branch
        that is not there.
        """
        return not self.number and self.title == PREAMBLE_TITLE


@dataclass
class Chunk:
    """The final unit handed to the embedding step."""

    chunk_id: str
    doc_id: str
    source_file: str
    text: str                      # body with the section_path prefix prepended
    raw_text: str                  # body as-is, without the prefix
    section_number: str
    section_title: str
    section_path: str
    section_level: int
    page: int
    page_source: str               # actual | estimated
    is_continued: bool             # this section continues in the next chunk
    is_continuation: bool          # this chunk continues a section from before
    part_index: int                # index of this part within the section
    part_total: int
    prev_chunk_id: str | None
    next_chunk_id: str | None
    char_count: int
    est_tokens: int
    has_table: bool
    has_figure: bool = False
    figures: list[dict] = field(default_factory=list)   # figures inside this chunk
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
