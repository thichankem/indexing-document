"""Ghép các bước xử lý thành một luồng hoàn chỉnh cho từng file."""
from __future__ import annotations

import hashlib
import os
import re

from . import extract_docx, extract_pdf
from .chunker import ChunkConfig, build_chunks
from .headings import build_sections, detect_headings, merge_short_sections
from .models import Chunk, Section

SUPPORTED = {".pdf", ".docx"}


def make_doc_id(path: str) -> str:
    """Mã tài liệu ổn định: tên file rút gọn + băm đường dẫn."""
    name = os.path.splitext(os.path.basename(path))[0]
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()[:48]
    digest = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}" if slug else digest


def process_file(path: str, cfg: ChunkConfig | None = None,
                 figure_dir: str | None = None,
                 stats: dict | None = None) -> tuple[list[Chunk], list[Section], str]:
    ext = os.path.splitext(path)[1].lower()
    doc_id = make_doc_id(path)
    if ext == ".pdf":
        blocks, page_source = extract_pdf.extract(
            path, figure_dir=figure_dir, doc_id=doc_id, stats=stats)
        source = "pdf"
    elif ext == ".docx":
        blocks, page_source = extract_docx.extract(
            path, figure_dir=figure_dir, doc_id=doc_id, stats=stats)
        source = "docx"
    else:
        raise ValueError(f"Định dạng chưa hỗ trợ: {ext}")

    cfg = cfg or ChunkConfig()
    blocks = detect_headings(blocks, source)
    sections = build_sections(blocks)
    if cfg.merge_short:
        sections = merge_short_sections(sections, cfg.min_tokens, cfg.max_tokens)
    chunks = build_chunks(
        sections,
        doc_id=doc_id,
        source_file=os.path.basename(path),
        page_source=page_source,
        cfg=cfg,
    )
    return chunks, sections, page_source


def iter_input_files(folder: str) -> list[str]:
    out: list[str] = []
    for root, _dirs, files in os.walk(folder):
        for name in sorted(files):
            if name.startswith("~$"):
                continue  # file tạm của Word
            if os.path.splitext(name)[1].lower() in SUPPORTED:
                out.append(os.path.join(root, name))
    return out
