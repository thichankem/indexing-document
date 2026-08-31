"""Command line interface: turn a single file or a whole folder into JSONL chunks."""
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
    """The default Windows console cannot print non-ASCII text."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="docindex",
        description="Split PDF/DOCX documents into outline-aware chunks, each fitting inside one page.",
    )
    p.add_argument("input", help="A .pdf/.docx file, or a folder containing them")
    p.add_argument("-o", "--output", default="output", help="Output folder (default: output)")
    p.add_argument("--max-tokens", type=int, default=MAX_TOKENS,
                   help=f"Token ceiling for a single chunk (default: {MAX_TOKENS})")
    p.add_argument("--min-tokens", type=int, default=MIN_TOKENS,
                   help="Threshold below which short sections are merged (tokens)")
    p.add_argument("--overlap", type=int, default=1,
                   help="Sentences repeated when a section is split across pages (0 = off)")
    p.add_argument("--keep-short-sections", action="store_true",
                   help="Keep very short sections instead of merging them into a neighbour")
    p.add_argument("--no-prefix", action="store_true",
                   help="Do not prepend the outline path to the chunk text")
    p.add_argument("--path-depth", type=int, default=4, help="Number of outline levels kept in the prefix")
    p.add_argument("--merge", action="store_true",
                   help="Merge every document into a single chunks.jsonl file")
    p.add_argument("--preview", type=int, default=0, metavar="N",
                   help="Print the first N chunks of each document")
    p.add_argument("--extract-figures", action="store_true",
                   help="Export figures as PNG files into <output>/figures "
                        "(the rebuilt document always extracts them in order to embed them)")
    p.add_argument("--format", choices=["same", "docx", "pdf"], default="pdf",
                   help="Format of the rebuilt document (default: pdf — the format "
                        "a DLA model reads most reliably)")
    p.add_argument("--keep-layout", action="store_true",
                   help="Keep the original layout instead of rebuilding the document")
    p.add_argument("--page-per-section", action="store_true",
                   help="Give every section its own page (default: content flows "
                        "continuously, like an ordinary document)")
    p.add_argument("--outline", action="store_true",
                   help="Print each document's outline tree so it can be checked against the original")
    p.add_argument("--no-clean", action="store_true",
                   help="Do not write the cleaned document")
    p.add_argument("--no-jsonl", action="store_true",
                   help="Do not write JSONL chunks, only clean the document")
    p.add_argument("--keep-cover", action="store_true",
                   help="Keep the cover image on the first page when cleaning")
    p.add_argument("--keep-logo", action="store_true",
                   help="Keep logos and repeated ornaments when keeping the original layout")
    p.add_argument("--keep-header-footer", action="store_true",
                   help="Keep header / footer text when keeping the original layout")
    p.add_argument("--keep-toc", action="store_true",
                   help="Keep the table of contents when keeping the original layout")
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
        print(f"Not found: {target}", file=sys.stderr)
        return 1

    if not files:
        print("No .pdf or .docx files found in that folder.", file=sys.stderr)
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
        # The input folder tree is mirrored in the output, otherwise two files
        # with the same name in different subfolders would overwrite each other
        file_out = out_dir_for(path, in_root, args.output)
        os.makedirs(file_out, exist_ok=True)
        print(f"\n>> {os.path.relpath(path, in_root)}")
        # The rebuilt document needs the image files in order to embed figures
        figure_dir = (os.path.join(args.output, "figures")
                      if args.extract_figures or rebuild else None)
        extract_stats: dict = {}

        try:
            chunks, sections, page_source = process_file(
                path, cfg, figure_dir=figure_dir, stats=extract_stats)
        except Exception as exc:  # one bad file should not stop the whole batch
            failed += 1
            print(f"  [ERROR] {type(exc).__name__}: {exc}")
            continue

        # For a scanned document everything downstream is meaningless — say so
        # right away, before spending the effort of rebuilding it.
        if scanned_warning(extract_stats):
            print("  [!] scanned document, no text layer — see the full warning below")

        # The cleaned document
        if not args.no_clean:
            try:
                if rebuild:
                    dst, clean_stats = rebuild_document(
                        sections, path, file_out, out_format=args.format,
                        figure_dir=figure_dir, drop_cover=clean_opts.drop_cover,
                        max_tokens=cfg.max_tokens,
                        page_per_section=args.page_per_section)
                    print(f"  -> {os.path.basename(dst)}  "
                          f"(rebuilt {clean_stats['pages_out']} pages for "
                          f"{clean_stats['sections_out']} sections, "
                          f"kept {clean_stats['figures_kept']} figures)")
                else:
                    dst, clean_stats = clean_document(
                        path, file_out, opts=clean_opts)
                    print(f"  -> {os.path.basename(dst)}  "
                          f"(removed {clean_stats['images_removed']} noise images, "
                          f"kept {clean_stats['figures_kept']} figures)")
                extract_stats["cleaned_file"] = os.path.basename(dst)
                extract_stats.update({f"clean_{k}": v for k, v in clean_stats.items()})
            except Exception as exc:
                print(f"  [CLEANING ERROR] {type(exc).__name__}: {exc}")

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
            print(f"  --- {chunk.chunk_id} | page {chunk.page} | "
                  f"{chunk.est_tokens} tokens / {chunk.char_count} chars")
            print("      " + chunk.text[:300].replace("\n", "\n      "))

    if args.merge and not args.no_jsonl:
        out_path = os.path.join(args.output, "chunks.jsonl")
        _write_jsonl(out_path, all_chunks)
        print(f"\n-> {out_path}")

    report_path = os.path.join(args.output, "report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\nTotal: {len(all_chunks)} chunks from {len(files) - failed}/{len(files)} documents")
    print(f"Report: {report_path}")
    return 1 if failed and not all_chunks else 0


def _write_jsonl(path: str, chunks) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk.to_dict(), ensure_ascii=False) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
