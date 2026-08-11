# Vutech Price Checker — App theo dõi giá đối thủ (Lớp 1, 0 token)

## Liên kết nhanh

- **Dashboard xem giá (live):** https://phamlevu1991-spec.github.io/vutech-price-checker/
- **Chạy tay ngay (không đợi lịch):** https://github.com/phamlevu1991-spec/vutech-price-checker/actions/workflows/check-prices.yml → nút "Run workflow"
- **Google Sheet nguồn:** https://docs.google.com/spreadsheets/d/1nbSgLMnXfVAft1mU1MCYiAH0B1LPFPuPy5kXtFcP3YU (xem tab "Theo dõi giá đối thủ (Auto)")

App độc lập, chạy tự động hàng ngày qua GitHub Actions, **không dùng Claude/AI**
— refresh giá đối thủ cho các SKU đã có sẵn link tham khảo trong Google Sheet
"Khởi nghiệp" / tab **Website**, tính lại lợi nhuận + xuất dashboard.

## App này làm được gì

- Đọc danh sách SKU + link đối thủ đã lưu (cột M, tab Website).
- Tải lại từng trang, lấy giá hiện tại (ưu tiên JSON-LD schema.org → meta tag
  → microdata → fallback selector, xem chi tiết trong `check_prices.py`).
- Tính lại công thức giá Vutech (mốc 1 triệu: <1tr trừ 1.000đ, ≥1tr trừ 10.000đ)
  và lợi nhuận thực (Giá bán − Giá nhập − 35.000đ ship).
- Gắn cờ cảnh báo: giá bán ≤ giá vốn, lỗ sau ship, biên mỏng, Vutech đang cao
  hơn đối thủ (mất cạnh tranh), đối thủ hết hàng (giá không đáng tin), lỗi lấy
  giá tự động (cần Vũ kiểm tra tay)...
- Ghi kết quả vào 1 **tab MỚI** "Theo dõi giá đối thủ (Auto)" trên chính Sheet
  đó — **không đụng/ghi đè** tab Website gốc.
- Xuất `docs/index.html` — dashboard dark theme, search + sort + filter, xem
  qua GitHub Pages bất cứ lúc nào.
- Chạy theo lịch cron hàng ngày (mặc định 08:01 sáng giờ VN), hoặc bấm chạy
  tay bất cứ lúc nào qua tab Actions.

## App này KHÔNG làm được (vẫn cần Claude + skill `vutech-1-danh-gia-dinh-gia`)

- **Không tự tìm giá cho SKU mới** chưa có link trong cột M — việc đó cần
  phân biệt đúng biến thể/tên model theo thị trường VN, nhận diện giá mồi câu
  traffic... đòi hỏi phán đoán ngôn ngữ tự nhiên.
- **Không tự sửa giá bán** trên Haravan hay trên Sheet — chỉ báo cáo lệch giá,
  Vũ tự quyết định có đổi giá hay không.
- Nếu 1 site đối thủ đổi giao diện, script có thể lấy sai hoặc không lấy được
  giá — luôn xem cột "Trạng thái lấy giá" để biết dòng nào đáng tin.

## Setup lần đầu (làm 1 lần)

### 1. Tạo Google Service Account (để script tự đọc/ghi Google Sheet)

1. Vào [console.cloud.google.com](https://console.cloud.google.com) → tạo project mới (hoặc dùng project có sẵn).
2. Vào **APIs & Services → Library**, bật **Google Sheets API**.
3. Vào **APIs & Services → Credentials → Create Credentials → Service Account**.
   Đặt tên bất kỳ (vd `vutech-price-bot`), bỏ qua phần quyền role (không cần).
4. Mở service account vừa tạo → tab **Keys → Add Key → Create new key → JSON**.
   File JSON sẽ tự tải về máy — **giữ kín file này, không đăng công khai**.
5. Mở file JSON, copy giá trị `client_email` (dạng `...@...iam.gserviceaccount.com`).

### 2. Chia sẻ Google Sheet với service account

Mở Google Sheet "Khởi nghiệp" → nút **Share** → dán email `client_email` ở
bước trên vào → chọn quyền **Editor** → Gửi (bỏ tick "Notify people" cũng
được, đây là tài khoản máy không phải người thật).

### 3. Tạo repo GitHub và đẩy code này lên

```bash
cd vutech-price-checker
git init
git add .
git commit -m "Init: app theo dõi giá đối thủ Vutech"
git branch -M main
git remote add origin https://github.com/<tên-github-của-anh>/vutech-price-checker.git
git push -u origin main
```

(Có thể tạo repo Private — không ai khác cần thấy code này.)

### 4. Thêm GitHub Secrets

Vào repo trên GitHub → **Settings → Secrets and variables → Actions → New
repository secret**, thêm 2 secret:

| Tên secret | Giá trị |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Dán **toàn bộ nội dung** file JSON key ở bước 1 (mở file bằng Notepad, copy hết) |
| `SHEET_ID` | `1nbSgLMnXfVAftlmU1MCYiAH0B1LPFPuPy5kXtFcP3YU` (đã set sẵn làm mặc định trong code, thêm secret này chỉ cần nếu sau này đổi sang Sheet khác) |

### 5. Bật GitHub Pages để xem dashboard qua 1 đường link cố định

Vào **Settings → Pages** → phần Source chọn **Deploy from a branch** → branch
`main`, thư mục `/docs` → Save. Sau lần chạy đầu tiên, dashboard sẽ có tại:

```
https://phamlevu1991-spec.github.io/vutech-price-checker/
```

### 6. Chạy thử lần đầu (không cần đợi lịch)

Vào tab **Actions** trên GitHub → chọn workflow "Check giá đối thủ Vutech" ở
cột trái → nút **Run workflow** → Run. Xem log để biết bao nhiêu SKU OK, bao
nhiêu lỗi, theo từng domain đối thủ.

## Sau khi chạy — Vũ nên kiểm tra gì

1. Mở tab mới **"Theo dõi giá đối thủ (Auto)"** trên Sheet — cột "Cờ cảnh báo"
   ưu tiên xem dòng có 🔴 trước.
2. Cột "Trạng thái lấy giá" ghi `LỖI: ...` nghĩa là script không lấy được giá
   — không dùng dòng đó để quyết định đổi giá, tự kiểm tra tay.
3. Site nào lỗi lặp lại nhiều lần (xem log Action, phần "Theo domain") có thể
   cần thêm 1 selector riêng trong `extract_from_common_selectors()` — báo lại
   Claude kèm log lỗi để sửa nhanh.

## Bảo trì khi 1 site đối thủ đổi giao diện

Mở `check_prices.py`, tìm hàm `extract_from_common_selectors()` — thêm 1
dòng CSS selector mới phù hợp site đó vào danh sách `selectors`. Nếu không
chắc, đưa link lỗi + gửi lại đoạn HTML nguồn của trang đó cho Claude để thêm
selector chính xác.
