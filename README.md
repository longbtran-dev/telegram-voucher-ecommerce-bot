# Bot Telegram bán voucher Jollibee tự động

> 🤖 **Trải nghiệm Bot trực tiếp:** [@jollibee_dylan_bot](https://t.me/jollibee_dylan_bot)

Bot bán mã voucher Jollibee (UrBox) qua Telegram, hỗ trợ thanh toán bằng ví hoặc
QR SePay và tự động giao mã sau khi xác nhận thanh toán.

## Tính năng

**Phía khách** (menu nút sau khi `/start`):

- 🛒 **Mua hàng — giỏ hàng đa mệnh giá**: chỉnh số lượng từng loại (100k/200k)
  bằng nút ➕/➖ ngay trên 1 tin nhắn, mua nhiều loại trong 1 đơn, rồi chọn
  **thanh toán bằng ví** hoặc **ảnh QR SePay có sẵn số tiền và mã đơn**. Có nút
  **❌ Huỷ đơn & đặt lại** khi muốn đổi số lượng.
- 👛 **Ví khách hàng** (`/vi`): xem số dư và lịch sử giao dịch, tạo QR nạp tiền
  với nội dung `NAP...`; Bank Webhook SePay tự cộng đúng một lần khi nhận đủ tiền.
- 📖 **Hướng dẫn sử dụng**: chọn "dùng trực tiếp tại quầy" hoặc "đặt qua app",
  kèm mẹo dùng nhiều voucher trên 1 hóa đơn.
- ☎️ **Hỗ trợ**: hiện số điện thoại + Telegram của shop.
- 🧾 **Lịch sử giao dịch**: nhận file HTML chứa mã đã mua, nút copy và tổng
  chi tiêu (lệnh `/donhang` cũng ra kết quả này); không hiển thị link UrBox.
- 🔔 **Báo khi có hàng**: loại nào hết thì khách bấm chuông đăng ký, bot tự
  nhắn khi admin nhập kho.
- Sau khi giao mã, tin cuối có nút **📖 Cách sử dụng** / **🛒 Mua thêm**.

**Phía vận hành**:

- **Bảng điều khiển admin bằng nút**: kho, nhập mã, công cụ check nhanh/file/toàn
  kho, quản lý proxy (xem nguyên thông tin, thêm, xóa, test live), báo cáo,
  lịch sử, ví khách hàng, giá bán, cấu hình và
  trạng thái SePay.
- **Quản trị ví**: tìm khách bằng Telegram ID, xem toàn bộ ledger, cộng/trừ hoặc
  đặt số dư kèm lý do; mọi thay đổi được lưu lịch sử và thông báo cho khách.
- **Tự động giao mã** ngay khi SePay gửi Bank Webhook đúng tài khoản, mã đơn và số tiền;
  báo thanh toán thành công ngay, hiển thị tiến trình kiểm tra, rồi xóa tiến trình
  trước khi giao mã. Mã hỏng tự đổi mã khác và báo admin.
- **Cooldown route**: IP VPS hoặc proxy nào bị Jollibee rate-limit sẽ tự bị bỏ qua
  trong 30 phút; admin có thể kiểm tra trạng thái live của từng route.
- **Giữ mã (reserve)** khi tạo đơn, tránh bán trùng; **tự huỷ đơn** sau 15 phút
  nếu chưa trả tiền, mã trả về kho.
- **Nhập kho qua bot**: admin gửi file `.txt` chứa mã voucher;
  không check lúc nhập, bắt buộc check khả dụng ngay trước khi giao khách.
- **Báo cáo**: `/lichsu` xuất file HTML (đơn, doanh thu, số mã đã bán từng loại);
  `/baocao` doanh thu hôm nay/tháng; tự gửi báo cáo ngày lúc 22h30.
- Giá bán khác mệnh giá (mặc định 100k→75k, 200k→150k), admin chỉnh được.
- Tuỳ chỉnh **tiêu đề gian hàng**, **link hiển thị**, **liên hệ hỗ trợ**.

## Cấu trúc

```
jollibee_bot/
├── bot.py               # điểm khởi động, chạy bot + 3 tác vụ nền
├── config.py            # nạp cấu hình từ .env
├── database.py          # SQLite: kho, đơn hàng, ví/ledger, chờ hàng, cấu hình
├── checker.py           # guest cart, proxy fallback và phân loại mã trước khi giao
├── payments.py          # SePay webhook đơn/nạp ví + hết hạn đơn + báo cáo ngày
├── sepay.py             # tương thích checkout/IPN của các đơn cũ
├── report.py            # dựng file HTML: lịch sử bán (admin) & lịch sử mua (khách)
├── utils.py             # sinh mã đơn và định dạng dữ liệu
├── handlers_customer.py # menu, giỏ hàng, hướng dẫn, hỗ trợ, lịch sử của khách
└── handlers_admin.py    # lệnh admin
tests/
└── test_smoke.py        # kiểm tra nhanh DB + render HTML (python tests/test_smoke.py)
logic_check_jollibee_code/
├── worker/              # lõi UrBox + guest cart Jollibee được bot sử dụng
└── tests/               # test parser và API checker
Dockerfile
docker-compose.yml
```

## Cài đặt (local / test)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate  |  Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # rồi điền thông tin thật vào .env
python -m jollibee_bot
```

Chạy kiểm thử:

```bash
pip install -r requirements-dev.txt
python tests/test_smoke.py
python -m pytest logic_check_jollibee_code/tests/test_jollibee.py \
  logic_check_jollibee_code/tests/test_urbox.py -q
```

> Bot mở một HTTP server nội bộ để nhận webhook/IPN SePay. Khi chạy thật,
> cần HTTPS công khai bằng domain hoặc certificate cho IP VPS, rồi reverse proxy
> qua Nginx tới cổng `PAYMENT_SERVER_PORT`.

### Lấy thông tin cấu hình

- **BOT_TOKEN**: nhắn [@BotFather](https://t.me/BotFather) → `/newbot`.
- **ADMIN_IDS**: nhắn [@userinfobot](https://t.me/userinfobot) để lấy ID Telegram của bạn.
- **BANK_ACCOUNT_NO / BANK_ACCOUNT_NAME / BANK_CODE**: tài khoản nhận tiền đã liên kết SePay.
- **SEPAY_WEBHOOK_SECRET**: chuỗi bí mật dùng chung giữa bot và Bank Webhook SePay.
- **SEPAY_PUBLIC_BASE_URL**: domain HTTPS hoặc IP VPS có certificate hợp lệ,
  ví dụ `https://pay.example.com` hoặc `https://203.0.113.10`.
- **JOLLIBEE_PROXY_FILE**: file proxy fallback; mỗi dòng dùng dạng
  `host:port`, `host:port:user:pass` hoặc URL proxy đầy đủ.
- **VOUCHER_CHECK_INTERVAL / JOLLIBEE_ROUTE_COOLDOWN_SECONDS / JOLLIBEE_APP_VERSION**:
  nhịp request, thời gian cooldown route và header phiên bản app của API Jollibee;
  mặc định lần lượt `1.5` giây, `1800` giây và `4.09`.
- Trên SePay, tạo Bank Webhook nhận giao dịch vào tại
  `https://pay.example.com/payments/sepay/webhook`, xác thực `HMAC-SHA256`.

## Sử dụng (lệnh admin)

| Lệnh | Chức năng |
|------|-----------|
| `/admin` | Xem danh sách lệnh |
| `/vi` | Khách xem số dư, lịch sử và nạp tiền vào ví |
| `/nhapkho` | Nhập mã: gửi file `.txt` (mỗi dòng 1 mã), bot tự phân loại 100k/200k |
| `/kho` | Xem tồn kho |
| `/lichsu [n]` | Xuất file HTML lịch sử bán hàng (mặc định 30 đơn gần nhất) |
| `/baocao` | Báo cáo doanh thu hôm nay / tháng này |
| `/gia` | Xem giá; `/gia 100 80000` để đổi giá |
| `/tieude <text>` | Đổi tiêu đề gian hàng |
| `/link <url>` | Đổi link hiển thị (bỏ trống để xoá) |
| `/hotro <@user>` | Đổi liên hệ hỗ trợ hiển thị cho khách |
| `/sdt <số>` | Đổi số điện thoại hỗ trợ hiển thị cho khách |
| `/xacnhan <mã đơn>` | Giao thủ công 1 đơn (khi khách chuyển sai nội dung) |
| `/thongbao` | Gửi thông báo tới tất cả khách đã dùng bot (có xem trước) |
| `/trangthai` | Trạng thái theo dõi thanh toán |

Khách hàng chỉ cần gõ `/start` — sau đó có **bàn phím cố định** dưới ô chat
(🛒 Mua hàng · 👛 Ví của tôi · 🧾 Đơn của tôi · 📖 Hướng dẫn · ☎️ Hỗ trợ) nên
mọi thao tác đều bằng nút bấm, không cần nhớ lệnh.

## Triển khai 24/7 trên VPS (Ubuntu)

Bot **phải chạy nền liên tục** (không dùng shared hosting/cPanel). VPS cần HTTPS
công khai bằng domain hoặc certificate cho IP để SePay gọi webhook.

SQLite nằm tại `./data/jollibee.db`; Docker Compose mount cả thư mục `./data`
vào `/app/data` để lưu bền file database, WAL và SHM.

```bash
# Sau khi cài Docker Engine + Compose plugin
cd /opt/jollibee-bot
cp .env.example .env
nano .env
mkdir -p data
touch proxy.txt
docker compose up -d --build
docker compose ps
docker compose logs -f bot
```

### Reverse proxy HTTPS cho SePay

Đặt Caddy hoặc Nginx phía trước cổng nội bộ `8080`. Ví dụ Nginx (thay
`pay.example.com` bằng domain thật):

```nginx
server {
    listen 443 ssl http2;
    server_name pay.example.com;

    ssl_certificate /etc/letsencrypt/live/pay.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pay.example.com/privkey.pem;

    location /payments/sepay/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Sau khi cấu hình, kiểm tra
`https://pay.example.com/payments/sepay/health` trả về `{"ok": true, ...}`.

## Lưu ý quan trọng

- Bot chỉ giao voucher khi IPN có đúng `X-Secret-Key`, trạng thái thành công,
  tiền tệ VND, đúng mã đơn và đúng số tiền.
- Callback trình duyệt `success/error/cancel` chỉ dùng để hiển thị kết quả;
  **không** được dùng làm căn cứ giao voucher. IPN mới là nguồn xác nhận.
- Nếu IPN đến sau khi đơn đã hết hạn/khách đã huỷ, bot tự cấp lại voucher từ kho.
  Nếu kho không đủ, đơn chuyển sang `paid_pending` và báo khẩn cho admin.
- Có thể dùng `/xacnhan <mã đơn>` để xử lý thủ công đơn `pending` hoặc `paid_pending`.
- Số dư ví được tính từ ledger giao dịch; bot không cho admin trừ xuống số âm và
  chống cộng/trừ lặp cho webhook nạp tiền hoặc thanh toán đơn.
- File `.env`, `proxy.txt`, `*.db`, `*.pem` đã được `.gitignore` — **không commit** thông tin nhạy cảm.
