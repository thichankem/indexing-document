"""Tiện ích dùng lại metadata lúc truy vấn.

Chunk được cắt nhỏ để tìm kiếm cho chính xác, nhưng khi đưa cho LLM trả lời
thì cần đủ ngữ cảnh. Hai hàm dưới đây khôi phục lại phần nội dung bị cắt do
ranh giới trang.
"""
from __future__ import annotations

import json


def load_chunks(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def index_by_id(chunks: list[dict]) -> dict[str, dict]:
    return {c["chunk_id"]: c for c in chunks}


def expand_section(chunk: dict, by_id: dict[str, dict], max_parts: int = 6) -> str:
    """Ghép lại toàn bộ các phần của cùng một mục quanh chunk đã tìm được.

    Dùng khi chunk khớp truy vấn có is_continued/is_continuation = True: một
    mình nó chỉ là một nửa nội dung của mục.
    """
    parts = [chunk]

    node = chunk
    while node.get("is_continuation") and node.get("prev_chunk_id") and len(parts) < max_parts:
        prev = by_id.get(node["prev_chunk_id"])
        if prev is None or prev["section_path"] != chunk["section_path"]:
            break
        parts.insert(0, prev)
        node = prev

    node = chunk
    while node.get("is_continued") and node.get("next_chunk_id") and len(parts) < max_parts:
        nxt = by_id.get(node["next_chunk_id"])
        if nxt is None or nxt["section_path"] != chunk["section_path"]:
            break
        parts.append(nxt)
        node = nxt

    header = chunk["section_path"]
    body = "\n".join(p["raw_text"] for p in parts)
    return f"{header}\n{body}"


def neighbours(chunk: dict, by_id: dict[str, dict], window: int = 1) -> list[dict]:
    """Lấy các chunk liền kề bất kể cùng mục hay không (mở rộng ngữ cảnh rộng)."""
    out = [chunk]
    node = chunk
    for _ in range(window):
        prev = by_id.get(node.get("prev_chunk_id") or "")
        if prev is None:
            break
        out.insert(0, prev)
        node = prev
    node = chunk
    for _ in range(window):
        nxt = by_id.get(node.get("next_chunk_id") or "")
        if nxt is None:
            break
        out.append(nxt)
        node = nxt
    return out
