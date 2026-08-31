"""Graphical interface: drop PDF/DOCX files in and they are processed.

Run it with:  python -m docindex.gui
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .chunker import MAX_TOKENS, MIN_TOKENS, ChunkConfig
from .export import CleanOptions, clean_document, rebuild_document
from .models import document_title
from .pipeline import SUPPORTED, iter_input_files, process_file
from .report import check, format_outline, scanned_warning

try:  # drag and drop needs a third-party library; without it the buttons work
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:  # pragma: no cover - environment dependent
    DND_FILES = None
    TkinterDnD = None
    HAS_DND = False

# Format of the rebuilt document: displayed label -> value passed to export.
# .pdf comes first because a DLA model reads PDF far more reliably than .docx.
FORMATS = {"Write .pdf": "pdf", "Write .docx": "docx", "Same as source": "same"}
FORMAT_LABELS = list(FORMATS)

# Preset cleaning levels: label -> state of each checkbox below.
#
# The first entry is the default because it is the safest: strip the logo and
# cover art to lighten the document while the text stays exactly where it was.
# Removing headers, footers or the table of contents edits the content, so the
# user has to choose that deliberately.
CLEAN_PRESETS: dict[str, dict[str, bool]] = {
    "Logo and cover art only": {
        "drop_logo": True, "drop_cover": True,
        "drop_header_footer": False, "drop_toc": False, "rebuild": False},
    "Also headers and footers": {
        "drop_logo": True, "drop_cover": True,
        "drop_header_footer": True, "drop_toc": False, "rebuild": False},
    "Full clean, keep layout": {
        "drop_logo": True, "drop_cover": True,
        "drop_header_footer": True, "drop_toc": True, "rebuild": False},
    "Rebuild layout for RAG": {
        "drop_logo": True, "drop_cover": True,
        "drop_header_footer": True, "drop_toc": True, "rebuild": True},
}
CUSTOM_PRESET = "Custom"
PRESET_LABELS = [*CLEAN_PRESETS, CUSTOM_PRESET]
DEFAULT_PRESET = PRESET_LABELS[0]

BG = "#f4f6f9"
CARD = "#ffffff"
ACCENT = "#2d6cdf"
DROP_IDLE = "#dbe4f3"
DROP_HOVER = "#c3d6f7"
MUTED = "#5b6675"
OK = "#1a7f4b"
WARN = "#b06a00"
ERR = "#c0392b"


def parse_drop(data: str) -> list[str]:
    """Split the path string the OS sends when files are dropped.

    Paths containing spaces are wrapped in braces, for example:
    "{C:/Tài liệu/a.pdf} C:/b.docx"
    """
    paths: list[str] = []
    buf = ""
    inside = False
    for ch in data:
        if ch == "{":
            inside = True
            buf = ""
        elif ch == "}":
            inside = False
            if buf.strip():
                paths.append(buf.strip())
            buf = ""
        elif ch == " " and not inside:
            if buf.strip():
                paths.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        paths.append(buf.strip())
    return paths


def expand_inputs(paths: list[str]) -> list[str]:
    """Scan folders for files inside; keep files whose format is supported."""
    out: list[str] = []
    for p in paths:
        p = p.strip('"')
        if os.path.isdir(p):
            out.extend(iter_input_files(p))
        elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in SUPPORTED:
            out.append(p)
    return out


class App:
    def __init__(self, root):
        self.root = root
        self.files: list[str] = []
        self.events: queue.Queue = queue.Queue()
        self.running = False
        self.output_dir = tk.StringVar(value=os.path.join(os.getcwd(), "output"))

        self.max_tokens = tk.IntVar(value=MAX_TOKENS)
        self.min_tokens = tk.IntVar(value=MIN_TOKENS)
        self.overlap = tk.IntVar(value=1)
        self.add_prefix = tk.BooleanVar(value=True)
        self.merge_short = tk.BooleanVar(value=True)
        self.extract_figures = tk.BooleanVar(value=False)
        self.merge = tk.BooleanVar(value=False)
        self.make_clean = tk.BooleanVar(value=True)
        self.make_jsonl = tk.BooleanVar(value=True)
        self.page_per_section = tk.BooleanVar(value=False)
        self.save_outline = tk.BooleanVar(value=True)

        # What to strip while cleaning — defaults come from the first preset
        self.clean_preset = tk.StringVar(value=DEFAULT_PRESET)
        self.drop_logo = tk.BooleanVar()
        self.drop_cover = tk.BooleanVar()
        self.drop_header_footer = tk.BooleanVar()
        self.drop_toc = tk.BooleanVar()
        self.rebuild = tk.BooleanVar()
        self._clean_vars = {
            "drop_logo": self.drop_logo,
            "drop_cover": self.drop_cover,
            "drop_header_footer": self.drop_header_footer,
            "drop_toc": self.drop_toc,
            "rebuild": self.rebuild,
        }
        for key, value in CLEAN_PRESETS[DEFAULT_PRESET].items():
            self._clean_vars[key].set(value)

        # outline tree of each processed document, for visual review
        self.outlines: list[tuple[str, str]] = []
        self.out_format = tk.StringVar(value=FORMAT_LABELS[0])

        root.title("docindex — split documents into chunks for RAG")
        # The options panel is tall; on a short screen the window shrinks to fit
        # rather than pushing the Run button and the log off the bottom edge.
        height = min(970, max(660, root.winfo_screenheight() - 90))
        root.geometry(f"1000x{height}")
        root.minsize(900, 640)
        root.configure(bg=BG)

        self._build_styles()
        self._build_ui()
        self._show_dnd_state(self._enable_dnd())
        self.root.after(80, self._drain_events)

    # ---------- interface ----------

    def _build_styles(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD, relief="flat")
        style.configure("TLabel", background=BG, font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=CARD, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=BG, font=("Segoe UI", 16, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED,
                        font=("Segoe UI", 9))
        style.configure("Card.TCheckbutton", background=CARD, font=("Segoe UI", 10))
        style.configure("TCheckbutton", background=BG, font=("Segoe UI", 10))
        style.configure("Run.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 6))

    def _build_ui(self) -> None:
        head = ttk.Frame(self.root, padding=(18, 14, 18, 6))
        head.pack(fill="x")
        ttk.Label(head, text="docindex", style="Title.TLabel").pack(anchor="w")
        self.hint_label = ttk.Label(
            head, style="Sub.TLabel",
            text="Drop PDF/DOCX files, or a whole folder, into the area below")
        self.hint_label.pack(anchor="w", pady=(2, 0))

        body = ttk.Frame(self.root, padding=(18, 6, 18, 12))
        body.pack(fill="both", expand=True)

        # --- drop area ---
        self.drop = tk.Frame(body, bg=DROP_IDLE, height=86,
                             highlightthickness=2, highlightbackground="#9db6e0")
        self.drop.pack(fill="x")
        self.drop.pack_propagate(False)
        self.drop_label = tk.Label(
            self.drop, bg=DROP_IDLE, fg="#2b3b55", justify="center",
            font=("Segoe UI", 11),
            text="⬇  Drop documents here\nAccepts .pdf, .docx or a folder",
        )
        self.drop_label.pack(expand=True)

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(8, 10))
        ttk.Button(btns, text="Choose files…", command=self.pick_files).pack(side="left")
        ttk.Button(btns, text="Choose folder…", command=self.pick_folder).pack(side="left", padx=6)
        ttk.Button(btns, text="Remove selected", command=self.remove_selected).pack(side="left")
        ttk.Button(btns, text="Clear all", command=self.clear_files).pack(side="left", padx=6)
        self.count_label = ttk.Label(btns, text="No documents yet", style="Sub.TLabel")
        self.count_label.pack(side="right")

        # --- file list ---
        list_wrap = ttk.Frame(body)
        list_wrap.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(
            list_wrap, height=5, selectmode="extended", activestyle="none",
            font=("Segoe UI", 10), bg=CARD, fg="#22303f",
            highlightthickness=1, highlightbackground="#d4dbe6", borderwidth=0,
        )
        self.listbox.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(list_wrap, orient="vertical", command=self.listbox.yview)
        sb.pack(side="right", fill="y")
        self.listbox.configure(yscrollcommand=sb.set)

        # --- options ---
        opt = ttk.Frame(body, style="Card.TFrame", padding=12)
        opt.pack(fill="x", pady=(12, 0))
        ttk.Label(opt, text="Options", style="Card.TLabel",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", columnspan=6)

        def spin(col, label, var, lo, hi, step, tip):
            ttk.Label(opt, text=label, style="Card.TLabel").grid(
                row=1, column=col, sticky="w", padx=(0, 6), pady=(8, 0))
            box = ttk.Spinbox(opt, from_=lo, to=hi, increment=step,
                              textvariable=var, width=7)
            box.grid(row=1, column=col + 1, sticky="w", padx=(0, 18), pady=(8, 0))
            ttk.Label(opt, text=tip, style="Card.TLabel", foreground=MUTED,
                      font=("Segoe UI", 8)).grid(row=2, column=col, columnspan=2,
                                                 sticky="w", pady=(0, 4))
            return box

        spin(0, "Token ceiling", self.max_tokens, 128, 2048, 32, "tokens per chunk")
        self.min_spin = spin(2, "Merge threshold", self.min_tokens, 0, 400, 10,
                             "shorter chunks are merged")
        spin(4, "Overlap sentences", self.overlap, 0, 5, 1, "when a section spans pages")

        # The merge switch sits right under its threshold because the two belong
        # together: turn merging off and the threshold does nothing.
        ttk.Checkbutton(opt, text="Merge short sections into a neighbour",
                        variable=self.merge_short, command=self._sync_merge_short,
                        style="Card.TCheckbutton").grid(row=3, column=0, columnspan=3,
                                                        sticky="w", pady=(6, 0))
        ttk.Checkbutton(opt, text="Prepend the outline path to chunk text",
                        variable=self.add_prefix,
                        style="Card.TCheckbutton").grid(row=3, column=3, columnspan=3,
                                                        sticky="w", pady=(6, 0))
        ttk.Checkbutton(opt, text="Also write .jsonl chunks for RAG",
                        variable=self.make_jsonl,
                        style="Card.TCheckbutton").grid(row=4, column=0, columnspan=3,
                                                        sticky="w")
        ttk.Checkbutton(opt, text="Merge chunks into one chunks.jsonl",
                        variable=self.merge,
                        style="Card.TCheckbutton").grid(row=4, column=3, columnspan=3,
                                                        sticky="w")
        ttk.Checkbutton(opt, text="Also export figures as PNG files",
                        variable=self.extract_figures,
                        style="Card.TCheckbutton").grid(row=5, column=0, columnspan=3,
                                                        sticky="w")
        ttk.Checkbutton(opt, text="Write the outline tree to a .txt file",
                        variable=self.save_outline,
                        style="Card.TCheckbutton").grid(row=5, column=3, columnspan=3,
                                                        sticky="w")
        self._sync_merge_short()

        self._build_clean_card(body)

        out = ttk.Frame(body)
        out.pack(fill="x", pady=(12, 0))
        ttk.Label(out, text="Save results to:").pack(side="left")
        ttk.Entry(out, textvariable=self.output_dir).pack(
            side="left", fill="x", expand=True, padx=8)
        ttk.Button(out, text="Change…", command=self.pick_output).pack(side="left")

        # --- run ---
        run = ttk.Frame(body)
        run.pack(fill="x", pady=(12, 6))
        self.run_btn = ttk.Button(run, text="▶  Start processing",
                                  style="Run.TButton", command=self.start)
        self.run_btn.pack(side="left")
        self.open_btn = ttk.Button(run, text="Open output folder",
                                   command=self.open_output)
        self.open_btn.pack(side="left", padx=8)
        self.outline_btn = ttk.Button(run, text="View outline tree",
                                      command=self.show_outline, state="disabled")
        self.outline_btn.pack(side="left")
        self.progress = ttk.Progressbar(run, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(12, 0))

        # --- log ---
        log_wrap = ttk.Frame(body)
        log_wrap.pack(fill="both", expand=True)
        self.log = tk.Text(log_wrap, height=9, wrap="word", font=("Consolas", 9),
                           bg="#1e2530", fg="#d7dee8", borderwidth=0,
                           padx=10, pady=8, state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        lsb = ttk.Scrollbar(log_wrap, orient="vertical", command=self.log.yview)
        lsb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=lsb.set)
        for tag, colour in (("ok", "#6ee7a5"), ("warn", "#ffcf7a"),
                            ("err", "#ff8f80"), ("head", "#8fc0ff"),
                            ("dim", "#8b97a8")):
            self.log.tag_configure(tag, foreground=colour)


    def _build_clean_card(self, body) -> None:
        """The panel for choosing what gets stripped from the cleaned document."""
        card = ttk.Frame(body, style="Card.TFrame", padding=12)
        card.pack(fill="x", pady=(10, 0))
        ttk.Label(card, text="Document cleaning", style="Card.TLabel",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w",
                                                      columnspan=2)
        ttk.Label(card, text="Cleaning level:", style="Card.TLabel").grid(
            row=0, column=2, sticky="e", padx=(0, 6))
        self.preset_combo = ttk.Combobox(card, textvariable=self.clean_preset,
                                         values=PRESET_LABELS, state="readonly",
                                         width=28)
        self.preset_combo.grid(row=0, column=3, sticky="w")
        self.preset_combo.bind("<<ComboboxSelected>>", self._apply_preset)

        ttk.Checkbutton(card, text="Write the cleaned document",
                        variable=self.make_clean, command=self._sync_clean,
                        style="Card.TCheckbutton").grid(row=1, column=0, columnspan=4,
                                                        sticky="w", pady=(8, 2))

        def drop_box(row, col, text, var):
            box = ttk.Checkbutton(card, text=text, variable=var,
                                  command=self._on_clean_toggle,
                                  style="Card.TCheckbutton")
            box.grid(row=row, column=col, columnspan=2, sticky="w", padx=(18, 12))
            return box

        self.drop_boxes = {
            "drop_logo": drop_box(2, 0, "Strip logos and repeated ornaments", self.drop_logo),
            "drop_cover": drop_box(2, 2, "Strip cover art on the first page", self.drop_cover),
            "drop_header_footer": drop_box(3, 0, "Strip header / footer text",
                                           self.drop_header_footer),
            "drop_toc": drop_box(3, 2, "Strip the table of contents", self.drop_toc),
        }

        self.rebuild_box = ttk.Checkbutton(
            card, text="Rebuild the layout for a RAG stack reading the outline",
            variable=self.rebuild, command=self._on_clean_toggle,
            style="Card.TCheckbutton")
        self.rebuild_box.grid(row=4, column=0, columnspan=2, sticky="w",
                              padx=(18, 12), pady=(6, 0))
        self.format_label = ttk.Label(card, text="Rebuilt document format:",
                                      style="Card.TLabel")
        self.format_label.grid(row=4, column=2, sticky="e", padx=(0, 6), pady=(6, 0))
        self.format_combo = ttk.Combobox(card, textvariable=self.out_format,
                                         values=FORMAT_LABELS, state="readonly",
                                         width=16)
        self.format_combo.grid(row=4, column=3, sticky="w", pady=(6, 0))
        self.page_box = ttk.Checkbutton(card, text="One page per section",
                                        variable=self.page_per_section,
                                        style="Card.TCheckbutton")
        self.page_box.grid(row=5, column=0, columnspan=2, sticky="w", padx=(36, 12))

        self.clean_note = ttk.Label(card, style="Card.TLabel", foreground=MUTED,
                                    font=("Segoe UI", 8), wraplength=880,
                                    justify="left")
        self.clean_note.grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self._sync_clean()

    # ---------- keeping the settings in sync ----------

    def _apply_preset(self, _event=None) -> None:
        """Choosing a preset re-ticks every checkbox below it."""
        preset = CLEAN_PRESETS.get(self.clean_preset.get())
        if preset:
            for key, value in preset.items():
                self._clean_vars[key].set(value)
        self._sync_clean()

    def _on_clean_toggle(self) -> None:
        """Ticking a box by hand moves the level to whichever preset now matches."""
        current = {k: v.get() for k, v in self._clean_vars.items()}
        match = next((name for name, preset in CLEAN_PRESETS.items()
                      if preset == current), CUSTOM_PRESET)
        self.clean_preset.set(match)
        self._sync_clean()

    def _sync_clean(self) -> None:
        """Grey out the boxes that no longer apply and spell out what this run strips."""
        on = self.make_clean.get()
        rebuilding = on and self.rebuild.get()
        state = "normal" if on else "disabled"

        self.preset_combo.configure(state="readonly" if on else "disabled")
        self.rebuild_box.configure(state=state)
        self.page_box.configure(state="normal" if rebuilding else "disabled")
        self.format_combo.configure(state="readonly" if rebuilding else "disabled")
        # ttk.Label does not grey out with state on every theme; recolour by hand
        self.format_label.configure(
            foreground="#22303f" if rebuilding else "#9aa4b1")

        # The rebuild takes its content from the extracted outline, where logos,
        # headers, footers and the table of contents were already dropped — these
        # boxes have nothing left to control, except cover art, which stays the
        # user's call.
        for key, box in self.drop_boxes.items():
            usable = on and (not rebuilding or key == "drop_cover")
            box.configure(state="normal" if usable else "disabled")

        self.clean_note.configure(text=self._clean_summary(on, rebuilding))

    def _clean_summary(self, on: bool, rebuilding: bool) -> str:
        if not on:
            return ("Off — no cleaned document is written, only the chunking "
                    "runs.")
        if rebuilding:
            keep = "" if self.drop_cover.get() else " Cover art on page one is kept."
            return ("The rebuild always drops logos, headers, footers and the "
                    "table of contents, because its content comes from the "
                    "extracted outline." + keep)
        picked = [label for key, label in (
            ("drop_logo", "logos and repeated ornaments"),
            ("drop_cover", "cover art"),
            ("drop_header_footer", "header/footer text"),
            ("drop_toc", "the table of contents"),
        ) if self._clean_vars[key].get()]
        if not picked:
            return ("Nothing is stripped — the output would be identical to the "
                    "original. Tick at least one item.")
        return ("Keeping the original layout, stripping only: " + ", ".join(picked)
                + ". Figures in the body are always kept.")

    def _sync_merge_short(self) -> None:
        """With merging off the threshold box greys out — it no longer changes anything."""
        self.min_spin.configure(
            state="normal" if self.merge_short.get() else "disabled")

    def _enable_dnd(self) -> bool:
        """Enable drag and drop. Returns False when this machine cannot use it.

        `tkinterdnd2` can be installed and still fail: it loads a prebuilt tkdnd
        library, and a build that does not match the running Tk raises TclError
        at the moment the drop target is registered.
        """
        if not HAS_DND:
            return False
        try:
            for widget in (self.drop, self.drop_label):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
                widget.dnd_bind("<<DragEnter>>", lambda e: self._paint_drop(DROP_HOVER))
                widget.dnd_bind("<<DragLeave>>", lambda e: self._paint_drop(DROP_IDLE))
        except (tk.TclError, AttributeError):
            return False
        return True

    def _show_dnd_state(self, ready: bool) -> None:
        """Say plainly when drag and drop is unavailable.

        If the drop area still invites "Drop documents here" while the library is
        missing, the user drops a file, nothing happens, and there is no clue why.
        """
        if ready:
            self._say("Ready. Drop documents into the area above to begin.", "dim")
            return
        self.hint_label.configure(
            text="Use the buttons to choose files — drag and drop is unavailable here")
        self.drop_label.configure(
            text="Drag and drop is unavailable on this machine\n"
                 "Use “Choose files…” or “Choose folder…” below",
            fg="#7a4b00")
        self._paint_drop("#f3e6cc")
        self._say("Drag and drop unavailable: the tkinterdnd2 library is missing.", "warn")
        self._say("Install it with:  pip install tkinterdnd2   then reopen.", "warn")

    def _paint_drop(self, colour: str) -> None:
        self.drop.configure(bg=colour)
        self.drop_label.configure(bg=colour)

    # ---------- file handling ----------

    def _on_drop(self, event):
        self._paint_drop(DROP_IDLE)
        raw = parse_drop(event.data)
        found = expand_inputs(raw)
        rejected = len(raw) - len(
            [p for p in raw if os.path.isdir(p) or os.path.splitext(p)[1].lower() in SUPPORTED]
        )
        self.add_files(found)
        if rejected > 0:
            self._say(f"Skipped {rejected} items that are not .pdf/.docx", "warn")

    def add_files(self, paths: list[str]) -> None:
        added = 0
        for p in paths:
            full = os.path.abspath(p)
            if full not in self.files:
                self.files.append(full)
                self.listbox.insert("end", f"  {os.path.basename(full)}")
                added += 1
        if added:
            self._say(f"Added {added} documents", "ok")
        self._refresh_count()

    def _refresh_count(self) -> None:
        n = len(self.files)
        self.count_label.configure(
            text="No documents yet" if n == 0 else f"{n} documents")

    def pick_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Choose documents",
            filetypes=[("PDF/Word documents", "*.pdf *.docx"), ("All files", "*.*")])
        self.add_files(expand_inputs(list(paths)))

    def pick_folder(self) -> None:
        folder = filedialog.askdirectory(title="Choose a folder of documents")
        if folder:
            found = iter_input_files(folder)
            if not found:
                self._say("That folder holds no .pdf or .docx files", "warn")
            self.add_files(found)

    def pick_output(self) -> None:
        folder = filedialog.askdirectory(title="Choose where to save the results")
        if folder:
            self.output_dir.set(folder)

    def remove_selected(self) -> None:
        for idx in sorted(self.listbox.curselection(), reverse=True):
            self.listbox.delete(idx)
            del self.files[idx]
        self._refresh_count()

    def clear_files(self) -> None:
        self.files.clear()
        self.listbox.delete(0, "end")
        self._refresh_count()

    def open_output(self) -> None:
        path = self.output_dir.get()
        if not os.path.isdir(path):
            return
        try:
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.run(["open", path], check=False)
            else:
                subprocess.run(["xdg-open", path], check=False)
        except OSError as exc:
            messagebox.showerror("Could not open the folder", str(exc))

    # ---------- processing ----------

    def start(self) -> None:
        if self.running:
            return
        if not self.files:
            messagebox.showinfo("No documents",
                                "Drop or choose at least one .pdf/.docx file.")
            return
        out_dir = self.output_dir.get().strip()
        if not out_dir:
            messagebox.showinfo("No output folder", "Choose where to save the results.")
            return

        cfg = ChunkConfig(
            max_tokens=max(128, self.max_tokens.get()),
            min_tokens=max(0, self.min_tokens.get()),
            overlap_sentences=max(0, self.overlap.get()),
            include_path_prefix=self.add_prefix.get(),
            merge_short=self.merge_short.get(),
        )
        self.running = True
        self.run_btn.configure(state="disabled", text="Processing…")
        self.outline_btn.configure(state="disabled")
        self.outlines = []
        self.progress.configure(maximum=len(self.files), value=0)
        self._clear_log()

        opts = {
            "want_figures": self.extract_figures.get(),
            "merge": self.merge.get(),
            "make_clean": self.make_clean.get(),
            "make_jsonl": self.make_jsonl.get(),
            "clean": CleanOptions(
                drop_logo=self.drop_logo.get(),
                drop_cover=self.drop_cover.get(),
                drop_header_footer=self.drop_header_footer.get(),
                drop_toc=self.drop_toc.get(),
            ),
            "rebuild": self.rebuild.get(),
            "page_per_section": self.page_per_section.get(),
            "save_outline": self.save_outline.get(),
            "out_format": FORMATS.get(self.out_format.get(), "pdf"),
        }
        worker = threading.Thread(
            target=self._work, args=(list(self.files), out_dir, cfg, opts), daemon=True)
        worker.start()

    def _work(self, files, out_dir, cfg, opts) -> None:
        """Runs on its own thread so the window does not freeze."""
        put = self.events.put
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            put(("error", f"Could not create the output folder: {exc}"))
            put(("done", None))
            return

        rebuild = opts["make_clean"] and opts["rebuild"]
        # The rebuild needs the image files in order to embed figures
        figure_dir = (os.path.join(out_dir, "figures")
                      if opts["want_figures"] or rebuild else None)
        merge = opts["merge"]
        summary, all_chunks, failed = [], [], 0

        for index, path in enumerate(files, start=1):
            name = os.path.basename(path)
            put(("head", f"[{index}/{len(files)}] {name}"))
            stats: dict = {}

            try:
                chunks, sections, page_source = process_file(
                    path, cfg, figure_dir=figure_dir, stats=stats)
            except Exception as exc:  # one bad file should not stop the whole batch
                failed += 1
                put(("error", f"    Error: {type(exc).__name__}: {exc}"))
                put(("step", index))
                continue

            # For a scan everything downstream is meaningless — the outline is
            # empty and the chunks hold nothing but [FIGURE] lines. Say so here
            # rather than waiting for the report, because someone who only cleans
            # documents never gets that far.
            scanned = scanned_warning(stats)
            if scanned:
                put(("warn", f"    ! {scanned}"))

            # The outline is ready right after extraction, whether or not chunks
            # are written — and it is the first thing that needs reviewing.
            tree = format_outline(sections, document_title(path))
            put(("outline", (name, tree)))
            if opts["save_outline"]:
                target = os.path.join(out_dir, f"{os.path.splitext(name)[0]}.outline.txt")
                try:
                    with open(target, "w", encoding="utf-8") as f:
                        f.write(tree + "\n")
                except OSError as exc:
                    put(("warn", f"    Could not write the outline: {exc}"))

            if opts["make_clean"]:
                try:
                    if rebuild:
                        dst, cstats = rebuild_document(
                            sections, path, out_dir, out_format=opts["out_format"],
                            figure_dir=figure_dir,
                            drop_cover=opts["clean"].drop_cover,
                            max_tokens=cfg.max_tokens,
                            page_per_section=opts["page_per_section"])
                        note = (f"       {cstats['pages_out']} pages for "
                                f"{cstats['sections_out']} sections · "
                                f"{cstats['figures_kept']} figures kept")
                    else:
                        dst, cstats = clean_document(
                            path, out_dir, opts=opts["clean"])
                        note = (f"       {cstats['images_removed']} noise images removed · "
                                f"{cstats['figures_kept']} figures kept in the file")
                    stats["cleaned_file"] = os.path.basename(dst)
                    stats.update({f"clean_{k}": v for k, v in cstats.items()})
                    put(("ok", f"    → {os.path.basename(dst)}"))
                    put(("info", note))
                except Exception as exc:
                    put(("error", f"    Could not clean it: {type(exc).__name__}: {exc}"))

            if not opts["make_jsonl"]:
                put(("step", index))
                continue

            info = check(chunks, sections, cfg, stats)
            info["file"] = name
            info["page_source"] = page_source
            summary.append(info)

            put(("info", f"    {info['chunks']} chunks · {info['headings_detected']} headings"
                         f" · outline {info['outline_depth']} levels deep"
                         f" · tokens: median {info['tokens_median']},"
                         f" max {info['tokens_max']}/{info['token_limit']}"))
            if info["logos_dropped"] or info["figures_kept"]:
                put(("info", f"    {info['logos_dropped']} logos dropped · "
                             f"{info['figures_kept']} figures kept"))
            for warning in info["warnings"]:
                put(("warn", f"    ! {warning}"))

            if not merge:
                target = os.path.join(out_dir, f"{os.path.splitext(name)[0]}.jsonl")
                try:
                    self._write_jsonl(target, chunks)
                    put(("ok", f"    → {os.path.basename(target)}"))
                except OSError as exc:
                    failed += 1
                    put(("error", f"    Could not write the file: {exc}"))
            all_chunks.extend(chunks)
            put(("step", index))

        if merge and all_chunks:
            target = os.path.join(out_dir, "chunks.jsonl")
            try:
                self._write_jsonl(target, all_chunks)
                put(("ok", f"→ Merged into {os.path.basename(target)}"))
            except OSError as exc:
                put(("error", f"Could not write the merged file: {exc}"))

        try:
            with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            put(("warn", f"Could not write report.json: {exc}"))

        ok_count = len(files) - failed
        put(("head", ""))
        put(("ok" if not failed else "warn",
             f"Done: {len(all_chunks)} chunks from {ok_count}/{len(files)} documents"))
        put(("done", None))

    @staticmethod
    def _write_jsonl(path: str, chunks) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for chunk in chunks:
                f.write(json.dumps(chunk.to_dict(), ensure_ascii=False) + "\n")

    # ---------- bridge from the worker thread to the interface ----------

    def _drain_events(self) -> None:
        """Tkinter may only be updated from the main thread, so events come by queue."""
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "step":
                    self.progress.configure(value=payload)
                elif kind == "outline":
                    self.outlines.append(payload)
                elif kind == "done":
                    self.running = False
                    self.run_btn.configure(state="normal", text="▶  Start processing")
                    self.open_btn.configure(state="normal")
                    if self.outlines:
                        self.outline_btn.configure(state="normal")
                elif kind == "head":
                    self._say(payload, "head")
                elif kind == "info":
                    self._say(payload, "dim")
                else:
                    self._say(payload, {"ok": "ok", "warn": "warn",
                                        "error": "err"}.get(kind, "dim"))
        except queue.Empty:
            pass
        self.root.after(80, self._drain_events)

    def show_outline(self) -> None:
        """Open a window showing the outline tree of the documents just processed.

        A RAG stack builds every chunk title from this tree, so one wrong level is
        a wrong title on every chunk below it — worth reviewing by eye before the
        knowledge base is loaded.
        """
        if not self.outlines:
            return
        win = tk.Toplevel(self.root)
        win.title("Outline tree — check it against the original document")
        win.geometry("900x640")
        win.configure(bg=BG)

        wrap = ttk.Frame(win, padding=10)
        wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, style="Sub.TLabel",
                  text="Every line is a heading; L1 is the top level. Anything "
                       "missing, extra or at the wrong level here makes the "
                       "chunk titles wrong too.").pack(anchor="w")

        text = tk.Text(wrap, wrap="none", font=("Consolas", 10), bg=CARD,
                       fg="#22303f", borderwidth=0, padx=10, pady=8)
        text.pack(side="left", fill="both", expand=True, pady=(8, 0))
        bar = ttk.Scrollbar(wrap, orient="vertical", command=text.yview)
        bar.pack(side="right", fill="y", pady=(8, 0))
        text.configure(yscrollcommand=bar.set)

        text.tag_configure("file", foreground=ACCENT,
                           font=("Consolas", 10, "bold"))
        for name, tree in self.outlines:
            text.insert("end", f"\n{name}\n", "file")
            text.insert("end", tree + "\n")
        text.configure(state="disabled")

    def _say(self, text: str, tag: str = "dim") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")


def main() -> int:
    # The Windows console defaults to cp1252, which mangles non-ASCII messages
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    root = None
    if HAS_DND:
        # A tkdnd build that does not match the running Tk fails right here.
        # Losing drag and drop is a pity; failing to open the window at all is a
        # real failure.
        try:
            root = TkinterDnD.Tk()
        except (tk.TclError, RuntimeError, OSError) as exc:
            print(f"Could not enable drag and drop ({exc}); opening the plain window.",
                  file=sys.stderr)
    if root is None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            print(f"Could not open the interface window: {exc}", file=sys.stderr)
            print("The command line version still works: python -m docindex.cli <path>",
                  file=sys.stderr)
            return 1

    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
