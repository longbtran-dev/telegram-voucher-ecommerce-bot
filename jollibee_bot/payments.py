"""Nhận thanh toán SePay, giao voucher và xử lý đơn hết hạn."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import html
import hmac
import json
import logging
import re
import time
from decimal import Decimal, InvalidOperation

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import web

from . import checker, config, database, report, sepay, utils

log = logging.getLogger("payments")

# Ngưỡng cảnh báo kho: còn ít hơn số này (mỗi mệnh giá) thì nhắc admin nhập thêm.
LOW_STOCK_THRESHOLD = 5
# Giờ gửi báo cáo doanh thu hằng ngày (giờ Việt Nam).
DAILY_REPORT_HOUR = 22
DAILY_REPORT_MINUTE = 30

_server_running = False
_last_ipn_at = 0
_last_server_error = ""
_ORDER_CODE_PATTERN = re.compile(r"JB[A-Z0-9]{6}")
_TOPUP_CODE_PATTERN = re.compile(r"NAP[A-Z0-9]{8}")
_WEBHOOK_MAX_AGE_SECONDS = 300
_MESSAGE_SEPARATOR = "━━━━━━━━━━━━━━━━━━"


def payment_gateway_status() -> tuple[bool, int, str]:
    return _server_running, _last_ipn_at, _last_server_error


def build_bank_webhook_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    """Tạo chữ ký HMAC-SHA256 theo định dạng webhook SePay."""
    signed_data = timestamp.encode("utf-8") + b"." + raw_body
    digest = hmac.new(secret.encode("utf-8"), signed_data, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def verify_bank_webhook_signature(
    secret: str,
    timestamp: str,
    raw_body: bytes,
    provided_signature: str,
    now: int | None = None,
) -> bool:
    """Xác thực chữ ký và chặn phát lại webhook quá 5 phút."""
    if not secret or not timestamp or not provided_signature:
        return False
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    current_time = int(time.time()) if now is None else now
    if abs(current_time - sent_at) > _WEBHOOK_MAX_AGE_SECONDS:
        return False
    expected = build_bank_webhook_signature(secret, timestamp, raw_body)
    return hmac.compare_digest(expected, provided_signature)


def extract_bank_order_code(payload: dict) -> str:
    """Tìm mã JB trong các trường nội dung giao dịch SePay."""
    for field in ("code", "content", "description"):
        match = _ORDER_CODE_PATTERN.search(str(payload.get(field) or "").upper())
        if match:
            return match.group(0)
    return ""


def extract_wallet_topup_code(payload: dict) -> str:
    for field in ("code", "content", "description"):
        match = _TOPUP_CODE_PATTERN.search(str(payload.get(field) or "").upper())
        if match:
            return match.group(0)
    return ""


def _normalized_account(value: object) -> str:
    return "".join(char for char in str(value or "") if char.isalnum()).upper()


def bank_transaction_matches_order(payload: dict, order: database.Order) -> bool:
    """Chỉ khớp tiền vào đúng tài khoản, đúng mã đơn và đúng số tiền."""
    return (
        str(payload.get("transferType") or "").lower() == "in"
        and _normalized_account(payload.get("accountNumber"))
        == _normalized_account(config.BANK_ACCOUNT_NO)
        and extract_bank_order_code(payload) == order.order_code
        and _amount_matches(payload.get("transferAmount"), order.total_amount)
    )


def bank_transaction_matches_wallet_topup(
    payload: dict, topup: database.WalletTopup,
) -> bool:
    return (
        str(payload.get("transferType") or "").lower() == "in"
        and _normalized_account(payload.get("accountNumber"))
        == _normalized_account(config.BANK_ACCOUNT_NO)
        and extract_wallet_topup_code(payload) == topup.topup_code
        and _amount_matches(payload.get("transferAmount"), topup.amount)
    )


def _delivery_product_label(order: database.Order) -> str:
    label = utils.items_label(database.get_order_lines(order.id))
    if not label and order.denom:
        label = f"{order.quantity} × {utils.format_k(order.denom)}"
    return label.replace("k", "K")


def _delivery_confirmation_text(
    order: database.Order,
    product_label: str | None = None,
) -> str:
    resolved_label = product_label or _delivery_product_label(order)
    return (
        "✅ <b>THANH TOÁN THÀNH CÔNG</b>\n"
        f"{_MESSAGE_SEPARATOR}\n"
        "<blockquote>"
        f"🧾 Đơn hàng: <code>{html.escape(order.order_code)}</code>\n"
        f"🎁 Sản phẩm: <b>{html.escape(resolved_label)}</b>\n"
        f"💰 Đã thanh toán: <b>{utils.format_vnd(order.total_amount)}</b>"
        "</blockquote>\n"
        f"{_MESSAGE_SEPARATOR}\n"
        "<i>Thanh toán đã được ghi nhận. Bạn không cần thanh toán lại.</i>"
    )


def _delivery_checking_text(order: database.Order, total: int) -> str:
    return (
        "⏳ <b>HỆ THỐNG ĐANG KIỂM TRA MÃ</b>\n"
        f"{_MESSAGE_SEPARATOR}\n"
        "<blockquote>"
        f"🧾 Đơn hàng: <code>{html.escape(order.order_code)}</code>\n"
        f"🔍 Đang kiểm tra: <b>{total:02d} voucher</b>\n"
        "Vui lòng đợi trong giây lát, bot sẽ tự động giao mã sau khi kiểm tra xong."
        "</blockquote>\n"
        f"{_MESSAGE_SEPARATOR}\n"
        "<i>Bạn có thể theo dõi trạng thái tại tin nhắn này.</i>"
    )


def _delivery_pending_text(order: database.Order, detail: str) -> str:
    return (
        "⚠️ <b>ĐƠN HÀNG ĐANG ĐƯỢC XỬ LÝ</b>\n"
        f"{_MESSAGE_SEPARATOR}\n"
        "<blockquote>"
        f"🧾 Đơn hàng: <code>{html.escape(order.order_code)}</code>\n"
        "✅ Thanh toán đã được ghi nhận.\n"
        f"⏳ {html.escape(detail)}"
        "</blockquote>\n"
        f"{_MESSAGE_SEPARATOR}\n"
        "<i>Shop đã nhận cảnh báo và sẽ xử lý; bạn không cần thanh toán lại.</i>"
    )


async def _update_delivery_progress(
    bot: Bot,
    order: database.Order,
    progress_message,
    detail: str,
) -> None:
    text = _delivery_pending_text(order, detail)
    message_id = getattr(progress_message, "message_id", 0)
    if message_id:
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=order.chat_id,
                message_id=message_id,
            )
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("Không cập nhật được tiến trình đơn %s: %s", order.order_code, exc)
    try:
        await bot.send_message(order.chat_id, text)
    except Exception as exc:  # noqa: BLE001
        log.warning("Không gửi được trạng thái chờ đơn %s: %s", order.order_code, exc)


def _delivery_voucher_text(
    index: int,
    total: int,
    code: str,
    denom: int,
    result,
) -> str:
    lines = [
        f"🎟 <b>VOUCHER {index:02d} / {total:02d}</b>",
        f"<i>Jollibee Gift Voucher • Mệnh giá {utils.format_vnd(denom)}</i>",
        _MESSAGE_SEPARATOR,
        (
            "<blockquote>"
            "📊 Trạng thái: <b>Đã kiểm tra — sử dụng được</b>\n"
            "🛡 Bảo hành: <b>24 giờ</b>"
            "</blockquote>"
        ),
        _MESSAGE_SEPARATOR,
        "🔑 <b>MÃ VOUCHER JOLLIBEE</b>",
        f"<blockquote><code>{html.escape(code)}</code></blockquote>",
        "📋 <i>Nhấn vào mã phía trên để sao chép.</i>",
        _MESSAGE_SEPARATOR,
    ]
    return "\n".join(lines)


def _delivery_complete_text(
    order: database.Order,
    total: int,
    support: str,
    phone: str,
) -> str:
    return (
        f"🎉 <b>ĐÃ GIAO ĐỦ {total:02d} / {total:02d} VOUCHER</b>\n"
        f"{_MESSAGE_SEPARATOR}\n"
        "<blockquote>"
        "⚠️ <b>LƯU Ý QUAN TRỌNG</b>\n\n"
        "• Hãy sử dụng voucher ngay sau khi mua.\n"
        "• Voucher được bảo hành trong vòng 24 giờ.\n"
        "• Nếu có lỗi, gửi ảnh màn hình kèm mã đơn "
        f"<code>{html.escape(order.order_code)}</code>."
        "</blockquote>\n\n"
        "☎️ <b>LIÊN HỆ HỖ TRỢ</b>\n"
        f"💬 Telegram: {html.escape(support)}\n"
        f"📞 Điện thoại: <code>{html.escape(phone)}</code>\n\n"
        "<i>Cảm ơn bạn đã mua hàng tại Jollibee Voucher Store ❤️</i>"
    )


async def deliver_order(
    bot: Bot,
    order: database.Order,
    tran_id: str,
    announce_payment: bool | None = None,
) -> bool:
    """Đánh dấu đơn đã thanh toán và gửi mã voucher cho khách. Chống giao 2 lần."""
    was_paid_pending = order.status == "paid_pending"
    if announce_payment is None:
        announce_payment = not was_paid_pending
    preparation = database.prepare_order_payment(order.id, tran_id)
    if preparation not in {"ready", "stock_shortage"}:
        return False

    if announce_payment:
        try:
            await bot.send_message(
                order.chat_id,
                _delivery_confirmation_text(order),
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Không gửi được xác nhận thanh toán đơn %s: %s", order.order_code, exc)

    if preparation == "stock_shortage":
        if announce_payment:
            try:
                await bot.send_message(
                    order.chat_id,
                    _delivery_pending_text(
                        order,
                        "Kho đang thiếu mã để giao tự động; shop sẽ bổ sung và giao sớm nhất.",
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Không gửi được trạng thái thiếu kho đơn %s: %s", order.order_code, exc)
        if not was_paid_pending or announce_payment:
            await notify_admins(
                bot,
                f"🚨 <b>ĐÃ NHẬN TIỀN NHƯNG THIẾU KHO</b>\n"
                f"Đơn <code>{order.order_code}</code> • {utils.format_vnd(order.total_amount)}\n"
                f"Giao dịch SePay: <code>{tran_id}</code>\n"
                "Nhập thêm mã rồi dùng /xacnhan để giao đơn.",
            )
        return False

    progress_message = None
    try:
        progress_message = await bot.send_message(
            order.chat_id,
            _delivery_checking_text(order, order.quantity),
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Không gửi được tiến trình kiểm tra đơn %s: %s", order.order_code, exc)

    final, verified = await _verify_and_swap(bot, order)
    if not verified:
        await _update_delivery_progress(
            bot,
            order,
            progress_message,
            "Một số mã chưa thể xác nhận sử dụng được; shop đang kiểm tra và thay mã.",
        )
        return False
    if not database.complete_order_delivery(order.id):
        await _update_delivery_progress(
            bot,
            order,
            progress_message,
            "Mã đã kiểm tra xong nhưng hệ thống chưa thể chốt giao tự động.",
        )
        await notify_admins(
            bot,
            f"🚨 Không thể chốt trạng thái giao đơn <code>{order.order_code}</code> "
            "sau khi đã kiểm tra mã. Vui lòng xử lý thủ công.",
        )
        return False

    progress_message_id = getattr(progress_message, "message_id", 0)
    if progress_message_id:
        try:
            await bot.delete_message(order.chat_id, progress_message_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Không xóa được tiến trình đơn %s: %s", order.order_code, exc)

    # --- Tin cho khách ---
    # Tin thanh toán đã gửi trước checker; sau khi xóa tiến trình mới giao từng mã.
    support = database.get_config("support_contact", "@YourSupportBot")
    phone = database.get_config("support_phone", "0900000000")
    total = len(final)
    label = _delivery_product_label(order)
    messages = list(
        _delivery_voucher_text(i, total, code, denom, res)
        for i, (code, _, denom, res) in enumerate(final, start=1)
    )
    messages.append(_delivery_complete_text(order, total, support, phone))

    # Tin cuối kèm nút dẫn khách xem cách dùng hoặc mua thêm (gửi tin mới,
    # không sửa tin chứa mã) — thời điểm khách vừa nhận mã là lúc cần nhất.
    after_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📖 Cách sử dụng", callback_data="new:guide"),
        InlineKeyboardButton(text="🛒 Mua thêm", callback_data="new:buy"),
    ]])
    for i, msg in enumerate(messages):
        try:
            await bot.send_message(
                order.chat_id, msg, disable_web_page_preview=True,
                reply_markup=after_kb if i == len(messages) - 1 else None,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Không gửi được mã cho đơn %s: %s", order.order_code, exc)

    try:
        await _send_order_barcode_document(
            bot,
            order.chat_id,
            order.order_code,
            [(code, denom) for code, _, denom, _ in final],
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Không gửi được file HTML barcode cho đơn %s: %s", order.order_code, exc)

    # --- Tin cho admin ---
    buyer = html.escape(
        utils.buyer_label(order.buyer_username, order.buyer_name, order.chat_id)
    )
    admin_lines = [
        "✅ <b>Thanh toán thành công!</b>",
        f"🧾 Đơn {order.order_code}",
        f"👤 Người mua: {buyer}",
        f"💵 Số tiền: {utils.format_vnd(order.total_amount)}",
        f"📦 {label}",
        f"🕒 {utils.format_time(int(time.time()))}",
        "━━━━━━━━━━━━━━━━━━",
        "Mã đã giao:",
    ]
    for i, (code, _, denom, res) in enumerate(final, start=1):
        badge, extra = _status_badge(res)
        admin_lines.append(
            f"{i}. <code>{code}</code> ({utils.format_k(denom)})  {badge}"
            + (f" • {extra}" if extra else "")
        )
    await notify_admins(bot, "\n".join(admin_lines))

    # Cảnh báo nếu kho của các mệnh giá trong đơn còn ít.
    for denom, _, _ in database.get_order_lines(order.id):
        await low_stock_alert(bot, denom)
    return True


async def _send_order_barcode_document(
    bot: Bot,
    chat_id: int,
    order_code: str,
    vouchers: list[tuple[str, int]],
) -> None:
    barcode_html = report.render_order_vouchers_barcode_html(order_code, vouchers)
    barcode_doc = BufferedInputFile(
        barcode_html.encode("utf-8"),
        filename=f"{order_code}.html",
    )
    await bot.send_document(
        chat_id,
        barcode_doc,
        caption=(
            f"🎫 <b>Giao diện mã vạch đơn <code>{html.escape(order_code)}</code></b>\n"
            "👆 Bấm mở file để hiển thị thẻ voucher & mã vạch (barcode) đưa nhân viên quét tại quầy."
        ),
    )


async def simulate_order_delivery(
    bot: Bot,
    chat_id: int,
    vouchers: list[tuple[str, int]],
    order_code: str = "JBTEST01",
    check_delay: float = 0.8,
) -> None:
    """Giả lập giao trọn bộ đơn hàng kèm file barcode .html cho Admin mà không trừ kho thật."""
    now = int(time.time())
    counts: dict[int, int] = {}
    for _, denom in vouchers:
        counts[denom] = counts.get(denom, 0) + 1
    lines = [
        (denom, qty, database.get_price(denom))
        for denom, qty in sorted(counts.items(), reverse=True)
    ]
    total_amount = sum(qty * unit_price for _, qty, unit_price in lines)
    single_denom = next(iter(counts)) if len(counts) == 1 else 0
    fake_order = database.Order(
        id=0,
        order_code=order_code,
        chat_id=chat_id,
        denom=single_denom,
        quantity=len(vouchers),
        unit_price=database.get_price(single_denom) if single_denom else 0,
        total_amount=total_amount,
        status="delivered",
        created_at=now,
        expires_at=now + 900,
        tran_id=f"TEST-{order_code}",
        delivered_at=now,
    )
    product_label = utils.items_label(lines).replace("k", "K")

    await bot.send_message(
        chat_id,
        _delivery_confirmation_text(fake_order, product_label=product_label),
        disable_web_page_preview=True,
    )
    progress_message = await bot.send_message(
        chat_id,
        _delivery_checking_text(fake_order, len(vouchers)),
        disable_web_page_preview=True,
    )
    if check_delay > 0:
        await asyncio.sleep(check_delay)
    progress_message_id = getattr(progress_message, "message_id", 0)
    if progress_message_id:
        try:
            await bot.delete_message(chat_id, progress_message_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Không xóa được tiến trình giả lập đơn %s: %s", order_code, exc)

    support = database.get_config("support_contact", "@YourSupportBot")
    phone = database.get_config("support_phone", "0900000000")
    total = len(vouchers)
    messages = [
        _delivery_voucher_text(i, total, code, denom, None)
        for i, (code, denom) in enumerate(vouchers, start=1)
    ]
    messages.append(_delivery_complete_text(fake_order, total, support, phone))
    after_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📖 Cách sử dụng", callback_data="new:guide"),
        InlineKeyboardButton(text="🛒 Mua thêm", callback_data="new:buy"),
    ]])
    for i, msg in enumerate(messages):
        await bot.send_message(
            chat_id,
            msg,
            disable_web_page_preview=True,
            reply_markup=after_kb if i == len(messages) - 1 else None,
        )
    await _send_order_barcode_document(bot, chat_id, order_code, vouchers)


async def _verify_and_swap(bot: Bot, order: database.Order):
    """Chỉ trả verified=True khi mọi mã áp thành công vào guest cart."""
    buyer = html.escape(
        utils.buyer_label(order.buyer_username, order.buyer_name, order.chat_id)
    )
    details = database.get_order_voucher_details(order.id)  # [(vid, code, link, denom)]
    swap_events: list[tuple[str, int, str, str]] = []

    def replace_bad(index: int, result: checker.CheckResult) -> str | None:
        vid, code, _, denom = details[index]
        replacement = database.replace_voucher_in_order(order.id, vid)
        new_code = ""
        if replacement is not None:
            new_id, new_code, new_link = replacement
            details[index] = (new_id, new_code, new_link, denom)
        swap_events.append((code, denom, checker.status_label(result.status), new_code))
        return new_code or None

    results = await asyncio.to_thread(
        checker.check_codes_with_replacements,
        [code for _, code, _, _ in details],
        replace_bad,
    )

    for bad_code, denom, reason, replacement_code in swap_events:
        replacement_text = (
            f"Mã thay thế: <code>{replacement_code}</code> — đã kiểm tra tiếp "
            "trên cùng guest cart."
            if replacement_code
            else "Không còn mã cùng mệnh giá để thay; đơn tiếp tục giữ ở trạng thái chờ."
        )
        await notify_admins(
            bot,
            f"❌ <b>MÃ LỖI ĐÃ LOẠI KHỎI KHO</b>\n"
            f"Đơn <code>{order.order_code}</code> — khách {buyer}\n"
            f"Mã lỗi: <code>{bad_code}</code> ({utils.format_vnd(denom)})\n"
            f"Nguyên nhân: <b>{reason}</b>\n"
            f"{replacement_text}",
        )

    final: list[tuple[str, str, int, object]] = []
    verified = bool(details) and len(results) == len(details)
    for (_, code, link, denom), res in zip(details, results):
        if res.status != "VALID":
            verified = False
            detail = checker.status_label(res.status)
            if res.jollibee_status and res.status == "ERROR":
                detail = checker.status_label(res.jollibee_status)
            await notify_admins(
                bot,
                f"🚨 <b>ĐƠN ĐÃ NHẬN TIỀN — CHƯA GIAO MÃ</b>\n"
                f"Đơn {order.order_code} — khách {buyer}\n"
                f"Mã <code>{code}</code> ({utils.format_vnd(denom)}): <b>{detail}</b>.\n"
                "Bot giữ đơn ở trạng thái chờ, không gửi mã chưa được xác nhận.",
            )
        final.append((code, link, denom, res))

    return final, verified


def _status_badge(res) -> tuple[str, str]:
    """Trả về (nhãn trạng thái, dòng phụ) từ kết quả checker cho 1 mã."""
    if res.status == "VALID":
        route = "qua proxy" if getattr(res, "proxy_used", False) else "IP chính"
        return "✅ Sử dụng được", route
    if res.status in checker.BAD_STATUSES:
        return f"❌ {checker.status_label(res.status)}", ""
    status = getattr(res, "jollibee_status", "") or res.status
    return f"❓ {checker.status_label(status)}", ""


def _html_response(content: str, status: int = 200) -> web.Response:
    return web.Response(text=content, status=status, content_type="text/html", charset="utf-8")


async def _checkout_handler(request: web.Request) -> web.Response:
    order_code = request.match_info["order_code"].strip().upper()
    order = database.get_order_by_code(order_code)
    if order is None:
        return _html_response(
            sepay.render_result_page("Không tìm thấy đơn", "Mã đơn không tồn tại."), 404,
        )
    if order.status == "delivered":
        return _html_response(
            sepay.render_result_page(
                "Đơn đã thanh toán", "Voucher đã được gửi về cuộc trò chuyện Telegram.",
            )
        )
    if order.status != "pending" or order.expires_at <= int(time.time()):
        return _html_response(
            sepay.render_result_page(
                "Đơn đã hết hạn", "Vui lòng quay lại Telegram và tạo đơn mới.",
            ),
            410,
        )

    label = utils.items_label(database.get_order_lines(order.id)) or f"Đơn {order_code}"
    fields = sepay.build_checkout_fields(
        order_code=order.order_code,
        amount=order.total_amount,
        description=f"Jollibee {order.order_code} - {label}",
        customer_id=str(order.chat_id),
    )
    return _html_response(sepay.render_checkout_form(fields))


def _amount_matches(value: object, expected: int) -> bool:
    try:
        return Decimal(str(value)) == Decimal(expected)
    except (InvalidOperation, TypeError, ValueError):
        return False


async def _ipn_handler(request: web.Request) -> web.Response:
    global _last_ipn_at
    provided_secret = request.headers.get("X-Secret-Key", "")
    if not provided_secret or not hmac.compare_digest(
        provided_secret, config.SEPAY_IPN_SECRET,
    ):
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "Invalid JSON"}, status=400)

    _last_ipn_at = int(time.time())
    notification_type = str(payload.get("notification_type", ""))
    order_data = payload.get("order") or {}
    transaction = payload.get("transaction") or {}
    order_code = str(order_data.get("order_invoice_number", "")).strip().upper()
    tran_id = str(transaction.get("transaction_id") or transaction.get("id") or "")

    if notification_type == "TRANSACTION_VOID":
        await notify_admins(
            request.app["bot"],
            f"⚠️ SePay báo huỷ giao dịch <code>{tran_id or 'không rõ'}</code> "
            f"cho đơn <code>{order_code or 'không rõ'}</code>. Vui lòng kiểm tra thủ công.",
        )
        return web.json_response({"success": True})
    if notification_type != "ORDER_PAID":
        return web.json_response({"success": True, "ignored": True})

    order = database.get_order_by_code(order_code)
    if order is None:
        log.error("SePay IPN không tìm thấy đơn %s", order_code)
        await notify_admins(
            request.app["bot"],
            f"🚨 SePay báo đã nhận tiền nhưng không tìm thấy đơn "
            f"<code>{order_code or 'trống'}</code>. Giao dịch: <code>{tran_id}</code>.",
        )
        return web.json_response({"success": True})

    valid_status = (
        str(order_data.get("order_status", "")) == "CAPTURED"
        and str(transaction.get("transaction_status", "")) == "APPROVED"
    )
    valid_currency = (
        str(order_data.get("order_currency", "")) == "VND"
        and str(transaction.get("transaction_currency", "")) == "VND"
    )
    valid_amount = (
        _amount_matches(order_data.get("order_amount"), order.total_amount)
        and _amount_matches(transaction.get("transaction_amount"), order.total_amount)
    )
    if not tran_id or not valid_status or not valid_currency or not valid_amount:
        log.error("SePay IPN không hợp lệ cho đơn %s: %s", order_code, payload)
        await notify_admins(
            request.app["bot"],
            f"🚨 <b>IPN SEPAY KHÔNG KHỚP</b>\n"
            f"Đơn <code>{order_code}</code> • cần thu {utils.format_vnd(order.total_amount)}\n"
            f"Giao dịch: <code>{tran_id or 'trống'}</code>\n"
            "Bot chưa giao voucher. Vui lòng kiểm tra trên SePay.",
        )
        return web.json_response({"success": True})

    if order.status == "delivered" and order.tran_id == tran_id:
        return web.json_response({"success": True, "duplicate": True})
    if database.tran_already_used(tran_id) and order.tran_id != tran_id:
        log.error("Giao dịch SePay %s đã gắn với đơn khác", tran_id)
        return web.json_response({"success": True, "duplicate": True})

    delivered = await deliver_order(request.app["bot"], order, tran_id)
    log.info(
        "Xử lý SePay IPN đơn %s, giao dịch %s: %s",
        order_code, tran_id, "delivered" if delivered else "not-delivered",
    )
    return web.json_response({"success": True})


async def _bank_webhook_handler(request: web.Request) -> web.Response:
    """Nhận biến động số dư từ SePay cho các QR chuyển khoản trực tiếp."""
    global _last_ipn_at
    raw_body = await request.read()
    timestamp = request.headers.get("X-SePay-Timestamp", "")
    signature = request.headers.get("X-SePay-Signature", "")
    if not verify_bank_webhook_signature(
        config.SEPAY_WEBHOOK_SECRET, timestamp, raw_body, signature,
    ):
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "Invalid payload"}, status=400)

    _last_ipn_at = int(time.time())
    if str(payload.get("transferType") or "").lower() != "in":
        return web.json_response({"success": True, "ignored": True})

    event_id = str(payload.get("id") or payload.get("referenceCode") or "").strip()
    tran_id = f"SEPAY-BANK-{event_id}" if event_id else ""
    order_code = extract_bank_order_code(payload)
    topup_code = extract_wallet_topup_code(payload)

    if topup_code and not order_code:
        topup = database.get_wallet_topup(topup_code)
        if topup is None:
            log.info("Bỏ qua giao dịch SePay không khớp yêu cầu nạp ví: %s", topup_code)
            return web.json_response({"success": True, "ignored": True})
        if not tran_id or not bank_transaction_matches_wallet_topup(payload, topup):
            await notify_admins(
                request.app["bot"],
                f"🚨 <b>CHUYỂN KHOẢN NẠP VÍ KHÔNG KHỚP</b>\n"
                f"Mã nạp: <code>{topup_code}</code> • cần nhận "
                f"{utils.format_vnd(topup.amount)}\n"
                f"Giao dịch: <code>{tran_id or 'không có mã'}</code>\n"
                "Bot chưa cộng ví vì sai tài khoản, số tiền hoặc nội dung.",
            )
            return web.json_response({"success": True})

        status, balance = database.complete_wallet_topup(topup_code, tran_id)
        if status == "credited":
            try:
                await request.app["bot"].send_message(
                    topup.chat_id,
                    f"✅ <b>NẠP VÍ THÀNH CÔNG</b>\n"
                    f"Mã nạp: <code>{topup_code}</code>\n"
                    f"Đã cộng: <b>{utils.format_vnd(topup.amount)}</b>\n"
                    f"Số dư mới: <b>{utils.format_vnd(balance)}</b>",
                )
            except Exception:  # noqa: BLE001
                pass
            buyer = html.escape(
                utils.buyer_label(topup.username, topup.buyer_name, topup.chat_id)
            )
            await notify_admins(
                request.app["bot"],
                f"💰 <b>NẠP VÍ THÀNH CÔNG</b>\n"
                f"Khách: {buyer} · <code>{topup.chat_id}</code>\n"
                f"Số tiền: <b>{utils.format_vnd(topup.amount)}</b>\n"
                f"Mã nạp: <code>{topup_code}</code>\n"
                f"Số dư mới: <b>{utils.format_vnd(balance)}</b>",
            )
            return web.json_response({"success": True})
        if status == "duplicate":
            return web.json_response({"success": True, "duplicate": True})
        if status == "already_paid":
            await notify_admins(
                request.app["bot"],
                f"⚠️ <b>MÃ NẠP VÍ ĐÃ ĐƯỢC THANH TOÁN TRƯỚC ĐÓ</b>\n"
                f"Mã nạp: <code>{topup_code}</code>\n"
                f"Giao dịch mới: <code>{tran_id}</code>\n"
                "Vui lòng kiểm tra và hoàn tiền hoặc cộng ví thủ công.",
            )
            return web.json_response({"success": True, "duplicate_payment": True})
        await notify_admins(
            request.app["bot"],
            f"🚨 Không thể cộng ví cho mã <code>{topup_code}</code>. "
            f"Trạng thái: <b>{status}</b>.",
        )
        return web.json_response({"success": True})

    if not order_code:
        return web.json_response({"success": True, "ignored": True})

    order = database.get_order_by_code(order_code)
    if order is None:
        log.info("Bỏ qua giao dịch SePay không khớp đơn bot: %s", order_code)
        return web.json_response({"success": True, "ignored": True})

    if not tran_id or not bank_transaction_matches_order(payload, order):
        log.error("Webhook ngân hàng SePay không khớp đơn %s", order_code)
        await notify_admins(
            request.app["bot"],
            f"🚨 <b>CHUYỂN KHOẢN KHÔNG KHỚP ĐƠN</b>\n"
            f"Đơn <code>{order_code}</code> • cần thu "
            f"{utils.format_vnd(order.total_amount)}\n"
            f"Giao dịch: <code>{tran_id or 'không có mã'}</code>\n"
            "Bot chưa giao voucher vì sai tài khoản, số tiền hoặc nội dung.",
        )
        return web.json_response({"success": True})

    if order.status == "delivered":
        if order.tran_id == tran_id:
            return web.json_response({"success": True, "duplicate": True})
        await notify_admins(
            request.app["bot"],
            f"⚠️ <b>ĐƠN ĐÃ GIAO NHƯNG NHẬN THÊM TIỀN</b>\n"
            f"Đơn <code>{order_code}</code> • {utils.format_vnd(order.total_amount)}\n"
            f"Giao dịch mới: <code>{tran_id}</code>\n"
            "Vui lòng kiểm tra và hoàn tiền nếu khách chuyển trùng.",
        )
        return web.json_response({"success": True, "duplicate_payment": True})

    if database.tran_already_used(tran_id):
        log.error("Giao dịch ngân hàng SePay %s đã gắn với đơn khác", tran_id)
        return web.json_response({"success": True, "duplicate": True})

    delivered = await deliver_order(request.app["bot"], order, tran_id)
    log.info(
        "Xử lý webhook ngân hàng SePay đơn %s, giao dịch %s: %s",
        order_code, tran_id, "delivered" if delivered else "not-delivered",
    )
    return web.json_response({"success": True})


async def _result_handler(request: web.Request) -> web.Response:
    result = request.match_info["result"]
    order_code = request.query.get("order", "").strip().upper()
    order = database.get_order_by_code(order_code) if order_code else None
    if result == "success":
        if order and order.status == "delivered":
            title = "Thanh toán thành công"
            message = "Voucher đã được gửi về Telegram. Bạn có thể đóng trang này."
        else:
            title = "Đang xác nhận thanh toán"
            message = "Vui lòng quay lại Telegram; bot sẽ gửi voucher ngay khi nhận IPN SePay."
    elif result == "cancel":
        title = "Đã đóng thanh toán"
        message = "Đơn chưa bị huỷ. Bạn có thể thanh toán lại hoặc huỷ đơn trong Telegram."
    else:
        title = "Thanh toán chưa hoàn tất"
        message = "Vui lòng quay lại Telegram và thử lại hoặc liên hệ hỗ trợ."
    return _html_response(sepay.render_result_page(title, message))


async def _health_handler(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "gateway": "sepay", "mode": "direct_qr"})


async def run_sepay_server(bot: Bot) -> None:
    """Chạy HTTP server nhận webhook ngân hàng và IPN SePay cũ."""
    global _server_running, _last_server_error
    app = web.Application(client_max_size=256 * 1024)
    app["bot"] = bot
    app.add_routes([
        web.get("/payments/sepay/checkout/{order_code}", _checkout_handler),
        web.post("/payments/sepay/webhook", _bank_webhook_handler),
        web.post("/payments/sepay/ipn", _ipn_handler),
        web.get("/payments/sepay/{result:success|error|cancel}", _result_handler),
        web.get("/payments/sepay/health", _health_handler),
    ])
    runner = web.AppRunner(app, access_log=log)
    try:
        await runner.setup()
        site = web.TCPSite(
            runner, host=config.PAYMENT_SERVER_HOST, port=config.PAYMENT_SERVER_PORT,
        )
        await site.start()
        _server_running = True
        _last_server_error = ""
        log.info(
            "SePay webhook server đang nghe tại %s:%d",
            config.PAYMENT_SERVER_HOST, config.PAYMENT_SERVER_PORT,
        )
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        _last_server_error = f"{type(exc).__name__}: {exc}"
        log.exception("Không khởi động được SePay webhook server")
        await notify_admins(
            bot,
            f"🚨 <b>Không khởi động được SePay webhook server</b>\n"
            f"<code>{_last_server_error}</code>",
        )
    finally:
        _server_running = False
        await runner.cleanup()


async def watch_expiry(bot: Bot) -> None:
    """Vòng lặp nền: huỷ đơn quá hạn, trả voucher về kho, báo khách."""
    while True:
        try:
            now = int(time.time())
            for order in database.list_pending_orders():
                if order.expires_at <= now and database.expire_order(order.id):
                    # Báo khách.
                    try:
                        await bot.send_message(
                            order.chat_id,
                            f"⏰ Đơn <code>{order.order_code}</code> đã hết hạn "
                            "(chưa nhận được thanh toán). Vui lòng đặt lại nếu bạn "
                            "vẫn muốn mua. /start",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    # Báo admin: đơn bị huỷ do quá hạn.
                    buyer = utils.buyer_label(
                        order.buyer_username, order.buyer_name, order.chat_id
                    )
                    label = utils.items_label(
                        database.get_order_lines(order.id)
                    ) or f"{order.quantity} mã"
                    await notify_admins(
                        bot,
                        f"❌ <b>Đơn hàng bị huỷ</b>\n"
                        f"🧾 Đơn {order.order_code}\n"
                        f"👤 Người mua: {buyer}\n"
                        f"💵 Số tiền: {utils.format_vnd(order.total_amount)}\n"
                        f"📦 {label}\n"
                        f"⏰ Quá {config.ORDER_TIMEOUT_MINUTES} phút không thanh toán "
                        "— mã đã trả về kho.",
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("Lỗi khi kiểm tra hết hạn đơn: %s", exc)
        await asyncio.sleep(30)


async def notify_admins(bot: Bot, text: str, disable_preview: bool = True) -> None:
    """Gửi 1 tin cho tất cả admin (bỏ qua lỗi từng người)."""
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id, text, disable_web_page_preview=disable_preview
            )
        except Exception:  # noqa: BLE001
            pass


# Giữ tên cũ để tương thích chỗ gọi nội bộ.
_notify_admins = notify_admins


async def low_stock_alert(bot: Bot, denom: int) -> None:
    """Nhắc admin khi kho của 1 mệnh giá còn ít hơn ngưỡng."""
    remaining = database.count_available(denom)
    if remaining >= LOW_STOCK_THRESHOLD:
        return
    if remaining == 0:
        msg = (
            f"🚨 <b>HẾT KHO</b> voucher {utils.format_vnd(denom)}!\n"
            "Khách sẽ không mua được loại này. Dùng /nhapkho để nạp thêm ngay."
        )
    else:
        msg = (
            f"⚠️ <b>Kho sắp hết</b> — voucher {utils.format_vnd(denom)} chỉ còn "
            f"<b>{remaining}</b> mã.\n"
            "Nên dùng /nhapkho để nạp thêm."
        )
    await notify_admins(bot, msg)


async def watch_daily_report(bot: Bot) -> None:
    """Mỗi ngày lúc 22h30 (giờ VN), gửi báo cáo doanh thu trong ngày cho admin."""
    while True:
        now = datetime.datetime.now(utils.VN_TZ)
        target = now.replace(
            hour=DAILY_REPORT_HOUR, minute=DAILY_REPORT_MINUTE,
            second=0, microsecond=0,
        )
        if target <= now:
            target += datetime.timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        try:
            await _send_daily_report(bot, report_day=target)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("Lỗi khi gửi báo cáo doanh thu ngày: %s", exc)


async def _send_daily_report(bot: Bot, report_day: datetime.datetime) -> None:
    """Gửi báo cáo doanh thu cho ngày chứa report_day (00:00 -> thời điểm gửi)."""
    day_start = report_day.replace(hour=0, minute=0, second=0, microsecond=0)
    text = build_report_text(
        start=day_start, end=report_day,
        title=f"Ngày {day_start.strftime('%d/%m/%Y')}",
    )
    await notify_admins(bot, text)


def build_report_text(
    start: datetime.datetime, end: datetime.datetime, title: str,
) -> str:
    """Dựng nội dung báo cáo doanh thu cho khoảng [start, end)."""
    start_ts, end_ts = int(start.timestamp()), int(end.timestamp())
    count, revenue, by_denom = database.delivered_stats_between(start_ts, end_ts)

    # Bảng doanh thu theo mệnh giá (+ dòng tổng).
    rows = []
    for denom in config.DENOMINATIONS:
        info = by_denom.get(denom, {"count": 0, "qty": 0, "revenue": 0})
        rows.append([
            utils.format_k(denom), str(info["count"]), str(info["qty"]),
            utils.format_vnd(info["revenue"]),
        ])
    rows.append(["TỔNG", str(count), str(sum(i["qty"] for i in by_denom.values())),
                 utils.format_vnd(revenue)])
    sales_table = utils.mono_table(["Loại", "Đơn", "Mã", "Doanh thu"], rows)

    # Bảng tồn kho.
    stock_rows = [
        [utils.format_k(d), f"{c} mã"] for d, c in database.stock_summary().items()
    ]
    stock_table = utils.mono_table(["Loại", "Còn lại"], stock_rows)

    return (
        f"📊 <b>BÁO CÁO DOANH THU</b>\n"
        f"🗓 {title}\n"
        f"{sales_table}\n"
        f"📦 <b>Tồn kho còn lại</b>\n{stock_table}"
    )
