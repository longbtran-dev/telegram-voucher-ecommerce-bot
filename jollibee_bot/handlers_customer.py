"""Tương tác khách hàng: menu chính, giỏ hàng nhiều mệnh giá, hướng dẫn, hỗ trợ, lịch sử."""

from __future__ import annotations

import datetime
import html
import logging
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    URLInputFile,
)

from . import config, database, payments, report, utils

router = Router()
log = logging.getLogger("handlers.customer")

# Giỏ hàng trong bộ nhớ: {chat_id: {denom: quantity}}.
# Bot chạy 1 process nên dict là đủ; mất giỏ khi restart cũng không sao.
_carts: dict[int, dict[int, int]] = {}


class WalletTopupInput(StatesGroup):
    waiting_amount = State()


def _cart(chat_id: int) -> dict[int, int]:
    return _carts.setdefault(chat_id, {})


# =========================
# MENU CHÍNH
# =========================
def _menu_text() -> str:
    title = database.get_config("shop_title") or "Shop Voucher Jollibee"
    for emoji in ("🐣", "🐥", "🍗", "🐔"):
        title = title.replace(emoji, "").strip()
    link = database.get_config("shop_link")
    lines = [
        f"🐣 <b>{title.upper()}</b> 🐥",
        "<i>Voucher chính hãng • Giá tốt • Giao tự động</i>",
        "━━━━━━━━━━━━━━━━━━",
        "<blockquote>"
        "✨ Voucher Jollibee chính hãng\n"
        "⚡ Thanh toán tự động — nhận mã nhanh chóng\n"
        "🛡 Bảo hành 24 giờ"
        "</blockquote>",
    ]
    if link:
        lines.append(f"🔗 {link}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("🎁 <b>SẴN SÀNG CHỌN VOUCHER?</b>")
    lines.append("<i>Chạm nút mua bên dưới để bắt đầu.</i>")
    return "\n".join(lines)


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 MUA VOUCHER NGAY", callback_data="menu:buy")],
        [
            InlineKeyboardButton(text="💰 Ví của tôi", callback_data="menu:wallet"),
            InlineKeyboardButton(text="🧾 Đơn đã mua", callback_data="menu:history"),
        ],
        [
            InlineKeyboardButton(text="📖 Cách sử dụng", callback_data="menu:guide"),
            InlineKeyboardButton(text="☎️ Hỗ trợ", callback_data="menu:support"),
        ],
    ])


# Nhãn nút của bàn phím cố định (luôn hiện dưới ô chat). Dùng lại để khớp handler.
BTN_BUY = "🛒 Mua hàng"
BTN_WALLET = "💰 Ví của tôi"
BTN_HISTORY = "🧾 Đơn của tôi"
BTN_GUIDE = "📖 Hướng dẫn"
BTN_SUPPORT = "☎️ Hỗ trợ"


def _reply_keyboard() -> ReplyKeyboardMarkup:
    """Bàn phím cố định dưới màn hình — khách luôn có lối thao tác, không cần gõ lệnh."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_BUY), KeyboardButton(text=BTN_WALLET)],
            [KeyboardButton(text=BTN_HISTORY), KeyboardButton(text=BTN_GUIDE)],
            [KeyboardButton(text=BTN_SUPPORT)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Chọn thao tác bên dưới 👇",
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    database.touch_user(message.chat.id, message.from_user.username or "")
    # Tin 1: đặt bàn phím cố định. Tin 2: menu chính dạng nút bấm trong tin.
    await message.answer(
        "🍗 Chào mừng bạn! Dùng thanh nút bên dưới để thao tác nhanh bất cứ lúc nào 👇",
        reply_markup=_reply_keyboard(),
    )
    await message.answer(_menu_text(), reply_markup=_menu_keyboard())


@router.callback_query(F.data == "menu:home")
async def on_menu_home(cb: CallbackQuery) -> None:
    await _safe_edit(cb.message, _menu_text(), _menu_keyboard())
    await cb.answer()


async def _safe_edit(message: Message, text: str, kb: InlineKeyboardMarkup) -> None:
    """edit_text nhưng bỏ qua lỗi 'message is not modified' khi nội dung y hệt."""
    try:
        await message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest:
        pass


# =========================
# VÍ KHÁCH HÀNG
# =========================
def _wallet_kind_label(kind: str) -> str:
    return {
        "topup": "Nạp tiền SePay",
        "purchase": "Thanh toán đơn",
        "admin_credit": "Admin cộng tiền",
        "admin_debit": "Admin trừ tiền",
        "admin_set": "Admin điều chỉnh",
    }.get(kind, kind)


def _wallet_text(chat_id: int) -> str:
    balance = database.wallet_balance(chat_id)
    transactions = database.list_wallet_transactions(chat_id, limit=8)
    pending = database.list_pending_wallet_topups(chat_id, limit=3)
    lines = [
        "💰 <b>VÍ CỦA TÔI</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"Số dư khả dụng: <b>{utils.format_vnd(balance)}</b>",
        "<i>Bạn có thể dùng ví để thanh toán voucher ngay lập tức.</i>",
    ]
    if pending:
        lines.extend(["━━━━━━━━━━━━━━━━━━", "⏳ <b>Yêu cầu nạp đang chờ:</b>"])
        for topup in pending:
            lines.append(
                f"• <code>{topup.topup_code}</code> · {utils.format_vnd(topup.amount)}"
            )
    lines.extend(["━━━━━━━━━━━━━━━━━━", "📜 <b>Giao dịch gần đây</b>"])
    if not transactions:
        lines.append("<i>Chưa có giao dịch ví.</i>")
    for transaction in transactions:
        sign = "+" if transaction.amount > 0 else "−"
        reference = (
            f" · <code>{html.escape(transaction.reference)}</code>"
            if transaction.reference
            else ""
        )
        lines.append(
            f"• <b>{sign}{utils.format_vnd(abs(transaction.amount))}</b> — "
            f"{_wallet_kind_label(transaction.kind)}{reference}\n"
            f"  <i>{utils.format_time(transaction.created_at)}</i>"
        )
    return "\n".join(lines)


def _wallet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Nạp tiền vào ví", callback_data="wallet:topup")],
        [InlineKeyboardButton(text="🛒 Mua voucher", callback_data="menu:buy")],
        [
            InlineKeyboardButton(text="🔄 Làm mới", callback_data="wallet:refresh"),
            InlineKeyboardButton(text="⬅️ Về menu", callback_data="menu:home"),
        ],
    ])


@router.callback_query(F.data.in_({"menu:wallet", "wallet:refresh"}))
async def on_menu_wallet(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    database.touch_user(cb.message.chat.id, cb.from_user.username or "")
    await _safe_edit(cb.message, _wallet_text(cb.message.chat.id), _wallet_keyboard())
    await cb.answer()


@router.message(Command("vi", "wallet"))
async def cmd_wallet(message: Message, state: FSMContext) -> None:
    await state.clear()
    database.touch_user(message.chat.id, message.from_user.username or "")
    await message.answer(_wallet_text(message.chat.id), reply_markup=_wallet_keyboard())


@router.callback_query(F.data == "wallet:topup")
async def on_wallet_topup(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WalletTopupInput.waiting_amount)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Huỷ", callback_data="wallet:cancel_topup"),
    ]])
    await cb.message.answer(
        "➕ <b>NẠP TIỀN VÀO VÍ</b>\n"
        "Nhập số tiền muốn nạp, ví dụ <code>100000</code> hoặc <code>100k</code>.\n"
        "Số tiền hỗ trợ: từ <b>10.000đ</b> đến <b>100.000.000đ</b>.",
        reply_markup=keyboard,
    )
    await cb.answer()


@router.callback_query(F.data == "wallet:cancel_topup")
async def on_wallet_cancel_topup(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _safe_edit(cb.message, _wallet_text(cb.message.chat.id), _wallet_keyboard())
    await cb.answer("Đã huỷ tạo yêu cầu nạp ví.")


@router.message(WalletTopupInput.waiting_amount, F.text)
async def on_wallet_topup_amount(message: Message, state: FSMContext) -> None:
    amount = utils.parse_vnd_amount(message.text)
    if amount is None or not 10_000 <= amount <= 100_000_000:
        await message.answer(
            "Số tiền không hợp lệ. Hãy nhập từ <b>10.000đ</b> đến "
            "<b>100.000.000đ</b>, ví dụ <code>100k</code>."
        )
        return
    full_name = " ".join(
        value for value in [message.from_user.first_name, message.from_user.last_name]
        if value
    )
    topup = database.create_wallet_topup(
        utils.generate_wallet_topup_code(),
        message.chat.id,
        amount,
        username=message.from_user.username or "",
        buyer_name=full_name,
    )
    if topup is None:
        await message.answer("Không tạo được yêu cầu nạp ví. Vui lòng thử lại.")
        return
    await state.clear()
    qr_url = utils.build_sepay_qr_url(
        account_no=config.BANK_ACCOUNT_NO,
        bank_code=config.BANK_CODE,
        amount=topup.amount,
        description=topup.topup_code,
        account_name=config.BANK_ACCOUNT_NAME,
    )
    caption = (
        "💰 <b>QUÉT QR ĐỂ NẠP VÍ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Số tiền: <b>{utils.format_vnd(topup.amount)}</b>\n"
        f"Ngân hàng: <b>{html.escape(_bank_display_name())}</b>\n"
        f"Số tài khoản: <code>{html.escape(config.BANK_ACCOUNT_NO)}</code>\n"
        f"Nội dung bắt buộc: <code>{topup.topup_code}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Bot sẽ tự cộng số dư ngay khi SePay xác nhận tiền vào."
    )
    try:
        await message.answer_photo(
            URLInputFile(qr_url, filename=f"{topup.topup_code}.png"),
            caption=caption,
            reply_markup=_wallet_keyboard(),
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Không gửi được QR nạp ví %s: %s", topup.topup_code, exc)
        await message.answer(
            caption,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📷 Mở ảnh QR", url=qr_url)],
                [InlineKeyboardButton(text="⬅️ Về ví", callback_data="menu:wallet")],
            ]),
            disable_web_page_preview=True,
        )


# =========================
# MUA HÀNG — GIỎ HÀNG NHIỀU MỆNH GIÁ
# =========================
def _prune_cart(chat_id: int) -> None:
    """Hạ số lượng trong giỏ xuống mức tồn kho hiện tại (kho có thể vừa cạn)."""
    cart = _cart(chat_id)
    for denom in list(cart):
        stock = database.count_available(denom)
        if cart[denom] > stock:
            cart[denom] = stock
        if cart[denom] <= 0:
            del cart[denom]


def _shop_text(chat_id: int) -> str:
    _prune_cart(chat_id)
    cart = _cart(chat_id)
    lines = [
        "🛒 <b>GIAN HÀNG VOUCHER JOLLIBEE</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    product_lines = []
    for denom in config.DENOMINATIONS:
        price = database.get_price(denom)
        stock = database.count_available(denom)
        status = f"còn <b>{stock}</b> mã" if stock > 0 else "<b>hết hàng</b> 😢"
        product_lines.append(
            f"🎟 Voucher <b>{utils.format_vnd(denom)}</b> — "
            f"giá {utils.format_vnd(price)} ({status})"
        )
    lines.append("<blockquote>" + "\n".join(product_lines) + "</blockquote>")
    lines.append("━━━━━━━━━━━━━━━━━━")

    items = [(d, q) for d, q in cart.items() if q > 0]
    if items:
        cart_lines = ["🧺 <b>Giỏ hàng của bạn:</b>"]
        total = 0
        for denom, qty in sorted(items, reverse=True):
            price = database.get_price(denom)
            cart_lines.append(
                f"   • {qty} × {utils.format_vnd(denom)} = "
                f"{utils.format_vnd(qty * price)}"
            )
            total += qty * price
        cart_lines.append(f"💰 Tổng thanh toán: <b>{utils.format_vnd(total)}</b>")
        lines.append("<blockquote>" + "\n".join(cart_lines) + "</blockquote>")
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append(
            "<blockquote>"
            "👇 Chỉnh số lượng bằng nút ➕/➖, xong bấm <b>Thanh toán</b>."
            "</blockquote>"
        )
    else:
        lines.append(
            "<blockquote>"
            "🧺 Giỏ hàng đang trống.\n"
            "👇 Bấm nút <b>➕</b> để thêm voucher vào giỏ "
            "(mua được nhiều loại cùng lúc)."
            "</blockquote>"
        )
    return "\n".join(lines)


def _shop_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    cart = _cart(chat_id)
    rows = []
    for denom in config.DENOMINATIONS:
        qty = cart.get(denom, 0)
        if database.count_available(denom) <= 0:
            # Hết hàng: thay hàng nút +/- bằng nút đăng ký báo khi có hàng.
            rows.append([InlineKeyboardButton(
                text=f"🔔 Báo tôi khi có voucher {utils.format_k(denom)}",
                callback_data=f"alert:{denom}",
            )])
            continue
        rows.append([
            InlineKeyboardButton(text="➖", callback_data=f"cart:dec:{denom}"),
            InlineKeyboardButton(
                text=f"🎟 {utils.format_k(denom)}  ×  {qty}",
                callback_data="cart:noop",
            ),
            InlineKeyboardButton(text="➕", callback_data=f"cart:inc:{denom}"),
        ])
    total = sum(q * database.get_price(d) for d, q in cart.items())
    if total > 0:
        rows.append([
            InlineKeyboardButton(
                text=f"✅ Thanh toán • {utils.format_vnd(total)}",
                callback_data="cart:pay",
            ),
        ])
        rows.append([
            InlineKeyboardButton(text="🗑 Xoá giỏ", callback_data="cart:clear"),
            InlineKeyboardButton(text="⬅️ Về menu", callback_data="menu:home"),
        ])
    else:
        rows.append([InlineKeyboardButton(text="⬅️ Về menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _bank_display_name() -> str:
    normalized = "".join(char for char in config.BANK_CODE.upper() if char.isalnum())
    return "TP BANK" if normalized in {"TPB", "TPBANK"} else config.BANK_CODE


def _order_created_text(order: database.Order, detail: str) -> str:
    balance = database.wallet_balance(order.chat_id)
    return (
        f"🧾 Đã tạo đơn <code>{order.order_code}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<blockquote>"
        f"{detail}\n"
        f"💰 Tổng: <b>{utils.format_vnd(order.total_amount)}</b>\n"
        f"👛 Số dư ví: <b>{utils.format_vnd(balance)}</b>"
        "</blockquote>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<blockquote>"
        "👇 Chọn thanh toán bằng ví hoặc chuyển khoản ngân hàng."
        "</blockquote>"
    )


def _order_detail(order: database.Order) -> str:
    lines = database.get_order_lines(order.id)
    if lines:
        return "\n".join(
            f"   • {quantity} × {utils.format_vnd(denom)} = "
            f"{utils.format_vnd(quantity * unit_price)}"
            for denom, quantity, unit_price in lines
        )
    return (
        f"   • {order.quantity} × {utils.format_vnd(order.denom)} = "
        f"{utils.format_vnd(order.total_amount)}"
    )


def _payment_method_keyboard(order: database.Order) -> InlineKeyboardMarkup:
    rows = []
    balance = database.wallet_balance(order.chat_id)
    if balance >= order.total_amount:
        rows.append([InlineKeyboardButton(
            text=f"👛 Thanh toán bằng ví • {utils.format_vnd(order.total_amount)}",
            callback_data=f"pay:wallet:{order.order_code}",
        )])
    rows.append([InlineKeyboardButton(
        text="🏦 Chuyển khoản ngân hàng",
        callback_data=f"pay:bank:{order.order_code}",
    )])
    rows.append([InlineKeyboardButton(
        text="❌ Huỷ đơn & đặt lại",
        callback_data=f"cancel:{order.order_code}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _paid_order_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Xem ví", callback_data="menu:wallet")],
        [InlineKeyboardButton(text="⬅️ Về menu", callback_data="menu:home")],
    ])


def _payment_caption(order: database.Order, detail: str) -> str:
    return (
        f"🧾 <b>ĐƠN {order.order_code}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<blockquote>"
        f"{detail}\n"
        f"💰 Tổng: <b>{utils.format_vnd(order.total_amount)}</b>"
        "</blockquote>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📷 <b>QUÉT QR ĐỂ THANH TOÁN</b>\n"
        "<blockquote>"
        "Chạm vào ảnh để phóng to hoặc lưu về máy. QR đã điền sẵn:\n"
        f"🏦 Ngân hàng: <b>{html.escape(_bank_display_name())}</b>\n"
        f"👤 Chủ tài khoản: <b>{html.escape(config.BANK_ACCOUNT_NAME)}</b>\n"
        f"💳 Số tài khoản: <code>{html.escape(config.BANK_ACCOUNT_NO)}</code>\n"
        f"📝 Nội dung: <code>{order.order_code}</code>"
        "</blockquote>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<blockquote>"
        f"⏱ Đơn tự huỷ sau <b>{config.ORDER_TIMEOUT_MINUTES} phút</b> "
        "nếu chưa thanh toán.\n"
        "✅ Thanh toán xong, bot tự gửi mã ngay khi hệ thống xác nhận.\n"
        "⚠️ Vui lòng chuyển <b>đúng số tiền và nội dung</b>; không sửa mã đơn.\n"
        "✏️ Muốn đổi số lượng/loại? Bấm <b>❌ Huỷ đơn & đặt lại</b> bên dưới "
        "rồi chọn lại từ đầu — mã sẽ được trả về kho ngay."
        "</blockquote>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<blockquote>"
        "📌 <b>Lưu ý:</b> Vui lòng dùng mã <b>ngay sau khi mua</b>, tránh tích trữ. "
        "Voucher chỉ được <b>bảo hành trong 24 giờ</b> kể từ lúc mua."
        "</blockquote>"
    )


async def _send_bank_payment(message: Message, bot, order: database.Order) -> None:
    detail = _order_detail(order)
    qr_url = utils.build_sepay_qr_url(
        account_no=config.BANK_ACCOUNT_NO,
        bank_code=config.BANK_CODE,
        amount=order.total_amount,
        description=order.order_code,
        account_name=config.BANK_ACCOUNT_NAME,
    )
    payment_text = _payment_caption(order, detail)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="❌ Huỷ đơn & đặt lại",
            callback_data=f"cancel:{order.order_code}",
        ),
    ]])
    try:
        await message.answer_photo(
            photo=URLInputFile(qr_url, filename=f"{order.order_code}.png"),
            caption=payment_text,
            reply_markup=keyboard,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Không gửi được ảnh QR cho đơn %s: %s", order.order_code, exc)
        fallback_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📷 Mở ảnh QR SePay", url=qr_url)],
            [InlineKeyboardButton(
                text="❌ Huỷ đơn & đặt lại",
                callback_data=f"cancel:{order.order_code}",
            )],
        ])
        await message.answer(
            payment_text + "\n\n<i>Telegram chưa tải được ảnh; bấm nút Mở ảnh QR.</i>",
            reply_markup=fallback_keyboard,
            disable_web_page_preview=True,
        )

    buyer = html.escape(
        utils.buyer_label(order.buyer_username, order.buyer_name, order.chat_id)
    )
    label = html.escape(utils.items_label(database.get_order_lines(order.id)))
    await payments.notify_admins(
        bot,
        f"👤 {buyer} vừa đặt {label}\n"
        f"💰 Chờ chuyển khoản: {utils.format_vnd(order.total_amount)}\n"
        f"🧾 Đơn: <code>{order.order_code}</code>\n"
        f"⏳ Tự huỷ sau {config.ORDER_TIMEOUT_MINUTES} phút nếu chưa thanh toán.",
    )


@router.callback_query(F.data == "menu:buy")
async def on_menu_buy(cb: CallbackQuery) -> None:
    chat_id = cb.message.chat.id
    await _safe_edit(cb.message, _shop_text(chat_id), _shop_keyboard(chat_id))
    await cb.answer()


@router.callback_query(F.data == "cart:noop")
async def on_cart_noop(cb: CallbackQuery) -> None:
    await cb.answer("Dùng nút ➕ / ➖ hai bên để chỉnh số lượng nhé.")


@router.callback_query(F.data.startswith("cart:inc:"))
async def on_cart_inc(cb: CallbackQuery) -> None:
    denom = int(cb.data.split(":")[2])
    chat_id = cb.message.chat.id
    cart = _cart(chat_id)
    stock = database.count_available(denom)
    current = cart.get(denom, 0)
    if stock <= 0:
        await cb.answer(
            "Loại này vừa hết hàng 😢 Bấm nút 🔔 để được báo ngay khi có hàng nhé.",
            show_alert=True,
        )
        await _safe_edit(cb.message, _shop_text(chat_id), _shop_keyboard(chat_id))
        return
    if current >= stock:
        await cb.answer(f"Kho chỉ còn {stock} mã loại này thôi.", show_alert=True)
        return
    cart[denom] = current + 1
    await _safe_edit(cb.message, _shop_text(chat_id), _shop_keyboard(chat_id))
    await cb.answer(f"Đã thêm 1 voucher {utils.format_k(denom)} vào giỏ 🧺")


@router.callback_query(F.data.startswith("cart:dec:"))
async def on_cart_dec(cb: CallbackQuery) -> None:
    denom = int(cb.data.split(":")[2])
    chat_id = cb.message.chat.id
    cart = _cart(chat_id)
    if cart.get(denom, 0) <= 0:
        await cb.answer("Giỏ chưa có loại này.")
        return
    cart[denom] -= 1
    if cart[denom] == 0:
        del cart[denom]
    await _safe_edit(cb.message, _shop_text(chat_id), _shop_keyboard(chat_id))
    await cb.answer()


@router.callback_query(F.data.startswith("alert:"))
async def on_stock_alert(cb: CallbackQuery) -> None:
    """Khách đăng ký 'báo tôi khi có hàng' cho 1 mệnh giá đang hết."""
    denom = int(cb.data.split(":")[1])
    chat_id = cb.message.chat.id
    if database.count_available(denom) > 0:
        # Hàng vừa về trong lúc khách còn nhìn nút cũ — vẽ lại gian hàng.
        await cb.answer(f"Voucher {utils.format_k(denom)} đã có hàng lại rồi, mua ngay nhé! 🎉")
        await _safe_edit(cb.message, _shop_text(chat_id), _shop_keyboard(chat_id))
        return
    if database.add_stock_alert(chat_id, denom):
        await cb.answer(
            f"✅ Đã ghi nhận! Bot sẽ nhắn bạn ngay khi voucher "
            f"{utils.format_k(denom)} có hàng trở lại.",
            show_alert=True,
        )
    else:
        await cb.answer("Bạn đã đăng ký nhận thông báo loại này rồi nhé 👌")


@router.callback_query(F.data == "cart:clear")
async def on_cart_clear(cb: CallbackQuery) -> None:
    chat_id = cb.message.chat.id
    _carts[chat_id] = {}
    await _safe_edit(cb.message, _shop_text(chat_id), _shop_keyboard(chat_id))
    await cb.answer("Đã xoá giỏ hàng 🗑")


@router.callback_query(F.data == "cart:pay")
async def on_cart_pay(cb: CallbackQuery) -> None:
    chat_id = cb.message.chat.id
    cart = _cart(chat_id)
    items = [
        (denom, qty, database.get_price(denom))
        for denom, qty in sorted(cart.items(), reverse=True)
        if qty > 0
    ]
    if not items:
        await cb.answer("Giỏ hàng đang trống, bấm ➕ để chọn voucher trước nhé.", show_alert=True)
        return

    full_name = " ".join(
        p for p in [cb.from_user.first_name, cb.from_user.last_name] if p
    )
    order = database.create_order_multi(
        order_code=utils.generate_order_code(),
        chat_id=chat_id,
        items=items,
        timeout_minutes=config.ORDER_TIMEOUT_MINUTES,
        buyer_username=cb.from_user.username or "",
        buyer_name=full_name,
    )
    if order is None:
        # Kho vừa thay đổi — vẽ lại gian hàng với tồn kho mới.
        await cb.answer(
            "Kho không còn đủ số lượng bạn chọn. Bạn chỉnh lại giỏ giúp mình nhé.",
            show_alert=True,
        )
        await _safe_edit(cb.message, _shop_text(chat_id), _shop_keyboard(chat_id))
        return

    _carts[chat_id] = {}  # đã chốt đơn, dọn giỏ

    # Tóm tắt đơn thay cho gian hàng (khoá lại các nút cũ).
    detail = "\n".join(
        f"   • {q} × {utils.format_vnd(d)} = {utils.format_vnd(q * p)}"
        for d, q, p in items
    )
    await _safe_edit(
        cb.message,
        _order_created_text(order, detail),
        _payment_method_keyboard(order),
    )
    await cb.answer("Đã tạo đơn. Chọn phương thức thanh toán.")


@router.callback_query(F.data.startswith("pay:bank:"))
async def on_pay_bank(cb: CallbackQuery) -> None:
    order_code = cb.data.rsplit(":", 1)[1]
    order = database.get_order_by_code(order_code)
    if order is None or order.chat_id != cb.message.chat.id:
        await cb.answer("Không tìm thấy đơn hàng này.", show_alert=True)
        return
    if order.status != "pending":
        await cb.answer("Đơn này đã được thanh toán hoặc không còn hiệu lực.", show_alert=True)
        return
    if order.expires_at <= int(time.time()):
        database.expire_order(order.id)
        await cb.answer("Đơn đã hết hạn. Vui lòng đặt lại.", show_alert=True)
        return

    await cb.answer("Đang tạo ảnh QR thanh toán...")
    await _safe_edit(
        cb.message,
        _order_created_text(order, _order_detail(order))
        + "\n\n🏦 <b>Đã chọn chuyển khoản ngân hàng.</b>",
        InlineKeyboardMarkup(inline_keyboard=[]),
    )
    await _send_bank_payment(cb.message, cb.bot, order)


@router.callback_query(F.data.startswith("pay:wallet:"))
async def on_pay_wallet(cb: CallbackQuery) -> None:
    order_code = cb.data.rsplit(":", 1)[1]
    order = database.get_order_by_code(order_code)
    if order is None or order.chat_id != cb.message.chat.id:
        await cb.answer("Không tìm thấy đơn hàng này.", show_alert=True)
        return

    status, balance, tran_id = database.pay_order_with_wallet(order_code, order.chat_id)
    if status == "insufficient":
        current = database.get_order_by_code(order_code)
        await cb.answer(
            f"Số dư ví chỉ còn {utils.format_vnd(balance)}.", show_alert=True,
        )
        if current is not None and current.status == "pending":
            await _safe_edit(
                cb.message,
                _order_created_text(current, _order_detail(current))
                + "\n\n⚠️ <b>Số dư ví không đủ, vui lòng chuyển khoản.</b>",
                _payment_method_keyboard(current),
            )
        return
    if status == "expired":
        await cb.answer("Đơn đã hết hạn.", show_alert=True)
        await _safe_edit(
            cb.message,
            f"⌛ Đơn <code>{order_code}</code> đã hết hạn. Mã đã được trả về kho.",
            _menu_keyboard(),
        )
        return
    if status != "paid":
        current = database.get_order_by_code(order_code)
        message = (
            "Đơn đã được ghi nhận thanh toán và đang xử lý."
            if current is not None and current.status in {"paid_pending", "delivered"}
            else "Đơn không còn thanh toán được."
        )
        await cb.answer(message, show_alert=True)
        return

    await cb.answer("Đã trừ ví, bot đang xử lý đơn...")
    paid_order = database.get_order_by_code(order_code)
    await _safe_edit(
        cb.message,
        f"⏳ Bot đang xử lý đơn <code>{order_code}</code>.\n"
        "Theo dõi các tin nhắn mới để xem trạng thái thanh toán và kiểm tra mã.",
        InlineKeyboardMarkup(inline_keyboard=[]),
    )
    try:
        delivered = bool(
            paid_order
            and await payments.deliver_order(
                cb.bot,
                paid_order,
                tran_id=tran_id,
                announce_payment=True,
            )
        )
    except Exception as exc:  # noqa: BLE001
        delivered = False
        log.exception("Lỗi giao đơn thanh toán ví %s", order_code)
        await payments.notify_admins(
            cb.bot,
            f"🚨 <b>ĐƠN ĐÃ TRỪ VÍ NHƯNG GIAO LỖI</b>\n"
            f"Đơn <code>{order_code}</code> · khách <code>{order.chat_id}</code>\n"
            f"Lỗi: <code>{html.escape(str(exc)[:300])}</code>",
        )

    if delivered:
        text = (
            f"✅ Đơn <code>{order_code}</code> đã thanh toán bằng ví và giao mã thành công.\n"
            f"👛 Số dư còn lại: <b>{utils.format_vnd(balance)}</b>"
        )
    else:
        text = (
            f"✅ Đơn <code>{order_code}</code> đã thanh toán bằng ví.\n"
            "Bot đang giữ đơn để kiểm tra/giao mã; bạn không cần thanh toán lại.\n"
            f"👛 Số dư còn lại: <b>{utils.format_vnd(balance)}</b>"
        )
    await _safe_edit(cb.message, text, _paid_order_keyboard())


@router.callback_query(F.data.startswith("cancel:"))
async def on_cancel_order(cb: CallbackQuery) -> None:
    order_code = cb.data.split(":")[1]
    order = database.cancel_customer_order(order_code, cb.message.chat.id)
    if order is None:
        await cb.answer(
            "Đơn này không còn huỷ được (đã thanh toán hoặc đã hết hạn).",
            show_alert=True,
        )
        return

    try:
        await cb.message.edit_text(
            text=(
                f"❌ Đơn <code>{order_code}</code> đã được huỷ theo yêu cầu của bạn.\n"
                "Mã đã trả về kho — bạn có thể đặt đơn mới bất cứ lúc nào."
            ),
            reply_markup=None,
        )
    except TelegramBadRequest:
        try:
            await cb.message.edit_caption(
                caption=(
                    f"❌ Đơn <code>{order_code}</code> đã được huỷ theo yêu cầu của bạn.\n"
                    "Mã đã trả về kho — bạn có thể đặt đơn mới bất cứ lúc nào."
                ),
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass
    await cb.message.answer(
        "🛒 Muốn đặt lại? Bấm <b>Mua hàng</b> bên dưới để chọn lại số lượng nhé.",
        reply_markup=_menu_keyboard(),
    )
    await cb.answer("Đã huỷ đơn.")

    buyer = utils.buyer_label(order.buyer_username, order.buyer_name, order.chat_id)
    await payments.notify_admins(
        cb.bot,
        f"↩️ {buyer} tự huỷ đơn {order_code} "
        f"({utils.format_vnd(order.total_amount)}) — mã đã trả về kho.",
    )


# =========================
# HƯỚNG DẪN SỬ DỤNG
# =========================
def _guide_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🏪 Dùng trực tiếp tại cửa hàng", callback_data="guide:direct",
        )],
        [InlineKeyboardButton(
            text="📱 Đặt hàng qua app Jollibee", callback_data="guide:app",
        )],
        [InlineKeyboardButton(text="⬅️ Về menu", callback_data="menu:home")],
    ])


_GUIDE_COMBO_NOTE = (
    "💡 <b>Mẹo hay:</b> 1 hóa đơn dùng được <b>NHIỀU voucher cùng lúc</b>!\n"
    "Ví dụ hóa đơn 300k bạn có thể dùng:\n"
    "   • 3 voucher 100k, hoặc\n"
    "   • 1 voucher 200k + 1 voucher 100k\n"
    "👉 Mua kết hợp mệnh giá cho khớp hóa đơn để tiết kiệm tối đa."
)


_GUIDE_MENU_TEXT = (
    "📖 <b>HƯỚNG DẪN SỬ DỤNG VOUCHER</b>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "Bạn định dùng voucher theo cách nào?\n"
    "👇 Chọn để xem hướng dẫn chi tiết:"
)


@router.callback_query(F.data == "menu:guide")
async def on_menu_guide(cb: CallbackQuery) -> None:
    await _safe_edit(cb.message, _GUIDE_MENU_TEXT, _guide_menu_kb())
    await cb.answer()


# Nút dưới tin giao mã: gửi TIN MỚI (không sửa tin cũ, giữ nguyên mã + lưu ý).
@router.callback_query(F.data == "new:guide")
async def on_new_guide(cb: CallbackQuery) -> None:
    await cb.message.answer(_GUIDE_MENU_TEXT, reply_markup=_guide_menu_kb())
    await cb.answer()


@router.callback_query(F.data == "new:buy")
async def on_new_buy(cb: CallbackQuery) -> None:
    chat_id = cb.message.chat.id
    await cb.message.answer(
        _shop_text(chat_id), reply_markup=_shop_keyboard(chat_id),
        disable_web_page_preview=True,
    )
    await cb.answer()


def _guide_direct_text() -> str:
    return (
        "🏪 <b>DÙNG TRỰC TIẾP TẠI CỬA HÀNG</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "1️⃣ Mở file <code>&lt;mã_đơn&gt;.html</code> bot gửi kèm cuối đơn hàng "
        "để mở thẻ <b>mã vạch (barcode)</b> đưa nhân viên quét trực tiếp tại quầy.\n"
        "2️⃣ Hoặc bấm vào mã <code>UB...</code> trong tin nhắn để sao chép và đọc/đưa mã cho nhân viên.\n"
        "3️⃣ Kiểm tra số tiền được trừ trên hóa đơn trước khi thanh toán — xong! 🎉\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{_GUIDE_COMBO_NOTE}"
    )


@router.callback_query(F.data == "guide:direct")
async def on_guide_direct(cb: CallbackQuery) -> None:
    text = _guide_direct_text()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Mua hàng ngay", callback_data="menu:buy")],
        [InlineKeyboardButton(text="⬅️ Quay lại", callback_data="menu:guide")],
    ])
    await _safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(F.data == "guide:app")
async def on_guide_app(cb: CallbackQuery) -> None:
    text = (
        "📱 <b>ĐẶT HÀNG QUA APP JOLLIBEE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "1️⃣ Trong tin nhắn voucher bot gửi, <b>bấm vào mã</b> dạng "
        "<code>UB...</code> để copy.\n"
        "2️⃣ Mở app Jollibee, chọn món và vào giỏ hàng như bình thường.\n"
        "3️⃣ Ở bước thanh toán, tìm ô <b>«Nhập mã khuyến mãi/coupon»</b>.\n"
        "4️⃣ <b>Dán mã UB...</b> vào ô đó và bấm <b>«Áp dụng»</b> — số tiền được "
        "trừ ngay vào đơn. 🎉\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{_GUIDE_COMBO_NOTE}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Mua hàng ngay", callback_data="menu:buy")],
        [InlineKeyboardButton(text="⬅️ Quay lại", callback_data="menu:guide")],
    ])
    await _safe_edit(cb.message, text, kb)
    await cb.answer()


# =========================
# HỖ TRỢ
# =========================
def _support_text() -> str:
    phone = database.get_config("support_phone", "0900000000")
    contact = database.get_config("support_contact", "@YourSupportBot")
    return (
        "☎️ <b>HỖ TRỢ KHÁCH HÀNG</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📞 Điện thoại/Zalo: <code>{phone}</code>\n"
        f"💬 Telegram: <b>{contact}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ Khi cần hỗ trợ về mã lỗi, bạn vui lòng gửi kèm:\n"
        "   • Mã đơn hàng (dạng <code>JB...</code>)\n"
        "   • Ảnh chụp màn hình lỗi\n"
        "để được xử lý nhanh nhất nhé!\n\n"
        "🛡 Voucher được bảo hành trong <b>24 giờ</b> kể từ lúc mua."
    )


@router.callback_query(F.data == "menu:support")
async def on_menu_support(cb: CallbackQuery) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Về menu", callback_data="menu:home")],
    ])
    await _safe_edit(cb.message, _support_text(), kb)
    await cb.answer()


# =========================
# LỊCH SỬ GIAO DỊCH (KHÁCH) — CHỌN THEO TỪNG MÃ ĐƠN
# =========================
_HISTORY_PAGE_SIZE = 6


def _customer_history_page_data(
    chat_id: int, page: int = 0,
) -> tuple[str, InlineKeyboardMarkup, int, int]:
    total_count, total_spent = database.customer_stats(chat_id)
    if total_count <= 0:
        return (
            "🧾 Bạn chưa có giao dịch nào.\n"
            "Bấm <b>Mua hàng</b> để chọn voucher đầu tiên nhé!",
            _menu_keyboard(),
            0,
            1,
        )

    pages = max(1, (total_count + _HISTORY_PAGE_SIZE - 1) // _HISTORY_PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    orders = database.list_customer_delivered(
        chat_id,
        limit=_HISTORY_PAGE_SIZE,
        offset=page * _HISTORY_PAGE_SIZE,
    )

    lines = [
        "🧾 <b>ĐƠN HÀNG ĐÃ MUA</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"📦 Tổng đã mua: <b>{total_count}</b> đơn · <b>{utils.format_vnd(total_spent)}</b>",
        f"📄 Trang: <b>{page + 1}/{pages}</b>",
        "━━━━━━━━━━━━━━━━━━",
        "👇 <i>Chọn mã đơn bên dưới để xem lại mã voucher và nhận file mã vạch (barcode):</i>",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for o in orders:
        ts = o.delivered_at or o.created_at
        order_lines = database.get_order_lines(o.id)
        items_label = utils.items_label(order_lines) or f"{o.quantity} mã"
        btn_text = (
            f"🧾 {o.order_code} · {items_label} · "
            f"{utils.format_dmy(ts)[:5]} {utils.format_hm(ts)}"
        )
        rows.append([InlineKeyboardButton(
            text=btn_text,
            callback_data=f"hist:order:{o.order_code}:{page}",
        )])

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(
            text="⬅️ Trang trước", callback_data=f"hist:page:{page - 1}",
        ))
    if page + 1 < pages:
        nav_row.append(InlineKeyboardButton(
            text="Trang sau ➡️", callback_data=f"hist:page:{page + 1}",
        ))
    if nav_row:
        rows.append(nav_row)
    rows.append([InlineKeyboardButton(text="⬅️ Về menu", callback_data="menu:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows), page, pages


def _customer_order_history_text(order: database.Order) -> str:
    ts = order.delivered_at or order.created_at
    order_lines = database.get_order_lines(order.id)
    items_label = utils.items_label(order_lines) or f"{order.quantity} mã"
    details = database.get_order_voucher_details(order.id)
    voucher_lines = [
        f"{idx:02d}. <code>{html.escape(code)}</code> ({utils.format_k(denom)})"
        for idx, (_, code, _, denom) in enumerate(details, start=1)
    ]
    vouchers_block = "\n".join(voucher_lines) if voucher_lines else "<i>Chưa có dữ liệu mã</i>"
    return (
        f"🧾 <b>CHI TIẾT ĐƠN <code>{html.escape(order.order_code)}</code></b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<blockquote>"
        f"🕒 Thời gian: <b>{utils.format_time(ts)}</b>\n"
        f"🎁 Sản phẩm: <b>{html.escape(items_label)}</b>\n"
        f"💰 Đã thanh toán: <b>{utils.format_vnd(order.total_amount)}</b>"
        "</blockquote>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔑 <b>MÃ VOUCHER TRONG ĐƠN:</b>\n"
        f"<blockquote>{vouchers_block}</blockquote>\n"
        "📋 <i>Nhấn vào từng mã để sao chép, hoặc mở file HTML bên dưới để quét mã vạch (barcode).</i>"
    )


async def _send_customer_history(message: Message, chat_id: int) -> None:
    text, kb, _, _ = _customer_history_page_data(chat_id, 0)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data == "menu:history")
async def on_menu_history(cb: CallbackQuery) -> None:
    text, kb, _, _ = _customer_history_page_data(cb.message.chat.id, 0)
    await _safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(F.data.startswith("hist:page:"))
async def on_history_page(cb: CallbackQuery) -> None:
    page = int(cb.data.rsplit(":", 1)[1])
    text, kb, _, _ = _customer_history_page_data(cb.message.chat.id, page)
    await _safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(F.data.startswith("hist:order:"))
async def on_history_order(cb: CallbackQuery) -> None:
    parts = cb.data.split(":")
    order_code = parts[2]
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    chat_id = cb.message.chat.id
    order = database.get_order_by_code(order_code)
    if order is None or order.chat_id != chat_id or order.status != "delivered":
        await cb.answer("Không tìm thấy đơn hàng này.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Danh sách đơn", callback_data=f"hist:page:{page}")],
        [InlineKeyboardButton(text="🏠 Về menu", callback_data="menu:home")],
    ])
    await _safe_edit(cb.message, _customer_order_history_text(order), kb)
    await cb.answer(f"Đang gửi file mã vạch đơn {order.order_code}...")

    details = database.get_order_voucher_details(order.id)
    if details:
        await payments._send_order_barcode_document(
            cb.bot,
            chat_id=chat_id,
            order_code=order.order_code,
            vouchers=[(code, denom) for _, code, _, denom in details],
        )


@router.message(Command("donhang"))
async def cmd_my_orders(message: Message) -> None:
    """Khách chọn mã đơn đã mua để xem lại mã voucher & nhận file barcode HTML."""
    await _send_customer_history(message, message.chat.id)


# =========================
# BÀN PHÍM CỐ ĐỊNH (reply keyboard) — mỗi nút gửi 1 tin text, khớp ở đây
# =========================
@router.message(F.text == BTN_BUY)
async def on_kb_buy(message: Message) -> None:
    database.touch_user(message.chat.id, message.from_user.username or "")
    chat_id = message.chat.id
    await message.answer(
        _shop_text(chat_id), reply_markup=_shop_keyboard(chat_id),
        disable_web_page_preview=True,
    )


@router.message(F.text == BTN_WALLET)
async def on_kb_wallet(message: Message, state: FSMContext) -> None:
    await state.clear()
    database.touch_user(message.chat.id, message.from_user.username or "")
    await message.answer(_wallet_text(message.chat.id), reply_markup=_wallet_keyboard())


@router.message(F.text == BTN_HISTORY)
async def on_kb_history(message: Message) -> None:
    database.touch_user(message.chat.id, message.from_user.username or "")
    await _send_customer_history(message, message.chat.id)


@router.message(F.text == BTN_GUIDE)
async def on_kb_guide(message: Message) -> None:
    database.touch_user(message.chat.id, message.from_user.username or "")
    await message.answer(_GUIDE_MENU_TEXT, reply_markup=_guide_menu_kb())


@router.message(F.text == BTN_SUPPORT)
async def on_kb_support(message: Message) -> None:
    database.touch_user(message.chat.id, message.from_user.username or "")
    await message.answer(_support_text())


# =========================
# TƯƠNG THÍCH NGƯỢC: nút của phiên bản cũ
# =========================
@router.callback_query(F.data.startswith("denom:"))
@router.callback_query(F.data.startswith("qty:"))
async def on_legacy_buttons(cb: CallbackQuery) -> None:
    await cb.answer(
        "Giao diện mua hàng đã được nâng cấp. Gõ /start để mở menu mới nhé!",
        show_alert=True,
    )
