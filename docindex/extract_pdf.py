"""Đọc file .pdf thành danh sách Block, giữ lại tín hiệu bố cục (đậm, cỡ chữ)."""
from __future__ import annotations

import re
from collections import Counter

import fitz

from .images import collect_pdf_images, collect_vector_figures, export_figures
from .models import Block, clean_text, rows_to_markdown

BOLD_FLAG = 1 << 4          # bit "in đậm" trong span flags của PyMuPDF
HEADER_ZONE = 0.10          # 10% chiều cao trên cùng coi là vùng header
# Dải chân trang. Nới rộng hơn mức 10% đối xứng vì chân trang trong văn bản
# ngân hàng thường có hai ba dòng (tên đơn vị, số phiên bản, câu bảo mật) và
# dòng trên cùng của khối đó nằm quanh 85% chiều cao trang — để mốc ở 90% thì
# nó không bao giờ được nhận là dòng lặp và trôi thẳng vào nội dung.
FOOTER_ZONE = 0.84
REPEAT_RATIO = 0.3          # xuất hiện trên >=30% số trang thì coi là header/footer
REPEAT_MIN_PAGES = 2        # tài liệu ngắn: lặp 2 trang đã đủ kết luận
# Dải rộng dành riêng cho luật "lặp lại ở đúng một chỗ". Chân trang không phải
# tài liệu nào cũng nằm sát đáy: có bản đặt số trang ở 81% chiều cao, cao hơn
# mọi mốc chân trang hợp lý. Bù lại, luật này đòi hỏi dòng phải lặp *đúng một
# toạ độ* qua các trang, nên nới dải ra không kéo theo nội dung thật.
REPEAT_ZONE = 0.75
# Chênh lệch toạ độ tối đa (tính theo phần chiều cao trang) giữa các lần xuất
# hiện thì vẫn coi là "đúng một chỗ"
REPEAT_Y_SPREAD = 0.02

# Cước chú nằm dưới đường kẻ cuối trang, luôn ở dải cuối và luôn nhỏ hơn cỡ
# chữ nội dung ít nhất một point — hai dấu hiệu này đủ để khoanh vùng.
FOOTNOTE_ZONE = 0.70        # từ 70% chiều cao trở xuống mới có thể là cước chú
FOOTNOTE_SIZE_GAP = 1.0     # nhỏ hơn cỡ chữ nội dung ngần này point (pt)
# Dấu tham chiếu cước chú nhỏ hơn chữ xung quanh ít nhất ngần này point
REF_MARK_GAP = 1.5
# Số ký tự ngữ cảnh ghi lại để tìm đúng chỗ dấu tham chiếu trong text của bảng
REF_CONTEXT = 12

# Trang có dưới ngần này ký tự coi như không có lớp text. Để mốc trên 0 vì bản
# scan vẫn hay được đóng thêm một dòng số trang hay dấu bản quyền dạng chữ.
SCAN_PAGE_CHARS = 40

# Các dòng chân trang quen thuộc trong văn bản hành chính/ngân hàng
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

# "9/34", "Trang 3", "- 5 -" ... là đánh số trang, không phải nội dung
_PAGE_NUM = re.compile(r"^(trang\s*)?[-–\s]*\d+\s*(/\s*\d+)?\s*[-–]*$", re.I)
# dòng mục lục có chuỗi dấu chấm dẫn tới số trang
_DOT_LEADER = re.compile(r"\.{4,}\s*\d*\s*$")

# Nội dung của một span dấu tham chiếu cước chú: chỉ có số thứ tự (đôi khi vài
# số ngăn nhau bằng dấu phẩy) hoặc ký hiệu quen thuộc.
_REF_MARK = re.compile(r"^(\d{1,3}([,;]\s*\d{1,3})*|[*†‡§¶]{1,3})$")

# Dòng mở đầu một mục mới: gạch đầu dòng, "a)", "(i)", "1.2.", "Điều 5"
_ITEM_START = re.compile(
    r"^([-–•▪]|\(?[a-zA-Z]\)|\(?[ivx]+\)|(Điều|PHẦN|Phần|Chương|Mục)\b"
    r"|\d+(\.\d+)*[.)]?\s)"
)
# Lề treo rộng nhất còn coi là của cùng một đoạn — quá ngần này thì dòng thụt
# vào là một mục con thật sự, không phải phần đuôi của dòng trên.
HANGING_INDENT_PT = 60.0

# Công cụ "làm phẳng" PDF (in ra PDF, gỡ form, ký số…) hay dựng lại từng dòng
# chữ bằng cách đặt riêng vị trí cho mỗi glyph, và chèn giữa hai chữ cái một
# glyph dấu cách rộng gần bằng không. Trên giấy không thấy gì lạ, nhưng mọi
# công cụ đọc lại đều nhận về "B á n , x á c n h ậ n" — câu vỡ thành từng ký
# tự rời, tokenizer và bản dựng lại hỏng theo, chữ in ra trông giãn nham nhở.
#
# Dấu cách thật đẩy chữ kế tiếp đi một quãng thấy được (hẹp nhất cũng khoảng
# 0.2 lần cỡ chữ), dấu cách giả thì không đẩy gì cả. PyMuPDF trả về hộp bao của
# từng glyph theo bước tiến, nên hai chữ cái liền nhau trong một từ luôn sát
# nhau — cứ đo khoảng hở giữa hai glyph *thật* là phân biệt được, không cần tin
# vào dấu cách có sẵn trong file.
GHOST_SPACE_RATIO = 0.12    # hở hẹp hơn ngần này lần cỡ chữ thì không phải dấu cách

# Chỉ nhận là bảng khi khung của nó được vẽ bằng nét kẻ thật. Mặc định PyMuPDF
# còn coi hình chữ nhật tô mỏng là nét kẻ, và nếu vẫn không thấy bảng nào thì
# lùi về đoán khung theo khoảng trắng giữa các từ. Cả hai lối đoán ấy đều vỡ
# trên file đã làm phẳng: file loại này rải đầy hình chữ nhật mỏng, còn dấu
# cách giả giữa từng chữ cái thì mở ra vô số "cột" ảo. Kết quả là một đoạn văn
# xuôi bị xẻ dọc thành bảng mười cột, chữ đứt ngang từ nằm rải khắp các ô.
_TABLE_STRATEGY = {"vertical_strategy": "lines_strict",
                   "horizontal_strategy": "lines_strict"}


def _rebuild_line(line: dict) -> None:
    """Dựng lại `text` của từng span trong một dòng từ vị trí của từng glyph.

    Bỏ hẳn ký tự trắng có sẵn rồi đặt lại dấu cách theo khoảng hở đo được: dấu
    cách giả (không đẩy chữ kế tiếp đi đâu) biến mất, còn chỗ ngắt từ chỉ được
    đánh dấu bằng vị trí — không có glyph dấu cách nào — thì vẫn có dấu cách.

    Phải chạy trên cả dòng chứ không từng span riêng lẻ: chữ có dấu thường lấy
    glyph từ font khác nên PyMuPDF cắt dòng thành nhiều span, và dấu cách hay
    rơi đúng vào cuối một span ("QUY " | "ĐỊNH"). Đo trong phạm vi một span thì
    khoảng hở đó không còn ai đối chiếu và hai từ dính liền nhau.

    Dòng xoay nghiêng thì trục ngang không nói lên điều gì, giữ nguyên chuỗi gốc.
    """
    spans = line["spans"]
    if abs(line.get("dir", (1.0, 0.0))[1]) >= 0.01:
        for span in spans:
            span["text"] = "".join(c["c"] for c in span.get("chars") or [])
        return

    prev_end: float | None = None      # mép phải của glyph thật gần nhất
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
    """Như `get_text("dict")`, nhưng text của mỗi span dựng lại từ hình học."""
    data = page.get_text("rawdict")
    for block in data["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            _rebuild_line(line)
    return data


def _norm_key(s: str) -> str:
    """Chuẩn hoá để so khớp header/footer giữa các trang (bỏ chữ số)."""
    return re.sub(r"\d+", "#", clean_text(s).lower())


def _dominant_size(spans: list[dict]) -> float:
    """Cỡ chữ chiếm phần lớn ký tự của một dòng."""
    counter: Counter[float] = Counter()
    for span in spans:
        text = span["text"].strip()
        if text:
            counter[round(span["size"], 1)] += len(text)
    return counter.most_common(1)[0][0] if counter else 0.0


def _is_ref_mark(span: dict, main: float) -> bool:
    """Span này có phải dấu tham chiếu cước chú (số mũ nhỏ) không?"""
    return bool(main and span["size"] <= main - REF_MARK_GAP
                and _REF_MARK.match(span["text"].strip()))


def _starts_with_ref_mark(line: dict) -> bool:
    """Dòng có mở đầu bằng dấu tham chiếu cước chú không?

    Đây là dấu hiệu nhận ra dòng *đầu* của một cước chú ở cuối trang, tách nó
    khỏi đoạn nội dung bình thường cũng in cỡ chữ nhỏ.
    """
    main = _dominant_size(line["spans"])
    for span in line["spans"]:
        if span["text"].strip():
            return _is_ref_mark(span, main)
    return False


def _line_text(line: dict) -> str:
    """Ghép các span trong một dòng.

    PDF xuất từ Word hay tách chỉ mục thành span riêng ("1." | "Phạm vi ...")
    nên phải ghép lại mới nhận ra được heading.

    Dấu tham chiếu cước chú bị bỏ hẳn: nó là một span chữ số cỡ nhỏ nằm lọt
    giữa dòng, ghép thẳng vào thì dính liền vào chữ ("…từng thời kỳ6.") và hệ
    RAG đọc ra một con số không hề có trong câu.
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
            # coi như dấu tham chiếu chưa từng có mặt: span kế tiếp nối thẳng
            # vào span trước nó, không sinh thêm khoảng trắng giả
            prev_end = span["bbox"][2]
            after_ref = True
            continue
        if after_ref:
            t = t.lstrip()
            after_ref = False
        # khoảng trống ngang đáng kể giữa 2 span -> chèn dấu cách
        elif prev_end is not None and span["bbox"][0] - prev_end > 1.5:
            parts.append(" ")
        parts.append(t)
        prev_end = span["bbox"][2]
    return clean_text("".join(parts))


def _footnote_cutoff(blocks: list[Block], height: float,
                     body_size: float) -> float | None:
    """Toạ độ y nơi khối cước chú cuối trang bắt đầu, None nếu trang không có.

    Cước chú không phải nội dung của mục nào: nó là chú thích của một chỗ nào
    đó trong thân bài, in cỡ chữ nhỏ dưới một đường kẻ ngang cuối trang. Giữ
    lại thì dòng "1 Theo địa giới hành chính cũ…" trông y hệt một đề mục đánh
    số, và cả nhánh nội dung phía sau bị treo nhầm xuống dưới nó.

    Quét ngược từ đáy trang lên, gom mạch chữ nhỏ liên tục cuối cùng. Chỉ cắt
    khi trong mạch đó có ít nhất một dòng mở đầu bằng dấu tham chiếu — đoạn
    nội dung bình thường in cỡ nhỏ ở cuối trang thì không có dấu ấy.
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
    """Tìm các dòng lặp ở đầu/cuối trang để loại bỏ.

    Hai điều kiện, phải có cả hai: dòng xuất hiện trên phần lớn số trang, **và**
    lần nào cũng ở đúng một toạ độ. Riêng điều kiện thứ nhất thì chưa đủ — một
    câu dẫn chiếu hay lặp cũng thoả — còn thêm điều kiện thứ hai vào thì chỉ
    còn đúng thứ được đặt trong khung đầu/chân trang.
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
                # Số trang thì mỗi trang một khác, nhưng `_norm_key` xoá hết
                # chữ số nên "1/5" và "2/5" cùng quy về "#/#" — vẫn bắt được.
                if not t or (len(t) < 3 and not _PAGE_NUM.match(t)):
                    continue
                seen.setdefault(_norm_key(t), ratio)
        for key, ratio in seen.items():
            spots.setdefault(key, []).append(ratio)

    threshold = max(REPEAT_MIN_PAGES, int(pages * REPEAT_RATIO))
    return {key for key, ys in spots.items()
            if len(ys) >= threshold and max(ys) - min(ys) <= REPEAT_Y_SPREAD}


def is_boilerplate(text: str, ratio: float, repeats: set[str]) -> bool:
    """Dòng này có phải đầu/chân trang không? `ratio` là y0 chia chiều cao trang.

    Dòng lặp lại đúng một chỗ được xét trên dải rộng; các câu chân trang quen
    mặt và số trang thì chỉ xét sát mép, vì bản thân chúng cũng xuất hiện trong
    thân bài (ô "Trang 5" của bảng mục lục chẳng hạn).
    """
    if (ratio <= HEADER_ZONE or ratio >= REPEAT_ZONE) and _norm_key(text) in repeats:
        return True
    if HEADER_ZONE < ratio < FOOTER_ZONE:
        return False
    return bool(_PAGE_NUM.match(text) or _BOILERPLATE.match(text))


_TOC_TITLE = re.compile(r"^\s*(mục lục|nội dung|table of contents|contents)\s*$", re.I)


def _toc_cutoff(blocks: list[Block]) -> float | None:
    """Xác định mục lục bắt đầu từ toạ độ y nào trên trang.

    Nhiều tài liệu đặt bìa và mục lục chung một trang. Bỏ cả trang sẽ mất luôn
    tên sản phẩm và số văn bản phê chuẩn ở phần bìa, nên chỉ cắt từ chỗ mục lục
    trở xuống.
    """
    leaders = [b for b in blocks if _DOT_LEADER.search(b.text)]
    if len(leaders) < 4:
        return None
    start = min(b.meta.get("y", 0) for b in leaders)
    # tiêu đề "MỤC LỤC" nằm ngay trên khối dấu chấm dẫn thì cắt từ đó
    for b in blocks:
        y = b.meta.get("y", 0)
        if _TOC_TITLE.match(b.text) and y <= start:
            return y
    return start


def _ref_mark_fixes(page: fitz.Page) -> list[tuple[str, str]]:
    """Các cặp (chữ liền trước, dấu tham chiếu cước chú) trên một trang.

    PyMuPDF trích bảng thẳng ra chuỗi, không còn thông tin span để nhận ra dấu
    tham chiếu, nên ô bảng giữ nguyên "…từng thời kỳ6.". Ghi lại vài ký tự
    đứng ngay trước mỗi dấu ở bước đọc span thì sau đó gỡ nó ra khỏi text của ô
    được — mà không đụng tới những con số thật sự thuộc về câu.
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
    """Cỡ chữ phổ biến nhất — dùng làm mốc nhận biết tiêu đề lớn."""
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

    # Logo bị loại thẳng; hình minh hoạ được giữ chỗ bằng một dòng mô tả
    images = collect_pdf_images(doc)
    # Lưu đồ vẽ bằng nét cũng là hình, dù không có ảnh nào nhúng trong file
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
        # Bản scan: mỗi trang là một tấm ảnh chụp, không có lớp text. Chữ, logo,
        # đầu/chân trang đều là điểm ảnh nằm chung trong một tấm hình duy nhất
        # — không có đối tượng riêng nào để gỡ, mà gỡ tấm hình đó thì trang
        # trắng trơn. Đếm ra đây để báo cho người dùng biết phải OCR trước.
        stats["pages_total"] = doc.page_count
        stats["pages_without_text"] = sum(
            1 for page in doc if len(page.get_text("text").strip()) < SCAN_PAGE_CHARS)

    blocks: list[Block] = []
    footnote_lines = 0

    for pno, page in enumerate(doc, start=1):
        height = page.rect.height
        ref_fixes = _ref_mark_fixes(page)

        # Chữ nằm trong một sơ đồ là nhãn của các ô trong sơ đồ đó, không phải
        # nội dung đọc theo dòng. Moi ra thì được một chuỗi vô nghĩa mà bản thân
        # sơ đồ cũng mất luôn — giữ nguyên nó ở dạng ảnh mới đúng.
        #
        # Chỉ bỏ chữ khi đã xuất được ảnh: không có ảnh thay thế thì thà giữ
        # chữ lộn xộn còn hơn mất trắng cả khối nội dung.
        diagrams = [fitz.Rect(i.bbox) for i in images.get(pno, [])
                    if i.meta.get("vector") and i.file]

        # Vùng bảng được xử lý riêng; các dòng nằm trong đó sẽ bị bỏ qua
        table_boxes: list[fitz.Rect] = []
        table_blocks: list[Block] = []
        try:
            for tbl in page.find_tables(**_TABLE_STRATEGY):
                md = _table_markdown(tbl, ref_fixes)
                if md:
                    rect = fitz.Rect(tbl.bbox)
                    table_boxes.append(rect)
                    # Lưu đồ có khung kẻ nên `find_tables` nhận nhầm nó thành
                    # một cái bảng khổng lồ một ô.
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
                # số trang lẫn vào giữa dòng chân trang, vd "Quy định sản phẩm 3/8"
                if in_margin and len(text) < 90 and _PAGE_NUM.search(text.split()[-1] if text.split() else ""):
                    stripped = _norm_key(re.sub(r"\s*\d+\s*/\s*\d+\s*$", "", text))
                    if stripped in repeats:
                        continue
                mid = fitz.Point((x0 + x1) / 2, (y0 + y1) / 2)
                if any(mid in r for r in table_boxes):
                    continue
                if any(mid in r for r in diagrams):
                    continue        # nhãn bên trong sơ đồ, đã nằm trong ảnh

                spans = [s for s in line["spans"] if s["text"].strip()]
                if not spans:
                    continue
                size = max(s["size"] for s in spans)
                # coi là đậm khi phần lớn ký tự trong dòng ở dạng đậm
                bold_chars = sum(len(s["text"]) for s in spans if s["flags"] & BOLD_FLAG)
                total_chars = sum(len(s["text"]) for s in spans)
                bold = total_chars > 0 and bold_chars / total_chars >= 0.6

                candidates.append(Block(
                    text=text, page=pno, kind="para", bold=bold, size=round(size, 1),
                    meta={"x0": round(x0, 1), "y": round(y0, 1), "body_size": body_size,
                          "ref_start": _starts_with_ref_mark(line)},
                ))

        # Cắt bỏ khối cước chú ở cuối trang trước khi nối dòng: để lại thì các
        # dòng cước chú nối tiếp nhau thành một đoạn dài giả.
        note_at = _footnote_cutoff(candidates, height, body_size)
        if note_at is not None:
            kept = [b for b in candidates if b.meta.get("y", 0) < note_at]
            footnote_lines += len(candidates) - len(kept)
            candidates = kept

        # Cắt bỏ phần mục lục nhưng giữ nội dung nằm phía trên nó trên cùng trang
        cutoff = _toc_cutoff(candidates)
        if cutoff is not None:
            candidates = [b for b in candidates if b.meta.get("y", 0) < cutoff]
        candidates = [b for b in candidates if not _DOT_LEADER.search(b.text)]

        merged = _merge_wrapped(_join_same_row(candidates))
        merged.extend(table_blocks)

        # Giữ chỗ cho hình minh hoạ đúng vị trí của nó trong mạch nội dung,
        # để chunk biết mục này có hình mà không nuốt mất logo vào text.
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
    """Gộp các mảnh nằm trên cùng một hàng ngang.

    Trong PDF, ký hiệu đầu mục ("a.", "(i)", "-") thường được đặt ở cột lề
    riêng nên PyMuPDF tách thành dòng độc lập. Nếu không gộp lại, ta có block
    rác chỉ chứa "a." còn nội dung thì mất ký hiệu đầu mục.
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
        # giữ lề của phần nội dung để bước nối dòng phía sau canh đúng
        body = next((b for b in row[1:] if len(b.text) > 4), None)
        if body is not None:
            head.meta["x0"] = body.meta.get("x0", head.meta.get("x0"))
        out.append(head)
    return out


def _merge_wrapped(lines: list[Block]) -> list[Block]:
    """Nối các dòng bị xuống hàng do tràn lề thành một đoạn văn hoàn chỉnh.

    PDF lưu từng dòng hiển thị, không lưu đoạn văn. Nếu không nối lại, mỗi
    dòng thành một block rời rạc và câu bị cắt giữa chừng khi chunk.
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
        # Đoạn có chỉ mục dùng lề treo: dòng thứ hai trở đi thụt vào đúng bằng
        # bề rộng của chỉ mục ("15.10. " ~ 32pt). Đòi hai dòng thẳng lề nhau thì
        # tiêu đề dài hai dòng không bao giờ được nối, và nửa sau của nó ("liệu
        # sản xuất") rơi xuống thành một đoạn nội dung cụt lủn.
        hanging = 0 < indent <= HANGING_INDENT_PT and bool(_ITEM_START.match(prev.text))
        gap = blk.meta.get("y", 0) - prev.meta.get("y", 0)
        # dòng trước chưa kết thúc câu và dòng này không mở đầu mục mới
        unfinished = not re.search(r"[.:;!?]$", prev.text)
        starts_new = bool(_ITEM_START.match(blk.text))
        near = 0 < gap < prev.size * 2.2

        # Từ bị gạch nối cắt đôi ở cuối dòng ("…và Dai-" / "ichi Life Việt
        # Nam…"). Phải nối kể cả khi hai dòng khác kiểu chữ: dòng đầu của một
        # mục thường in đậm phần tên, dòng sau thì không, nên điều kiện cùng
        # kiểu chữ ở dưới không bao giờ đúng và từ cứ nằm đứt đôi.
        if (near and not starts_new
                and re.search(r"\w-$", prev.text) and blk.text[:1].islower()):
            prev.text = clean_text(prev.text + blk.text)
            continue

        if same_style and (aligned or hanging) and near and unfinished and not starts_new:
            prev.text = clean_text(prev.text + " " + blk.text)
            continue
        out.append(blk)
    return out
