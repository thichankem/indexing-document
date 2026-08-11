"""Render chỉ mục tự động của Word (numbering.xml).

Đây là mấu chốt: khi Word đánh số tự động, các số "1.", "2.1.", "a)" KHÔNG nằm
trong text của paragraph mà được Word sinh ra lúc hiển thị. Đọc file bằng
python-docx thuần sẽ mất sạch chỉ mục -> RAG không còn tín hiệu phân cấp.
Module này dựng lại đúng chuỗi số đó theo quy tắc OOXML.
"""
from __future__ import annotations

from docx.oxml.ns import qn

_ROMAN = [
    (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"),
    (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
    (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
]


def _to_roman(n: int) -> str:
    if n <= 0:
        return str(n)
    out = []
    for val, sym in _ROMAN:
        while n >= val:
            out.append(sym)
            n -= val
    return "".join(out)


def _to_letter(n: int) -> str:
    """1 -> a, 26 -> z, 27 -> aa (kiểu bảng chữ cái Word)."""
    if n <= 0:
        return str(n)
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("a") + rem) + out
    return out


def _format_counter(value: int, fmt: str) -> str:
    if fmt == "decimal":
        return str(value)
    if fmt == "decimalZero":
        return f"{value:02d}"
    if fmt == "lowerLetter":
        return _to_letter(value)
    if fmt == "upperLetter":
        return _to_letter(value).upper()
    if fmt == "lowerRoman":
        return _to_roman(value)
    if fmt == "upperRoman":
        return _to_roman(value).upper()
    return str(value)


# Các định dạng được coi là "có đánh số" (khác bullet/none)
NUMERIC_FORMATS = {
    "decimal", "decimalZero", "lowerLetter", "upperLetter",
    "lowerRoman", "upperRoman",
}


class _Level:
    __slots__ = ("ilvl", "fmt", "text", "start", "restart", "pstyle")

    def __init__(self, el):
        self.ilvl = int(el.get(qn("w:ilvl")) or 0)
        self.fmt = _val(el, "w:numFmt", "decimal")
        self.text = _val(el, "w:lvlText", "")
        self.start = int(_val(el, "w:start", "1") or 1)
        r = _val(el, "w:lvlRestart", None)
        self.restart = int(r) if r is not None else None
        self.pstyle = _val(el, "w:pStyle", None)


def _val(parent, tag: str, default=None):
    el = parent.find(qn(tag))
    if el is None:
        return default
    v = el.get(qn("w:val"))
    return default if v is None else v


class NumberingResolver:
    """Duyệt tài liệu theo thứ tự và trả về chỉ mục cho từng paragraph.

    Phải gọi `number_for()` theo đúng thứ tự xuất hiện trong tài liệu vì
    bộ đếm là trạng thái tích luỹ (giống cách Word render).
    """

    def __init__(self, document):
        self._levels: dict[str, dict[int, _Level]] = {}   # abstractNumId -> {ilvl: _Level}
        self._num_to_abstract: dict[str, str] = {}
        self._overrides: dict[str, dict[int, int]] = {}   # numId -> {ilvl: startOverride}
        self._counters: dict[str, dict[int, int]] = {}    # abstractNumId -> {ilvl: giá trị hiện tại}
        self._seen_num: set[str] = set()
        self._load(document)

    def _load(self, document) -> None:
        try:
            part = document.part.numbering_part
        except (KeyError, AttributeError, ValueError):
            return  # tài liệu không có numbering
        root = part.element

        for an in root.findall(qn("w:abstractNum")):
            aid = an.get(qn("w:abstractNumId"))
            lv: dict[int, _Level] = {}
            for lvl_el in an.findall(qn("w:lvl")):
                lvl = _Level(lvl_el)
                lv[lvl.ilvl] = lvl
            self._levels[aid] = lv
            # abstractNum có thể trỏ tới abstractNum khác qua numStyleLink
            link = _val(an, "w:numStyleLink", None)
            if link:
                self._levels[aid] = lv  # giữ nguyên, sẽ resolve ở dưới nếu rỗng

        for num in root.findall(qn("w:num")):
            nid = num.get(qn("w:numId"))
            ab = num.find(qn("w:abstractNumId"))
            if ab is None:
                continue
            self._num_to_abstract[nid] = ab.get(qn("w:val"))
            ov: dict[int, int] = {}
            for o in num.findall(qn("w:lvlOverride")):
                il = int(o.get(qn("w:ilvl")) or 0)
                so = o.find(qn("w:startOverride"))
                if so is not None:
                    ov[il] = int(so.get(qn("w:val")) or 1)
            if ov:
                self._overrides[nid] = ov

    def has_numbering(self) -> bool:
        return bool(self._num_to_abstract)

    def level_def(self, num_id: str | int, ilvl: int) -> _Level | None:
        aid = self._num_to_abstract.get(str(num_id))
        if aid is None:
            return None
        return self._levels.get(aid, {}).get(ilvl)

    def number_for(self, num_id: str | int | None, ilvl: int | None) -> tuple[str | None, str]:
        """Tăng bộ đếm và trả về (chuỗi chỉ mục, định dạng).

        Trả về (None, fmt) nếu là bullet hoặc không đánh số.
        """
        if num_id is None:
            return None, "none"
        num_id = str(num_id)
        ilvl = int(ilvl or 0)
        aid = self._num_to_abstract.get(num_id)
        if aid is None:
            return None, "none"
        levels = self._levels.get(aid) or {}
        lvl = levels.get(ilvl)
        if lvl is None:
            return None, "none"

        counters = self._counters.setdefault(aid, {})

        # Áp startOverride lần đầu numId này xuất hiện
        if num_id not in self._seen_num:
            self._seen_num.add(num_id)
            for il, start in self._overrides.get(num_id, {}).items():
                counters[il] = start - 1

        if lvl.fmt in ("bullet", "none"):
            # bullet vẫn phải reset cấp con để chỉ mục cấp dưới không bị trôi
            self._reset_deeper(counters, levels, ilvl)
            return None, lvl.fmt

        cur = counters.get(ilvl)
        counters[ilvl] = lvl.start if cur is None else cur + 1
        self._reset_deeper(counters, levels, ilvl)

        return self._render(lvl, counters, levels), lvl.fmt

    @staticmethod
    def _reset_deeper(counters: dict[int, int], levels: dict[int, _Level], ilvl: int) -> None:
        """Cấp con quay về mốc ban đầu khi cấp cha tăng (trừ khi lvlRestart=0)."""
        for il in list(counters):
            if il > ilvl:
                deeper = levels.get(il)
                if deeper is not None and deeper.restart == 0:
                    continue
                counters.pop(il, None)

    @staticmethod
    def _render(lvl: _Level, counters: dict[int, int], levels: dict[int, _Level]) -> str:
        """Thay %1..%9 trong lvlText bằng giá trị bộ đếm tương ứng."""
        text = lvl.text or ""
        if not text:
            return ""
        out = text
        for i in range(9, 0, -1):
            ph = f"%{i}"
            if ph not in out:
                continue
            idx = i - 1
            ldef = levels.get(idx)
            if counters.get(idx) is None:
                # cấp cha chưa từng chạy -> dùng giá trị start để không sinh số rỗng
                value = ldef.start if ldef is not None else 1
            else:
                value = counters[idx]
            fmt = ldef.fmt if ldef is not None else "decimal"
            out = out.replace(ph, _format_counter(value, fmt))
        return out.strip()
