"""Classify the images in a document: decorative logo or content figure.

Logos and ornaments repeated at the top or bottom of every page are pure noise
— put them in a chunk and they only dilute the vector. Charts and diagrams, on
the other hand, are real content: they need a placeholder so the section is
known to carry a figure, plus the path of the extracted image file.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field

import fitz

# An image smaller than these is almost certainly a logo, an icon or a rule
MIN_FIGURE_AREA_RATIO = 0.025    # 2.5% of the page area
MIN_FIGURE_SIDE = 70             # pixels, shortest side
MARGIN_TOP = 0.12
MARGIN_BOTTOM = 0.88
# The same image on several pages is branding, not content
REPEAT_PAGES = 2

# A watermark — the hand holding a shield, the leaf, the globe printed faintly
# across the middle of the page — is half a page wide, so it clears every size
# threshold, and it prints only once, so the repeat count misses it too. What it
# does not have is **dark ink**: a watermark has to stay faint for the text on
# top of it to remain readable, while a chart or a flowchart is unreadable
# without dark ink. Measured on real documents: watermarks <= 0.02, every
# content figure >= 0.17.
WATERMARK_INK = 170            # darker than this grey level counts as dark ink
WATERMARK_INK_RATIO = 0.05
WATERMARK_DPI = 72             # lower and anti-aliasing washes out thin strokes
NEAR_WHITE = 247               # brighter than this counts as paper background

# A rounded red frame drawn around a paragraph, a colour band above a heading —
# the ink measure above does not catch these, because it divides by the number
# of *non-paper* pixels: a frame is a few red strokes on a transparent ground,
# so nearly 100% of those pixels are dark ink and it scores 0.67, level with a
# dense chart.
#
# The signal that does separate them is elsewhere: a frame is drawn *under* the
# text, so the page's live text sits on top of it. A content figure is the
# opposite — a chart's labels and the text inside each flowchart box live in the
# image file itself, so that area holds no live characters at all. Measured over
# 60 kept images: every content figure 0 characters, every decorative frame
# 292-523.
TEXT_OVER_IMAGE_CHARS = 40

# Marketing photographs (the fan-shaped family collage on a part's cover page)
# carry no text on top, so the rule above cannot reach them. What separates them
# from a content figure is the **colour count**: charts and flowcharts are drawn
# in flat colour areas, a photograph has continuous gradients. Counting distinct
# colours when the image area is rendered at 48 dpi:
#
#   chart / flowchart / decorative frame     610 - 1,229 colours
#   marketing photograph                  39,832 - 48,968 colours
#
# The threshold sits at 8,000 — 6.5x below the upper group, 5x above the lower.
PHOTO_COLOURS = 8000
PHOTO_DPI = 48
# A scanned page is also one photograph of the whole sheet — it fails the test,
# and dropping it loses the content entirely. When a page has less live text
# than this, its image *is* the page content and is not measured at all.
PAGE_TEXT_FOR_PHOTO_TEST = 200

# The keyword must be followed by an index number or a colon to count as a
# caption. Without either, sentences opening with "Hình thức...", "Ảnh
# hưởng..." would be mistaken for captions. (Keywords are Vietnamese because
# the source documents are.)
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
    reason: str                    # why it was classified that way
    xref: int = 0
    caption: str = ""
    file: str = ""                 # path of the extracted image, when there is one
    meta: dict = field(default_factory=dict)

    @property
    def y(self) -> float:
        return self.bbox[1]

    def placeholder(self) -> str:
        """The placeholder line inserted into the chunk body."""
        label = self.caption or "Figure"
        size = f"{round(self.width)}x{round(self.height)}"
        if self.file:
            return f"[FIGURE: {label} | {size} | {os.path.basename(self.file)}]"
        return f"[FIGURE: {label} | {size}]"


def _find_caption(page: fitz.Page, bbox) -> str:
    """Find the caption line right below (or right above) an image."""
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
    """Share of dark pixels among all pixels that are not paper background.

    The image area is re-read exactly as the eye sees it on the page: a partly
    transparent image is composited over white before being measured, so a
    watermark shows up as the pale grey wash it really is, not as the strong
    original colour stored in the image file.
    """
    rect = fitz.Rect(bbox) & page.rect
    if rect.is_empty:
        return 1.0                    # if it cannot be measured, treat it as a real figure
    try:
        pix = page.get_pixmap(clip=rect, dpi=WATERMARK_DPI, colorspace=fitz.csGRAY)
    except (RuntimeError, ValueError):
        return 1.0
    hist = Counter(pix.samples)
    nonwhite = sum(count for value, count in hist.items() if value < NEAR_WHITE)
    if not nonwhite:
        return 0.0                    # pure white: nothing worth keeping
    dark = sum(count for value, count in hist.items() if value < WATERMARK_INK)
    return dark / nonwhite


def text_over_image(page: fitz.Page, bbox) -> int:
    """How many of the page's *live* characters fall inside the image box.

    Anything above zero means the image is drawn under the text — it is a frame,
    a colour band or a background, not a figure: a content figure carries its own
    text inside the image file.
    """
    rect = fitz.Rect(bbox) & page.rect
    if rect.is_empty:
        return 0
    try:
        return len(page.get_text("text", clip=rect).strip())
    except (RuntimeError, ValueError):
        return 0


def colour_count(page: fitz.Page, bbox) -> int:
    """Distinct colours in the rendered image area — photographs far exceed line art."""
    rect = fitz.Rect(bbox) & page.rect
    if rect.is_empty:
        return 0
    try:
        return page.get_pixmap(clip=rect, dpi=PHOTO_DPI).color_count()
    except (AttributeError, RuntimeError, ValueError):
        return 0                      # unmeasurable: do not risk dropping it


def collect_pdf_images(doc: fitz.Document,
                       treat_first_page_as_cover: bool = False) -> dict[int, list[DocImage]]:
    """Walk the whole document, classify every image and group them by page.

    `treat_first_page_as_cover`: a large image on the first page is almost
    always decorative cover art rather than content. Turn it on when writing the
    cleaned document, so those get dropped.
    """
    # Count the pages each image appears on: many pages = logo/background
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

        # An image covering nearly the whole page while the page still holds
        # plenty of text is a decorative background: the real content is the
        # text, and keeping the image only adds noise.
        covers_page = area_ratio >= 0.85
        text_len = len(page.get_text("text").strip())

        caption = ""
        if covers_page and text_len >= 200:
            kind, reason = "logo", "background image covering the page"
        elif treat_first_page_as_cover and pno == 1 and area_ratio >= MIN_FIGURE_AREA_RATIO:
            kind, reason = "cover", "cover image"
        elif repeats >= REPEAT_PAGES:
            kind, reason = "logo", f"repeated on {repeats} pages"
        elif min(w, h) < MIN_FIGURE_SIDE or area_ratio < MIN_FIGURE_AREA_RATIO:
            kind, reason = "logo", f"too small ({round(w)}x{round(h)})"
        elif in_margin and area_ratio < 0.12:
            kind, reason = "logo", "sits in the page margin"
        else:
            # A caption ("Hình 3: …") is the author declaring this a content
            # figure — take their word for it and skip the measurements.
            caption = _find_caption(page, (x0, y0, x1, y1))
            chars = 0 if caption else text_over_image(page, (x0, y0, x1, y1))
            ink = 1.0 if caption else dark_ink_ratio(page, (x0, y0, x1, y1))
            colours = 0
            if not caption and text_len >= PAGE_TEXT_FOR_PHOTO_TEST:
                colours = colour_count(page, (x0, y0, x1, y1))
            if ink < WATERMARK_INK_RATIO:
                kind, reason = "logo", f"watermark, no dark ink ({ink:.0%})"
            elif chars >= TEXT_OVER_IMAGE_CHARS:
                kind, reason = "logo", f"decorative frame, page text on top ({chars} chars)"
            elif colours >= PHOTO_COLOURS:
                kind, reason = "logo", f"decorative photograph ({colours} colours)"
            else:
                kind, reason = "figure", f"content image ({round(w)}x{round(h)})"

        img = DocImage(
            page=pno, bbox=(x0, y0, x1, y1), width=w, height=h,
            kind=kind, reason=reason, xref=xref,
            caption=caption if kind == "figure" else "",
        )
        out.setdefault(pno, []).append(img)
    return out


# --- vector diagrams ------------------------------------------------------
#
# A process flowchart in a banking document is not an image: it is a mass of
# vector strokes plus the text inside each box. Ordinary extraction pulls all
# that text out and lays it in one meaningless line ("Nội dung Mẫu ĐVKD lưu Tiếp
# nhận và khai báo Kiểm tra, đối chiếu…") — the diagram disappears and only
# scattered words remain.
#
# Tables are drawn with strokes too, so the two have to be told apart. The
# difference: table rules are always horizontal or vertical, while a flowchart
# has **diagonal strokes** (arrows, the sides of a diamond) and **curves**
# (ellipses, rounded corners). Measured on real documents, a table page has
# exactly 0 such strokes and a flowchart page has dozens.
MIN_DIAGRAM_MARKS = 6
# Two strokes closer than this belong to the same figure
DIAGRAM_GAP = 26.0
# Pad the figure box so the outermost stroke is not clipped on export
DIAGRAM_PAD = 6.0
MIN_DIAGRAM_AREA_RATIO = 0.05
MIN_DIAGRAM_SIDE = 90


def _stroke_boxes(page: fitz.Page) -> tuple[list[fitz.Rect], list[fitz.Rect]]:
    """Bounding boxes of every stroke, with the *non*-orthogonal ones kept apart."""
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
    """Merge nearby boxes into contiguous regions."""
    out: list[fitz.Rect] = []
    for box in boxes:
        grown = box + (-gap, -gap, gap, gap)
        touching = [r for r in out if r.intersects(grown)]
        for r in touching:
            out.remove(r)
            box = box | r
        out.append(box)
    # one more pass for regions that only touch after the first merge
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
    """The vector diagrams on a page, each with its bounding box."""
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
            kind="figure", reason=f"vector diagram ({inside} diagonal/curved strokes)",
            meta={"vector": True},
        )
        img.caption = _find_caption(page, img.bbox)
        out.append(img)
    return out


def export_figures(doc: fitz.Document, images: dict[int, list[DocImage]],
                   out_dir: str, doc_id: str) -> int:
    """Export content figures as PNG files for reuse (e.g. by a vision model)."""
    saved = 0
    os.makedirs(out_dir, exist_ok=True)

    # Clear this document's previous images. If an earlier run extracted more
    # figures (because a threshold changed), the leftovers would linger and
    # confuse whoever reads the folder.
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
