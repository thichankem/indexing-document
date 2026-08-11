"""Dựng lại bố cục tài liệu cho hệ RAG đọc theo cây phân cấp DLA.

Hệ RAG tự cắt chunk theo cây chỉ mục nó đọc được từ trang giấy, nên việc của
module này là làm cây đó hiện lên **không thể nhầm**:

  1. Tên mục luôn đứng riêng một dòng, in đậm, cỡ chữ giảm dần theo cấp và
     cách biệt rõ với cỡ chữ nội dung — đó là dấu hiệu để DLA xếp dòng đó vào
     nhãn `title` thay vì `text`/`list-item`.
  2. Tiêu đề tài liệu đứng đầu, cỡ lớn nhất: hệ RAG lấy nó làm gốc của title
     mỗi chunk.
  3. Không chèn thêm dòng tiêu đề nào không có trong tài liệu gốc — mỗi dòng
     thừa là một nhánh giả trong cây chỉ mục.
  4. Tiêu đề không bao giờ đứng trơ trọi ở cuối trang, tách khỏi nội dung của
     chính nó.

Mặc định nội dung chảy liên tục như một tài liệu bình thường. Chế độ *mỗi mục
một trang* (`page_per_section=True`) vẫn giữ lại cho trường hợp muốn ép ranh
giới chunk trùng ranh giới trang.

Module này chỉ tính toán bố cục (chia trang, cỡ chữ). Việc ghi ra file .docx
hoặc .pdf nằm ở `render.py`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .chunker import (
    CHARS_PER_TOKEN, MAX_TOKENS, _split_long_text, _split_table, est_tokens,
)
from .models import Section, continues_sentence

# Cỡ chữ tiêu đề theo cấp: "1." to nhất, "1.1" nhỏ hơn, "1.1.1" nhỏ nhất.
# Cấp sâu hơn danh sách này dùng luôn phần tử cuối.
#
# Cấp sâu nhất vẫn phải lớn hơn nội dung một khoảng thấy được bằng mắt: DLA
# phân biệt tiêu đề với dòng gạch đầu dòng chủ yếu bằng cỡ chữ và độ đậm, lệch
# nửa point thì nó xếp nhầm thành list-item.
HEADING_PT = [20.0, 17.0, 15.0, 13.5, 12.5, 11.5]
TITLE_PT = 24.0                # tiêu đề tài liệu — cấp 0, lớn nhất trang đầu
BODY_PT = 10.5                 # nội dung luôn nhỏ hơn mọi cỡ tiêu đề

# Hệ RAG đọc cây chỉ mục sâu tối đa 6 cấp. Mục sâu hơn vẫn được vẽ, nhưng dùng
# lại cỡ chữ của cấp 6 — thêm một cỡ nữa thì nó lọt xuống dưới cỡ nội dung và
# DLA sẽ đọc thành đoạn văn thường.
MAX_TREE_DEPTH = len(HEADING_PT)

# Ước lượng cho khổ A4, lề 2cm, giãn dòng 1.15
LINES_PER_PAGE = 42
CHARS_PER_LINE = 88            # ở cỡ chữ nội dung

# Chế độ chảy liên tục không chặn token: hệ RAG tự cắt chunk theo cây chỉ mục,
# không cắt theo trang, nên ép trần token cho từng trang chỉ làm trang lửng.
_NO_TOKEN_CAP = 10 ** 9


def heading_pt(level: int) -> float:
    """Cỡ chữ của tiêu đề cấp `level` (0 = tiêu đề tài liệu, 1 = cấp cao nhất)."""
    if level <= 0:
        return TITLE_PT
    return HEADING_PT[min(level, MAX_TREE_DEPTH) - 1]


@dataclass
class PageItem:
    """Một khối nội dung trên trang: tiêu đề, đoạn văn, bảng hoặc hình."""

    kind: str                  # heading | para | table | figure
    text: str
    level: int = 0             # chỉ dùng cho heading
    size: float = BODY_PT
    meta: dict = field(default_factory=dict)

    @property
    def lines(self) -> int:
        """Số dòng ước lượng khối này chiếm trên trang."""
        return _lines_of(self)

    @property
    def tokens(self) -> int:
        """Số token khối này đóng góp khi hệ RAG đọc lại trang."""
        if self.kind == "figure":
            # ảnh không sinh token, chỉ dòng chú thích bên dưới mới thành chữ
            return est_tokens(self.meta.get("caption") or "")
        return est_tokens(self.text)


@dataclass
class LayoutPage:
    """Một trang của tài liệu dựng lại — luôn thuộc đúng một mục."""

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
        # ảnh cao bao nhiêu điểm thì chiếm bấy nhiêu dòng văn bản, + dòng chú thích
        return max(2, math.ceil(float(height) / 14) + 1) if height else 3
    if item.kind == "table":
        rows = [r for r in item.text.split("\n") if r.strip()]
        # mỗi hàng ít nhất một dòng, hàng nhiều chữ thì xuống dòng trong ô
        return sum(max(1, math.ceil(len(r) / CHARS_PER_LINE)) for r in rows) + 1
    per_line = CHARS_PER_LINE * BODY_PT / item.size
    used = 0
    for line in item.text.split("\n"):
        used += max(1, math.ceil(len(line) / per_line))
    # khoảng cách trên/dưới khối
    return used + (1 if item.kind == "heading" else 0)


def _items_of(section: Section, drop_cover: bool = True) -> list[PageItem]:
    """Chuyển các block của một mục thành khối nội dung để xếp trang."""
    items: list[PageItem] = []
    for block in section.blocks:
        if drop_cover and block.kind == "figure" and block.page <= 1:
            continue                       # ảnh bìa: trang trí, không phải nội dung
        if block.is_table or block.kind == "table":
            items.append(PageItem("table", block.text, meta=dict(block.meta)))
        elif block.kind == "figure":
            items.append(PageItem("figure", block.text, meta=dict(block.meta)))
        elif block.text.strip():
            # PDF lưu theo dòng hiển thị nên một câu hay nằm rải ở hai khối
            # liền nhau. Nối lại thì đoạn văn mới liền mạch, và chunk cắt theo
            # câu mới đúng chỗ.
            if (items and items[-1].kind == "para"
                    and continues_sentence(items[-1].text, block.text)):
                items[-1] = PageItem("para", f"{items[-1].text} {block.text}",
                                     meta=items[-1].meta)
            else:
                items.append(PageItem("para", block.text, meta=dict(block.meta)))
    return items


def _explode(item: PageItem, cap_lines: int, cap_tokens: int) -> list[PageItem]:
    """Cắt nhỏ một khối vượt quá sức chứa một trang hoặc trần token."""
    if item.lines <= cap_lines and item.tokens <= cap_tokens:
        return [item]

    if item.kind == "figure":
        # hình không cắt được -> thu nhỏ lại cho vừa một trang
        height = float(item.meta.get("height") or 0)
        if not height:
            return [item]
        scale = max(0.1, (cap_lines - 1) / item.lines)
        meta = dict(item.meta)
        meta["height"] = height * scale
        meta["width"] = float(item.meta.get("width") or 0) * scale
        return [PageItem("figure", item.text, meta=meta)]

    # Cắt trong không gian token: đó là đơn vị của trần RAG, còn sức chứa của
    # trang thì quy đổi sang token để so cùng một thước.
    by_page = math.ceil(cap_lines * CHARS_PER_LINE / CHARS_PER_TOKEN)
    limit = max(40, min(cap_tokens, by_page))
    parts = [item]
    for _ in range(5):
        if item.kind == "table":
            # Bảng cắt theo hàng và lặp lại dòng tiêu đề ở mỗi phần. Hàng dài
            # quá trần bị trải thành văn xuôi, mảnh đó không còn là bảng nữa —
            # gắn nhãn "table" cho nó thì bộ ghi tìm không ra hàng nào và bỏ
            # trắng cả khối.
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
    """Số phần cần dùng nếu mỗi phần không vượt cả hai trần."""
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
    """Chia các khối thành số trang ít nhất, các trang đầy gần bằng nhau.

    Đổ đầy lần lượt sẽ để trang cuối gần như trống. Thay vào đó, hạ dần *cả
    hai* trần theo cùng một tỷ lệ, tìm mức đầy nhỏ nhất mà vẫn xếp vừa đúng
    ngần ấy trang, rồi mới đổ theo mức đó — các trang nhờ vậy dài xấp xỉ nhau.
    """
    if (sum(u.lines for u in units) <= cap_lines
            and sum(u.tokens for u in units) <= cap_tokens):
        return [units]

    need = _fits_in(units, cap_lines, cap_tokens)
    low, high = 10, 100                   # mức đầy, tính theo phần trăm hai trần
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
    """Bỏ dấu "…" đánh dấu chỗ tên mục bị cắt bớt (kèm dấu cộng lửng nếu có)."""
    return name[:-1].strip(" +") if name.endswith("…") else name


def _heading_and_rest(section: Section) -> tuple[str, str]:
    """Tách dòng tên mục ra khỏi phần nội dung viết liền sau nó.

    Văn bản hành chính hay viết cả nội dung vào ngay dòng tiêu đề ("14. KOC:
    Là những người tiêu dùng chủ chốt…"). Để nguyên thì cả đoạn bị in đậm cỡ
    chữ tiêu đề, nên chỉ giữ lại tên mục, phần còn lại chuyển xuống nội dung.

    Tên mục lấy ở độ dài *đầy đủ* chứ không phải bản rút gọn dùng cho đường dẫn
    mục lục: bản rút gọn cắt cứng ở mốc ký tự nên hay đứt ngang một cụm từ, và
    dòng tiêu đề trên giấy đọc ra thành thiếu chữ.
    """
    full = section.heading_text.strip()
    probe = _without_ellipsis(section.display_heading.strip())
    if probe and full.startswith(probe):
        return probe, full[len(probe):].lstrip(" :.-–\n")

    line, _sep, rest = full.partition("\n")
    line = line.strip()
    if not probe:
        return line, rest.strip()

    # Tên mục không đứng đầu dòng gốc. Hai trường hợp: hàng bảng đã làm phẳng
    # ("STT: 3.1 | Nội dung: Đối tượng khách hàng | …") thì tên nằm lọt giữa
    # dòng; mục đã gộp chỉ mục ("1.1 + 1.2") thì tên không có trong dòng nào cả.
    # Cả hai cùng một cách xử lý: dòng tiêu đề chỉ mang tên mục, còn nguyên dòng
    # gốc chuyển xuống nội dung — in cả dòng thành tiêu đề thì DLA đọc ra một
    # tên mục dài dằng dặc.
    number = section.number.split(" + ")[0].strip()
    body = (line[len(number):].lstrip(" :.-–")
            if number and line.startswith(number) else line)
    # Với mục đã gộp chỉ mục, tên riêng của nó vừa được in trên dòng tiêu đề.
    # Ở những mục mà *nội dung chính là tên mục* — danh mục định nghĩa "13 Doanh
    # nghiệp bán hàng đa cấp: Là doanh nghiệp…" — để nguyên thì dòng ngay dưới
    # tiêu đề lặp lại đúng từng chữ của tiêu đề.
    own = _without_ellipsis(section.own_title.strip())
    if own and body.startswith(own):
        body = body[len(own):].lstrip(" :.-–")
    return probe, "\n".join(p for p in (body, rest.strip()) if p)


def _is_descendant(child: Section, parent: Section) -> bool:
    return (len(child.path) > len(parent.path)
            and child.path[:len(parent.path)] == parent.path)


def _heading_item(section: Section) -> PageItem | None:
    """Khối tiêu đề của một mục, hoặc None nếu mục không có tiêu đề thật."""
    if section.is_preamble or not section.heading_text:
        return None
    title, _rest = _heading_and_rest(section)
    return PageItem("heading", title, level=section.level,
                    size=heading_pt(section.level))


def _content_items(section: Section, drop_cover: bool) -> list[PageItem]:
    """Nội dung của một mục, đã tách phần viết liền sau dòng tiêu đề ra."""
    _title, rest = _heading_and_rest(section) if section.heading_text else ("", "")
    units = _items_of(section, drop_cover=drop_cover)
    if not rest or section.is_preamble:
        return units

    # Phần viết liền sau tên mục thường mới là *nửa đầu* của câu, nửa sau nằm ở
    # khối tiếp theo (PDF lưu theo dòng hiển thị). Tách tên mục ra rồi để nguyên
    # thì đoạn văn vỡ làm đôi ngay dưới mỗi tiêu đề.
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
    """Xếp toàn bộ tài liệu thành các trang.

    Mặc định nội dung chảy liên tục; `page_per_section=True` quay lại lối cũ,
    mỗi mục chiếm trang riêng và mỗi trang gọn trong `max_tokens` token.
    """
    if not page_per_section:
        return _flow_pages(sections, lines_per_page, drop_cover, doc_title,
                           max_tokens)
    return _section_pages(sections, lines_per_page, max_tokens, drop_cover,
                          doc_title)


def _continuation(head: PageItem) -> PageItem:
    """Dòng tiêu đề mở lại một mục bị cắt làm nhiều phần."""
    return PageItem("heading", f"{head.text} (tiếp)", level=head.level,
                    size=head.size)


def _flow_pages(sections: list[Section], lines_per_page: int,
                drop_cover: bool, doc_title: str,
                max_tokens: int = MAX_TOKENS) -> list[LayoutPage]:
    """Dựng một dòng chảy liên tục, việc ngắt trang để bộ ghi tự lo.

    Số dòng tính ở module này chỉ là ước lượng, không bao giờ khớp tuyệt đối
    với engine dàn trang thật — nhất là với bảng. Tự chia trang theo ước lượng
    rồi ép bộ ghi theo thì trang nào tính hụt sẽ tràn, phần thừa rơi sang trang
    sau và để lại một trang gần như trống. Word và MuPDF dàn trang chính xác,
    cứ để chúng làm; đổi lại cả tài liệu chỉ còn một "trang bố cục".

    Trần token thì vẫn phải giữ, dù chảy liên tục. Hệ RAG cắt chunk theo cây
    chỉ mục chứ không theo trang, nên một mục dài bốn nghìn token sẽ thành đúng
    một chunk bốn nghìn token và bị khâu embedding cắt cụt. Mục vượt trần được
    chia thành nhiều phần, mỗi phần mở đầu bằng dòng "(tiếp)" — đó là nút mà hệ
    RAG bám vào để cắt, và các phần được chia đều nhau thay vì dồn hết vào phần
    đầu.
    """
    if not sections:
        return []
    items: list[PageItem] = []
    if doc_title:
        items.append(PageItem("heading", doc_title, level=0, size=TITLE_PT))
    for section in sections:
        head = _heading_item(section)
        # Dòng tiêu đề cũng được RAG đọc thành token của chunk, trừ ra trước.
        # Dòng "(tiếp)" dài hơn tiêu đề gốc nên lấy theo dòng tốn hơn.
        budget = max_tokens if max_tokens > 0 else _NO_TOKEN_CAP
        if head is not None:
            budget = max(40, budget - _continuation(head).tokens)

        units: list[PageItem] = []
        for unit in _content_items(section, drop_cover):
            units.extend(_explode(unit, lines_per_page, budget))

        if head is None:
            items.extend(units)
            continue
        # Sức chứa một trang không còn là ràng buộc ở chế độ chảy liên tục,
        # chỉ trần token mới là.
        for index, part in enumerate(_split_even(units, _NO_TOKEN_CAP, budget)):
            items.append(head if index == 0 else _continuation(head))
            items.extend(part)
    return [LayoutPage(sections[0], items)] if items else []


def _section_pages(sections: list[Section],
                   lines_per_page: int,
                   max_tokens: int,
                   drop_cover: bool,
                   doc_title: str) -> list[LayoutPage]:
    """Lối cũ: mỗi mục một trang riêng, mỗi trang gọn trong trần token."""
    pages: list[LayoutPage] = []
    # Mục cha thường chỉ có mỗi dòng tiêu đề. Dành hẳn một trang trống cho nó
    # thì phí, nên giữ lại để đặt lên đầu trang của mục con đầu tiên — vẫn
    # không vi phạm ràng buộc, vì mục cha không có nội dung riêng nào.
    pending: list[tuple[Section, PageItem]] = []

    for section in sections:
        # Tên mục phải đứng riêng một dòng, phần viết liền sau nó là nội dung.
        head = _heading_item(section)
        units = _content_items(section, drop_cover)

        if not units:
            if head is not None:
                pending.append((section, head))
            continue

        # Chỉ các tiêu đề treo *liền ngay trước* và là tổ tiên của mục này mới
        # được đặt lên đầu trang; số còn lại là mục rỗng thật sự, đứng riêng
        # trang để giữ đúng thứ tự tài liệu.
        keep = len(pending)
        while keep > 0 and _is_descendant(section, pending[keep - 1][0]):
            keep -= 1
        for owner, item in pending[:keep]:
            pages.append(LayoutPage(owner, [item]))
        lead: list[PageItem] = [item for _owner, item in pending[keep:]]
        pending = []
        if head is not None:
            lead.append(head)

        # Tiêu đề treo ở đầu trang cũng chiếm chỗ và cũng được RAG đọc thành
        # token, nên trừ ra trước khi xếp nội dung. Trang tiếp theo mang dòng
        # "(tiếp)" dài hơn tiêu đề gốc, phải trừ theo dòng nào tốn hơn.
        cont_text = f"{head.text} (tiếp)" if head is not None else ""
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
