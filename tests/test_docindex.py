"""Kiểm tra các ràng buộc cốt lõi của tool trên bộ tài liệu thật."""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docindex.chunker import (
    ChunkConfig, _split_long_text, _split_table, est_tokens,
)
from docindex.headings import (
    _match_heading, build_sections, detect_headings, shorten_title,
)
from docindex.images import CAPTION, DocImage
from docindex.models import (
    Block, Section, clean_text, normalize_symbols, rows_to_markdown,
)
from docindex.numbering import _format_counter, _to_letter, _to_roman
from docindex.pipeline import iter_input_files, process_file

TEST_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testing file")
CFG = ChunkConfig()


def _docs():
    if not os.path.isdir(TEST_DIR):
        pytest.skip("thư mục 'testing file' không tồn tại")
    files = iter_input_files(TEST_DIR)
    if not files:
        pytest.skip("không có file pdf/docx để kiểm tra")
    return files


@pytest.fixture(scope="module")
def processed():
    out = []
    for path in _docs():
        stats: dict = {}
        chunks, sections, src = process_file(path, CFG, stats=stats)
        out.append((path, chunks, sections, src, stats))
    return out


# --- đơn vị nhỏ ---------------------------------------------------------

def test_roman_and_letter_counters():
    assert _to_roman(4) == "iv"
    assert _to_roman(2026) == "mmxxvi"
    assert _to_letter(1) == "a"
    assert _to_letter(27) == "aa"
    assert _format_counter(3, "upperRoman") == "III"
    assert _format_counter(2, "decimalZero") == "02"


def test_clean_text_removes_invisible_chars():
    assert clean_text("1.​Phạm vi") == "1.Phạm vi"
    assert clean_text("a  b") == "a b"


def test_heading_patterns():
    assert _match_heading("ĐIỀU 2: MỘT SỐ QUY ĐỊNH")[0] == "ĐIỀU 2"
    assert _match_heading("2.3.1. Nghĩa vụ cung cấp")[0] == "2.3.1"
    assert _match_heading("PHẦN 1: QUY TẮC CHUNG")[3] == "part"
    assert _match_heading("Đây là câu văn bình thường") is None


def test_reference_phrase_is_not_a_heading():
    """Mảnh câu dẫn chiếu từng bị nhận nhầm và nuốt trọn các mục phía sau."""
    blocks = [
        Block(text="Phần 1 này", page=1, bold=False),
        Block(text="ĐIỀU 3: PHÍ BẢO HIỂM", page=1, bold=True),
    ]
    detect_headings(blocks, "pdf")
    assert blocks[0].kind != "heading"
    assert blocks[1].kind == "heading"


def test_split_long_text_respects_limit():
    text = ". ".join(f"Câu số {i} trong đoạn văn dài" for i in range(80))
    parts = _split_long_text(text, 200)
    assert parts and all(len(p) <= 200 for p in parts)


def test_split_table_repeats_header():
    md = "\n".join(["| A | B |", "| --- | --- |"] + [f"| {i} | giá trị {i} |" for i in range(60)])
    parts = _split_table(md, 300)
    assert len(parts) > 1
    assert all(p.startswith("| A | B |") for p in parts)


# --- ràng buộc trên tài liệu thật ---------------------------------------

def test_every_document_produces_chunks(processed):
    for path, chunks, _sections, _src, _st in processed:
        assert chunks, f"không tạo được chunk nào cho {os.path.basename(path)}"


def test_chunk_never_spans_two_pages(processed):
    """Ràng buộc chính: nội dung trong một chunk phải cùng thuộc một trang.

    Đối chiếu từng dòng của chunk với trang gốc của nó trong tài liệu. Dòng nào
    chỉ xuất hiện ở một trang duy nhất mà khác trang của chunk là lỗi.
    """
    for path, chunks, sections, _src, _st in processed:
        pages_of_line: dict[str, set[int]] = {}
        for section in sections:
            for block in section.blocks:
                for line in block.text.split("\n"):
                    line = line.strip()
                    if len(line) > 40:
                        pages_of_line.setdefault(line, set()).add(block.page)

        for chunk in chunks:
            assert isinstance(chunk.page, int) and chunk.page >= 1
            for line in chunk.raw_text.split("\n"):
                line = line.strip()
                pages = pages_of_line.get(line)
                if pages and len(pages) == 1:
                    assert chunk.page in pages, (
                        f"{os.path.basename(path)} {chunk.chunk_id}: dòng thuộc trang "
                        f"{pages} nhưng chunk khai báo trang {chunk.page}"
                    )


def test_chunk_never_exceeds_token_limit(processed):
    """Ràng buộc cứng của hệ RAG: 512 token. Vượt là bị cắt cụt lúc embedding."""
    for path, chunks, _sections, _src, _st in processed:
        for chunk in chunks:
            assert chunk.est_tokens <= CFG.max_tokens, (
                f"{os.path.basename(path)} {chunk.chunk_id}: {chunk.est_tokens} "
                f"token > {CFG.max_tokens}"
            )
            assert chunk.est_tokens == est_tokens(chunk.text)


def test_token_estimate_is_not_optimistic():
    """Ước lượng token phải cao hơn thực tế, không được thấp hơn.

    Đếm thiếu thì chunk lọt qua mọi kiểm tra rồi mới bị cắt cụt ở khâu
    embedding — hỏng âm thầm, nên thà ước lượng dư.
    """
    plain = "Khách hàng cá nhân ưu tiên được hưởng chính sách lãi suất ưu đãi."
    assert est_tokens(plain) >= len(plain.split())
    table = "| Phân hạng | Mức giảm |\n| --- | --- |\n| Diamond | 0,2% |"
    assert est_tokens(table) >= table.count("|")
    assert est_tokens("") == 0


def test_chunk_ids_unique(processed):
    for _path, chunks, _sections, _src, _st in processed:
        ids = [c.chunk_id for c in chunks]
        assert len(ids) == len(set(ids))


def test_chunk_links_are_consistent(processed):
    for _path, chunks, _sections, _src, _st in processed:
        by_id = {c.chunk_id: c for c in chunks}
        for chunk in chunks:
            if chunk.prev_chunk_id:
                assert by_id[chunk.prev_chunk_id].next_chunk_id == chunk.chunk_id
            if chunk.next_chunk_id:
                assert by_id[chunk.next_chunk_id].prev_chunk_id == chunk.chunk_id


def test_continuation_flags_match_section(processed):
    """is_continued chỉ đúng khi chunk kế tiếp thuộc cùng một mục."""
    for _path, chunks, _sections, _src, _st in processed:
        by_id = {c.chunk_id: c for c in chunks}
        for chunk in chunks:
            if chunk.is_continued:
                nxt = by_id[chunk.next_chunk_id]
                assert nxt.section_path == chunk.section_path
                assert nxt.is_continuation
            if chunk.is_continuation:
                prev = by_id[chunk.prev_chunk_id]
                assert prev.section_path == chunk.section_path


def test_section_path_starts_the_text(processed):
    for _path, chunks, _sections, _src, _st in processed:
        for chunk in chunks:
            if chunk.section_path:
                assert chunk.text.startswith(chunk.section_path.split(" > ")[0][:20])


def test_most_chunks_belong_to_a_section(processed):
    """Nếu nhận diện tiêu đề hỏng, phần lớn chunk sẽ rơi vào 'Phần mở đầu'."""
    for path, chunks, _sections, _src, _st in processed:
        orphan = [c for c in chunks if c.section_path == "Phần mở đầu"]
        assert len(orphan) <= max(2, len(chunks) * 0.15), os.path.basename(path)


def test_headings_are_detected(processed):
    for path, _chunks, sections, _src, _st in processed:
        numbered = [s for s in sections if s.number]
        assert numbered, f"không nhận được tiêu đề nào trong {os.path.basename(path)}"


def test_no_dot_leader_garbage(processed):
    """Trang mục lục phải bị loại, không sinh chunk toàn dấu chấm."""
    for _path, chunks, _sections, _src, _st in processed:
        for chunk in chunks:
            assert "........" not in chunk.text


# --- lọc logo / giữ hình minh hoạ ---------------------------------------

def test_caption_pattern():
    assert CAPTION.match("Hình 1: Quy trình xử lý")
    assert CAPTION.match("Biểu đồ 2 - Tăng trưởng")
    assert CAPTION.match("Sơ đồ 3. Cơ cấu tổ chức")
    assert not CAPTION.match("Hình thức thanh toán theo quy định")


def test_figure_placeholder_format():
    img = DocImage(page=2, bbox=(0, 0, 300, 200), width=300, height=200,
                   kind="figure", reason="test", caption="Biểu đồ 1: Doanh thu")
    text = img.placeholder()
    assert text.startswith("[HÌNH:")
    assert "Biểu đồ 1" in text and "300x200" in text


def test_logos_are_dropped_and_not_in_text(processed):
    """Logo lặp ở đầu/cuối trang không được lọt vào nội dung chunk."""
    total_logos = sum(st.get("logos_dropped", 0) for _p, _c, _s, _src, st in processed)
    assert total_logos > 0, "không loại được logo nào trên bộ tài liệu thật"

    for _path, chunks, _sections, _src, _st in processed:
        for chunk in chunks:
            for line in chunk.raw_text.split("\n"):
                if line.strip().startswith("[HÌNH:"):
                    continue
                assert ".png" not in line.lower() and ".jpeg" not in line.lower()


def test_figures_are_kept_with_metadata(processed):
    """Hình nội dung phải được giữ chỗ kèm metadata, không bị xoá nhầm."""
    with_fig = [c for _p, chunks, _s, _src, _st in processed for c in chunks if c.has_figure]
    assert with_fig, "không giữ được hình minh hoạ nào"
    for chunk in with_fig:
        assert chunk.figures
        assert "[HÌNH:" in chunk.raw_text
        for fig in chunk.figures:
            assert fig["width"] and fig["height"]


def test_figure_flag_matches_content(processed):
    for _path, chunks, _sections, _src, _st in processed:
        for chunk in chunks:
            if "[HÌNH:" in chunk.raw_text:
                assert chunk.has_figure, f"{chunk.chunk_id} có hình nhưng không đánh dấu"


def test_letter_marker_nests_under_decimal():
    """"a)" là mục con của "2.", không phải anh em của nó.

    Tài liệu Word thật hay khai danh sách "a) b) c)" ở cùng cấp `ilvl` với
    "1. 2. 3.". Tin theo cấp đó thì cây chỉ mục bị phẳng, và title của mọi
    chunk bên dưới mất một tầng.
    """
    from docindex.headings import build_sections

    blocks = [
        Block(text="Chính sách lãi suất huy động", page=1, bold=True,
              number="2.", level=1, meta={"list_format": "decimal"}),
        Block(text="Phạm vi áp dụng", page=1, bold=True,
              number="a)", level=1, meta={"list_format": "lowerLetter"}),
    ]
    detect_headings(blocks, "docx")
    sections = build_sections(blocks)
    assert [s.level for s in sections] == [1, 2]
    assert sections[1].path == ["2. Chính sách lãi suất huy động", "a) Phạm vi áp dụng"]


def test_sentence_starting_with_a_number_is_not_a_heading():
    """"30 (ba mươi) ngày tuổi…" là câu văn bị PDF cắt dòng, không phải mục 30.

    Nhận nhầm một dòng như vậy không chỉ đẻ ra mục ma: mọi mục thật phía sau
    tụt xuống làm con của nó và title của cả nhánh sai theo.
    """
    from docindex.headings import build_sections

    blocks = [
        Block(text="1.25 Người được bảo hiểm: là cá nhân được chấp thuận",
              page=1, bold=True),
        Block(text="30 (ba mươi) ngày tuổi đến 70 (bảy mươi) tuổi tại thời điểm",
              page=1, bold=True),
        Block(text="1.26 Người thụ hưởng: là cá nhân được chỉ định", page=1, bold=True),
    ]
    detect_headings(blocks, "pdf")
    assert blocks[1].kind != "heading"
    sections = build_sections(blocks)
    assert [s.number for s in sections] == ["1.25", "1.26"]
    assert [s.level for s in sections] == [1, 1], "mục thật bị tụt cấp"


def test_padded_number_is_a_quantity_not_an_index():
    """"04 (bốn) Năm hợp đồng" là số lượng — chỉ mục không đệm số 0."""
    blocks = [
        Block(text="3.1 Quy định chung về Phí bảo hiểm", page=1, bold=True),
        Block(text="04 (bốn) Năm hợp đồng đầu tiên.", page=1, bold=True),
        Block(text="3.2 Thời gian gia hạn đóng phí", page=1, bold=True),
    ]
    detect_headings(blocks, "pdf")
    assert blocks[1].kind != "heading"
    assert [b.number for b in blocks if b.kind == "heading"] == ["3.1", "3.2"]


def test_missing_parent_heading_does_not_drop_the_subtree():
    """Tiêu đề cha bị trích xuất sót thì mục con vẫn phải được giữ."""
    blocks = [
        Block(text="2.2.5 Nội dung mục con cuối", page=1, bold=True),
        Block(text="2.3.1 Nội dung mục con đầu của nhánh sau", page=1, bold=True),
    ]
    detect_headings(blocks, "pdf")
    assert [b.number for b in blocks if b.kind == "heading"] == ["2.2.5", "2.3.1"]


def test_section_name_stops_at_the_colon():
    """Tên mục là phần trước dấu hai chấm, dù cả dòng chưa dài quá trần.

    Văn bản hành chính viết liền cả câu vào dòng tiêu đề. Để nguyên thì cả câu
    bị in đậm cỡ tiêu đề và title của mọi chunk trong mục đọc như văn xuôi.
    """
    assert shorten_title(
        "Hợp đồng bảo hiểm: là tất cả văn bản thể hiện sự thỏa thuận giữa hai bên"
    ) == "Hợp đồng bảo hiểm"
    assert shorten_title("Điều kiện áp dụng:") == "Điều kiện áp dụng"
    # dấu hai chấm giữa một câu dài thì không phải ranh giới tên mục
    long_head = "Trường hợp Khách hàng " + "rất dài " * 20
    assert shorten_title(long_head + ": nội dung") != long_head.strip()
    # không cắt trúng giờ giấc hay tỉ lệ
    assert shorten_title("Khung 8:30 tới 17:00") == "Khung 8:30 tới 17:00"


def test_run_on_heading_moves_its_body_down(processed):
    """Phần viết liền sau tên mục phải nằm ở thân bài, không phải ở dòng tiêu đề."""
    from docindex.layout import _content_items, _heading_and_rest

    checked = 0
    for _path, _chunks, sections, _src, _st in processed:
        for section in sections:
            # Mục đã gộp chỉ mục mang tên của nhiều tiêu đề cộng lại nên dài
            # hơn dòng tiêu đề gốc của chính nó — đó là chủ ý, không phải lỗi
            if ":" not in section.full_heading or not section.number or section.is_merged:
                continue
            name, rest = _heading_and_rest(section)
            if not rest:
                continue
            checked += 1
            assert len(name) <= len(section.full_heading)
            assert ":" not in name.rstrip(":"), f"tên mục còn dính nội dung: {name!r}"
            body = " ".join(i.text for i in _content_items(section, True))
            assert rest.split()[0] in body, "phần viết liền bị rơi khỏi thân bài"
    assert checked, "không có mục nào viết liền nội dung để kiểm tra"


def test_hyphenated_word_split_across_lines_is_rejoined():
    """"…và Dai-" ở cuối dòng nối với "ichi Life…" thành "Dai-ichi Life"."""
    from docindex.extract_pdf import _merge_wrapped

    lines = [
        Block(text="Hợp đồng bảo hiểm là thỏa thuận giữa Bên mua và Dai-", page=1,
              bold=True, size=11, meta={"x0": 70, "y": 100}),
        Block(text="ichi Life Việt Nam trên cơ sở yêu cầu bảo hiểm.", page=1,
              bold=False, size=11, meta={"x0": 70, "y": 114}),
    ]
    merged = _merge_wrapped(lines)
    assert len(merged) == 1
    assert "Dai-ichi Life Việt Nam" in merged[0].text


def test_short_sections_are_merged_without_losing_text():
    """Mục ngắn bị gộp thì mất nút trong cây, nhưng không được mất chữ."""
    from docindex.headings import build_sections, merge_short_sections

    blocks = [
        Block(text="1. Giải thích từ ngữ", page=1, bold=True),
        Block(text="1.1 Bệnh: là tình trạng sức khỏe kém", page=1, bold=True),
        Block(text="1.2 Khoản nợ: là khoản tiền đến hạn", page=1, bold=True),
    ]
    detect_headings(blocks, "pdf")
    sections = build_sections(blocks)
    assert len(sections) == 3

    merged = merge_short_sections(sections, min_tokens=60, max_tokens=512)
    assert len(merged) == 1, "mục ngắn chưa được gộp"
    text = merged[0].heading_text + " " + " ".join(b.text for b in merged[0].blocks)
    for word in ("Bệnh", "sức khỏe kém", "Khoản nợ", "đến hạn"):
        assert word in text, f"mất chữ khi gộp: {word}"

    kept = merge_short_sections(sections, min_tokens=0, max_tokens=512)
    assert len(kept) == 3, "min_tokens=0 thì không được gộp gì"


def test_upper_roman_ranks_above_decimal():
    """"I. THÔNG TIN CHUNG" là cấp lớn, đứng trên "10. Điều kiện"."""
    assert _match_heading("I. THÔNG TIN CHUNG")[2] < _match_heading("10. Điều kiện")[2]
    assert _match_heading("I. THÔNG TIN CHUNG")[3] == "roman-upper"


def test_bare_lowercase_i_stays_inside_the_letter_list():
    """"i)" giữa danh sách a) b) c)… là mục thứ 9, không phải một cấp mới."""
    assert _match_heading("i) Đơn phương hủy bỏ")[2] == _match_heading("h) Nội dung")[2]
    # còn "(i)" trong ngoặc thì đúng là cấp sâu nhất
    assert _match_heading("(i) Đơn phương hủy bỏ")[2] > _match_heading("a) Nội dung")[2]


def test_letter_marker_keeps_its_separator():
    """Ký hiệu đầu mục phải giữ nguyên dấu ("c." chứ không thành "c")."""
    assert _match_heading("c. Ngày đáo hạn hợp đồng")[0] == "c."
    assert _match_heading("a) Hội viên gắn kết")[0] == "a)"
    assert _match_heading("(i) Đơn phương hủy bỏ")[0] == "(i)"


def test_heading_line_not_duplicated_in_chunk(processed):
    """Tiêu đề đã nằm ở tiền tố thì không lặp lại ngay dòng dưới."""
    for _path, chunks, _sections, _src, _st in processed:
        for chunk in chunks:
            lines = [l for l in chunk.text.split("\n") if l.strip()]
            if len(lines) >= 2:
                assert lines[0].strip() != lines[1].strip(), chunk.chunk_id


def test_shorten_title_keeps_meaning():
    long_title = ("Sản phẩm áp dụng: Tiết kiệm thường, Tiết kiệm lẻ ngày, "
                  "Tiết kiệm bậc thang, Tiền gửi có kỳ hạn và nhiều loại khác nữa")
    assert shorten_title(long_title) == "Sản phẩm áp dụng"
    assert shorten_title("Hội viên gắn kết") == "Hội viên gắn kết"
    assert len(shorten_title("x" * 300)) <= 91


def test_flattened_table_row_names_the_section_by_its_content():
    """Hàng bảng làm phẳng: tên mục là giá trị mang nghĩa, không phải tên cột.

    Bước tiền xử lý bên ngoài trải mỗi hàng bảng thành một dòng
    "STT: 3.1 | Nội dung: Đối tượng khách hàng | Cụ thể: …". Cắt tại dấu hai
    chấm đầu tiên như tiêu đề thường thì mọi hàng đều mang tên "STT" và cây chỉ
    mục không còn phân biệt được mục nào với mục nào.
    """
    row = "STT: 3.1 | Nội dung: Đối tượng khách hàng | Cụ thể:"
    assert shorten_title(row) == "Đối tượng khách hàng"
    # cột số thứ tự bị bỏ qua dù đứng ở đâu
    assert shorten_title("Nội dung: Điều kiện vay | STT: 3.2") == "Điều kiện vay"
    # không còn cột nào ngắn thì mới lấy tới cột nội dung dài
    long_row = "STT: 3.5 | Nội dung: | Cụ thể: Lãi suất theo quy định của Ngân hàng"
    assert shorten_title(long_row) == "Lãi suất theo quy định của Ngân hàng"
    # hàng nối tiếp, mọi cột đều trống -> mục không có tên, chỉ còn số
    assert shorten_title("STT: | Nội dung: | Cụ thể:") == ""
    # tiêu đề thường có dấu sổ đứng thì vẫn cắt theo luật cũ
    assert shorten_title("Hợp đồng bảo hiểm: là văn bản thỏa thuận") == "Hợp đồng bảo hiểm"


def test_tree_stops_at_level_three():
    """Từ cấp 4 ("1.1.1.1") trở xuống không vào cây nữa, thành nội dung thường."""
    lines = ["1. Phạm vi", "1.1 Đối tượng", "1.1.1 Cá nhân",
             "1.1.1.1 Đủ 18 tuổi trở lên", "1.1.1.1.1 Sâu hơn nữa", "1.2 Loại tiền"]
    blocks = [Block(text=t, page=1, bold=True) for t in lines]
    sections = [s for s in build_sections(detect_headings(blocks, "pdf"))
                if not s.is_preamble]
    assert [(s.level, s.heading_str) for s in sections] == [
        (1, "1 Phạm vi"), (2, "1.1 Đối tượng"),
        (3, "1.1.1 Cá nhân"), (2, "1.2 Loại tiền"),
    ]
    # hai dòng cấp 4/5 nằm lại làm nội dung của mục cấp 3, giữ nguyên số mục
    body = [b.text for b in sections[2].blocks]
    assert body == ["1.1.1.1 Đủ 18 tuổi trở lên", "1.1.1.1.1 Sâu hơn nữa"]


def test_demoted_heading_keeps_its_number_from_word_numbering():
    """DOCX giữ số mục ngoài text: hạ cấp mà quên ghép lại là mất số."""
    from docindex.headings import _demote

    block = Block(text="Đủ 18 tuổi trở lên", page=1, number="1.1.1.1", kind="heading")
    _demote(block)
    assert block.text == "1.1.1.1 Đủ 18 tuổi trở lên"
    assert block.kind == "para" and block.number is None
    # PDF thì số đã nằm sẵn trong text, không được ghép thêm lần nữa
    pdf_block = Block(text="1.1.1.1 Đủ 18 tuổi", page=1, number="1.1.1.1", kind="heading")
    _demote(pdf_block)
    assert pdf_block.text == "1.1.1.1 Đủ 18 tuổi"


def test_merging_a_short_section_adds_up_the_index():
    """Gộp 1.2 vào 1.1 thì chỉ mục cộng vào, không chỉ gộp mỗi nội dung."""
    from docindex.headings import merge_short_sections

    def sect(number, title, body):
        return Section(number=number, title=title, level=2,
                       path=["1. Gốc", f"{number} {title}"],
                       full_heading=f"{number} {title}",
                       blocks=[Block(text=body, page=1)], page_start=1, page_end=1)

    long_body = "nội dung dài " * 60
    out = merge_short_sections(
        [sect("1.1", "Mục đích", long_body), sect("1.2", "Loại tiền", "ngắn")],
        min_tokens=60, max_tokens=512)
    assert len(out) == 1
    assert out[0].number == "1.1 + 1.2"
    assert out[0].title == "Mục đích + Loại tiền"
    assert out[0].path == ["1. Gốc", "1.1 + 1.2 Mục đích + Loại tiền"]
    assert "ngắn" in " ".join(b.text for b in out[0].blocks), "mất nội dung mục bị gộp"


def test_short_section_stays_separate_when_the_merge_would_bust_the_limit():
    """Gộp mà vượt 512 token thì vẫn phải tách ra."""
    from docindex.headings import merge_short_sections

    def sect(number, body):
        return Section(number=number, title="T", level=2,
                       path=["1. Gốc", f"{number} T"], full_heading=f"{number} T",
                       blocks=[Block(text=body, page=1)], page_start=1, page_end=1)

    almost_full = "chữ " * 470        # ~500 token, gộp thêm là tràn
    out = merge_short_sections([sect("1.1", almost_full), sect("1.2", "ngắn")],
                               min_tokens=60, max_tokens=512)
    assert [s.number for s in out] == ["1.1", "1.2"]


def test_merge_room_counts_the_path_prefix():
    """Trần 512 là trần của chunk, mà chunk còn mang cả tiền tố mục lục.

    Bỏ quên tiền tố thì mục gộp xong vẫn bị khâu chunk cắt làm đôi — gộp thành
    công cốc, lại còn đẻ ra hai chunk mang cùng một cái tên.
    """
    from docindex.headings import merge_short_sections

    long_path = ["PHẦN 1 " + "X" * 200, "ĐIỀU 1 " + "Y" * 200]

    def sect(number, body):
        return Section(number=number, title="T", level=3,
                       path=long_path + [f"{number} T"], full_heading=f"{number} T",
                       blocks=[Block(text=body, page=1)], page_start=1, page_end=1)

    # 330 + 60 = 390 < 512 nếu bỏ qua tiền tố, nhưng tiền tố ngốn ~150 token
    out = merge_short_sections([sect("1.1", "chữ " * 310), sect("1.2", "chữ " * 55)],
                               min_tokens=200, max_tokens=512)
    assert [s.number for s in out] == ["1.1", "1.2"], "gộp xong sẽ bị cắt lại làm đôi"


def test_merged_titles_do_not_bloat_the_path():
    """Gộp nhiều mục thì số giữ đủ, còn tên bị chặn để tiền tố chunk không phình."""
    from docindex.headings import MAX_TITLE_IN_PATH, merge_short_sections

    sections = [
        Section(number=f"1.{i}", title=f"Tên mục khá dài số {i} của tài liệu",
                level=2, path=["1. Gốc", f"1.{i} T"], full_heading=f"1.{i} T",
                blocks=[Block(text="ngắn", page=1)], page_start=1, page_end=1)
        for i in range(1, 7)
    ]
    out = merge_short_sections(sections, min_tokens=60, max_tokens=512)
    assert len(out) == 1
    assert out[0].number == "1.1 + 1.2 + 1.3 + 1.4 + 1.5 + 1.6", "số mục phải giữ đủ"
    assert len(out[0].title) <= MAX_TITLE_IN_PATH + 4
    assert out[0].title.endswith("…")


def test_flattened_row_with_a_missing_column_label():
    """Bước làm phẳng rơi mất nhãn một cột thì cả ô đó chính là giá trị."""
    assert shorten_title("Khoản: 3.1 | Điều kiện vay vốn | Quy định:") == "Điều kiện vay vốn"
    assert (shorten_title("Khoản: 3.13 | Điều kiện tái cấp Hạn mức | Quy định: a. Trước")
            == "Điều kiện tái cấp Hạn mức")


def test_continuation_row_is_not_named_after_its_column():
    """Hàng nối tiếp chỉ còn trơ một cột: "Quy định" là tên cột, không phải tên mục."""
    rows = [
        "3.1.1 Khoản: 3.1 | Điều kiện: Mục đích cho vay | Quy định: Cho vay bổ sung",
        "3.1.2 Quy định:",
        "3.1.3 Quy định: năm/lần ĐVKD thực hiện đánh giá lại hạn mức để xem xét lại mức",
        "3.1.4 Quy định: Việt Nam đồng",
    ]
    blocks = [Block(text=t, page=1, bold=True) for t in rows]
    sections = [s for s in build_sections(detect_headings(blocks, "pdf"))
                if not s.is_preamble]
    assert [s.title for s in sections] == [
        "Mục đích cho vay",     # hàng đủ cột
        "",                     # hàng nối tiếp rỗng
        "",                     # hàng nối tiếp mang nội dung tràn, không có tên
        "Việt Nam đồng",        # hàng nối tiếp nhưng giá trị đủ ngắn để làm tên
    ]
    # không có bảng làm phẳng thì "Quy định:" vẫn là một tên mục bình thường
    plain = [Block(text="3.1.2 Quy định:", page=1, bold=True)]
    plain_sections = build_sections(detect_headings(plain, "pdf"))
    assert plain_sections[0].title == "Quy định"


def test_appendix_code_in_a_reference_is_not_a_heading():
    """"…theo Phụ lục số PL01.1016.PDS.2026(1);" là câu dẫn chiếu, không phải mục."""
    assert _match_heading("PL01.1016.PDS.2026(1);") is None
    hit = _match_heading("PL02.1003.PCS.2026(1): Giải thích từ ngữ")
    assert hit[0] == "PL02.1003.PCS.2026(1)" and hit[1] == "Giải thích từ ngữ"


def test_numbered_appendix_is_not_named_after_itself():
    """"Phụ lục 01" không có tên riêng — đường dẫn không được thành "Phụ lục 01 Phụ lục 01"."""
    blocks = [Block(text="Phụ lục 01", page=1, bold=True)]
    section = build_sections(detect_headings(blocks, "docx"))[0]
    assert section.title == ""
    assert section.heading_str == "Phụ lục 01"
    assert section.path == ["Phụ lục 01"]


def test_flattened_row_heading_line_carries_only_the_name():
    """Dựng lại thì dòng tiêu đề chỉ mang tên mục, cả hàng xuống nội dung."""
    from docindex.layout import _heading_and_rest

    row = "STT: 3.1 | Nội dung: Đối tượng khách hàng | Cụ thể:"
    section = Section(number="3.1.1", title=shorten_title(row), level=3,
                      path=["3.1.1"], full_heading=f"3.1.1 {row}")
    head, rest = _heading_and_rest(section)
    assert head == "3.1.1 Đối tượng khách hàng"
    # cả hàng gốc vẫn còn đủ chữ ở phần nội dung — cột nào của bảng cũng giữ nhãn
    assert rest.startswith("STT: 3.1 | Nội dung: Đối tượng khách hàng")


def test_full_heading_is_not_truncated_in_content(processed):
    """Tiêu đề rút gọn chỉ dùng cho đường dẫn; nội dung phải giữ đủ chữ."""
    for _path, _chunks, sections, _src, _st in processed:
        for section in sections:
            # Mục đã gộp chỉ mục thì heading_str gom tên của nhiều tiêu đề, còn
            # full_heading vẫn là dòng gốc của riêng nó — chữ của mục bị nuốt
            # nằm ở thân bài, test_no_content_loss soát chỗ đó.
            if section.full_heading and not section.is_merged:
                assert len(section.full_heading) >= len(section.heading_str) - 1


def test_rows_to_markdown_drops_empty_columns():
    rows = [["", "Năm hợp đồng", "", "1", ""], ["", "Lãi suất", "", "3%", ""]]
    md = rows_to_markdown(rows)
    assert md.split("\n")[0] == "| Năm hợp đồng | 1 |"
    assert rows_to_markdown([["", ""], ["", ""]]) == ""


def test_long_table_row_keeps_all_cell_text():
    """Hàng dài chuyển sang dạng 'cột: giá trị', không được mất chữ."""
    long_cell = "giá trị rất dài " * 90
    md = "\n".join([
        "| Tiêu chí | Nội dung |",
        "| --- | --- |",
        f"| Điều kiện | {long_cell} |",
    ])
    parts = _split_table(md, 800)
    joined = " ".join(parts)
    assert "Điều kiện" in joined
    assert joined.count("giá trị rất dài") == 90
    for part in parts:
        rows = [r for r in part.split("\n") if r.strip().startswith("|")]
        widths = {r.count("|") for r in rows}
        assert len(widths) <= 1, "bảng bị vỡ số cột"


def test_toc_page_keeps_cover_content(processed):
    """Bìa và mục lục chung một trang: chỉ được bỏ phần mục lục."""
    for path, chunks, _sections, _src, _st in processed:
        if "Tràng An" not in os.path.basename(path):
            continue
        first_page = " ".join(c.raw_text for c in chunks if c.page == 1)
        assert "AN LỘC TÍCH LŨY THỊNH VƯỢNG" in first_page
        assert "4474/BTC-QLBH" in first_page, "mất số công văn phê chuẩn ở trang bìa"
        assert "....." not in first_page, "còn sót dòng mục lục"


def test_footnotes_are_dropped(processed):
    """Cước chú không phải nội dung, càng không phải một mục trong cây chỉ mục.

    "1 Theo địa giới hành chính cũ…" nằm dưới đường kẻ cuối trang là chú thích
    cho một chỗ trong thân bài. Giữ lại thì nó trông y hệt đề mục "1." và bị
    dựng thành một nhánh lớn ngang hàng với "1. Phạm vi điều chỉnh".
    """
    for path, chunks, sections, _src, _st in processed:
        if "Xuân Thành" not in os.path.basename(path):
            continue
        body = " ".join(c.raw_text for c in chunks)
        assert "Theo địa giới hành chính cũ" not in body
        assert "ĐVKD chủ động thẩm định và chịu trách nhiệm" not in body
        # Ba đề mục của tài liệu, không có nút nào sinh ra từ dòng cước chú.
        # Hai mục đầu ngắn nên được gộp làm một — chỉ mục vẫn ghi đủ cả hai.
        titles = [s.title for s in sections]
        assert titles == ["Phần mở đầu",
                          "Phạm vi điều chỉnh và đối tượng áp dụng + Giải thích từ ngữ",
                          "Nội dung sản phẩm"]
        assert [s.number for s in sections] == ["", "1 + 2", "3"]
        # dấu tham chiếu ("…từng thời kỳ⁶") không được dính vào chữ
        assert "thời kỳ6" not in body and "thời kỳ4" not in body
        assert "chuyển nợ về nhóm 1 thì" in body
        break
    else:
        pytest.fail("thiếu file mẫu Xuân Thành")


def test_footnote_zone_leaves_real_content_alone(processed):
    """Chỉ cắt chữ nhỏ ở đáy trang — nội dung cỡ chữ thường phải còn nguyên."""
    for path, chunks, _sections, _src, _st in processed:
        if "Xuân Thành" not in os.path.basename(path):
            continue
        body = " ".join(c.raw_text for c in chunks)
        # dòng cuối cùng của phần thân trên trang có cước chú
        assert "không quá 70 tuổi tại thời điểm kết thúc khoản vay" in body
        assert "Các giấy tờ khác có giá trị tương đương" in body
        break


def test_no_content_loss(processed):
    """Chốt chặn chống mất chữ: mọi từ trong tài liệu phải còn trong chunk.

    Ngoại trừ dòng mục lục, chân trang lặp lại và cước chú — ba loại nhiễu bị
    loại có chủ đích. Ngưỡng 95% để chừa chỗ cho phần nhiễu đó.
    """
    import re
    from collections import Counter

    import docx
    import fitz

    dot = re.compile(r"\.{4,}")

    def words(s):
        return re.findall(r"\w+", clean_text(s).lower())

    def pdf_lines(path):
        """Chữ trong PDF, đã bỏ khối cước chú cuối trang và dòng đầu trang lặp.

        Cước chú và đầu/chân trang bị loại có chủ đích nên đếm chúng vào phần
        "chữ phải giữ" sẽ báo động giả. Khoanh vùng lại ở đây theo đúng *định
        nghĩa* của chúng — mạch chữ nhỏ liền đáy trang, dòng nằm sát mép trên
        và lặp gần như mọi trang — chứ không gọi vào code đang được kiểm tra,
        để test vẫn bắt được mọi chỗ mất chữ khác.
        """
        doc = fitz.open(path)
        sizes = Counter()
        pages = []
        for page in doc:
            rows = []
            for block in page.get_text("dict")["blocks"]:
                if block["type"] != 0:
                    continue
                for line in block["lines"]:
                    spans = [s for s in line["spans"] if s["text"].strip()]
                    if not spans:
                        continue
                    text = "".join(s["text"] for s in spans)
                    size = round(max(s["size"] for s in spans), 1)
                    rows.append((line["bbox"][1] / page.rect.height, size, text))
                    sizes[size] += len(text)
            pages.append(sorted(rows))
        doc.close()

        body = sizes.most_common(1)[0][0] if sizes else 12.0

        def in_margin(y):
            return y <= 0.10 or y >= 0.90

        seen = Counter()
        for rows in pages:
            seen.update({text for y, _size, text in rows if in_margin(y)})
        repeated = {text for text, count in seen.items()
                    if count >= max(2, len(pages) // 2)}

        out = []
        for rows in pages:
            while rows and rows[-1][0] > 0.70 and rows[-1][1] <= body - 1.0:
                rows.pop()
            out += [text for y, _size, text in rows
                    if not (in_margin(y) and text in repeated)]
        return out

    for path, chunks, _sections, _src, _st in processed:
        lines = []
        if path.lower().endswith(".pdf"):
            lines = pdf_lines(path)
        else:
            document = docx.Document(path)
            lines = [p.text for p in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    seen = set()
                    for cell in row.cells:
                        if cell._tc in seen:
                            continue
                        seen.add(cell._tc)
                        lines.append(cell.text)

        src = Counter()
        for line in lines:
            if dot.search(line):
                continue
            # So khớp trên bản đã quy ký hiệu: "𝐋𝟏" của Equation Editor và "L1"
            # là cùng một chữ, khác nhau ở cách mã hoá chứ không phải mất chữ.
            src.update(words(normalize_symbols(line)))
        got = Counter()
        for chunk in chunks:
            got.update(words(chunk.raw_text))

        total = sum(src.values())
        missing = sum((src - got).values())
        kept = (total - missing) / total if total else 1
        assert kept >= 0.95, (
            f"{os.path.basename(path)}: chỉ giữ {kept:.1%} số từ, "
            f"thiếu {missing}/{total}"
        )


# --- xuất bản tài liệu đã làm sạch ---------------------------------------

@pytest.fixture(scope="module")
def cleaned(tmp_path_factory):
    """Làm sạch toàn bộ tài liệu thật một lần, dùng chung cho các test dưới."""
    from docindex.export import clean_document, out_dir_for

    out = tmp_path_factory.mktemp("cleaned")
    results = []
    for path in _docs():
        # giữ nguyên cây thư mục: hai thư mục con có file trùng tên
        dst_dir = out_dir_for(path, TEST_DIR, str(out))
        os.makedirs(dst_dir, exist_ok=True)
        dst, stats = clean_document(path, dst_dir)
        results.append((path, dst, stats))
    return results


def test_output_keeps_the_source_file_name(cleaned):
    """Tên file kết quả giữ nguyên tên gốc, không thêm hậu tố nào."""
    for src, dst, _stats in cleaned:
        assert os.path.basename(dst) == os.path.basename(src)


def test_output_refuses_to_overwrite_the_source():
    """Tên file không còn hậu tố nên phải chặn việc ghi đè lên tài liệu gốc."""
    from docindex.export import clean_document, out_path

    src = _docs()[0]
    with pytest.raises(ValueError, match="trùng file gốc"):
        clean_document(src, os.path.dirname(src))
    with pytest.raises(ValueError, match="trùng file gốc"):
        out_path(src, os.path.dirname(src), out_format="same")


def test_cleaned_output_keeps_source_format(cleaned):
    for src, dst, _stats in cleaned:
        assert os.path.splitext(dst)[1].lower() == os.path.splitext(src)[1].lower()
        assert os.path.isfile(dst) and os.path.getsize(dst) > 0


def test_cleaned_pdf_keeps_content_figures(cleaned):
    """Hình minh hoạ phải nằm lại trong file, không bị gỡ cùng logo."""
    import fitz

    checked = 0
    for src, dst, stats in cleaned:
        if not src.lower().endswith(".pdf") or stats["figures_kept"] == 0:
            continue
        checked += 1
        doc = fitz.open(dst)
        drawn = 0
        for page in doc:
            for info in page.get_image_info():
                w = info["bbox"][2] - info["bbox"][0]
                h = info["bbox"][3] - info["bbox"][1]
                if min(w, h) >= 70:
                    drawn += 1
        doc.close()
        assert drawn >= stats["figures_kept"], (
            f"{os.path.basename(dst)}: giữ {stats['figures_kept']} hình nhưng "
            f"chỉ còn {drawn} ảnh đủ lớn trong file"
        )
    assert checked, "không có PDF nào chứa hình để kiểm tra"


def test_cleaned_pdf_drops_repeated_boilerplate(cleaned):
    """Chân trang lặp và số trang phải biến mất khỏi bản sạch."""
    import re

    import fitz

    page_num = re.compile(r"^\s*\d+\s*/\s*\d+\s*$")
    for src, dst, _stats in cleaned:
        if not src.lower().endswith(".pdf"):
            continue
        doc = fitz.open(dst)
        for page in doc:
            for line in page.get_text("text").split("\n"):
                assert not page_num.match(line.strip()), (
                    f"{os.path.basename(dst)}: còn sót số trang '{line.strip()}'"
                )
        doc.close()


def test_cleaned_pdf_is_not_bigger_than_source(cleaned):
    """Ghi lại PDF không được làm phình file (từng phồng gần gấp đôi)."""
    for src, dst, _stats in cleaned:
        if not src.lower().endswith(".pdf"):
            continue
        assert os.path.getsize(dst) <= os.path.getsize(src) * 1.05, (
            f"{os.path.basename(dst)} phình to hơn bản gốc"
        )


def test_cleaned_docx_keeps_body_and_clears_headers(cleaned):
    import docx

    for src, dst, _stats in cleaned:
        if not src.lower().endswith(".docx"):
            continue
        before = docx.Document(src)
        after = docx.Document(dst)
        assert sum(len(p.text) for p in after.paragraphs) == \
            sum(len(p.text) for p in before.paragraphs)
        assert len(after.tables) == len(before.tables)
        for section in after.sections:
            for part in (section.header, section.footer):
                assert not "".join(p.text for p in part.paragraphs).strip()


# --- dựng lại bố cục: mỗi mục một trang -----------------------------------

@pytest.fixture(scope="module")
def laid_out(processed):
    from docindex.layout import build_pages

    return [(path, build_pages(sections), sections)
            for path, _chunks, sections, _src, _st in processed]


@pytest.fixture(scope="module")
def laid_out_per_section(processed):
    """Bố cục lối cũ: mỗi mục một trang riêng, mỗi trang gọn trong trần token."""
    from docindex.layout import build_pages

    return [(path, build_pages(sections, page_per_section=True), sections)
            for path, _chunks, sections, _src, _st in processed]


def test_page_belongs_to_exactly_one_section(laid_out_per_section):
    """Ràng buộc chính của bố cục: không trang nào chứa nội dung của hai mục.

    Trang chỉ được mang tiêu đề của chính mục đó, hoặc của mục cha đứng trên
    nó — mục cha chỉ có mỗi dòng tiêu đề nên không có nội dung để lẫn vào.
    """
    from docindex.layout import _heading_and_rest

    for path, pages, sections in laid_out_per_section:
        assert pages, os.path.basename(path)
        by_path = {s.path_str: s for s in sections}

        for page in pages:
            assert all(i.text.strip() for i in page.items)
            allowed = set()
            for depth in range(1, len(page.section.path) + 1):
                owner = by_path.get(" > ".join(page.section.path[:depth]))
                if owner is not None:
                    allowed.add(_heading_and_rest(owner)[0])

            for item in page.items:
                if item.kind != "heading":
                    continue
                name = item.text.removesuffix(" (tiếp)")
                assert name in allowed, (
                    f"{os.path.basename(path)}: tiêu đề '{name}' lạc vào trang "
                    f"của mục {page.section.path_str}")


def test_section_content_stays_together(laid_out):
    """Các phần của cùng một mục phải nằm liền nhau, không bị mục khác chen vào."""
    for path, pages, _sections in laid_out:
        seen: list[str] = []
        for page in pages:
            key = page.section.path_str
            if not seen or seen[-1] != key:
                assert key not in seen, (
                    f"{os.path.basename(path)}: mục {key} bị tách rời nhau")
                seen.append(key)


def test_heading_size_decreases_by_level():
    """1. to nhất, 1.1 nhỏ hơn, 1.1.1 nhỏ nhất — và nội dung nhỏ hơn mọi tiêu đề."""
    from docindex.layout import BODY_PT, HEADING_PT, heading_pt

    sizes = [heading_pt(level) for level in range(1, len(HEADING_PT) + 2)]
    assert sizes[0] > sizes[1] > sizes[2]
    assert all(a >= b for a, b in zip(sizes, sizes[1:]))
    assert min(sizes) > BODY_PT


def test_heading_is_on_its_own_line(laid_out):
    for _path, pages, _sections in laid_out:
        for page in pages:
            for item in page.items:
                if item.kind == "heading":
                    assert "\n" not in item.text.strip()


def test_long_section_is_split_evenly(laid_out_per_section):
    """Mục dài bị cắt ra thì các phần phải đầy xấp xỉ nhau, không phần nào tràn.

    Trang bị chặn bởi hai trần: sức chứa của giấy (dòng) và trần token của hệ
    RAG. Chỗ cắt do trần nào chạm trước quyết định, nên chỉ đòi cân bằng ở
    thước đo đang chặn — mật độ chữ trên dòng thay đổi nên thước còn lại lệch
    là chuyện bình thường. Bảng và hình không cắt nhỏ hơn được, nên phần nhẹ
    nhất chỉ có thể thấp hơn mức chia đều đúng bằng khối lớn nhất; lệch hơn
    ngần ấy nghĩa là thuật toán đang đổ đầy lần lượt thay vì chia đều.
    """
    from docindex.layout import LINES_PER_PAGE

    def balanced(sizes: list[int], biggest: int) -> bool:
        return min(sizes) >= sum(sizes) / len(sizes) - biggest

    checked = 0
    for path, pages, _sections in laid_out_per_section:
        groups: dict[str, list] = {}
        for page in pages:
            if page.part_total > 1:
                groups.setdefault(page.section.path_str, []).append(page)
        for key, parts in groups.items():
            if len(parts) < 2:
                continue
            checked += 1
            lines = [p.lines for p in parts]
            tokens = [p.tokens for p in parts]
            assert max(lines) <= LINES_PER_PAGE, (
                f"{os.path.basename(path)} {key}: trang tràn {lines} dòng")
            assert (balanced(lines, max(i.lines for p in parts for i in p.items))
                    or balanced(tokens, max(i.tokens for p in parts for i in p.items))), (
                f"{os.path.basename(path)} {key}: các phần lệch nhau "
                f"{lines} dòng / {tokens} token")
    assert checked, "không có mục nào dài hơn một trang để kiểm tra"


def test_page_fits_the_rag_token_limit(laid_out_per_section):
    """Mỗi trang phải gọn trong 512 token.

    Hệ RAG đọc tài liệu theo cây DLA rồi cắt chunk ở trần 512 token. Trang nào
    vượt trần sẽ bị nó cắt ở chỗ ngẫu nhiên giữa câu, nên phải cắt sẵn tại
    ranh giới câu ngay từ khâu dựng lại.
    """
    from docindex.chunker import MAX_TOKENS

    for path, pages, _sections in laid_out_per_section:
        for page in pages:
            biggest = max(i.tokens for i in page.items)
            if biggest > MAX_TOKENS:
                continue        # một khối đơn lẻ không cắt nhỏ hơn được nữa
            assert page.tokens <= MAX_TOKENS, (
                f"{os.path.basename(path)} {page.section.path_str}: "
                f"trang {page.part_index} nặng {page.tokens} token")


def test_rebuilt_pdf_shows_headings_as_titles(processed, tmp_path_factory):
    """Tiêu đề trong bản PDF phải in đậm và to hơn nội dung.

    Mô hình DLA nhìn trang giấy chứ không đọc cấu trúc file: cỡ chữ và độ đậm
    là thứ quyết định nó gán nhãn `title` hay `list-item`.
    """
    import fitz

    from docindex.export import rebuild_document
    from docindex.layout import BODY_PT, build_pages

    out = tmp_path_factory.mktemp("dla")
    path, _chunks, sections, _src, _st = processed[0]
    heads = [i for p in build_pages(sections) for i in p.items if i.kind == "heading"]
    dst, _stats = rebuild_document(sections, path, str(out), out_format="pdf")

    doc = fitz.open(dst)
    try:
        spans = [s for page in doc
                 for block in page.get_text("dict")["blocks"]
                 if block.get("type") == 0
                 for line in block["lines"] for s in line["spans"]]
    finally:
        doc.close()

    checked = 0
    for head in heads:
        probe = head.text.strip()[:12]
        # Tên mục ngắn ("Phụ lục 01") còn xuất hiện trong câu dẫn chiếu giữa
        # thân bài, nên phải xét *mọi* span khớp rồi lấy span to nhất — lấy
        # span đầu tiên gặp được thì bắt nhầm dòng nội dung.
        hits = [s for s in spans if probe and probe in s["text"]]
        if not hits:
            continue                # tiêu đề bị ngắt dòng giữa chừng
        hit = max(hits, key=lambda s: s["size"])
        checked += 1
        assert hit["size"] > BODY_PT * 1.1, (
            f"tiêu đề '{probe}' chỉ {hit['size']}pt, không nổi hơn nội dung")
        bold = bool(hit["flags"] & 2 ** 4) or "bold" in hit["font"].lower()
        assert bold, f"tiêu đề '{probe}' không in đậm ({hit['font']})"
    assert checked >= len(heads) // 2, "không kiểm tra được tiêu đề nào trong bản PDF"


def test_rebuilt_document_invents_no_heading(laid_out):
    """Mỗi dòng tiêu đề vẽ ra phải có thật trong tài liệu.

    Hệ RAG dựng cây chỉ mục từ chính các dòng tiêu đề nó nhìn thấy, nên một
    dòng do tool bịa thêm ("Phần mở đầu") là một nhánh giả trong cây và một
    title sai cho mọi chunk nằm dưới nó.

    Ngoại lệ duy nhất là hậu tố "(tiếp)": mục dài hơn trần token buộc phải cắt
    làm nhiều phần, và dòng mở lại mục ấy chính là chỗ hệ RAG bám vào để cắt.
    Nó không bịa ra một mục mới — tên mục vẫn nguyên vẹn phía trước.
    """
    from docindex.layout import _heading_and_rest
    from docindex.models import document_title

    for path, pages, sections in laid_out:
        real = {_heading_and_rest(s)[0] for s in sections if not s.is_preamble}
        real.add(document_title(path))
        for page in pages:
            for item in page.items:
                if item.kind != "heading":
                    continue
                name = item.text[:-len(" (tiếp)")] if item.text.endswith(
                    " (tiếp)") else item.text
                assert name in real, (
                    f"{os.path.basename(path)}: tiêu đề '{item.text}' không "
                    "có trong tài liệu gốc")


def test_every_level_keeps_one_font_size(laid_out):
    """Cùng một cấp phải luôn cùng một cỡ chữ, cấp sâu hơn không to hơn cấp cạn.

    DLA suy ra cấp của tiêu đề từ cỡ chữ. Cùng một cấp mà lúc to lúc nhỏ thì
    cây chỉ mục nó dựng ra sẽ so le.
    """
    sizes: dict[int, set[float]] = {}
    for _path, pages, _sections in laid_out:
        for page in pages:
            for item in page.items:
                if item.kind == "heading":
                    sizes.setdefault(item.level, set()).add(item.size)

    assert sizes
    for level, values in sizes.items():
        assert len(values) == 1, f"cấp {level} có nhiều cỡ chữ: {values}"
    ordered = [next(iter(sizes[level])) for level in sorted(sizes)]
    assert ordered == sorted(ordered, reverse=True), ordered


def test_headings_ask_word_to_keep_them_with_their_content(processed, tmp_path_factory):
    """Tiêu đề trong .docx phải mang cờ keep_with_next.

    Việc ngắt trang do Word quyết định, tool không chen vào. Thứ giữ cho tiêu
    đề không đứng trơ trọi cuối trang là cờ này — thiếu nó thì tiêu đề và nội
    dung của nó bị tách ra hai trang.
    """
    import docx
    from docx.shared import Pt

    from docindex.export import rebuild_document
    from docindex.layout import BODY_PT

    out = tmp_path_factory.mktemp("keep")
    path, _chunks, sections, _src, _st = processed[0]
    dst, _stats = rebuild_document(sections, path, str(out), out_format="docx")

    heads = [p for p in docx.Document(dst).paragraphs
             if p.runs and p.runs[0].bold and p.runs[0].font.size > Pt(BODY_PT)]
    assert heads, "không tìm thấy tiêu đề nào trong bản .docx"
    for para in heads:
        assert para.paragraph_format.keep_with_next, (
            f"tiêu đề '{para.text[:40]}' thiếu keep_with_next")


def test_outline_stays_within_the_rag_depth_limit(processed):
    """Hệ RAG đọc cây sâu tối đa 6 cấp."""
    from docindex.layout import MAX_TREE_DEPTH

    for path, _chunks, sections, _src, _st in processed:
        depth = max(s.level for s in sections)
        assert depth <= MAX_TREE_DEPTH, (
            f"{os.path.basename(path)}: cây sâu {depth} cấp")


def test_rebuilt_pdf_leads_with_the_document_title(processed, tmp_path_factory):
    """Trang đầu phải mở bằng tên tài liệu, in đậm và to nhất trang.

    Hệ RAG lấy tiêu đề tài liệu làm gốc title của mọi chunk; thiếu nó thì mọi
    chunk mất một cấp trên cùng.
    """
    import fitz

    from docindex.export import rebuild_document
    from docindex.models import document_title

    out = tmp_path_factory.mktemp("title")
    path, _chunks, sections, _src, _st = processed[0]
    dst, stats = rebuild_document(sections, path, str(out), out_format="pdf")
    assert stats["doc_title"] == document_title(path)

    doc = fitz.open(dst)
    spans = [s for block in doc[0].get_text("dict")["blocks"]
             if block.get("type") == 0
             for line in block["lines"] for s in line["spans"]]
    doc.close()

    biggest = max(spans, key=lambda s: s["size"])
    assert biggest["text"].strip()[:20] in stats["doc_title"]
    assert bool(biggest["flags"] & 2 ** 4) or "bold" in biggest["font"].lower()


def test_rebuilt_pdf_uses_plain_spaces(processed, tmp_path_factory):
    """Đọc lại bản PDF phải ra dấu cách thường, không phải U+00A0.

    Font TrueType nhúng vào PDF làm glyph dấu cách trỏ về U+00A0 trong bảng
    ToUnicode. Trên giấy không thấy khác biệt gì, nhưng mọi công cụ trích xuất
    sẽ trả về chuỗi không có lấy một dấu cách thường — tách từ, BM25 và
    tokenizer phía RAG hỏng theo mà không báo lỗi.
    """
    import fitz

    from docindex.export import rebuild_document

    out = tmp_path_factory.mktemp("spaces")
    path, _chunks, sections, _src, _st = processed[0]
    dst, _stats = rebuild_document(sections, path, str(out), out_format="pdf")

    doc = fitz.open(dst)
    text = "".join(page.get_text("text") for page in doc)
    doc.close()
    assert " " not in text, "dấu cách trong PDF vẫn là U+00A0"
    assert text.count(" ") > 100, "không trích được dấu cách thường nào"


def test_rebuilt_document_keeps_all_words(laid_out):
    """Dựng lại bố cục không được làm rơi chữ nào của nội dung đã trích xuất."""
    import re
    from collections import Counter

    def words(s):
        return re.findall(r"\w+", clean_text(s).lower())

    for path, pages, sections in laid_out:
        src = Counter()
        for section in sections:
            if not section.is_preamble:
                # "Phần mở đầu" là tên do tool đặt, không có trong tài liệu
                src.update(words(section.heading_text))
            for block in section.blocks:
                if block.kind == "figure":
                    continue        # hình được thay bằng ảnh thật, không phải chữ
                src.update(words(block.text))
        got = Counter()
        for page in pages:
            for item in page.items:
                got.update(words(item.text))

        missing = sum((src - got).values())
        total = sum(src.values())
        assert missing == 0, (
            f"{os.path.basename(path)}: mất {missing}/{total} từ khi dựng lại")


def test_rebuild_writes_both_formats(processed, tmp_path_factory):
    """Chọn được .docx hay .pdf; số trang báo cáo đúng bằng số trang file thật."""
    import docx
    import fitz
    from docx.oxml.ns import qn

    from docindex.export import rebuild_document
    from docindex.layout import build_pages

    out = tmp_path_factory.mktemp("rebuilt")
    path, _chunks, sections, _src, _st = processed[0]

    # Chế độ chảy liên tục: bộ ghi tự ngắt trang nên tài liệu không mang sẵn
    # dấu ngắt trang nào; ép ngắt theo ước lượng của tool là nguồn gốc của
    # những trang gần như trống.
    dst_docx, stats = rebuild_document(sections, path, str(out), out_format="docx")
    assert dst_docx.endswith(".docx") and stats["pages_out"] >= 1
    document = docx.Document(dst_docx)
    breaks = sum(1 for p in document.paragraphs for br in p._p.iter(qn("w:br"))
                 if br.get(qn("w:type")) == "page")
    assert breaks == 0, "bản chảy liên tục không được chèn dấu ngắt trang"

    dst_pdf, stats = rebuild_document(sections, path, str(out), out_format="pdf")
    doc = fitz.open(dst_pdf)
    assert dst_pdf.endswith(".pdf") and stats["pages_out"] == doc.page_count

    # Lối cũ mỗi mục một trang thì số dấu ngắt phải khớp số trang đã bố trí
    per_section = len(build_pages(sections, page_per_section=True))
    dst2, stats2 = rebuild_document(sections, path, str(out), out_format="docx",
                                    suffix="_persection", page_per_section=True)
    breaks2 = sum(1 for p in docx.Document(dst2).paragraphs
                  for br in p._p.iter(qn("w:br")) if br.get(qn("w:type")) == "page")
    assert breaks2 == per_section - 1 and stats2["pages_out"] == per_section
    # font phải nhúng đủ dấu tiếng Việt, không rơi thành ô vuông
    text = "".join(page.get_text("text") for page in doc)
    doc.close()
    assert any(ch in text for ch in "ăâđêôơư"), "bản PDF mất dấu tiếng Việt"


def _docx_with_picture(tmp_path):
    """Tạo một .docx có ảnh nhúng — bộ tài liệu thật không có file nào như vậy."""
    import docx
    import fitz
    from docx.shared import Pt as DocxPt

    png = tmp_path / "hinh.png"
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 260, 180))
    pix.clear_with(210)
    pix.save(str(png))

    document = docx.Document()
    document.add_paragraph("1. Mục có hình minh hoạ")
    document.add_picture(str(png), width=DocxPt(260))
    src = tmp_path / "co-hinh.docx"
    document.save(str(src))
    return str(src)


def test_docx_image_is_extracted_and_rebuilt(tmp_path):
    """Hình trong .docx phải ra được file để nhúng lại vào bản dựng lại."""
    import docx
    import fitz

    from docindex.export import rebuild_document
    from docindex.headings import build_sections, detect_headings
    from docindex.extract_docx import extract

    src = _docx_with_picture(tmp_path)
    figure_dir = str(tmp_path / "figures")
    blocks, _source = extract(src, figure_dir=figure_dir, doc_id="t")
    figures = [b for b in blocks if b.kind == "figure"]
    assert figures, "không nhận ra hình minh hoạ trong .docx"
    assert os.path.isfile(figures[0].meta["file"]), "chưa tách được file ảnh"

    sections = build_sections(detect_headings(blocks, "docx"))
    out = str(tmp_path / "out")
    dst, _stats = rebuild_document(sections, src, out, out_format="docx",
                                   figure_dir=figure_dir)
    parts = docx.Document(dst).part.package.image_parts
    assert len(list(parts)) == 1, "ảnh không được nhúng vào bản .docx dựng lại"

    dst, _stats = rebuild_document(sections, src, out, out_format="pdf",
                                   figure_dir=figure_dir)
    doc = fitz.open(dst)
    embedded = sum(len(page.get_images()) for page in doc)
    doc.close()
    assert embedded == 1, "ảnh không được nhúng vào bản .pdf dựng lại"


def test_rebuilt_pdf_is_not_bloated_by_fonts(processed, tmp_path_factory):
    """Nhúng nguyên font hệ thống từng làm file phồng lên vài MB."""
    from docindex.export import rebuild_document

    out = tmp_path_factory.mktemp("rebuilt-size")
    path, _chunks, sections, _src, _st = processed[0]
    dst, _stats = rebuild_document(sections, path, str(out), out_format="pdf")
    assert os.path.getsize(dst) < 1_500_000, "font chưa được rút gọn"


# --- giao diện đồ hoạ -----------------------------------------------------

def test_outline_shows_the_tree_with_indentation(processed):
    """Bản in cây chỉ mục phải thụt lề theo cấp và không chứa mục do tool đặt tên."""
    from docindex.models import PREAMBLE_TITLE, document_title
    from docindex.report import format_outline, outline

    path, _chunks, sections, _src, _st = processed[0]
    tree = format_outline(sections, document_title(path))
    lines = tree.split("\n")

    assert lines[0].startswith("[tài liệu] ")
    assert PREAMBLE_TITLE not in tree
    nodes = outline(sections)
    assert len(lines) == len(nodes) + 1
    for line, node in zip(lines[1:], nodes):
        assert line.startswith("   " * node["level"] + f"L{node['level']} ")


def test_gui_options_cover_everything_the_worker_reads():
    """Thiếu một khoá trong opts là giao diện chết giữa chừng lúc đang chạy."""
    import inspect

    from docindex import gui

    source = inspect.getsource(gui.App._work)
    used = set(re.findall(r"opts\[\"(\w+)\"\]", source))
    built = set(re.findall(r"\"(\w+)\":", inspect.getsource(gui.App.start)))
    assert used <= built, f"opts thiếu khoá: {used - built}"


def test_parse_drop_handles_paths_with_spaces():
    """Đường dẫn có dấu cách được hệ điều hành bọc trong ngoặc nhọn."""
    from docindex.gui import parse_drop

    assert parse_drop("{C:/Máy tính/tài liệu a.pdf} C:/b.docx") == [
        "C:/Máy tính/tài liệu a.pdf", "C:/b.docx",
    ]
    assert parse_drop("C:/x.pdf") == ["C:/x.pdf"]
    assert parse_drop("") == []
    # Windows gửi dấu \ và bọc từng file khi thả nhiều file cùng lúc
    assert parse_drop(r"{C:\Tài liệu\quy dinh.docx} {C:\a\b c.pdf}") == [
        r"C:\Tài liệu\quy dinh.docx", r"C:\a\b c.pdf",
    ]


def test_gui_says_so_when_drag_and_drop_is_unavailable():
    """Thiếu tkinterdnd2 thì phải nói thẳng, không mời thả suông.

    Vùng thả vẫn ghi "Thả tài liệu vào đây" trong khi thư viện chưa có thì
    người dùng thả xong không thấy gì xảy ra và không biết vì sao.
    """
    import tkinter as tk

    from docindex import gui

    try:
        root = tk.Tk()          # Tk thường: chắc chắn không kéo thả được
    except tk.TclError:
        pytest.skip("môi trường không mở được cửa sổ Tk")
    try:
        app = gui.App(root)
        root.update()
        assert not app._enable_dnd()
        assert "chưa dùng được" in app.drop_label["text"]
        assert "Chọn file" in app.drop_label["text"]
        assert "tkinterdnd2" in app.log.get("1.0", "end")
    finally:
        root.destroy()


def test_expand_inputs_filters_and_walks_folders():
    from docindex.gui import expand_inputs

    from_folder = expand_inputs([TEST_DIR])
    assert from_folder and all(
        os.path.splitext(p)[1].lower() in {".pdf", ".docx"} for p in from_folder
    )
    assert expand_inputs(["khong-ton-tai.txt"]) == []


def test_page_numbers_are_not_in_text(processed):
    """Dòng kiểu '9/34' ở chân trang là nhiễu, phải bị loại sạch."""
    import re
    pat = re.compile(r"^\s*\d+\s*/\s*\d+\s*$")
    for _path, chunks, _sections, _src, _st in processed:
        for chunk in chunks:
            for line in chunk.raw_text.split("\n"):
                assert not pat.match(line), f"{chunk.chunk_id}: còn sót '{line}'"


# --- chuẩn hoá ký hiệu đặc biệt -------------------------------------------

def test_math_variables_become_plain_letters():
    """Biến của Equation Editor phải hạ về chữ Latin thường.

    Công thức tính lãi được soạn bằng Equation Editor nên "Mi" thật ra nằm ở
    khối Mathematical Alphanumeric Symbols. Hệ RAG không có font cho khối đó,
    đọc ra thành "$Mi$" hoặc bỏ hẳn — chuẩn phải là "Mi".
    """
    assert normalize_symbols("𝐌𝐢") == "Mi"
    assert normalize_symbols("𝐋 = 𝐌 ∗𝐓 ∗𝐑") == "L = M *T *R"
    assert normalize_symbols("𝟏𝟐") == "12"
    assert "$" not in clean_text("𝑳: Là số tiền lãi")


def test_symbols_are_spelled_out_or_flattened():
    """Ký hiệu toán và dấu câu kiểu chữ quy về chữ hoặc dấu ASCII."""
    assert "Tổng" in normalize_symbols("∑")
    assert normalize_symbols("a ≤ b ≥ c ≠ d") == "a <= b >= c != d"
    assert normalize_symbols("5 × 3 ÷ 2 ± 1") == "5 x 3 / 2 +/- 1"
    assert normalize_symbols("“abc” – ‘x’") == "\"abc\" - 'x'"
    # font Symbol của Word đẩy glyph vào vùng dùng riêng, giữ nguyên mã ASCII
    assert normalize_symbols("a \uf02b b") == "a + b"
    assert normalize_symbols("\uf0b7 gạch đầu dòng") == "- gạch đầu dòng"


def test_clean_text_keeps_ordinary_vietnamese_intact():
    """Chuẩn hoá không được đụng tới chữ tiếng Việt hay dấu câu thường."""
    src = "Điều 5.1: Lãi suất 6,5%/năm (áp dụng từ 01/2026) - xem mục 2.3."
    assert clean_text(src) == src


def test_rebuilt_pdf_reads_back_every_character(tmp_path):
    """Đọc lại bản PDF phải ra đúng ký tự đã ghi vào.

    Bảng ToUnicode do MuPDF dựng trỏ nhầm vài glyph sang ký tự song trùng —
    dấu gạch thành gạch nối mềm U+00AD, chấm phẩy thành dấu chấm hỏi Hy Lạp
    U+037E. Trên giấy không thấy khác biệt, nhưng phía RAG nhận về một chuỗi
    không còn dấu gạch hay chấm phẩy nào.
    """
    import fitz

    from docindex.layout import LayoutPage, PageItem
    from docindex.render import write_pdf

    probe = "a-b; c-d; Điều 5.1 - mục 2,3; x/y"
    dst = str(tmp_path / "probe.pdf")
    write_pdf([LayoutPage(None, [PageItem("para", probe)])], dst)

    doc = fitz.open(dst)
    text = "".join(page.get_text("text") for page in doc)
    doc.close()
    for mark in ("-", ";", " "):
        assert mark in text, f"đọc lại PDF không còn ký tự {mark!r}"
    for wrong in ("\u00ad", "\u037e", "\u00a0"):
        assert wrong not in text, f"PDF đọc ra ký tự song trùng U+{ord(wrong):04X}"


# --- tiêu đề lớn không đánh số --------------------------------------------

def test_two_big_titles_become_the_top_level(processed):
    """Tài liệu có nhiều tiêu đề lớn thì chúng phải là cấp cao nhất của cây.

    "TỔNG QUAN VĂN BẢN QUY ĐỊNH" rồi "QUY ĐỊNH SẢN PHẨM…" chia tài liệu làm hai
    phần lớn. Không nhận ra thì "1. TIÊU ĐỀ SẢN PHẨM" của phần đầu và "Điều 1"
    của phần sau nằm ngang hàng nhau, và cây chỉ mục mất hẳn cấp trên cùng.
    """
    for path, _chunks, sections, _src, _st in processed:
        if "rút gốc linh hoạt" not in os.path.basename(path):
            continue
        banners = [s for s in sections if s.is_banner]
        assert len(banners) >= 2, "không nhận ra tiêu đề lớn nào"
        assert all(s.level == 1 for s in banners), "tiêu đề lớn không ở cấp 1"
        assert all(not s.number for s in banners), "tiêu đề lớn không đánh số"
        assert "TỔNG QUAN VĂN BẢN QUY ĐỊNH" in [s.title for s in banners]
        # mọi mục có đánh số đều nằm dưới một tiêu đề lớn nào đó
        numbered = [s for s in sections if s.number]
        assert numbered and all(s.level >= 2 for s in numbered)
        break
    else:
        pytest.fail("thiếu file mẫu tiết kiệm rút gốc linh hoạt")


def test_a_formula_line_is_not_taken_for_a_big_title():
    """Dòng "L = M *T *R" cũng cỡ lớn và toàn chữ hoa, nhưng không phải tiêu đề."""
    from docindex.headings import _is_banner

    def block(text):
        return Block(text=text, page=1, size=16.0, bold=True,
                     meta={"body_size": 12.0})

    assert not _is_banner(block("L = M *T *R"), 12.0)
    assert not _is_banner(block("12"), 12.0)
    assert _is_banner(block("TỔNG QUAN VĂN BẢN QUY ĐỊNH"), 12.0)
    # cỡ chữ bằng nội dung thì không phải tiêu đề lớn
    assert not _is_banner(block("TỔNG QUAN VĂN BẢN"), 16.0)


# --- gộp mục ngắn ----------------------------------------------------------

def test_merging_looks_both_ways(processed):
    """Mục ngắn phải xét được cả mục trên lẫn mục dưới.

    Chỉ nhìn lên trên thì một mục ngắn nằm ngay sau một mục đồ sộ sẽ mắc kẹt
    mãi mãi — "Điều 1" một trăm token đứng riêng chỉ vì phía trên nó là "MỤC
    LỤC" tám trăm token, trong khi "Điều 2" ngay dưới còn thừa chỗ.
    """
    from docindex.headings import merge_short_sections

    for path, _chunks, sections, _src, _st in processed:
        if "rút gốc linh hoạt" not in os.path.basename(path):
            continue
        assert [s for s in sections if s.is_merged], "không gộp được mục ngắn nào"
        # chạy lại một lượt nữa không được gộp thêm gì: đã tới điểm dừng
        again = merge_short_sections(sections, CFG.min_tokens, CFG.max_tokens)
        assert len(again) == len(sections), "còn cặp mục ngắn chưa gộp hết"
        break
    else:
        pytest.fail("thiếu file mẫu tiết kiệm rút gốc linh hoạt")


def test_merged_section_does_not_repeat_its_own_name(laid_out):
    """Mục đã gộp chỉ mục không được in lại tên của chính nó ngay dưới tiêu đề.

    Danh mục định nghĩa có *nội dung chính là tên mục* ("12 Doanh nghiệp cho
    thuê lại lao động: Là doanh nghiệp…"). Gộp hai mục như vậy thì tên mục vừa
    nằm trên dòng tiêu đề vừa mở đầu phần nội dung — đọc ra thành lặp nguyên
    văn một lần nữa.
    """
    from docindex.layout import _heading_and_rest

    for _path, _pages, sections in laid_out:
        for section in sections:
            if not section.is_merged or not section.own_title:
                continue
            _title, rest = _heading_and_rest(section)
            first = rest.split("\n")[0].strip()
            assert not first.startswith(section.own_title), (
                f"'{section.own_title}' vừa là tiêu đề vừa là dòng nội dung đầu")


# --- tiêu đề đầy đủ --------------------------------------------------------

def test_heading_line_keeps_the_whole_name(laid_out):
    """Dòng tiêu đề in ra lấy tên mục ở độ dài đầy đủ, không phải bản rút gọn.

    Tên mục bị rút về 90 ký tự để tiền tố mục lục còn chỗ cho nội dung. Dùng
    luôn bản rút gọn ấy làm dòng tiêu đề trên giấy thì "…làm nguyên" nằm trên
    tiêu đề còn "liệu sản xuất" rơi xuống thành một đoạn nội dung cụt.
    """
    from docindex.headings import MAX_TITLE_IN_HEADING, MAX_TITLE_IN_PATH

    longer = 0
    for _path, _pages, sections in laid_out:
        for section in sections:
            if section.is_preamble or not section.own_title:
                continue
            # chỉ được cắt bớt khi tên mục thật sự dài quá trần của dòng tiêu đề
            assert (not section.own_title.endswith("…")
                    or len(section.own_title) >= MAX_TITLE_IN_HEADING - 20), (
                f"tên mục '{section.own_title}' bị cắt sớm")
            longer += len(section.own_title) > MAX_TITLE_IN_PATH
    assert longer, "không tiêu đề nào dài quá trần của đường dẫn — mẫu không đủ"


def test_long_heading_keeps_every_word(processed):
    """Tiêu đề dài của văn bản pháp lý không được coi là câu văn mà bỏ đi."""
    for path, _chunks, sections, _src, _st in processed:
        if "Ký quỹ" not in os.path.basename(path):
            continue
        target = [s for s in sections if s.number.startswith("15.10")]
        assert target, "mất hẳn đề mục 15.10"
        assert "nguyên liệu sản xuất" in target[0].full_heading, (
            f"tiêu đề 15.10 thiếu chữ: {target[0].full_heading!r}")
        break
    else:
        pytest.fail("thiếu file mẫu tiền gửi Ký quỹ")


# --- mục quá dài -----------------------------------------------------------

def test_long_section_is_reopened_with_a_continuation_line(laid_out):
    """Mục dài hơn trần token phải được cắt bằng dòng "(tiếp)".

    Hệ RAG cắt chunk theo cây chỉ mục chứ không theo trang, nên một mục bốn
    nghìn token cho ra đúng một chunk bốn nghìn token và bị khâu embedding cắt
    cụt. Dòng "(tiếp)" là nút để nó cắt.
    """
    from docindex.chunker import MAX_TOKENS

    seen = 0
    for path, pages, _sections in laid_out:
        for page in pages:
            run = 0
            for item in page.items:
                if item.kind == "heading":
                    seen += item.text.endswith("(tiếp)")
                    run = 0
                    continue
                run += item.tokens
                assert run <= MAX_TOKENS * 2, (
                    f"{os.path.basename(path)}: khối {run} token không có dòng "
                    "'(tiếp)' nào cắt")
    assert seen, "không tài liệu nào có mục dài phải cắt — mẫu không đủ"


# --- ngắt trang ------------------------------------------------------------

def test_no_heading_is_left_alone_at_the_bottom_of_a_page(processed, tmp_path_factory):
    """Tiêu đề không được đứng trơ cuối trang, cũng không được vắt qua hai trang.

    Mô hình DLA đọc cây chỉ mục theo từng trang. Tiêu đề nằm cuối trang này còn
    nội dung của nó ở trang sau thì nó bị đọc thành một mục rỗng, và nội dung
    trang sau thành một mục không có tên.
    """
    import fitz

    from docindex.export import rebuild_document
    from docindex.render import _stranded_pages

    out = tmp_path_factory.mktemp("breaks")
    checked = 0
    for path, _chunks, sections, _src, _st in processed[:6]:
        dst, _stats = rebuild_document(sections, path, str(out), out_format="pdf")
        doc = fitz.open(dst)
        try:
            stranded = _stranded_pages(doc)
        finally:
            doc.close()
        assert not stranded, (
            f"{os.path.basename(path)}: tiêu đề đứng trơ ở trang "
            f"{sorted(p + 1 for p in stranded)}")
        checked += 1
    assert checked, "không dựng được tài liệu nào để kiểm tra"


# --- sơ đồ vẽ bằng nét ------------------------------------------------------

def test_flowchart_is_kept_as_a_picture(tmp_path):
    """Lưu đồ quy trình phải giữ nguyên dạng hình, không bị moi thành chữ.

    Lưu đồ là nét vẽ cộng với chữ trong từng ô. Trích xuất theo lối thường sẽ
    xếp hết chữ trong các ô thành một dòng dài vô nghĩa và bản thân sơ đồ biến
    mất — vừa mất hình vừa thêm nhiễu.
    """
    import fitz

    from docindex.images import collect_vector_figures

    name = "Quy định phan phoi trai phieu LPBank ra công chúng.pdf"
    path = os.path.join(TEST_DIR, "Tiết kiệm kênh bank", name)
    if not os.path.isfile(path):
        pytest.skip("thiếu file mẫu có lưu đồ")

    doc = fitz.open(path)
    try:
        found = [page.number + 1 for page in doc if collect_vector_figures(page)]
    finally:
        doc.close()
    assert found, "không nhận ra lưu đồ nào"

    chunks, _sections, _src = process_file(
        path, CFG, figure_dir=str(tmp_path / "figures"))
    body = " ".join(c.raw_text for c in chunks)
    assert "[HÌNH:" in body, "lưu đồ không để lại dòng giữ chỗ nào"
    # chữ trong ô của lưu đồ không được moi ra thành nội dung
    assert "Tiếp nhận và khai báo" not in body


def test_a_bordered_table_is_not_taken_for_a_flowchart():
    """Bảng cũng vẽ bằng nét, nhưng toàn nét ngang dọc — không phải sơ đồ."""
    import fitz

    from docindex.images import collect_vector_figures

    for path in _docs():
        if not path.lower().endswith(".pdf"):
            continue
        doc = fitz.open(path)
        try:
            for page in doc:
                for figure in collect_vector_figures(page):
                    # mọi hình nhận ra được đều phải vì có nét chéo hoặc nét cong
                    assert "nét chéo/cong" in figure.reason
        finally:
            doc.close()
