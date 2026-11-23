"""Điểm khởi động bot: Telegram polling + SePay webhook + tác vụ nền."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from . import config, database, handlers_admin, handlers_customer, payments

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")
_background_tasks: set[asyncio.Task] = set()


def _start_background_task(coro, name: str) -> None:
    """Giữ tham chiếu task để Python không thu gom khi task vẫn đang chạy."""
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)

    def _finished(done: asyncio.Task) -> None:
        _background_tasks.discard(done)
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            log.error("Tác vụ nền %s đã dừng: %s", done.get_name(), error)

    task.add_done_callback(_finished)


async def _on_startup(bot: Bot) -> None:
    # Menu lệnh hiện trong khung chat của khách.
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="🍗 Mở menu chính (mua hàng, hướng dẫn, hỗ trợ)"),
            BotCommand(command="vi", description="👛 Xem số dư và lịch sử ví"),
            BotCommand(command="donhang", description="🧾 Xem lại mã voucher đã mua"),
        ])
    except Exception:  # noqa: BLE001
        pass
    # Chạy nền: HTTP server SePay + hết hạn đơn + báo cáo doanh thu ngày.
    _start_background_task(payments.run_sepay_server(bot), "sepay-webhook-server")
    _start_background_task(payments.watch_expiry(bot), "order-expiry-watcher")
    _start_background_task(payments.watch_daily_report(bot), "daily-report-watcher")
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                "🤖 Bot đã khởi động.\n\n" + handlers_admin.admin_dashboard_text(),
                reply_markup=handlers_admin.admin_menu_keyboard(),
            )
        except Exception:  # noqa: BLE001
            pass


async def _on_shutdown() -> None:
    tasks = list(_background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def main() -> None:
    errors = config.validate()
    if errors:
        raise SystemExit("Lỗi cấu hình (.env):\n- " + "\n- ".join(errors))

    database.init_db()
    log.info("Đã khởi tạo DB tại %s", config.DB_PATH)

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    # Admin trước để lệnh admin (và FSM nhập kho) ưu tiên hơn handler khách.
    dp.include_router(handlers_admin.router)
    dp.include_router(handlers_customer.router)
    dp.startup.register(_on_startup)
    dp.shutdown.register(_on_shutdown)

    log.info("Bot bắt đầu polling Telegram...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
