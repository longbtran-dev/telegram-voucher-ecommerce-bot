"""Xử lý lệnh admin: nhập kho, xem kho, đặt giá, cấu hình gian hàng, xác nhận thủ công."""

from __future__ import annotations

import asyncio
import datetime
import html
import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import checker, config, database, payments, report, utils

log = logging.getLogger("admin")
router = Router()
_BUYERS_PAGE_SIZE = 8
_WALLETS_PAGE_SIZE = 7
_WALLET_HISTORY_PAGE_SIZE = 6
_PROXY_PAGE_SIZE = 5


def _is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in config.ADMIN_IDS


class ImportStock(StatesGroup):
    waiting_file = State()


class VoucherCheck(StatesGroup):
    waiting_text = State()
    waiting_file = State()


class ProxyAdmin(StatesGroup):
    waiting_add = State()


class Broadcast(StatesGroup):
    waiting_text = State()


class AdminInput(StatesGroup):
    waiting_price = State()
    waiting_setting = State()


class WalletAdminInput(StatesGroup):
    waiting_chat_id = State()
    waiting_adjustment = State()


class DeleteStock(StatesGroup):
    waiting_codes = State()


class BarcodeBuilder(StatesGroup):
    waiting_codes = State()


def admin_dashboard_text() -> str:
    stock = database.stock_summary()
    stock_label = " · ".join(
        f"{utils.format_k(denom)}: <b>{count}</b>" for denom, count in stock.items()
    )
    waiting = len(database.list_manual_confirmation_orders())
    wallet_customers, wallet_balance = database.wallet_summary()
    return (
        "⚙️ <b>BẢNG ĐIỀU KHIỂN ADMIN</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📦 Kho: {stock_label}\n"
        f"🧾 Đã giao: <b>{database.count_delivered()}</b> đơn\n"
        f"💰 Doanh thu: <b>{utils.format_vnd(database.revenue_delivered())}</b>\n"
        f"👛 Ví khách: <b>{utils.format_vnd(wallet_balance)}</b> "
        f"· <b>{wallet_customers}</b> khách có giao dịch\n"
        f"⏳ Cần kiểm tra: <b>{waiting}</b> đơn\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Chọn chức năng bên dưới:"
    )


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📦 Tồn kho", callback_data="admin:stock"),
            InlineKeyboardButton(text="➕ Nhập kho", callback_data="admin:import"),
        ],
        [
            InlineKeyboardButton(text="🗑 Xoá mã kho", callback_data="admin:delete_stock"),
            InlineKeyboardButton(text="🎫 Tạo HTML Barcode", callback_data="admin:barcode"),
        ],
        [InlineKeyboardButton(text="🧪 Công cụ check mã", callback_data="admin:checker")],
        [
            InlineKeyboardButton(text="📊 Báo cáo", callback_data="admin:report"),
            InlineKeyboardButton(text="🧾 Lịch sử bán", callback_data="admin:history"),
        ],
        [
            InlineKeyboardButton(text="👥 Khách đã mua", callback_data="admin:buyers"),
            InlineKeyboardButton(text="👛 Ví khách hàng", callback_data="admin:wallets"),
        ],
        [
            InlineKeyboardButton(text="💵 Giá bán", callback_data="admin:prices"),
            InlineKeyboardButton(text="🏪 Gian hàng", callback_data="admin:settings"),
        ],
        [
            InlineKeyboardButton(text="📢 Thông báo", callback_data="admin:broadcast"),
            InlineKeyboardButton(text="✅ Xác nhận đơn", callback_data="admin:confirm"),
        ],
        [
            InlineKeyboardButton(text="💳 Trạng thái SePay", callback_data="admin:status"),
            InlineKeyboardButton(text="🎬 Test giao đơn mẫu", callback_data="admin:test"),
        ],
        [InlineKeyboardButton(text="🔄 Làm mới", callback_data="admin:home")],
    ])


def _admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Bảng điều khiển", callback_data="admin:home"),
    ]])


def _admin_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Huỷ thao tác", callback_data="admin:cancel"),
    ]])


async def _edit_or_send_admin(
    message: Message,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    try:
        await message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    except TelegramBadRequest:
        await message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


# =========================
# HELP
# =========================
@router.message(Command("admin", "help"))
async def cmd_admin_help(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(admin_dashboard_text(), reply_markup=admin_menu_keyboard())


@router.callback_query(F.data == "admin:home")
async def on_admin_home(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await state.clear()
    await _edit_or_send_admin(cb.message, admin_dashboard_text(), admin_menu_keyboard())
    await cb.answer()


@router.callback_query(F.data == "admin:cancel")
async def on_admin_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await state.clear()
    await _edit_or_send_admin(cb.message, admin_dashboard_text(), admin_menu_keyboard())
    await cb.answer("Đã huỷ thao tác.")


# =========================
# NHẬP KHO (FILE .TXT HOẶC TIN NHẮN)
# =========================
async def _start_import(message: Message, state: FSMContext) -> None:
    await message.answer(
        "📥 <b>Nhập kho tự động phân loại</b>\n\n"
        "• Gửi trực tiếp <b>tin nhắn chứa mã <code>UB...</code> / link UrBox</b> (mỗi mã 1 dòng), hoặc\n"
        "• Gửi file <b>.txt</b> chứa danh sách mã.\n\n"
        "Bot tự đọc mã và phân loại mệnh giá để nhập nhanh. "
        "Mã sẽ được kiểm tra trên Jollibee ngay trước lúc giao khách.",
        reply_markup=_admin_cancel_keyboard(),
    )
    await state.set_state(ImportStock.waiting_file)


@router.message(Command("nhapkho"))
async def cmd_import(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await _start_import(message, state)


@router.callback_query(F.data == "admin:import")
async def on_admin_import(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await _start_import(cb.message, state)
    await cb.answer("Hãy gửi tin nhắn chứa mã hoặc file .txt")


@router.message(Command("huy"), ImportStock.waiting_file)
async def cmd_cancel_import(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "Đã huỷ nhập kho.", reply_markup=admin_menu_keyboard(),
    )


async def _import_vouchers_from_text(
    message: Message, state: FSMContext, bot: Bot, text: str,
) -> None:
    inputs = checker.extract_inputs(text)
    if not inputs:
        await message.answer(
            "Không tìm thấy link UrBox hoặc mã UB hợp lệ. Vui lòng gửi lại hoặc bấm Huỷ thao tác.",
            reply_markup=_admin_cancel_keyboard(),
        )
        return
    await state.clear()

    status_msg = await message.answer(
        f"⏳ Đang đọc & tự phân loại {len(inputs)} mã..."
    )

    results = await asyncio.to_thread(checker.read_inputs, inputs)

    # Đếm nhập thành công theo từng mệnh giá.
    imported = {d: 0 for d in config.DENOMINATIONS}
    duplicated = errored = unknown = 0
    error_lines: list[str] = []
    for r in results:
        if r.code:
            denom = utils.classify_denom(r.code)
            if denom is None:
                unknown += 1
                if len(error_lines) < 12:
                    error_lines.append(f"• {r.code} — không rõ mệnh giá (bỏ qua)")
                continue
            link = r.urbox_url or (r.input_url if r.input_url.lower().startswith("http") else "")
            if database.add_voucher(denom, r.code, link):
                imported[denom] += 1
            else:
                duplicated += 1
        else:
            errored += 1
            if len(error_lines) < 12:
                error_lines.append(f"• {r.input_url} — {r.error}")

    total_imported = sum(imported.values())
    summary = [
        "📥 <b>Kết quả nhập kho</b>",
        f"✅ Nhập thành công: <b>{total_imported}</b>",
    ]
    for denom in config.DENOMINATIONS:
        if imported[denom]:
            summary.append(f"   • {utils.format_vnd(denom)}: {imported[denom]} mã")
    if duplicated:
        summary.append(f"♻️ Trùng (đã có sẵn): {duplicated}")
    if unknown:
        summary.append(f"❓ Không rõ mệnh giá (bỏ qua): {unknown}")
    if errored:
        summary.append(f"⚠️ Lỗi (bỏ qua): {errored}")
    summary.append("")
    summary.append("📦 <b>Tồn kho hiện tại</b>")
    for denom, count in database.stock_summary().items():
        summary.append(f"   • {utils.format_vnd(denom)}: {count} mã")
    if error_lines:
        summary.append("\n<b>Chi tiết:</b>\n" + "\n".join(error_lines))

    # Báo các khách đã bấm 🔔 chờ những mệnh giá vừa có hàng trở lại.
    notified = await _notify_stock_waiters(bot, imported)
    if notified:
        summary.append(f"\n🔔 Đã báo <b>{notified}</b> khách đang chờ hàng.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Nhập thêm", callback_data="admin:import"),
            InlineKeyboardButton(text="🗑 Xoá mã kho", callback_data="admin:delete_stock"),
        ],
        [InlineKeyboardButton(text="⬅️ Bảng điều khiển", callback_data="admin:home")],
    ])
    await status_msg.edit_text("\n".join(summary), reply_markup=kb)


@router.message(ImportStock.waiting_file, F.document)
async def on_import_file(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    buf = await bot.download(message.document)
    text = buf.read().decode("utf-8", errors="ignore")
    await _import_vouchers_from_text(message, state, bot, text)


@router.message(ImportStock.waiting_file, F.text)
async def on_import_text(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    if (message.text or "").strip().startswith("/"):
        return
    await _import_vouchers_from_text(message, state, bot, message.text or "")


async def _notify_stock_waiters(bot: Bot, imported: dict[int, int]) -> int:
    """Nhắn các khách đăng ký chờ hàng cho mệnh giá vừa nhập. Trả về số tin gửi được."""
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛒 Mua ngay", callback_data="menu:buy"),
    ]])
    sent = 0
    for denom, count in imported.items():
        if count <= 0:
            continue
        for chat_id in database.pop_stock_alerts(denom):
            try:
                await bot.send_message(
                    chat_id,
                    f"🔔 <b>CÓ HÀNG TRỞ LẠI!</b>\n"
                    f"Voucher <b>{utils.format_vnd(denom)}</b> bạn chờ đã về kho "
                    f"(giá {utils.format_vnd(database.get_price(denom))}/cái).\n"
                    "👇 Nhanh tay kẻo hết nhé!",
                    reply_markup=kb,
                )
                sent += 1
            except Exception:  # noqa: BLE001
                pass  # khách chặn bot — bỏ qua
    return sent


# =========================
# CÔNG CỤ CHECK JOLLIBEE
# =========================
def _checker_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔎 Check nhanh", callback_data="admin:check_text"),
            InlineKeyboardButton(text="📄 Check file", callback_data="admin:check_file"),
        ],
        [InlineKeyboardButton(text="📦 Check toàn bộ kho", callback_data="admin:check_stock")],
        [InlineKeyboardButton(text="🌐 Quản lý proxy", callback_data="admin:proxies")],
        [InlineKeyboardButton(text="🌐 Test API & proxy", callback_data="admin:check_probe")],
        [InlineKeyboardButton(text="⬅️ Bảng điều khiển", callback_data="admin:home")],
    ])


def _checker_text() -> str:
    return (
        "🧪 <b>CÔNG CỤ CHECK MÃ JOLLIBEE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "• Nhận trực tiếp mã <code>UB...</code> hoặc link UrBox.\n"
        "• Một cart kiểm tra lần lượt toàn bộ mã.\n"
        "• Nếu rate-limit: giữ kết quả cũ, đổi proxy, tạo cart mới và retry từ mã lỗi.\n"
        "• Route bị rate-limit sẽ cooldown 30 phút và bị bỏ qua trong thời gian đó.\n"
        "• Phân loại: dùng được, đã dùng, hết hạn, chưa hiệu lực, không hợp lệ.\n"
        f"• Proxy đang nạp: <b>{checker.proxy_count()}</b> · "
        f"route cooldown: <b>{checker.cooldown_route_count()}</b>.\n"
        f"• Phiên bản API Jollibee: <b>{html.escape(checker.current_app_version())}</b> (tự động nâng cấp).\n\n"
        "Nhập kho không chạy checker; bot bắt buộc check lại ngay trước khi giao khách."
    )


def _proxy_state_text(seconds: int) -> str:
    if seconds <= 0:
        return "🟢 Sẵn sàng"
    return f"🟠 Cooldown {(seconds + 59) // 60} phút"


def _proxy_page_data(
    page: int,
) -> tuple[str, int, int, list[tuple[int, str, str]]]:
    entries = checker.proxy_entries()
    total_pages = max(1, (len(entries) + _PROXY_PAGE_SIZE - 1) // _PROXY_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _PROXY_PAGE_SIZE
    page_entries = [
        (index, raw, proxy)
        for index, (raw, proxy) in enumerate(entries[start:start + _PROXY_PAGE_SIZE], start=start)
    ]
    direct_state = _proxy_state_text(checker.route_cooldown_remaining(None))
    lines = [
        "🌐 <b>QUẢN LÝ PROXY CHECK MÃ</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"Route IP VPS chính: <b>{direct_state}</b>",
        f"Proxy đang dùng: <b>{checker.proxy_count()}</b>",
        f"File: <code>{html.escape(str(checker.proxy_file_path()))}</code>",
        "Cooldown rate-limit: <b>30 phút</b>",
        "<i>Thông tin proxy bên dưới hiển thị đầy đủ, không che credential.</i>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    if not entries:
        lines.append("<i>Chưa có proxy. Checker đang dùng IP VPS chính.</i>")
    for index, raw, proxy in page_entries:
        lines.extend([
            f"<b>#{index + 1}</b> · {_proxy_state_text(checker.route_cooldown_remaining(proxy))}",
            f"<code>{html.escape(raw)}</code>",
        ])
        if raw != proxy:
            lines.append(f"<i>Route: {html.escape(proxy)}</i>")
    if total_pages > 1:
        lines.append(f"\nTrang <b>{page + 1}/{total_pages}</b>")
    return "\n".join(lines), page, total_pages, page_entries


def _proxy_keyboard(
    page: int,
    total_pages: int,
    entries: list[tuple[int, str, str]],
) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="➕ Thêm proxy", callback_data="admin:proxy_add"),
        InlineKeyboardButton(text="🩺 Test live", callback_data="admin:proxy_test"),
    ]]
    for index, _, _ in entries:
        rows.append([
            InlineKeyboardButton(
                text=f"🗑 Xóa proxy #{index + 1}",
                callback_data=f"admin:proxy_delete:{index}:{page}",
            ),
        ])
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="⬅️", callback_data=f"admin:proxy_page:{page - 1}",
            ))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton(
                text="➡️", callback_data=f"admin:proxy_page:{page + 1}",
            ))
        rows.append(nav)
    rows.extend([
        [InlineKeyboardButton(text="🔄 Làm mới", callback_data=f"admin:proxy_page:{page}")],
        [InlineKeyboardButton(text="⬅️ Công cụ check", callback_data="admin:checker")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_proxy_admin(message: Message, page: int = 0) -> None:
    text, page, total_pages, entries = _proxy_page_data(page)
    await _edit_or_send_admin(message, text, _proxy_keyboard(page, total_pages, entries))


def _check_result_line(index: int, result: checker.CheckResult) -> str:
    icons = {
        "VALID": "✅",
        "USED": "❌",
        "EXPIRED": "⌛",
        "NOT_ACTIVE": "🕒",
        "INVALID": "🚫",
        "REJECTED": "⚠️",
        "NOT_APPLIED": "⚠️",
        "ERROR": "❓",
    }
    status = result.status
    detail_status = result.jollibee_status if status == "ERROR" else status
    route = " · proxy" if result.proxy_used else ""
    code = html.escape(result.code or result.input_url)
    line = f"{index}. {icons.get(status, '❓')} <code>{code}</code> — {checker.status_label(detail_status)}{route}"
    if result.cart_status == "REPLACED_OR_REMOVED":
        line += " · đã áp thành công, sau đó bị mã kế tiếp thay thế trong cart"
    if result.error:
        line += f"\n   <i>{html.escape(result.error[:180])}</i>"
    return line


async def _run_admin_check(message: Message, inputs: list[str], title: str) -> None:
    status_message = await message.answer(
        f"⏳ <b>{html.escape(title)}</b>\nĐang kiểm tra {len(inputs)} mã..."
    )
    results = await asyncio.to_thread(checker.check_inputs, inputs)
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    summary = [
        f"🧪 <b>{html.escape(title)}</b>",
        f"Tổng: <b>{len(results)}</b> · Dùng được: <b>{counts.get('VALID', 0)}</b> · "
        f"Lỗi mã: <b>{sum(counts.get(status, 0) for status in checker.BAD_STATUSES)}</b> · "
        f"Chưa xác minh: <b>{counts.get('ERROR', 0)}</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    lines = [_check_result_line(index, result) for index, result in enumerate(results, start=1)]
    preview_lines = summary.copy()
    shown = 0
    for line in lines:
        if len("\n".join([*preview_lines, line])) > 3700:
            break
        preview_lines.append(line)
        shown += 1
    if shown < len(lines):
        preview_lines.append(
            f"\n<i>Còn {len(lines) - shown} kết quả trong file đính kèm.</i>"
        )
    await status_message.edit_text("\n".join(preview_lines), reply_markup=_checker_keyboard())
    if shown < len(lines):
        plain_lines = [
            re.sub(r"<[^>]+>", "", line).replace("&lt;", "<").replace("&gt;", ">")
            for line in lines
        ]
        filename = f"check_jollibee_{datetime.datetime.now(utils.VN_TZ).strftime('%Y%m%d_%H%M%S')}.txt"
        await message.answer_document(
            BufferedInputFile("\n".join(plain_lines).encode("utf-8"), filename=filename),
            caption=f"📄 Toàn bộ {len(results)} kết quả check.",
        )


@router.callback_query(F.data == "admin:checker")
async def on_admin_checker(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await state.clear()
    await _edit_or_send_admin(cb.message, _checker_text(), _checker_keyboard())
    await cb.answer()


@router.callback_query(F.data == "admin:proxies")
async def on_admin_proxies(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await state.clear()
    await _show_proxy_admin(cb.message)
    await cb.answer()


@router.callback_query(F.data.startswith("admin:proxy_page:"))
async def on_admin_proxy_page(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    try:
        page = max(0, int(cb.data.rsplit(":", 1)[1]))
    except ValueError:
        page = 0
    await _show_proxy_admin(cb.message, page)
    await cb.answer()


@router.callback_query(F.data == "admin:proxy_add")
async def on_admin_proxy_add(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await state.set_state(ProxyAdmin.waiting_add)
    await cb.message.answer(
        "➕ Gửi proxy cần thêm, mỗi dòng một proxy.\n"
        "Hỗ trợ: <code>host:port</code>, <code>host:port:user:pass</code> hoặc URL đầy đủ.\n"
        "Thông tin credential sẽ được lưu nguyên bản trong file proxy trên VPS.",
        reply_markup=_admin_cancel_keyboard(),
    )
    await cb.answer()


@router.message(Command("huy"), ProxyAdmin.waiting_add)
async def cmd_cancel_proxy_add(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("Đã hủy thêm proxy.", reply_markup=_checker_keyboard())


@router.message(ProxyAdmin.waiting_add, F.text)
async def on_admin_proxy_add_input(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    values = message.text.splitlines()
    added, duplicate, invalid = checker.add_proxies(values)
    if not added and not duplicate and not invalid:
        await message.answer("Không nhận được proxy nào. Gửi mỗi dòng một proxy hợp lệ.")
        return
    await state.clear()
    await message.answer(
        "✅ <b>Đã xử lý proxy</b>\n"
        f"Thêm mới: <b>{added}</b>\n"
        f"Trùng: <b>{duplicate}</b>\n"
        f"Không hợp lệ: <b>{invalid}</b>",
    )
    await _show_proxy_admin(message)


@router.callback_query(F.data.startswith("admin:proxy_delete:"))
async def on_admin_proxy_delete(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    try:
        _, _, index_text, page_text = cb.data.split(":", 3)
        index = int(index_text)
        page = max(0, int(page_text))
    except ValueError:
        await cb.answer("Dữ liệu proxy không hợp lệ.", show_alert=True)
        return
    if checker.remove_proxy(index) is None:
        await cb.answer("Proxy này không còn tồn tại, hãy làm mới danh sách.", show_alert=True)
        await _show_proxy_admin(cb.message, page)
        return
    await cb.answer(f"Đã xóa proxy #{index + 1}.")
    await _show_proxy_admin(cb.message, page)
    log.info("Admin %s đã xóa proxy #%s", cb.from_user.id, index + 1)


@router.callback_query(F.data == "admin:proxy_test")
async def on_admin_proxy_test(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await cb.answer("Đang test từng route tạo guest cart...")
    status_message = await cb.message.answer("⏳ Đang kiểm tra IP VPS và từng proxy...")
    results = await asyncio.to_thread(checker.test_routes)
    lines = [
        "🩺 <b>KẾT QUẢ TEST ROUTE CHECKER</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for result in results:
        route = "IP VPS chính" if result["route"] is None else result["route"]
        if result["ok"] and result["cooldown_seconds"]:
            state = (
                f"🟠 Live · cooldown "
                f"{(result['cooldown_seconds'] + 59) // 60} phút"
            )
        elif result["ok"]:
            state = f"✅ Live · {result['latency_ms']} ms"
        elif result["cooldown_seconds"]:
            state = f"🟠 Cooldown {(result['cooldown_seconds'] + 59) // 60} phút"
        else:
            state = f"❌ {checker.status_label(result['status'])}"
        lines.append(f"<b>#{result['index'] + 1}</b> {state}")
        lines.append(f"<code>{html.escape(route)}</code>")
        if result["message"]:
            lines.append(f"<i>{html.escape(result['message'][:160])}</i>")
    preview_lines: list[str] = []
    shown = 0
    for line in lines:
        if len("\n".join([*preview_lines, line])) > 3700:
            break
        preview_lines.append(line)
        shown += 1
    if shown < len(lines):
        preview_lines.append("\n<i>Chi tiết đầy đủ nằm trong file đính kèm.</i>")
    await status_message.edit_text("\n".join(preview_lines), reply_markup=_checker_keyboard())
    if shown < len(lines):
        plain_lines = [html.unescape(re.sub(r"<[^>]+>", "", line)) for line in lines]
        filename = (
            "test_proxy_"
            f"{datetime.datetime.now(utils.VN_TZ).strftime('%Y%m%d_%H%M%S')}.txt"
        )
        await cb.message.answer_document(
            BufferedInputFile("\n".join(plain_lines).encode("utf-8"), filename=filename),
            caption="📄 Kết quả test đầy đủ từng route.",
        )


@router.callback_query(F.data == "noop")
async def on_admin_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(F.data == "admin:check_text")
async def on_admin_check_text(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await state.set_state(VoucherCheck.waiting_text)
    await cb.message.answer(
        "🔎 Gửi một hoặc nhiều mã <code>UB...</code>/link UrBox trong tin nhắn. "
        "Không giới hạn số lượng.",
        reply_markup=_admin_cancel_keyboard(),
    )
    await cb.answer()


@router.message(VoucherCheck.waiting_text, F.text)
async def on_admin_check_text_input(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    inputs = checker.extract_inputs(message.text)
    if not inputs:
        await message.answer("Không tìm thấy link UrBox hoặc mã UB hợp lệ.")
        return
    await state.clear()
    await _run_admin_check(message, inputs, "Kết quả check nhanh")


@router.callback_query(F.data == "admin:check_file")
async def on_admin_check_file(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await state.set_state(VoucherCheck.waiting_file)
    await cb.message.answer(
        "📄 Gửi file <b>.txt</b> chứa mã/link cần check. Không giới hạn số lượng.",
        reply_markup=_admin_cancel_keyboard(),
    )
    await cb.answer()


@router.message(VoucherCheck.waiting_file, F.document)
async def on_admin_check_file_input(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    buf = await bot.download(message.document)
    inputs = checker.extract_inputs(buf.read().decode("utf-8", errors="ignore"))
    if not inputs:
        await message.answer("File không có link UrBox hoặc mã UB hợp lệ.")
        return
    await state.clear()
    await _run_admin_check(message, inputs, "Kết quả check file")


@router.callback_query(F.data == "admin:check_stock")
async def on_admin_check_stock(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    vouchers = database.list_available_vouchers()
    if not vouchers:
        await cb.answer("Kho đang trống.", show_alert=True)
        return
    await cb.answer("Bắt đầu check toàn bộ kho...")
    await _run_admin_check(cb.message, [code for _, code, _ in vouchers], "Rà soát toàn bộ kho")


@router.callback_query(F.data == "admin:check_probe")
async def on_admin_check_probe(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await cb.answer("Đang tạo guest cart thử...")
    result = await asyncio.to_thread(checker.probe)
    if result["ok"]:
        route = "proxy fallback" if result["proxy_used"] else "IP VPS chính"
        text = (
            "✅ <b>CHECKER JOLLIBEE SẴN SÀNG</b>\n"
            f"App version: <code>{result['app_version']}</code>\n"
            f"SKU test: <code>{result['sku']}</code>\n"
            f"Kết nối: <b>{route}</b>\n"
            f"Proxy đã nạp: <b>{result['proxy_count']}</b>"
        )
    else:
        text = (
            "❌ <b>CHECKER JOLLIBEE CHƯA SẴN SÀNG</b>\n"
            f"Trạng thái: <b>{checker.status_label(result['status'])}</b>\n"
            f"Proxy đã nạp: <b>{result['proxy_count']}</b>"
        )
    await _edit_or_send_admin(cb.message, text, _checker_keyboard())


# =========================
# XEM KHO
# =========================
async def _send_stock(message: Message) -> None:
    summary = database.stock_summary()
    rows = [
        [utils.format_vnd(denom), f"{count} mã", utils.format_vnd(database.get_price(denom))]
        for denom, count in summary.items()
    ]
    table = utils.mono_table(["Mệnh giá", "Còn lại", "Giá bán"], rows)
    stock_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Nhập kho", callback_data="admin:import"),
            InlineKeyboardButton(text="🗑 Xoá mã kho", callback_data="admin:delete_stock"),
        ],
        [InlineKeyboardButton(text="⬅️ Bảng điều khiển", callback_data="admin:home")],
    ])
    await message.answer(
        "📦 <b>TỒN KHO HIỆN TẠI</b>\n" + table,
        reply_markup=stock_kb,
    )

    vouchers = database.list_available_vouchers()
    if not vouchers:
        await message.answer("Kho hiện đang trống — chưa có mã nào để xuất.")
        return

    file_lines: list[str] = []
    for denom in config.DENOMINATIONS:
        group = [(code, link) for item_denom, code, link in vouchers if item_denom == denom]
        if not group:
            continue
        file_lines.append(f"# ===== {utils.format_vnd(denom)} — {len(group)} mã =====")
        for code, link in group:
            file_lines.append(link or code)
        file_lines.append("")

    fname = f"kho_{datetime.datetime.now(utils.VN_TZ).strftime('%Y%m%d_%H%M')}.txt"
    doc = BufferedInputFile(("\n".join(file_lines)).encode("utf-8"), filename=fname)
    await message.answer_document(
        doc,
        caption=f"📄 <b>{len(vouchers)} link</b> còn trong kho — file sao lưu kho hiện tại.",
    )


@router.message(Command("kho"))
async def cmd_stock(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    await _send_stock(message)


@router.callback_query(F.data == "admin:stock")
async def on_admin_stock(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await cb.answer("Đang xuất tồn kho...")
    await _send_stock(cb.message)


# =========================
# LỊCH SỬ ĐÃ BÁN
# =========================
async def _send_sold_history(message: Message, limit: int = 30) -> None:
    orders = database.list_delivered_orders(limit)
    total_cnt = database.count_delivered()
    total_rev = database.revenue_delivered()

    if not orders:
        await message.answer("Chưa có đơn nào được bán.", reply_markup=_admin_back_keyboard())
        return

    orders_data = []
    for order in orders:
        timestamp = order.delivered_at or order.created_at
        orders_data.append({
            "order_code": order.order_code,
            "time": utils.format_hm(timestamp),
            "date": utils.format_dmy(timestamp),
            "buyer": utils.buyer_label(
                order.buyer_username, order.buyer_name, order.chat_id,
            ),
            "quantity": order.quantity,
            "denom": order.denom,
            "total_amount": order.total_amount,
            "vouchers": database.get_order_vouchers(order.id),
        })

    shop_title = database.get_config("shop_title", "Jollibee")
    html_str = report.render_history_html(
        orders_data, total_cnt, total_rev, shop_title,
        sold_by_denom=database.sold_qty_by_denom(),
    )
    fname = f"lichsu_{datetime.datetime.now(utils.VN_TZ).strftime('%Y%m%d_%H%M')}.html"
    doc = BufferedInputFile(html_str.encode("utf-8"), filename=fname)
    await message.answer_document(
        doc,
        caption=(
            f"📊 <b>{len(orders)} đơn gần nhất</b>\n"
            f"Tổng đã bán: <b>{total_cnt}</b> đơn · "
            f"<b>{utils.format_vnd(total_rev)}</b>"
        ),
        reply_markup=_admin_back_keyboard(),
    )


@router.message(Command("lichsu", "dahang"))
async def cmd_sold(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id):
        return
    limit = 30
    if command.args and command.args.strip().isdigit():
        limit = max(1, min(int(command.args.strip()), 200))
    await _send_sold_history(message, limit)


@router.callback_query(F.data == "admin:history")
async def on_admin_history(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="30 đơn", callback_data="admin:history_limit:30"),
            InlineKeyboardButton(text="100 đơn", callback_data="admin:history_limit:100"),
            InlineKeyboardButton(text="200 đơn", callback_data="admin:history_limit:200"),
        ],
        [InlineKeyboardButton(text="⬅️ Quay lại", callback_data="admin:home")],
    ])
    await _edit_or_send_admin(cb.message, "🧾 Chọn số đơn muốn xuất:", keyboard)
    await cb.answer()


@router.callback_query(F.data.startswith("admin:history_limit:"))
async def on_admin_history_limit(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    limit = max(1, min(int(cb.data.rsplit(":", 1)[1]), 200))
    await cb.answer("Đang tạo file lịch sử...")
    await _send_sold_history(cb.message, limit)


# =========================
# KHÁCH ĐÃ MUA
# =========================
def _buyer_identity(summary: database.BuyerSummary) -> str:
    username = f"@{html.escape(summary.buyer_username)}" if summary.buyer_username else ""
    name = html.escape(summary.buyer_name)
    if username and name:
        return f"{name} · {username}"
    return username or name or f"ID {summary.chat_id}"


def _buyers_page_text(page: int) -> tuple[str, int, int]:
    total_buyers = database.count_buyers()
    total_pages = max(1, (total_buyers + _BUYERS_PAGE_SIZE - 1) // _BUYERS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    buyers = database.list_buyer_summaries(
        limit=_BUYERS_PAGE_SIZE,
        offset=page * _BUYERS_PAGE_SIZE,
    )
    lines = [
        "👥 <b>KHÁCH ĐÃ MUA</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"📊 Tổng cộng: <b>{total_buyers}</b> khách hàng",
        f"📄 Trang: <b>{page + 1}/{total_pages}</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    if not buyers:
        lines.append("<i>Chưa có khách hàng nào mua voucher.</i>")
        return "\n".join(lines), page, total_pages

    for position, buyer in enumerate(buyers, start=page * _BUYERS_PAGE_SIZE + 1):
        lines.append(
            "<blockquote>"
            f"👤 <b>{position:02d}. {_buyer_identity(buyer)}</b>\n"
            f"🆔 <code>{buyer.chat_id}</code>\n"
            f"🧾 Đã mua: <b>{buyer.order_count}</b> đơn · "
            f"<b>{buyer.voucher_quantity}</b> voucher\n"
            f"💰 Tổng chi: <b>{utils.format_vnd(buyer.total_spent)}</b>\n"
            f"🕒 Gần nhất: <b>{utils.format_time(buyer.latest_purchase_at)}</b>"
            "</blockquote>"
        )
    return "\n".join(lines), page, total_pages


def _buyers_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(
            text="⬅️ Trang trước", callback_data=f"admin:buyers:{page - 1}",
        ))
    navigation.append(InlineKeyboardButton(
        text=f"{page + 1}/{total_pages}", callback_data=f"admin:buyers:{page}",
    ))
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton(
            text="Trang sau ➡️", callback_data=f"admin:buyers:{page + 1}",
        ))
    return InlineKeyboardMarkup(inline_keyboard=[
        navigation,
        [InlineKeyboardButton(text="⬅️ Bảng điều khiển", callback_data="admin:home")],
    ])


@router.callback_query(F.data.startswith("admin:buyers"))
async def on_admin_buyers(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    parts = cb.data.split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    text, page, total_pages = _buyers_page_text(page)
    await _edit_or_send_admin(cb.message, text, _buyers_keyboard(page, total_pages))
    await cb.answer()


# =========================
# VÍ KHÁCH HÀNG
# =========================
def _wallet_customer_identity(customer: database.WalletCustomer) -> str:
    username = f"@{html.escape(customer.username)}" if customer.username else ""
    name = html.escape(customer.buyer_name)
    if username and name:
        return f"{name} · {username}"
    return username or name or f"ID {customer.chat_id}"


def _wallets_page_data(
    page: int,
) -> tuple[str, int, int, list[database.WalletCustomer]]:
    total_customers = database.count_wallet_customers()
    total_pages = max(1, (total_customers + _WALLETS_PAGE_SIZE - 1) // _WALLETS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    customers = database.list_wallet_customers(
        limit=_WALLETS_PAGE_SIZE,
        offset=page * _WALLETS_PAGE_SIZE,
    )
    active_customers, total_balance = database.wallet_summary()
    lines = [
        "👛 <b>VÍ KHÁCH HÀNG</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"Tổng số dư: <b>{utils.format_vnd(total_balance)}</b>",
        f"Khách có giao dịch ví: <b>{active_customers}</b>",
        f"Tất cả khách: <b>{total_customers}</b> · Trang <b>{page + 1}/{total_pages}</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    if not customers:
        lines.append("<i>Chưa có khách hàng nào.</i>")
    for position, customer in enumerate(
        customers, start=page * _WALLETS_PAGE_SIZE + 1,
    ):
        lines.append(
            "<blockquote>"
            f"👤 <b>{position:02d}. {_wallet_customer_identity(customer)}</b>\n"
            f"🆔 <code>{customer.chat_id}</code>\n"
            f"💰 Số dư: <b>{utils.format_vnd(customer.balance)}</b> · "
            f"{customer.transaction_count} giao dịch"
            "</blockquote>"
        )
    return "\n".join(lines), page, total_pages, customers


def _wallets_keyboard(
    page: int,
    total_pages: int,
    customers: list[database.WalletCustomer],
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"👤 {customer.chat_id} · {utils.format_vnd(customer.balance)}",
            callback_data=f"admin:wallet:{customer.chat_id}:0",
        )]
        for customer in customers
    ]
    rows.append([InlineKeyboardButton(
        text="🔎 Tìm bằng Telegram ID", callback_data="admin:wallet_lookup",
    )])
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(
            text="⬅️ Trang trước", callback_data=f"admin:wallets:{page - 1}",
        ))
    navigation.append(InlineKeyboardButton(
        text=f"{page + 1}/{total_pages}", callback_data=f"admin:wallets:{page}",
    ))
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton(
            text="Trang sau ➡️", callback_data=f"admin:wallets:{page + 1}",
        ))
    rows.append(navigation)
    rows.append([InlineKeyboardButton(
        text="⬅️ Bảng điều khiển", callback_data="admin:home",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_wallets(message: Message, page: int = 0) -> None:
    text, page, total_pages, customers = _wallets_page_data(page)
    await _edit_or_send_admin(
        message,
        text,
        _wallets_keyboard(page, total_pages, customers),
    )


def _wallet_transaction_label(kind: str) -> str:
    return {
        "topup": "Nạp tiền SePay",
        "purchase": "Thanh toán đơn",
        "admin_credit": "Admin cộng tiền",
        "admin_debit": "Admin trừ tiền",
        "admin_set": "Admin đặt số dư",
    }.get(kind, kind)


def _wallet_customer_detail_data(chat_id: int, page: int) -> tuple[str, int, int] | None:
    customer = database.get_wallet_customer(chat_id)
    if customer is None:
        return None
    total_transactions = database.count_wallet_transactions(chat_id)
    total_pages = max(
        1,
        (total_transactions + _WALLET_HISTORY_PAGE_SIZE - 1)
        // _WALLET_HISTORY_PAGE_SIZE,
    )
    page = max(0, min(page, total_pages - 1))
    transactions = database.list_wallet_transactions(
        chat_id,
        limit=_WALLET_HISTORY_PAGE_SIZE,
        offset=page * _WALLET_HISTORY_PAGE_SIZE,
    )
    lines = [
        "👛 <b>CHI TIẾT VÍ KHÁCH HÀNG</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"👤 {_wallet_customer_identity(customer)}",
        f"🆔 <code>{customer.chat_id}</code>",
        f"💰 Số dư: <b>{utils.format_vnd(customer.balance)}</b>",
        f"📜 Lịch sử: <b>{total_transactions}</b> giao dịch · "
        f"trang <b>{page + 1}/{total_pages}</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    if not transactions:
        lines.append("<i>Khách chưa có giao dịch ví.</i>")
    for transaction in transactions:
        sign = "+" if transaction.amount > 0 else "−"
        lines.append(
            f"• <b>{sign}{utils.format_vnd(abs(transaction.amount))}</b> — "
            f"{html.escape(_wallet_transaction_label(transaction.kind))}\n"
            f"  <i>{utils.format_time(transaction.created_at)}</i>"
        )
        if transaction.reference:
            lines.append(f"  Mã: <code>{html.escape(transaction.reference)}</code>")
        if transaction.note:
            lines.append(f"  Lý do: {html.escape(transaction.note)}")
        if transaction.admin_id:
            lines.append(f"  Admin: <code>{transaction.admin_id}</code>")
    return "\n".join(lines), page, total_pages


def _wallet_customer_keyboard(
    chat_id: int, page: int, total_pages: int,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="➕ Cộng tiền", callback_data=f"admin:wallet_adjust:add:{chat_id}",
            ),
            InlineKeyboardButton(
                text="➖ Trừ tiền", callback_data=f"admin:wallet_adjust:subtract:{chat_id}",
            ),
        ],
        [InlineKeyboardButton(
            text="🎯 Đặt số dư", callback_data=f"admin:wallet_adjust:set:{chat_id}",
        )],
    ]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(
            text="⬅️ Mới hơn", callback_data=f"admin:wallet:{chat_id}:{page - 1}",
        ))
    navigation.append(InlineKeyboardButton(
        text=f"{page + 1}/{total_pages}", callback_data=f"admin:wallet:{chat_id}:{page}",
    ))
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton(
            text="Cũ hơn ➡️", callback_data=f"admin:wallet:{chat_id}:{page + 1}",
        ))
    rows.append(navigation)
    rows.append([InlineKeyboardButton(
        text="⬅️ Danh sách ví", callback_data="admin:wallets",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_wallet_customer(message: Message, chat_id: int, page: int = 0) -> bool:
    data = _wallet_customer_detail_data(chat_id, page)
    if data is None:
        return False
    text, page, total_pages = data
    await _edit_or_send_admin(
        message,
        text,
        _wallet_customer_keyboard(chat_id, page, total_pages),
    )
    return True


@router.callback_query(F.data.startswith("admin:wallets"))
async def on_admin_wallets(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    parts = cb.data.split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    await _show_wallets(cb.message, page)
    await cb.answer()


@router.callback_query(F.data.startswith("admin:wallet:"))
async def on_admin_wallet_customer(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    parts = cb.data.split(":")
    if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
        await cb.answer("Dữ liệu ví không hợp lệ.", show_alert=True)
        return
    if not await _show_wallet_customer(cb.message, int(parts[2]), int(parts[3])):
        await cb.answer("Không tìm thấy khách hàng.", show_alert=True)
        return
    await cb.answer()


@router.callback_query(F.data == "admin:wallet_lookup")
async def on_admin_wallet_lookup(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await state.set_state(WalletAdminInput.waiting_chat_id)
    await cb.message.answer(
        "🔎 Gửi <b>Telegram ID</b> của khách cần xem ví.",
        reply_markup=_admin_cancel_keyboard(),
    )
    await cb.answer()


@router.message(WalletAdminInput.waiting_chat_id, F.text)
async def on_admin_wallet_lookup_text(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw_chat_id = message.text.strip()
    if not raw_chat_id.isdigit() or int(raw_chat_id) <= 0:
        await message.answer(
            "Telegram ID không hợp lệ. Hãy gửi một dãy số, ví dụ <code>123456789</code>.",
            reply_markup=_admin_cancel_keyboard(),
        )
        return
    chat_id = int(raw_chat_id)
    if not database.wallet_customer_exists(chat_id):
        await message.answer(
            f"Không tìm thấy khách <code>{chat_id}</code> trong dữ liệu bot.",
            reply_markup=_admin_cancel_keyboard(),
        )
        return
    await state.clear()
    if not await _show_wallet_customer(message, chat_id):
        await message.answer("Không tải được thông tin ví khách hàng.")


@router.callback_query(F.data.startswith("admin:wallet_adjust:"))
async def on_admin_wallet_adjust(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    parts = cb.data.split(":")
    if len(parts) != 4 or parts[2] not in {"add", "subtract", "set"} or not parts[3].isdigit():
        await cb.answer("Thao tác ví không hợp lệ.", show_alert=True)
        return
    action = parts[2]
    chat_id = int(parts[3])
    customer = database.get_wallet_customer(chat_id)
    if customer is None:
        await cb.answer("Không tìm thấy khách hàng.", show_alert=True)
        return
    labels = {
        "add": "cộng thêm vào ví",
        "subtract": "trừ khỏi ví",
        "set": "đặt số dư ví thành",
    }
    await state.set_state(WalletAdminInput.waiting_adjustment)
    await state.update_data(wallet_action=action, wallet_chat_id=chat_id)
    await cb.message.answer(
        f"👛 Khách: <code>{chat_id}</code>\n"
        f"Số dư hiện tại: <b>{utils.format_vnd(customer.balance)}</b>\n\n"
        f"Nhập số tiền muốn <b>{labels[action]}</b>. Có thể kèm lý do sau dấu <code>|</code>.\n"
        "Ví dụ: <code>50000 | bồi hoàn đơn lỗi</code>.",
        reply_markup=_admin_cancel_keyboard(),
    )
    await cb.answer()


@router.message(WalletAdminInput.waiting_adjustment, F.text)
async def on_admin_wallet_adjust_text(
    message: Message, state: FSMContext, bot: Bot,
) -> None:
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    action = str(data.get("wallet_action") or "")
    chat_id = int(data.get("wallet_chat_id") or 0)
    if action not in {"add", "subtract", "set"} or chat_id <= 0:
        await state.clear()
        await message.answer("Phiên điều chỉnh ví đã hết hạn.", reply_markup=admin_menu_keyboard())
        return

    amount_text, separator, note_text = message.text.partition("|")
    amount = utils.parse_vnd_amount(amount_text, allow_zero=action == "set")
    if amount is None or amount > 100_000_000_000:
        await message.answer(
            "Số tiền không hợp lệ. Ví dụ: <code>50000 | lý do</code>.",
            reply_markup=_admin_cancel_keyboard(),
        )
        return
    note = note_text.strip() if separator else ""
    default_notes = {
        "add": "Admin cộng số dư",
        "subtract": "Admin trừ số dư",
        "set": f"Admin đặt số dư thành {amount}",
    }
    note = (note or default_notes[action])[:300]
    before = database.wallet_balance(chat_id)
    if action == "set":
        status, balance = database.set_wallet_balance(
            chat_id, amount, message.from_user.id, note,
        )
    else:
        delta = amount if action == "add" else -amount
        status, balance = database.adjust_wallet_balance(
            chat_id, delta, message.from_user.id, note,
        )
    if status == "insufficient":
        await message.answer(
            f"Không thể trừ {utils.format_vnd(amount)} vì ví chỉ có "
            f"<b>{utils.format_vnd(balance)}</b>.",
            reply_markup=_admin_cancel_keyboard(),
        )
        return
    if status not in {"applied", "unchanged"}:
        await message.answer("Không thể điều chỉnh số dư ví. Vui lòng thử lại.")
        return

    await state.clear()
    action_text = {
        "add": f"được cộng {utils.format_vnd(amount)}",
        "subtract": f"bị trừ {utils.format_vnd(amount)}",
        "set": f"được đặt thành {utils.format_vnd(amount)}",
    }[action]
    if status == "applied":
        try:
            await bot.send_message(
                chat_id,
                "👛 <b>SỐ DƯ VÍ ĐÃ ĐƯỢC ĐIỀU CHỈNH</b>\n"
                f"Ví của bạn {action_text}.\n"
                f"Số dư trước: <b>{utils.format_vnd(before)}</b>\n"
                f"Số dư mới: <b>{utils.format_vnd(balance)}</b>\n"
                f"Lý do: {html.escape(note)}",
            )
        except Exception as exc:  # noqa: BLE001
            log.info("Không gửi được thông báo điều chỉnh ví cho %s: %s", chat_id, exc)
    result = (
        "Số dư không thay đổi."
        if status == "unchanged"
        else f"Đã cập nhật từ {utils.format_vnd(before)} thành {utils.format_vnd(balance)}."
    )
    detail = _wallet_customer_detail_data(chat_id, 0)
    if detail is None:
        await message.answer(f"✅ {result}", reply_markup=_admin_back_keyboard())
        return
    text, page, total_pages = detail
    await message.answer(
        f"✅ <b>{result}</b>\nLý do: {html.escape(note)}\n\n{text}",
        reply_markup=_wallet_customer_keyboard(chat_id, page, total_pages),
    )


# =========================
# ĐẶT GIÁ
# =========================
def _parse_denom(token: str) -> int | None:
    """Chấp nhận '100', '100k', '100000' -> 100000; kiểm tra thuộc DENOMINATIONS."""
    token = token.lower().replace("k", "").strip()
    if not token.isdigit():
        return None
    val = int(token)
    if val < 1000:
        val *= 1000
    return val if val in config.DENOMINATIONS else None


def _parse_money(text: str) -> int | None:
    digits = "".join(char for char in (text or "") if char.isdigit())
    if not digits:
        return None
    amount = int(digits)
    if amount < 1000:
        amount *= 1000
    return amount if amount > 0 else None


def _prices_text() -> str:
    lines = ["💵 <b>GIÁ BÁN HIỆN TẠI</b>", "━━━━━━━━━━━━━━━━━━"]
    for denom in config.DENOMINATIONS:
        lines.append(
            f"• Voucher {utils.format_k(denom)}: "
            f"<b>{utils.format_vnd(database.get_price(denom))}</b>"
        )
    lines.append("\nChọn mệnh giá để điều chỉnh:")
    return "\n".join(lines)


def _prices_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"✏️ Đổi giá voucher {utils.format_k(denom)}",
            callback_data=f"admin:price:{denom}",
        )]
        for denom in config.DENOMINATIONS
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Quay lại", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("gia"))
async def cmd_price(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id):
        return
    args = (command.args or "").split()
    if not args:
        await message.answer(_prices_text(), reply_markup=_prices_keyboard())
        return

    if len(args) != 2:
        await message.answer("Cú pháp: <code>/gia &lt;mệnh giá&gt; &lt;giá bán&gt;</code>, "
                             "vd <code>/gia 100 80000</code>")
        return
    denom = _parse_denom(args[0])
    if denom is None:
        await message.answer(
            f"Mệnh giá không hợp lệ. Chỉ hỗ trợ: "
            f"{', '.join(utils.format_vnd(d) for d in config.DENOMINATIONS)}."
        )
        return
    if not args[1].isdigit():
        await message.answer("Giá bán phải là số. Vd: <code>/gia 100 80000</code>")
        return

    database.set_price(denom, int(args[1]))
    await message.answer(
        f"✅ Đã đặt giá {utils.format_vnd(denom)} → "
        f"<b>{utils.format_vnd(int(args[1]))}</b>.",
        reply_markup=_prices_keyboard(),
    )


@router.callback_query(F.data == "admin:prices")
async def on_admin_prices(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await _edit_or_send_admin(cb.message, _prices_text(), _prices_keyboard())
    await cb.answer()


@router.callback_query(F.data.startswith("admin:price:"))
async def on_admin_price_detail(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    denom = int(cb.data.rsplit(":", 1)[1])
    if denom not in config.DENOMINATIONS:
        await cb.answer("Mệnh giá không hợp lệ.", show_alert=True)
        return
    current = database.get_price(denom)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="➖ 5.000đ", callback_data=f"admin:price_adjust:{denom}:-5000",
            ),
            InlineKeyboardButton(
                text="➕ 5.000đ", callback_data=f"admin:price_adjust:{denom}:5000",
            ),
        ],
        [InlineKeyboardButton(
            text="⌨️ Nhập giá khác", callback_data=f"admin:price_input:{denom}",
        )],
        [InlineKeyboardButton(text="⬅️ Danh sách giá", callback_data="admin:prices")],
    ])
    await _edit_or_send_admin(
        cb.message,
        f"💵 Voucher <b>{utils.format_vnd(denom)}</b>\n"
        f"Giá hiện tại: <b>{utils.format_vnd(current)}</b>",
        keyboard,
    )
    await cb.answer()


@router.callback_query(F.data.startswith("admin:price_adjust:"))
async def on_admin_price_adjust(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    _, _, _, denom_raw, delta_raw = cb.data.split(":")
    denom = int(denom_raw)
    new_price = max(1_000, database.get_price(denom) + int(delta_raw))
    database.set_price(denom, new_price)
    await cb.answer(f"Đã đổi thành {utils.format_vnd(new_price)}")
    await _edit_or_send_admin(cb.message, _prices_text(), _prices_keyboard())


@router.callback_query(F.data.startswith("admin:price_input:"))
async def on_admin_price_input(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    denom = int(cb.data.rsplit(":", 1)[1])
    if denom not in config.DENOMINATIONS:
        await cb.answer("Mệnh giá không hợp lệ.", show_alert=True)
        return
    await state.set_state(AdminInput.waiting_price)
    await state.update_data(denom=denom)
    await cb.message.answer(
        f"⌨️ Gửi giá bán mới cho voucher <b>{utils.format_k(denom)}</b>.\n"
        "Ví dụ: <code>75000</code> hoặc <code>75k</code>.",
        reply_markup=_admin_cancel_keyboard(),
    )
    await cb.answer()


@router.message(AdminInput.waiting_price, F.text)
async def on_admin_price_text(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    amount = _parse_money(message.text)
    if amount is None:
        await message.answer(
            "Giá không hợp lệ. Hãy gửi số như <code>75000</code> hoặc <code>75k</code>.",
            reply_markup=_admin_cancel_keyboard(),
        )
        return
    data = await state.get_data()
    denom = int(data["denom"])
    database.set_price(denom, amount)
    await state.clear()
    await message.answer(
        f"✅ Đã đổi giá voucher {utils.format_k(denom)} thành "
        f"<b>{utils.format_vnd(amount)}</b>.\n\n{_prices_text()}",
        reply_markup=_prices_keyboard(),
    )


# =========================
# CẤU HÌNH GIAN HÀNG
# =========================
_SETTING_FIELDS = {
    "title": ("shop_title", "tiêu đề gian hàng"),
    "link": ("shop_link", "link gian hàng"),
    "support": ("support_contact", "Telegram hỗ trợ"),
    "phone": ("support_phone", "số điện thoại hỗ trợ"),
}


def _settings_text() -> str:
    title = html.escape(database.get_config("shop_title", "Jollibee"))
    link = html.escape(database.get_config("shop_link", "") or "Chưa đặt")
    support = html.escape(database.get_config("support_contact", "@YourSupportBot"))
    phone = html.escape(database.get_config("support_phone", "0900000000"))
    return (
        "🏪 <b>CẤU HÌNH GIAN HÀNG</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🏷 Tiêu đề: <b>{title}</b>\n"
        f"🔗 Link: {link}\n"
        f"💬 Telegram: <b>{support}</b>\n"
        f"☎️ Điện thoại: <b>{phone}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Chọn mục muốn thay đổi:"
    )


def _settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏷 Đổi tiêu đề", callback_data="admin:setting:title"),
            InlineKeyboardButton(text="🔗 Đổi link", callback_data="admin:setting:link"),
        ],
        [
            InlineKeyboardButton(
                text="💬 Đổi Telegram", callback_data="admin:setting:support",
            ),
            InlineKeyboardButton(text="☎️ Đổi SĐT", callback_data="admin:setting:phone"),
        ],
        [InlineKeyboardButton(text="🗑 Xoá link", callback_data="admin:setting_delete_link")],
        [InlineKeyboardButton(text="⬅️ Quay lại", callback_data="admin:home")],
    ])


@router.callback_query(F.data == "admin:settings")
async def on_admin_settings(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await _edit_or_send_admin(cb.message, _settings_text(), _settings_keyboard())
    await cb.answer()


@router.callback_query(F.data.startswith("admin:setting:"))
async def on_admin_setting_pick(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    field = cb.data.rsplit(":", 1)[1]
    if field not in _SETTING_FIELDS:
        await cb.answer("Mục cài đặt không hợp lệ.", show_alert=True)
        return
    key, label = _SETTING_FIELDS[field]
    current = database.get_config(key, "") or "Chưa đặt"
    await state.set_state(AdminInput.waiting_setting)
    await state.update_data(setting_key=key, setting_label=label)
    await cb.message.answer(
        f"✏️ Gửi <b>{label}</b> mới.\n"
        f"Hiện tại: <code>{html.escape(current)}</code>\n\n"
        "Riêng link, gửi <code>xoá</code> nếu muốn bỏ link.",
        reply_markup=_admin_cancel_keyboard(),
    )
    await cb.answer()


@router.callback_query(F.data == "admin:setting_delete_link")
async def on_admin_delete_link(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    database.set_config("shop_link", "")
    await cb.answer("Đã xoá link gian hàng.")
    await _edit_or_send_admin(cb.message, _settings_text(), _settings_keyboard())


@router.message(AdminInput.waiting_setting, F.text)
async def on_admin_setting_text(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = str(data["setting_key"])
    label = str(data["setting_label"])
    value = message.text.strip()
    if key == "shop_link" and value.lower() in {"xoa", "xoá", "-"}:
        value = ""
    if key == "shop_title" and not value:
        await message.answer("Tiêu đề không được để trống.", reply_markup=_admin_cancel_keyboard())
        return
    database.set_config(key, value)
    await state.clear()
    display_value = html.escape(value) if value else "Đã xoá"
    await message.answer(
        f"✅ Đã cập nhật {label}: <b>{display_value}</b>\n\n{_settings_text()}",
        reply_markup=_settings_keyboard(),
        disable_web_page_preview=True,
    )


@router.message(Command("tieude"))
async def cmd_set_title(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id):
        return
    title = (command.args or "").strip()
    if not title:
        await message.answer("Cú pháp: <code>/tieude Tên gian hàng của bạn</code>")
        return
    database.set_config("shop_title", title)
    await message.answer(f"✅ Đã đổi tiêu đề gian hàng thành:\n<b>{title}</b>")


@router.message(Command("link"))
async def cmd_set_link(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id):
        return
    link = (command.args or "").strip()
    database.set_config("shop_link", link)
    if link:
        await message.answer(f"✅ Đã đặt link gian hàng:\n{link}")
    else:
        await message.answer("✅ Đã xoá link gian hàng.")


@router.message(Command("hotro"))
async def cmd_set_support(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id):
        return
    contact = (command.args or "").strip()
    if not contact:
        current = database.get_config("support_contact", "@YourSupportBot")
        await message.answer(
            f"Liên hệ hỗ trợ hiện tại: <b>{current}</b>\n"
            "Đổi bằng: <code>/hotro @username_moi</code>"
        )
        return
    database.set_config("support_contact", contact)
    await message.answer(f"✅ Đã đặt liên hệ hỗ trợ: <b>{contact}</b>")


@router.message(Command("sdt"))
async def cmd_set_phone(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id):
        return
    phone = (command.args or "").strip()
    if not phone:
        current = database.get_config("support_phone", "0900000000")
        await message.answer(
            f"Số điện thoại hỗ trợ hiện tại: <b>{current}</b>\n"
            "Đổi bằng: <code>/sdt 0912345678</code>"
        )
        return
    database.set_config("support_phone", phone)
    await message.answer(f"✅ Đã đặt số điện thoại hỗ trợ: <b>{phone}</b>")


# =========================
# XÁC NHẬN THỦ CÔNG
# =========================
@router.message(Command("xacnhan"))
async def cmd_confirm(message: Message, command: CommandObject, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    code = (command.args or "").strip().upper()
    if not code:
        await message.answer("Cú pháp: <code>/xacnhan JBXXXXXX</code>")
        return
    order = database.get_order_by_code(code)
    if order is None:
        await message.answer(f"Không tìm thấy đơn <code>{code}</code>.")
        return
    if order.status not in {"pending", "paid_pending"}:
        await message.answer(
            f"Đơn <code>{code}</code> đang ở trạng thái "
            f"<b>{order.status}</b>, không thể xác nhận."
        )
        return
    delivered = await payments.deliver_order(
        bot, order, tran_id=order.tran_id or f"MANUAL-{code}",
    )
    if delivered:
        await message.answer(
            f"✅ Đã xác nhận & giao thủ công đơn <code>{code}</code>.",
            reply_markup=_admin_back_keyboard(),
        )
    else:
        await message.answer(
            f"⚠️ Chưa thể giao đơn <code>{code}</code>. Kiểm tra tồn kho và log.",
            reply_markup=_admin_back_keyboard(),
        )


async def _show_manual_confirmation_orders(message: Message) -> None:
    orders = database.list_manual_confirmation_orders(limit=20)
    if not orders:
        await _edit_or_send_admin(
            message,
            "✅ Hiện không có đơn nào đang chờ xác nhận thủ công.",
            _admin_back_keyboard(),
        )
        return

    rows = []
    for order in orders:
        badge = "💰" if order.status == "paid_pending" else "⏳"
        rows.append([InlineKeyboardButton(
            text=(
                f"{badge} {order.order_code} · "
                f"{utils.format_vnd(order.total_amount)}"
            ),
            callback_data=f"admin:confirm_pick:{order.order_code}",
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Quay lại", callback_data="admin:home")])
    await _edit_or_send_admin(
        message,
        "✅ <b>XÁC NHẬN ĐƠN THỦ CÔNG</b>\n"
        "Chọn đơn đã kiểm tra thấy tiền vào ngân hàng:\n\n"
        "⚠️ Không xác nhận đơn nếu chưa thấy tiền thực nhận.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "admin:confirm")
async def on_admin_confirm_list(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await _show_manual_confirmation_orders(cb.message)
    await cb.answer()


@router.callback_query(F.data.startswith("admin:confirm_pick:"))
async def on_admin_confirm_pick(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    code = cb.data.rsplit(":", 1)[1].upper()
    order = database.get_order_by_code(code)
    if order is None or order.status not in {"pending", "paid_pending"}:
        await cb.answer("Đơn không còn chờ xác nhận.", show_alert=True)
        await _show_manual_confirmation_orders(cb.message)
        return
    buyer = html.escape(
        utils.buyer_label(order.buyer_username, order.buyer_name, order.chat_id)
    )
    label = utils.items_label(database.get_order_lines(order.id))
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ ĐÃ THẤY TIỀN — GIAO MÃ",
            callback_data=f"admin:confirm_do:{order.order_code}",
        )],
        [InlineKeyboardButton(text="⬅️ Danh sách đơn", callback_data="admin:confirm")],
    ])
    await _edit_or_send_admin(
        cb.message,
        "⚠️ <b>XÁC NHẬN LẦN CUỐI</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🧾 Đơn: <code>{order.order_code}</code>\n"
        f"👤 Khách: {buyer}\n"
        f"📦 {label}\n"
        f"💵 Phải nhận: <b>{utils.format_vnd(order.total_amount)}</b>\n"
        f"📍 Trạng thái: <b>{order.status}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Chỉ bấm giao mã khi bạn đã kiểm tra tài khoản ngân hàng.",
        keyboard,
    )
    await cb.answer()


@router.callback_query(F.data.startswith("admin:confirm_do:"))
async def on_admin_confirm_do(cb: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    code = cb.data.rsplit(":", 1)[1].upper()
    order = database.get_order_by_code(code)
    if order is None or order.status not in {"pending", "paid_pending"}:
        await cb.answer("Đơn không còn chờ xác nhận.", show_alert=True)
        await _show_manual_confirmation_orders(cb.message)
        return
    await cb.answer("Đang giao voucher...")
    delivered = await payments.deliver_order(
        bot, order, tran_id=order.tran_id or f"MANUAL-{code}",
    )
    if delivered:
        text = f"✅ Đã xác nhận và giao đơn <code>{code}</code> thành công."
    else:
        text = (
            f"⚠️ Chưa thể giao đơn <code>{code}</code>. "
            "Hãy kiểm tra tồn kho hoặc trạng thái đơn."
        )
    await _edit_or_send_admin(cb.message, text, _admin_back_keyboard())


# =========================
# BÁO CÁO DOANH THU (thủ công: chọn hôm nay / tháng này)
# =========================
def _report_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📅 Hôm nay", callback_data="report:today"),
            InlineKeyboardButton(text="🗓️ Tháng này", callback_data="report:month"),
        ],
        [InlineKeyboardButton(text="⬅️ Quay lại", callback_data="admin:home")],
    ])


@router.message(Command("baocao"))
async def cmd_report(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        "📊 Xem báo cáo doanh thu cho khoảng nào?", reply_markup=_report_keyboard(),
    )


@router.callback_query(F.data == "admin:report")
async def on_admin_report(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await _edit_or_send_admin(
        cb.message, "📊 Xem báo cáo doanh thu cho khoảng nào?", _report_keyboard(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("report:"))
async def on_pick_report_period(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        return
    period = cb.data.split(":")[1]
    now = datetime.datetime.now(utils.VN_TZ)

    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        title = f"Hôm nay · {start.strftime('%d/%m/%Y')}"
    else:  # month
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        title = f"Tháng {start.strftime('%m/%Y')}"

    text = payments.build_report_text(start=start, end=now, title=title)
    await cb.message.edit_text(text, reply_markup=_admin_back_keyboard())
    await cb.answer()


# =========================
# TRẠNG THÁI SEPAY WEBHOOK
# =========================
def _sepay_status_text() -> str:
    running, last_ipn_at, last_error = payments.payment_gateway_status()
    if not running:
        return (
            "🔴 SePay webhook server chưa hoạt động.\n"
            f"<code>{html.escape(last_error or 'Chưa có thông tin lỗi')}</code>"
        )
    last_webhook = (
        utils.format_time(last_ipn_at) if last_ipn_at else "chưa nhận webhook"
    )
    return (
        "🟢 <b>SePay webhook đang hoạt động</b>\n"
        f"🏦 Tài khoản: <code>{config.BANK_ACCOUNT_NO}</code> ({config.BANK_CODE})\n"
        f"🕒 Webhook gần nhất: <b>{last_webhook}</b>\n"
        f"🔗 <code>{config.SEPAY_PUBLIC_BASE_URL}/payments/sepay/webhook</code>"
    )


@router.message(Command("trangthai"))
async def cmd_status(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    await message.answer(_sepay_status_text(), reply_markup=_admin_back_keyboard())


@router.callback_query(F.data == "admin:status")
async def on_admin_status(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await _edit_or_send_admin(cb.message, _sepay_status_text(), _admin_back_keyboard())
    await cb.answer()


# =========================
# GỬI THÔNG BÁO HÀNG LOẠT (BROADCAST)
# =========================
async def _start_broadcast(message: Message, state: FSMContext) -> None:
    total = database.count_users()
    if total == 0:
        await message.answer(
            "Chưa có khách nào dùng bot để gửi thông báo.",
            reply_markup=_admin_back_keyboard(),
        )
        return
    await message.answer(
        f"📢 <b>Soạn thông báo gửi tới {total} khách</b>\n\n"
        "Gửi nội dung thông báo ở tin nhắn tiếp theo. Bot sẽ cho xem trước "
        "và hỏi lại trước khi gửi.",
        reply_markup=_admin_cancel_keyboard(),
    )
    await state.set_state(Broadcast.waiting_text)


@router.message(Command("thongbao"))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await _start_broadcast(message, state)


@router.callback_query(F.data == "admin:broadcast")
async def on_admin_broadcast(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await _start_broadcast(cb.message, state)
    await cb.answer()


@router.message(Command("huy"), Broadcast.waiting_text)
async def cmd_cancel_broadcast(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("Đã huỷ soạn thông báo.", reply_markup=admin_menu_keyboard())


@router.message(Broadcast.waiting_text, F.text)
async def on_broadcast_text(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    # Giữ nguyên định dạng admin gõ (đậm/nghiêng/link) bằng html_text.
    content = message.html_text
    await state.update_data(content=content)
    await state.set_state(None)  # thoát trạng thái chờ, chuyển sang chờ xác nhận

    total = database.count_users()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Gửi cho {total} khách", callback_data="bcast:send"),
        InlineKeyboardButton(text="❌ Huỷ", callback_data="bcast:cancel"),
    ]])
    await message.answer(
        "👀 <b>XEM TRƯỚC THÔNG BÁO</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{content}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Gửi tới <b>{total}</b> khách?",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "bcast:cancel")
async def on_broadcast_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cb.from_user.id):
        return
    await state.clear()
    await cb.message.edit_text(
        "❌ Đã huỷ, không gửi thông báo nào.", reply_markup=_admin_back_keyboard(),
    )
    await cb.answer()


@router.callback_query(F.data == "bcast:send")
async def on_broadcast_send(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(cb.from_user.id):
        return
    data = await state.get_data()
    content = data.get("content")
    await state.clear()
    if not content:
        await cb.message.edit_text("Phiên soạn thông báo đã hết hạn. Gõ /thongbao để soạn lại.")
        await cb.answer()
        return

    chat_ids = database.all_user_chat_ids()
    await cb.message.edit_text(f"📤 Đang gửi tới {len(chat_ids)} khách...")
    await cb.answer()

    sent = failed = 0
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, content, disable_web_page_preview=True)
            sent += 1
        except Exception:  # noqa: BLE001 — khách chặn bot / xoá chat
            failed += 1
        # Giãn nhịp để không chạm giới hạn ~30 tin/giây của Telegram.
        await asyncio.sleep(0.05)

    await cb.message.edit_text(
        "✅ <b>Đã gửi xong thông báo</b>\n"
        f"• Thành công: <b>{sent}</b>\n"
        f"• Không gửi được (khách chặn bot): <b>{failed}</b>",
        reply_markup=_admin_back_keyboard(),
    )


# =========================
# GIẢ LẬP GIAO ĐƠN MẪU (ADMIN TEST)
# =========================
_TEST_SCENARIOS: dict[str, tuple[str, list[tuple[str, int]]]] = {
    "1": ("JBTEST01", [("UB100KTEST01", 100_000)]),
    "2": ("JBTEST02", [("UB100KTEST01", 100_000), ("UB200KTEST02", 200_000)]),
    "3": (
        "JBTEST03",
        [("UB100KTEST01", 100_000), ("UB100KTEST02", 100_000), ("UB200KTEST03", 200_000)],
    ),
}


def _test_menu_text() -> str:
    return (
        "🎬 <b>CHẾ ĐỘ TEST GIAO ĐƠN MẪU</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Giả lập trọn bộ quy trình giao đơn hàng cho khách ngay tại khung chat này:\n"
        "• Tin xác nhận thanh toán & kiểm tra mã\n"
        "• Tin nhắn từng mã voucher và hướng dẫn\n"
        "• File <code>&lt;mã_đơn&gt;.html</code> chứa giao diện thẻ UrBox + mã vạch Code 128\n\n"
        "🛡 <i>Sử dụng mã mẫu — tuyệt đối không trừ kho thật và không tính vào doanh thu.</i>"
    )


def _test_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🎟 Giao thử đơn 1 mã (100K)", callback_data="test:deliver:1",
        )],
        [InlineKeyboardButton(
            text="🎟🎟 Giao thử đơn 2 mã (100K + 200K)", callback_data="test:deliver:2",
        )],
        [InlineKeyboardButton(
            text="🎟×3 Giao thử đơn 3 mã (2×100K + 1×200K)", callback_data="test:deliver:3",
        )],
        [InlineKeyboardButton(
            text="📄 Chỉ xuất nhanh file HTML mẫu", callback_data="test:html",
        )],
        [InlineKeyboardButton(text="⬅️ Bảng điều khiển", callback_data="admin:home")],
    ])


@router.message(Command("test"))
async def cmd_test(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    await message.answer(_test_menu_text(), reply_markup=_test_menu_keyboard())


@router.callback_query(F.data == "admin:test")
async def on_admin_test(cb: CallbackQuery) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await _edit_or_send_admin(cb.message, _test_menu_text(), _test_menu_keyboard())
    await cb.answer()


@router.callback_query(F.data.startswith("test:deliver:"))
async def on_admin_test_deliver(cb: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    key = cb.data.rsplit(":", 1)[1]
    order_code, vouchers = _TEST_SCENARIOS.get(key, _TEST_SCENARIOS["1"])
    await cb.answer(f"Đang giả lập giao đơn {order_code}...")
    await payments.simulate_order_delivery(
        bot,
        chat_id=cb.message.chat.id,
        vouchers=vouchers,
        order_code=order_code,
    )


@router.callback_query(F.data == "test:html")
async def on_admin_test_html(cb: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await cb.answer("Đang tạo file HTML mẫu...")
    await payments._send_order_barcode_document(
        bot,
        chat_id=cb.message.chat.id,
        order_code="JBTEST99",
        vouchers=[("UB100KTEST01", 100_000), ("UB200KTEST02", 200_000)],
    )


# =========================
# XOÁ MÃ TRONG KHO (ADMIN)
# =========================
_BARCODE_CODE_TOKEN_RE = re.compile(r"^[A-Z0-9-]{4,32}$")
_UB_DENOM_PREFIX_RE = re.compile(r"^UB(\d+)K?", re.IGNORECASE)


def _extract_codes_for_delete(text: str) -> list[str]:
    """Tách danh sách mã cần xoá từ tin nhắn (hỗ trợ cả mã gõ sai định dạng chuẩn)."""
    codes: list[str] = []
    seen: set[str] = set()
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        extracted = checker.extract_inputs(line)
        tokens = extracted if extracted else re.split(r"[\s,;]+", line)
        for token in tokens:
            cleaned = token.strip().upper()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                codes.append(cleaned)
    return codes


async def _start_delete_stock(message: Message, state: FSMContext) -> None:
    await message.answer(
        "🗑 <b>Xoá mã đã thêm vào kho</b>\n\n"
        "Gửi danh sách mã cần xoá khỏi kho (mỗi mã 1 dòng hoặc gửi file <b>.txt</b>).\n"
        "• Chỉ xoá các mã đang ở trạng thái <b>còn trong kho (chưa bán)</b>.\n"
        "• Các mã đã bán trong lịch sử đơn hàng sẽ được giữ nguyên.",
        reply_markup=_admin_cancel_keyboard(),
    )
    await state.set_state(DeleteStock.waiting_codes)


@router.message(Command("xoama"))
async def cmd_delete_stock(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await _start_delete_stock(message, state)


@router.callback_query(F.data == "admin:delete_stock")
async def on_admin_delete_stock(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await _start_delete_stock(cb.message, state)
    await cb.answer("Gửi mã cần xoá khỏi kho")


async def _handle_delete_stock_text(
    message: Message, state: FSMContext, text: str,
) -> None:
    raw_codes = _extract_codes_for_delete(text)
    if not raw_codes:
        await message.answer(
            "Không tìm thấy mã nào trong nội dung bạn gửi. Vui lòng gửi lại hoặc bấm Huỷ thao tác.",
            reply_markup=_admin_cancel_keyboard(),
        )
        return

    # Nếu có link UrBox thì đọc mã từ link, còn mã thường thì giữ nguyên để xoá được cả mã nhập sai.
    resolved_codes: list[str] = []
    for item in raw_codes:
        if item.startswith("HTTP://") or item.startswith("HTTPS://"):
            read_res = await asyncio.to_thread(checker.read_inputs, [item])
            if read_res and read_res[0].code:
                resolved_codes.append(read_res[0].code.upper())
            else:
                resolved_codes.append(item)
        else:
            resolved_codes.append(item)

    await state.clear()
    deleted, skipped = database.delete_available_vouchers(resolved_codes)

    lines = [
        "🗑 <b>Kết quả xoá mã khỏi kho</b>",
        f"✅ Đã xoá: <b>{len(deleted)}</b> mã",
    ]
    if deleted:
        preview = [
            f"   • <code>{html.escape(code)}</code> ({utils.format_k(denom)})"
            for code, denom in deleted[:20]
        ]
        lines.extend(preview)
        if len(deleted) > 20:
            lines.append(f"   <i>... và {len(deleted) - 20} mã khác</i>")
    if skipped:
        lines.append(f"⚠️ Không tìm thấy trong kho chưa bán: <b>{len(skipped)}</b> mã")
        skip_preview = [f"   • <code>{html.escape(c)}</code>" for c in skipped[:12]]
        lines.extend(skip_preview)
        if len(skipped) > 12:
            lines.append(f"   <i>... và {len(skipped) - 12} mã khác</i>")

    lines.append("")
    lines.append("📦 <b>Tồn kho hiện tại</b>")
    for denom, count in database.stock_summary().items():
        lines.append(f"   • {utils.format_vnd(denom)}: {count} mã")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 Xoá tiếp", callback_data="admin:delete_stock"),
            InlineKeyboardButton(text="➕ Nhập kho", callback_data="admin:import"),
        ],
        [InlineKeyboardButton(text="⬅️ Bảng điều khiển", callback_data="admin:home")],
    ])
    await message.answer("\n".join(lines), reply_markup=kb)


@router.message(DeleteStock.waiting_codes, F.document)
async def on_delete_stock_file(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    buf = await bot.download(message.document)
    text = buf.read().decode("utf-8", errors="ignore")
    await _handle_delete_stock_text(message, state, text)


@router.message(DeleteStock.waiting_codes, F.text)
async def on_delete_stock_text(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    if (message.text or "").strip().startswith("/"):
        return
    await _handle_delete_stock_text(message, state, message.text or "")


# =========================
# TẠO FILE HTML BARCODE TỪ CHUỖI MÃ (ADMIN)
# =========================
def _infer_barcode_denom(code: str, explicit_hint: str = "") -> int:
    if explicit_hint:
        parsed_hint = utils.parse_vnd_amount(explicit_hint)
        if parsed_hint and 1_000 <= parsed_hint <= 100_000_000:
            return parsed_hint
    classified = utils.classify_denom(code)
    if classified:
        return classified
    match = _UB_DENOM_PREFIX_RE.match(code)
    if match:
        val = int(match.group(1)) * 1_000
        if 1_000 <= val <= 100_000_000:
            return val
    return 100_000


def _parse_barcode_input_lines(text: str) -> list[tuple[str, int]]:
    """Đọc danh sách mã (mỗi dòng 1 mã) thành [(code, denom)] để tạo file HTML barcode."""
    vouchers: list[tuple[str, int]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p for p in re.split(r"[\s,;|]+", line) if p]
        if not parts:
            continue
        candidate = parts[0].strip().upper()
        hint = parts[1].strip() if len(parts) > 1 else ""
        if not _BARCODE_CODE_TOKEN_RE.match(candidate):
            ub_matches = checker.UB_CODE_RE.findall(line)
            if ub_matches:
                for m in ub_matches:
                    code_up = m.upper()
                    vouchers.append((code_up, _infer_barcode_denom(code_up)))
            continue
        vouchers.append((candidate, _infer_barcode_denom(candidate, hint)))
    return vouchers


async def _start_barcode_builder(message: Message, state: FSMContext) -> None:
    await message.answer(
        "🎫 <b>Tạo file HTML giao diện thẻ + Barcode</b>\n\n"
        "Gửi danh sách mã voucher (<b>mỗi mã 1 dòng</b> hoặc gửi file <b>.txt</b>).\n"
        "• Bot tự nhận mệnh giá từ mã (<code>UB100...</code> → 100.000đ, <code>UB200...</code> → 200.000đ).\n"
        "• Hoặc ghi kèm mệnh giá sau mã, ví dụ: <code>UB100K49D1399 100k</code>.\n"
        "• File HTML xuất ra chỉ hiển thị thẻ & mã vạch Code 128, không cho khách tự tạo mã.",
        reply_markup=_admin_cancel_keyboard(),
    )
    await state.set_state(BarcodeBuilder.waiting_codes)


@router.message(Command("barcode"))
async def cmd_barcode_builder(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await _start_barcode_builder(message, state)


@router.callback_query(F.data == "admin:barcode")
async def on_admin_barcode_builder(cb: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(cb.from_user.id):
        await cb.answer("Bạn không có quyền quản trị.", show_alert=True)
        return
    await _start_barcode_builder(cb.message, state)
    await cb.answer("Gửi danh sách mã (mỗi mã 1 dòng)")


async def _handle_barcode_builder_text(
    message: Message, state: FSMContext, bot: Bot, text: str,
) -> None:
    vouchers = _parse_barcode_input_lines(text)
    if not vouchers:
        await message.answer(
            "Không tìm thấy mã hợp lệ (4–32 ký tự A–Z, 0–9 hoặc dấu gạch ngang). "
            "Vui lòng gửi lại hoặc bấm Huỷ thao tác.",
            reply_markup=_admin_cancel_keyboard(),
        )
        return

    await state.clear()
    stamp = datetime.datetime.now(utils.VN_TZ).strftime("%Y%m%d_%H%M%S")
    label = vouchers[0][0] if len(vouchers) == 1 else f"BARCODE_{stamp}"
    html_str = report.render_order_vouchers_barcode_html(label, vouchers)
    doc = BufferedInputFile(html_str.encode("utf-8"), filename=f"{label}.html")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎫 Tạo file Barcode khác", callback_data="admin:barcode")],
        [InlineKeyboardButton(text="⬅️ Bảng điều khiển", callback_data="admin:home")],
    ])
    await bot.send_document(
        message.chat.id,
        doc,
        caption=(
            f"✅ <b>Đã tạo file HTML Barcode ({len(vouchers)} mã)</b>\n"
            "👆 Bấm mở file để xem giao diện thẻ UrBox + mã vạch Code 128."
        ),
        reply_markup=kb,
    )


@router.message(BarcodeBuilder.waiting_codes, F.document)
async def on_barcode_builder_file(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    buf = await bot.download(message.document)
    text = buf.read().decode("utf-8", errors="ignore")
    await _handle_barcode_builder_text(message, state, bot, text)


@router.message(BarcodeBuilder.waiting_codes, F.text)
async def on_barcode_builder_text(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return
    if (message.text or "").strip().startswith("/"):
        return
    await _handle_barcode_builder_text(message, state, bot, message.text or "")
