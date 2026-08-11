"""Phân loại ảnh trong tài liệu: logo trang trí hay hình minh hoạ nội dung.

Logo và hoạ tiết lặp ở đầu/cuối trang là nhiễu — đưa vào chunk chỉ làm loãng
vector. Ngược lại, biểu đồ và sơ đồ là nội dung thật, phải giữ chỗ để biết ở
mục nào có hình, kèm đường dẫn file ảnh đã tách ra.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field

import fitz

# Ảnh nhỏ hơn ngưỡng này gần như chắc chắn là logo, biểu tượng hoặc gạch trang trí
MIN_FIGURE_AREA_RATIO = 0.025    # 2.5% diện tích trang
MIN_FIGURE_SIDE = 70             # điểm ảnh, cạnh ngắn nhất
MARGIN_TOP = 0.12
MARGIN_BOTTOM = 0.88
# Cùng một ảnh lặp trên nhiều trang thì đó là yếu tố thương hiệu, không phải nội dung
REPEAT_PAGES = 2

# Hoa văn chìm — bàn tay đỡ chiếc khiên, chiếc lá, quả địa cầu in mờ giữa trang —
# to bằng cả nửa trang nên lọt hết mọi ngưỡng kích thước, không lặp lại nên cũng
# thoát luôn phép đếm số trang. Cái nó không có là **nét đậm**: hoa văn in mờ để
# chữ đè lên vẫn đọc được, còn biểu đồ hay lưu đồ thì bắt buộc phải có mực đậm
# mới nhìn ra. Đo trên tài liệu thật: hoa văn ≤ 0.02, mọi hình nội dung ≥ 0.17.
WATERMARK_INK = 170            # dưới mức xám này mới tính là nét đậm
WATERMARK_INK_RATIO = 0.05
WATERMARK_DPI = 72             # thấp hơn thì nét chữ mảnh bị khử răng cưa làm nhạt đi
NEAR_WHITE = 247               # sáng hơn mức này coi như nền giấy

# Khung bo góc màu đỏ vẽ quanh một đoạn văn, dải màu ngang trên đầu mục — phép
# đo mực ở trên không bắt được chúng, vì nó chia cho *số điểm không phải nền
# giấy*: khung chỉ có mấy nét đỏ trên nền trong suốt nên gần như 100% số điểm
# ấy đều là nét đậm, chấm 0.67 ngang với một biểu đồ dày đặc.
#
# Dấu hiệu tách được nằm ở chỗ khác: khung được vẽ *dưới* chữ, nên chữ sống của
# trang nằm đè lên nó. Hình nội dung thì ngược lại — nhãn của biểu đồ, chữ
# trong từng ô lưu đồ đều nằm sẵn trong chính file ảnh, nên vùng đó không có
# lấy một ký tự sống nào. Đo trên 60 ảnh đang được giữ: mọi hình nội dung 0 ký
# tự, mọi khung trang trí 292–523 ký tự.
TEXT_OVER_IMAGE_CHARS = 40

# Ảnh chụp quảng cáo (mảng ảnh gia đình cắt theo hình cánh quạt ở trang bìa
# từng phần) không có chữ đè lên nên luật trên không với tới. Cái nó khác hình
# nội dung là **số màu**: biểu đồ, lưu đồ vẽ bằng mảng màu phẳng, còn ảnh chụp
# thì chuyển sắc liên tục. Đếm số màu khác nhau khi dựng lại vùng ảnh ở 48 dpi:
#
#   biểu đồ / lưu đồ / khung trang trí   610 – 1 229 màu
#   ảnh chụp quảng cáo                  39 832 – 48 968 màu
#
# Ngưỡng đặt ở 8 000, cách mép trên 6.5 lần và mép dưới 5 lần.
PHOTO_COLOURS = 8000
PHOTO_DPI = 48
# Trang scan cũng là một tấm ảnh chụp cả trang — đo thì rớt, mà gỡ đi là mất
# trắng nội dung. Trang nào chữ sống ít hơn mức này thì ảnh *chính là* nội dung
# của trang, không đem ra đo.
PAGE_TEXT_FOR_PHOTO_TEST = 200

# Sau từ khoá phải có số thứ tự hoặc dấu hai chấm thì mới là chú thích hình.
# Nếu để trống cả hai, những câu mở đầu bằng "Hình thức...", "Ảnh hưởng..."
# sẽ bị nhận nhầm thành chú thích.
CAPTION = re.compile(
    r"^\s*(hình vẽ|hình|biểu đồ|sơ đồ|đồ thị|lưu đồ|ảnh|figure|fig|chart|diagram)"
    r"\s*(?:(\d+|(?-i:[IVX]{1,4}))\s*[:.\-–]?|[:.\-–])\s*(.*)$",
    re.I,
)


@dataclass
class DocImage:
    page: int
    bbox: tuple[float, float, float, float]
    width: float
    height: float
    kind: str                      # figure | logo
    reason: str                    # vì sao xếp loại như vậy
    xref: int = 0
    caption: str = ""
    file: str = ""                 # đường dẫn ảnh đã tách (nếu có)
    meta: dict = field(default_factory=dict)

    @property
    def y(self) -> float:
        return self.bbox[1]

    def placeholder(self) -> str:
        """Dòng giữ chỗ chèn vào nội dung chunk."""
        label = self.caption or "Hình minh hoạ"
        size = f"{round(self.width)}x{round(self.height)}"
        if self.file:
            return f"[HÌNH: {label} | {size} | {os.path.basename(self.file)}]"
        return f"[HÌNH: {label} | {size}]"


def _find_caption(page: fitz.Page, bbox) -> str:
    """Tìm dòng chú thích ngay dưới (hoặc ngay trên) ảnh."""
    x0, y0, x1, y1 = bbox
    zone = fitz.Rect(x0 - 30, y1, x1 + 30, y1 + 60)
    below = page.get_text("text", clip=zone).strip()
    for line in below.split("\n"):
        m = CAPTION.match(line.strip())
        if m:
            return line.strip()[:180]
    zone_above = fitz.Rect(x0 - 30, max(y0 - 45, 0), x1 + 30, y0)
    above = page.get_text("text", clip=zone_above).strip()
    for line in above.split("\n"):
        m = CAPTION.match(line.strip())
        if m:
            return line.strip()[:180]
    return ""


def dark_ink_ratio(page: fitz.Page, bbox) -> float:
    """Tỉ lệ điểm ảnh đậm trên tổng số điểm không phải nền giấy.

    Đọc lại vùng ảnh đúng như mắt người nhìn thấy trên trang: ảnh có phần trong
    suốt được chồng lên nền trắng trước khi đo, nên hoa văn chìm hiện ra đúng là
    một mảng xám nhạt chứ không phải màu gốc đậm của file ảnh.
    """
    rect = fitz.Rect(bbox) & page.rect
    if rect.is_empty:
        return 1.0                    # không đo được thì coi là hình thật
    try:
        pix = page.get_pixmap(clip=rect, dpi=WATERMARK_DPI, colorspace=fitz.csGRAY)
    except (RuntimeError, ValueError):
        return 1.0
    hist = Counter(pix.samples)
    nonwhite = sum(count for value, count in hist.items() if value < NEAR_WHITE)
    if not nonwhite:
        return 0.0                    # trắng tinh: không có gì để giữ
    dark = sum(count for value, count in hist.items() if value < WATERMARK_INK)
    return dark / nonwhite


def text_over_image(page: fitz.Page, bbox) -> int:
    """Số ký tự *sống* của trang nằm trong khung ảnh.

    Khác 0 nghĩa là ảnh được vẽ dưới chữ — nó là khung, dải màu hay nền, chứ
    không phải hình: chữ của một hình nội dung nằm trong chính file ảnh.
    """
    rect = fitz.Rect(bbox) & page.rect
    if rect.is_empty:
        return 0
    try:
        return len(page.get_text("text", clip=rect).strip())
    except (RuntimeError, ValueError):
        return 0


def colour_count(page: fitz.Page, bbox) -> int:
    """Số màu khác nhau khi dựng lại vùng ảnh — ảnh chụp nhiều hơn hẳn nét vẽ."""
    rect = fitz.Rect(bbox) & page.rect
    if rect.is_empty:
        return 0
    try:
        return page.get_pixmap(clip=rect, dpi=PHOTO_DPI).color_count()
    except (AttributeError, RuntimeError, ValueError):
        return 0                      # không đo được thì đừng gỡ nhầm


def collect_pdf_images(doc: fitz.Document,
                       treat_first_page_as_cover: bool = False) -> dict[int, list[DocImage]]:
    """Duyệt toàn tài liệu, xếp loại từng ảnh rồi gom theo trang.

    `treat_first_page_as_cover`: ảnh lớn ở trang đầu gần như luôn là hình bìa
    trang trí, không phải nội dung. Bật khi xuất bản sạch để gỡ chúng đi.
    """
    # Đếm số trang mà mỗi ảnh xuất hiện: lặp nhiều = logo/nền
    pages_per_xref: dict[int, set[int]] = {}
    raw: list[tuple[int, dict]] = []
    for pno, page in enumerate(doc, start=1):
        try:
            infos = page.get_image_info(xrefs=True)
        except Exception:
            infos = []
        for info in infos:
            xref = info.get("xref", 0)
            if xref:
                pages_per_xref.setdefault(xref, set()).add(pno)
            raw.append((pno, info))

    out: dict[int, list[DocImage]] = {}
    for pno, info in raw:
        page = doc[pno - 1]
        page_area = page.rect.width * page.rect.height
        x0, y0, x1, y1 = info["bbox"]
        w, h = abs(x1 - x0), abs(y1 - y0)
        area_ratio = (w * h) / page_area if page_area else 0
        xref = info.get("xref", 0)
        repeats = len(pages_per_xref.get(xref, {pno}))
        in_margin = y0 < page.rect.height * MARGIN_TOP or y1 > page.rect.height * MARGIN_BOTTOM

        # Ảnh phủ gần kín trang mà trang vẫn có nhiều chữ thì đó là nền trang trí:
        # nội dung thật nằm ở phần text, giữ ảnh lại chỉ thêm nhiễu.
        covers_page = area_ratio >= 0.85
        text_len = len(page.get_text("text").strip())

        caption = ""
        if covers_page and text_len >= 200:
            kind, reason = "logo", "ảnh nền phủ kín trang"
        elif treat_first_page_as_cover and pno == 1 and area_ratio >= MIN_FIGURE_AREA_RATIO:
            kind, reason = "cover", "hình bìa"
        elif repeats >= REPEAT_PAGES:
            kind, reason = "logo", f"lặp trên {repeats} trang"
        elif min(w, h) < MIN_FIGURE_SIDE or area_ratio < MIN_FIGURE_AREA_RATIO:
            kind, reason = "logo", f"quá nhỏ ({round(w)}x{round(h)})"
        elif in_margin and area_ratio < 0.12:
            kind, reason = "logo", "nằm ở lề trang"
        else:
            # Chú thích "Hình 3: …" là lời người soạn tự nhận đây là hình nội
            # dung — tin lời đó, khỏi đo đạc gì nữa.
            caption = _find_caption(page, (x0, y0, x1, y1))
            chars = 0 if caption else text_over_image(page, (x0, y0, x1, y1))
            ink = 1.0 if caption else dark_ink_ratio(page, (x0, y0, x1, y1))
            colours = 0
            if not caption and text_len >= PAGE_TEXT_FOR_PHOTO_TEST:
                colours = colour_count(page, (x0, y0, x1, y1))
            if ink < WATERMARK_INK_RATIO:
                kind, reason = "logo", f"hoa văn chìm, không có nét đậm ({ink:.0%})"
            elif chars >= TEXT_OVER_IMAGE_CHARS:
                kind, reason = "logo", f"khung trang trí, chữ của trang đè lên ({chars} ký tự)"
            elif colours >= PHOTO_COLOURS:
                kind, reason = "logo", f"ảnh chụp trang trí ({colours} màu)"
            else:
                kind, reason = "figure", f"ảnh nội dung ({round(w)}x{round(h)})"

        img = DocImage(
            page=pno, bbox=(x0, y0, x1, y1), width=w, height=h,
            kind=kind, reason=reason, xref=xref,
            caption=caption if kind == "figure" else "",
        )
        out.setdefault(pno, []).append(img)
    return out


# --- sơ đồ vẽ bằng nét ----------------------------------------------------
#
# Lưu đồ quy trình trong văn bản ngân hàng không phải ảnh: nó là một mớ nét vẽ
# cộng với chữ nằm trong từng ô. Trích xuất theo lối thường sẽ moi hết chữ ra
# rồi xếp thành một dòng dài vô nghĩa ("Nội dung Mẫu ĐVKD lưu Tiếp nhận và khai
# báo Kiểm tra, đối chiếu…") — cả sơ đồ biến mất, chỉ còn lại chữ rời rạc.
#
# Bảng cũng vẽ bằng nét, nên phải phân biệt cho được. Khác nhau ở chỗ: đường kẻ
# bảng bao giờ cũng ngang hoặc dọc, còn lưu đồ thì có **nét chéo** (mũi tên,
# cạnh hình thoi) và **nét cong** (hình bầu dục, góc bo). Đo trên tài liệu thật,
# trang bảng có đúng 0 nét như vậy còn trang lưu đồ có vài chục.
MIN_DIAGRAM_MARKS = 6
# Hai nét cách nhau trong khoảng này thì thuộc cùng một hình
DIAGRAM_GAP = 26.0
# Nới rìa vùng hình để không cắt cụt nét ngoài cùng khi xuất ảnh
DIAGRAM_PAD = 6.0
MIN_DIAGRAM_AREA_RATIO = 0.05
MIN_DIAGRAM_SIDE = 90


def _stroke_boxes(page: fitz.Page) -> tuple[list[fitz.Rect], list[fitz.Rect]]:
    """Khung bao của từng nét vẽ, tách riêng các nét *không* ngang dọc."""
    boxes: list[fitz.Rect] = []
    marks: list[fitz.Rect] = []
    for drawing in page.get_drawings():
        rect = fitz.Rect(drawing["rect"])
        if rect.is_empty or rect.is_infinite:
            continue
        boxes.append(rect)
        for item in drawing["items"]:
            if item[0] == "l":
                start, end = item[1], item[2]
                if abs(start.x - end.x) > 1.0 and abs(start.y - end.y) > 1.0:
                    marks.append(rect)
                    break
            elif item[0] in ("c", "qu"):
                marks.append(rect)
                break
    return boxes, marks


def _clusters(boxes: list[fitz.Rect], gap: float) -> list[fitz.Rect]:
    """Gom các khung nằm gần nhau thành từng vùng liền khối."""
    out: list[fitz.Rect] = []
    for box in boxes:
        grown = box + (-gap, -gap, gap, gap)
        touching = [r for r in out if r.intersects(grown)]
        for r in touching:
            out.remove(r)
            box = box | r
        out.append(box)
    # một vòng nữa cho các vùng chỉ dính nhau sau khi đã gộp
    if len(out) > 1:
        merged: list[fitz.Rect] = []
        for box in out:
            grown = box + (-gap, -gap, gap, gap)
            touching = [r for r in merged if r.intersects(grown)]
            for r in touching:
                merged.remove(r)
                box = box | r
            merged.append(box)
        return merged
    return out


def collect_vector_figures(page: fitz.Page) -> list[DocImage]:
    """Các sơ đồ vẽ bằng nét trên một trang, kèm khung bao của từng cái."""
    boxes, marks = _stroke_boxes(page)
    if len(marks) < MIN_DIAGRAM_MARKS:
        return []

    page_area = page.rect.width * page.rect.height
    out: list[DocImage] = []
    for cluster in _clusters(boxes, DIAGRAM_GAP):
        inside = sum(1 for m in marks if m in cluster)
        if inside < MIN_DIAGRAM_MARKS:
            continue
        rect = (cluster + (-DIAGRAM_PAD, -DIAGRAM_PAD,
                           DIAGRAM_PAD, DIAGRAM_PAD)) & page.rect
        if rect.is_empty or min(rect.width, rect.height) < MIN_DIAGRAM_SIDE:
            continue
        if page_area and (rect.width * rect.height) / page_area < MIN_DIAGRAM_AREA_RATIO:
            continue
        img = DocImage(
            page=page.number + 1, bbox=tuple(rect),
            width=rect.width, height=rect.height,
            kind="figure", reason=f"sơ đồ vẽ bằng nét ({inside} nét chéo/cong)",
            meta={"vector": True},
        )
        img.caption = _find_caption(page, img.bbox)
        out.append(img)
    return out


def export_figures(doc: fitz.Document, images: dict[int, list[DocImage]],
                   out_dir: str, doc_id: str) -> int:
    """Tách các hình nội dung ra file PNG để dùng lại (vd. cho mô hình ảnh)."""
    saved = 0
    os.makedirs(out_dir, exist_ok=True)

    # Dọn ảnh cũ của đúng tài liệu này. Nếu lần chạy trước tách ra nhiều hình
    # hơn (do đổi ngưỡng phân loại), các file thừa sẽ nằm lại và gây nhầm lẫn.
    prefix = f"{doc_id}_p"
    for name in os.listdir(out_dir):
        if name.startswith(prefix) and name.endswith(".png"):
            try:
                os.remove(os.path.join(out_dir, name))
            except OSError:
                pass
    for pno, items in images.items():
        page = doc[pno - 1]
        for idx, img in enumerate(i for i in items if i.kind == "figure"):
            name = f"{doc_id}_p{pno}_{idx}.png"
            path = os.path.join(out_dir, name)
            try:
                rect = fitz.Rect(img.bbox) & page.rect
                if rect.is_empty:
                    continue
                pix = page.get_pixmap(clip=rect, dpi=150)
                pix.save(path)
            except Exception:
                continue
            img.file = path
            saved += 1
    return saved
