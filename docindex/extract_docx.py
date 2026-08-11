"""Đọc file .docx thành danh sách Block theo đúng thứ tự tài liệu."""
from __future__ import annotations

import os

import docx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from .models import Block, clean_text, rows_to_markdown
from .numbering import NUMERIC_FORMATS, NumberingResolver

# Số ký tự ước lượng cho một trang A4 khi tài liệu không có dấu ngắt trang
CHARS_PER_PAGE = 2200


def _iter_body(document):
    """Duyệt paragraph và table xen kẽ nhau theo đúng thứ tự trong file."""
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _num_pr(paragraph: Paragraph) -> tuple[str | None, int | None]:
    """Lấy (numId, ilvl) từ paragraph, kể cả khi được thừa kế từ style."""
    pPr = paragraph._p.pPr
    if pPr is not None and pPr.numPr is not None:
        num_id = pPr.numPr.numId.val if pPr.numPr.numId is not None else None
        ilvl = pPr.numPr.ilvl.val if pPr.numPr.ilvl is not None else 0
        if num_id is not None:
            return str(num_id), int(ilvl or 0)

    # numbering có thể khai báo ở style thay vì trực tiếp trên paragraph
    style = paragraph.style
    seen = set()
    while style is not None and style.style_id not in seen:
        seen.add(style.style_id)
        el = getattr(style, "_element", None)
        if el is not None:
            pPr_s = el.find(qn("w:pPr"))
            if pPr_s is not None:
                numPr = pPr_s.find(qn("w:numPr"))
                if numPr is not None:
                    nid = numPr.find(qn("w:numId"))
                    il = numPr.find(qn("w:ilvl"))
                    if nid is not None:
                        return (
                            nid.get(qn("w:val")),
                            int(il.get(qn("w:val"))) if il is not None else 0,
                        )
        style = getattr(style, "base_style", None)
    return None, None


def _has_page_break(paragraph: Paragraph) -> bool:
    """Dấu ngắt trang do người dùng chèn."""
    for br in paragraph._p.iter(qn("w:br")):
        if br.get(qn("w:type")) == "page":
            return True
    return False


def _rendered_breaks(paragraph: Paragraph) -> int:
    """Dấu ngắt trang Word ghi lại ở lần mở file gần nhất — khá sát thực tế."""
    return len(list(paragraph._p.iter(qn("w:lastRenderedPageBreak"))))


EMU_PER_PT = 12700          # 1 pt = 12700 EMU trong OOXML
MIN_FIGURE_SIDE_PT = 70     # nhỏ hơn ngưỡng này coi là logo/biểu tượng


def _paragraph_images(paragraph: Paragraph) -> list[dict]:
    """Lấy ảnh nhúng trong một paragraph kèm kích thước thật (đơn vị pt).

    Ảnh ở header/footer nằm ngoài body nên không lọt vào đây — đúng ý đồ, vì
    đó thường là logo.
    """
    out: list[dict] = []
    for drawing in paragraph._p.iter(qn("w:drawing")):
        extent = next(drawing.iter(qn("wp:extent")), None)
        w = h = 0.0
        if extent is not None:
            try:
                w = int(extent.get("cx", 0)) / EMU_PER_PT
                h = int(extent.get("cy", 0)) / EMU_PER_PT
            except (TypeError, ValueError):
                w = h = 0.0
        name = ""
        docpr = next(drawing.iter(qn("wp:docPr")), None)
        if docpr is not None:
            name = (docpr.get("descr") or docpr.get("name") or "").strip()
        blip = next(drawing.iter(qn("a:blip")), None)
        rid = blip.get(qn("r:embed")) if blip is not None else None
        out.append({"width": round(w), "height": round(h), "name": name, "rid": rid})
    return out


def _save_image(paragraph: Paragraph, rid: str | None, dst_base: str) -> str:
    """Ghi ảnh nhúng ra file để dựng lại tài liệu vẫn còn hình. Trả về đường dẫn."""
    if not rid:
        return ""
    try:
        part = paragraph.part.related_parts[rid]
        ext = os.path.splitext(part.partname)[1] or ".png"
        path = f"{dst_base}{ext}"
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as f:
            f.write(part.blob)
        return path
    except (KeyError, AttributeError, OSError):
        return ""


def _classify_docx_image(img: dict) -> str:
    w, h = img["width"], img["height"]
    if w and h and min(w, h) < MIN_FIGURE_SIDE_PT:
        return "logo"
    if not w or not h:
        return "logo"      # không xác định được kích thước -> nhiều khả năng là hoạ tiết
    return "figure"


def _table_page_breaks(table: Table) -> int:
    """Số lần bảng bị ngắt sang trang mới.

    Word đặt lastRenderedPageBreak vào mọi ô của hàng đầu tiên sau chỗ ngắt,
    nên phải đếm theo hàng chứ không đếm marker, nếu không số trang sẽ phồng
    lên gấp số cột.
    """
    rows_with_break = 0
    for tr in table._tbl.iter(qn("w:tr")):
        if next(tr.iter(qn("w:lastRenderedPageBreak")), None) is not None:
            rows_with_break += 1
    return rows_with_break


def _para_style(paragraph: Paragraph) -> tuple[bool, float]:
    """Trả về (in đậm, cỡ chữ) lấy từ run đầu tiên có nội dung."""
    for run in paragraph.runs:
        if not run.text.strip():
            continue
        bold = bool(run.bold)
        if run.bold is None and paragraph.style is not None:
            f = getattr(paragraph.style, "font", None)
            bold = bool(getattr(f, "bold", False))
        size = 0.0
        if run.font.size is not None:
            size = run.font.size.pt
        elif paragraph.style is not None:
            f = getattr(paragraph.style, "font", None)
            if f is not None and f.size is not None:
                size = f.size.pt
        return bold, size
    return False, 0.0


def _table_to_markdown(table: Table) -> str:
    """Chuyển bảng sang markdown để giữ được quan hệ hàng/cột khi embed."""
    rows: list[list[str]] = []
    for row in table.rows:
        cells: list[str] = []
        seen_tc = set()
        for cell in row.cells:
            # ô gộp (merge) lặp lại cùng một _tc -> chỉ lấy một lần
            if cell._tc in seen_tc:
                continue
            seen_tc.add(cell._tc)
            cells.append(clean_text(cell.text).replace("\n", " ").replace("|", "\\|"))
        if any(c for c in cells):
            rows.append(cells)
    return rows_to_markdown(rows)


def extract(path: str, figure_dir: str | None = None, doc_id: str = "doc",
            stats: dict | None = None) -> tuple[list[Block], str]:
    """Trả về (danh sách block, nguồn số trang)."""
    document = docx.Document(path)
    resolver = NumberingResolver(document)
    logos = figures = 0

    # Nếu Word có ghi lastRenderedPageBreak thì số trang bám sát bản in thật
    total_rendered = len(list(document.element.body.iter(qn("w:lastRenderedPageBreak"))))
    use_rendered = total_rendered > 0

    blocks: list[Block] = []
    page = 1
    chars_on_page = 0

    for item in _iter_body(document):
        if isinstance(item, Table):
            md = _table_to_markdown(item)
            # Bảng dài thường trải nhiều trang: dấu ngắt trang nằm bên trong ô,
            # phải cộng vào trước khi gán trang cho bảng, nếu không mọi nội dung
            # phía sau sẽ bị dồn nhầm về trang đầu.
            breaks_inside = _table_page_breaks(item)
            if md:
                chars_on_page += len(md)
                blocks.append(Block(
                    text=md, page=page, kind="table", is_table=True,
                    meta={"spans_pages": breaks_inside + 1},
                ))
            if use_rendered:
                page += breaks_inside
            elif chars_on_page >= CHARS_PER_PAGE:
                page += 1
                chars_on_page = 0
            continue

        para = item
        text = clean_text(para.text)

        if use_rendered:
            # ngắt trang được ghi nhận ngay tại paragraph này -> sang trang mới
            page += _rendered_breaks(para)
        elif _has_page_break(para):
            page += 1
            chars_on_page = 0

        # Hình minh hoạ được giữ chỗ; logo bị bỏ qua vì chỉ làm loãng vector
        for img in _paragraph_images(para):
            if _classify_docx_image(img) != "figure":
                logos += 1
                continue
            figures += 1
            label = img["name"] or "Hình minh hoạ"
            saved = ""
            if figure_dir:
                saved = _save_image(
                    para, img.get("rid"),
                    os.path.join(figure_dir, f"{doc_id}_p{page}_{figures}"))
            blocks.append(Block(
                text=f"[HÌNH: {label} | {img['width']}x{img['height']}]",
                page=page, kind="figure",
                meta={"figure": True, "caption": img["name"],
                      "width": img["width"], "height": img["height"], "file": saved},
            ))

        if not text:
            continue

        num_id, ilvl = _num_pr(para)
        number, fmt = resolver.number_for(num_id, ilvl)
        bold, size = _para_style(para)

        if not use_rendered:
            chars_on_page += len(text)

        blocks.append(Block(
            text=text,
            page=page,
            kind="para",
            number=number if fmt in NUMERIC_FORMATS else None,
            level=(ilvl + 1) if ilvl is not None else None,
            bold=bold,
            size=size,
            meta={"style": para.style.name if para.style else "", "list_format": fmt},
        ))

        if not use_rendered and chars_on_page >= CHARS_PER_PAGE:
            page += 1
            chars_on_page = 0

    if stats is not None:
        stats["logos_dropped"] = logos
        stats["figures_found"] = figures
        # Lưu đồ trong DOCX là đối tượng vẽ của Word, không phải nét trên trang
        # giấy như ở PDF — chưa nhận diện được.
        stats["diagrams_found"] = 0
        # Header/footer và cước chú của Word nằm ngoài body (header*.xml,
        # footnotes.xml) nên không bao giờ lọt vào chunk
        stats["boilerplate_lines_dropped"] = 0
        stats["footnote_lines_dropped"] = 0

    if not use_rendered and not any(_has_page_break_any(document)):
        source = "estimated"
    else:
        source = "rendered" if use_rendered else "page_break"
    return blocks, source


def _has_page_break_any(document) -> list[bool]:
    return [
        br.get(qn("w:type")) == "page"
        for br in document.element.body.iter(qn("w:br"))
    ]
