<div align="center">

# docindex

**Clean PDF/DOCX documents and chunk them along their outline, for RAG.**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-pytest-orange)](tests/)

</div>

---

`docindex` takes a messy real-world document — a bank's product regulation, a
legal circular, an insurance policy — and turns it into two things a retrieval
pipeline can actually use:

1. **A cleaned, rebuilt document** (`.pdf` by default). Content flows
   continuously like an ordinary document, every section name alone on its line
   at a size that shrinks with depth. Logos, cover art, headers, footers and the
   table of contents are gone; the figures that are real content stay. *This is
   the file you load into RAG.*
2. **A `.jsonl` chunk file**, if you are not using a layout-analysis model —
   every chunk inside **512 tokens**, inside **one page**, and belonging to
   **one section**.

```
output/
  Quy định ABC_formalized.pdf   ← the clean file to load into RAG
  Quy định ABC.jsonl            ← chunks for embedding
  Quy định ABC.outline.txt      ← the outline tree, for review
  figures/                      ← figures extracted for embedding into the rebuild
  report.json                   ← per-document quality stats and outline
```

> **Note on language.** The tool's code, interface and documentation are in
> English. Its *heading grammar* is Vietnamese, because it was built for
> Vietnamese administrative and legal documents (`PHẦN`, `CHƯƠNG`, `ĐIỀU`,
> `Phụ lục`, `MỤC`). Those keywords are part of the input format, not UI text.

---

## Why this exists

Most RAG failures on institutional documents are not retrieval failures — they
are *ingestion* failures. A document arrives as a scanned-looking PDF where the
logo is glued to the text, the outline is invisible to any layout model, half
the spaces are `U+00A0`, and the flowchart on page 9 has been mangled into one
long meaningless sentence.

`docindex` is built for a RAG stack that reads documents **by their outline
hierarchy** (Layout Analysis → Reconstruction → Chunking), **at most 3 levels
deep**, with a **512-token chunk ceiling**. Such a stack derives its own chunk
boundaries from the tree it reads off the printed page. So the tool's job is to
make that tree **impossible to misread**:

- headings are presented as headings — bold, sized by level, alone on their line
  — so a DLA model labels them `title` rather than `text` or `list-item`;
- the tree is re-derived from the section *numbers themselves*, not from Word's
  list levels, and phantom sections born from sentences that happen to start
  with a digit are rejected;
- the document title leads, as the root of the tree, because every chunk title
  starts there;
- a heading is never stranded at the foot of a page, nor split across two;
- exotic glyphs are folded to readable letters (`𝐌𝐢` → `Mi`, `∑` → `Tổng`), and
  vector flowcharts stay pictures instead of being mined for text;
- output defaults to **`.pdf`**, the format a DLA model reads most reliably.

---

## Install

```bash
pip install PyMuPDF python-docx tkinterdnd2
```

Python 3.10+. `tkinterdnd2` is optional — it only adds drag and drop to the GUI.

Or, from a clone:

```bash
pip install -r requirements.txt
```

---

## Usage

### GUI

Double-click the **`docindex` shortcut on your Desktop**. If you do not have one
yet, double-click `scripts/create-shortcut.bat` to create it.

Or run `scripts/docindex-gui.bat`, or:

```bash
python -m docindex.gui
```

Drop `.pdf`/`.docx` files (or a whole folder) into the large area at the top,
adjust the options, and press **Start processing**. The window reports progress
per document: chunk count, outline depth, median and maximum tokens, logos
dropped, figures kept, and quality warnings.

Press **View outline tree** when it finishes: that tree is what every chunk
title is built from, so it is the thing to check against the original before you
load anything into a knowledge base.

A bad file does not stop the batch — the rest keeps going and the error is
logged.

#### The "Document cleaning" panel

Pick **exactly what you want stripped**, rather than all-or-nothing. The
**Cleaning level** dropdown holds four presets; ticking a box by hand switches it
to *Custom*. The small line at the bottom always spells out what this run will
strip.

| Cleaning level | What it strips |
|---|---|
| **Logo and cover art only** (default) | Logos, repeated ornaments, background images, first-page cover art. Text stays exactly where it was; the original layout is untouched |
| Also headers and footers | The above, plus header and footer text |
| Full clean, keep layout | The above, plus the table of contents and TOC-only pages |
| Rebuild layout for RAG | Rebuilds the document from the outline — no logos, headers, footers or TOC |

Ticking **Rebuild the layout for a RAG stack** greys out the four strip boxes:
the content then comes from the extracted outline, where those were already
dropped, so there is nothing left to control. Only **Strip cover art** still
applies. **Rebuilt document format** (default `.pdf`) and **One page per
section** are available in this mode only.

Drag and drop needs `tkinterdnd2`. Without it, the drop area changes colour and
says so plainly — the GUI still works through **Choose files…** / **Choose
folder…**. To enable it:

```bash
pip install tkinterdnd2
```

Close and reopen the GUI afterwards. If you launch from the Desktop shortcut,
install into the Python that shortcut uses (right-click → *Properties* → the
*Target* field).

### Command line

```bash
python -m docindex.cli "input folder" -o output
```

One file:

```bash
python -m docindex.cli "input folder/Quy dinh.docx" -o output
```

Every document merged into a single chunk file:

```bash
python -m docindex.cli "input folder" -o output --merge
```

Print each document's outline tree to check against the original:

```bash
python -m docindex.cli "input folder" -o output --outline
```

Strip **only the logo and cover art**, leaving the text exactly in place:

```bash
python -m docindex.cli doc.pdf -o output --keep-layout --keep-header-footer --keep-toc --no-jsonl
```

### Options

| Flag | Default | Meaning |
|---|---|---|
| `--max-tokens` | 512 | Token ceiling of one chunk, outline prefix already deducted (and of one page under `--page-per-section`) |
| `--min-tokens` | 200 | Below this, a section is merged into a neighbour |
| `--keep-short-sections` | off | Keep short sections instead of merging them |
| `--overlap` | 1 | Sentences repeated when a section is split across pages (0 = off) |
| `--no-prefix` | off | Do not prepend the outline path to the chunk text |
| `--path-depth` | 4 | Outline levels kept in the prefix |
| `--format` | pdf | Rebuild format: `pdf`, `docx`, `same` (as the source) |
| `--page-per-section` | off | One page per section, each page ≤ `--max-tokens` |
| `--outline` | off | Print each document's outline tree |
| `--keep-layout` | off | Keep the original layout instead of rebuilding |
| `--merge` | off | Merge every document into `chunks.jsonl` |
| `--preview N` | 0 | Print the first N chunks of each document |
| `--no-clean` | off | Do not write the cleaned document |
| `--no-jsonl` | off | Only clean the document, produce no chunks |
| `--keep-cover` | off | Keep the cover image on the first page |
| `--keep-logo` | off | Keep logos and repeated ornaments (only with `--keep-layout`) |
| `--keep-header-footer` | off | Keep header/footer text (only with `--keep-layout`) |
| `--keep-toc` | off | Keep the table of contents (only with `--keep-layout`) |
| `--extract-figures` | off | Export figures as PNG (the rebuild always does this anyway) |

---

## The outline tree — the part that matters most

A RAG stack builds every chunk title from the outline, so **one wrong level here
is a wrong title on every chunk below it.** Six rules govern the tree:

| Rule | Why |
|---|---|
| Hierarchy comes from the **marker**, not Word's list level | Real documents routinely declare `a) b) c)` at the same `ilvl` as `1. 2. 3.`; trust that and a child becomes a sibling of its parent. Only `numFmt` (decimal / lowerLetter / upperRoman) states the true hierarchy |
| `I. II. III.` is a high level, `(i) (ii)` the deepest, a bare `i)` stays inside the letter list | `I. THÔNG TIN CHUNG` ranks above `10. Điều kiện`; an `i)` among `a) b) c)…` is just item nine, and splitting it out invents a tier |
| A heading's number must **continue the open sequence** | `30 (ba mươi) ngày tuổi đến 70 tuổi…` is prose the PDF wrapped, not section 30. Accept it and every real section after it drops down to become its child |
| A zero-padded number (`04`) is a **quantity**, not a section number | `04 (bốn) Năm hợp đồng đầu tiên` |
| The name is the part **before the colon**; the rest is content | Administrative documents run whole sentences into the heading line. Left alone, the sentence is set in bold at heading size and the chunk title reads like prose |
| **Unnumbered banner headings** become the top level, when a document has two or more | Two banners divide a document into two major parts. Miss them and the first part's `1. …` sits level with the second part's `Điều 1`, and the tree loses its top level |

The full spec, with every threshold and its justification, is in
**[docs/NORMALIZATION-RULES.md](docs/NORMALIZATION-RULES.md)**.

Across a corpus of 9 real documents, these rules eliminated 8 phantom sections
and pulled one document back from 5 spurious levels to its true depth.

### The tree stops at level 3

From level 4 (`1.1.1.1`) down, a heading **stops being a branch** and returns to
being ordinary content of its parent — the number stays in the text, so no
numbering information is lost. Split that far and each node holds one or two
sentences: its vector is near-meaningless, and the chunk title grows a useless
level.

### Short sections: content *and* numbers are merged

A section under `--min-tokens` (default **200**) with no children is **merged
with a neighbour** — its parent, or the sibling immediately above *or below* —
provided the result still fits in 512 tokens **including the outline prefix**
every chunk carries. If there is no room, they stay apart. A chunk of one or two
hundred tokens carries almost nothing; merging loses that node from the tree but
**loses no words**: the heading line moves down to open the body.

How the partner is chosen decides the quality:

- **Look both ways.** Looking only backwards strands a short section forever
  behind a huge one — a hundred-token `Điều 1` standing alone only because an
  eight-hundred-token `MỤC LỤC` precedes it, while `Điều 2` just below has room.
- **Prefer balance.** Each round takes the shortest remaining section and joins
  it to its *smaller* neighbour, so merged sections come out roughly equal
  instead of one bloated block beside a handful of scraps.
- **Repeat until dry.** A parent that has just absorbed all its children becomes
  a leaf itself and is reconsidered; a single-pass merge misses that case
  entirely.

The threshold sits deliberately close to half the ceiling — measured over 18 real
documents:

| `--min-tokens` | chunks | median | chunks < 200 tokens |
|---|---|---|---|
| 60 | 1142 | 273 | 389 |
| **200** | **890** | **402** | **121** |
| 512 | 877 | 410 | 121 |

Past 200 almost nothing changes, because by then the 512 ceiling is what binds.

A small section still standing alone means one of four things: it **has
children** (merging would delete a branch), the **two neighbours are in different
branches**, **merging would exceed 512**, or the neighbour is the preamble or a
banner. A parent holding nothing but its heading line never becomes a chunk of
its own — it travels with its first child.

Merging two siblings **adds up their numbers**:

```
1.1 Mục đích cho vay  +  1.2 Loại tiền cho vay   ->   1.1 + 1.2 Mục đích cho vay + Loại tiền cho vay
```

Keeping the old number alone would make the tree lie — a chunk titled `1.1` that
also holds `1.2`, invisible to a lookup by number. Numbers are always kept in
full; names are capped at 90 characters (`A + B + …`) so the chunk prefix does
not eat the content's token budget. Disable the whole mechanism with
`--keep-short-sections`.

For a glossary, **the content *is* the section name** (`12 Doanh nghiệp cho thuê
lại lao động: Là doanh nghiệp…`). Merge two of those carelessly and the name
appears both on the heading line and at the start of the body, reading as a
verbatim repetition — so the section's own name is stripped from the first body
line, exactly as for an unmerged section.

---

## The rebuilt layout

| Rule | How |
|---|---|
| Document title | Top of page 1, centred, 24pt bold — the root of the outline |
| Heading sizes | Shrinking by level: `1.` 20pt → `1.1` 17pt → `1.1.1` 15pt (the tree stops at 3) |
| Body size | 10.5pt — always at least 1pt below every heading level |
| Section names | Always alone on their line, bold, flush left, 16pt/9pt space above/below |
| No invented headings | Every extra line is a phantom branch. The one exception is the `(tiếp)` suffix, when a section over the token ceiling must be split |
| Sections over 512 tokens | Split into *roughly equal* parts, each reopening with `1.2 Section name (tiếp)`. A RAG stack chunks by the outline rather than by page, so a four-thousand-token section would otherwise become one four-thousand-token chunk and get truncated |
| Page breaks | Left to Word/MuPDF. The tool's line estimate never matches a real layout engine; forcing breaks from the estimate makes undercounted pages overflow and leaves a nearly blank page behind |
| No stranded or split headings | `.docx`: set `keep_with_next`. `.pdf`: after rendering, **read every page back** — where a page ends on a heading line, mark it and rebuild with a page break just before (see below) |
| Figures are never squashed | When the gap at the foot of a page is too short, MuPDF *shrinks the image* to fit rather than pushing it over — a 630pt flowchart squeezed to a third with unreadable box labels. Open a new page before placing it |
| Bullet lines | 18pt indent, hanging — reads as a list, not as prose |
| Tables and figures | Tables are rebuilt as real tables, the header row repeats on every page, no row is split in half; figures are embedded in place |

Add `--page-per-section` to force **one page per section**, each page within 512
tokens (chunk boundaries then coincide with page boundaries). To keep the input
file's original layout and only strip logos and headers/footers, add
`--keep-layout`.

### Why the PDF is rendered more than once

MuPDF decides page breaks *after* laying out the text, and the block-placement
API cannot report what actually appeared on the page: given a table row taller
than the remaining gap, it reports the space as used while in fact drawing
nothing but the heading line and pushing the row to the next page. The only
reliable check is to **read the rendered page back**: where the last line on a
page is at heading size with nothing below it, mark that block, rebuild, and open
a new page just before it.

Pushing one heading down shifts everything below it and sometimes exposes another
stranded heading, so the pass runs up to 10 times — but stops as soon as a pass
finds nothing new, so an ordinary document costs exactly one. Measured over 32
documents / 526 pages: **no stranded headings remain.**

---

## What a DLA model actually sees

A layout-analysis model looks at the **printed page**; it does not read file
structure. The label it assigns each line depends on three geometric cues, so
the rebuild is forced to satisfy exactly those three:

| DLA label | The cues it uses | What the tool does |
|---|---|---|
| `title` | bold, clearly larger than body, alone on its line, generous whitespace above and below | Document title 24pt, headings bold 11.5–20pt by level, 16pt/9pt spacing, flush left, never with content run into them |
| `list-item` | opens with a bullet marker, indented, at body size | Every bullet line is its own paragraph, indented 18pt |
| `text` | justified paragraph, no indent, no marker | Body 10.5pt, justified |
| `header` / `footer` | text hugging the top or bottom edge | The rebuild **has no** headers or footers; page numbers are dropped too |

Headings additionally carry `outlineLvl` in `.docx`, so a Word export already
has a correctly levelled bookmark tree.

---

## Characters read back must be the characters written in

Two failure modes corrupt text while looking perfectly fine on paper.

**The ToUnicode table maps glyphs to their twins.** A TrueType font embedded in
a PDF makes MuPDF rebuild the ToUnicode table with several glyphs pointing at a
completely different character that draws identically:

| Written | Read back | Consequence |
|---|---|---|
| space | `U+00A0` non-breaking space | the string contains **not one ordinary space** |
| `-` | `U+00AD` soft hyphen | the soft hyphen is an *invisible* character |
| `;` | `U+037E` Greek question mark | clause boundaries disappear |

Word segmentation, BM25 and the downstream tokenizer all break, and none of them
report an error. The tool repairs the ToUnicode table after font subsetting,
right before saving.

**Exotic symbols in the source.** Interest formulas are typed in Equation
Editor, so `Mi` really lives in the Mathematical Alphanumeric Symbols block
(`𝐌𝐢`) — a block a RAG stack has no font for, so it reads `$Mi$` or drops it
entirely. Everything is folded to plain Latin during cleaning:

| Source | Becomes | |
|---|---|---|
| `𝐌𝐢`, `𝑳`, `𝟏𝟐` | `Mi`, `L`, `12` | mathematical letters → Latin (`NFKC`) |
| `∑`, `∏`, `√` | `Tổng`, `Tích`, `căn` | spelled out as words |
| `≤ ≥ ≠ × ÷ ± → ∗` | `<= >= != x / +/- -> *` | maths symbols → ASCII |
| `– — ‐ “ ” ‘ ’ •` | `- - - " " ' ' -` | typographic punctuation → ASCII |
| `U+F02B`, `U+F0B7` | `+`, `-` | Word's Symbol font pushes glyphs into the PUA while keeping their ASCII code positions |

The Symbol font's *letter* range (Greek alphabet, Wingdings ornaments) is **not**
folded to Latin — doing so would invent a word that is nowhere in the document.

Measured over 32 rebuilt documents / 526 pages: no exotic characters remain.

---

## Noise removal and figure preservation

Logos and footers are pure noise — in a chunk they only dilute the vector.
Charts and diagrams are the opposite: real content, and you need to know which
section carries one.

The two are told apart by measurable cues:

| Classified as **logo** (dropped) | Classified as **figure** (kept) |
|---|---|
| The same image on 2 or more pages | Appears once |
| Shortest side < 70px, or < 2.5% of the page area | Large enough, inside the content area |
| Sits in the top/bottom margin | Sits in the middle of the page |
| Covers the page while the page still holds plenty of text (background) | — |
| No dark ink at all (watermark) | Has ink dark enough to read |
| The page's live text sits on top of it (frame, colour band) | Its text lives inside the image file |
| Tens of thousands of colours (marketing photograph) | A few hundred (line art, flat fills) |

A cover page is usually empty once stripped — it was never more than one image —
so that blank page is dropped entirely rather than left as a white sheet.

### Watermarks slip through every size threshold

The faint hand-holding-a-shield, the leaf, the globe printed across the middle of
the page: half a page wide, so every size threshold clears it, and printed only
once, so the repeat count misses it too. Four of the cues above fail, and it
walks straight into a chunk as a genuine figure.

The remaining cue is elsewhere: a watermark has to stay faint for the text on top
to remain readable, so it carries **no dark ink at all**. A chart or flowchart is
the opposite — without dark ink it is unreadable itself. The tool re-reads the
image area exactly as the eye sees it on the page (transparency composited over
the paper) and counts pixels darker than grey level 170:

| | Dark pixel ratio |
|---|---|
| Watermark | 0.00 – 0.02 |
| Every content figure in the real corpus | 0.17 – 0.68 |

The threshold sits at **0.05**, at least 3× from either side — and a test holds
that gap open, so the next tweak to the number cannot silently drop a pale chart.
An image with a caption ("Hình 3: …") skips the measurement entirely: the author
has already declared it content.

### Frames and photographs: dark ink, still decoration

The ink measure divides by the number of *non-paper* pixels, so a rounded red
frame drawn around a paragraph — a few red strokes on a transparent ground —
scores **0.67**, level with the densest diagram. The fan-shaped family collage on
a part's cover page does the same. Both walk into the clean document as content.

Two further cues separate them:

| | The page's live text on top | Colours (rendered at 48 dpi) |
|---|---|---|
| Frames and colour bands under text | 292 – 523 characters | 758 – 1,229 |
| Marketing photographs | 0 – 523 characters | 39,832 – 48,968 |
| **Content diagrams and charts** | **0 characters** | **610 – 621** |

A frame is drawn *under* the text, so the page's text sits on it; a diagram's
labels live inside its own image file, and that area holds no live characters at
all. Photographs have continuous gradients while diagrams use flat colour areas —
the threshold sits at **8,000 colours**, 6.5× below the upper group and 5× above
the lower.

A scanned page is also one photograph of the whole sheet: it fails the tests, and
dropping it loses the content entirely. So a page with fewer than 200 live
characters has its images **exempted from measurement** — the image *is* the
page's content. Captioned images are exempt from both tests.

Measured over 6 insurance documents: these two rules removed **19 further
decorative images** without touching a single content diagram, across 38 scanned
pages.

### Headers and footers repeat **in exactly one spot**

Not every document's footer hugs the bottom — some place the page number at 81%
of the page height, above any reasonable footer mark. Widening the scan band
alone drags real content in with it (a `Trang 5` cell in a table of contents, for
instance).

So the rule is: **repeats on most pages *and* appears at exactly the same
coordinate every time.** The first condition alone is not enough — a frequently
repeated cross-reference satisfies it too — but adding the second leaves only
what was actually placed in the header/footer frame. Page numbers differ on every
page, but with digits erased `1/5` and `2/5` fold to one string and are caught
anyway.

### Figures in the chunk file

In the rebuilt document, figures are embedded back at their place in the flow of
their section; logos, cover art and headers/footers are not carried over. A
figure taller than one page is scaled to fit.

In the **chunk file**, a figure becomes a placeholder line:

```
[FIGURE: Biểu đồ 1: Cơ cấu quyền lợi | 325x324 | tailieu_p3_1.png]
```

plus a `has_figure` flag and a `figures` list (size, caption, file path) in the
metadata. At query time you therefore know the section carries an image you can
display alongside the answer, or hand to a vision model.

Captions are recognised from the usual patterns: `Hình 1:`, `Biểu đồ 2 -`,
`Sơ đồ 3.`, `Figure 4:` …

With `--keep-layout`, images are removed by their own reference id, never by
area. Logos often sit on top of a large figure, and area-based redaction would
take the content with them.

In DOCX, logos usually live in the header/footer — outside the document body, so
they never reach a chunk in the first place.

### Vector flowcharts

A process flowchart in a PDF is not an image: it is a mass of vector strokes plus
the text inside each box. Ordinary extraction pulls all that text out and lays it
in one long meaningless line — *the diagram disappears and only scattered words
remain*:

```
| Nội dung Mẫu ĐVKD lưu Đại ký lưu ký lưu Tiếp nhận và khai báo Kiểm tra, đối chiếu danh sách …
```

Tables are drawn with strokes too, so the two must be told apart. The difference:
table rules are always horizontal or vertical, while a flowchart has **diagonal
strokes** (arrows, the sides of a diamond) and **curves** (ellipses, rounded
corners). Measured on real documents, a table page has exactly 0 such strokes and
a flowchart page has dozens.

Any region with 6 or more diagonal/curved strokes is exported as a PNG and
embedded back into the rebuild, and the text inside that region is dropped —
it is already in the image. If the image cannot be exported (no figure directory
given), the text is kept: jumbled text beats losing the block entirely.

### Scans: nothing can be stripped, and the tool says so

A scanned page is **one photograph of the whole sheet**: text, logo, headers,
footers and table of contents are all pixels inside a single image. There is no
separate object to strip, and stripping that image leaves a blank page. The
chunks would hold nothing but a `[FIGURE: …]` placeholder, and the outline would
be empty — loading that into RAG is pointless.

The tool used to run quietly and produce a 6.6 MB file that looked like success.
Now any document where **80% or more of its pages have no text layer** is flagged
at extraction time, in both the GUI and the CLI:

```
[!] 21/21 pages have no text layer — this is a scan. Text and logo share a
    single full-page image, so the logo cannot be stripped on its own and the
    chunks hold nothing but [FIGURE] placeholders. Run OCR (or fetch the
    original .docx/.pdf) and feed it back in.
```

The warning appears **before** cleaning, so you do not wait for a whole batch to
find out. `report.json` records `pages_total` / `pages_without_text` for
cross-checking.

---

## What a chunk looks like

```json
{
  "chunk_id": "quy-dinh-phan-hang-a1b2c3d4#p9#0030",
  "text": "PHẦN 1 ... > ĐIỀU 2 ... > 2.3.1 Nghĩa vụ cung cấp thông tin\n2.3.1 Nghĩa vụ ...",
  "raw_text": "2.3.1 Nghĩa vụ cung cấp thông tin ...",
  "section_number": "2.3.1",
  "section_path": "PHẦN 1 ... > ĐIỀU 2 ... > 2.3.1 ...",
  "section_level": 3,
  "page": 9,
  "page_source": "actual",
  "is_continued": true,
  "is_continuation": false,
  "part_index": 1,
  "part_total": 5,
  "prev_chunk_id": "...#p9#0029",
  "next_chunk_id": "...#p10#0031",
  "char_count": 812,
  "est_tokens": 291,
  "has_table": false,
  "has_figure": false,
  "figures": []
}
```

- `text` — what you embed (the outline path is already prepended).
- `est_tokens` — the estimated token count of `text`, always ≤ `--max-tokens`.
  The estimate deliberately runs **high** (2.8 chars/token, punctuation counted),
  because an undercount passes every check here and is only truncated later, at
  embedding time.
- `raw_text` — the original content, for display to a reader.
- `page_source` — `actual` (PDF, real page numbers) or
  `rendered`/`page_break`/`estimated` for DOCX, since Word files do not store
  page numbers.

### Recovering full context at query time

Small chunks make retrieval precise, but an answer needs the section's full
content. When a matching chunk has `is_continued` or `is_continuation` set to
`true`, reassemble the rest of its section:

```python
from docindex.retrieval import load_chunks, index_by_id, expand_section

chunks = load_chunks("output/chunks.jsonl")
by_id = index_by_id(chunks)

hit = chunks[30]                      # a chunk returned by vector search
context = expand_section(hit, by_id)  # the section's full content
```

---

## Problems handled

| Problem | How it is handled |
|---|---|
| Word auto-numbering: the number is not in the text | Rebuilt from `numbering.xml` per the OOXML rules |
| Word declares `a) b) c)` level with `1. 2. 3.` | Hierarchy from the marker kind (`numFmt`), not the list level |
| A sentence starting with a number read as a heading | Checked against the open number sequence; rejected if it does not continue it |
| Headings deeper than level 3 (`1.1.1.1`) | Leave the tree and become content of the parent — the number stays in the text |
| Headers/footers repeating on every page | Detected as lines repeating on ≥50% of pages at one coordinate, then removed |
| TOC pages producing junk chunks | Pages with many dot-leader lines are dropped |
| A section spanning several pages | Split by page, keeping the outline path, the `is_continued` flag and prev/next links |
| A page holding several sections | Split into several chunks, never mixing two topics |
| A section too short (heading only) | Merged with a child or sibling on the same page |
| A section too long | Split at sentence boundaries; tables split by row with the header repeated |
| A table spanning pages | Extracted as markdown, with the page count computed correctly |
| PDF putting item markers in their own column | Rejoined by horizontal row |
| PDF storing rendered lines | Rejoined into complete paragraphs; a sentence unfinished at a block's end joins the next |
| A word hyphenated across lines (`…và Dai-` / `ichi Life…`) | Rejoined, even when the two lines differ in style |
| Cross-references ("…tại Điều 2.3 Phần 1 này") | Not mistaken for headings |
| Logos, ornaments, decorative backgrounds | Removed entirely, never reaching a chunk |
| Watermarks half a page wide | Recognised by having no dark ink at all |
| Charts and diagrams | Preserved with a `[FIGURE: …]` placeholder plus metadata; exportable as PNG |
| Page numbers "9/34", "Lưu hành nội bộ" | Removed by pattern and by cross-page repetition |
| Cover and TOC on the same page | Only the TOC part is cut; the cover content stays |
| Merged cells creating empty columns | Empty columns dropped when building the markdown table |
| A table row over the chunk ceiling | Converted to "column: value" lines rather than breaking the table |
| A single merged cell spanning a page | Recognised as a text frame, returned verbatim and split by sentence — otherwise the whole block blows the ceiling |
| Spaces in a PDF becoming `U+00A0` | ToUnicode table repaired so they read back as ordinary spaces |
| A heading with the content run into it | Shortened for the path while the body keeps every word |
| The source file dropped characters when the PDF was made | Flagged in the report so you can fetch the original |

---

## Project layout

```
docindex/
  gui.py           drag-and-drop interface
  cli.py           command line entry point
  export.py        writes the cleaned document (rebuilt layout, or original layout)
  layout.py        turns the outline into a layout: sizes by level, pagination
  render.py        writes the layout out as .docx / .pdf
  pipeline.py      wires the steps together for one file
  extract_pdf.py   PDF -> blocks (PyMuPDF)
  extract_docx.py  DOCX -> blocks (python-docx)
  numbering.py     rebuilds Word's automatic numbering
  images.py        logo/figure classification, vector diagrams, image export
  headings.py      heading detection, sequence checking, outline building
  chunker.py       splits into chunks by page and by section
  report.py        quality checks
  retrieval.py     context expansion at query time
  models.py        shared data types, symbol normalization
docs/
  NORMALIZATION-RULES.md   the full spec, with every threshold justified
scripts/
  docindex-gui.bat         open the GUI
  create-shortcut.bat/.ps1 create Desktop shortcuts (Windows)
tests/
  test_docindex.py         constraint tests against a real corpus
```

---

## Quality checks

`report.json` records per-document statistics: chunk count, headings detected,
`tokens_median` / `tokens_max` against `token_limit`, chunk lengths, logos
dropped, figures kept, and any warnings. `tokens_max` touching `token_limit`
exactly is normal — exceeding it is a bug, and then `too_long` is non-zero with a
warning attached.

The warning worth watching for is `suspect_truncated_lines`: some PDFs made from
Word lose characters in the source file itself ("Khách hàng" becoming "h hàng").
The tool cannot recover what is already gone, so when you see this, process the
original `.docx` instead if you still have it.

The test suite checks the important constraints against real documents: no chunk
over 512 tokens, no page over 512 tokens, headings in the PDF bold and larger
than the body, no stranded headings, characters reading back as they were written
(spaces, hyphens, semicolons), flowcharts surviving as pictures, plus a backstop
against text loss (every document must keep ≥95% of its words — the remainder is
the TOC and headers/footers dropped on purpose):

```bash
python -m pytest tests/ -q
```

The tests skip automatically when no document corpus is present. Point
`TEST_DIR` in `tests/test_docindex.py` at a folder of your own `.pdf`/`.docx`
files to run them against real input.

---

## Requirements

Python 3.10+, `PyMuPDF`, `python-docx`. Add `tkinterdnd2` for drag and drop in
the GUI.

## License

[MIT](LICENSE)
