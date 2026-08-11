# docindex

Tool làm sạch tài liệu PDF/DOCX và chia chunk cho hệ thống RAG.

Tool được chỉnh cho hệ RAG **đọc tài liệu theo cây phân cấp chỉ mục** (Layout
Analysis → Reconstruction → Chunking), **sâu tối đa 3 cấp**, **chunk trần 512
token**. Hệ đó tự cắt chunk theo cây nó đọc được từ trang giấy, nên việc của
tool là làm cây chỉ mục hiện lên **không thể nhầm**:

* tiêu đề và chỉ mục trình bày đúng kiểu văn bản chuẩn — in đậm, cỡ chữ lớn
  theo cấp, đứng riêng một dòng — để DLA gán nhãn `title` thay vì lẫn vào
  `text`/`list-item`;
* cây chỉ mục được soát lại từ chính ký hiệu đánh số, không tin cấp danh sách
  của Word, và loại các "mục ma" do câu văn bắt đầu bằng số sinh ra;
* tiêu đề tài liệu đứng đầu làm gốc của cây, vì title mỗi chunk bắt đầu từ đó;
* tiêu đề không bao giờ bị bỏ trơ ở cuối trang hay vắt qua hai trang;
* ký hiệu lạ được quy về chữ thường đọc được (`𝐌𝐢` → `Mi`, `∑` → `Tổng`),
  và lưu đồ vẽ bằng nét được giữ nguyên dạng hình thay vì moi thành chữ;
* mặc định xuất ra **`.pdf`**, định dạng DLA đọc chuẩn nhất.

Mỗi tài liệu cho ra hai thứ:

1. **Bản tài liệu đã làm sạch, dựng lại bố cục** — nội dung chảy liên tục như
   một văn bản bình thường, tên mục đứng riêng một dòng với cỡ chữ giảm dần
   theo cấp. Logo, hình bìa, đầu/chân trang và mục lục bị gỡ bỏ; hình minh hoạ
   nội dung vẫn nằm trong file. Đây là file nên nạp vào RAG. Chọn được mức làm
   sạch nhẹ hơn — giữ nguyên bố cục gốc, chỉ gỡ đúng những thứ mình muốn — ở
   bảng **Làm sạch tài liệu** trong giao diện hoặc các tham số `--keep-*`.
2. **File chunk `.jsonl`** để nạp thẳng vào khâu embedding nếu bạn không dùng
   DLA — mỗi chunk gọn trong **512 token**, trong **một trang** và thuộc về
   **một mục** của tài liệu.

```
output/
  Quy định ABC_formalized.pdf ← bản sạch để nạp vào RAG (tên gốc + `_formalized`)
  Quy định ABC.jsonl          ← chunk cho RAG
  Quy định ABC.outline.txt    ← cây chỉ mục để soát lại (giao diện kéo thả)
  figures/                    ← hình tách ra để nhúng vào bản dựng lại
  report.json                 ← thống kê chất lượng + cây chỉ mục từng tài liệu
```

## Cây chỉ mục — phần quan trọng nhất

Hệ RAG dựng title của chunk từ cây chỉ mục, nên **sai một cấp ở đây là sai
title của mọi chunk bên dưới**. Tool soát cây bằng sáu luật:

| Luật | Vì sao |
|---|---|
| Thứ bậc suy từ **ký hiệu**, không từ cấp danh sách của Word | Tài liệu thật hay khai `a) b) c)` cùng `ilvl` với `1. 2. 3.`; tin theo đó thì mục con thành mục ngang hàng với mục cha. `numFmt` (decimal / lowerLetter / upperRoman) mới nói đúng thứ bậc |
| `I. II. III.` là cấp lớn, `(i) (ii)` là cấp sâu nhất, `i)` trơ trọi vẫn thuộc danh sách chữ cái | `I. THÔNG TIN CHUNG` đứng trên `10. Điều kiện`; còn `i)` giữa `a) b) c)…` chỉ là mục thứ 9, tách ra thành cấp riêng là đẻ thêm một tầng giả |
| Số của đề mục phải **nối tiếp dãy đang mở** | `30 (ba mươi) ngày tuổi đến 70 tuổi…` là câu văn bị PDF cắt dòng, không phải mục 30. Nhận nhầm thì mọi mục thật phía sau tụt xuống làm con của nó |
| Số đệm 0 (`04`) là **số lượng**, không phải chỉ mục | `04 (bốn) Năm hợp đồng đầu tiên` |
| Tên mục là phần **trước dấu hai chấm**, phần sau là nội dung | Văn bản hành chính viết liền cả câu vào dòng tiêu đề: `1.9 Hợp đồng bảo hiểm: là tất cả văn bản thể hiện sự thỏa thuận…`. Để nguyên thì cả câu bị in đậm cỡ tiêu đề và title của chunk đọc như văn xuôi |
| **Tiêu đề lớn không đánh số** làm cấp trên cùng, khi tài liệu có từ hai cái trở lên | `TỔNG QUAN VĂN BẢN QUY ĐỊNH` rồi `QUY ĐỊNH SẢN PHẨM TIẾT KIỆM…` chia tài liệu thành hai phần lớn. Bỏ qua chúng thì `1. TIÊU ĐỀ SẢN PHẨM` của phần đầu nằm ngang hàng `Điều 1` của phần sau, và cây mất hẳn cấp trên cùng |

### Tiêu đề lớn không đánh số

Dấu hiệu phải hội đủ cả ba: **cỡ chữ lớn hơn hẳn nội dung**, **viết hoa gần như
toàn bộ**, **không mang chỉ mục nào**. Công thức toán (`L = M *T *R`) cũng in
cỡ lớn và toàn chữ hoa nên bị loại thêm bằng luật "tiêu đề là một cụm từ": phải
có ít nhất hai từ dài từ hai chữ cái trở lên.

Chỉ nhận khi tài liệu có **từ hai tiêu đề lớn trở lên**. Đúng một cái thì đó
chính là tên tài liệu — vốn đã là gốc của cây — thêm một nhánh nữa chỉ mọc thừa
một cấp, mà cây chỉ sâu được 3 cấp.

Tiêu đề lớn là vách ngăn, không phải một chủ đề, nên nó **không gộp nội dung
với mục nào**: đem nội dung của một mục có số hiệu bỏ vào một nút không có chỉ
mục để tra cứu là mất đường tra.

### Cây dừng ở cấp 3

Từ cấp 4 (`1.1.1.1`) trở xuống, đề mục **không còn là một nhánh** mà trở lại
làm nội dung thường của mục cha — số mục vẫn nằm nguyên trong dòng chữ nên
không mất thông tin đánh số. Chia nhỏ tới cấp 4 thì mỗi nút chỉ còn một hai
câu, vector của nó gần như trống nghĩa, mà title của chunk lại dài thêm một cấp
vô ích.

### Mục quá ngắn: gộp cả nội dung lẫn chỉ mục

Mục dưới `--min-tokens` (mặc định **200**) và không có mục con thì được **gộp
với mục liền kề** — mục cha, hoặc mục anh em ngay trên *hoặc ngay dưới* nó —
với điều kiện **kết quả vẫn ≤ 512 token, đã tính cả tiền tố mục lục** mà mọi
chunk phải mang theo; không đủ chỗ thì vẫn để tách. Chunk một hai trăm token
gần như không mang thông tin, gộp lại thì mất nút đó trong cây nhưng **không
mất chữ**: dòng tiêu đề được đưa xuống thành đoạn mở đầu của phần nội dung.

Cách chọn bạn gộp quyết định chất lượng:

* **Xét cả hai phía.** Chỉ nhìn lên trên thì một mục ngắn nằm ngay sau một mục
  đồ sộ mắc kẹt vĩnh viễn — `Điều 1` một trăm token đứng riêng chỉ vì phía trên
  là `MỤC LỤC` tám trăm token, trong khi `Điều 2` ngay dưới còn thừa chỗ.
* **Ưu tiên cân bằng.** Mỗi vòng lấy mục ngắn nhất còn lại rồi ghép nó vào
  người hàng xóm *nhỏ hơn*, nên các mục sau khi gộp dài xấp xỉ nhau thay vì một
  khối phình to bên cạnh mấy mẩu vụn.
* **Lặp tới khi hết.** Một mục cha vừa nuốt hết mục con của nó lại trở thành
  mục cụt nhánh và được xét lại; gộp một lượt duy nhất thì bỏ sót hẳn nhóm này.

Ngưỡng đặt cao gần nửa trần là có chủ ý — đo trên 18 tài liệu thật:

| `--min-tokens` | chunk | trung vị | chunk < 200 token |
|---|---|---|---|
| 60 | 1142 | 273 | 389 |
| **200** | **890** | **402** | **121** |
| 512 | 877 | 410 | 121 |

Quá 200 thì gần như không đổi gì nữa, vì lúc đó trần 512 mới là thứ chặn lại.

Mục nhỏ mà vẫn đứng riêng thì chỉ vì một trong bốn lý do: nó **còn mục con**
(gộp là xoá mất một nhánh), **hai mục liền kề ở hai nhánh khác nhau**, **gộp
vào sẽ vượt 512**, hoặc hàng xóm là phần mở đầu / một tiêu đề lớn. Mục cha chỉ
có mỗi dòng tiêu đề không thành chunk rời — nó đi kèm mục con đầu tiên.

Gộp hai mục ngang hàng thì **chỉ mục cộng vào nhau**:

```
1.1 Mục đích cho vay  +  1.2 Loại tiền cho vay   ->   1.1 + 1.2 Mục đích cho vay + Loại tiền cho vay
```

Giữ nguyên chỉ mục cũ thì cây nói dối — chunk mang title `1.1` nhưng bên trong
có cả phần `1.2`, tra theo số mục sẽ không ra. Số mục luôn giữ đủ; riêng phần
tên bị chặn ở 90 ký tự (`A + B + …`) để tiền tố của chunk không phình lên ăn
mất ngân sách token của nội dung. Tắt cả cơ chế bằng `--keep-short-sections`.

Với danh mục định nghĩa, **nội dung của mục chính là tên mục** (`12 Doanh
nghiệp cho thuê lại lao động: Là doanh nghiệp…`). Gộp hai mục như vậy mà không
để ý thì tên mục vừa nằm trên dòng tiêu đề vừa mở đầu phần nội dung, đọc ra
thành lặp nguyên văn một lần nữa — nên tên riêng của mục được gỡ khỏi dòng nội
dung đầu tiên, đúng như với một mục không gộp.

Trên bộ 9 tài liệu thật, các luật soát cây loại được 8 mục ma và kéo một tài
liệu từ 5 cấp giả về đúng cấp.

Xem cây chỉ mục để đối chiếu với bản gốc:

```bash
python -m docindex.cli "testing file" -o output --outline
```

Cây cũng nằm trong `report.json` (`outline`, `outline_depth`).

## Bố cục bản tài liệu dựng lại

| Quy tắc | Cách làm |
|---|---|
| Tiêu đề tài liệu | Đứng đầu trang 1, căn giữa, 24pt in đậm — gốc của cây chỉ mục |
| Cỡ chữ tiêu đề | Giảm dần theo cấp: `1.` 20pt → `1.1` 17pt → `1.1.1` 15pt (cây dừng ở cấp 3) |
| Cỡ chữ nội dung | 10.5pt — luôn nhỏ hơn mọi cấp tiêu đề ít nhất 1pt |
| Tên mục | Luôn đứng riêng một dòng, in đậm, sát lề trái, cách đoạn trên/dưới 16pt/9pt |
| Không bịa thêm dòng tiêu đề | Mỗi dòng thừa là một nhánh giả trong cây của hệ RAG. Ngoại lệ duy nhất là hậu tố `(tiếp)` khi một mục dài quá trần token phải cắt làm nhiều phần |
| Mục dài quá 512 token | Cắt thành nhiều phần *dài xấp xỉ nhau*, mỗi phần mở lại bằng dòng `1.2 Tên mục (tiếp)`. Hệ RAG cắt chunk theo cây chỉ mục chứ không theo trang, nên một mục bốn nghìn token sẽ thành đúng một chunk bốn nghìn token và bị khâu embedding cắt cụt |
| Ngắt trang | Do Word/MuPDF tự quyết định. Ước lượng số dòng của tool không bao giờ khớp engine dàn trang thật, ép ngắt theo ước lượng thì trang nào tính hụt sẽ tràn và để lại một trang gần như trống |
| Tiêu đề không đứng trơ cuối trang, không vắt qua hai trang | `.docx`: đánh cờ `keep_with_next`. `.pdf`: dựng xong **đọc lại từng trang**, thấy trang nào kết thúc bằng một dòng tiêu đề thì đánh dấu mở trang mới ngay trước nó rồi dựng lại (xem bên dưới) |
| Hình không bị bóp nhỏ | Chỗ trống cuối trang không đủ cao thì MuPDF *co ảnh lại* cho vừa thay vì đẩy sang trang sau — một lưu đồ cao 630pt bị nén còn một phần ba và mất hết chữ trong ô. Mở trang mới trước khi đặt |
| Dòng gạch đầu dòng | Thụt lề 18pt, treo dòng — trông đúng kiểu danh sách, không lẫn vào văn xuôi |
| Bảng, hình | Bảng dựng lại thành bảng thật, hàng tiêu đề lặp lại ở mỗi trang và không hàng nào bị ngắt làm đôi; hình nhúng vào đúng vị trí |

Thêm `--page-per-section` nếu muốn ép **mỗi mục một trang riêng** và mỗi trang
gọn trong 512 token (ranh giới chunk trùng ranh giới trang). Muốn giữ nguyên
bố cục gốc như file đầu vào (chỉ gỡ logo và đầu/chân trang) thì thêm
`--keep-layout`.

### Vì sao phải dựng lại vài lượt mới chặn được tiêu đề trơ

Chỗ ngắt trang do MuPDF quyết định *sau khi* đã xếp chữ, và API đặt khối không
nói được phần nào thật sự hiện ra trên giấy: với một hàng bảng cao hơn chỗ
trống còn lại, nó báo đã dùng hết chỗ nhưng thực tế chỉ vẽ mỗi dòng tiêu đề rồi
đẩy cả hàng sang trang sau. Cách duy nhất chắc chắn là **đọc lại trang vừa
dựng**: trang nào có dòng cuối cùng là chữ cỡ tiêu đề mà bên dưới không còn gì
thì đánh dấu khối đó, dựng lại và mở trang mới ngay trước nó.

Đẩy một tiêu đề xuống làm mọi thứ phía dưới trôi theo và đôi khi lộ ra một tiêu
đề trơ khác, nên vòng soát chạy tối đa 10 lượt — nhưng dừng ngay khi một lượt
không tìm thấy chỗ nào mới, nên tài liệu bình thường chỉ tốn đúng một lượt.
Đo trên 32 tài liệu / 526 trang: **không còn tiêu đề nào đứng trơ**.

## Vì sao trình bày như vậy — DLA nhìn thấy gì

Mô hình DLA nhìn **trang giấy**, không đọc cấu trúc file. Nhãn nó gán cho mỗi
dòng phụ thuộc vào ba dấu hiệu hình học, nên bản dựng lại được ép đúng ba dấu
hiệu đó:

| Nhãn DLA | Dấu hiệu nó dựa vào | Tool làm gì |
|---|---|---|
| `title` | chữ đậm, cỡ lớn hơn hẳn nội dung, đứng riêng dòng, nhiều khoảng trắng trên/dưới | Tên tài liệu 24pt, tiêu đề in đậm 11.5–20pt theo cấp, cách đoạn 16pt/9pt, sát lề trái, không bao giờ dính nội dung viết liền phía sau |
| `list-item` | có ký hiệu đầu mục, thụt vào so với lề, cỡ chữ bằng nội dung | Mỗi dòng gạch đầu dòng là một đoạn riêng, thụt lề 18pt |
| `text` | đoạn căn đều, không thụt, không ký hiệu | Nội dung 10.5pt căn đều |
| `header` / `footer` | chữ nằm sát mép trên/dưới trang | Bản dựng lại **không có** đầu/chân trang, số trang cũng bỏ luôn |

Ngoài ra tiêu đề còn được đánh dấu `outlineLvl` trong `.docx`, nên bản Word
mở ra có sẵn cây mục lục đúng cấp.

## Ký tự đọc ra phải đúng là ký tự đã ghi vào

Hai chỗ làm hỏng chữ mà nhìn trên giấy hoàn toàn không thấy gì bất thường.

**Bảng ToUnicode trỏ nhầm sang ký tự song trùng.** Font TrueType nhúng vào PDF
làm MuPDF dựng lại bảng ToUnicode với vài glyph trỏ sang một ký tự khác hẳn
nhưng vẽ ra y hệt:

| Ghi vào | Đọc ra | Hậu quả |
|---|---|---|
| dấu cách | `U+00A0` dấu cách không ngắt | chuỗi **không có lấy một dấu cách thường nào** |
| `-` | `U+00AD` gạch nối mềm | gạch nối mềm vốn là ký tự *vô hình* |
| `;` | `U+037E` dấu chấm hỏi Hy Lạp | mất ranh giới vế câu |

Tách từ, BM25 và tokenizer phía RAG hỏng theo mà không báo lỗi. Tool sửa lại
bảng ToUnicode sau bước rút gọn font, ngay trước khi lưu file.

**Ký hiệu đặc biệt trong văn bản nguồn.** Công thức tính lãi được soạn bằng
Equation Editor nên `Mi` thật ra nằm ở khối Mathematical Alphanumeric Symbols
(`𝐌𝐢`), khối mà hệ RAG không có font — nó đọc ra thành `$Mi$` hoặc bỏ hẳn.
Tool quy hết về chữ Latin thường ngay từ khâu làm sạch:

| Nguồn | Thành | |
|---|---|---|
| `𝐌𝐢`, `𝑳`, `𝟏𝟐` | `Mi`, `L`, `12` | chữ toán học → chữ Latin (`NFKC`) |
| `∑`, `∏`, `√` | `Tổng`, `Tích`, `căn` | đọc thành lời luôn |
| `≤ ≥ ≠ × ÷ ± → ∗` | `<= >= != x / +/- -> *` | ký hiệu toán → dấu ASCII |
| `– — ‐ “ ” ‘ ’ •` | `- - - " " ' ' -` | dấu câu kiểu chữ → ASCII |
| `U+F02B`, `U+F0B7` | `+`, `-` | font Symbol của Word đẩy glyph vào vùng dùng riêng nhưng giữ nguyên vị trí mã ASCII |

Vùng chữ cái của font Symbol (bảng Hy Lạp, hoạ tiết Wingdings) thì **không**
dịch sang chữ Latin — làm vậy sẽ đẻ ra một từ không hề có trong văn bản.

Đo trên 32 tài liệu / 526 trang bản dựng lại: không còn ký tự lạ nào.

## Cách dùng

### Giao diện kéo thả

Nhảy đúp vào **shortcut `docindex` trên Desktop**. Chưa có thì tạo bằng cách
nhảy đúp `tao-shortcut.bat`.

Hoặc chạy trực tiếp `docindex-gui.bat`, hoặc:

```bash
python -m docindex.gui
```

Kéo thả file `.pdf`/`.docx` (hoặc cả thư mục) vào ô lớn phía trên, chỉnh tuỳ
chọn nếu cần rồi bấm **Bắt đầu xử lý**. Ô **Trần token** đặt trần cho chunk
`.jsonl` (mặc định 512). Cửa sổ hiển thị tiến độ từng tài liệu, số chunk, độ
sâu cây chỉ mục, token trung vị và cao nhất, số logo đã loại, số hình giữ lại
và cảnh báo chất lượng.

#### Bảng "Làm sạch tài liệu"

Chọn **đúng những thứ muốn gỡ**, không phải gỡ tất hay không gỡ gì. Ô **Mức làm
sạch** là bốn tổ hợp dựng sẵn; tick tay từng ô bên dưới thì nó tự nhảy sang
*Tuỳ chỉnh*. Dòng chữ nhỏ cuối bảng luôn ghi rõ lần chạy này sẽ gỡ những gì.

| Mức làm sạch | Gỡ những gì |
|---|---|
| **Chỉ gỡ logo và ảnh bìa** (mặc định) | Logo, hoạ tiết lặp, ảnh nền, ảnh bìa trang đầu. Chữ nằm y nguyên chỗ cũ, bố cục gốc giữ nguyên |
| Gỡ thêm đầu/chân trang | Như trên, cộng chữ ở đầu trang và chân trang |
| Làm sạch toàn bộ, giữ bố cục | Như trên, cộng phần mục lục và trang chỉ còn mục lục |
| Dựng lại bố cục cho RAG | Dựng lại tài liệu từ cây chỉ mục — bỏ hết logo, đầu/chân trang, mục lục |

Bốn ô **Gỡ logo và hoạ tiết lặp** / **Gỡ ảnh bìa ở trang đầu** / **Gỡ chữ đầu
trang – chân trang** / **Gỡ phần mục lục** bật tắt được riêng lẻ. Hình minh hoạ
trong thân bài không bao giờ bị gỡ, dù chọn mức nào.

Tick **Dựng lại bố cục cho hệ RAG đọc cây chỉ mục** để đổi sang lối dựng lại; khi
đó bốn ô trên mờ đi vì nội dung lấy từ cây mục lục đã trích xuất nên chúng
không còn gì để điều khiển, chỉ còn **Gỡ ảnh bìa** là vẫn có tác dụng. Ô **Định
dạng bản dựng lại** (mặc định `.pdf`) và **Mỗi mục một trang riêng** chỉ dùng
được ở lối này.

Xong thì bấm **Xem cây chỉ mục** để soát lại thứ bậc đề mục của từng tài liệu
ngay trong cửa sổ — đây là thứ cần đối chiếu với bản gốc trước khi nạp tri
thức, vì title của mọi chunk dựng từ cây này. Cây cũng được ghi ra
`<tên tài liệu>.outline.txt` trong thư mục kết quả. Bấm **Mở thư mục kết quả**
để lấy file.

Một file lỗi không làm dừng cả lô — phần còn lại vẫn chạy tiếp và lỗi được ghi
rõ trong nhật ký.

Kéo thả cần thư viện `tkinterdnd2`. Thiếu nó thì ô thả đổi màu và ghi rõ là
chưa dùng được — giao diện vẫn chạy bình thường qua nút **Chọn file…** /
**Chọn thư mục…**. Bật lại kéo thả:

```bash
pip install tkinterdnd2
```

Cài xong phải **đóng và mở lại** giao diện thì mới có tác dụng. Nếu bạn mở
bằng shortcut trên Desktop, hãy cài vào đúng Python mà shortcut dùng — bấm
chuột phải shortcut → *Properties* để xem đường dẫn ở ô *Target*.

### Dòng lệnh

```bash
python -m docindex.cli "testing file" -o output
```

Xử lý một file:

```bash
python -m docindex.cli "testing file/Quy dinh.docx" -o output
```

Gộp tất cả tài liệu vào một file duy nhất:

```bash
python -m docindex.cli "testing file" -o output --merge
```

Kết quả: mỗi tài liệu một file `.jsonl` (một chunk mỗi dòng) và `report.json`
tổng hợp thống kê chất lượng.

### Tuỳ chọn

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `--max-tokens` | 512 | Trần token của một chunk, đã trừ tiền tố mục lục (và của một trang khi bật `--page-per-section`) |
| `--min-tokens` | 200 | Dưới ngưỡng này thì mục/chunk được gộp với mục liền trước |
| `--keep-short-sections` | tắt | Giữ nguyên mục quá ngắn, không gộp |
| `--overlap` | 1 | Số câu lặp lại khi một mục bị cắt ngang trang (0 = tắt) |
| `--no-prefix` | tắt | Không chèn đường dẫn mục lục vào nội dung chunk |
| `--path-depth` | 4 | Số cấp mục lục đưa vào tiền tố |
| `--format` | pdf | Định dạng bản dựng lại: `pdf`, `docx`, `same` (giống file gốc) |
| `--page-per-section` | tắt | Mỗi mục một trang riêng, mỗi trang ≤ `--max-tokens` |
| `--outline` | tắt | In cây chỉ mục từng tài liệu để đối chiếu với bản gốc |
| `--keep-layout` | tắt | Giữ nguyên bố cục gốc thay vì dựng lại tài liệu |
| `--merge` | tắt | Gộp mọi tài liệu vào `chunks.jsonl` |
| `--preview N` | 0 | In thử N chunk đầu mỗi tài liệu |
| `--no-clean` | tắt | Không xuất bản tài liệu đã làm sạch |
| `--no-jsonl` | tắt | Chỉ làm sạch tài liệu, không tạo chunk |
| `--keep-cover` | tắt | Giữ lại hình bìa ở trang đầu |
| `--keep-logo` | tắt | Giữ lại logo và hoạ tiết lặp (chỉ có tác dụng với `--keep-layout`) |
| `--keep-header-footer` | tắt | Giữ nguyên chữ đầu trang / chân trang (chỉ với `--keep-layout`) |
| `--keep-toc` | tắt | Giữ nguyên phần mục lục (chỉ với `--keep-layout`) |
| `--extract-figures` | tắt | Tách hình ra PNG (bản dựng lại luôn tự tách để nhúng hình) |

Bốn tham số `--keep-*` cho phép chọn riêng từng thứ cần gỡ khi giữ bố cục gốc.
Ví dụ **chỉ gỡ logo và ảnh bìa**, chữ để nguyên chỗ cũ:

```bash
python -m docindex.cli tai-lieu.pdf -o output --keep-layout --keep-header-footer --keep-toc --no-jsonl
```

## Chunk trông như thế nào

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

- `text` — dùng để tính vector (đã gắn sẵn đường dẫn mục lục ở đầu).
- `est_tokens` — số token ước lượng của `text`, luôn ≤ `--max-tokens`. Ước
  lượng lấy mức **cao hơn** thực tế (2.8 ký tự/token, có tính dấu câu), vì đếm
  thiếu thì chunk lọt qua mọi kiểm tra rồi mới bị cắt cụt ở khâu embedding.
- `raw_text` — nội dung gốc, dùng để hiển thị cho người đọc.
- `page_source` — `actual` (PDF, trang thật) hoặc `rendered`/`page_break`/
  `estimated` với DOCX, do file Word không lưu sẵn số trang.

## Lấy đủ ngữ cảnh khi truy vấn

Chunk nhỏ giúp tìm kiếm chính xác, nhưng câu trả lời cần nội dung đầy đủ của
mục. Khi chunk khớp truy vấn có `is_continued` hoặc `is_continuation` bằng
`true`, hãy ghép các phần còn lại của cùng mục:

```python
from docindex.retrieval import load_chunks, index_by_id, expand_section

chunks = load_chunks("output/chunks.jsonl")
by_id = index_by_id(chunks)

hit = chunks[30]                      # chunk do vector search trả về
context = expand_section(hit, by_id)  # nội dung đầy đủ của mục đó
```

## Tool xử lý những gì

| Vấn đề | Cách xử lý |
|---|---|
| Word đánh số tự động, chỉ mục không nằm trong text | Dựng lại số mục từ `numbering.xml` theo chuẩn OOXML |
| Word khai `a) b) c)` ngang cấp với `1. 2. 3.` | Xếp thứ bậc theo loại ký hiệu (`numFmt`), không theo cấp danh sách |
| Câu văn mở đầu bằng số bị nhận thành đề mục | Đối chiếu với dãy chỉ mục đang mở, không nối tiếp được thì loại |
| Đề mục sâu quá cấp 3 (`1.1.1.1`) | Không vào cây nữa, trở lại làm nội dung thường của mục cha — số mục giữ nguyên trong dòng chữ |
| Header/footer lặp ở mọi trang | Nhận diện dòng lặp trên ≥50% số trang rồi loại bỏ |
| Trang mục lục sinh chunk rác | Bỏ trang có nhiều dòng dấu chấm dẫn |
| Một mục trải nhiều trang | Cắt theo trang, giữ đường dẫn mục lục + cờ `is_continued` + prev/next |
| Một trang chứa nhiều mục | Tách thành nhiều chunk, không trộn hai chủ đề |
| Mục quá ngắn (chỉ có tiêu đề) | Gộp với mục con/anh em cùng trang |
| Mục quá dài | Cắt tại ranh giới câu, bảng cắt theo hàng và lặp lại dòng tiêu đề |
| Bảng trải nhiều trang | Trích thành markdown, tính đúng số trang bị ngắt |
| PDF tách ký hiệu đầu mục ra cột riêng | Gộp lại theo hàng ngang |
| PDF lưu từng dòng hiển thị | Nối lại thành đoạn văn hoàn chỉnh; câu còn dở ở cuối khối được nối với khối sau |
| Từ bị gạch nối cắt đôi cuối dòng (`…và Dai-` / `ichi Life…`) | Nối liền lại, kể cả khi hai dòng khác kiểu chữ |
| Câu dẫn chiếu "…tại Điều 2.3 Phần 1 này" | Không nhận nhầm thành tiêu đề |
| Logo, hoạ tiết, ảnh nền trang trí | Loại bỏ hoàn toàn, không đưa vào chunk |
| Hoa văn chìm to bằng nửa trang | Nhận ra vì không có lấy một nét đậm nào — xem [Hoa văn chìm](#hoa-văn-chìm-không-lọt-được-lưới-kích-thước) |
| Biểu đồ, sơ đồ minh hoạ | Giữ chỗ bằng dòng `[HÌNH: …]` + metadata, tách PNG được |
| Số trang "9/34", "Lưu hành nội bộ" | Loại theo mẫu câu và theo mức độ lặp giữa các trang |
| Bìa và mục lục nằm chung một trang | Chỉ cắt phần mục lục, giữ nguyên nội dung bìa |
| Ô gộp tạo cột rỗng trong bảng | Lược bỏ cột rỗng khi dựng bảng markdown |
| Hàng bảng dài quá trần chunk | Chuyển sang dạng "tên cột: giá trị", không cắt vỡ bảng |
| Bảng một ô gộp trải cả trang | Nhận ra đây là khung văn bản, trả về nguyên văn rồi cắt theo câu — không thì cả khối vượt trần token |
| Dấu cách trong PDF thành `U+00A0` | Sửa bảng ToUnicode để đọc lại ra dấu cách thường |
| Tiêu đề viết liền cả nội dung | Rút gọn cho đường dẫn, nội dung vẫn giữ đủ chữ |
| File nguồn rơi ký tự khi tạo PDF | Cảnh báo trong báo cáo để bạn lấy lại bản gốc |

## Lọc nhiễu và giữ hình

Logo và chân trang là nhiễu thuần tuý — đưa vào chunk chỉ làm loãng vector.
Ngược lại biểu đồ, sơ đồ là nội dung thật, cần biết mục nào có hình.

Tool phân biệt hai loại bằng các dấu hiệu đo được:

| Xếp là **logo** (loại bỏ) | Xếp là **hình minh hoạ** (giữ) |
|---|---|
| Cùng một ảnh lặp trên ≥2 trang | Chỉ xuất hiện một lần |
| Cạnh ngắn < 70px hoặc < 2.5% diện tích trang | Đủ lớn trong vùng nội dung |
| Nằm ở lề trên/dưới trang | Nằm giữa trang |
| Phủ kín trang mà trang vẫn nhiều chữ (ảnh nền) | — |
| Không có lấy một nét đậm nào (hoa văn chìm) | Có mực đậm để đọc được |
| Chữ sống của trang nằm đè lên (khung, dải màu) | Chữ nằm sẵn trong file ảnh |
| Hàng chục nghìn màu (ảnh chụp quảng cáo) | Vài trăm màu (nét vẽ, mảng phẳng) |

Trang bìa gỡ xong thường chẳng còn gì — bìa vốn chỉ có mỗi tấm ảnh — nên trang
rỗng đó bị bỏ hẳn thay vì để lại một tờ trắng trong bản sạch.

### Bản scan: không gỡ logo được, và tool nói thẳng ra

Trang scan là **một tấm ảnh chụp cả trang**: chữ, logo, đầu/chân trang, mục lục
đều là điểm ảnh nằm chung trong đúng một tấm hình. Không có đối tượng riêng nào
để gỡ, mà gỡ tấm hình ấy đi thì trang trắng trơn. Chunk cho ra cũng chỉ có mỗi
dòng giữ chỗ `[HÌNH: …]`, cây chỉ mục rỗng — nạp vào RAG là vô nghĩa.

Trước đây tool vẫn chạy êm và cho ra một file 6.6 MB trông như thành công. Giờ
tài liệu nào có **từ 80% số trang trở lên không có lớp text** sẽ bị báo ngay từ
lúc trích xuất, cả trong giao diện lẫn dòng lệnh:

```
[!] 21/21 trang không có lớp text — đây là bản scan. Chữ và logo nằm chung
    trong một tấm ảnh chụp cả trang nên không gỡ riêng logo được, chunk cũng
    chỉ có dòng giữ chỗ [HÌNH]. Chạy OCR (hoặc lấy bản .docx/.pdf gốc) rồi
    đưa lại vào tool.
```

Cảnh báo hiện ra **trước** khâu làm sạch, nên không phải chờ hết cả lô mới biết.
`report.json` ghi kèm `pages_total` / `pages_without_text` để đối chiếu.

Trong **bản tài liệu dựng lại**, hình minh hoạ được nhúng lại đúng chỗ của nó
trong mạch nội dung của mục; logo, hình bìa và đầu/chân trang không được đưa
sang. Hình cao hơn một trang thì thu nhỏ cho vừa.

Với `--keep-layout`, việc gỡ ảnh được thực hiện theo đúng mã tham chiếu của
từng ảnh, không xoá theo vùng. Logo hay nằm đè lên hình minh hoạ lớn, xoá theo
vùng sẽ cuốn mất luôn cả hình nội dung.

### Hoa văn chìm: không lọt được lưới kích thước

Bàn tay đỡ chiếc khiên in mờ giữa trang, chiếc lá, quả địa cầu — thứ hoa văn
này **to bằng cả nửa trang** nên vượt mọi ngưỡng kích thước, và **chỉ in đúng
một lần** nên phép đếm số trang lặp cũng không bắt được. Bốn dấu hiệu ở bảng
trên đều trượt, nó đi thẳng vào chunk như một hình minh hoạ thật.

Dấu hiệu còn lại nằm ở chỗ khác: hoa văn phải in mờ thì chữ đè lên mới đọc
được, nên nó **không có lấy một nét đậm nào**. Biểu đồ hay lưu đồ thì ngược
lại — không có mực đậm thì chính nó không đọc được. Tool đọc lại vùng ảnh đúng
như mắt người nhìn thấy trên trang (phần trong suốt đã chồng lên nền giấy) rồi
đếm tỉ lệ điểm ảnh đậm hơn mức xám 170:

| | Tỉ lệ điểm ảnh đậm |
|---|---|
| Hoa văn chìm | 0.00 – 0.02 |
| Mọi hình nội dung trong bộ tài liệu thật | 0.17 – 0.68 |

Ngưỡng đặt ở **0.05**, cách cả hai bên ít nhất ba lần — có test giữ đúng khoảng
trống này để lần đổi số sau không âm thầm gỡ mất một biểu đồ nhạt màu. Ảnh có
chú thích ("Hình 3: …") thì bỏ qua phép đo: người soạn đã tự nhận đó là hình
nội dung.

### Khung bo góc và ảnh chụp quảng cáo: mực đậm nhưng vẫn là trang trí

Phép đo mực chia cho *số điểm không phải nền giấy*, nên một khung bo góc màu đỏ
vẽ quanh đoạn văn — chỉ có mấy nét đỏ trên nền trong suốt — chấm **0.67**, ngang
với sơ đồ dày đặc nhất. Mảng ảnh chụp gia đình ở trang bìa từng phần cũng vậy.
Cả hai đi thẳng vào tài liệu sạch như hình nội dung.

Hai dấu hiệu khác tách được chúng ra:

| | Chữ sống của trang đè lên | Số màu (dựng lại ở 48 dpi) |
|---|---|---|
| Khung, dải màu vẽ dưới chữ | 292 – 523 ký tự | 758 – 1 229 |
| Ảnh chụp quảng cáo | 0 – 523 ký tự | 39 832 – 48 968 |
| **Sơ đồ, biểu đồ nội dung** | **0 ký tự** | **610 – 621** |

Khung được vẽ *dưới* chữ nên chữ của trang nằm đè lên nó; nhãn của một sơ đồ thì
nằm sẵn trong chính file ảnh, vùng đó không có lấy một ký tự sống nào. Còn ảnh
chụp thì chuyển sắc liên tục, sơ đồ vẽ bằng mảng màu phẳng — ngưỡng đặt ở
**8 000 màu**, cách mép trên 6.5 lần và mép dưới 5 lần.

Trang scan là một tấm ảnh chụp cả trang, đo thì rớt mà gỡ đi là mất trắng nội
dung, nên trang nào có dưới 200 ký tự chữ sống thì ảnh của nó **không đem ra
đo** — ảnh chính là nội dung của trang. Ảnh có chú thích vẫn được miễn cả hai
phép đo.

Đo trên 6 tài liệu bảo hiểm: hai luật này gỡ thêm **19 ảnh trang trí** mà không
đụng tới sơ đồ nội dung nào lẫn 38 trang scan.

### Đầu/chân trang: lặp lại **ở đúng một chỗ**

Chân trang không phải tài liệu nào cũng nằm sát đáy — có bản đặt số trang ở 81%
chiều cao, cao hơn mọi mốc chân trang hợp lý. Nới dải quét ra thì lại cuốn theo
nội dung thật (ô `Trang 5` của bảng mục lục chẳng hạn).

Nên luật là **lặp lại trên phần lớn số trang *và* lần nào cũng ở đúng một toạ
độ**. Riêng điều kiện thứ nhất chưa đủ — một câu dẫn chiếu hay lặp cũng thoả —
thêm điều kiện thứ hai vào thì chỉ còn đúng thứ được đặt trong khung đầu/chân
trang. Số trang thì mỗi trang một khác, nhưng khi xoá hết chữ số đi thì `1/5`
và `2/5` cùng quy về một chuỗi nên vẫn bắt được.

Trong **file chunk**, hình được thay bằng một dòng giữ chỗ:

```
[HÌNH: Biểu đồ 1: Cơ cấu quyền lợi | 325x324 | tailieu_p3_1.png]
```

kèm cờ `has_figure` và danh sách `figures` (kích thước, chú thích, đường dẫn
file) trong metadata. Nhờ vậy khi truy vấn bạn biết mục đó có hình để hiển thị
kèm, hoặc đưa file ảnh qua mô hình đọc ảnh nếu cần.

Chú thích được nhận từ các mẫu quen thuộc: `Hình 1:`, `Biểu đồ 2 -`,
`Sơ đồ 3.`, `Figure 4:`…

Với DOCX, logo thường nằm ở header/footer — vốn không thuộc phần thân tài liệu
nên không bao giờ lọt vào chunk.

### Lưu đồ vẽ bằng nét

Lưu đồ quy trình trong PDF không phải ảnh: nó là một mớ nét vẽ cộng với chữ nằm
trong từng ô. Trích xuất theo lối thường sẽ moi hết chữ ấy ra rồi xếp thành một
dòng dài vô nghĩa — *cả sơ đồ biến mất, chỉ còn lại chữ rời rạc*:

```
| Nội dung Mẫu ĐVKD lưu Đại ký lưu ký lưu Tiếp nhận và khai báo Kiểm tra, đối chiếu danh sách …
```

Bảng cũng vẽ bằng nét nên phải phân biệt cho được. Khác nhau ở chỗ: đường kẻ
bảng bao giờ cũng **ngang hoặc dọc**, còn lưu đồ thì có **nét chéo** (mũi tên,
cạnh hình thoi) và **nét cong** (hình bầu dục, góc bo). Đo trên tài liệu thật,
trang bảng có đúng 0 nét như vậy còn trang lưu đồ có vài chục.

Vùng nào có từ 6 nét chéo/cong trở lên được xuất thành ảnh PNG rồi nhúng lại
vào bản dựng lại, còn chữ bên trong vùng đó bị bỏ đi vì đã nằm sẵn trong ảnh.
Nếu không xuất được ảnh (chạy không kèm thư mục hình) thì chữ vẫn giữ nguyên —
thà lộn xộn còn hơn mất trắng cả khối nội dung.

## Cấu trúc mã nguồn

```
docindex/
  gui.py           giao diện kéo thả
  cli.py           điểm vào dòng lệnh
  export.py        xuất bản tài liệu sạch (dựng lại bố cục hoặc giữ bố cục gốc)
  layout.py        dựng cây chỉ mục thành bố cục: cỡ chữ theo cấp, chia trang
  render.py        ghi bố cục ra .docx / .pdf
  pipeline.py      ghép các bước cho từng file
  extract_pdf.py   PDF -> block (PyMuPDF)
  extract_docx.py  DOCX -> block (python-docx)
  numbering.py     dựng lại chỉ mục tự động của Word
  images.py        phân loại logo / hình minh hoạ, nhận lưu đồ vẽ bằng nét, tách ảnh
  headings.py      nhận diện tiêu đề, soát dãy chỉ mục, dựng cây mục lục
  chunker.py       cắt chunk theo trang và theo mục
  report.py        kiểm tra chất lượng
  retrieval.py     mở rộng ngữ cảnh lúc truy vấn
  models.py        kiểu dữ liệu dùng chung, chuẩn hoá ký hiệu đặc biệt
```

## Kiểm tra chất lượng

`report.json` ghi lại thống kê từng tài liệu: số chunk, số tiêu đề nhận được,
`tokens_median` / `tokens_max` so với `token_limit`, độ dài chunk, số logo đã
loại, số hình giữ lại và các cảnh báo. `tokens_max` chạm đúng `token_limit` là
bình thường — vượt mới là lỗi, và khi đó `too_long` khác 0 kèm cảnh báo.

Đáng chú ý nhất là cảnh báo `suspect_truncated_lines`: một số PDF được tạo từ
Word bị rơi ký tự ngay trong file gốc (ví dụ "Khách hàng" thành "h hàng").
Tool không thể khôi phục phần đã mất, nên khi thấy cảnh báo này hãy xử lý từ
bản `.docx` gốc nếu còn.

Bộ test kiểm tra các ràng buộc quan trọng trên chính tài liệu thật: không chunk
nào vượt 512 token, không trang nào vượt 512 token, tiêu đề trong PDF phải in
đậm và to hơn nội dung, không tiêu đề nào đứng trơ cuối trang, ký tự đọc lại
phải đúng ký tự đã ghi vào (dấu cách, dấu gạch, chấm phẩy), lưu đồ phải còn
nguyên dạng hình, cùng chốt chặn chống mất chữ (mọi tài liệu phải giữ ≥95% số
từ, phần còn lại là mục lục và đầu/chân trang bị loại có chủ đích):

```bash
python -m pytest tests/ -q
```

## Yêu cầu

Python 3.10+, `PyMuPDF`, `python-docx`. Thêm `tkinterdnd2` nếu muốn kéo thả
trong giao diện.

```bash
pip install PyMuPDF python-docx tkinterdnd2
```
