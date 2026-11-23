"""Cấu hình bot — nạp từ biến môi trường (.env)."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _parse_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.replace(" ", "").split(","):
        if part:
            ids.add(int(part))
    return ids


# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = _parse_ids(os.getenv("ADMIN_IDS", ""))

# --- SePay ---
# QR chuyển khoản trực tiếp. Giữ fallback tên MB_* để dùng lại cấu hình cũ.
BANK_ACCOUNT_NO = os.getenv("BANK_ACCOUNT_NO", "") or os.getenv("MB_ACCOUNT_NO", "")
BANK_ACCOUNT_NAME = os.getenv("BANK_ACCOUNT_NAME", "") or os.getenv("MB_ACCOUNT_NAME", "")
BANK_CODE = os.getenv("BANK_CODE", "") or os.getenv("MB_BANK_CODE", "")
SEPAY_WEBHOOK_SECRET = os.getenv("SEPAY_WEBHOOK_SECRET", "")

# Cổng thanh toán cũ được giữ để IPN của các đơn đã tạo trước khi nâng cấp
# vẫn có thể được xử lý.
SEPAY_MERCHANT_ID = os.getenv("SEPAY_MERCHANT_ID", "")
SEPAY_SECRET_KEY = os.getenv("SEPAY_SECRET_KEY", "")
# Có thể đặt riêng secret cho IPN. Nếu bỏ trống, dùng chung SECRET KEY merchant.
SEPAY_IPN_SECRET = os.getenv("SEPAY_IPN_SECRET", "") or SEPAY_SECRET_KEY
SEPAY_CHECKOUT_URL = os.getenv(
    "SEPAY_CHECKOUT_URL", "https://pay.sepay.vn/v1/checkout/init",
)
SEPAY_PUBLIC_BASE_URL = os.getenv("SEPAY_PUBLIC_BASE_URL", "").rstrip("/")
SEPAY_PAYMENT_METHOD = os.getenv("SEPAY_PAYMENT_METHOD", "BANK_TRANSFER")

# HTTP server nội bộ nhận IPN/callback. Nên đặt sau reverse proxy HTTPS.
PAYMENT_SERVER_HOST = os.getenv("PAYMENT_SERVER_HOST", "127.0.0.1")
PAYMENT_SERVER_PORT = int(os.getenv("PAYMENT_SERVER_PORT", "8080"))

# --- Vận hành ---
ORDER_TIMEOUT_MINUTES = int(os.getenv("ORDER_TIMEOUT_MINUTES", "15"))
DB_PATH = os.getenv("DB_PATH", "jollibee.db")

# --- Sản phẩm ---
# Các mệnh giá voucher Jollibee bán trên bot.
DENOMINATIONS = [100_000, 200_000]
# Giá bán mặc định khi khởi tạo DB lần đầu (admin có thể chỉnh bằng /gia).
DEFAULT_PRICES = {100_000: 75_000, 200_000: 150_000}


def validate() -> list[str]:
    """Trả về danh sách lỗi cấu hình (rỗng nếu hợp lệ)."""
    errors: list[str] = []
    if not BOT_TOKEN:
        errors.append("Thiếu BOT_TOKEN")
    if not ADMIN_IDS:
        errors.append("Thiếu ADMIN_IDS")
    if not BANK_ACCOUNT_NO:
        errors.append("Thiếu BANK_ACCOUNT_NO hoặc MB_ACCOUNT_NO")
    if not BANK_ACCOUNT_NAME:
        errors.append("Thiếu BANK_ACCOUNT_NAME hoặc MB_ACCOUNT_NAME")
    if not BANK_CODE:
        errors.append("Thiếu BANK_CODE hoặc MB_BANK_CODE")
    if not SEPAY_WEBHOOK_SECRET:
        errors.append("Thiếu SEPAY_WEBHOOK_SECRET")
    if not SEPAY_PUBLIC_BASE_URL:
        errors.append("Thiếu SEPAY_PUBLIC_BASE_URL")
    elif not SEPAY_PUBLIC_BASE_URL.startswith("https://"):
        errors.append("SEPAY_PUBLIC_BASE_URL phải bắt đầu bằng https://")
    if SEPAY_PAYMENT_METHOD not in {"CARD", "BANK_TRANSFER", "NAPAS_BANK_TRANSFER"}:
        errors.append("SEPAY_PAYMENT_METHOD không hợp lệ")
    return errors
