"""Tiện ích dùng chung: sinh mã đơn, chuẩn hoá chuỗi và định dạng dữ liệu."""

from __future__ import annotations

import datetime
import random
import unicodedata
from urllib.parse import urlencode

from . import config, database

# Múi giờ Việt Nam (UTC+7) để hiển thị thời gian đơn hàng.
VN_TZ = datetime.timezone(datetime.timedelta(hours=7))
_VN_TZ = VN_TZ  # bí danh nội bộ

# Bảng ký tự an toàn cho mã đơn: bỏ các ký tự dễ nhầm (0/O, 1/I).
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def format_vnd(amount: int) -> str:
    return f"{amount:,.0f}".replace(",", ".") + "đ"


def format_k(amount: int) -> str:
    """Nhãn ngắn gọn cho mệnh giá: 100000 -> '100k'."""
    if amount % 1000 == 0:
        return f"{amount // 1000}k"
    return str(amount)


def parse_vnd_amount(text: str, allow_zero: bool = False) -> int | None:
    digits = "".join(char for char in (text or "") if char.isdigit())
    if not digits:
        return None
    amount = int(digits)
    if amount < 1000 and amount > 0:
        amount *= 1000
    if amount == 0 and allow_zero:
        return 0
    return amount if amount > 0 else None


def items_label(lines: list[tuple[int, int, int]]) -> str:
    """Nhãn ngắn cho đơn nhiều mệnh giá: [(denom, qty, price)] -> '2 × 200k + 1 × 100k'."""
    return " + ".join(f"{q} × {format_k(d)}" for d, q, _ in lines if q > 0)


def _ascii_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    return "".join(char for char in normalized if not unicodedata.combining(char))


def build_sepay_qr_url(
    account_no: str,
    bank_code: str,
    amount: int,
    description: str,
    account_name: str = "",
) -> str:
    """Tạo URL ảnh QR VietQR động theo tài liệu SePay."""
    params = {
        "acc": account_no.strip(),
        "bank": bank_code.strip(),
        "amount": str(amount),
        "des": description.strip().upper(),
        "template": "compact",
        "download": "false",
        "showinfo": "true",
        "fullacc": "true",
    }
    if account_name.strip():
        params["holder"] = _ascii_text(account_name).upper().strip()
    return "https://vietqr.app/img?" + urlencode(params)


def classify_denom(code: str) -> int | None:
    """Đoán mệnh giá từ tiền tố mã: 'UB100...' -> 100000, 'UB200...' -> 200000.

    Trả về None nếu mã không khớp mệnh giá nào đã cấu hình.
    """
    up = (code or "").upper()
    # Ưu tiên tiền tố dài hơn để tránh nhầm (vd 'UB100' vs 'UB10').
    for denom in sorted(config.DENOMINATIONS, key=lambda d: -d):
        if up.startswith(f"UB{denom // 1000}"):
            return denom
    return None


def mono_table(headers: list[str], rows: list, aligns: str = "") -> str:
    """Dựng bảng monospace căn cột, bọc sẵn trong <pre> (chống xuống dòng lệch).

    aligns: chuỗi 'l'/'r' cho mỗi cột (mặc định cột đầu 'l', còn lại 'r').
    Dùng None trong rows để chèn 1 dòng gạch ngăn cách.
    """
    ncol = len(headers)
    if not aligns:
        aligns = "l" + "r" * (ncol - 1)
    widths = [len(h) for h in headers]
    for row in rows:
        if row is None:
            continue
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: list[str]) -> str:
        out = []
        for i, cell in enumerate(cells):
            if aligns[i] == "r":
                out.append(cell.rjust(widths[i]))
            else:
                out.append(cell.ljust(widths[i]))
        return "  ".join(out)

    sep = "  ".join("-" * w for w in widths)
    lines = [fmt(headers), sep]
    for row in rows:
        lines.append(sep if row is None else fmt(row))
    return "<pre>" + "\n".join(lines) + "</pre>"


def format_time(unix_ts: int) -> str:
    """Đổi unix timestamp -> 'HH:MM dd/mm/yyyy' theo giờ Việt Nam."""
    if not unix_ts:
        return "—"
    return datetime.datetime.fromtimestamp(unix_ts, _VN_TZ).strftime("%H:%M %d/%m/%Y")


def format_time_short(unix_ts: int) -> str:
    """Ngắn gọn cho bảng: 'dd/mm HH:MM' theo giờ Việt Nam."""
    if not unix_ts:
        return "—"
    return datetime.datetime.fromtimestamp(unix_ts, _VN_TZ).strftime("%d/%m %H:%M")


def format_hm(unix_ts: int) -> str:
    """Chỉ giờ: 'HH:MM' theo giờ Việt Nam."""
    if not unix_ts:
        return "—"
    return datetime.datetime.fromtimestamp(unix_ts, _VN_TZ).strftime("%H:%M")


def format_dmy(unix_ts: int) -> str:
    """Chỉ ngày: 'dd/mm/yyyy' theo giờ Việt Nam."""
    if not unix_ts:
        return "—"
    return datetime.datetime.fromtimestamp(unix_ts, _VN_TZ).strftime("%d/%m/%Y")


def buyer_label(username: str, name: str, chat_id: int) -> str:
    """Nhãn hiển thị người mua: ưu tiên @username, rồi tên, cuối cùng là chat_id."""
    if username:
        return f"@{username}"
    if name:
        return name
    return f"ID {chat_id}"


def generate_order_code() -> str:
    """Sinh mã đơn duy nhất dạng 'JB' + 6 ký tự (vd JB7K3M9Q)."""
    while True:
        code = "JB" + "".join(random.choices(_CODE_ALPHABET, k=6))
        if not database.order_code_exists(code):
            return code


def generate_wallet_topup_code() -> str:
    while True:
        code = "NAP" + "".join(random.choices(_CODE_ALPHABET, k=8))
        if not database.wallet_topup_code_exists(code):
            return code
