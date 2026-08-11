"""Xuất bản tài liệu đã làm sạch.

Có hai cách xuất bản:

* **Dựng lại bố cục** (mặc định) — mỗi mục một trang riêng, tên mục đứng riêng
  một dòng với cỡ chữ giảm dần theo cấp. Chọn được đầu ra `.docx` hay `.pdf`.
* **Giữ bố cục gốc** — PDF vào thì PDF ra, DOCX vào thì DOCX ra, chỉ gỡ những
  thứ được chọn trong `CleanOptions`: logo, ảnh bìa, đầu/chân trang, mục lục.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass

import docx
import fitz
from docx.oxml.ns import qn

from . import render
from .extract_pdf import (
    _DOT_LEADER, _PAGE_NUM, _TOC_TITLE, FOOTER_ZONE, HEADER_ZONE,
    _collect_repeats, _line_text, _norm_key, _page_dict, is_boilerplate,
)
from .chunker import MAX_TOKENS
from .extract_docx import _classify_docx_image, _paragraph_images
from .images import collect_pdf_images
from .layout import LINES_PER_PAGE, build_pages
from .models import Section, document_title

# Sau khi gỡ nhiễu, trang còn ít hơn ngần này ký tự coi như trang trắng
EMPTY_PAGE_CHARS = 15

# Hậu tố của bản đã chuẩn hóa. Nhìn tên là biết ngay đâu là tài liệu gốc, đâu
# là bản dựng cho hệ RAG, kể cả khi hai bản nằm chung một thư mục.
FORMALIZED_SUFFIX = "_formalized"


@dataclass
class CleanOptions:
    """Chọn riêng từng thứ cần gỡ khi giữ nguyên bố cục gốc.

    Không phải ai cũng muốn gỡ hết. Có người chỉ cần bỏ logo với ảnh bìa để tài
    liệu nhẹ đi mà chữ vẫn y nguyên trang gốc; gỡ luôn đầu/chân trang và mục lục
    là đổi nội dung, phải do người dùng chủ động bật.
    """

    drop_logo: bool = True            # logo, hoạ tiết lặp, ảnh nền phủ trang
    drop_cover: bool = True           # ảnh bìa lớn ở trang đầu
    drop_header_footer: bool = True   # chữ ở đầu trang / chân trang
    drop_toc: bool = True             # phần mục lục (và trang chỉ còn mục lục)

    @property
    def touches_text(self) -> bool:
        return self.drop_header_footer or self.drop_toc


def _noise_rects(page: fitz.Page, repeats: set[str],
                 drop_header_footer: bool = True,
                 drop_toc: bool = True) -> tuple[list[fitz.Rect], bool]:
    """Tìm các vùng chữ cần xoá trên một trang.

    Trả về (danh sách vùng, trang này có phải chỉ toàn mục lục không).
    """
    height = page.rect.height
    rects: list[fitz.Rect] = []
    leaders: list[float] = []
    toc_title_y: float | None = None
    kept_before_toc = 0

    lines = []
    for block in _page_dict(page)["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            text = _line_text(line)
            if text:
                lines.append((text, fitz.Rect(line["bbox"])))

    for text, rect in lines:
        if _DOT_LEADER.search(text):
            leaders.append(rect.y0)
        elif _TOC_TITLE.match(text):
            toc_title_y = rect.y0

    toc_start: float | None = None
    if len(leaders) >= 4:
        toc_start = min(leaders)
        if toc_title_y is not None and toc_title_y <= toc_start:
            toc_start = toc_title_y

    for text, rect in lines:
        ratio = rect.y0 / height if height else 0
        in_margin = ratio <= HEADER_ZONE or ratio >= FOOTER_ZONE
        noise = is_boilerplate(text, ratio, repeats)
        if not noise and in_margin and len(text) < 90:
            tail = text.split()[-1] if text.split() else ""
            if _PAGE_NUM.search(tail):
                stripped = _norm_key(re.sub(r"\s*\d+\s*/\s*\d+\s*$", "", text))
                noise = stripped in repeats

        in_toc = toc_start is not None and rect.y0 >= toc_start
        is_toc = in_toc or bool(_DOT_LEADER.search(text))
        if (noise and drop_header_footer) or (is_toc and drop_toc):
            rects.append(rect)
        # Phần chữ thân bài đếm theo cách xếp loại, không theo lựa chọn của
        # người dùng: tắt gỡ mục lục không được biến trang mục lục thành trang
        # có nội dung thật.
        if not noise and not is_toc and not in_margin:
            kept_before_toc += len(text)

    only_toc = drop_toc and toc_start is not None and kept_before_toc < 40
    return rects, only_toc


def clean_pdf(src: str, dst: str, opts: CleanOptions | None = None) -> dict:
    """Tạo bản PDF sạch. Trả về thống kê những gì đã gỡ."""
    opts = opts or CleanOptions()
    doc = fitz.open(src)
    images = collect_pdf_images(doc, treat_first_page_as_cover=opts.drop_cover)
    # Chỉ quét chữ lặp khi thật sự đụng tới chữ — quét cả tài liệu không rẻ
    repeats = _collect_repeats(doc) if opts.touches_text else set()
    drop_kinds = {k for k, on in (("logo", opts.drop_logo),
                                  ("cover", opts.drop_cover)) if on}

    removed_images = 0
    removed_text_zones = 0
    kept_figures = 0
    blank_pages: list[int] = []

    for pno in range(doc.page_count):
        page = doc[pno]

        # 1) Gỡ logo, hoạ tiết và ảnh bìa.
        #    Phải gỡ theo đúng xref của từng ảnh. Nếu dùng redaction theo vùng,
        #    mọi ảnh *giao* với vùng đó cũng bị xoá — logo thường nằm đè lên
        #    hình minh hoạ lớn, nên cách đó sẽ cuốn mất luôn hình nội dung.
        drop = [i for i in images.get(pno + 1, []) if i.kind in drop_kinds]
        kept_figures += sum(1 for i in images.get(pno + 1, []) if i.kind == "figure")
        leftover: list[fitz.Rect] = []
        for img in drop:
            if img.xref:
                try:
                    page.delete_image(img.xref)
                    removed_images += 1
                    continue
                except (ValueError, RuntimeError):
                    pass
            rect = fitz.Rect(img.bbox) & page.rect
            if not rect.is_empty:
                leftover.append(rect)
        if leftover:
            # ảnh nhúng thẳng trong luồng nội dung, không có xref để gỡ riêng
            for rect in leftover:
                page.add_redact_annot(rect)
            page.apply_redactions(images=1, graphics=0, text=0)
            removed_images += len(leftover)

        # 2) Gỡ chữ ở đầu/chân trang và phần mục lục, không đụng tới hình
        if not opts.touches_text:
            continue
        rects, only_toc = _noise_rects(page, repeats,
                                       drop_header_footer=opts.drop_header_footer,
                                       drop_toc=opts.drop_toc)
        if rects:
            for rect in rects:
                page.add_redact_annot(rect)
            page.apply_redactions(images=0, graphics=0, text=0)
            removed_text_zones += len(rects)

        if only_toc and len(page.get_text("text").strip()) < EMPTY_PAGE_CHARS:
            blank_pages.append(pno)

    # 3) Trang chỉ còn lại mục lục thì bỏ hẳn cho gọn
    for pno in reversed(blank_pages):
        doc.delete_page(pno)

    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    # use_objstms gộp các đối tượng nhỏ vào luồng nén. Thiếu nó, file ghi ra
    # phình gần gấp đôi bản gốc dù nội dung đã bị gỡ bớt.
    try:
        doc.save(dst, garbage=4, deflate=True, use_objstms=1)
    except TypeError:  # bản PyMuPDF cũ chưa có tham số này
        doc.save(dst, garbage=4, deflate=True)
    pages_out = doc.page_count
    doc.close()

    return {
        "images_removed": removed_images,
        "figures_kept": kept_figures,
        "text_zones_removed": removed_text_zones,
        "pages_removed": len(blank_pages),
        "pages_out": pages_out,
    }


def _has_image(element) -> bool:
    """Run này có chứa ảnh không (ảnh Word hiện đại lẫn ảnh nhúng kiểu cũ)."""
    return bool(element.findall(qn("w:drawing")) or element.findall(qn("w:pict")))


def _clear_headers_footers(document, drop_text: bool = True,
                           drop_images: bool = True) -> int:
    """Xoá nội dung đầu trang và chân trang của mọi phần.

    Gỡ riêng chữ hay riêng ảnh được: logo công ty gần như luôn nằm trong header,
    nên người chỉ muốn bỏ logo vẫn phải đụng vào header mà không được mất dòng
    "Ban hành kèm theo Quyết định số…" ở đó.
    """
    removed = 0
    for section in document.sections:
        for part in (section.header, section.footer,
                     section.even_page_header, section.even_page_footer,
                     section.first_page_header, section.first_page_footer):
            if part is None:
                continue
            for para in part.paragraphs:
                if drop_text and drop_images:
                    if para.text.strip() or _has_image(para._p):
                        removed += 1
                    for child in list(para._p):
                        if child.tag != qn("w:pPr"):
                            para._p.remove(child)
                    continue
                for run in list(para.runs):
                    has_image = _has_image(run._r)
                    if (has_image and drop_images) or (not has_image and drop_text):
                        run._r.getparent().remove(run._r)
                        removed += 1
            if not drop_text:
                continue
            for table in part.tables:
                table._tbl.getparent().remove(table._tbl)
                removed += 1
    return removed


def clean_docx(src: str, dst: str, opts: CleanOptions | None = None) -> dict:
    """Tạo bản DOCX sạch, hình minh hoạ vẫn nằm trong file."""
    opts = opts or CleanOptions()
    document = docx.Document(src)

    # Word không có khái niệm "trang bìa" khi soạn thảo, nên ảnh bìa với logo
    # trong file .docx đều rơi vào cùng một phép xếp loại theo kích thước.
    drop_images = opts.drop_logo or opts.drop_cover
    removed_headers = 0
    if opts.drop_header_footer or drop_images:
        removed_headers = _clear_headers_footers(
            document, drop_text=opts.drop_header_footer, drop_images=drop_images)
    removed_images = 0
    kept_figures = 0

    for para in document.paragraphs:
        for run in list(para.runs):
            if not run._r.findall(qn("w:drawing")):
                continue
            infos = _paragraph_images(para)
            # Ảnh nhỏ hoặc không rõ kích thước là logo/hoạ tiết -> gỡ cả run
            keep = not drop_images or any(
                _classify_docx_image(i) == "figure" for i in infos)
            if keep:
                kept_figures += 1
                continue
            run._r.getparent().remove(run._r)
            removed_images += 1

    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    document.save(dst)

    return {
        "images_removed": removed_images,
        "figures_kept": kept_figures,
        "header_footer_parts_cleared": removed_headers,
        "text_zones_removed": 0,
        "pages_removed": 0,
    }


def out_dir_for(src: str, in_root: str, out_dir: str) -> str:
    """Thư mục kết quả của một file, giữ nguyên cây thư mục đầu vào.

    Quét cả cây thì hai thư mục con hoàn toàn có thể chứa hai file trùng tên
    (bản gốc và bản đã làm phẳng bảng chẳng hạn). Đổ chung một chỗ thì file sau
    ghi đè file trước, mất hẳn kết quả của file kia mà không báo gì.
    """
    rel = os.path.relpath(os.path.dirname(os.path.abspath(src)),
                          os.path.abspath(in_root))
    return out_dir if rel in (".", "") else os.path.join(out_dir, rel)


def _stem_with_suffix(name: str, suffix: str) -> str:
    """Ghép hậu tố vào tên file, không lặp lại nếu tên đã có sẵn.

    Chạy lại công cụ trên chính thư mục kết quả là chuyện thường, không có
    bước này thì tên phình dần thành `quy-dinh_formalized_formalized.pdf`.
    """
    return name if not suffix or name.endswith(suffix) else f"{name}{suffix}"


def out_path(src: str, out_dir: str, out_format: str = "same",
             suffix: str = FORMALIZED_SUFFIX) -> str:
    """Đường dẫn file kết quả; `out_format` là "same", "docx" hoặc "pdf"."""
    name, ext = os.path.splitext(os.path.basename(src))
    ext = ext.lower() if out_format in ("same", "", None) else f".{out_format.lower()}"
    dst = os.path.join(out_dir, f"{_stem_with_suffix(name, suffix)}{ext}")
    _refuse_to_overwrite_source(src, dst)
    return dst


def _refuse_to_overwrite_source(src: str, dst: str) -> None:
    """Chặn trường hợp file kết quả trùng đúng file đầu vào.

    Hậu tố `_formalized` đã tách bản dựng lại khỏi tài liệu gốc, nhưng chạy
    công cụ ngay trên một file đã chuẩn hóa thì hậu tố không nhân đôi, tên kết
    quả trùng đúng tên nguồn — ghi đè là mất tài liệu, không lấy lại được.
    """
    if os.path.abspath(src) == os.path.abspath(dst):
        raise ValueError(
            f"file kết quả trùng file gốc ({os.path.basename(src)}) — "
            "chọn thư mục -o khác thư mục chứa tài liệu đầu vào"
        )


def _pages_written(dst: str, pages, lines_per_page: int) -> int:
    """Số trang của file vừa ghi.

    Ở chế độ chảy liên tục, bộ ghi mới là bên quyết định ngắt trang, nên đếm
    trong file thật. Với .docx thì Word phân trang lúc mở, không đọc ra được —
    đành ước lượng theo số dòng.
    """
    if os.path.splitext(dst)[1].lower() == ".pdf":
        try:
            with fitz.open(dst) as doc:
                return doc.page_count
        except (RuntimeError, ValueError):
            pass
    if len(pages) > 1:
        return len(pages)
    total = sum(p.lines for p in pages)
    return max(1, math.ceil(total / lines_per_page)) if total else 0


def rebuild_document(sections: list[Section], src: str, out_dir: str,
                     out_format: str = "pdf", suffix: str = FORMALIZED_SUFFIX,
                     figure_dir: str | None = None, drop_cover: bool = True,
                     lines_per_page: int = LINES_PER_PAGE,
                     max_tokens: int = MAX_TOKENS,
                     page_per_section: bool = False,
                     doc_title: str | None = None) -> tuple[str, dict]:
    """Dựng lại tài liệu với cây chỉ mục hiện rõ cho hệ RAG đọc.

    Nội dung lấy từ cây mục lục đã trích xuất nên logo, đầu/chân trang và mục
    lục đã bị loại từ trước — không phải gỡ lại lần nữa.

    Mặc định xuất ra `.pdf`: mô hình DLA đọc PDF ổn định hơn hẳn `.docx`, nên
    đây là định dạng nên nạp vào hệ RAG. Nội dung chảy liên tục như một tài
    liệu bình thường; `page_per_section=True` quay lại lối mỗi mục một trang,
    khi đó `max_tokens` là trần token của từng trang.
    """
    dst = out_path(src, out_dir, out_format, suffix)
    # Ảnh lớn ở trang đầu PDF gần như luôn là hình bìa. Word thì không có khái
    # niệm trang khi soạn, nên không áp quy tắc đó cho .docx.
    cover = drop_cover and os.path.splitext(src)[1].lower() == ".pdf"
    title = document_title(src) if doc_title is None else doc_title
    pages = build_pages(sections, lines_per_page=lines_per_page,
                        max_tokens=max_tokens, drop_cover=cover,
                        page_per_section=page_per_section, doc_title=title)
    render.write(pages, dst, figure_dir=figure_dir)

    figures = sum(1 for p in pages for i in p.items if i.kind == "figure")
    return dst, {
        "images_removed": 0,
        "figures_kept": figures,
        "text_zones_removed": 0,
        "pages_removed": 0,
        "pages_out": _pages_written(dst, pages, lines_per_page),
        # đếm mục thật đã dựng, không đếm theo trang: chế độ chảy liên tục chỉ
        # có một "trang bố cục" duy nhất
        "sections_out": sum(1 for s in sections if not s.is_preamble),
        "doc_title": title,
        "layout": "per-section" if page_per_section else "flow",
    }


def clean_document(src: str, out_dir: str, suffix: str = FORMALIZED_SUFFIX,
                   opts: CleanOptions | None = None) -> tuple[str, dict]:
    """Làm sạch một tài liệu, giữ nguyên định dạng và bố cục đầu vào."""
    name, ext = os.path.splitext(os.path.basename(src))
    ext = ext.lower()
    dst = os.path.join(out_dir, f"{_stem_with_suffix(name, suffix)}{ext}")
    _refuse_to_overwrite_source(src, dst)

    if ext == ".pdf":
        stats = clean_pdf(src, dst, opts)
    elif ext == ".docx":
        stats = clean_docx(src, dst, opts)
    else:
        raise ValueError(f"Định dạng chưa hỗ trợ: {ext}")
    return dst, stats
