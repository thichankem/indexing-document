"""Detect headings and build the outline tree.

The governing principle: a chunk should hold exactly one topic. Topics are
defined here by the document's own outline, so chunk quality depends directly
on whether headings are detected correctly.

The heading patterns below are written for Vietnamese administrative and legal
documents, which is what this tool processes; the keywords in them are part of
the input format, not user-facing text.
"""
from __future__ import annotations

import re

from .models import PREAMBLE_TITLE, Block, Section, clean_text, continues_sentence

# Rank of each heading kind in Vietnamese administrative and legal documents.
# Lower number = higher level.
#
# The marks are spaced widely because a Word list level is added on top of the
# mark: two different marker kinds must never land on the same number, or two
# distinct levels collapse into one when the tree is built.
RANK_BANNER = -1       # an unnumbered banner heading, e.g. "TỔNG QUAN…"
RANK_PART = 0          # PHẦN 1, Phần I
RANK_CHAPTER = 1       # CHƯƠNG I
RANK_APPENDIX = 1      # Phụ lục 01
RANK_SECTION = 2       # MỤC 1
RANK_ROMAN_UPPER = 3   # I.  II.  III.  — a high level in administrative texts
RANK_ARTICLE = 4       # ĐIỀU 5
RANK_DECIMAL = 10      # 1.  /  2.1.  /  2.3.1.
RANK_LETTER_UPPER = 25 # A)  B)
RANK_LETTER = 30       # a)  b)
RANK_ROMAN = 50        # (i) (ii)

# Valid Roman numerals: I..XXXIX. Used to tell "I." (a high level) from "i)"
# (the ninth item of an a) b) … i) letter list — an entirely different thing).
_IS_ROMAN = re.compile(r"^(?=[ivx])x{0,3}(ix|iv|v?i{0,3})$", re.I)

_PATTERNS: list[tuple[re.Pattern, int, str]] = [
    (re.compile(r"^(PHẦN|Phần)\s+([0-9IVXivx]+)\s*[:.\-–]?\s*(.*)$"), RANK_PART, "part"),
    (re.compile(r"^(CHƯƠNG|Chương)\s+([0-9IVXivx]+)\s*[:.\-–]?\s*(.*)$"), RANK_CHAPTER, "chapter"),
    (re.compile(r"^(PHỤ LỤC|Phụ lục)\s*([0-9IVXivx]*)\s*[:.\-–]?\s*(.*)$"), RANK_APPENDIX, "appendix"),
    # An internal appendix code, e.g. "PL02.1003.PCS.2026(1): Giải thích từ ngữ".
    # The separator does not accept a dot: the dot is part of the code itself, and
    # accepting it makes a cross-reference like "…theo Phụ lục số
    # PL01.1016.PDS.2026(1);" match too, spawning a high-level branch that
    # swallows every section after it.
    (re.compile(r"^(PL)\s*([0-9][0-9.()A-Za-z]*)\s*[:\-–]\s*(.*)$"), RANK_APPENDIX, "appendix"),
    (re.compile(r"^(MỤC|Mục)\s+([0-9IVXivx]+)\s*[:.\-–]?\s*(.*)$"), RANK_SECTION, "section"),
    (re.compile(r"^(ĐIỀU|Điều)\s+(\d+)\s*[:.\-–]?\s*(.*)$"), RANK_ARTICLE, "article"),
]

# "1." "2.1." "2.3.1." — the trailing dot is optional
_DECIMAL = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.*)$")
# A heading that is only a number, with the name on the line below
_DECIMAL_ONLY = re.compile(r"^(\d+(?:\.\d+)*)\.?$")
# Keep the punctuation after the marker ("c." rather than "c") so the heading
# reads exactly as it does in the source document.
_LETTER = re.compile(r"^([a-zA-Z][.)])\s+(\S.*)$")
_ROMAN = re.compile(r"^(\(?[ivxIVX]{1,5}[.)])\s+(\S.*)$")

# A sentence opening with a clause cross-reference, easily mistaken for a heading
_REFERENCE = re.compile(
    r"^(theo|tại|quy định tại|căn cứ|xem|nêu tại|như)\b", re.I
)

# An unnumbered banner heading ("TỔNG QUAN VĂN BẢN QUY ĐỊNH", "QUY ĐỊNH SẢN
# PHẨM TIẾT KIỆM LINH HOẠT"): at most this many characters, and almost entirely
# uppercase.
MAX_BANNER_LEN = 120
BANNER_UPPER_RATIO = 0.8
# A document with exactly one banner heading has that heading as its title —
# which is already the root of the tree. Giving it another branch grows one
# redundant level, and the tree only goes 3 deep. From two banners up they
# genuinely divide the document into major parts and must become the top level.
MIN_BANNERS = 2

# The outline goes at most 3 levels deep. From level 4 ("1.1.1.1") down, a
# heading stops being a branch and becomes ordinary content of its parent:
# splitting that far leaves each node holding one or two sentences, its vector
# carries almost no information, and the chunk title grows a pointless level.
MAX_TREE_LEVEL = 3

MAX_HEADING_LEN = 250
# Maximum heading length when it goes into the outline path
MAX_TITLE_IN_PATH = 90
# Maximum heading length when it is *printed on the page*. Far wider than the
# path ceiling: the outline prefix has to yield room to the content and must be
# trimmed, but the heading line on the page has no such constraint — cutting it
# at 90 characters breaks mid-phrase ("…làm nguyên" / "liệu sản xuất") and a DLA
# model reads a section name with words missing.
MAX_TITLE_IN_HEADING = 200
# A section name (the part before the colon) longer than this is no longer a name
MAX_NAME_IN_HEADING = 60
# A heading with no colon: the whole line is the name. Only lines longer than
# this are treated as prose that happens to open with a number
MAX_PLAIN_HEADING_LEN = 160
# The colon separating a section name from its content — not one between digits
_NAME_COLON = re.compile(r"(?<!\d):(?!\d)")

# A table row flattened into one line: "STT: 3.1 | Nội dung: Đối tượng khách
# hàng | Cụ thể: …". The pipe separates the columns.
_FLAT_FIELD = re.compile(r"\s*\|\s*")
# A cell holding only an index ("3.1", "02", "-") is not a section name
_INDEX_VALUE = re.compile(r"^[\d.,/()\-–\s]*$")


def _flat_row_name(title: str, limit: int) -> str | None:
    """The section name of a flattened table row, or None if it is not one.

    When a table is flattened before the file reaches the tool, every row
    becomes one line, and that line always opens with the index column
    ("STT: 3.1"). Cutting at the first colon, as for an ordinary heading, takes
    the *column name* as the section name — turning the whole outline into a run
    of identical "STT" entries with nothing to tell one section from another.

    The real name is the first value that carries meaning: skip columns holding
    only an index and columns left blank, then prefer a value short enough to
    serve as a name.
    """
    fields = _FLAT_FIELD.split(title)
    if len(fields) < 2:
        return None
    labelled = False
    values = []
    for field in fields:
        label, sep, value = field.partition(":")
        # The flattening step sometimes loses a column's label ("Khoản: 3.1 |
        # Điều kiện vay vốn | Quy định:"): the whole cell is then the value.
        if not sep:
            value = label
        labelled = labelled or bool(sep)
        value = value.strip(" .;-–")
        if value and not _INDEX_VALUE.match(value):
            values.append(value)
    if not labelled:
        return None
    # A continuation row of a cell spanning several rows: every column is blank
    # and this row has no name. Return an empty name so the section shows as its
    # number alone — grabbing a column label instead fills the outline with an
    # indistinguishable run of "STT" entries.
    if not values:
        return ""
    # A long content column is body text, not a name; used only as a last resort
    return next((v for v in values if len(v) <= limit), values[0])


def shorten_title(title: str, limit: int = MAX_TITLE_IN_PATH) -> str:
    """Reduce a heading to its most meaningful part — the section's *name*.

    Many sections in administrative documents run the content straight into the
    heading line ("Hợp đồng bảo hiểm: là tất cả văn bản thể hiện sự thỏa thuận
    giữa Bên mua bảo hiểm và Dai-ichi Life Việt Nam…"). The name is the part
    before the colon; what follows is content and moves down into the body.

    The cut-at-the-colon rule has to run at *every* length, not only when the
    heading exceeds the ceiling: an 80-character sentence is still a sentence,
    and using it whole as a section name makes the outline read like prose and
    the chunk titles plainly wrong.
    """
    title = title.strip()
    flat = _flat_row_name(title, limit)
    if flat is not None:
        title = flat
    # Skip a colon between two digits ("8:30", "1:2") — that is a time or a
    # ratio, not the boundary between name and content.
    mark = _NAME_COLON.search(title)
    if mark:
        head = title[:mark.start()].strip()
        # if the name exceeds the ceiling, that colon sits mid-sentence and is
        # not a name boundary
        if head and len(head) <= limit:
            return head
    if len(title) <= limit:
        return title
    cut = title[:limit]
    for sep in (". ", ", ", "; "):
        idx = cut.rfind(sep)
        if idx >= limit // 2:
            return cut[:idx].strip()
    idx = cut.rfind(" ")
    return (cut[:idx] if idx >= limit // 2 else cut).strip() + "…"


def _match_heading(text: str) -> tuple[str, str, int, str] | None:
    """Return (number, title, rank, kind) if the text looks like a heading."""
    for pattern, rank, kind in _PATTERNS:
        m = pattern.match(text)
        if m:
            prefix, number = m.group(1), m.group(2)
            # An internal code is written solid ("PL02.1003.PCS"); splitting it
            # into "PL 02" would break lookups by code.
            if prefix.upper() == "PL":
                num = f"{prefix}{number}".strip()
            else:
                num = f"{prefix} {number}".strip()
            return num, m.group(3).strip(" :.-–"), rank, kind

    m = _DECIMAL.match(text)
    if m:
        num, title = m.group(1), m.group(2)
        depth = num.count(".") + 1
        return num, title.strip(" :.-–"), RANK_DECIMAL + depth - 1, "decimal"

    m = _DECIMAL_ONLY.match(text)
    if m:
        num = m.group(1)
        depth = num.count(".") + 1
        return num, "", RANK_DECIMAL + depth - 1, "decimal"

    m = _ROMAN.match(text)
    if m:
        marker, title = m.group(1), m.group(2).strip(" :.-–")
        rank, kind = _roman_rank(marker)
        return marker, title, rank, kind

    m = _LETTER.match(text)
    if m:
        marker = m.group(1)
        rank = RANK_LETTER_UPPER if marker[:1].isupper() else RANK_LETTER
        return marker, m.group(2).strip(" :.-–"), rank, "letter"

    return None


def _roman_rank(marker: str) -> tuple[int, str]:
    """The level of a Roman marker — not all of them are deep levels.

    In Vietnamese administrative documents "I." and "II." are a high level,
    above even "1.", while "(i)" and "(ii)" are the deepest. A bare "i)", by
    contrast, is usually just the ninth item of an a) b) c)… list — giving it a
    level of its own spawns a phantom tier in the middle of that list.
    """
    token = marker.strip("().")
    if token.isupper() and _IS_ROMAN.match(token):
        return RANK_ROMAN_UPPER, "roman-upper"
    if len(token) > 1 or marker.startswith("("):
        return RANK_ROMAN, "roman"
    return RANK_LETTER, "letter"


def _word_rank(marker: str, list_format: str, word_level: int) -> int:
    """The level of a number Word generated.

    Word's list level (`ilvl`) is not trustworthy: real documents routinely
    declare an "a) b) c)" list at the same ilvl as "1. 2. 3.", and the outline
    then flattens — a child section becomes a sibling of its parent. It is the
    marker kind (`numFmt`) that states the true hierarchy; `ilvl` is used only to
    rank levels *within* one marker kind.
    """
    fmt = (list_format or "").lower()
    offset = max(0, (word_level or 1) - 1)
    if "roman" in fmt:
        return (RANK_ROMAN_UPPER if "upper" in fmt else RANK_ROMAN) + offset
    if "letter" in fmt:
        return (RANK_LETTER_UPPER if "upper" in fmt else RANK_LETTER) + offset
    # A multi-level number ("2.1") already states its own depth
    dots = marker.strip().rstrip(".").count(".")
    return RANK_DECIMAL + max(dots, offset)


def _looks_like_heading(block: Block, text: str, title: str, kind: str,
                        from_word_numbering: bool) -> bool:
    """Filter out ordinary sentences that get mistaken for headings."""
    if _REFERENCE.match(text):
        return False

    # "PHẦN 1" and "ĐIỀU 5" appear constantly inside cross-references
    # ("...quy định tại Điều 2.3 Phần 1 này"). Once PDF extraction wraps the
    # line, that fragment looks exactly like a heading, and mistaking it swallows
    # every section after it. A real heading always carries a capitalised name.
    if kind in ("part", "chapter", "article", "section", "roman-upper"):
        if not title:
            return False
        first = title.lstrip("“\"'(")[:1]
        if first and first.islower():
            return False
        if not from_word_numbering:
            # A real heading in a PDF is always emphasised (bold or uppercase).
            # A cross-reference like "Điều 5.4.a, Điều 5.4.b nêu trên" is not.
            letters = [c for c in title if c.isalpha()]
            mostly_upper = bool(letters) and sum(
                1 for c in letters if c.isupper()) / len(letters) >= 0.8
            if not block.bold and not mostly_upper:
                return False

    if from_word_numbering:
        # Word has declared this a numbered item in the document structure. That
        # is a far stronger signal than anything inferred from typography, above
        # all in documents that do not set their headings in bold.
        return True

    if len(text) > MAX_HEADING_LEN:
        return False

    # No structural signal from the file (usually a PDF): typography is all there
    # is. Letter and Roman markers are extremely common in plain enumerations, so
    # they are only accepted in bold — otherwise the content is shredded into
    # hundreds of tiny sections.
    if kind in ("letter", "roman") and not block.bold:
        return False

    if kind == "decimal" and not block.bold:
        # Headings often run the content into the same line ("1.9 Hợp đồng bảo
        # hiểm: là tất cả văn bản thể hiện sự thỏa thuận…"). Measure the length of
        # the *name* — the part before the colon — because measuring the whole
        # line rejects every heading of this shape, taking its whole subtree with
        # it.
        mark = _NAME_COLON.search(title)
        if mark:
            if len(title[:mark.start()].strip()) > MAX_NAME_IN_HEADING:
                return False
        elif len(text) > MAX_PLAIN_HEADING_LEN:
            # With no colon the whole line is the name. Long section names are
            # routine in legal texts ("15.4 Đối với Khách hàng là doanh nghiệp
            # Việt Nam đưa người lao động Việt Nam đi đào tạo, nâng cao trình độ,
            # kỹ năng nghề ở nước ngoài"); only a line well past that length is
            # prose that happens to open with a number.
            return False

    if title and title[:1].islower() and not block.bold and kind == "decimal":
        return False
    return True


def _flat_column_labels(marked: list[tuple]) -> set[str]:
    """Column names of a flattened table, gathered from rows that kept their labels.

    A cell crossing a page break produces a continuation row holding nothing but
    a column label ("3.1.2 Quy định:"). Looked at alone, "Quy định" is
    indistinguishable from a section name; only comparing it against the other
    rows in the same document reveals it as a column label, and shows that this
    section in fact has no name.
    """
    labels: set[str] = set()
    for _block, _num, title, _rank, _kind, _word in marked:
        fields = _FLAT_FIELD.split(title)
        if len(fields) < 2:
            continue
        for field in fields:
            label, sep, _value = field.partition(":")
            if sep and label.strip():
                labels.add(label.strip().lower())
    return labels


def _drop_column_label(title: str, labels: set[str]) -> str:
    """Strip a leading column label from a flattened table's continuation row.

    A continuation row is down to a single column ("3.1.9 Quy định: năm/lần ĐVKD
    thực hiện đánh giá lại hạn mức…"). The section name is not "Quy định" — that
    is a column label; what follows it is content spilling over from the previous
    row, and if it runs long this row has no name at all.

    Only single-column lines are considered: full rows are `_flat_row_name`'s job.
    """
    if len(_FLAT_FIELD.split(title)) > 1:
        return title
    label, _sep, rest = title.partition(":")
    if label.strip().lower() not in labels:
        return title
    rest = rest.strip(" :.-–")
    return rest if len(rest) <= MAX_NAME_IN_HEADING else ""


_NUMBER_SEQ = re.compile(r"^\d+(?:\.\d+)*\.?$")
# "04 (bốn) Năm hợp đồng đầu tiên" — a zero-padded number is a quantity, not a
# section number. Section numbers are never zero-padded.
_PADDED = re.compile(r"(^|\.)0\d")


def _as_counter(number: str) -> tuple[int, ...] | None:
    """"2.3.1." -> (2, 3, 1). Returns None if it is not a numeric section number."""
    if not _NUMBER_SEQ.match(number.strip()):
        return None
    return tuple(int(p) for p in number.strip().rstrip(".").split("."))


def _continues_sequence(num: tuple[int, ...], prev: tuple[int, ...] | None) -> bool:
    """Does this number continue the sequence currently open?

    A sentence starting with a number ("30 (ba mươi) ngày tuổi đến 70 tuổi…")
    looks exactly like a heading once PDF extraction wraps the line. Mistaking
    one does more than spawn a phantom section: every real section after it drops
    down to become its child, and the titles of every chunk below go wrong with
    it. A real heading's number always continues the open sequence; a number
    inside a sentence does not.
    """
    if prev is None:
        return True
    depth = len(num)
    if depth <= len(prev) and num[:depth - 1] == prev[:depth - 1]:
        # same branch: continue (leaving room for a section extraction missed)
        # or restart the sequence from the beginning
        reference = prev[depth - 1]
        return num[-1] == 1 or reference < num[-1] <= reference + 2
    if depth >= 2 and depth <= len(prev) + 1 and num[-1] == 1:
        # the first child of a different branch — that branch has to continue the
        # sequence too ("2.2.5" then "2.3.1" when the "2.3" heading was missed)
        parent = num[:-1]
        return (parent == prev[:len(parent)]
                or _continues_sequence(parent, prev))
    return False


def _drop_broken_sequence(marked: list[tuple]) -> list[tuple]:
    """Drop numbered headings that continue no sequence.

    Only numbers inferred from typography are checked. Numbers Word generated are
    the document's real structure rather than a guess, so they are always kept.
    """
    out: list[tuple] = []
    prev: tuple[int, ...] | None = None
    for entry in marked:
        _block, num, _title, _rank, kind, from_word = entry
        if kind != "decimal":
            prev = None                        # PHẦN/ĐIỀU/Phụ lục opens a new sequence
            out.append(entry)
            continue
        counter = _as_counter(num)
        if from_word or counter is None:
            prev = counter or prev
            out.append(entry)
            continue
        if _PADDED.search(num) or not _continues_sequence(counter, prev):
            continue
        prev = counter
        out.append(entry)
    return out


def _is_banner(block: Block, body_size: float) -> bool:
    """Is this line an unnumbered banner heading?

    Three cues must all be present: **clearly larger than the body text**,
    **almost entirely uppercase** and **carrying no section number**. Ordinary
    prose never satisfies all three, so this rule almost never misfires — which
    matters, because a misdetected banner swallows the whole part of the document
    that follows it.
    """
    if block.is_table or block.kind == "figure" or block.number:
        return False
    text = clean_text(block.text)
    if not (3 <= len(text) <= MAX_BANNER_LEN):
        return False
    if not body_size or block.size <= body_size:
        return False
    if _match_heading(text):
        return False
    letters = [c for c in text if c.isalpha()]
    if not letters or len(letters) / len(text) < 0.5:
        return False
    # A formula ("L = M *T *R") is also set large and all in capitals. A real
    # heading is a phrase: it needs at least two words of two or more letters.
    if sum(1 for w in text.split() if len(w) >= 2 and w.isalpha()) < 2:
        return False
    upper = sum(1 for c in letters if c.isupper()) / len(letters)
    return upper >= BANNER_UPPER_RATIO


def _mark_banners(blocks: list[Block]) -> list[tuple[Block, str, str, int, str, bool]]:
    """Find the unnumbered banner headings, joining adjacent lines into one.

    A banner is often broken across lines ("QUY ĐỊNH" / "SẢN PHẨM TIẾT KIỆM
    LINH HOẠT" at two different sizes), and the earlier line-joining step leaves
    them apart because the sizes differ. Left split, the outline grows two stub
    branches in place of one heading.
    """
    body_size = next((b.meta.get("body_size") for b in blocks
                      if b.meta.get("body_size")), 0.0)
    if not body_size:
        sizes: dict[float, int] = {}
        for b in blocks:
            if b.kind == "para" and not b.is_table and b.size:
                sizes[b.size] = sizes.get(b.size, 0) + len(b.text)
        body_size = max(sizes, key=sizes.get) if sizes else 0.0

    runs: list[list[Block]] = []
    prev_index = -2
    for index, block in enumerate(blocks):
        if not _is_banner(block, body_size):
            continue
        if runs and index == prev_index + 1 and block.page == runs[-1][-1].page:
            runs[-1].append(block)
        else:
            runs.append([block])
        prev_index = index

    if len(runs) < MIN_BANNERS:
        return []

    marked = []
    for run in runs:
        head = run[0]
        title = clean_text(" ".join(b.text for b in run))
        for extra in run[1:]:
            extra.text = ""                # text moved up onto the heading's first line
        head.text = title
        marked.append((head, "", title, RANK_BANNER, "banner", False))
    return marked


def detect_headings(blocks: list[Block], source: str) -> list[Block]:
    """Mark which blocks are headings and assign them a number and level."""
    ranks_seen: set[int] = set()
    # (block, number, name, rank, kind, was the number generated by Word?)
    marked: list[tuple[Block, str, str, int, str, bool]] = []
    banners = {id(entry[0]): entry for entry in _mark_banners(blocks)}

    for block in blocks:
        if block.is_table or block.kind == "figure":
            continue
        banner = banners.get(id(block))
        if banner is not None:
            marked.append(banner)
            ranks_seen.add(RANK_BANNER)
            continue
        text = block.text
        if not text.strip():
            continue
        word_number = block.number      # a Word-generated number (DOCX only)

        # Hand-typed headings (PHẦN, ĐIỀU, Phụ lục…) are recognised first, even
        # when the paragraph also sits inside a numbered list.
        hit = _match_heading(text)
        if hit and hit[3] in ("part", "chapter", "appendix", "section", "article"):
            num, title, rank, kind = hit
            if _looks_like_heading(block, text, title, kind, False):
                marked.append((block, num, title, rank, kind, False))
                ranks_seen.add(rank)
                continue

        if word_number:
            # Word gave the number -> keep its original form ("1.", "a)", "1.1.")
            # rather than re-deriving it, but infer the level from the marker.
            rank = _word_rank(word_number, block.meta.get("list_format", ""),
                              block.level or 1)
            if _looks_like_heading(block, text, text, "decimal", True):
                marked.append((block, word_number, text, rank, "decimal", True))
                ranks_seen.add(rank)
            continue

        if not hit:
            continue
        num, title, rank, kind = hit
        if not _looks_like_heading(block, text, title, kind, False):
            continue
        marked.append((block, num, title, rank, kind, False))
        ranks_seen.add(rank)

    marked = _drop_broken_sequence(marked)
    labels = _flat_column_labels(marked)

    # Compress the ranks that actually occur into consecutive levels 1, 2, 3…
    ranks_seen = {rank for _b, _n, _t, rank, _k, _w in marked}
    order = {rank: i + 1 for i, rank in enumerate(sorted(ranks_seen))}

    for block, num, title, rank, kind, _from_word in marked:
        block.kind = "heading"
        block.number = num
        block.level = order[rank]
        block.meta["heading_kind"] = kind
        # Continuation row of a flattened table: a column label is not a name
        named = _drop_column_label(title, labels) if labels else title
        # A heading like "Phụ lục 01" has no name of its own; using block.text as
        # the title would render "Phụ lục 01 Phụ lục 01" in the outline path.
        if named:
            block.meta["title"] = named
        elif title or clean_text(block.text) == clean_text(num):
            block.meta["title"] = ""
        else:
            block.meta["title"] = block.text
    return blocks


def _section_tokens(section: Section) -> int:
    from .chunker import est_tokens

    return (est_tokens(section.heading_text)
            + sum(est_tokens(b.text) for b in section.blocks))


def _branch_paths(sections: list[Section]) -> set[tuple[str, ...]]:
    """Paths of the sections that still have children beneath them."""
    branches: set[tuple[str, ...]] = set()
    for section in sections:
        for depth in range(1, len(section.path)):
            branches.add(tuple(section.path[:depth]))
    return branches


def _adjacent_in_tree(a: Section, b: Section) -> bool:
    """Do two adjacent sections belong to the same branch?

    Only siblings (sharing a parent) or a parent-child pair may be merged;
    merging across two branches mixes two topics into one vector.
    """
    return (a.path[:-1] == b.path[:-1]
            or a.path[:-1] == b.path
            or b.path[:-1] == a.path)


def _absorb_section(target: Section, section: Section) -> None:
    """Move all of `section`'s content onto the end of `target`."""
    moved = list(section.blocks)
    if section.heading_text:
        # The absorbed section's heading line is usually the first half of a
        # sentence whose second half is in the block right after it — join them
        # rather than leaving it broken in two.
        if moved and continues_sentence(section.heading_text, moved[0].text):
            moved[0] = Block(text=f"{section.heading_text} {moved[0].text}",
                             page=moved[0].page, kind=moved[0].kind,
                             is_table=moved[0].is_table, meta=moved[0].meta)
        else:
            moved.insert(0, Block(text=section.heading_text,
                                  page=section.page_start, bold=True))
    target.blocks.extend(moved)
    target.page_end = max(target.page_end, section.page_end)
    if target.path[:-1] == section.path[:-1]:
        _absorb_index(target, section)


def merge_short_sections(sections: list[Section], min_tokens: int,
                         max_tokens: int) -> list[Section]:
    """Merge sections that are too short so the chunks do not come out in shreds.

    A glossary running "1.1 … 1.60", one or two lines per entry, yields dozens of
    chunks of a few dozen tokens each — vectors carrying almost no information.
    Merging loses that node from the outline but *loses no words*: the heading
    line moves down to open the body.

    **Both neighbours are considered**, and each round merges exactly one pair:
    take the shortest remaining section and join it to its *smaller* neighbour.
    Looking only backwards leaves a short section stranded forever behind a huge
    one — precisely why a hundred-token "Điều 1" stayed on its own right below an
    eight-hundred-token "MỤC LỤC". Preferring the smaller neighbour keeps the
    merged sections roughly equal in length instead of producing one bloated
    block beside a handful of scraps.

    The loop runs until no mergeable pair is left, so a parent that has just
    absorbed all its children is reconsidered — a single-pass merge misses that
    case entirely.

    The absorbed section must be a leaf (merging one with children would delete a
    branch), the two must share a branch, and the total must still fit the token
    ceiling *minus the outline prefix* that every chunk has to carry.
    """
    if min_tokens <= 0:
        return sections

    out = list(sections)
    size = {id(s): _section_tokens(s) for s in out}

    while True:
        branches = _branch_paths(out)
        best: tuple[int, int, int] | None = None
        for i, section in enumerate(out):
            if section.is_preamble or section.is_banner:
                continue
            if size[id(section)] >= min_tokens:
                continue
            if tuple(section.path) in branches:
                continue
            for j in (i - 1, i + 1):
                if not 0 <= j < len(out):
                    continue
                neighbour = out[j]
                if neighbour.is_preamble or neighbour.is_banner:
                    continue
                if not _adjacent_in_tree(section, neighbour):
                    continue
                left, right = (out[i], out[j]) if j > i else (out[j], out[i])
                # The later section is the one absorbed: it has to be a leaf,
                # while the earlier one keeps its place in the tree.
                if tuple(right.path) in branches:
                    continue
                sibling = left.path[:-1] == right.path[:-1]
                # The 512-token ceiling is the *chunk* ceiling, and a chunk also
                # carries the outline prefix at its head. Forget the prefix and
                # the merged section is split in two by the chunker anyway — the
                # merge achieves nothing and produces two chunks with one name.
                room = max_tokens - _prefix_tokens(left, right if sibling else None)
                if size[id(left)] + size[id(right)] > room:
                    continue
                key = (size[id(neighbour)], i, j)
                if best is None or key < best:
                    best = key
        if best is None:
            return out

        _, i, j = best
        left, right = (out[i], out[j]) if j > i else (out[j], out[i])
        _absorb_section(left, right)
        size[id(left)] += size[id(right)]
        out.remove(right)


def _absorb_index(target: Section, section: Section) -> None:
    """Add the absorbed section's number to the target: 1.1 takes 1.2 -> "1.1 + 1.2".

    Merge the content while keeping the old number and the outline lies: the
    chunk is titled "1.1 Mục đích" while holding all of 1.2 as well. Adding the
    number keeps a lookup by section number landing on the chunk that holds it.

    Numbers are only combined for two siblings. When a child merges up into its
    parent, the parent's number already covers the child and adding it is noise.
    """
    target.number, target.title = _merged_index(target, section)
    target.display_title = _join_names(
        _merged_names(target.display_title, section.display_title),
        MAX_TITLE_IN_HEADING)
    # This section's path changes with it; it has no children (only leaves are
    # ever absorbed), so no branch below is left with a stale path.
    target.path = target.path[:-1] + [target.heading_str]


def _merged_names(kept: str, added: str) -> list[str]:
    names = [t for t in kept.split(" + ") if t and t != _MORE]
    if added:
        names.append(added)
    return list(dict.fromkeys(names))


def _merged_index(target: Section, section: Section) -> tuple[str, str]:
    """The number and name of `target` after it absorbs `section`."""
    numbers = [n for n in dict.fromkeys(target.number.split(" + ")
                                        + [section.number]) if n]
    names = _merged_names(target.title, section.title)
    return " + ".join(numbers), _join_names(names)


def _prefix_tokens(target: Section, absorbing: Section | None) -> int:
    """Tokens of the outline prefix every chunk of `target` has to carry."""
    from .chunker import est_tokens

    path = target.path
    if absorbing is not None:
        number, title = _merged_index(target, absorbing)
        path = path[:-1] + [f"{number} {title}".strip()]
    return est_tokens(" > ".join(path))


_MORE = "…"


def _join_names(names: list[str], limit: int = MAX_TITLE_IN_PATH) -> str:
    """Join the names of merged sections, trimming past the path length ceiling.

    Numbers are kept in full — those are what a lookup uses. Names only tell the
    reader what the section is about, and joining five or six of them bloats the
    chunk prefix and eats the token budget meant for content.
    """
    out: list[str] = []
    used = 0
    for name in names:
        if out and used + len(name) + 3 > limit:
            out.append(_MORE)
            break
        out.append(name)
        used += len(name) + 3
    return " + ".join(out)


def _demote(block: Block) -> None:
    """Demote a heading to an ordinary content line, keeping its number.

    In DOCX the number lives in `block.number` rather than in the text (Word
    generates it at display time), so it has to be spliced back in — drop it and
    the rebuilt document loses its numbering entirely, leaving readers unable to
    check it against the original.
    """
    number = (block.number or "").strip()
    if number and not clean_text(block.text).startswith(number):
        block.text = f"{number} {block.text}".strip()
    block.kind = "para"
    block.number = None
    block.level = None
    block.meta.pop("heading_kind", None)
    block.meta.pop("title", None)


def build_sections(blocks: list[Block]) -> list[Section]:
    """Group blocks into sections, preserving document order.

    Content belongs to the nearest heading above it, whatever page that heading
    is on — which is how a section spanning several pages is handled.
    """
    sections: list[Section] = []
    stack: list[Section] = []          # stack of currently open ancestor headings
    ranks: list[int] = []              # marker-derived levels, for parent/child comparison
    preamble: Section | None = None

    for block in blocks:
        if block.kind == "heading":
            level = block.level or 1
            # The preamble (cover, opening remarks) parents no section
            if stack and stack[0] is preamble:
                stack.clear()
                ranks.clear()
            while ranks and ranks[-1] >= level:
                stack.pop()
                ranks.pop()

            if len(stack) + 1 > MAX_TREE_LEVEL:
                # Deeper than the tree allows: this line stops being a branch and
                # returns to being ordinary content of its parent. The number is
                # spliced into the text so the document's numbering is not lost.
                _demote(block)
                if stack:
                    stack[-1].blocks.append(block)
                    for ancestor in stack:
                        ancestor.page_end = max(ancestor.page_end, block.page)
                    continue

            # An empty name is a settled conclusion from detection ("Phụ lục 01"
            # has no name of its own, nor does a flattened table's continuation
            # row) — not a gap to be filled in from block.text.
            full_title = block.meta.get("title", block.text)
            title = shorten_title(full_title)
            shown = shorten_title(full_title, MAX_TITLE_IN_HEADING)
            path = [s.heading_str for s in stack] + [f"{block.number} {title}".strip()]
            section = Section(
                number=block.number or "",
                title=title,
                kind=block.meta.get("heading_kind", ""),
                display_title=shown,
                own_title=shown,
                full_heading=f"{block.number} {full_title}".strip(),
                # displayed level = true depth in the tree, so no level is skipped
                level=len(stack) + 1,
                path=path,
                page_start=block.page,
                page_end=block.page,
            )
            ranks.append(level)
            sections.append(section)
            stack.append(section)
            continue

        if not sections:
            # content before the first heading (cover page, opening remarks)
            if preamble is None:
                preamble = Section(number="", title=PREAMBLE_TITLE, level=1,
                                   path=[PREAMBLE_TITLE], page_start=block.page,
                                   page_end=block.page)
                sections.append(preamble)
                stack.append(preamble)
            preamble.blocks.append(block)
            preamble.page_end = max(preamble.page_end, block.page)
            continue

        current = stack[-1] if stack else sections[-1]
        current.blocks.append(block)
        current.page_end = max(current.page_end, block.page)
        # ancestors extend their page range too, to keep the metadata consistent
        for ancestor in stack:
            ancestor.page_end = max(ancestor.page_end, block.page)

    return sections
