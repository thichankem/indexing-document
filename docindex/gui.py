"""Giao diện đồ hoạ: kéo thả file PDF/DOCX vào là xử lý.

Chạy bằng:  python -m docindex.gui
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

try:  # kéo thả cần thư viện ngoài; thiếu thì vẫn chạy bằng nút chọn file
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:  # pragma: no cover - phụ thuộc môi trường
    DND_FILES = None
    TkinterDnD = None
    HAS_DND = False

# Định dạng của bản tài liệu dựng lại: nhãn hiển thị -> giá trị cho export.
# .pdf đứng đầu vì mô hình DLA đọc PDF chuẩn hơn hẳn .docx.
FORMATS = {"Xuất ra .pdf": "pdf", "Xuất ra .docx": "docx", "Giống file gốc": "same"}
FORMAT_LABELS = list(FORMATS)

# Mức làm sạch dựng sẵn: nhãn -> trạng thái từng ô đánh dấu bên dưới.
#
# Mục đầu tiên là mặc định vì nó là mức an toàn nhất: gỡ logo với ảnh bìa cho
# nhẹ tài liệu, còn chữ thì nằm y nguyên chỗ cũ. Gỡ đầu/chân trang hay mục lục
# là sửa nội dung, phải do người dùng chủ động chọn.
CLEAN_PRESETS: dict[str, dict[str, bool]] = {
    "Chỉ gỡ logo và ảnh bìa": {
        "drop_logo": True, "drop_cover": True,
        "drop_header_footer": False, "drop_toc": False, "rebuild": False},
    "Gỡ thêm đầu/chân trang": {
        "drop_logo": True, "drop_cover": True,
        "drop_header_footer": True, "drop_toc": False, "rebuild": False},
    "Làm sạch toàn bộ, giữ bố cục": {
        "drop_logo": True, "drop_cover": True,
        "drop_header_footer": True, "drop_toc": True, "rebuild": False},
    "Dựng lại bố cục cho RAG": {
        "drop_logo": True, "drop_cover": True,
        "drop_header_footer": True, "drop_toc": True, "rebuild": True},
}
CUSTOM_PRESET = "Tuỳ chỉnh"
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
    """Tách chuỗi đường dẫn do hệ điều hành gửi khi thả file.

    Đường dẫn có dấu cách được bọc trong ngoặc nhọn, ví dụ:
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
    """Thư mục thì quét file bên trong, file thì giữ nếu đúng định dạng."""
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

        # Từng thứ cần gỡ khi làm sạch — mặc định lấy theo mức dựng sẵn đầu tiên
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

        # cây chỉ mục của từng tài liệu vừa xử lý, để soát lại bằng mắt
        self.outlines: list[tuple[str, str]] = []
        self.out_format = tk.StringVar(value=FORMAT_LABELS[0])

        root.title("docindex — Chia tài liệu thành chunk cho RAG")
        # Bảng tuỳ chọn khá cao; màn hình thấp thì thu lại cho vừa thay vì để
        # nút "Bắt đầu xử lý" và ô nhật ký bị đẩy khuất xuống dưới mép dưới.
        height = min(970, max(660, root.winfo_screenheight() - 90))
        root.geometry(f"1000x{height}")
        root.minsize(900, 640)
        root.configure(bg=BG)

        self._build_styles()
        self._build_ui()
        self._show_dnd_state(self._enable_dnd())
        self.root.after(80, self._drain_events)

    # ---------- giao diện ----------

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
            text="Kéo thả file PDF/DOCX hoặc cả thư mục vào ô bên dưới")
        self.hint_label.pack(anchor="w", pady=(2, 0))

        body = ttk.Frame(self.root, padding=(18, 6, 18, 12))
        body.pack(fill="both", expand=True)

        # --- vùng thả file ---
        self.drop = tk.Frame(body, bg=DROP_IDLE, height=86,
                             highlightthickness=2, highlightbackground="#9db6e0")
        self.drop.pack(fill="x")
        self.drop.pack_propagate(False)
        self.drop_label = tk.Label(
            self.drop, bg=DROP_IDLE, fg="#2b3b55", justify="center",
            font=("Segoe UI", 11),
            text="⬇  Thả tài liệu vào đây\nChấp nhận .pdf, .docx hoặc thư mục",
        )
        self.drop_label.pack(expand=True)

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(8, 10))
        ttk.Button(btns, text="Chọn file…", command=self.pick_files).pack(side="left")
        ttk.Button(btns, text="Chọn thư mục…", command=self.pick_folder).pack(side="left", padx=6)
        ttk.Button(btns, text="Bỏ mục đã chọn", command=self.remove_selected).pack(side="left")
        ttk.Button(btns, text="Xoá hết", command=self.clear_files).pack(side="left", padx=6)
        self.count_label = ttk.Label(btns, text="Chưa có tài liệu nào", style="Sub.TLabel")
        self.count_label.pack(side="right")

        # --- danh sách file ---
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

        # --- tuỳ chọn ---
        opt = ttk.Frame(body, style="Card.TFrame", padding=12)
        opt.pack(fill="x", pady=(12, 0))
        ttk.Label(opt, text="Tuỳ chọn", style="Card.TLabel",
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

        spin(0, "Trần token", self.max_tokens, 128, 2048, 32, "token mỗi chunk")
        self.min_spin = spin(2, "Ngưỡng gộp", self.min_tokens, 0, 400, 10,
                             "chunk ngắn hơn sẽ gộp")
        spin(4, "Câu lặp lại", self.overlap, 0, 5, 1, "khi mục bị cắt ngang trang")

        # Gộp mục ngắn nằm ngay dưới ô ngưỡng vì hai thứ đi liền nhau: tắt gộp
        # thì ngưỡng không còn tác dụng gì.
        ttk.Checkbutton(opt, text="Gộp mục ngắn vào mục liền trước",
                        variable=self.merge_short, command=self._sync_merge_short,
                        style="Card.TCheckbutton").grid(row=3, column=0, columnspan=3,
                                                        sticky="w", pady=(6, 0))
        ttk.Checkbutton(opt, text="Chèn đường dẫn mục lục vào nội dung chunk",
                        variable=self.add_prefix,
                        style="Card.TCheckbutton").grid(row=3, column=3, columnspan=3,
                                                        sticky="w", pady=(6, 0))
        ttk.Checkbutton(opt, text="Xuất thêm chunk .jsonl cho RAG",
                        variable=self.make_jsonl,
                        style="Card.TCheckbutton").grid(row=4, column=0, columnspan=3,
                                                        sticky="w")
        ttk.Checkbutton(opt, text="Gộp chunk vào một file chunks.jsonl",
                        variable=self.merge,
                        style="Card.TCheckbutton").grid(row=4, column=3, columnspan=3,
                                                        sticky="w")
        ttk.Checkbutton(opt, text="Tách thêm hình ra file PNG riêng",
                        variable=self.extract_figures,
                        style="Card.TCheckbutton").grid(row=5, column=0, columnspan=3,
                                                        sticky="w")
        ttk.Checkbutton(opt, text="Xuất cây chỉ mục ra file .txt",
                        variable=self.save_outline,
                        style="Card.TCheckbutton").grid(row=5, column=3, columnspan=3,
                                                        sticky="w")
        self._sync_merge_short()

        self._build_clean_card(body)

        out = ttk.Frame(body)
        out.pack(fill="x", pady=(12, 0))
        ttk.Label(out, text="Lưu kết quả vào:").pack(side="left")
        ttk.Entry(out, textvariable=self.output_dir).pack(
            side="left", fill="x", expand=True, padx=8)
        ttk.Button(out, text="Đổi…", command=self.pick_output).pack(side="left")

        # --- chạy ---
        run = ttk.Frame(body)
        run.pack(fill="x", pady=(12, 6))
        self.run_btn = ttk.Button(run, text="▶  Bắt đầu xử lý",
                                  style="Run.TButton", command=self.start)
        self.run_btn.pack(side="left")
        self.open_btn = ttk.Button(run, text="Mở thư mục kết quả",
                                   command=self.open_output)
        self.open_btn.pack(side="left", padx=8)
        self.outline_btn = ttk.Button(run, text="Xem cây chỉ mục",
                                      command=self.show_outline, state="disabled")
        self.outline_btn.pack(side="left")
        self.progress = ttk.Progressbar(run, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(12, 0))

        # --- nhật ký ---
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
        """Bảng chọn từng thứ cần gỡ khỏi bản tài liệu đã làm sạch."""
        card = ttk.Frame(body, style="Card.TFrame", padding=12)
        card.pack(fill="x", pady=(10, 0))
        ttk.Label(card, text="Làm sạch tài liệu", style="Card.TLabel",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w",
                                                      columnspan=2)
        ttk.Label(card, text="Mức làm sạch:", style="Card.TLabel").grid(
            row=0, column=2, sticky="e", padx=(0, 6))
        self.preset_combo = ttk.Combobox(card, textvariable=self.clean_preset,
                                         values=PRESET_LABELS, state="readonly",
                                         width=28)
        self.preset_combo.grid(row=0, column=3, sticky="w")
        self.preset_combo.bind("<<ComboboxSelected>>", self._apply_preset)

        ttk.Checkbutton(card, text="Xuất bản tài liệu đã làm sạch",
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
            "drop_logo": drop_box(2, 0, "Gỡ logo và hoạ tiết lặp lại", self.drop_logo),
            "drop_cover": drop_box(2, 2, "Gỡ ảnh bìa ở trang đầu", self.drop_cover),
            "drop_header_footer": drop_box(3, 0, "Gỡ chữ đầu trang / chân trang",
                                           self.drop_header_footer),
            "drop_toc": drop_box(3, 2, "Gỡ phần mục lục", self.drop_toc),
        }

        self.rebuild_box = ttk.Checkbutton(
            card, text="Dựng lại bố cục cho hệ RAG đọc cây chỉ mục",
            variable=self.rebuild, command=self._on_clean_toggle,
            style="Card.TCheckbutton")
        self.rebuild_box.grid(row=4, column=0, columnspan=2, sticky="w",
                              padx=(18, 12), pady=(6, 0))
        self.format_label = ttk.Label(card, text="Định dạng bản dựng lại:",
                                      style="Card.TLabel")
        self.format_label.grid(row=4, column=2, sticky="e", padx=(0, 6), pady=(6, 0))
        self.format_combo = ttk.Combobox(card, textvariable=self.out_format,
                                         values=FORMAT_LABELS, state="readonly",
                                         width=16)
        self.format_combo.grid(row=4, column=3, sticky="w", pady=(6, 0))
        self.page_box = ttk.Checkbutton(card, text="Mỗi mục một trang riêng",
                                        variable=self.page_per_section,
                                        style="Card.TCheckbutton")
        self.page_box.grid(row=5, column=0, columnspan=2, sticky="w", padx=(36, 12))

        self.clean_note = ttk.Label(card, style="Card.TLabel", foreground=MUTED,
                                    font=("Segoe UI", 8), wraplength=880,
                                    justify="left")
        self.clean_note.grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self._sync_clean()

    # ---------- đồng bộ các ô cấu hình ----------

    def _apply_preset(self, _event=None) -> None:
        """Chọn một mức dựng sẵn thì tick lại toàn bộ các ô bên dưới."""
        preset = CLEAN_PRESETS.get(self.clean_preset.get())
        if preset:
            for key, value in preset.items():
                self._clean_vars[key].set(value)
        self._sync_clean()

    def _on_clean_toggle(self) -> None:
        """Tự tay đổi một ô thì mức làm sạch nhảy về đúng tên của tổ hợp đó."""
        current = {k: v.get() for k, v in self._clean_vars.items()}
        match = next((name for name, preset in CLEAN_PRESETS.items()
                      if preset == current), CUSTOM_PRESET)
        self.clean_preset.set(match)
        self._sync_clean()

    def _sync_clean(self) -> None:
        """Làm mờ những ô không còn tác dụng và nói rõ lần chạy này gỡ những gì."""
        on = self.make_clean.get()
        rebuilding = on and self.rebuild.get()
        state = "normal" if on else "disabled"

        self.preset_combo.configure(state="readonly" if on else "disabled")
        self.rebuild_box.configure(state=state)
        self.page_box.configure(state="normal" if rebuilding else "disabled")
        self.format_combo.configure(state="readonly" if rebuilding else "disabled")
        # ttk.Label không tự mờ đi theo state trên mọi theme, phải đổi màu tay
        self.format_label.configure(
            foreground="#22303f" if rebuilding else "#9aa4b1")

        # Bản dựng lại lấy nội dung từ cây mục lục đã trích xuất, ở đó logo,
        # đầu/chân trang và mục lục đã bị loại từ trước — mấy ô này không còn
        # gì để điều khiển nữa, trừ ảnh bìa vẫn do người dùng quyết.
        for key, box in self.drop_boxes.items():
            usable = on and (not rebuilding or key == "drop_cover")
            box.configure(state="normal" if usable else "disabled")

        self.clean_note.configure(text=self._clean_summary(on, rebuilding))

    def _clean_summary(self, on: bool, rebuilding: bool) -> str:
        if not on:
            return ("Đang tắt — không xuất bản tài liệu đã làm sạch, "
                    "chỉ chạy phần chunk.")
        if rebuilding:
            keep = "" if self.drop_cover.get() else " Ảnh bìa trang đầu được giữ lại."
            return ("Bản dựng lại luôn bỏ logo, đầu/chân trang và mục lục vì nội "
                    "dung lấy từ cây mục lục đã trích xuất." + keep)
        picked = [label for key, label in (
            ("drop_logo", "logo và hoạ tiết lặp"),
            ("drop_cover", "ảnh bìa"),
            ("drop_header_footer", "chữ đầu/chân trang"),
            ("drop_toc", "phần mục lục"),
        ) if self._clean_vars[key].get()]
        if not picked:
            return ("Không gỡ gì cả — file kết quả sẽ giống hệt bản gốc. "
                    "Hãy tick ít nhất một mục.")
        return ("Giữ nguyên bố cục gốc, chỉ gỡ: " + ", ".join(picked)
                + ". Hình minh hoạ trong nội dung vẫn được giữ lại.")

    def _sync_merge_short(self) -> None:
        """Tắt gộp thì ô ngưỡng gộp mờ đi — nó không còn ảnh hưởng gì nữa."""
        self.min_spin.configure(
            state="normal" if self.merge_short.get() else "disabled")

    def _enable_dnd(self) -> bool:
        """Bật kéo thả. Trả về False nếu máy không dùng được.

        `tkinterdnd2` có thể cài rồi mà vẫn hỏng: nó nạp thư viện tkdnd biên
        dịch sẵn, bản không khớp với Tk đang chạy sẽ ném TclError ngay lúc
        đăng ký vùng thả.
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
        """Nói thẳng khi không kéo thả được.

        Vùng thả vẫn mời "Thả tài liệu vào đây" trong khi thư viện chưa có thì
        người dùng thả xong không thấy gì xảy ra và không biết vì sao.
        """
        if ready:
            self._say("Sẵn sàng. Thả tài liệu vào ô phía trên để bắt đầu.", "dim")
            return
        self.hint_label.configure(
            text="Bấm nút để chọn file — máy này chưa kéo thả được")
        self.drop_label.configure(
            text="Kéo thả chưa dùng được trên máy này\n"
                 "Bấm “Chọn file…” hoặc “Chọn thư mục…” bên dưới",
            fg="#7a4b00")
        self._paint_drop("#f3e6cc")
        self._say("Không kéo thả được: thiếu thư viện tkinterdnd2.", "warn")
        self._say("Cài bằng lệnh:  pip install tkinterdnd2   rồi mở lại.", "warn")

    def _paint_drop(self, colour: str) -> None:
        self.drop.configure(bg=colour)
        self.drop_label.configure(bg=colour)

    # ---------- thao tác file ----------

    def _on_drop(self, event):
        self._paint_drop(DROP_IDLE)
        raw = parse_drop(event.data)
        found = expand_inputs(raw)
        rejected = len(raw) - len(
            [p for p in raw if os.path.isdir(p) or os.path.splitext(p)[1].lower() in SUPPORTED]
        )
        self.add_files(found)
        if rejected > 0:
            self._say(f"Bỏ qua {rejected} mục không phải .pdf/.docx", "warn")

    def add_files(self, paths: list[str]) -> None:
        added = 0
        for p in paths:
            full = os.path.abspath(p)
            if full not in self.files:
                self.files.append(full)
                self.listbox.insert("end", f"  {os.path.basename(full)}")
                added += 1
        if added:
            self._say(f"Đã thêm {added} tài liệu", "ok")
        self._refresh_count()

    def _refresh_count(self) -> None:
        n = len(self.files)
        self.count_label.configure(
            text="Chưa có tài liệu nào" if n == 0 else f"{n} tài liệu")

    def pick_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Chọn tài liệu",
            filetypes=[("Tài liệu PDF/Word", "*.pdf *.docx"), ("Tất cả", "*.*")])
        self.add_files(expand_inputs(list(paths)))

    def pick_folder(self) -> None:
        folder = filedialog.askdirectory(title="Chọn thư mục chứa tài liệu")
        if folder:
            found = iter_input_files(folder)
            if not found:
                self._say("Thư mục không có file .pdf hoặc .docx nào", "warn")
            self.add_files(found)

    def pick_output(self) -> None:
        folder = filedialog.askdirectory(title="Chọn nơi lưu kết quả")
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
            messagebox.showerror("Không mở được thư mục", str(exc))

    # ---------- xử lý ----------

    def start(self) -> None:
        if self.running:
            return
        if not self.files:
            messagebox.showinfo("Chưa có tài liệu",
                                "Hãy kéo thả hoặc chọn ít nhất một file .pdf/.docx.")
            return
        out_dir = self.output_dir.get().strip()
        if not out_dir:
            messagebox.showinfo("Thiếu thư mục", "Hãy chọn nơi lưu kết quả.")
            return

        cfg = ChunkConfig(
            max_tokens=max(128, self.max_tokens.get()),
            min_tokens=max(0, self.min_tokens.get()),
            overlap_sentences=max(0, self.overlap.get()),
            include_path_prefix=self.add_prefix.get(),
            merge_short=self.merge_short.get(),
        )
        self.running = True
        self.run_btn.configure(state="disabled", text="Đang xử lý…")
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
        """Chạy trong luồng riêng để cửa sổ không bị treo."""
        put = self.events.put
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            put(("error", f"Không tạo được thư mục kết quả: {exc}"))
            put(("done", None))
            return

        rebuild = opts["make_clean"] and opts["rebuild"]
        # Bản dựng lại cần file ảnh để nhúng hình minh hoạ vào tài liệu mới
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
            except Exception as exc:  # một file hỏng không nên chặn cả lô
                failed += 1
                put(("error", f"    Lỗi: {type(exc).__name__}: {exc}"))
                put(("step", index))
                continue

            # Bản scan thì mọi thứ phía sau đều vô nghĩa — cây chỉ mục rỗng,
            # chunk chỉ có dòng [HÌNH]. Nói ngay ở đây chứ không đợi tới phần
            # báo cáo, vì người chỉ làm sạch tài liệu không chạy tới đó.
            scanned = scanned_warning(stats)
            if scanned:
                put(("warn", f"    ! {scanned}"))

            # Cây chỉ mục dựng xong ngay sau khi trích xuất, không phụ thuộc
            # vào việc có xuất chunk hay không — đây là thứ cần soát trước hết.
            tree = format_outline(sections, document_title(path))
            put(("outline", (name, tree)))
            if opts["save_outline"]:
                target = os.path.join(out_dir, f"{os.path.splitext(name)[0]}.outline.txt")
                try:
                    with open(target, "w", encoding="utf-8") as f:
                        f.write(tree + "\n")
                except OSError as exc:
                    put(("warn", f"    Không ghi được cây chỉ mục: {exc}"))

            if opts["make_clean"]:
                try:
                    if rebuild:
                        dst, cstats = rebuild_document(
                            sections, path, out_dir, out_format=opts["out_format"],
                            figure_dir=figure_dir,
                            drop_cover=opts["clean"].drop_cover,
                            max_tokens=cfg.max_tokens,
                            page_per_section=opts["page_per_section"])
                        note = (f"       {cstats['pages_out']} trang cho "
                                f"{cstats['sections_out']} mục · "
                                f"giữ {cstats['figures_kept']} hình")
                    else:
                        dst, cstats = clean_document(
                            path, out_dir, opts=opts["clean"])
                        note = (f"       gỡ {cstats['images_removed']} ảnh nhiễu · "
                                f"giữ {cstats['figures_kept']} hình trong file")
                    stats["cleaned_file"] = os.path.basename(dst)
                    stats.update({f"clean_{k}": v for k, v in cstats.items()})
                    put(("ok", f"    → {os.path.basename(dst)}"))
                    put(("info", note))
                except Exception as exc:
                    put(("error", f"    Không làm sạch được: {type(exc).__name__}: {exc}"))

            if not opts["make_jsonl"]:
                put(("step", index))
                continue

            info = check(chunks, sections, cfg, stats)
            info["file"] = name
            info["page_source"] = page_source
            summary.append(info)

            put(("info", f"    {info['chunks']} chunk · {info['headings_detected']} tiêu đề"
                         f" · cây {info['outline_depth']} cấp"
                         f" · token: trung vị {info['tokens_median']},"
                         f" max {info['tokens_max']}/{info['token_limit']}"))
            if info["logos_dropped"] or info["figures_kept"]:
                put(("info", f"    đã loại {info['logos_dropped']} logo · "
                             f"giữ {info['figures_kept']} hình"))
            for warning in info["warnings"]:
                put(("warn", f"    ! {warning}"))

            if not merge:
                target = os.path.join(out_dir, f"{os.path.splitext(name)[0]}.jsonl")
                try:
                    self._write_jsonl(target, chunks)
                    put(("ok", f"    → {os.path.basename(target)}"))
                except OSError as exc:
                    failed += 1
                    put(("error", f"    Không ghi được file: {exc}"))
            all_chunks.extend(chunks)
            put(("step", index))

        if merge and all_chunks:
            target = os.path.join(out_dir, "chunks.jsonl")
            try:
                self._write_jsonl(target, all_chunks)
                put(("ok", f"→ Đã gộp vào {os.path.basename(target)}"))
            except OSError as exc:
                put(("error", f"Không ghi được file gộp: {exc}"))

        try:
            with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            put(("warn", f"Không ghi được report.json: {exc}"))

        ok_count = len(files) - failed
        put(("head", ""))
        put(("ok" if not failed else "warn",
             f"Xong: {len(all_chunks)} chunk từ {ok_count}/{len(files)} tài liệu"))
        put(("done", None))

    @staticmethod
    def _write_jsonl(path: str, chunks) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for chunk in chunks:
                f.write(json.dumps(chunk.to_dict(), ensure_ascii=False) + "\n")

    # ---------- cầu nối luồng nền -> giao diện ----------

    def _drain_events(self) -> None:
        """Tkinter chỉ được cập nhật từ luồng chính, nên đọc qua hàng đợi."""
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "step":
                    self.progress.configure(value=payload)
                elif kind == "outline":
                    self.outlines.append(payload)
                elif kind == "done":
                    self.running = False
                    self.run_btn.configure(state="normal", text="▶  Bắt đầu xử lý")
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
        """Mở cửa sổ xem cây chỉ mục của các tài liệu vừa xử lý.

        Hệ RAG dựng title của chunk từ cây này, nên sai một cấp là sai title
        của mọi chunk bên dưới — cần soát lại bằng mắt trước khi nạp tri thức.
        """
        if not self.outlines:
            return
        win = tk.Toplevel(self.root)
        win.title("Cây chỉ mục — đối chiếu với tài liệu gốc")
        win.geometry("900x640")
        win.configure(bg=BG)

        wrap = ttk.Frame(win, padding=10)
        wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, style="Sub.TLabel",
                  text="Mỗi dòng là một đề mục; L1 là cấp cao nhất. Thiếu, thừa "
                       "hay sai cấp ở đây thì title của chunk sai theo.").pack(anchor="w")

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
    # Console Windows mặc định là cp1252, thông báo lỗi tiếng Việt sẽ vỡ mã
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    root = None
    if HAS_DND:
        # Bản tkdnd không khớp với Tk đang chạy sẽ hỏng ngay ở đây. Mất kéo thả
        # thì tiếc, nhưng không mở được giao diện mới là hỏng thật.
        try:
            root = TkinterDnD.Tk()
        except (tk.TclError, RuntimeError, OSError) as exc:
            print(f"Không bật được kéo thả ({exc}), mở giao diện thường.",
                  file=sys.stderr)
    if root is None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            print(f"Không mở được cửa sổ giao diện: {exc}", file=sys.stderr)
            print("Bạn vẫn có thể dùng bản dòng lệnh: python -m docindex.cli <đường dẫn>",
                  file=sys.stderr)
            return 1

    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
