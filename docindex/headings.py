"""Nhận diện tiêu đề và dựng cây mục lục.

Nguyên tắc: một chunk chỉ nên chứa một chủ đề. Chủ đề ở đây được xác định bằng
mục lục của tài liệu, nên chất lượng chunk phụ thuộc trực tiếp vào việc nhận
diện tiêu đề có đúng hay không.
"""
from __future__ import annotations

import re

from .models import PREAMBLE_TITLE, Block, Section, clean_text, continues_sentence

# Thứ bậc các loại tiêu đề trong văn bản hành chính/pháp lý Việt Nam.
# Số nhỏ = cấp cao hơn.
#
# Các mốc cách nhau rộng vì cấp của danh sách Word được cộng thêm vào mốc:
# hai loại ký hiệu khác nhau không bao giờ được rơi trúng cùng một số, nếu
# không hai cấp khác nhau sẽ bị nén thành một khi dựng cây.
RANK_BANNER = -1       # TỔNG QUAN VĂN BẢN QUY ĐỊNH — tiêu đề lớn không đánh số
RANK_PART = 0          # PHẦN 1, Phần I
RANK_CHAPTER = 1       # CHƯƠNG I
RANK_APPENDIX = 1      # Phụ lục 01
RANK_SECTION = 2       # MỤC 1
RANK_ROMAN_UPPER = 3   # I.  II.  III.  — cấp lớn trong văn bản hành chính
RANK_ARTICLE = 4       # ĐIỀU 5
RANK_DECIMAL = 10      # 1.  /  2.1.  /  2.3.1.
RANK_LETTER_UPPER = 25 # A)  B)
RANK_LETTER = 30       # a)  b)
RANK_ROMAN = 50        # (i) (ii)

# Chữ số La Mã hợp lệ: I..XXXIX. Dùng để tách "I." (cấp lớn) khỏi "i)" (mục
# thứ 9 của một danh sách chữ cái a) b) ... i) — hai thứ hoàn toàn khác nhau).
_IS_ROMAN = re.compile(r"^(?=[ivx])x{0,3}(ix|iv|v?i{0,3})$", re.I)

_PATTERNS: list[tuple[re.Pattern, int, str]] = [
    (re.compile(r"^(PHẦN|Phần)\s+([0-9IVXivx]+)\s*[:.\-–]?\s*(.*)$"), RANK_PART, "part"),
    (re.compile(r"^(CHƯƠNG|Chương)\s+([0-9IVXivx]+)\s*[:.\-–]?\s*(.*)$"), RANK_CHAPTER, "chapter"),
    (re.compile(r"^(PHỤ LỤC|Phụ lục)\s*([0-9IVXivx]*)\s*[:.\-–]?\s*(.*)$"), RANK_APPENDIX, "appendix"),
    # Mã hiệu phụ lục nội bộ, vd "PL02.1003.PCS.2026(1): Giải thích từ ngữ".
    # Dấu ngăn không nhận dấu chấm: chấm là một phần của chính mã hiệu, nhận nó
    # thì câu dẫn chiếu "…theo Phụ lục số PL01.1016.PDS.2026(1);" cũng khớp và
    # đẻ ra một nhánh cấp cao nuốt hết các mục đứng sau.
    (re.compile(r"^(PL)\s*([0-9][0-9.()A-Za-z]*)\s*[:\-–]\s*(.*)$"), RANK_APPENDIX, "appendix"),
    (re.compile(r"^(MỤC|Mục)\s+([0-9IVXivx]+)\s*[:.\-–]?\s*(.*)$"), RANK_SECTION, "section"),
    (re.compile(r"^(ĐIỀU|Điều)\s+(\d+)\s*[:.\-–]?\s*(.*)$"), RANK_ARTICLE, "article"),
]

# "1." "2.1." "2.3.1." — dấu chấm cuối có thể có hoặc không
_DECIMAL = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.*)$")
# Tiêu đề chỉ có số, nội dung nằm ở dòng dưới
_DECIMAL_ONLY = re.compile(r"^(\d+(?:\.\d+)*)\.?$")
# Giữ nguyên dấu sau ký hiệu ("c." chứ không phải "c") để tiêu đề đọc đúng như
# trong văn bản gốc.
_LETTER = re.compile(r"^([a-zA-Z][.)])\s+(\S.*)$")
_ROMAN = re.compile(r"^(\(?[ivxIVX]{1,5}[.)])\s+(\S.*)$")

# Câu mở đầu bằng cách dẫn chiếu điều khoản, dễ bị nhầm là tiêu đề
_REFERENCE = re.compile(
    r"^(theo|tại|quy định tại|căn cứ|xem|nêu tại|như)\b", re.I
)

# Tiêu đề lớn không đánh số ("TỔNG QUAN VĂN BẢN QUY ĐỊNH", "QUY ĐỊNH SẢN PHẨM
# TIẾT KIỆM LINH HOẠT"): dài tối đa ngần này ký tự và viết hoa gần như toàn bộ.
MAX_BANNER_LEN = 120
BANNER_UPPER_RATIO = 0.8
# Tài liệu chỉ có đúng một tiêu đề lớn thì đó chính là tên tài liệu — vốn đã
# được đặt làm gốc của cây. Thêm một nhánh nữa cho nó là mọc thêm một cấp thừa,
# mà cây chỉ sâu được 3 cấp. Từ hai cái trở lên thì chúng mới thật sự chia tài
# liệu thành các phần lớn và phải trở thành cấp cao nhất.
MIN_BANNERS = 2

# Cây chỉ mục chỉ sâu tối đa 3 cấp. Từ cấp 4 ("1.1.1.1") trở xuống, đề mục
# không còn là một nhánh nữa mà chỉ là nội dung của mục cha: chia nhỏ tới đó
# thì mỗi nút chỉ còn một hai câu, vector của nó gần như không mang thông tin,
# mà title của chunk lại dài thêm một cấp vô ích.
MAX_TREE_LEVEL = 3

MAX_HEADING_LEN = 250
# Độ dài tối đa của tiêu đề khi đưa vào đường dẫn mục lục
MAX_TITLE_IN_PATH = 90
# Độ dài tối đa của tiêu đề khi *in ra giấy*. Rộng hơn hẳn trần của đường dẫn:
# tiền tố mục lục phải nhường chỗ cho nội dung nên buộc phải cắt bớt, nhưng
# dòng tiêu đề trên trang thì không có ràng buộc đó — cắt nó ở mốc 90 ký tự là
# đứt ngang giữa một cụm từ ("…làm nguyên" / "liệu sản xuất") và mô hình DLA
# đọc ra một tên mục thiếu chữ.
MAX_TITLE_IN_HEADING = 200
# Tên mục (phần trước dấu hai chấm) dài hơn ngần này thì không còn là tên nữa
MAX_NAME_IN_HEADING = 60
# Đề mục không có dấu hai chấm: cả dòng là tên mục, dài hơn ngần này mới coi là
# câu văn mở đầu bằng một con số chứ không phải đề mục
MAX_PLAIN_HEADING_LEN = 160
# Dấu hai chấm ngăn tên mục với nội dung — không tính dấu giữa hai chữ số
_NAME_COLON = re.compile(r"(?<!\d):(?!\d)")

# Hàng bảng đã làm phẳng thành một dòng: "STT: 3.1 | Nội dung: Đối tượng khách
# hàng | Cụ thể: …". Dấu sổ đứng ngăn các cột với nhau.
_FLAT_FIELD = re.compile(r"\s*\|\s*")
# Ô chỉ chứa số thứ tự ("3.1", "02", "-") thì không phải tên mục
_INDEX_VALUE = re.compile(r"^[\d.,/()\-–\s]*$")


def _flat_row_name(title: str, limit: int) -> str | None:
    """Tên mục của một hàng bảng đã làm phẳng, hoặc None nếu không phải hàng bảng.

    Bảng được làm phẳng trước khi đưa vào tool thì mỗi hàng thành một dòng, và
    dòng ấy luôn mở đầu bằng cột số thứ tự ("STT: 3.1"). Cắt tại dấu hai chấm
    đầu tiên như tiêu đề thường sẽ lấy đúng *tên cột* làm tên mục — cả cây chỉ
    mục biến thành một dãy "STT" giống hệt nhau, không còn phân biệt được mục
    nào với mục nào.

    Tên mục thật là giá trị đầu tiên mang nghĩa: bỏ qua cột chỉ chứa số thứ tự
    và cột bỏ trống, rồi ưu tiên giá trị đủ ngắn để làm tên.
    """
    fields = _FLAT_FIELD.split(title)
    if len(fields) < 2:
        return None
    labelled = False
    values = []
    for field in fields:
        label, sep, value = field.partition(":")
        # Bước làm phẳng có lúc rơi mất nhãn của một cột ("Khoản: 3.1 | Điều
        # kiện vay vốn | Quy định:"): cả ô khi ấy chính là giá trị.
        if not sep:
            value = label
        labelled = labelled or bool(sep)
        value = value.strip(" .;-–")
        if value and not _INDEX_VALUE.match(value):
            values.append(value)
    if not labelled:
        return None
    # Hàng nối tiếp của một ô trải dài: mọi cột đều trống, hàng này không có
    # tên. Trả về tên rỗng để mục hiện ra bằng đúng số của nó — lấy đại tên cột
    # làm tên mục thì cây chỉ mục có một dãy "STT" không phân biệt được.
    if not values:
        return ""
    # Cột nội dung dài là thân bài, không phải tên; chỉ dùng khi không còn gì khác
    return next((v for v in values if len(v) <= limit), values[0])


def shorten_title(title: str, limit: int = MAX_TITLE_IN_PATH) -> str:
    """Rút tiêu đề về phần mang nghĩa nhất — chính là *tên* của mục.

    Nhiều mục trong văn bản hành chính viết liền cả nội dung vào dòng tiêu đề
    ("Hợp đồng bảo hiểm: là tất cả văn bản thể hiện sự thỏa thuận giữa Bên mua
    bảo hiểm và Dai-ichi Life Việt Nam…"). Tên mục là phần trước dấu hai chấm;
    phần sau là nội dung và sẽ được đưa xuống thân bài.

    Luật cắt tại dấu hai chấm phải chạy với *mọi* độ dài, không chỉ khi tiêu đề
    vượt trần: một câu dài 80 ký tự vẫn là câu, đặt nguyên nó làm tên mục thì
    cây chỉ mục đọc như văn xuôi và title của chunk sai hẳn.
    """
    title = title.strip()
    flat = _flat_row_name(title, limit)
    if flat is not None:
        title = flat
    # Bỏ qua dấu hai chấm nằm giữa hai chữ số ("8:30", "1:2") — đó là giờ giấc
    # hay tỉ lệ, không phải ranh giới giữa tên mục và nội dung.
    mark = _NAME_COLON.search(title)
    if mark:
        head = title[:mark.start()].strip()
        # tên mục dài quá trần thì dấu hai chấm đó nằm giữa một câu, không phải
        # ranh giới tên
        if head and len(head) <= limit:
            return head
    if len(title) <= limit:
        return title
    cut = title[:limit]
    for sep in (". ", ", ", "; "):
        idx = cut.rfind(sep)
        if idx >= limit // 2:
            return cut[:idx].strip()
    idx = cut.rfind(" ")
    return (cut[:idx] if idx >= limit // 2 else cut).strip() + "…"


def _match_heading(text: str) -> tuple[str, str, int, str] | None:
    """Trả về (số mục, tiêu đề, rank, loại) nếu text trông như một tiêu đề."""
    for pattern, rank, kind in _PATTERNS:
        m = pattern.match(text)
        if m:
            prefix, number = m.group(1), m.group(2)
            # Mã hiệu nội bộ viết liền ("PL02.1003.PCS"), tách ra thành "PL 02"
            # sẽ khiến tra cứu theo mã không khớp nữa.
            if prefix.upper() == "PL":
                num = f"{prefix}{number}".strip()
            else:
                num = f"{prefix} {number}".strip()
            return num, m.group(3).strip(" :.-–"), rank, kind

    m = _DECIMAL.match(text)
    if m:
        num, title = m.group(1), m.group(2)
        depth = num.count(".") + 1
        return num, title.strip(" :.-–"), RANK_DECIMAL + depth - 1, "decimal"

    m = _DECIMAL_ONLY.match(text)
    if m:
        num = m.group(1)
        depth = num.count(".") + 1
        return num, "", RANK_DECIMAL + depth - 1, "decimal"

    m = _ROMAN.match(text)
    if m:
        marker, title = m.group(1), m.group(2).strip(" :.-–")
        rank, kind = _roman_rank(marker)
        return marker, title, rank, kind

    m = _LETTER.match(text)
    if m:
        marker = m.group(1)
        rank = RANK_LETTER_UPPER if marker[:1].isupper() else RANK_LETTER
        return marker, m.group(2).strip(" :.-–"), rank, "letter"

    return None


def _roman_rank(marker: str) -> tuple[int, str]:
    """Cấp của một ký hiệu La Mã — không phải cái nào cũng là cấp sâu.

    "I." "II." trong văn bản hành chính Việt Nam là cấp lớn, đứng trên cả "1.".
    Còn "(i)" "(ii)" là cấp sâu nhất. Riêng "i)" trơ trọi thường chỉ là mục thứ
    9 của danh sách a) b) c)… — xếp nó thành một cấp riêng sẽ đẻ ra một tầng
    giả ngay giữa danh sách.
    """
    token = marker.strip("().")
    if token.isupper() and _IS_ROMAN.match(token):
        return RANK_ROMAN_UPPER, "roman-upper"
    if len(token) > 1 or marker.startswith("("):
        return RANK_ROMAN, "roman"
    return RANK_LETTER, "letter"


def _word_rank(marker: str, list_format: str, word_level: int) -> int:
    """Cấp của một chỉ mục do Word sinh ra.

    Cấp danh sách của Word (`ilvl`) không đáng tin: tài liệu thật hay khai
    danh sách "a) b) c)" ở cùng ilvl với "1. 2. 3.", và khi đó cây mục lục bị
    phẳng ra — mục con thành mục ngang hàng với mục cha. Loại ký hiệu
    (`numFmt`) mới là thứ nói đúng thứ bậc, `ilvl` chỉ dùng để phân cấp *bên
    trong* cùng một loại ký hiệu.
    """
    fmt = (list_format or "").lower()
    offset = max(0, (word_level or 1) - 1)
    if "roman" in fmt:
        return (RANK_ROMAN_UPPER if "upper" in fmt else RANK_ROMAN) + offset
    if "letter" in fmt:
        return (RANK_LETTER_UPPER if "upper" in fmt else RANK_LETTER) + offset
    # Chỉ mục nhiều cấp ("2.1") đã tự nói lên độ sâu của nó
    dots = marker.strip().rstrip(".").count(".")
    return RANK_DECIMAL + max(dots, offset)


def _looks_like_heading(block: Block, text: str, title: str, kind: str,
                        from_word_numbering: bool) -> bool:
    """Lọc bớt trường hợp câu văn thường bị bắt nhầm thành tiêu đề."""
    if _REFERENCE.match(text):
        return False

    # "PHẦN 1", "ĐIỀU 5" rất hay xuất hiện giữa câu dẫn chiếu
    # ("...quy định tại Điều 2.3 Phần 1 này"). Khi trích xuất PDF cắt dòng,
    # mảnh câu đó trông y hệt một tiêu đề và nếu nhận nhầm sẽ nuốt toàn bộ
    # các mục phía sau. Tiêu đề thật luôn có phần tên viết hoa đi kèm.
    if kind in ("part", "chapter", "article", "section", "roman-upper"):
        if not title:
            return False
        first = title.lstrip("“\"'(")[:1]
        if first and first.islower():
            return False
        if not from_word_numbering:
            # Tiêu đề thật trong PDF luôn được nhấn mạnh (in đậm hoặc viết hoa).
            # Câu dẫn chiếu như "Điều 5.4.a, Điều 5.4.b nêu trên" thì không.
            letters = [c for c in title if c.isalpha()]
            mostly_upper = bool(letters) and sum(
                1 for c in letters if c.isupper()) / len(letters) >= 0.8
            if not block.bold and not mostly_upper:
                return False

    if from_word_numbering:
        # Word đã khẳng định đây là một mục có đánh số trong cấu trúc tài liệu.
        # Đây là tín hiệu chắc chắn hơn nhiều so với suy đoán từ hình thức chữ,
        # nhất là với tài liệu không dùng in đậm cho tiêu đề.
        return True

    if len(text) > MAX_HEADING_LEN:
        return False

    # Không có tín hiệu cấu trúc từ file (thường là PDF): phải dựa vào hình thức.
    # Ký hiệu chữ cái/La Mã rất phổ biến trong danh sách liệt kê nên chỉ nhận
    # khi in đậm, tránh băm nhỏ nội dung thành hàng loạt mục vụn.
    if kind in ("letter", "roman") and not block.bold:
        return False

    if kind == "decimal" and not block.bold:
        # Tiêu đề hay viết liền cả nội dung vào cùng dòng ("1.9 Hợp đồng bảo
        # hiểm: là tất cả văn bản thể hiện sự thỏa thuận…"). Đo độ dài của
        # *tên mục* — phần trước dấu hai chấm — chứ đo cả dòng thì mọi tiêu đề
        # kiểu này đều bị loại, và cả nhánh mục con của nó mất theo.
        mark = _NAME_COLON.search(title)
        if mark:
            if len(title[:mark.start()].strip()) > MAX_NAME_IN_HEADING:
                return False
        elif len(text) > MAX_PLAIN_HEADING_LEN:
            # Không có dấu hai chấm thì cả dòng chính là tên mục. Tên mục dài
            # là chuyện thường trong văn bản pháp lý ("15.4 Đối với Khách hàng
            # là doanh nghiệp Việt Nam đưa người lao động Việt Nam đi đào tạo,
            # nâng cao trình độ, kỹ năng nghề ở nước ngoài"); chỉ dòng dài hơn
            # hẳn mức đó mới là câu văn mở đầu bằng một con số.
            return False

    if title and title[:1].islower() and not block.bold and kind == "decimal":
        return False
    return True


def _flat_column_labels(marked: list[tuple]) -> set[str]:
    """Tên các cột của bảng đã làm phẳng, gom từ những hàng còn đủ nhãn.

    Ô bảng trải qua ngắt trang sinh ra hàng nối tiếp chỉ còn trơ tên cột
    ("3.1.2 Quy định:"). Nhìn riêng dòng ấy thì "Quy định" trông y hệt một tên
    mục; chỉ khi đối chiếu với các hàng khác trong cùng tài liệu mới biết đó là
    tên cột, và mục này thật ra không có tên.
    """
    labels: set[str] = set()
    for _block, _num, title, _rank, _kind, _word in marked:
        fields = _FLAT_FIELD.split(title)
        if len(fields) < 2:
            continue
        for field in fields:
            label, sep, _value = field.partition(":")
            if sep and label.strip():
                labels.add(label.strip().lower())
    return labels


def _drop_column_label(title: str, labels: set[str]) -> str:
    """Gỡ tên cột đứng đầu tiêu đề của một hàng nối tiếp bảng làm phẳng.

    Hàng nối tiếp chỉ còn lại đúng một cột ("3.1.9 Quy định: năm/lần ĐVKD thực
    hiện đánh giá lại hạn mức…"). Tên mục không phải là "Quy định" — đó là tên
    cột; phần sau nó là nội dung tràn từ hàng trước, và nếu dài quá thì hàng
    này không có tên nào cả.

    Chỉ xét dòng còn đúng một cột: hàng đủ cột đã có `_flat_row_name` lo.
    """
    if len(_FLAT_FIELD.split(title)) > 1:
        return title
    label, _sep, rest = title.partition(":")
    if label.strip().lower() not in labels:
        return title
    rest = rest.strip(" :.-–")
    return rest if len(rest) <= MAX_NAME_IN_HEADING else ""


_NUMBER_SEQ = re.compile(r"^\d+(?:\.\d+)*\.?$")
# "04 (bốn) Năm hợp đồng đầu tiên" — số viết kèm số 0 đứng đầu là số lượng,
# không phải chỉ mục. Chỉ mục không bao giờ đệm số 0.
_PADDED = re.compile(r"(^|\.)0\d")


def _as_counter(number: str) -> tuple[int, ...] | None:
    """"2.3.1." -> (2, 3, 1). Trả về None nếu không phải chỉ mục dạng số."""
    if not _NUMBER_SEQ.match(number.strip()):
        return None
    return tuple(int(p) for p in number.strip().rstrip(".").split("."))


def _continues_sequence(num: tuple[int, ...], prev: tuple[int, ...] | None) -> bool:
    """Chỉ mục này có nối tiếp được dãy đang mở không?

    Một câu văn bắt đầu bằng số ("30 (ba mươi) ngày tuổi đến 70 tuổi…") trông
    y hệt một đề mục sau khi PDF cắt dòng. Nhận nhầm một dòng như vậy không chỉ
    đẻ ra một mục ma: mọi mục thật phía sau tụt xuống làm con của nó, và title
    của toàn bộ chunk bên dưới sai theo. Số của đề mục thật luôn nối tiếp dãy
    đang mở, số trong câu văn thì không.
    """
    if prev is None:
        return True
    depth = len(num)
    if depth <= len(prev) and num[:depth - 1] == prev[:depth - 1]:
        # cùng một nhánh: đi tiếp (chừa chỗ cho một mục bị trích xuất sót)
        # hoặc mở lại dãy từ đầu
        reference = prev[depth - 1]
        return num[-1] == 1 or reference < num[-1] <= reference + 2
    if depth >= 2 and depth <= len(prev) + 1 and num[-1] == 1:
        # mục con đầu tiên của một nhánh khác — nhánh đó cũng phải nối tiếp
        # được ("2.2.5" rồi tới "2.3.1" khi tiêu đề "2.3" bị trích xuất sót)
        parent = num[:-1]
        return (parent == prev[:len(parent)]
                or _continues_sequence(parent, prev))
    return False


def _drop_broken_sequence(marked: list[tuple]) -> list[tuple]:
    """Loại các đề mục số không nối được vào dãy nào.

    Chỉ soát các chỉ mục đoán từ hình thức. Chỉ mục do Word sinh ra là cấu
    trúc thật của tài liệu, không phải phỏng đoán, nên luôn được giữ.
    """
    out: list[tuple] = []
    prev: tuple[int, ...] | None = None
    for entry in marked:
        _block, num, _title, _rank, kind, from_word = entry
        if kind != "decimal":
            prev = None                        # PHẦN/ĐIỀU/Phụ lục mở dãy mới
            out.append(entry)
            continue
        counter = _as_counter(num)
        if from_word or counter is None:
            prev = counter or prev
            out.append(entry)
            continue
        if _PADDED.search(num) or not _continues_sequence(counter, prev):
            continue
        prev = counter
        out.append(entry)
    return out


def _is_banner(block: Block, body_size: float) -> bool:
    """Dòng này có phải một tiêu đề lớn không đánh số không?

    Ba dấu hiệu phải cùng có mặt: **cỡ chữ lớn hơn hẳn nội dung**, **viết hoa
    gần như toàn bộ** và **không mang chỉ mục nào**. Câu văn thường không bao
    giờ hội đủ cả ba, nên luật này gần như không bắt nhầm — điều quan trọng, vì
    một tiêu đề lớn nhận nhầm sẽ nuốt trọn phần tài liệu đứng sau nó.
    """
    if block.is_table or block.kind == "figure" or block.number:
        return False
    text = clean_text(block.text)
    if not (3 <= len(text) <= MAX_BANNER_LEN):
        return False
    if not body_size or block.size <= body_size:
        return False
    if _match_heading(text):
        return False
    letters = [c for c in text if c.isalpha()]
    if not letters or len(letters) / len(text) < 0.5:
        return False
    # Công thức toán ("L = M *T *R") cũng in cỡ lớn và toàn chữ hoa. Tiêu đề
    # thật là một cụm từ: phải có ít nhất hai từ dài từ hai chữ cái trở lên.
    if sum(1 for w in text.split() if len(w) >= 2 and w.isalpha()) < 2:
        return False
    upper = sum(1 for c in letters if c.isupper()) / len(letters)
    return upper >= BANNER_UPPER_RATIO


def _mark_banners(blocks: list[Block]) -> list[tuple[Block, str, str, int, str, bool]]:
    """Tìm các tiêu đề lớn không đánh số, gộp những dòng liền nhau làm một.

    Tiêu đề lớn hay được xuống dòng giữa chừng ("QUY ĐỊNH" / "SẢN PHẨM TIẾT
    KIỆM LINH HOẠT" ở hai cỡ chữ khác nhau), và bước nối dòng phía trước không
    nối chúng lại vì khác cỡ chữ. Để rời thì cây chỉ mục có hai nhánh cụt thay
    vì một tiêu đề.
    """
    body_size = next((b.meta.get("body_size") for b in blocks
                      if b.meta.get("body_size")), 0.0)
    if not body_size:
        sizes: dict[float, int] = {}
        for b in blocks:
            if b.kind == "para" and not b.is_table and b.size:
                sizes[b.size] = sizes.get(b.size, 0) + len(b.text)
        body_size = max(sizes, key=sizes.get) if sizes else 0.0

    runs: list[list[Block]] = []
    prev_index = -2
    for index, block in enumerate(blocks):
        if not _is_banner(block, body_size):
            continue
        if runs and index == prev_index + 1 and block.page == runs[-1][-1].page:
            runs[-1].append(block)
        else:
            runs.append([block])
        prev_index = index

    if len(runs) < MIN_BANNERS:
        return []

    marked = []
    for run in runs:
        head = run[0]
        title = clean_text(" ".join(b.text for b in run))
        for extra in run[1:]:
            extra.text = ""                # chữ đã dồn lên dòng đầu của tiêu đề
        head.text = title
        marked.append((head, "", title, RANK_BANNER, "banner", False))
    return marked


def detect_headings(blocks: list[Block], source: str) -> list[Block]:
    """Đánh dấu block nào là tiêu đề, gán number/level cho chúng."""
    ranks_seen: set[int] = set()
    # (block, số mục, tên mục, rank, loại, chỉ mục do Word sinh?)
    marked: list[tuple[Block, str, str, int, str, bool]] = []
    banners = {id(entry[0]): entry for entry in _mark_banners(blocks)}

    for block in blocks:
        if block.is_table or block.kind == "figure":
            continue
        banner = banners.get(id(block))
        if banner is not None:
            marked.append(banner)
            ranks_seen.add(RANK_BANNER)
            continue
        text = block.text
        if not text.strip():
            continue
        word_number = block.number      # chỉ mục do Word sinh ra (chỉ có ở DOCX)

        # Tiêu đề gõ tay (PHẦN, ĐIỀU, Phụ lục...) được ưu tiên nhận diện trước,
        # kể cả khi paragraph đó cũng nằm trong một danh sách đánh số.
        hit = _match_heading(text)
        if hit and hit[3] in ("part", "chapter", "appendix", "section", "article"):
            num, title, rank, kind = hit
            if _looks_like_heading(block, text, title, kind, False):
                marked.append((block, num, title, rank, kind, False))
                ranks_seen.add(rank)
                continue

        if word_number:
            # Word đã cho biết chỉ mục -> giữ nguyên định dạng gốc ("1.", "a)",
            # "1.1.") thay vì đoán lại, nhưng thứ bậc thì suy từ ký hiệu.
            rank = _word_rank(word_number, block.meta.get("list_format", ""),
                              block.level or 1)
            if _looks_like_heading(block, text, text, "decimal", True):
                marked.append((block, word_number, text, rank, "decimal", True))
                ranks_seen.add(rank)
            continue

        if not hit:
            continue
        num, title, rank, kind = hit
        if not _looks_like_heading(block, text, title, kind, False):
            continue
        marked.append((block, num, title, rank, kind, False))
        ranks_seen.add(rank)

    marked = _drop_broken_sequence(marked)
    labels = _flat_column_labels(marked)

    # Nén các rank thực sự xuất hiện thành cấp liên tiếp 1,2,3...
    ranks_seen = {rank for _b, _n, _t, rank, _k, _w in marked}
    order = {rank: i + 1 for i, rank in enumerate(sorted(ranks_seen))}

    for block, num, title, rank, kind, _from_word in marked:
        block.kind = "heading"
        block.number = num
        block.level = order[rank]
        block.meta["heading_kind"] = kind
        # Hàng nối tiếp của bảng làm phẳng: tên cột không phải tên mục
        named = _drop_column_label(title, labels) if labels else title
        # Tiêu đề dạng "Phụ lục 01" không có phần tên riêng; lấy block.text làm
        # tiêu đề sẽ thành "Phụ lục 01 Phụ lục 01" trong đường dẫn mục lục.
        if named:
            block.meta["title"] = named
        elif title or clean_text(block.text) == clean_text(num):
            block.meta["title"] = ""
        else:
            block.meta["title"] = block.text
    return blocks


def _section_tokens(section: Section) -> int:
    from .chunker import est_tokens

    return (est_tokens(section.heading_text)
            + sum(est_tokens(b.text) for b in section.blocks))


def _branch_paths(sections: list[Section]) -> set[tuple[str, ...]]:
    """Đường dẫn của những mục còn mục con bên dưới."""
    branches: set[tuple[str, ...]] = set()
    for section in sections:
        for depth in range(1, len(section.path)):
            branches.add(tuple(section.path[:depth]))
    return branches


def _adjacent_in_tree(a: Section, b: Section) -> bool:
    """Hai mục đứng cạnh nhau có cùng thuộc một nhánh không?

    Chỉ mục anh em (cùng mục cha) hoặc quan hệ cha – con mới được gộp; gộp hai
    mục ở hai nhánh khác nhau là trộn hai chủ đề vào một vector.
    """
    return (a.path[:-1] == b.path[:-1]
            or a.path[:-1] == b.path
            or b.path[:-1] == a.path)


def _absorb_section(target: Section, section: Section) -> None:
    """Dồn toàn bộ nội dung của `section` vào cuối `target`."""
    moved = list(section.blocks)
    if section.heading_text:
        # Dòng tiêu đề của mục bị gộp thường là nửa đầu của câu, nửa sau
        # nằm ở khối ngay sau nó — nối lại thay vì để vỡ làm đôi.
        if moved and continues_sentence(section.heading_text, moved[0].text):
            moved[0] = Block(text=f"{section.heading_text} {moved[0].text}",
                             page=moved[0].page, kind=moved[0].kind,
                             is_table=moved[0].is_table, meta=moved[0].meta)
        else:
            moved.insert(0, Block(text=section.heading_text,
                                  page=section.page_start, bold=True))
    target.blocks.extend(moved)
    target.page_end = max(target.page_end, section.page_end)
    if target.path[:-1] == section.path[:-1]:
        _absorb_index(target, section)


def merge_short_sections(sections: list[Section], min_tokens: int,
                         max_tokens: int) -> list[Section]:
    """Gộp mục quá ngắn với mục liền kề để chunk không bị vụn.

    Danh mục định nghĩa kiểu "1.1 … 1.60", mỗi mục một hai dòng, cho ra hàng
    chục chunk chỉ vài chục token — vector của chúng gần như không mang thông
    tin. Gộp lại thì mất nút đó trong cây chỉ mục, nhưng *không mất chữ*: dòng
    tiêu đề được đưa xuống thành đoạn mở đầu của phần nội dung.

    Xét **cả mục trên lẫn mục dưới**, và mỗi vòng chỉ gộp một cặp: lấy mục ngắn
    nhất còn lại rồi ghép nó vào người hàng xóm *nhỏ hơn*. Chỉ nhìn về phía
    trước thì một mục ngắn nằm ngay sau một mục đồ sộ sẽ mắc kẹt vĩnh viễn — đó
    chính là lý do "Điều 1" một trăm token vẫn đứng riêng ngay dưới "MỤC LỤC"
    tám trăm token. Ưu tiên người hàng xóm nhỏ hơn để các mục sau khi gộp dài
    xấp xỉ nhau thay vì một khối phình to bên cạnh mấy mẩu vụn.

    Vòng lặp chạy tới khi không còn cặp nào gộp được, nhờ vậy một mục cha vừa
    nuốt hết mục con của nó sẽ được xét lại — bước gộp một lượt bỏ sót hẳn
    trường hợp này.

    Chỉ mục bị nuốt phải là mục không còn mục con (gộp là xoá mất một nhánh),
    hai mục phải cùng một nhánh, và tổng vẫn phải nằm trong trần token *đã trừ
    tiền tố mục lục* mà mọi chunk phải mang theo.
    """
    if min_tokens <= 0:
        return sections

    out = list(sections)
    size = {id(s): _section_tokens(s) for s in out}

    while True:
        branches = _branch_paths(out)
        best: tuple[int, int, int] | None = None
        for i, section in enumerate(out):
            if section.is_preamble or section.is_banner:
                continue
            if size[id(section)] >= min_tokens:
                continue
            if tuple(section.path) in branches:
                continue
            for j in (i - 1, i + 1):
                if not 0 <= j < len(out):
                    continue
                neighbour = out[j]
                if neighbour.is_preamble or neighbour.is_banner:
                    continue
                if not _adjacent_in_tree(section, neighbour):
                    continue
                left, right = (out[i], out[j]) if j > i else (out[j], out[i])
                # Mục đứng sau là mục bị nuốt: nó phải là mục cụt nhánh, còn
                # mục đứng trước giữ nguyên chỗ của nó trong cây.
                if tuple(right.path) in branches:
                    continue
                sibling = left.path[:-1] == right.path[:-1]
                # Trần 512 token là trần của *chunk*, mà chunk còn mang cả tiền
                # tố mục lục ở đầu. Bỏ quên tiền tố thì mục gộp xong vẫn bị khâu
                # chunk cắt làm đôi — gộp thành công cốc, lại còn đẻ ra hai
                # chunk cùng tên.
                room = max_tokens - _prefix_tokens(left, right if sibling else None)
                if size[id(left)] + size[id(right)] > room:
                    continue
                key = (size[id(neighbour)], i, j)
                if best is None or key < best:
                    best = key
        if best is None:
            return out

        _, i, j = best
        left, right = (out[i], out[j]) if j > i else (out[j], out[i])
        _absorb_section(left, right)
        size[id(left)] += size[id(right)]
        out.remove(right)


def _absorb_index(target: Section, section: Section) -> None:
    """Cộng chỉ mục của mục bị gộp vào mục đích: 1.1 nuốt 1.2 -> "1.1 + 1.2".

    Gộp nội dung mà giữ nguyên chỉ mục cũ thì cây chỉ mục nói dối: chunk mang
    title "1.1 Mục đích" nhưng bên trong có cả phần 1.2. Cộng chỉ mục vào thì
    tra theo số mục vẫn ra đúng chunk đang chứa nó.

    Chỉ cộng khi gộp hai mục ngang hàng. Mục con gộp lên mục cha thì chỉ mục
    của cha vốn đã bao mục con rồi, thêm vào chỉ dài dòng.
    """
    target.number, target.title = _merged_index(target, section)
    target.display_title = _join_names(
        _merged_names(target.display_title, section.display_title),
        MAX_TITLE_IN_HEADING)
    # Đường dẫn của mục này đổi theo; mục con thì không có (chỉ mục không còn
    # con mới được gộp), nên không có nhánh nào bên dưới bị lệch đường dẫn.
    target.path = target.path[:-1] + [target.heading_str]


def _merged_names(kept: str, added: str) -> list[str]:
    names = [t for t in kept.split(" + ") if t and t != _MORE]
    if added:
        names.append(added)
    return list(dict.fromkeys(names))


def _merged_index(target: Section, section: Section) -> tuple[str, str]:
    """Số mục và tên mục của `target` sau khi nó nuốt `section`."""
    numbers = [n for n in dict.fromkeys(target.number.split(" + ")
                                        + [section.number]) if n]
    names = _merged_names(target.title, section.title)
    return " + ".join(numbers), _join_names(names)


def _prefix_tokens(target: Section, absorbing: Section | None) -> int:
    """Số token của tiền tố mục lục mà mọi chunk của `target` phải mang theo."""
    from .chunker import est_tokens

    path = target.path
    if absorbing is not None:
        number, title = _merged_index(target, absorbing)
        path = path[:-1] + [f"{number} {title}".strip()]
    return est_tokens(" > ".join(path))


_MORE = "…"


def _join_names(names: list[str], limit: int = MAX_TITLE_IN_PATH) -> str:
    """Nối tên các mục đã gộp, cắt bớt khi vượt trần độ dài của đường dẫn.

    Số mục thì giữ đủ — đó mới là chỉ mục để tra cứu. Tên chỉ để người đọc hiểu
    mục nói về gì, gộp năm sáu cái lại thì tiền tố của chunk phình lên và ăn
    mất ngân sách token dành cho nội dung.
    """
    out: list[str] = []
    used = 0
    for name in names:
        if out and used + len(name) + 3 > limit:
            out.append(_MORE)
            break
        out.append(name)
        used += len(name) + 3
    return " + ".join(out)


def _demote(block: Block) -> None:
    """Hạ một đề mục xuống thành dòng nội dung thường, giữ nguyên số mục.

    DOCX giữ số mục ở `block.number` chứ không nằm trong text (Word tự sinh khi
    hiển thị), nên phải ghép lại — bỏ đi thì tài liệu dựng lại mất hẳn phần
    đánh số và người đọc không đối chiếu được với bản gốc nữa.
    """
    number = (block.number or "").strip()
    if number and not clean_text(block.text).startswith(number):
        block.text = f"{number} {block.text}".strip()
    block.kind = "para"
    block.number = None
    block.level = None
    block.meta.pop("heading_kind", None)
    block.meta.pop("title", None)


def build_sections(blocks: list[Block]) -> list[Section]:
    """Gom block vào các mục, giữ nguyên thứ tự tài liệu.

    Nội dung thuộc về tiêu đề gần nhất phía trên, bất kể nó nằm ở trang nào —
    đây là cách xử lý trường hợp một mục trải dài qua nhiều trang.
    """
    sections: list[Section] = []
    stack: list[Section] = []          # ngăn xếp tiêu đề tổ tiên đang mở
    ranks: list[int] = []              # cấp suy ra từ ký hiệu, dùng để so sánh cha/con
    preamble: Section | None = None

    for block in blocks:
        if block.kind == "heading":
            level = block.level or 1
            # Phần mở đầu (bìa, lời dẫn) không phải mục cha của bất kỳ mục nào
            if stack and stack[0] is preamble:
                stack.clear()
                ranks.clear()
            while ranks and ranks[-1] >= level:
                stack.pop()
                ranks.pop()

            if len(stack) + 1 > MAX_TREE_LEVEL:
                # Sâu quá mức cây cho phép: dòng này thôi làm nhánh, trở lại
                # thành một dòng nội dung bình thường của mục cha. Số mục được
                # ghép vào text để không mất thông tin đánh số của tài liệu.
                _demote(block)
                if stack:
                    stack[-1].blocks.append(block)
                    for ancestor in stack:
                        ancestor.page_end = max(ancestor.page_end, block.page)
                    continue

            # Tên rỗng là kết luận đã chốt ở khâu nhận diện ("Phụ lục 01" không
            # có tên riêng, hàng nối tiếp của bảng làm phẳng cũng vậy), không
            # phải chỗ thiếu để lấy block.text bù vào.
            full_title = block.meta.get("title", block.text)
            title = shorten_title(full_title)
            shown = shorten_title(full_title, MAX_TITLE_IN_HEADING)
            path = [s.heading_str for s in stack] + [f"{block.number} {title}".strip()]
            section = Section(
                number=block.number or "",
                title=title,
                kind=block.meta.get("heading_kind", ""),
                display_title=shown,
                own_title=shown,
                full_heading=f"{block.number} {full_title}".strip(),
                # cấp hiển thị = độ sâu thật trong cây, để không nhảy cóc
                level=len(stack) + 1,
                path=path,
                page_start=block.page,
                page_end=block.page,
            )
            ranks.append(level)
            sections.append(section)
            stack.append(section)
            continue

        if not sections:
            # nội dung nằm trước tiêu đề đầu tiên (trang bìa, lời mở đầu)
            if preamble is None:
                preamble = Section(number="", title=PREAMBLE_TITLE, level=1,
                                   path=[PREAMBLE_TITLE], page_start=block.page,
                                   page_end=block.page)
                sections.append(preamble)
                stack.append(preamble)
            preamble.blocks.append(block)
            preamble.page_end = max(preamble.page_end, block.page)
            continue

        current = stack[-1] if stack else sections[-1]
        current.blocks.append(block)
        current.page_end = max(current.page_end, block.page)
        # mục cha cũng phải mở rộng phạm vi trang để metadata nhất quán
        for ancestor in stack:
            ancestor.page_end = max(ancestor.page_end, block.page)

    return sections
