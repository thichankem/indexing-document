"""Kiểm tra chất lượng chunk trước khi đưa vào embedding."""
from __future__ import annotations

import statistics

import re

from .chunker import ChunkConfig
from .layout import MAX_TREE_DEPTH
from .models import Chunk, Section

# Mẩu chữ cụt đầu dòng ("h hàng là...") báo hiệu file nguồn rơi ký tự khi tạo
# PDF. Chỉ xét mẩu một chữ cái, vì mẩu hai chữ cái trùng với rất nhiều từ tiếng
# Việt hợp lệ ("là", "và", "có"…) và sẽ báo động giả hàng loạt.
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


# Từ tỉ lệ này trở lên số trang không có lớp text thì tài liệu là bản scan
SCAN_PAGE_RATIO = 0.8


def scanned_warning(extract_stats: dict | None) -> str:
    """Câu cảnh báo khi tài liệu là bản scan, chuỗi rỗng nếu không phải.

    Trang scan là một tấm ảnh chụp cả trang: chữ, logo, đầu/chân trang, mục lục
    đều là điểm ảnh nằm chung trong đúng một tấm hình. Không có đối tượng riêng
    nào để gỡ, mà gỡ tấm hình ấy đi thì trang trắng trơn — nên tool không làm
    được gì cho loại file này ngoài việc nói thẳng ra.
    """
    stats = extract_stats or {}
    total = stats.get("pages_total", 0)
    blank = stats.get("pages_without_text", 0)
    if not total or blank < total * SCAN_PAGE_RATIO:
        return ""
    return (
        f"{blank}/{total} trang không có lớp text — đây là bản scan. Chữ và "
        "logo nằm chung trong một tấm ảnh chụp cả trang nên không gỡ riêng "
        "logo được, chunk cũng chỉ có dòng giữ chỗ [HÌNH]. Chạy OCR (hoặc lấy "
        "bản .docx/.pdf gốc) rồi đưa lại vào tool."
    )


def outline(sections: list[Section]) -> list[dict]:
    """Cây chỉ mục của tài liệu, để đối chiếu với văn bản gốc.

    Hệ RAG dựng title của chunk từ cây này, nên sai một cấp ở đây là sai title
    của mọi chunk bên dưới. Xuất ra thành danh sách phẳng kèm cấp để người dùng
    soát nhanh bằng mắt.
    """
    return [
        {"level": s.level, "number": s.number, "title": s.title,
         "page": s.page_start}
        for s in sections if not s.is_preamble
    ]


def format_outline(sections: list[Section], doc_title: str = "") -> str:
    """In cây chỉ mục ra dạng thụt lề để đọc bằng mắt."""
    lines = [f"[tài liệu] {doc_title}"] if doc_title else []
    for node in outline(sections):
        indent = "   " * node["level"]
        number = f"{node['number']} " if node["number"] else ""
        lines.append(f"{indent}L{node['level']} {number}{node['title']}")
    return "\n".join(lines)


def check(chunks: list[Chunk], sections: list[Section], cfg: ChunkConfig,
          extract_stats: dict | None = None) -> dict:
    """Soát lỗi thường gặp và trả về báo cáo tóm tắt cho từng tài liệu."""
    warnings: list[str] = []
    lengths = [c.char_count for c in chunks]

    no_section = [c for c in chunks if not c.section_path or c.section_path == "Phần mở đầu"]
    # Trần token là ràng buộc cứng của khâu embedding: vượt là bị cắt cụt, nên
    # không nới thêm dung sai như khi đo bằng ký tự.
    too_long = [c for c in chunks if c.est_tokens > cfg.max_tokens]
    too_short = [c for c in chunks if c.est_tokens < 30]
    cross_page = [c for c in chunks if c.page <= 0]
    multi_part = {c.section_path for c in chunks if c.part_total > 1}

    for chunk in chunks:
        if chunk.est_tokens > cfg.max_tokens:
            chunk.warnings.append("vượt trần token")
        if chunk.est_tokens < 30:
            chunk.warnings.append("quá ngắn, ngữ nghĩa yếu")
        if not chunk.section_number and chunk.section_path == "Phần mở đầu":
            chunk.warnings.append("không thuộc mục nào")

    scanned = scanned_warning(extract_stats)
    if scanned:
        warnings.append(scanned)
    if not chunks:
        warnings.append(
            "Không tạo được chunk nào — file rỗng, hoặc là bản scan chưa có "
            "lớp text (cần chạy OCR trước khi đưa vào tool)"
        )
    if len(no_section) > len(chunks) * 0.3 and chunks:
        warnings.append(
            f"{len(no_section)}/{len(chunks)} chunk không gắn được vào mục nào "
            "— nhiều khả năng tiêu đề bị nhận sai"
        )
    if too_long:
        warnings.append(
            f"{len(too_long)} chunk vượt trần {cfg.max_tokens} token — phía "
            "embedding sẽ cắt cụt phần thừa"
        )

    # Tên mục mở đầu bằng chữ thường là dấu hiệu file nguồn rơi mất mấy ký tự
    # đầu dòng ("Phương thức trả nợ" -> "ng thức trả nợ"). Tiêu đề tiếng Việt
    # luôn viết hoa chữ đầu nên tín hiệu này hầu như không báo nhầm, mà mắt
    # thường thì rất dễ bỏ qua giữa một cây chỉ mục dài.
    lost_head = [s for s in sections
                 if s.title[:1].isalpha() and s.title[:1].islower()]
    if lost_head:
        warnings.append(
            f"{len(lost_head)} tên mục mở đầu bằng chữ thường "
            f"(vd \"{lost_head[0].number} {lost_head[0].title[:40]}\") — file "
            "nguồn rơi ký tự đầu dòng, nên lấy lại bản gốc (.docx) nếu có"
        )

    truncated = _suspect_truncated(chunks)
    if truncated:
        warnings.append(
            f"{truncated} dòng nghi bị rơi ký tự đầu — lỗi từ file nguồn khi "
            "tạo PDF, nên lấy lại bản gốc (.docx) nếu có"
        )

    pages = {c.page for c in chunks}
    heading_count = len([s for s in sections if s.number])
    figures = sum(len(c.figures) for c in chunks)
    tokens = [c.est_tokens for c in chunks]

    tree = outline(sections)
    depth = max((n["level"] for n in tree), default=0)
    if depth > MAX_TREE_DEPTH:
        warnings.append(
            f"cây chỉ mục sâu {depth} cấp, quá {MAX_TREE_DEPTH} cấp hệ RAG đọc "
            "được — các mục sâu nhất sẽ bị gộp vào cấp cuối"
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
        f"  chunk={stats['chunks']:<5} mục={stats['sections']:<4} "
        f"tiêu đề={stats['headings_detected']:<4} trang={stats['pages_covered']}"
        f" | cây chỉ mục sâu {stats['outline_depth']} cấp",
        f"  token: trung vị={stats['tokens_median']} max={stats['tokens_max']}"
        f"/{stats['token_limit']} | ký tự: trung vị={stats['chars_median']} "
        f"max={stats['chars_max']}",
        f"  mục trải nhiều chunk={stats['chunks_multi_page_sections']} "
        f"| quá dài={stats['too_long']} quá ngắn={stats['too_short']} "
        f"không thuộc mục={stats['orphan']}",
        f"  đã lọc: logo={stats['logos_dropped']} "
        f"dòng chân/đầu trang={stats['boilerplate_lines_dropped']} "
        f"cước chú={stats['footnote_lines_dropped']} "
        f"| giữ lại hình={stats['figures_kept']}"
        + (f" (trong đó {stats['diagrams_found']} lưu đồ)"
           if stats.get("diagrams_found") else ""),
    ]
    for w in stats["warnings"]:
        lines.append(f"  [!] {w}")
    return "\n".join(lines)
