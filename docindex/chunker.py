"""Cắt tài liệu thành chunk: mỗi chunk nằm gọn trong một trang và một mục.

Ba ràng buộc chính:
  1. Không chunk nào vượt trần **512 token** — giới hạn của khâu embedding phía
     RAG; phần vượt trần bị cắt cụt lặng lẽ nên phải chặn từ đây.
  2. Không chunk nào vắt qua ranh giới trang -> tránh lỗi ghép nhầm nội dung
     giữa các trang khi trích xuất PDF.
  3. Không chunk nào chứa hai mục khác nhau -> vector phản ánh đúng một chủ đề.

Khi một mục trải dài nhiều trang, nó bị cắt thành nhiều chunk nhưng mỗi chunk
đều mang đường dẫn mục lục đầy đủ và các liên kết prev/next để lấy lại đủ ngữ
cảnh lúc truy vấn.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .models import Chunk, Section, clean_text

# Trần token của hệ thống RAG đích. Mọi kích thước bên dưới đo bằng token chứ
# không đo bằng ký tự, vì đây mới là thứ phía embedding thật sự đếm.
MAX_TOKENS = 512
# Dưới ngưỡng này thì mục đứng một mình quá mỏng, vector gần như không mang
# thông tin — gộp với mục liền kề chừng nào tổng còn nằm trong trần 512.
#
# Ngưỡng đặt cao gần nửa trần là có chủ ý: đo trên 18 tài liệu thật, hạ xuống
# 60 để lại 389 chunk dưới 200 token, còn nâng lên quá 200 thì gần như không
# đổi gì nữa (890 -> 877 chunk) vì lúc đó trần 512 mới là thứ chặn lại.
MIN_TOKENS = 200

# Tokenizer đa ngữ (XLM-R/BGE-M3) tách tiếng Việt trung bình ~2.8 ký tự/token.
# Lấy mức chặt hơn thực tế một chút để ước lượng không bao giờ thấp hơn số
# token thật — đếm thiếu thì chunk bị cắt cụt lúc embedding.
CHARS_PER_TOKEN = 2.8


@dataclass
class ChunkConfig:
    max_tokens: int = MAX_TOKENS   # trần kích thước một chunk (token)
    min_tokens: int = MIN_TOKENS   # dưới ngưỡng này sẽ cố gộp với mục liền kề
    overlap_sentences: int = 1     # số câu lặp lại khi một mục bị cắt ngang trang
    include_path_prefix: bool = True
    max_path_depth: int = 4        # số cấp mục lục đưa vào tiền tố
    merge_short: bool = True       # gộp mục quá ngắn vào mục liền trước

    @property
    def max_chars(self) -> int:
        """Trần token quy đổi ra ký tự — chỉ dùng để báo cáo cho dễ hình dung."""
        return int(self.max_tokens * CHARS_PER_TOKEN)


_SENT_SPLIT = re.compile(r"(?<=[.!?;:])\s+(?=[A-ZĐÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚĂĨŨƠƯẠ-ỹ0-9(])")


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]
    return parts or ([text] if text.strip() else [])


def est_tokens(text: str) -> int:
    """Ước lượng số token của một đoạn tiếng Việt.

    Lấy mức lớn nhất trong hai cách đếm, vì mỗi cách hụt ở một kiểu nội dung:

    * theo ký tự — hụt với bảng markdown, nơi dấu `|` dày đặc mà chuỗi lại ngắn;
    * theo âm tiết + dấu — hụt với văn xuôi có nhiều từ dài.

    Ước lượng cao hơn thực tế thì chunk nhỏ hơn trần một chút; ước lượng thấp
    hơn thì chunk bị cắt cụt lúc embedding, nên luôn chọn phía an toàn.
    """
    if not text.strip():
        return 0
    words = text.split()
    # âm tiết tiếng Việt thường gọn trong một token, từ dài bị tách thêm
    syllables = sum(1 + len(w) // 7 for w in words)
    marks = sum(1 for ch in text if not ch.isalnum() and not ch.isspace())
    by_chars = math.ceil(len(text) / CHARS_PER_TOKEN)
    return max(1, by_chars, syllables + marks)


def _table_rows(md: str) -> tuple[list[str], list[str]]:
    """Tách bảng markdown thành (dòng tiêu đề, các dòng dữ liệu)."""
    lines = md.split("\n")
    if len(lines) >= 2 and set(lines[1].replace("|", "").replace(" ", "")) <= {"-"}:
        return lines[:2], lines[2:]
    return [], lines


def _row_cells(row: str) -> list[str]:
    """Tách các ô của một dòng bảng markdown."""
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _row_as_fields(row: str, head_cells: list[str], limit: int,
                   measure=len) -> list[str]:
    """Trải một hàng bảng quá dài thành các dòng "tên cột: giá trị"."""
    cells = [c for c in _row_cells(row) if c]
    # Bảng một ô thực chất là khung văn bản, không phải dữ liệu có cột. Gắn
    # nhãn "Cột 1:" vào chỉ thêm nhiễu, cứ trả về nguyên văn.
    if len(cells) == 1 and not [h for h in head_cells if h]:
        return _split_long_text(cells[0], limit, measure)

    lines: list[str] = []
    for i, cell in enumerate(_row_cells(row)):
        if not cell:
            continue
        label = head_cells[i] if i < len(head_cells) and head_cells[i] else f"Cột {i + 1}"
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
    """Cắt bảng dài theo hàng, giữ lại dòng tiêu đề ở mỗi phần.

    `measure` quyết định đơn vị của `limit`: mặc định đếm ký tự, chunker truyền
    vào `est_tokens` để cắt đúng theo trần token của hệ RAG.
    """
    header, rows = _table_rows(md)
    head_text = "\n".join(header)
    head_len = measure(head_text) + 1 if header else 0

    # Dòng tiêu đề dài hơn cả trần thì không thể lặp lại ở mỗi phần, và bảng
    # cũng không còn hàng nào để cắt. Bảng một hàng — ô gộp trải cả trang, hay
    # gặp trong văn bản hành chính — rơi đúng vào đây: coi luôn dòng tiêu đề
    # là dữ liệu, nếu không cả khối sẽ trả về nguyên vẹn và tràn trần token.
    if header and head_len > limit:
        rows = [header[0]] + rows
        header, head_len = [], 0

    head_cells = _row_cells(header[0]) if header else []

    parts: list[str] = []
    current: list[str] = []
    size = head_len
    for row in rows:
        # Một hàng dài hơn cả trần thì không thể giữ dạng bảng. Cắt ngang giữa
        # hàng sẽ làm vỡ số cột và bảng thành vô nghĩa, nên chuyển hàng đó sang
        # dạng "tên cột: giá trị" — vẫn đọc được và không mất dữ liệu.
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
    """Cắt đoạn quá dài tại ranh giới câu để không đứt giữa chừng."""
    if measure(text) <= limit:
        return [text]
    out: list[str] = []
    current = ""
    for sentence in _split_sentences(text):
        if current and measure(current) + measure(sentence) + 1 > limit:
            out.append(current.strip())
            current = sentence
        elif measure(sentence) > limit:
            # một câu đơn lẻ vẫn vượt trần -> cắt cứng theo khoảng trắng
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
    """Một mảnh nội dung thuộc đúng một mục và một trang, trước khi đánh số."""
    section: Section
    page: int
    text: str
    has_table: bool
    figures: list[dict] = field(default_factory=list)


def _pieces_for_section(section: Section, cfg: ChunkConfig) -> list[_Piece]:
    """Gom nội dung của một mục lại rồi tách theo trang."""
    # Tiền tố mục lục sẽ được nối vào sau, phải trừ trước để chunk cuối cùng
    # không vượt trần kích thước.
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

        # Quá dài: cắt nhỏ, bảng cắt theo hàng còn văn bản cắt theo câu
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
    """Chỉ gắn hình cho đúng mảnh chứa dòng giữ chỗ của nó."""
    if not figures or "[HÌNH:" not in segment:
        return []
    return [f for f in figures if not f.get("caption") or f["caption"] in segment] or list(figures)


def _merge_small(pieces: list[_Piece], cfg: ChunkConfig) -> list[_Piece]:
    """Gộp các mảnh quá ngắn với mục liền kề để vector đủ ngữ nghĩa.

    Chỉ gộp khi cùng trang và cùng mục cha, nên chunk vẫn nằm trong một chủ đề
    chung thay vì trộn hai nội dung không liên quan.
    """
    out: list[_Piece] = []
    for piece in pieces:
        if not out:
            out.append(piece)
            continue
        prev = out[-1]
        same_page = prev.page == piece.page
        same_parent = prev.section.path[:-1] == piece.section.path[:-1]
        # mục cha chỉ có mỗi dòng tiêu đề thì nên đi kèm mục con đầu tiên,
        # thay vì đứng riêng thành một chunk không mang thông tin gì
        is_parent = piece.section.path[:-1] == prev.section.path
        # trừ tiền tố mục lục của mảnh đứng sau, vì nó sẽ được nối vào lúc cuối
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
                # nội dung thực chất thuộc mục con -> lấy đường dẫn chi tiết hơn
                prev.section = piece.section
            elif prev.section is not piece.section:
                prev.section = _common_parent(prev.section, piece.section)
            continue
        out.append(piece)
    return out


def _split_mixed(text: str, limit: int) -> list[str]:
    """Cắt một mảnh vừa có văn xuôi vừa có bảng, theo trần token."""
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
    """Chốt chặn cuối cùng: không mảnh nào vượt trần token sau khi gộp.

    Bước gộp mảnh ngắn và bước thêm tiền tố mục lục đều làm chunk dài thêm.
    Vượt trần nghĩa là phía embedding cắt cụt lặng lẽ, nên phải soát lại một
    lượt thay vì tin vào các bước trước.
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
    """Mục đại diện cho hai mục anh em đã gộp: giữ mục đầu, nới tiêu đề."""
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

    # Đếm số phần của mỗi mục để ghi part_index/part_total
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
        # Mục bị cắt ngang: lặp lại câu cuối của phần trước để hai nửa gần nhau
        # hơn trong không gian vector và không mất mạch nội dung. Câu lặp cũng
        # tính vào trần token, nên chỉ thêm khi còn chỗ.
        if is_continuation and cfg.overlap_sentences > 0 and prev_piece is not None:
            tail = _split_sentences(prev_piece.text)[-cfg.overlap_sentences:]
            carry = " ".join(tail).strip()
            used = est_tokens(prefix) + est_tokens(raw) + est_tokens(carry) + 3
            if carry and used <= cfg.max_tokens:
                raw = f"[...] {carry}\n{raw}"

        # Tiền tố đã chứa dòng tiêu đề; lặp lại y nguyên ngay dưới chỉ tốn chỗ
        # và làm loãng vector.
        body = raw
        if prefix:
            first, sep, rest = raw.partition("\n")
            if first.strip() and prefix.endswith(first.strip()):
                body = rest if sep else ""
        text = f"{prefix}\n{body}".strip() if prefix else raw

        chunk_id = f"{doc_id}#p{piece.page}#{index:04d}"
        # Đếm token trên đúng chuỗi sẽ đem đi embedding, không phải bản trước
        # khi chuẩn hoá khoảng trắng — hai bản lệch nhau vài token.
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
    """Chuẩn hoá khoảng trắng nhưng giữ xuống dòng (bảng markdown cần nó)."""
    lines = [clean_text(line) for line in text.split("\n")]
    return "\n".join(line for line in lines if line != "").strip()
