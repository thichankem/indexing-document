"""Giao diện dòng lệnh: xử lý một file hoặc cả thư mục thành chunk JSONL."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .chunker import MAX_TOKENS, MIN_TOKENS, ChunkConfig
from .export import CleanOptions, clean_document, out_dir_for, rebuild_document
from .models import document_title
from .pipeline import iter_input_files, process_file
from .report import check, format_outline, format_report, scanned_warning


def _stdout_utf8() -> None:
    """Console Windows mặc định không in được tiếng Việt."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="docindex",
        description="Chia PDF/DOCX thành chunk theo mục lục, mỗi chunk gọn trong một trang.",
    )
    p.add_argument("input", help="File .pdf/.docx hoặc thư mục chứa chúng")
    p.add_argument("-o", "--output", default="output", help="Thư mục kết quả (mặc định: output)")
    p.add_argument("--max-tokens", type=int, default=MAX_TOKENS,
                   help=f"Trần token của một chunk (mặc định: {MAX_TOKENS})")
    p.add_argument("--min-tokens", type=int, default=MIN_TOKENS,
                   help="Ngưỡng gộp chunk quá ngắn (token)")
    p.add_argument("--overlap", type=int, default=1,
                   help="Số câu lặp lại khi một mục bị cắt ngang trang (0 = tắt)")
    p.add_argument("--keep-short-sections", action="store_true",
                   help="Giữ nguyên mục quá ngắn thay vì gộp vào mục liền trước")
    p.add_argument("--no-prefix", action="store_true",
                   help="Không chèn đường dẫn mục lục vào nội dung chunk")
    p.add_argument("--path-depth", type=int, default=4, help="Số cấp mục lục trong tiền tố")
    p.add_argument("--merge", action="store_true",
                   help="Gộp tất cả tài liệu vào một file chunks.jsonl duy nhất")
    p.add_argument("--preview", type=int, default=0, metavar="N",
                   help="In thử N chunk đầu của mỗi tài liệu")
    p.add_argument("--extract-figures", action="store_true",
                   help="Tách hình minh hoạ ra file PNG trong <output>/figures "
                        "(bản dựng lại luôn tự tách để nhúng hình vào tài liệu)")
    p.add_argument("--format", choices=["same", "docx", "pdf"], default="pdf",
                   help="Định dạng bản tài liệu dựng lại (mặc định: pdf — "
                        "định dạng mô hình DLA đọc chuẩn nhất)")
    p.add_argument("--keep-layout", action="store_true",
                   help="Giữ nguyên bố cục gốc thay vì dựng lại tài liệu")
    p.add_argument("--page-per-section", action="store_true",
                   help="Mỗi mục một trang riêng (mặc định: nội dung chảy liên "
                        "tục như tài liệu bình thường)")
    p.add_argument("--outline", action="store_true",
                   help="In cây chỉ mục của từng tài liệu để đối chiếu với bản gốc")
    p.add_argument("--no-clean", action="store_true",
                   help="Không xuất bản tài liệu đã làm sạch")
    p.add_argument("--no-jsonl", action="store_true",
                   help="Không xuất chunk JSONL, chỉ làm sạch tài liệu")
    p.add_argument("--keep-cover", action="store_true",
                   help="Giữ lại hình bìa ở trang đầu khi làm sạch")
    p.add_argument("--keep-logo", action="store_true",
                   help="Giữ lại logo và hoạ tiết lặp khi giữ bố cục gốc")
    p.add_argument("--keep-header-footer", action="store_true",
                   help="Giữ nguyên chữ đầu trang / chân trang khi giữ bố cục gốc")
    p.add_argument("--keep-toc", action="store_true",
                   help="Giữ nguyên phần mục lục khi giữ bố cục gốc")
    return p


def main(argv: list[str] | None = None) -> int:
    _stdout_utf8()
    args = build_parser().parse_args(argv)

    cfg = ChunkConfig(
        max_tokens=args.max_tokens,
        min_tokens=args.min_tokens,
        overlap_sentences=args.overlap,
        include_path_prefix=not args.no_prefix,
        max_path_depth=args.path_depth,
        merge_short=not args.keep_short_sections,
    )

    target = args.input
    if os.path.isdir(target):
        files = iter_input_files(target)
        in_root = target
    elif os.path.isfile(target):
        files = [target]
        in_root = os.path.dirname(os.path.abspath(target))
    else:
        print(f"Không tìm thấy: {target}", file=sys.stderr)
        return 1

    if not files:
        print("Không có file .pdf hoặc .docx nào trong thư mục.", file=sys.stderr)
        return 1

    os.makedirs(args.output, exist_ok=True)
    all_chunks = []
    summary = []
    failed = 0

    rebuild = not args.no_clean and not args.keep_layout
    clean_opts = CleanOptions(
        drop_logo=not args.keep_logo,
        drop_cover=not args.keep_cover,
        drop_header_footer=not args.keep_header_footer,
        drop_toc=not args.keep_toc,
    )

    for path in files:
        name = os.path.basename(path)
        # Cây thư mục đầu vào được giữ nguyên bên kết quả, nếu không hai file
        # trùng tên ở hai thư mục con sẽ ghi đè lên nhau
        file_out = out_dir_for(path, in_root, args.output)
        os.makedirs(file_out, exist_ok=True)
        print(f"\n>> {os.path.relpath(path, in_root)}")
        # Bản dựng lại cần file ảnh để nhúng hình minh hoạ vào tài liệu mới
        figure_dir = (os.path.join(args.output, "figures")
                      if args.extract_figures or rebuild else None)
        extract_stats: dict = {}

        try:
            chunks, sections, page_source = process_file(
                path, cfg, figure_dir=figure_dir, stats=extract_stats)
        except Exception as exc:  # một file lỗi không nên chặn cả lô
            failed += 1
            print(f"  [LỖI] {type(exc).__name__}: {exc}")
            continue

        # Bản scan thì mọi thứ phía sau đều vô nghĩa — nói ngay, trước khi bỏ
        # công dựng lại cả tài liệu.
        if scanned_warning(extract_stats):
            print("  [!] bản scan, chưa có lớp text — xem cảnh báo đầy đủ ở dưới")

        # Bản tài liệu đã làm sạch
        if not args.no_clean:
            try:
                if rebuild:
                    dst, clean_stats = rebuild_document(
                        sections, path, file_out, out_format=args.format,
                        figure_dir=figure_dir, drop_cover=clean_opts.drop_cover,
                        max_tokens=cfg.max_tokens,
                        page_per_section=args.page_per_section)
                    print(f"  -> {os.path.basename(dst)}  "
                          f"(dựng lại {clean_stats['pages_out']} trang cho "
                          f"{clean_stats['sections_out']} mục, "
                          f"giữ {clean_stats['figures_kept']} hình)")
                else:
                    dst, clean_stats = clean_document(
                        path, file_out, opts=clean_opts)
                    print(f"  -> {os.path.basename(dst)}  "
                          f"(gỡ {clean_stats['images_removed']} ảnh nhiễu, "
                          f"giữ {clean_stats['figures_kept']} hình)")
                extract_stats["cleaned_file"] = os.path.basename(dst)
                extract_stats.update({f"clean_{k}": v for k, v in clean_stats.items()})
            except Exception as exc:
                print(f"  [LỖI làm sạch] {type(exc).__name__}: {exc}")

        stats = check(chunks, sections, cfg, extract_stats)
        stats["file"] = os.path.relpath(path, in_root)
        stats["page_source"] = page_source
        summary.append(stats)
        print(format_report(name, stats))

        if args.outline:
            print(format_outline(sections, document_title(path)))

        if not args.merge and not args.no_jsonl:
            out_path = os.path.join(file_out, f"{os.path.splitext(name)[0]}.jsonl")
            _write_jsonl(out_path, chunks)
            print(f"  -> {os.path.basename(out_path)}")
        all_chunks.extend(chunks)

        for chunk in chunks[: args.preview]:
            print(f"  --- {chunk.chunk_id} | trang {chunk.page} | "
                  f"{chunk.est_tokens} token / {chunk.char_count} ký tự")
            print("      " + chunk.text[:300].replace("\n", "\n      "))

    if args.merge and not args.no_jsonl:
        out_path = os.path.join(args.output, "chunks.jsonl")
        _write_jsonl(out_path, all_chunks)
        print(f"\n-> {out_path}")

    report_path = os.path.join(args.output, "report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\nTổng: {len(all_chunks)} chunk từ {len(files) - failed}/{len(files)} tài liệu")
    print(f"Báo cáo: {report_path}")
    return 1 if failed and not all_chunks else 0


def _write_jsonl(path: str, chunks) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk.to_dict(), ensure_ascii=False) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
