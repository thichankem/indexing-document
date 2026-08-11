"""Kiểu dữ liệu dùng chung cho toàn bộ pipeline."""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Any


# Ký tự vô hình hay lẫn vào file PDF xuất từ Word (zero-width space, BOM, non-breaking space)
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍﻿­"), None)


# Ký hiệu toán học và dấu câu kiểu chữ mà hệ RAG đọc ra thành ký tự lạ. Quy về
# chữ hoặc dấu ASCII tương đương: "∑" đọc thành "Tổng" thì câu vẫn hiểu được,
# còn để nguyên thì khâu tokenize trả về một ký tự không có trong từ điển.
_SYMBOL_WORDS = {
    "∑": " Tổng ", "∏": " Tích ", "√": " căn ", "∞": " vô cùng ",
    "≤": "<=", "≥": ">=", "≠": "!=", "≈": "~=", "±": "+/-",
    "×": "x", "÷": "/", "∗": "*", "→": "->", "⇒": "=>",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-", "―": "-",
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "‘": "'", "’": "'", "‚": "'", "′": "'", "″": '"',
    "•": "-", "▪": "-", "●": "-", "◦": "-", "○": "-",
}
_SYMBOL_MAP = {ord(k): v for k, v in _SYMBOL_WORDS.items()}

# Font Symbol/Wingdings nhúng trong Word đẩy glyph vào vùng dùng riêng U+F0xx,
# giữ nguyên vị trí mã ASCII của chúng — "U+F02B" vì thế chính là dấu "+".
# Chỉ nhận lại dấu câu và chữ số: vùng chữ cái là bảng chữ Hy Lạp hoặc hoạ tiết
# Wingdings, dịch sang chữ Latin sẽ ra một từ không hề có trong văn bản.
_PUA_EXTRA = {0xF0B7: "-", 0xF0A7: "-", 0xF0D8: "->", 0xF0E0: "->"}


def _from_symbol_font(code: int) -> str:
    if code in _PUA_EXTRA:
        return _PUA_EXTRA[code]
    if 0xF020 <= code <= 0xF07E:
        ch = chr(code - 0xF000)
        return ch if not ch.isalpha() else " "
    return " "


def normalize_symbols(s: str) -> str:
    """Quy các ký tự đặc biệt về dạng chữ thường đọc được.

    Công thức trong văn bản ngân hàng được soạn bằng Equation Editor nên các
    biến nằm ở khối Mathematical Alphanumeric Symbols ("𝐌𝐢" chứ không phải
    "Mi"). Hệ RAG không có font cho khối đó nên đọc ra thành "$Mi$" hoặc bỏ
    hẳn — phải hạ về chữ Latin thường ngay từ khâu làm sạch.

    `NFKC` lo phần lớn việc đó (chữ toán học, chữ số toàn rộng, ligature, dấu
    cách không ngắt); phần còn lại là ký hiệu toán và dấu câu kiểu chữ, dịch
    bằng bảng tra ở trên.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    if any(0xE000 <= ord(ch) <= 0xF8FF for ch in s):
        s = "".join(_from_symbol_font(ord(ch))
                    if 0xE000 <= ord(ch) <= 0xF8FF else ch for ch in s)
    return s.translate(_SYMBOL_MAP)


def clean_text(s: str) -> str:
    """Chuẩn hoá text: bỏ ký tự vô hình, quy ký hiệu lạ, gộp khoảng trắng thừa."""
    if not s:
        return ""
    s = s.translate(_INVISIBLE)
    s = normalize_symbols(s)
    s = s.replace(" ", " ").replace("\t", " ")
    s = re.sub(r"[ ]{2,}", " ", s)
    return s.strip()


def rows_to_markdown(rows: list[list[str]]) -> str:
    """Dựng bảng markdown từ các hàng, bỏ những cột rỗng hoàn toàn.

    Ô gộp trong Word/PDF được trích ra thành nhiều cột trống liền nhau
    ("| | Năm hợp đồng | | |"), làm bảng khó đọc và tốn chỗ vô ích trong chunk.
    """
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return ""

    width = max(len(r) for r in rows)
    rows = [list(r) + [""] * (width - len(r)) for r in rows]
    keep = [i for i in range(width) if any(r[i].strip() for r in rows)]
    if not keep:
        return ""
    rows = [[r[i] for i in keep] for r in rows]

    out = ["| " + " | ".join(rows[0]) + " |",
           "| " + " | ".join(["---"] * len(keep)) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


@dataclass
class Block:
    """Một đơn vị nội dung nhỏ nhất lấy ra từ file (1 đoạn văn, 1 dòng, hoặc 1 bảng)."""

    text: str
    page: int                      # số trang (1-based)
    kind: str = "para"             # para | heading | table | caption
    number: str | None = None      # chỉ mục đã render, vd "1.", "2.1.", "a)"
    level: int | None = None       # cấp heading (1 = cao nhất)
    bold: bool = False
    size: float = 0.0
    is_table: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


PREAMBLE_TITLE = "Phần mở đầu"

_ENDS_SENTENCE = re.compile(r"[.:;!?]\s*$")
# Dòng mở đầu một ý mới: gạch đầu dòng, "a)", "(i)", "1.2."
_STARTS_ITEM = re.compile(
    r"^\s*([-–—•+*]|\(?[a-zA-Zđ][.)]\s|\(?[ivx]+[.)]\s|\d+(\.\d+)*[.)]?\s)")


def continues_sentence(lead: str, follow: str) -> bool:
    """`follow` có phải nửa sau của câu còn dở trong `lead` không?

    PDF lưu theo dòng hiển thị nên một câu hay nằm rải ở hai khối. Không nhận
    ra thì đoạn văn vỡ làm đôi ngay giữa câu. Không xét chữ hoa đầu dòng: nửa
    sau rất hay bắt đầu bằng tên riêng ("…Dai-ichi Life Việt" / "Nam được…").
    """
    if not lead or not follow:
        return False
    if _ENDS_SENTENCE.search(lead):
        return False
    return not _STARTS_ITEM.match(follow)


def document_title(path: str) -> str:
    """Tên tài liệu suy từ tên file, dùng làm gốc của cây chỉ mục.

    Hệ RAG lấy tiêu đề tài liệu làm cấp cao nhất trong title của chunk, nên
    bản dựng lại phải có sẵn dòng đó. File đi qua bước chuyển đổi hay mang
    đuôi kép ("quy-dinh.docx.pdf"), phải gỡ hết mới ra tên thật.
    """
    name = os.path.basename(path)
    for _ in range(2):
        stem, ext = os.path.splitext(name)
        if ext.lower() not in (".pdf", ".docx", ".doc"):
            break
        name = stem
    return clean_text(name.replace("_", " ")).strip()


@dataclass
class Section:
    """Một mục trong cây tài liệu."""

    number: str                    # "2.1."
    title: str                     # "Hội viên gắn kết" (đã rút gọn cho đường dẫn)
    level: int
    path: list[str]                # đường dẫn mục lục từ gốc tới đây
    kind: str = ""                 # loại tiêu đề: decimal | article | banner | …
    blocks: list[Block] = field(default_factory=list)
    page_start: int = 0
    page_end: int = 0
    # Dòng tiêu đề nguyên văn. Tiêu đề dài bị rút gọn khi đưa vào đường dẫn,
    # nhưng nội dung chunk phải giữ đủ chữ, nếu không là mất dữ liệu.
    full_heading: str = ""
    # Tên mục dùng cho dòng tiêu đề *in ra giấy*: rộng hơn `title` vì không
    # phải chia ngân sách token với nội dung như tiền tố mục lục.
    display_title: str = ""
    # Tên riêng của chính mục này, không đổi khi nó nuốt mục ngang hàng. Dùng
    # để nhận ra phần chữ đã nằm trên dòng tiêu đề và không in lại lần nữa.
    own_title: str = ""

    @property
    def path_str(self) -> str:
        return " > ".join(self.path)

    @property
    def heading_str(self) -> str:
        return f"{self.number} {self.title}".strip()

    @property
    def display_heading(self) -> str:
        """Dòng tiêu đề để in ra: số mục + tên mục ở độ dài đầy đủ."""
        return f"{self.number} {self.display_title or self.title}".strip()

    @property
    def heading_text(self) -> str:
        """Dòng tiêu đề đầy đủ để đưa vào nội dung chunk."""
        return self.full_heading or self.heading_str

    @property
    def is_merged(self) -> bool:
        """Mục này đã nuốt chỉ mục của mục ngang hàng liền sau ("1.1 + 1.2").

        Khi ấy `heading_str` gom tên của nhiều tiêu đề, còn `full_heading` vẫn
        là dòng tiêu đề gốc của riêng mục này — chữ của mục bị nuốt nằm ở thân
        bài, không nằm trên dòng tiêu đề.
        """
        return " + " in self.number

    @property
    def is_banner(self) -> bool:
        """Tiêu đề lớn không đánh số — một vách ngăn giữa các phần của tài liệu.

        Nó không phải một chủ đề, chỉ là cái khung chứa các mục thật bên dưới,
        nên không được gộp nội dung của mục khác vào (và ngược lại): làm vậy là
        đem nội dung của một mục có tên tuổi bỏ vào một nút không có chỉ mục
        nào để tra cứu.
        """
        return self.kind == "banner"

    @property
    def is_preamble(self) -> bool:
        """Phần nội dung nằm trước tiêu đề đầu tiên — không phải một mục thật.

        Tên "Phần mở đầu" do tool đặt ra để gom trang bìa và lời dẫn; nó không
        có trong tài liệu nên không được vẽ thành tiêu đề, nếu không cây chỉ
        mục của hệ RAG sẽ mọc thêm một nhánh không tồn tại.
        """
        return not self.number and self.title == PREAMBLE_TITLE


@dataclass
class Chunk:
    """Đơn vị cuối cùng đưa vào embedding."""

    chunk_id: str
    doc_id: str
    source_file: str
    text: str                      # nội dung đã gắn tiền tố section_path
    raw_text: str                  # nội dung gốc, không tiền tố
    section_number: str
    section_title: str
    section_path: str
    section_level: int
    page: int
    page_source: str               # actual | estimated
    is_continued: bool             # mục này còn tiếp ở chunk sau
    is_continuation: bool          # chunk này là phần tiếp của mục từ trước
    part_index: int                # thứ tự phần trong cùng một mục
    part_total: int
    prev_chunk_id: str | None
    next_chunk_id: str | None
    char_count: int
    est_tokens: int
    has_table: bool
    has_figure: bool = False
    figures: list[dict] = field(default_factory=list)   # hình nằm trong chunk này
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
