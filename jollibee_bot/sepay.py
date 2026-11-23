"""Tạo checkout form và chữ ký cho Cổng thanh toán SePay."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
from collections.abc import Mapping
from urllib.parse import quote

from . import config

SIGNATURE_FIELDS = (
    "order_amount",
    "merchant",
    "currency",
    "operation",
    "order_description",
    "order_invoice_number",
    "customer_id",
    "payment_method",
    "success_url",
    "error_url",
    "cancel_url",
)


def checkout_page_url(order_code: str) -> str:
    return f"{config.SEPAY_PUBLIC_BASE_URL}/payments/sepay/checkout/{quote(order_code)}"


def callback_url(result: str, order_code: str) -> str:
    return (
        f"{config.SEPAY_PUBLIC_BASE_URL}/payments/sepay/{result}"
        f"?order={quote(order_code)}"
    )


def build_checkout_fields(
    order_code: str,
    amount: int,
    description: str,
    customer_id: str = "",
) -> dict[str, str]:
    """Tạo field theo đúng thứ tự SePay dùng để ký checkout."""
    fields = {
        "order_amount": str(amount),
        "merchant": config.SEPAY_MERCHANT_ID,
        "currency": "VND",
        "operation": "PURCHASE",
        "order_description": description,
        "order_invoice_number": order_code,
    }
    if customer_id:
        fields["customer_id"] = customer_id
    if config.SEPAY_PAYMENT_METHOD:
        fields["payment_method"] = config.SEPAY_PAYMENT_METHOD
    fields["success_url"] = callback_url("success", order_code)
    fields["error_url"] = callback_url("error", order_code)
    fields["cancel_url"] = callback_url("cancel", order_code)
    fields["signature"] = sign_fields(fields, config.SEPAY_SECRET_KEY)
    return fields


def sign_fields(fields: Mapping[str, str], secret_key: str) -> str:
    """Ký HMAC-SHA256 rồi Base64 theo đặc tả checkout của SePay."""
    signed = [f"{name}={fields[name]}" for name in SIGNATURE_FIELDS if name in fields]
    digest = hmac.new(
        secret_key.encode("utf-8"),
        ",".join(signed).encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def render_checkout_form(fields: Mapping[str, str]) -> str:
    """Trang trung gian tự submit form POST sang SePay."""
    inputs = "\n".join(
        f'<input type="hidden" name="{html.escape(name, quote=True)}" '
        f'value="{html.escape(value, quote=True)}">'
        for name, value in fields.items()
    )
    action = html.escape(config.SEPAY_CHECKOUT_URL, quote=True)
    return f"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Đang chuyển đến SePay</title>
  <style>
    body {{ font-family: system-ui, sans-serif; display: grid; place-items: center;
      min-height: 100vh; margin: 0; background: #fff8ef; color: #2f241f; }}
    main {{ text-align: center; padding: 32px; }}
    button {{ border: 0; border-radius: 12px; padding: 14px 22px; cursor: pointer;
      background: #d71920; color: white; font-weight: 700; font-size: 16px; }}
  </style>
</head>
<body>
  <main>
    <h1>Đang mở cổng thanh toán SePay…</h1>
    <p>Nếu trang không tự chuyển, hãy bấm nút bên dưới.</p>
    <form id="sepay-checkout" action="{action}" method="post">
      {inputs}
      <button type="submit">Tiếp tục thanh toán</button>
    </form>
  </main>
  <script>document.getElementById('sepay-checkout').submit();</script>
</body>
</html>"""


def render_result_page(title: str, message: str) -> str:
    return f"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; display: grid; place-items: center;
      min-height: 100vh; margin: 0; background: #fff8ef; color: #2f241f; }}
    main {{ max-width: 520px; text-align: center; padding: 32px; }}
  </style>
</head>
<body><main><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></main></body>
</html>"""
