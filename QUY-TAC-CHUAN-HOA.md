# QUY TẮC CHUẨN HOÁ ĐỀ MỤC VÀ TEXT

Áp dụng cho pipeline trích xuất tài liệu hành chính/pháp lý tiếng Việt (PDF/DOCX)
để đưa vào hệ RAG. Đây là spec bắt buộc — không tự ý nới lỏng, mỗi con số trong
đây đều có lý do đi kèm.

## 0. Nguyên tắc bất biến

1. **Không mất chữ.** Mọi bước chuẩn hoá chỉ được đổi *hình thức*, không được xoá
   nội dung. Đề mục bị hạ cấp/bị gộp thì dòng tiêu đề của nó phải được đẩy xuống
   thân bài, kèm nguyên số mục.
2. **Một chunk = một chủ đề.** Chủ đề được xác định bằng cây mục lục, nên chất
   lượng chunk phụ thuộc trực tiếp vào việc nhận diện đề mục có đúng không.
3. **Nhận nhầm nguy hiểm hơn bỏ sót.** Một câu văn bị nhận nhầm thành đề mục cấp
   cao sẽ *nuốt toàn bộ* các mục đứng sau nó làm con → title của hàng loạt chunk
   bên dưới sai theo. Bỏ sót một đề mục chỉ làm mục đó dính vào mục cha.
4. **Tín hiệu cấu trúc thật > phỏng đoán hình thức.** Chỉ mục do Word sinh ra
   (`numbering.xml`) là cấu trúc thật của tài liệu → luôn tin. Chỉ mục đoán từ
   regex trên text PDF là phỏng đoán → phải qua bộ lọc.

---

## 1. Chuẩn hoá ký tự (text normalization)

Chạy đúng thứ tự này, trên **mọi** chuỗi text lấy ra từ file:

1. **Xoá ký tự vô hình**: U+200B (zero-width space), U+200C, U+200D, U+FEFF
   (BOM), U+00AD (soft hyphen).
2. **NFKC normalize**. Xử lý được phần lớn: chữ toán học
   (Mathematical Alphanumeric Symbols — công thức Equation Editor cho ra `𝐌𝐢`
   chứ không phải `Mi`, hệ RAG không có font sẽ đọc thành `$Mi$` hoặc bỏ hẳn),
   chữ số toàn rộng, ligature, non-breaking space.
3. **Gỡ glyph vùng Private Use Area** (U+E000–U+F8FF). Font Symbol/Wingdings nhúng
   trong Word đẩy glyph vào U+F0xx nhưng **giữ nguyên vị trí mã ASCII**:
   - `U+F020`–`U+F07E` → `chr(code - 0xF000)`, **nhưng chỉ nhận lại dấu câu và
     chữ số**; nếu ra chữ cái thì thay bằng khoảng trắng (vùng chữ cái là bảng Hy
     Lạp hoặc hoạ tiết Wingdings, dịch sang Latin sẽ đẻ ra một từ không hề có
     trong văn bản).
   - Ngoại lệ có bảng tra riêng: `U+F0B7`→`-`, `U+F0A7`→`-`, `U+F0D8`→`->`,
     `U+F0E0`→`->`.
   - Còn lại trong vùng PUA → khoảng trắng.
4. **Quy ký hiệu về chữ/ASCII tương đương** (bảng tra):
   - Toán: `∑`→` Tổng `, `∏`→` Tích `, `√`→` căn `, `∞`→` vô cùng `,
     `≤`→`<=`, `≥`→`>=`, `≠`→`!=`, `≈`→`~=`, `±`→`+/-`, `×`→`x`, `÷`→`/`,
     `∗`→`*`, `→`→`->`, `⇒`→`=>`.
   - Gạch ngang mọi loại (`‐ ‑ ‒ – — − ―`) → `-`.
   - Nháy kiểu chữ (`“ ” „ « »`) → `"`; (`‘ ’ ‚ ′`) → `'`; `″` → `"`.
   - Bullet (`• ▪ ● ◦ ○`) → `-`.
5. **Gộp khoảng trắng**: NBSP và tab → space; ≥2 space liên tiếp → 1 space;
   `strip()` hai đầu.

Quy tắc: **không** hạ dấu tiếng Việt, **không** lowercase, **không** bỏ dấu câu,
**không** đổi `Đ/đ`. Chuẩn hoá là để hệ RAG tokenize được, không phải để so khớp.

---

## 2. Chuẩn hoá dòng và đoạn

### 2.1 Nối dòng bị PDF cắt

PDF lưu theo dòng hiển thị nên một câu hay nằm rải ở hai khối. Nối khối `follow`
vào `lead` khi **cả hai** điều kiện đúng:

- `lead` **không** kết thúc bằng `[.:;!?]` (kèm khoảng trắng cuối).
- `follow` **không** mở đầu một ý mới, tức không khớp:
  `^\s*([-–—•+*] | \(?[a-zA-Zđ][.)]\s | \(?[ivx]+[.)]\s | \d+(\.\d+)*[.)]?\s)`

**Không** xét chữ hoa đầu dòng làm điều kiện: nửa sau của câu rất hay bắt đầu
bằng tên riêng ("…Dai-ichi Life Việt" / "Nam được…").

### 2.2 Bảng làm phẳng

Mỗi hàng bảng → một dòng, các cột ngăn nhau bằng ` | `, mỗi cột dạng
`Tên cột: giá trị`. Bảng markdown thì bỏ hẳn cột rỗng toàn bộ (ô gộp trong
Word/PDF bị trích ra thành nhiều cột trống liền nhau).

---

## 3. Nhận diện đề mục

### 3.1 Bảng thứ bậc (rank — số nhỏ = cấp cao)

| rank | Loại | Ví dụ |
|---|---|---|
| -1 | banner (tiêu đề lớn không đánh số) | `TỔNG QUAN VĂN BẢN QUY ĐỊNH` |
| 0 | part | `PHẦN 1`, `Phần I` |
| 1 | chapter / appendix | `CHƯƠNG I`, `Phụ lục 01`, `PL02.1003.PCS.2026(1)` |
| 2 | section | `MỤC 1` |
| 3 | roman-upper | `I.` `II.` `III.` |
| 4 | article | `ĐIỀU 5` |
| 10 | decimal | `1.` `2.1.` `2.3.1.` |
| 25 | letter-upper | `A)` `B)` |
| 30 | letter | `a)` `b)` |
| 50 | roman | `(i)` `(ii)` |

**Các mốc phải cách nhau rộng**, vì cấp danh sách của Word được *cộng thêm* vào
mốc. Hai loại ký hiệu khác nhau không bao giờ được rơi trúng cùng một số, nếu
không hai cấp khác nhau bị nén thành một khi dựng cây.

Với chỉ mục decimal, rank = `10 + (số dấu chấm)`, tức `2.1` sâu hơn `2` một cấp.

### 3.2 Regex nhận diện

```
^(PHẦN|Phần)\s+([0-9IVXivx]+)\s*[:.\-–]?\s*(.*)$
^(CHƯƠNG|Chương)\s+([0-9IVXivx]+)\s*[:.\-–]?\s*(.*)$
^(PHỤ LỤC|Phụ lục)\s*([0-9IVXivx]*)\s*[:.\-–]?\s*(.*)$
^(PL)\s*([0-9][0-9.()A-Za-z]*)\s*[:\-–]\s*(.*)$      # mã hiệu nội bộ
^(MỤC|Mục)\s+([0-9IVXivx]+)\s*[:.\-–]?\s*(.*)$
^(ĐIỀU|Điều)\s+(\d+)\s*[:.\-–]?\s*(.*)$
^(\d+(?:\.\d+)*)\.?\s+(\S.*)$                        # decimal
^(\d+(?:\.\d+)*)\.?$                                 # decimal, tên ở dòng dưới
^(\(?[ivxIVX]{1,5}[.)])\s+(\S.*)$                    # la mã
^([a-zA-Z][.)])\s+(\S.*)$                            # chữ cái
```

Lưu ý bắt buộc:
- Mẫu `PL...` **không** nhận dấu chấm làm dấu ngăn (chỉ `:` `-` `–`): dấu chấm là
  một phần của chính mã hiệu, nhận nó thì câu dẫn chiếu
  "…theo Phụ lục số PL01.1016.PDS.2026(1);" cũng khớp và đẻ ra một nhánh cấp cao
  nuốt hết các mục đứng sau.
- Mã hiệu viết liền giữ nguyên (`PL02.1003.PCS`), **không** tách thành `PL 02`,
  nếu không tra cứu theo mã sẽ không khớp.
- Ký hiệu giữ nguyên dấu đi kèm (`c.` chứ không phải `c`) để tiêu đề đọc đúng
  như văn bản gốc.

### 3.3 Phân biệt `I.` với `i)`

- Token viết hoa **và** khớp `^(?=[ivx])x{0,3}(ix|iv|v?i{0,3})$` (I..XXXIX) →
  roman-upper (rank 3, cấp lớn trong văn bản hành chính, đứng trên cả `1.`).
- Token dài >1 ký tự, hoặc có dấu ngoặc mở → roman (rank 50, cấp sâu nhất).
- `i)` trơ trọi → **xử như letter** (rank 30): đó thường chỉ là mục thứ 9 của
  danh sách `a) b) c)…`; xếp riêng một cấp sẽ đẻ ra một tầng giả giữa danh sách.

### 3.4 Bộ lọc chống nhận nhầm

Loại một ứng viên đề mục nếu:

- Mở đầu bằng từ dẫn chiếu: `^(theo|tại|quy định tại|căn cứ|xem|nêu tại|như)\b`
  (không phân biệt hoa thường).
- Với `part | chapter | article | section | roman-upper`:
  - Không có phần tên đi kèm → loại. ("PHẦN 1", "ĐIỀU 5" rất hay nằm giữa câu
    dẫn chiếu "…quy định tại Điều 2.3 Phần 1 này"; PDF cắt dòng làm mảnh câu ấy
    trông y hệt tiêu đề.)
  - Ký tự đầu của tên (sau khi bỏ `“ " ' (`) là chữ thường → loại.
  - Nếu **không** phải chỉ mục do Word sinh: phải in đậm **hoặc** ≥80% chữ cái
    của tên là chữ hoa. Tiêu đề thật trong PDF luôn được nhấn mạnh.
- Nếu chỉ mục **do Word sinh ra** → nhận ngay, bỏ qua các bộ lọc hình thức bên
  dưới (Word đã khẳng định đây là mục có đánh số; tín hiệu này chắc hơn hẳn suy
  đoán từ hình thức chữ, nhất là với tài liệu không dùng in đậm cho tiêu đề).
- Độ dài cả dòng > 250 ký tự → loại.
- `letter | roman` mà không in đậm → loại (rất phổ biến trong danh sách liệt kê,
  nhận bừa sẽ băm nội dung thành hàng loạt mục vụn).
- `decimal` mà không in đậm:
  - Có dấu hai chấm ngăn tên/nội dung → đo **phần trước dấu hai chấm**; dài hơn
    60 ký tự thì loại. (Đo cả dòng thì mọi tiêu đề kiểu "1.9 Hợp đồng bảo hiểm:
    là tất cả văn bản…" đều bị loại, mất luôn cả nhánh con của nó.)
  - Không có dấu hai chấm → cả dòng là tên mục; dài hơn **160 ký tự** mới loại.
    Tên mục dài là chuyện thường trong văn bản pháp lý.
  - Tên bắt đầu bằng chữ thường → loại.

Dấu hai chấm dùng để ngăn tên/nội dung phải khớp `(?<!\d):(?!\d)` — bỏ qua dấu
giữa hai chữ số (`8:30`, `1:2` là giờ giấc/tỉ lệ).

### 3.5 Kiểm tra tính liên tục của dãy số

Chỉ áp dụng cho chỉ mục **đoán từ hình thức** (Word-sinh thì luôn giữ).

Một câu mở đầu bằng số ("30 (ba mươi) ngày tuổi đến 70 tuổi…") trông y hệt đề mục
sau khi PDF cắt dòng. Loại nó bằng hai luật:

- **Số đệm 0 đứng đầu** (`(^|\.)0\d`) → không phải chỉ mục. "04 (bốn) Năm hợp
  đồng đầu tiên" là số lượng; chỉ mục không bao giờ đệm 0.
- **Phải nối tiếp được dãy đang mở.** Với `num` hiện tại và `prev` liền trước
  (dạng tuple số, `2.3.1.` → `(2,3,1)`):
  - Cùng nhánh (`num[:d-1] == prev[:d-1]`, `d = len(num) ≤ len(prev)`): hợp lệ khi
    `num[-1] == 1` (mở lại dãy) hoặc `prev[d-1] < num[-1] ≤ prev[d-1] + 2`
    (chừa chỗ cho một mục bị trích xuất sót).
  - Mục con đầu tiên của nhánh khác (`num[-1] == 1`, `2 ≤ d ≤ len(prev)+1`):
    hợp lệ khi nhánh cha của nó cũng nối tiếp được (xét đệ quy) — ví dụ `2.2.5`
    rồi tới `2.3.1` khi tiêu đề `2.3` bị sót.
  - Còn lại → loại.
- Gặp đề mục loại khác (PHẦN/ĐIỀU/Phụ lục…) thì **reset** `prev = None`: nó mở
  một dãy mới.

### 3.6 Banner — tiêu đề lớn không đánh số

Phải hội đủ **cả ba** dấu hiệu (câu văn thường không bao giờ đủ cả ba, nên luật
này gần như không bắt nhầm — điều quan trọng, vì banner nhận nhầm sẽ nuốt trọn
phần tài liệu đứng sau):

1. Cỡ chữ **lớn hơn hẳn** cỡ chữ thân bài (`body_size` = cỡ chữ chiếm nhiều ký tự
   nhất trong các đoạn `para` không thuộc bảng).
2. ≥80% chữ cái là chữ hoa.
3. Không mang chỉ mục nào (không khớp bất kỳ mẫu đề mục nào ở §3.2).

Thêm các chốt chặn:
- Độ dài 3–120 ký tự; tỉ lệ chữ cái/tổng ký tự ≥ 0.5.
- Phải có ≥2 từ dài ≥2 ký tự và toàn chữ cái (công thức `L = M *T *R` cũng in cỡ
  lớn và toàn hoa).
- Không phải ô bảng, không phải figure.
- **Nối các dòng banner liền kề** cùng trang thành một tiêu đề ("QUY ĐỊNH" /
  "SẢN PHẨM TIẾT KIỆM LINH HOẠT" ở hai cỡ chữ khác nhau, bước nối dòng thường
  không nối vì khác cỡ). Chữ dồn lên dòng đầu, các dòng sau xoá text.
- **Chỉ nhận khi có ≥2 banner** trong tài liệu. Tài liệu chỉ có đúng một banner
  thì đó chính là tên tài liệu — vốn đã là gốc của cây; thêm một nhánh nữa là
  mọc thêm một cấp thừa trong khi cây chỉ sâu được 3 cấp.

---

## 4. Chuẩn hoá chỉ mục Word (DOCX)

Khi Word đánh số tự động, các số `1.`, `2.1.`, `a)` **KHÔNG nằm trong text** của
paragraph mà được Word sinh ra lúc hiển thị. Đọc bằng `python-docx` thuần sẽ mất
sạch chỉ mục → RAG không còn tín hiệu phân cấp. Bắt buộc dựng lại từ
`numbering.xml` theo quy tắc OOXML:

- Đọc `w:abstractNum` → `{ilvl: (numFmt, lvlText, start, lvlRestart, pStyle)}`;
  `w:num` → ánh xạ `numId → abstractNumId` và các `w:lvlOverride/w:startOverride`.
- Duyệt tài liệu **đúng thứ tự xuất hiện** — bộ đếm là trạng thái tích luỹ.
- Áp `startOverride` ở lần đầu `numId` đó xuất hiện.
- `numFmt` ∈ {bullet, none} → không có chỉ mục, **nhưng vẫn phải reset cấp con**
  để chỉ mục cấp dưới không bị trôi.
- Tăng bộ đếm cấp hiện tại, reset mọi cấp sâu hơn (trừ cấp có `lvlRestart=0`).
- Render `lvlText`: thay `%1..%9` bằng giá trị bộ đếm cấp tương ứng, format theo
  `numFmt` của **cấp đó** (`decimal`, `decimalZero`→`%02d`, `lowerLetter`,
  `upperLetter`, `lowerRoman`, `upperRoman`). Cấp cha chưa từng chạy thì dùng
  `start` của nó (không sinh số rỗng).

**Thứ bậc suy từ `numFmt`, không suy từ `ilvl`.** `ilvl` không đáng tin: tài liệu
thật hay khai `a) b) c)` cùng `ilvl` với `1. 2. 3.`, dùng nó thì cây bị phẳng ra —
mục con thành ngang hàng với mục cha. Công thức:

- `roman` trong fmt → `(RANK_ROMAN_UPPER nếu upper, else RANK_ROMAN) + (ilvl-1)`
- `letter` trong fmt → `(RANK_LETTER_UPPER nếu upper, else RANK_LETTER) + (ilvl-1)`
- còn lại → `RANK_DECIMAL + max(số dấu chấm trong chỉ mục, ilvl-1)`

Giữ **nguyên định dạng gốc** của chỉ mục Word (`1.`, `a)`, `1.1.`), không đoán lại.

Đề mục gõ tay (PHẦN, CHƯƠNG, PHỤ LỤC, MỤC, ĐIỀU) được **ưu tiên nhận diện trước**
chỉ mục Word, kể cả khi paragraph đó cũng nằm trong một danh sách đánh số.

---

## 5. Chuẩn hoá tên mục (title)

### 5.1 Cắt tại dấu hai chấm

Nhiều mục viết liền cả nội dung vào dòng tiêu đề ("Hợp đồng bảo hiểm: là tất cả
văn bản thể hiện sự thoả thuận giữa…"). **Tên mục là phần trước dấu hai chấm**;
phần sau là nội dung, đưa xuống thân bài.

Luật này chạy với **mọi độ dài**, không chỉ khi tiêu đề vượt trần: một câu dài
80 ký tự vẫn là câu, đặt nguyên làm tên mục thì cây mục lục đọc như văn xuôi.

Nếu phần trước dấu hai chấm **dài hơn trần** → dấu ấy nằm giữa câu, không phải
ranh giới tên → bỏ qua, chuyển sang §5.3.

### 5.2 Hàng bảng đã làm phẳng

Dòng dạng `STT: 3.1 | Nội dung: … | Cụ thể: …` luôn mở đầu bằng cột số thứ tự.
Cắt tại dấu hai chấm đầu tiên như tiêu đề thường sẽ lấy đúng **tên cột** làm tên
mục — cả cây biến thành một dãy "STT" giống hệt nhau.

Quy tắc:
- Tách theo `\s*\|\s*`; cần ≥2 cột và ít nhất một cột có nhãn (`nhãn: giá trị`).
- Cột mất nhãn thì cả ô là giá trị (bước làm phẳng có lúc rơi mất nhãn).
- Bỏ cột rỗng và cột chỉ chứa số thứ tự (`^[\d.,/()\-–\s]*$`).
- Tên mục = giá trị đầu tiên **đủ ngắn** (≤ trần); nếu không có thì lấy giá trị
  đầu tiên. (Cột nội dung dài là thân bài, chỉ dùng khi không còn gì khác.)
- Mọi cột đều rỗng (hàng nối tiếp của ô trải dài) → **tên rỗng**, để mục hiện ra
  bằng đúng số của nó. Tuyệt đối không lấy đại tên cột làm tên mục.

**Hàng nối tiếp chỉ còn một cột** (`3.1.9 Quy định: năm/lần ĐVKD thực hiện…`):
gom tập tên cột từ *tất cả* các hàng bảng phẳng trong tài liệu; nếu nhãn đứng
đầu dòng nằm trong tập đó thì gỡ nhãn, lấy phần còn lại làm tên nếu ≤60 ký tự,
dài hơn thì tên rỗng. (Nhìn riêng dòng ấy thì "Quy định" trông y hệt tên mục;
chỉ khi đối chiếu cả tài liệu mới biết đó là tên cột.)

### 5.3 Cắt theo độ dài

Trần độ dài:

| Hằng | Giá trị | Dùng cho |
|---|---|---|
| `MAX_TITLE_IN_PATH` | 90 | tên mục khi đưa vào đường dẫn mục lục |
| `MAX_TITLE_IN_HEADING` | 200 | tên mục khi **in ra** dòng tiêu đề |
| `MAX_NAME_IN_HEADING` | 60 | trần của "tên mục" khi kiểm tra ứng viên decimal |
| `MAX_PLAIN_HEADING_LEN` | 160 | trần dòng không có dấu hai chấm |
| `MAX_HEADING_LEN` | 250 | trần tuyệt đối của một dòng tiêu đề |
| `MAX_BANNER_LEN` | 120 | trần của banner |

Hai trần 90 / 200 phải tách bạch: tiền tố mục lục phải nhường chỗ token cho nội
dung nên buộc cắt ở 90, nhưng dòng tiêu đề in trên trang không có ràng buộc đó —
cắt nó ở 90 là đứt ngang cụm từ ("…làm nguyên" / "liệu sản xuất") và mô hình đọc
ra một tên mục thiếu chữ.

Thuật toán cắt khi vượt trần `limit`:
1. Lấy `limit` ký tự đầu.
2. Tìm vị trí cuối cùng của `". "`, `", "`, `"; "` — nếu ≥ `limit//2` thì cắt ở đó,
   trả về (không thêm dấu `…`).
3. Ngược lại cắt ở khoảng trắng cuối cùng nếu ≥ `limit//2`, else cắt cứng, rồi
   thêm `…`.

### 5.4 Tên rỗng là kết luận, không phải chỗ thiếu

`Phụ lục 01` không có phần tên riêng. Nếu lấy `block.text` bù vào thì đường dẫn
thành "Phụ lục 01 Phụ lục 01". Quy tắc gán tên cuối cùng:

- Có tên (sau khi gỡ tên cột) → dùng tên đó.
- Không có tên **và** (đã từng có tên gốc, hoặc cả dòng chính bằng số mục) →
  tên rỗng.
- Còn lại → lấy nguyên `block.text`.

---

## 6. Dựng cây mục lục

- **Nén rank thành cấp liên tiếp**: gom tập rank *thực sự xuất hiện* trong tài
  liệu, sắp tăng dần, ánh xạ thành `1, 2, 3…`. Tài liệu không dùng PHẦN/CHƯƠNG
  thì `1.` phải là cấp 1, không phải cấp 5.
- **Cây sâu tối đa 3 cấp.** Từ cấp 4 (`1.1.1.1`) trở xuống, đề mục không còn là
  nhánh mà chỉ là nội dung của mục cha: chia nhỏ tới đó thì mỗi nút còn một hai
  câu, vector gần như không mang thông tin, mà title của chunk lại dài thêm một
  cấp vô ích.
- **Hạ cấp (demote)** một đề mục vượt quá 3 cấp: `kind → "para"`, xoá
  `number`/`level`/meta, và **ghép số mục vào đầu text** nếu text chưa bắt đầu
  bằng số đó (DOCX giữ số ở `block.number` chứ không nằm trong text — bỏ đi thì
  tài liệu dựng lại mất hẳn phần đánh số, người đọc không đối chiếu được bản gốc).
- **Nội dung thuộc về tiêu đề gần nhất phía trên**, bất kể trang nào — đây là
  cách xử lý mục trải dài qua nhiều trang. Mọi tổ tiên đang mở đều phải mở rộng
  `page_end` để metadata nhất quán.
- **Phần mở đầu**: nội dung nằm trước tiêu đề đầu tiên (bìa, lời dẫn) gom vào một
  section tên `Phần mở đầu`, `number` rỗng. Nó **không phải mục cha** của bất kỳ
  mục nào — gặp tiêu đề đầu tiên thì xoá sạch stack. Không được vẽ nó thành tiêu
  đề khi xuất (tên do tool đặt, không có trong tài liệu).
- **Đường dẫn** `path = [heading_str của tổ tiên…] + [f"{number} {title}"]`,
  nối bằng `" > "`. `level` hiển thị = độ sâu thật trong cây (`len(stack)+1`),
  để cấp không nhảy cóc.

---

## 7. Gộp mục quá ngắn

Danh mục định nghĩa kiểu `1.1 … 1.60`, mỗi mục một hai dòng, cho ra hàng chục
chunk vài chục token — vector gần như không mang thông tin. Gộp lại thì mất nút
đó trong cây, **nhưng không mất chữ**: dòng tiêu đề được đưa xuống thành đoạn mở
đầu của phần nội dung.

Thuật toán:
- Lặp tới khi không còn cặp nào gộp được; **mỗi vòng chỉ gộp một cặp**: lấy mục
  ngắn nhất còn lại rồi ghép vào người hàng xóm **nhỏ hơn**.
- Xét **cả mục trên lẫn mục dưới**. Chỉ nhìn về phía trước thì một mục ngắn nằm
  ngay sau một mục đồ sộ sẽ mắc kẹt vĩnh viễn.
- Vòng lặp cho phép một mục cha vừa nuốt hết mục con được xét lại (gộp một lượt
  bỏ sót hẳn trường hợp này).

Điều kiện gộp:
- Mục bị nuốt (mục đứng **sau**) phải là mục **không còn mục con** — gộp là xoá
  mất một nhánh.
- Hai mục phải cùng nhánh: cùng mục cha, hoặc quan hệ cha–con. Gộp hai nhánh
  khác nhau là trộn hai chủ đề vào một vector.
- `is_preamble` và `is_banner` **không bao giờ** tham gia gộp. Banner chỉ là vách
  ngăn, không phải chủ đề; dồn nội dung của một mục có tên tuổi vào một nút không
  có chỉ mục để tra cứu là sai.
- Tổng token sau gộp ≤ `max_tokens − token của tiền tố mục lục`. Bỏ quên tiền tố
  thì mục gộp xong vẫn bị khâu chunk cắt đôi — gộp thành công cốc, lại đẻ ra hai
  chunk cùng tên.

Chuẩn hoá chỉ mục sau khi gộp (**chỉ khi gộp hai mục ngang hàng**; mục con gộp
lên cha thì số của cha vốn đã bao mục con):
- `number` = các số nối bằng `" + "`, **giữ đủ** — đó mới là chỉ mục để tra cứu.
  Giữ nguyên số cũ là để cây nói dối: chunk mang title `1.1 Mục đích` nhưng bên
  trong có cả `1.2`.
- `title` = các tên nối bằng `" + "`, khử trùng lặp, **cắt bớt khi vượt trần**
  (90 cho path, 200 cho dòng in), phần bị cắt thay bằng `…`.
- Cập nhật `path[-1]` theo `heading_str` mới.

Khi dồn nội dung: nếu dòng tiêu đề của mục bị nuốt là nửa đầu của câu (theo luật
§2.1) thì **nối liền** với khối đầu tiên, không để vỡ làm đôi; ngược lại chèn nó
thành một block riêng (`bold=True`) ở đầu.

---

## 8. Checklist tự kiểm

Sau khi implement, tài liệu đầu ra phải qua hết các kiểm tra sau:

1. Không có ký tự nào trong vùng U+E000–U+F8FF, không có U+200B/FEFF/00AD.
2. Không có chuỗi `$`…`$` hay ký tự thay thế `�` trong text.
3. Mọi `section.level ≤ 3`; không có cấp nhảy cóc (level tăng nhiều hơn 1).
4. Dãy số của các mục cùng cha phải tăng dần, không có mục nào mang số đệm 0.
5. Không có section nào có `title` dài hơn trần tương ứng.
6. Không có section nào có `title` bằng đúng tên một cột bảng (`STT`, `Nội dung`,
   `Quy định`…) lặp lại nhiều lần.
7. Tổng số ký tự của text đầu ra ≈ tổng số ký tự trích xuất được (sai lệch chỉ do
   khoảng trắng) — chứng minh không mất chữ.
8. Với DOCX có `numbering.xml`: mọi paragraph thuộc danh sách đánh số phải có
   `number` khác rỗng.
9. Câu dẫn chiếu ("theo Điều 5…", "quy định tại Phần 1 này") không được xuất hiện
   như một section.
