"""Ghi bố cục đã dựng ra file .docx hoặc .pdf.

Hai bộ ghi cùng đọc một danh sách `LayoutPage` nên hai định dạng cho ra cùng
một bố cục: mỗi mục một trang, tiêu đề riêng dòng, cỡ chữ giảm dần theo cấp.

Cách trình bày ở đây phục vụ mô hình DLA đọc lại tài liệu. Mô hình đó nhìn
trang giấy chứ không đọc mã nguồn, nên nhãn nó gán cho từng dòng phụ thuộc vào
đúng ba dấu hiệu hình học:

* **title** — chữ đậm, cỡ lớn hơn hẳn nội dung, đứng riêng một dòng sát lề
  trái, có khoảng trắng rộng ở trên và dưới;
* **list-item** — dòng bắt đầu bằng ký hiệu đầu mục, thụt vào so với lề, cỡ
  chữ bằng nội dung;
* **text** — đoạn văn căn đều, không thụt, không ký hiệu.

Vì vậy tiêu đề phải được nới khoảng cách và giữ nguyên cỡ chữ theo cấp, còn
dòng gạch đầu dòng phải thụt lề thật sự — trông giống văn bản hành chính chuẩn
thì DLA mới dựng đúng cây phân cấp.
"""
from __future__ import annotations

import html
import io
import os
import re

import docx
import fitz
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from .layout import BODY_PT, LayoutPage, PageItem

# Ký hiệu mở đầu một dòng danh sách trong văn bản hành chính Việt Nam
_BULLET = re.compile(r"^\s*([-–—•+*]|\(?[a-zđ]\)|\(?[ivx]+\)|\d+[.)])\s+")

# Khoảng trắng quanh tiêu đề (pt). Rộng hơn khoảng cách giữa các đoạn văn để
# DLA thấy rõ tiêu đề là một khối riêng chứ không phải dòng đầu của đoạn.
HEAD_SPACE_BEFORE = 16.0
HEAD_SPACE_AFTER = 9.0
LIST_INDENT_PT = 18.0          # mức thụt lề của dòng gạch đầu dòng

# Vùng trống hẹp hơn ngần này thì không đặt nổi lấy một dòng chữ
MIN_PLACE_PT = 24.0
# Chặn vòng lặp đặt khối: một khối không bao giờ dài tới ngần này trang
_MAX_PAGES_PER_BLOCK = 500
# Số lượt dựng lại để dồn tiêu đề bị bỏ trơ cuối trang sang trang mới. Đẩy một
# tiêu đề xuống trang sau làm mọi thứ phía dưới trôi theo và đôi khi lộ ra một
# tiêu đề trơ khác, nên phải soát vài lượt; vòng lặp tự dừng ngay khi một lượt
# không tìm thấy chỗ nào mới, nên tài liệu bình thường chỉ tốn một lượt.
_MAX_REFLOW_PASSES = 10

# Bề rộng vùng nội dung A4 khi lề 2cm (đơn vị pt), dùng để co ảnh cho vừa
CONTENT_WIDTH_PT = 470.0

# Font phải có đủ dấu tiếng Việt. Base-14 của PDF thì không, nên phải nhúng
# font hệ thống; cặp đầu tiên tìm thấy sẽ được dùng.
_FONT_CANDIDATES = [
    ("times.ttf", "timesbd.ttf"),
    ("arial.ttf", "arialbd.ttf"),
    ("segoeui.ttf", "segoeuib.ttf"),
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
]
_FONT_DIRS = ["C:/Windows/Fonts", "/usr/share/fonts/truetype/dejavu",
              "/Library/Fonts", "/usr/share/fonts"]

DOCX_FONT = "Times New Roman"


# --- .docx ---------------------------------------------------------------

def _md_rows(md: str) -> list[list[str]]:
    """Đọc ngược bảng markdown thành các hàng ô."""
    rows: list[list[str]] = []
    for line in md.split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        if set(line.replace("|", "").replace(" ", "")) <= {"-"}:
            continue                      # dòng phân cách của markdown
        cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
        rows.append([c.strip().replace("\\|", "|") for c in cells])
    return rows


def _setup_docx(document) -> None:
    normal = document.styles["Normal"]
    normal.font.name = DOCX_FONT
    normal.font.size = Pt(BODY_PT)
    normal.paragraph_format.space_after = Pt(4)
    for section in document.sections:
        section.page_width, section.page_height = Cm(21), Cm(29.7)
        section.left_margin = section.right_margin = Cm(2)
        section.top_margin = section.bottom_margin = Cm(2)


def _outline_level(para, level: int) -> None:
    """Đánh dấu đoạn này là tiêu đề cấp `level` trong cấu trúc của Word.

    Cỡ chữ và độ đậm là thứ mô hình DLA nhìn thấy; `outlineLvl` là thứ Word và
    các bộ chuyển đổi sang PDF đọc được, nhờ đó bản PDF có sẵn cây bookmark
    đúng cấp mục.
    """
    pPr = para._p.get_or_add_pPr()
    tag = OxmlElement("w:outlineLvl")
    tag.set(qn("w:val"), str(min(max(level, 1), 9) - 1))
    pPr.append(tag)


def _docx_heading(document, item: PageItem) -> None:
    para = document.add_paragraph()
    # Khoảng trắng rộng ở trên/dưới là dấu hiệu mạnh nhất để DLA tách tiêu đề
    # ra khỏi đoạn văn liền kề.
    para.paragraph_format.space_before = Pt(HEAD_SPACE_BEFORE)
    para.paragraph_format.space_after = Pt(HEAD_SPACE_AFTER)
    para.paragraph_format.left_indent = Pt(0)
    para.paragraph_format.first_line_indent = Pt(0)
    para.paragraph_format.keep_with_next = True
    para.paragraph_format.keep_together = True
    # Tiêu đề tài liệu căn giữa như mọi văn bản hành chính, đề mục căn trái
    para.alignment = (WD_ALIGN_PARAGRAPH.CENTER if item.level <= 0
                      else WD_ALIGN_PARAGRAPH.LEFT)
    _outline_level(para, max(item.level, 1))
    run = para.add_run(item.text)
    run.bold = True
    run.font.size = Pt(item.size)
    run.font.name = DOCX_FONT


def _docx_body(document, item: PageItem) -> None:
    """Ghi một khối nội dung, tách riêng các dòng gạch đầu dòng.

    Gộp cả khối vào một đoạn căn đều sẽ làm dòng gạch đầu dòng trông y hệt văn
    xuôi; thụt lề thật thì DLA gán đúng nhãn `list-item` và cây phân cấp giữ
    được quan hệ mục cha – ý con.
    """
    for line in item.text.split("\n"):
        if not line.strip():
            continue
        bullet = bool(_BULLET.match(line))
        para = document.add_paragraph()
        fmt = para.paragraph_format
        if bullet:
            fmt.left_indent = Pt(LIST_INDENT_PT)
            fmt.first_line_indent = Pt(-LIST_INDENT_PT / 2)
            fmt.space_after = Pt(2)
            para.alignment = WD_ALIGN_PARAGRAPH.LEFT
        else:
            para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        run = para.add_run(line.strip())
        run.font.size = Pt(item.size)
        run.font.name = DOCX_FONT


def _row_property(row, tag: str) -> None:
    """Bật một thuộc tính bool của hàng bảng (w:tblHeader, w:cantSplit)."""
    trPr = row._tr.get_or_add_trPr()
    trPr.append(OxmlElement(tag))


def _repeat_header_row(table) -> None:
    if table.rows:
        _row_property(table.rows[0], "w:tblHeader")


def _keep_row_together(row) -> None:
    _row_property(row, "w:cantSplit")


def _docx_table(document, item: PageItem) -> None:
    rows = _md_rows(item.text)
    if not rows:
        # không còn hàng bảng nào (đã bị trải thành văn xuôi) -> ghi như đoạn
        # văn, tuyệt đối không bỏ trắng vì như vậy là mất nội dung
        _docx_body(document, item)
        return
    width = max(len(r) for r in rows)
    table = document.add_table(rows=len(rows), cols=width)
    table.style = "Table Grid"
    # Bảng dài vắt qua trang: lặp lại hàng tiêu đề ở mỗi trang và không cho
    # hàng nào bị ngắt làm đôi — hàng đứt đôi là lỗi hệ RAG phải sửa tay.
    _repeat_header_row(table)
    for row in table.rows:
        _keep_row_together(row)
    for r, cells in enumerate(rows):
        for c in range(width):
            cell = table.cell(r, c)
            cell.text = cells[c] if c < len(cells) else ""
            for para in cell.paragraphs:
                para.paragraph_format.space_after = Pt(0)
                for run in para.runs:
                    run.font.size = Pt(BODY_PT - 0.5)
                    run.font.name = DOCX_FONT
                    run.bold = r == 0
    document.add_paragraph()


def _docx_figure(document, item: PageItem) -> None:
    path = item.meta.get("file") or ""
    if path and os.path.isfile(path):
        width = float(item.meta.get("width") or 0) or CONTENT_WIDTH_PT
        document.add_picture(path, width=Pt(min(width, CONTENT_WIDTH_PT)))
        document.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
        caption = item.meta.get("caption") or ""
        if caption:
            para = document.add_paragraph()
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = para.add_run(caption)
            run.italic = True
            run.font.size = Pt(BODY_PT - 1)
        return
    # không có file ảnh -> giữ lại dòng giữ chỗ để không mất dấu vết của hình
    para = document.add_paragraph()
    run = para.add_run(item.text)
    run.italic = True


def write_docx(pages: list[LayoutPage], dst: str) -> None:
    document = docx.Document()
    _setup_docx(document)

    for index, page in enumerate(pages):
        if index:
            document.add_page_break()
        for item in page.items:
            if item.kind == "heading":
                _docx_heading(document, item)
            elif item.kind == "table":
                _docx_table(document, item)
            elif item.kind == "figure":
                _docx_figure(document, item)
            else:
                _docx_body(document, item)

    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    document.save(dst)


# --- .pdf ----------------------------------------------------------------

def _font_files() -> tuple[str, str, str] | None:
    """Tìm cặp font thường/đậm có sẵn trong hệ thống."""
    for folder in _FONT_DIRS:
        if not os.path.isdir(folder):
            continue
        for regular, bold in _FONT_CANDIDATES:
            if os.path.isfile(os.path.join(folder, regular)):
                if not os.path.isfile(os.path.join(folder, bold)):
                    bold = regular
                return folder, regular, bold
    return None


def _css(font: tuple[str, str, str] | None) -> str:
    face = ""
    if font:
        _folder, regular, bold = font
        face = (f"@font-face {{font-family: doc; src: url({regular});}}\n"
                f"@font-face {{font-family: doc; src: url({bold}); font-weight: bold;}}\n"
                "* {font-family: doc;}\n")
    return face + f"""
body {{font-size: {BODY_PT}px;}}
p {{font-size: {BODY_PT}px; margin: 0 0 4px 0; text-align: justify;}}
p.li {{margin-left: {LIST_INDENT_PT}px; text-align: left;}}
p.fig {{text-align: center; font-style: italic;}}
h1, h2, h3, h4, h5 {{font-weight: bold; text-align: left; margin-left: 0;
        margin-top: {HEAD_SPACE_BEFORE}px; margin-bottom: {HEAD_SPACE_AFTER}px;}}
table {{border-collapse: collapse; width: 100%; margin-bottom: 6px;}}
td, th {{border: 1px solid #808080; padding: 2px 4px; font-size: {BODY_PT - 1}px;
        text-align: left; vertical-align: top;}}
th {{font-weight: bold;}}
"""


def _table_html(md: str) -> str:
    rows = _md_rows(md)
    if not rows:
        return ""
    out = ["<table>"]
    for index, cells in enumerate(rows):
        tag = "th" if index == 0 else "td"
        out.append("<tr>" + "".join(
            f"<{tag}>{html.escape(c)}</{tag}>" for c in cells) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _item_html(item: PageItem) -> str:
    if item.kind == "heading":
        level = min(max(item.level, 1), 5)
        style = f"font-size: {item.size}px"
        if item.level <= 0:             # tiêu đề tài liệu
            style += "; text-align: center"
        return f'<h{level} style="{style}">{html.escape(item.text)}</h{level}>'

    if item.kind == "table":
        html_table = _table_html(item.text)
        if html_table:
            return html_table
        return f"<p>{html.escape(item.text)}</p>"   # đã trải thành văn xuôi

    if item.kind == "figure":
        path = item.meta.get("file") or ""
        if path and os.path.isfile(path):
            width = min(float(item.meta.get("width") or 0) or CONTENT_WIDTH_PT,
                        CONTENT_WIDTH_PT)
            out = (f'<p class="fig"><img src="{os.path.basename(path)}" '
                   f'width="{round(width)}"></p>')
            caption = item.meta.get("caption") or ""
            if caption:
                out += f'<p class="fig">{html.escape(caption)}</p>'
            return out
        return f'<p class="fig">{html.escape(item.text)}</p>'

    # mỗi dòng một thẻ <p> riêng: dòng gạch đầu dòng cần thụt lề thật để DLA
    # nhận ra là list-item thay vì đọc dính vào đoạn văn trên
    parts = [f'<p{" class=\"li\"" if _BULLET.match(line) else ""}>'
             f"{html.escape(line.strip())}</p>"
             for line in item.text.split("\n") if line.strip()]
    return "".join(parts)


def _page_html(page: LayoutPage) -> str:
    return "".join(_item_html(i) for i in page.items) or "<p></p>"


# Bảng ToUnicode do MuPDF dựng lại trỏ nhầm vài glyph sang ký tự "song trùng" —
# ký tự khác hẳn nhưng vẽ ra y hệt, nên nhìn trên giấy không thấy gì sai:
#
#   dấu cách  -> U+00A0 (dấu cách không ngắt)
#   dấu gạch  -> U+00AD (gạch nối mềm, vốn là ký tự vô hình)
#   chấm phẩy -> U+037E (dấu chấm hỏi Hy Lạp)
#
# Mọi công cụ đọc lại PDF vì thế nhận về một chuỗi không có lấy một dấu cách hay
# dấu gạch thường nào — tokenizer, BM25 và các bước tách từ phía RAG hỏng theo.
_ALIASES = {0x00A0: 0x0020, 0x00AD: 0x002D, 0x037E: 0x003B}

_BFCHAR = re.compile(rb"^<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]{4})>$")
_BFRANGE = re.compile(rb"^<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]{4})>$")


def _alias_of(code: bytes) -> bytes | None:
    fixed = _ALIASES.get(int(code, 16))
    return f"{fixed:04x}".encode() if fixed is not None else None


def _fix_tounicode_aliases(doc: fitz.Document) -> int:
    """Trỏ lại các glyph bị ánh xạ nhầm về đúng ký tự. Trả về số font đã sửa."""
    probes = [f"{code:04x}".encode() for code in _ALIASES]
    fixed = 0
    for xref in range(1, doc.xref_length()):
        if not doc.xref_is_stream(xref):
            continue
        try:
            data = doc.xref_stream(xref)
        except (RuntimeError, ValueError):
            continue
        if b"begincmap" not in data:
            continue
        lower = data.lower()
        if not any(probe in lower for probe in probes):
            continue

        out: list[bytes] = []
        section = b""
        changed = False
        for line in data.split(b"\n"):
            probe = line.strip()
            if probe.endswith(b"beginbfchar") or probe.endswith(b"beginbfrange"):
                section = b"char" if probe.endswith(b"beginbfchar") else b"range"
            elif probe in (b"endbfchar", b"endbfrange"):
                section = b""
            elif section == b"char":
                m = _BFCHAR.match(probe)
                alias = _alias_of(m.group(2)) if m else None
                if alias:
                    out.append(b"<" + m.group(1) + b"> <" + alias + b">")
                    changed = True
                    continue
            elif section == b"range":
                m = _BFRANGE.match(probe)
                alias = _alias_of(m.group(3)) if m else None
                if alias:
                    lo, hi, dst = m.group(1), m.group(2), m.group(3)
                    if lo.lower() == hi.lower():
                        out.append(b"<" + lo + b"> <" + hi + b"> <" + alias + b">")
                    else:
                        # dải *bắt đầu* từ ký tự sai: tách riêng mã đầu, phần
                        # còn lại vẫn phải trỏ về ký tự kế tiếp cho đúng thứ tự
                        nxt = f"{int(lo, 16) + 1:04x}".encode()
                        after = f"{int(dst, 16) + 1:04x}".encode()
                        out.append(b"<" + lo + b"> <" + lo + b"> <" + alias + b">")
                        out.append(b"<" + nxt + b"> <" + hi + b"> <" + after + b">")
                    changed = True
                    continue
            out.append(line)

        if changed:
            doc.update_stream(xref, b"\n".join(out), compress=True)
            fixed += 1
    return fixed


class _Sheet:
    """Con trỏ ghi: trang đang mở và mép dưới của phần đã ghi trên trang đó."""

    def __init__(self, writer, paper: fitz.Rect, frame: fitz.Rect,
                 first_page: int = 0):
        self.writer, self.paper, self.frame = writer, paper, frame
        self.device = None
        self.y = frame.y0
        self.page = first_page - 1     # chưa mở trang nào

    @property
    def at_top(self) -> bool:
        return self.device is None or self.y <= self.frame.y0

    @property
    def room(self) -> float:
        return self.frame.y1 - self.y

    def avail(self) -> fitz.Rect:
        return fitz.Rect(self.frame.x0, self.y, self.frame.x1, self.frame.y1)

    def new_page(self) -> None:
        self.close()
        self.device = self.writer.begin_page(self.paper)
        self.y = self.frame.y0
        self.page += 1

    def close(self) -> None:
        if self.device is not None:
            self.writer.end_page()
            self.device = None


def _keep_together(items: list[PageItem]) -> list[tuple[list[PageItem], int]]:
    """Gom các khối phải nằm cùng một trang, kèm số dòng tiêu đề mở đầu khối.

    Một khối = loạt dòng tiêu đề liền nhau + khối nội dung ngay sau chúng. Các
    dòng tiêu đề liền nhau (tiêu đề mục cha rồi tới mục con đầu tiên) phải đi
    cùng nhau, và phải kéo theo được ít nhất một mẩu nội dung của chính mình —
    nếu không thì tiêu đề ở lại trơ trọi cuối trang, đúng lỗi đang phải sửa.
    """
    groups: list[list[PageItem]] = []
    heads: list[int] = []
    for item in items:
        if item.kind == "heading":
            if groups and len(groups[-1]) == heads[-1]:
                groups[-1].append(item)      # nối vào loạt tiêu đề đang mở
                heads[-1] += 1
                continue
            groups.append([item])
            heads.append(1)
        elif groups and len(groups[-1]) == heads[-1] and heads[-1]:
            groups[-1].append(item)          # mẩu nội dung đầu tiên của loạt
        else:
            groups.append([item])
            heads.append(0)
    return list(zip(groups, heads))


def _natural_height(group: list[PageItem]) -> float:
    """Chiều cao khối này *phải* có để không bị bóp lại — chỉ hình mới có."""
    return max((float(i.meta.get("height") or 0)
                for i in group if i.kind == "figure"), default=0.0)


def _draw_flow(sheet: _Sheet, items: list[PageItem], css: str,
               archive: fitz.Archive,
               forced: set[int]) -> list[tuple[int, int, bool]]:
    """Đổ các khối xuống trang.

    Trả về vết đặt khối: (chỉ số khối, trang bắt đầu, khối có mở đầu bằng tiêu
    đề không). `forced` là các khối phải mở trang mới trước khi đặt — kết quả
    soát lại của lượt dựng trước.
    """
    trace: list[tuple[int, int, bool]] = []
    sheet.new_page()
    for index, (group, heads) in enumerate(_keep_together(items)):
        markup = "".join(_item_html(i) for i in group)
        if not markup:
            continue
        # Chỗ trống không đủ cao thì MuPDF *co ảnh lại* cho vừa thay vì đẩy
        # sang trang sau — một lưu đồ cao 630pt rơi vào cuối trang sẽ bị nén
        # còn hơn một phần ba và không còn đọc được chữ trong ô. Mở trang mới
        # trước khi đặt là cách duy nhất giữ nguyên cỡ hình.
        need = min(_natural_height(group), sheet.frame.height)
        if not sheet.at_top and (index in forced
                                 or sheet.room < max(MIN_PLACE_PT, need)):
            sheet.new_page()

        story = fitz.Story(html=markup, user_css=css, archive=archive)
        more, filled = story.place(sheet.avail())
        trace.append((index, sheet.page, bool(heads)))

        for _ in range(_MAX_PAGES_PER_BLOCK):
            story.draw(sheet.device)
            sheet.y = fitz.Rect(filled).y1
            if not more:
                break
            sheet.new_page()
            more, filled = story.place(sheet.avail())
    sheet.close()
    return trace


def _stranded_pages(doc: fitz.Document) -> set[int]:
    """Các trang kết thúc bằng một dòng tiêu đề, không còn nội dung phía dưới.

    Chỗ ngắt trang do MuPDF quyết định sau khi đã xếp chữ, và `place()` không
    nói được phần nào thật sự hiện ra: với một hàng bảng cao hơn chỗ trống còn
    lại, nó báo đã dùng hết chỗ nhưng thực tế chỉ vẽ mỗi dòng tiêu đề. Cách duy
    nhất chắc chắn là đọc lại trang vừa dựng và xem dưới tiêu đề còn gì không.
    """
    stranded: set[int] = set()
    for number in range(doc.page_count - 1):
        page = doc[number]
        bottom = 0.0
        last_size = 0.0
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                size = max((s["size"] for s in line["spans"] if s["text"].strip()),
                           default=0.0)
                if not size:
                    continue
                if line["bbox"][3] > bottom:
                    bottom, last_size = line["bbox"][3], size
        if last_size <= BODY_PT + 0.4:
            continue
        if any(info["bbox"][3] > bottom for info in page.get_image_info()):
            continue                    # dưới tiêu đề là một hình, không phải trống
        stranded.add(number)
    return stranded


def _render(pages: list[LayoutPage], css: str, archive: fitz.Archive,
            paper: fitz.Rect, frame: fitz.Rect,
            forced: dict[int, set[int]]) -> tuple[bytes, dict[int, tuple[int, int]]]:
    """Dựng cả tài liệu. Trả về (dữ liệu PDF, trang -> khối cuối cùng của trang)."""
    buffer = io.BytesIO()
    writer = fitz.DocumentWriter(buffer)
    last_on_page: dict[int, tuple[int, int]] = {}
    next_page = 0
    for index, page in enumerate(pages):
        # Mỗi LayoutPage mở một trang mới: ở chế độ mỗi mục một trang, đó chính
        # là ranh giới trang cần giữ.
        sheet = _Sheet(writer, paper, frame, next_page)
        for group, page_no, is_head in _draw_flow(sheet, page.items, css,
                                                  archive,
                                                  forced.get(index, set())):
            if is_head:
                last_on_page[page_no] = (index, group)
            else:
                last_on_page.pop(page_no, None)
        next_page = sheet.page + 1
    writer.close()
    return buffer.getvalue(), last_on_page


def write_pdf(pages: list[LayoutPage], dst: str, figure_dir: str | None = None) -> None:
    font = _font_files()
    archive = fitz.Archive()
    if font:
        archive.add(font[0])
    if figure_dir and os.path.isdir(figure_dir):
        archive.add(figure_dir)

    css = _css(font)
    paper = fitz.paper_rect("a4")
    frame = paper + (57, 57, -57, -57)      # lề 2cm

    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    # Dựng trong bộ nhớ rồi mới ghi ra: bản thô nhúng nguyên vẹn font hệ thống
    # (vài MB), phải rút gọn font trước khi lưu.
    #
    # Dựng lại vài lượt: mỗi lượt đọc trang vừa dựng, tìm những dòng tiêu đề bị
    # bỏ lại trơ trọi ở đáy trang rồi đánh dấu cho lượt sau mở trang mới ngay
    # trước chúng. Số khối bị đánh dấu chỉ tăng nên vòng lặp chắc chắn dừng.
    forced: dict[int, set[int]] = {}
    data, last_on_page = _render(pages, css, archive, paper, frame, forced)
    for _ in range(_MAX_REFLOW_PASSES):
        doc = fitz.open("pdf", data)
        try:
            stranded = _stranded_pages(doc)
        finally:
            doc.close()
        fresh = False
        for page_no in stranded:
            owner = last_on_page.get(page_no)
            if owner is None:
                continue
            index, group = owner
            if group not in forced.setdefault(index, set()):
                forced[index].add(group)
                fresh = True
        if not fresh:
            break
        data, last_on_page = _render(pages, css, archive, paper, frame, forced)

    doc = fitz.open("pdf", data)
    try:
        doc.subset_fonts()          # chỉ giữ các ký tự thật sự xuất hiện
    except (AttributeError, RuntimeError):
        pass
    _fix_tounicode_aliases(doc)     # sau khi rút gọn font, vì bước đó dựng lại ToUnicode
    try:
        doc.save(dst, garbage=4, deflate=True)
    finally:
        doc.close()


def write(pages: list[LayoutPage], dst: str, figure_dir: str | None = None) -> None:
    """Ghi ra file theo đuôi của `dst`."""
    ext = os.path.splitext(dst)[1].lower()
    if ext == ".pdf":
        write_pdf(pages, dst, figure_dir=figure_dir)
    elif ext == ".docx":
        write_docx(pages, dst)
    else:
        raise ValueError(f"Định dạng chưa hỗ trợ: {ext}")
