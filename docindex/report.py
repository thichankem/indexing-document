"""Quality checks run on the chunks before they reach the embedding step."""
from __future__ import annotations

import statistics

import re

from .chunker import ChunkConfig
from .layout import MAX_TREE_DEPTH
from .models import PREAMBLE_TITLE, Chunk, Section

# A clipped fragment at the start of a line ("h hàng là...") means the source
# file dropped characters while the PDF was produced. Only one-letter fragments
# are considered: two-letter fragments collide with plenty of valid Vietnamese
# words ("là", "và", "có"…) and would raise false alarms by the hundred.
_ONE_LETTER_WORDS = {"ở", "à", "ừ", "ê", "ơ", "y", "a", "ý"}
_TRUNCATED = re.compile(r"^([^\W\d_])\s+([^\W\d_]{2,})", re.U)


def _suspect_truncated(chunks: list[Chunk]) -> int:
    count = 0
    for chunk in chunks:
        for line in chunk.raw_text.split("\n"):
            for cell in (line.split("|") if line.strip().startswith("|") else [line]):
                cell = cell.strip()
                if len(cell) <= 25:
                    continue
                m = _TRUNCATED.match(cell)
                if m and m.group(1).lower() not in _ONE_LETTER_WORDS:
                    count += 1
    return count


# At or above this share of pages without a text layer, the file is a scan
SCAN_PAGE_RATIO = 0.8


def scanned_warning(extract_stats: dict | None) -> str:
    """Warning text for a scanned document; an empty string when it is not one.

    A scanned page is one photograph of the whole sheet: text, logo, header,
    footer and table of contents are all pixels inside a single image. There is
    no separate object to strip, and stripping the image leaves a blank page —
    so the only useful thing the tool can do here is say so plainly.
    """
    stats = extract_stats or {}
    total = stats.get("pages_total", 0)
    blank = stats.get("pages_without_text", 0)
    if not total or blank < total * SCAN_PAGE_RATIO:
        return ""
    return (
        f"{blank}/{total} pages have no text layer — this is a scan. Text and "
        "logo share a single full-page image, so the logo cannot be stripped "
        "on its own and the chunks hold nothing but [FIGURE] placeholders. Run "
        "OCR (or fetch the original .docx/.pdf) and feed it back in."
    )


def outline(sections: list[Section]) -> list[dict]:
    """The document's outline tree, for checking against the original text.

    A RAG stack builds every chunk title from this tree, so one wrong level
    here is a wrong title on every chunk below it. Returned as a flat list with
    levels attached, so it can be eyeballed quickly.
    """
    return [
        {"level": s.level, "number": s.number, "title": s.title,
         "page": s.page_start}
        for s in sections if not s.is_preamble
    ]


def format_outline(sections: list[Section], doc_title: str = "") -> str:
    """Render the outline tree as indented text for human review."""
    lines = [f"[document] {doc_title}"] if doc_title else []
    for node in outline(sections):
        indent = "   " * node["level"]
        number = f"{node['number']} " if node["number"] else ""
        lines.append(f"{indent}L{node['level']} {number}{node['title']}")
    return "\n".join(lines)


def check(chunks: list[Chunk], sections: list[Section], cfg: ChunkConfig,
          extract_stats: dict | None = None) -> dict:
    """Check for the usual problems and return a per-document summary report."""
    warnings: list[str] = []
    lengths = [c.char_count for c in chunks]

    no_section = [c for c in chunks
                  if not c.section_path or c.section_path == PREAMBLE_TITLE]
    # The token ceiling is a hard constraint of the embedding step: go over it
    # and the text is truncated, so no tolerance is added the way it is for
    # character counts.
    too_long = [c for c in chunks if c.est_tokens > cfg.max_tokens]
    too_short = [c for c in chunks if c.est_tokens < 30]
    cross_page = [c for c in chunks if c.page <= 0]
    multi_part = {c.section_path for c in chunks if c.part_total > 1}

    for chunk in chunks:
        if chunk.est_tokens > cfg.max_tokens:
            chunk.warnings.append("over the token ceiling")
        if chunk.est_tokens < 30:
            chunk.warnings.append("too short, weak semantics")
        if not chunk.section_number and chunk.section_path == PREAMBLE_TITLE:
            chunk.warnings.append("belongs to no section")

    scanned = scanned_warning(extract_stats)
    if scanned:
        warnings.append(scanned)
    if not chunks:
        warnings.append(
            "No chunks were produced — the file is empty, or it is a scan with "
            "no text layer (run OCR before feeding it to the tool)"
        )
    if len(no_section) > len(chunks) * 0.3 and chunks:
        warnings.append(
            f"{len(no_section)}/{len(chunks)} chunks could not be attached to a "
            "section — headings were most likely detected wrongly"
        )
    if too_long:
        warnings.append(
            f"{len(too_long)} chunks exceed the {cfg.max_tokens} token ceiling "
            "— the embedding step will truncate the overflow"
        )

    # A section name starting in lowercase means the source file lost a few
    # characters at the start of the line ("Phương thức trả nợ" -> "ng thức trả
    # nợ"). Vietnamese headings always capitalise the first letter, so this
    # signal almost never misfires — and the eye misses it easily in a long
    # outline.
    lost_head = [s for s in sections
                 if s.title[:1].isalpha() and s.title[:1].islower()]
    if lost_head:
        warnings.append(
            f"{len(lost_head)} section names start in lowercase "
            f"(e.g. \"{lost_head[0].number} {lost_head[0].title[:40]}\") — the "
            "source file lost leading characters; fetch the original .docx if "
            "you still have it"
        )

    truncated = _suspect_truncated(chunks)
    if truncated:
        warnings.append(
            f"{truncated} lines look like they lost leading characters — a fault "
            "in the source file when the PDF was produced; fetch the original "
            ".docx if you still have it"
        )

    pages = {c.page for c in chunks}
    heading_count = len([s for s in sections if s.number])
    figures = sum(len(c.figures) for c in chunks)
    tokens = [c.est_tokens for c in chunks]

    tree = outline(sections)
    depth = max((n["level"] for n in tree), default=0)
    if depth > MAX_TREE_DEPTH:
        warnings.append(
            f"the outline is {depth} levels deep, past the {MAX_TREE_DEPTH} "
            "levels a RAG stack reads — the deepest sections are folded into "
            "the last level"
        )

    return {
        "chunks": len(chunks),
        "sections": len(sections),
        "headings_detected": heading_count,
        "outline_depth": depth,
        "outline": tree,
        "pages_covered": len(pages),
        "chunks_multi_page_sections": len(multi_part),
        "chars_min": min(lengths) if lengths else 0,
        "chars_max": max(lengths) if lengths else 0,
        "chars_mean": round(statistics.mean(lengths)) if lengths else 0,
        "chars_median": round(statistics.median(lengths)) if lengths else 0,
        "token_limit": cfg.max_tokens,
        "tokens_mean": round(statistics.mean(tokens)) if tokens else 0,
        "tokens_median": round(statistics.median(tokens)) if tokens else 0,
        "tokens_max": max(tokens) if tokens else 0,
        "pages_total": (extract_stats or {}).get("pages_total", 0),
        "pages_without_text": (extract_stats or {}).get("pages_without_text", 0),
        "suspect_truncated_lines": truncated,
        "suspect_truncated_headings": len(lost_head),
        "figures_kept": figures,
        "diagrams_found": (extract_stats or {}).get("diagrams_found", 0),
        "logos_dropped": (extract_stats or {}).get("logos_dropped", 0),
        "boilerplate_lines_dropped": (extract_stats or {}).get("boilerplate_lines_dropped", 0),
        "footnote_lines_dropped": (extract_stats or {}).get("footnote_lines_dropped", 0),
        "too_long": len(too_long),
        "too_short": len(too_short),
        "orphan": len(no_section),
        "invalid_page": len(cross_page),
        "warnings": warnings,
    }


def format_report(name: str, stats: dict) -> str:
    lines = [
        f"  chunks={stats['chunks']:<5} sections={stats['sections']:<4} "
        f"headings={stats['headings_detected']:<4} pages={stats['pages_covered']}"
        f" | outline depth {stats['outline_depth']}",
        f"  tokens: median={stats['tokens_median']} max={stats['tokens_max']}"
        f"/{stats['token_limit']} | chars: median={stats['chars_median']} "
        f"max={stats['chars_max']}",
        f"  sections split across chunks={stats['chunks_multi_page_sections']} "
        f"| too long={stats['too_long']} too short={stats['too_short']} "
        f"orphan={stats['orphan']}",
        f"  filtered: logos={stats['logos_dropped']} "
        f"header/footer lines={stats['boilerplate_lines_dropped']} "
        f"footnotes={stats['footnote_lines_dropped']} "
        f"| figures kept={stats['figures_kept']}"
        + (f" (of which {stats['diagrams_found']} diagrams)"
           if stats.get("diagrams_found") else ""),
    ]
    for w in stats["warnings"]:
        lines.append(f"  [!] {w}")
    return "\n".join(lines)
