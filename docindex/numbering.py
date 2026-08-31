"""Render Word's automatic numbering (numbering.xml).

This is the crux of DOCX extraction: when Word numbers a list automatically,
the "1.", "2.1.", "a)" markers are NOT in the paragraph text — Word generates
them at display time. Reading the file with plain python-docx loses every
section number, and the RAG stack is left with no hierarchy signal at all.
This module rebuilds those strings following the OOXML rules.
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
    """1 -> a, 26 -> z, 27 -> aa (Word's alphabetic numbering)."""
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


# The formats that count as "numbered" (as opposed to bullet/none)
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
    """Walks the document in order and hands back a number for each paragraph.

    `number_for()` must be called in the order the paragraphs appear, because
    the counters are accumulated state (exactly how Word renders them).
    """

    def __init__(self, document):
        self._levels: dict[str, dict[int, _Level]] = {}   # abstractNumId -> {ilvl: _Level}
        self._num_to_abstract: dict[str, str] = {}
        self._overrides: dict[str, dict[int, int]] = {}   # numId -> {ilvl: startOverride}
        self._counters: dict[str, dict[int, int]] = {}    # abstractNumId -> {ilvl: current value}
        self._seen_num: set[str] = set()
        self._load(document)

    def _load(self, document) -> None:
        try:
            part = document.part.numbering_part
        except (KeyError, AttributeError, ValueError):
            return  # the document carries no numbering part
        root = part.element

        for an in root.findall(qn("w:abstractNum")):
            aid = an.get(qn("w:abstractNumId"))
            lv: dict[int, _Level] = {}
            for lvl_el in an.findall(qn("w:lvl")):
                lvl = _Level(lvl_el)
                lv[lvl.ilvl] = lvl
            self._levels[aid] = lv
            # an abstractNum may point at another one through numStyleLink
            link = _val(an, "w:numStyleLink", None)
            if link:
                self._levels[aid] = lv  # keep as-is; resolved below when empty

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
        """Advance the counters and return (number string, format).

        Returns (None, fmt) for bullets and unnumbered paragraphs.
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

        # Apply startOverride the first time this numId shows up
        if num_id not in self._seen_num:
            self._seen_num.add(num_id)
            for il, start in self._overrides.get(num_id, {}).items():
                counters[il] = start - 1

        if lvl.fmt in ("bullet", "none"):
            # bullets still reset deeper levels, or the numbers below drift
            self._reset_deeper(counters, levels, ilvl)
            return None, lvl.fmt

        cur = counters.get(ilvl)
        counters[ilvl] = lvl.start if cur is None else cur + 1
        self._reset_deeper(counters, levels, ilvl)

        return self._render(lvl, counters, levels), lvl.fmt

    @staticmethod
    def _reset_deeper(counters: dict[int, int], levels: dict[int, _Level], ilvl: int) -> None:
        """Deeper levels restart when a parent level advances (unless lvlRestart=0)."""
        for il in list(counters):
            if il > ilvl:
                deeper = levels.get(il)
                if deeper is not None and deeper.restart == 0:
                    continue
                counters.pop(il, None)

    @staticmethod
    def _render(lvl: _Level, counters: dict[int, int], levels: dict[int, _Level]) -> str:
        """Substitute %1..%9 in lvlText with the matching counter values."""
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
                # parent level never ran -> use its start value so no empty number appears
                value = ldef.start if ldef is not None else 1
            else:
                value = counters[idx]
            fmt = ldef.fmt if ldef is not None else "decimal"
            out = out.replace(ph, _format_counter(value, fmt))
        return out.strip()
