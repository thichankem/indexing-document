"""Read a .pdf into a list of Blocks, keeping the layout cues (bold, font size)."""
from __future__ import annotations

import re
from collections import Counter

import fitz

from .images import collect_pdf_images, collect_vector_figures, export_figures
from .models import Block, clean_text, rows_to_markdown

BOLD_FLAG = 1 << 4          # the "bold" bit in PyMuPDF's span flags
HEADER_ZONE = 0.10          # the top 10% of the page height is the header zone
# The footer band. Wider than a symmetric 10% because footers in banking
# documents usually run two or three lines (unit name, version number,
# confidentiality notice) and the top line of that block sits around 85% of the
# page height — put the mark at 90% and it is never recognised as a repeated
# line and flows straight into the content.
FOOTER_ZONE = 0.84
REPEAT_RATIO = 0.3          # on >=30% of pages counts as a header/footer
REPEAT_MIN_PAGES = 2        # short documents: repeating on 2 pages settles it
# A wider band reserved for the "repeats in exactly one spot" rule. Not every
# document's footer sits at the very bottom: some place the page number at 81%
# of the height, above any reasonable footer mark. In exchange, this rule
# demands the line repeat at *exactly one coordinate* across pages, so widening
# the band does not drag real content in with it.
REPEAT_ZONE = 0.75
# The largest coordinate spread (as a fraction of page height) between
# occurrences that still counts as "exactly one spot"
REPEAT_Y_SPREAD = 0.02

# Footnotes sit under the rule at the foot of the page, always in the bottom
# band and always at least one point smaller than the body — those two cues are
# enough to locate them.
FOOTNOTE_ZONE = 0.70        # only from 70% of the height down can be a footnote
FOOTNOTE_SIZE_GAP = 1.0     # this many points smaller than the body size
# A footnote reference mark is at least this many points smaller than the text
# around it
REF_MARK_GAP = 1.5
# Characters of context recorded so a reference mark can be located inside the
# extracted text of a table cell
REF_CONTEXT = 12

# A page with fewer characters than this is treated as having no text layer. The
# mark is above 0 because scans often carry a stamped page number or copyright
# line as real text.
SCAN_PAGE_CHARS = 40

# Footer lines common in Vietnamese administrative and banking documents
_BOILERPLATE = re.compile(
    r"^("
    r"(tài liệu|văn bản|thông tin)\s+(nội bộ|mật|lưu hành nội bộ)"
    r"|lưu hành nội bộ"
    r"|(bản|phiên bản|version)\s*[:\s]*[\d.]+"
    r"|(trang|page)\s*\d+\s*(/|của|of)?\s*\d*"
    r"|(mã|số)\s*(hiệu|văn bản|tài liệu)\s*[:.]"
    r"|(confidential|internal use only|all rights reserved)"
    r")",
    re.I,
)

# "9/34", "Trang 3", "- 5 -" … are page numbers, not content
_PAGE_NUM = re.compile(r"^(trang\s*)?[-–\s]*\d+\s*(/\s*\d+)?\s*[-–]*$", re.I)
# a TOC line, with dot leaders running to a page number
_DOT_LEADER = re.compile(r"\.{4,}\s*\d*\s*$")

# What a footnote reference span contains: an index number (occasionally several
# separated by commas) or one of the familiar symbols.
_REF_MARK = re.compile(r"^(\d{1,3}([,;]\s*\d{1,3})*|[*†‡§¶]{1,3})$")

# A line that opens a new item: a bullet, "a)", "(i)", "1.2.", "Điều 5"
_ITEM_START = re.compile(
    r"^([-–•▪]|\(?[a-zA-Z]\)|\(?[ivx]+\)|(Điều|PHẦN|Phần|Chương|Mục)\b"
    r"|\d+(\.\d+)*[.)]?\s)"
)
# The widest hanging indent still treated as one paragraph — beyond this, an
# indented line is a genuine sub-item, not the tail of the line above.
HANGING_INDENT_PT = 60.0

# PDF "flattening" tools (print-to-PDF, form removal, digital signing…) often
# rebuild each line of text by positioning every glyph individually, inserting a
# near-zero-width space glyph between letters. Nothing looks wrong on paper, but
# every tool that reads it back receives "B á n , x á c n h ậ n" — the sentence
# shatters into loose characters, tokenizers and the rebuild break with it, and
# the printed text comes out raggedly spaced.
#
# A real space pushes the next character a visible distance (0.2x the font size
# at the narrowest); a ghost space pushes nothing. PyMuPDF returns each glyph's
# bounding box from its advance, so two letters inside a word always sit flush —
# measuring the gap between *real* glyphs tells them apart, with no need to trust
# the space characters stored in the file.
GHOST_SPACE_RATIO = 0.12    # a gap narrower than this fraction of the font size is not a space

# Only accept a table when its frame is drawn with real rules. By default
# PyMuPDF also counts thin filled rectangles as rules, and if it still finds no
# table it falls back to guessing the frame from the whitespace between words.
# Both guesses collapse on a flattened file: such files are littered with thin
# rectangles, and the ghost spaces between individual letters open up countless
# phantom "columns". The result is a paragraph of prose sliced lengthwise into a
# ten-column table, with words broken across the cells.
_TABLE_STRATEGY = {"vertical_strategy": "lines_strict",
                   "horizontal_strategy": "lines_strict"}


def _rebuild_line(line: dict) -> None:
    """Rebuild each span's `text` in a line from the position of every glyph.

    The whitespace characters already in the file are discarded and spaces are
    reinserted from the measured gaps: a ghost space (one that pushes the next
    character nowhere) disappears, while a word break marked only by position —
    with no space glyph at all — still gets a space.

    This has to run over the whole line rather than span by span: accented
    letters usually take their glyphs from a different font, so PyMuPDF cuts the
    line into several spans, and a space often lands at the very end of one
    ("QUY " | "ĐỊNH"). Measured within a single span, that gap has nothing to be
    compared against and the two words run together.

    On a rotated line the horizontal axis says nothing, so the original string is
    kept as-is.
    """
    spans = line["spans"]
    if abs(line.get("dir", (1.0, 0.0))[1]) >= 0.01:
        for span in spans:
            span["text"] = "".join(c["c"] for c in span.get("chars") or [])
        return

    prev_end: float | None = None      # right edge of the nearest real glyph
    for span in spans:
        limit = GHOST_SPACE_RATIO * (span.get("size") or 0.0)
        out: list[str] = []
        for ch in span.get("chars") or []:
            if ch["c"].isspace():
                continue
            x0, _y0, x1, _y1 = ch["bbox"]
            if prev_end is not None and x0 - prev_end >= limit:
                out.append(" ")
            out.append(ch["c"])
            prev_end = x1
        span["text"] = "".join(out)


def _page_dict(page: fitz.Page) -> dict:
    """Like `get_text("dict")`, but each span's text is rebuilt from geometry."""
    data = page.get_text("rawdict")
    for block in data["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            _rebuild_line(line)
    return data


def _norm_key(s: str) -> str:
    """Normalise a line for header/footer matching across pages (digits dropped)."""
    return re.sub(r"\d+", "#", clean_text(s).lower())


def _dominant_size(spans: list[dict]) -> float:
    """The font size covering most of a line's characters."""
    counter: Counter[float] = Counter()
    for span in spans:
        text = span["text"].strip()
        if text:
            counter[round(span["size"], 1)] += len(text)
    return counter.most_common(1)[0][0] if counter else 0.0


def _is_ref_mark(span: dict, main: float) -> bool:
    """Is this span a footnote reference mark (a small superscript number)?"""
    return bool(main and span["size"] <= main - REF_MARK_GAP
                and _REF_MARK.match(span["text"].strip()))


def _starts_with_ref_mark(line: dict) -> bool:
    """Does the line open with a footnote reference mark?

    This is what identifies the *first* line of a footnote at the foot of a page,
    telling it apart from an ordinary passage that also happens to be set small.
    """
    main = _dominant_size(line["spans"])
    for span in line["spans"]:
        if span["text"].strip():
            return _is_ref_mark(span, main)
    return False


def _line_text(line: dict) -> str:
    """Join the spans of one line.

    A PDF exported from Word often splits the section number into its own span
    ("1." | "Phạm vi ..."), so the spans have to be joined before a heading can
    be recognised at all.

    Footnote reference marks are dropped entirely: such a mark is a small numeric
    span sitting mid-line, and joining it straight in glues it to the word
    ("…từng thời kỳ6."), leaving the RAG stack to read a number that is not in
    the sentence.
    """
    main = _dominant_size(line["spans"])
    parts = []
    prev_end = None
    after_ref = False
    for span in line["spans"]:
        t = span["text"]
        if not t:
            continue
        if _is_ref_mark(span, main):
            # act as if the reference mark was never there: the next span joins
            # straight onto the previous one, with no phantom space between
            prev_end = span["bbox"][2]
            after_ref = True
            continue
        if after_ref:
            t = t.lstrip()
            after_ref = False
        # a noticeable horizontal gap between two spans -> insert a space
        elif prev_end is not None and span["bbox"][0] - prev_end > 1.5:
            parts.append(" ")
        parts.append(t)
        prev_end = span["bbox"][2]
    return clean_text("".join(parts))


def _footnote_cutoff(blocks: list[Block], height: float,
                     body_size: float) -> float | None:
    """The y coordinate where the page's footnote block starts, None if it has none.

    A footnote is not the content of any section: it annotates some point in the
    body, set small under a horizontal rule at the foot of the page. Kept, a line
    like "1 Theo địa giới hành chính cũ…" looks exactly like a numbered heading,
    and the entire branch of content after it is hung underneath it by mistake.

    The scan runs upward from the bottom of the page, gathering the last
    unbroken run of small text. It only cuts when that run holds at least one
    line opening with a reference mark — an ordinary passage set small at the
    foot of a page has no such mark.
    """
    limit = body_size - FOOTNOTE_SIZE_GAP
    zone = height * FOOTNOTE_ZONE
    cutoff = None
    seen_mark = False
    for block in sorted(blocks, key=lambda b: b.meta.get("y", 0), reverse=True):
        y = block.meta.get("y", 0)
        if y < zone or not block.size or block.size > limit:
            break
        cutoff = y
        seen_mark = seen_mark or bool(block.meta.get("ref_start"))
    return cutoff if seen_mark else None


def _collect_repeats(doc: fitz.Document) -> set[str]:
    """Find the repeated header/footer lines so they can be stripped.

    Two conditions, both required: the line appears on most pages, **and** every
    time at exactly the same coordinate. The first alone is not enough — a
    frequently repeated cross-reference satisfies it too — but adding the second
    leaves only what was actually placed in the header/footer frame.
    """
    spots: dict[str, list[float]] = {}
    pages = doc.page_count
    for page in doc:
        h = page.rect.height
        seen: dict[str, float] = {}
        for block in _page_dict(page)["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                ratio = line["bbox"][1] / h if h else 0
                if HEADER_ZONE < ratio < REPEAT_ZONE:
                    continue
                t = _line_text(line)
                # Page numbers differ on every page, but `_norm_key` erases all
                # digits, so "1/5" and "2/5" both fold to "#/#" and are caught.
                if not t or (len(t) < 3 and not _PAGE_NUM.match(t)):
                    continue
                seen.setdefault(_norm_key(t), ratio)
        for key, ratio in seen.items():
            spots.setdefault(key, []).append(ratio)

    threshold = max(REPEAT_MIN_PAGES, int(pages * REPEAT_RATIO))
    return {key for key, ys in spots.items()
            if len(ys) >= threshold and max(ys) - min(ys) <= REPEAT_Y_SPREAD}


def is_boilerplate(text: str, ratio: float, repeats: set[str]) -> bool:
    """Is this line a header or footer? `ratio` is y0 divided by the page height.

    Lines repeating at exactly one spot are judged over the wide band; the
    familiar footer phrases and page numbers are judged only near the edges,
    because they also occur in the body (a "Trang 5" cell in a table of contents,
    for instance).
    """
    if (ratio <= HEADER_ZONE or ratio >= REPEAT_ZONE) and _norm_key(text) in repeats:
        return True
    if HEADER_ZONE < ratio < FOOTER_ZONE:
        return False
    return bool(_PAGE_NUM.match(text) or _BOILERPLATE.match(text))


_TOC_TITLE = re.compile(r"^\s*(mục lục|nội dung|table of contents|contents)\s*$", re.I)


def _toc_cutoff(blocks: list[Block]) -> float | None:
    """Find the y coordinate at which the table of contents starts on a page.

    Plenty of documents put the cover and the table of contents on one page.
    Dropping the whole page would also lose the product name and the approval
    document number on the cover, so the cut starts at the table of contents.
    """
    leaders = [b for b in blocks if _DOT_LEADER.search(b.text)]
    if len(leaders) < 4:
        return None
    start = min(b.meta.get("y", 0) for b in leaders)
    # if a "MỤC LỤC" heading sits right above the dot-leader block, cut from there
    for b in blocks:
        y = b.meta.get("y", 0)
        if _TOC_TITLE.match(b.text) and y <= start:
            return y
    return start


def _ref_mark_fixes(page: fitz.Page) -> list[tuple[str, str]]:
    """The (preceding text, footnote reference mark) pairs found on a page.

    PyMuPDF extracts tables straight to strings, with no span information left to
    recognise a reference mark, so a table cell keeps "…từng thời kỳ6.". Recording
    the few characters immediately before each mark during the span pass makes it
    possible to strip it from the cell text afterwards — without touching numbers
    that genuinely belong to the sentence.
    """
    fixes: list[tuple[str, str]] = []
    for block in _page_dict(page)["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            main = _dominant_size(line["spans"])
            before = ""
            for span in line["spans"]:
                text = span["text"]
                if not text:
                    continue
                if _is_ref_mark(span, main):
                    prefix = clean_text(before)[-REF_CONTEXT:]
                    if prefix:
                        fixes.append((prefix, text.strip()))
                    continue
                before += text
    return fixes


def _strip_ref_marks(text: str, fixes: list[tuple[str, str]]) -> str:
    for prefix, mark in fixes:
        text = text.replace(prefix + mark, prefix)
    return text


def _table_markdown(tbl, ref_fixes: list[tuple[str, str]]) -> str:
    try:
        rows = tbl.extract()
    except Exception:
        return ""
    clean_rows = [
        [_strip_ref_marks(clean_text(c or "").replace("\n", " "), ref_fixes)
         .replace("|", "\\|")
         for c in row]
        for row in rows
    ]
    return rows_to_markdown(clean_rows)


def _body_size(doc: fitz.Document) -> float:
    """The most common font size — the baseline for spotting large headings."""
    counter: Counter[float] = Counter()
    for page in doc:
        for block in _page_dict(page)["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    if span["text"].strip():
                        counter[round(span["size"], 1)] += len(span["text"])
    return counter.most_common(1)[0][0] if counter else 12.0


def extract(path: str, figure_dir: str | None = None, doc_id: str = "doc",
            stats: dict | None = None) -> tuple[list[Block], str]:
    doc = fitz.open(path)
    repeats = _collect_repeats(doc)
    body_size = _body_size(doc)

    # Logos are dropped outright; figures get a descriptive placeholder line
    images = collect_pdf_images(doc)
    # A vector flowchart is a figure too, even with no image embedded in the file
    for page in doc:
        for figure in collect_vector_figures(page):
            images.setdefault(figure.page, []).append(figure)
    if figure_dir:
        export_figures(doc, images, figure_dir, doc_id)
    if stats is not None:
        flat = [i for items in images.values() for i in items]
        stats["logos_dropped"] = sum(1 for i in flat if i.kind == "logo")
        stats["figures_found"] = sum(1 for i in flat if i.kind == "figure")
        stats["diagrams_found"] = sum(1 for i in flat if i.meta.get("vector"))
        stats["boilerplate_lines_dropped"] = len(repeats)
        # A scan: every page is one photograph with no text layer. Text, logo,
        # header and footer are all pixels inside a single image — there is no
        # separate object to strip, and stripping that image leaves a blank page.
        # Counted here so the user can be told to run OCR first.
        stats["pages_total"] = doc.page_count
        stats["pages_without_text"] = sum(
            1 for page in doc if len(page.get_text("text").strip()) < SCAN_PAGE_CHARS)

    blocks: list[Block] = []
    footnote_lines = 0

    for pno, page in enumerate(doc, start=1):
        height = page.rect.height
        ref_fixes = _ref_mark_fixes(page)

        # Text inside a diagram labels that diagram's boxes; it is not content to
        # be read line by line. Pulling it out yields a meaningless string and
        # loses the diagram itself — keeping it as an image is the right answer.
        #
        # The text is only dropped once the image has been exported: with no
        # image to stand in its place, jumbled text beats losing the block
        # entirely.
        diagrams = [fitz.Rect(i.bbox) for i in images.get(pno, [])
                    if i.meta.get("vector") and i.file]

        # Table areas are handled separately; lines inside them are skipped
        table_boxes: list[fitz.Rect] = []
        table_blocks: list[Block] = []
        try:
            for tbl in page.find_tables(**_TABLE_STRATEGY):
                md = _table_markdown(tbl, ref_fixes)
                if md:
                    rect = fitz.Rect(tbl.bbox)
                    table_boxes.append(rect)
                    # A flowchart has a ruled frame, so `find_tables` mistakes it
                    # for one enormous single-cell table.
                    if any(rect.get_area() and (rect & d).get_area()
                           > 0.6 * rect.get_area() for d in diagrams):
                        continue
                    table_blocks.append(Block(
                        text=md, page=pno, kind="table", is_table=True,
                        meta={"y": rect.y0},
                    ))
        except Exception:
            pass

        candidates: list[Block] = []

        for block in _page_dict(page)["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                text = _line_text(line)
                if not text:
                    continue

                x0, y0, x1, y1 = line["bbox"]
                ratio = y0 / height if height else 0
                if is_boilerplate(text, ratio, repeats):
                    continue
                in_margin = ratio <= HEADER_ZONE or ratio >= FOOTER_ZONE
                # a page number buried in a footer line, e.g. "Quy định sản phẩm 3/8"
                if in_margin and len(text) < 90 and _PAGE_NUM.search(text.split()[-1] if text.split() else ""):
                    stripped = _norm_key(re.sub(r"\s*\d+\s*/\s*\d+\s*$", "", text))
                    if stripped in repeats:
                        continue
                mid = fitz.Point((x0 + x1) / 2, (y0 + y1) / 2)
                if any(mid in r for r in table_boxes):
                    continue
                if any(mid in r for r in diagrams):
                    continue        # a label inside a diagram, already in the image

                spans = [s for s in line["spans"] if s["text"].strip()]
                if not spans:
                    continue
                size = max(s["size"] for s in spans)
                # treat the line as bold when most of its characters are bold
                bold_chars = sum(len(s["text"]) for s in spans if s["flags"] & BOLD_FLAG)
                total_chars = sum(len(s["text"]) for s in spans)
                bold = total_chars > 0 and bold_chars / total_chars >= 0.6

                candidates.append(Block(
                    text=text, page=pno, kind="para", bold=bold, size=round(size, 1),
                    meta={"x0": round(x0, 1), "y": round(y0, 1), "body_size": body_size,
                          "ref_start": _starts_with_ref_mark(line)},
                ))

        # Cut the footnote block off the foot of the page before joining lines:
        # left in, the footnote lines join into one long phantom paragraph.
        note_at = _footnote_cutoff(candidates, height, body_size)
        if note_at is not None:
            kept = [b for b in candidates if b.meta.get("y", 0) < note_at]
            footnote_lines += len(candidates) - len(kept)
            candidates = kept

        # Cut the table of contents but keep the content above it on that page
        cutoff = _toc_cutoff(candidates)
        if cutoff is not None:
            candidates = [b for b in candidates if b.meta.get("y", 0) < cutoff]
        candidates = [b for b in candidates if not _DOT_LEADER.search(b.text)]

        merged = _merge_wrapped(_join_same_row(candidates))
        merged.extend(table_blocks)

        # Reserve a figure's place at exactly its position in the flow of the
        # content, so a chunk knows the section carries a figure without swallowing
        # a logo into its text.
        for img in images.get(pno, []):
            if img.kind != "figure":
                continue
            merged.append(Block(
                text=img.placeholder(), page=pno, kind="figure",
                meta={
                    "y": img.y, "x0": img.bbox[0],
                    "figure": True,
                    "caption": img.caption,
                    "width": round(img.width),
                    "height": round(img.height),
                    "file": img.file,
                },
            ))
        merged.sort(key=lambda b: (b.meta.get("y", 0) if not b.is_table else b.meta.get("y", 0)))
        blocks.extend(merged)

    if stats is not None:
        stats["footnote_lines_dropped"] = footnote_lines

    doc.close()
    return blocks, "actual"


def _join_same_row(lines: list[Block]) -> list[Block]:
    """Merge fragments that sit on the same horizontal row.

    In a PDF the item marker ("a.", "(i)", "-") is usually placed in its own
    margin column, so PyMuPDF splits it into a separate line. Without merging,
    the result is a junk block holding only "a." while the content loses its
    marker.
    """
    rows: list[list[Block]] = []
    for blk in sorted(lines, key=lambda b: (b.meta.get("y", 0), b.meta.get("x0", 0))):
        y = blk.meta.get("y", 0)
        if rows and abs(rows[-1][0].meta.get("y", 0) - y) <= 2.5:
            rows[-1].append(blk)
        else:
            rows.append([blk])

    out: list[Block] = []
    for row in rows:
        if len(row) == 1:
            out.append(row[0])
            continue
        row.sort(key=lambda b: b.meta.get("x0", 0))
        head = row[0]
        head.text = clean_text(" ".join(b.text for b in row))
        head.bold = any(b.bold for b in row)
        head.size = max(b.size for b in row)
        # keep the content's left edge so the later line-joining step aligns right
        body = next((b for b in row[1:] if len(b.text) > 4), None)
        if body is not None:
            head.meta["x0"] = body.meta.get("x0", head.meta.get("x0"))
        out.append(head)
    return out


def _merge_wrapped(lines: list[Block]) -> list[Block]:
    """Join lines wrapped by the margin back into complete paragraphs.

    A PDF stores rendered lines, not paragraphs. Without joining, every line
    becomes a separate block and sentences are cut mid-way during chunking.
    """
    out: list[Block] = []
    for blk in lines:
        if not out:
            out.append(blk)
            continue
        prev = out[-1]
        same_style = prev.bold == blk.bold and abs(prev.size - blk.size) < 0.6
        indent = blk.meta.get("x0", 0) - prev.meta.get("x0", 0)
        aligned = abs(indent) < 12
        # A numbered paragraph uses a hanging indent: from the second line on it
        # is indented by exactly the width of the number ("15.10. " ~ 32pt).
        # Demanding that two lines share a left edge means a two-line heading is
        # never joined, and its second half drops down as a stunted paragraph.
        hanging = 0 < indent <= HANGING_INDENT_PT and bool(_ITEM_START.match(prev.text))
        gap = blk.meta.get("y", 0) - prev.meta.get("y", 0)
        # the previous line has not finished its sentence and this one opens no item
        unfinished = not re.search(r"[.:;!?]$", prev.text)
        starts_new = bool(_ITEM_START.match(blk.text))
        near = 0 < gap < prev.size * 2.2

        # A word split by a hyphen at the end of a line ("…và Dai-" / "ichi Life
        # Việt Nam…"). These must join even when the two lines differ in style:
        # an item's first line usually sets the name in bold and the next does
        # not, so the same-style condition below never holds and the word stays
        # broken in half.
        if (near and not starts_new
                and re.search(r"\w-$", prev.text) and blk.text[:1].islower()):
            prev.text = clean_text(prev.text + blk.text)
            continue

        if same_style and (aligned or hanging) and near and unfinished and not starts_new:
            prev.text = clean_text(prev.text + " " + blk.text)
            continue
        out.append(blk)
    return out
