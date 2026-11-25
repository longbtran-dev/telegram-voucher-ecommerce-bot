# -*- coding: utf-8 -*-
"""Kiểm tra nhanh (không cần mạng/Telegram): DB đa mệnh giá, chờ hàng, render HTML.

Chạy từ thư mục gốc dự án:  python tests/test_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("SEPAY_MERCHANT_ID", "MERCHANT_123")
os.environ.setdefault("SEPAY_SECRET_KEY", "test-secret")
os.environ.setdefault("SEPAY_PUBLIC_BASE_URL", "https://pay.example.com")
os.environ.setdefault("SEPAY_PAYMENT_METHOD", "BANK_TRANSFER")
os.environ.setdefault("BANK_ACCOUNT_NO", "0123456789")
os.environ.setdefault("BANK_ACCOUNT_NAME", "NGUYEN VAN A")
os.environ.setdefault("BANK_CODE", "MB")
os.environ.setdefault("SEPAY_WEBHOOK_SECRET", "bank-webhook-test-secret")

from jollibee_bot import (  # noqa: E402
    checker,
    config,
    database,
    handlers_admin,
    handlers_customer,
    payments,
    report,
    sepay,
    utils,
)


def main() -> None:
    tmpdir = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(tmpdir, "test.db")
    database.init_db()
    tables = {
        row[0]
        for row in database._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"wallet_transactions", "wallet_topups"} <= tables

    # --- Nhập kho: 3 mã 100k, 2 mã 200k ---
    for i in range(3):
        assert database.add_voucher(100_000, f"UB100TEST{i}", f"https://urbox.vn/a{i}")
    for i in range(2):
        assert database.add_voucher(200_000, f"UB200TEST{i}", f"https://urbox.vn/b{i}")
    assert not database.add_voucher(100_000, "UB100TEST0", "x")  # trùng mã

    # --- Đơn đa mệnh giá: 2 × 100k + 1 × 200k, thanh toán 1 lần ---
    items = [(200_000, 1, 150_000), (100_000, 2, 75_000)]
    order = database.create_order_multi(
        "JBTEST01", 111, items, 15, buyer_username="tester", buyer_name="Test User",
    )
    assert order is not None
    assert order.total_amount == 300_000
    assert order.quantity == 3
    assert order.denom == 0  # đơn hỗn hợp
    assert [item.order_code for item in database.list_manual_confirmation_orders()] == [
        order.order_code
    ]

    lines = database.get_order_lines(order.id)
    assert sorted(lines) == sorted(items), lines
    assert utils.items_label(lines) == "1 × 200k + 2 × 100k"

    # Kho đã reserve đúng
    assert database.count_available(100_000) == 1
    assert database.count_available(200_000) == 1

    # Đơn vượt kho -> None, không giữ mã nào
    assert database.create_order_multi("JBTEST02", 111, [(200_000, 5, 150_000)], 15) is None
    assert database.count_available(200_000) == 1

    # --- Khách tự huỷ đơn ---
    o2 = database.create_order_multi("JBTEST03", 222, [(100_000, 1, 75_000)], 15)
    assert o2 is not None
    assert database.cancel_customer_order("JBTEST03", 999) is None   # sai chat_id
    assert database.cancel_customer_order("JBTEST03", 222) is not None
    assert database.count_available(100_000) == 1                   # mã về kho
    assert database.cancel_customer_order("JBTEST03", 222) is None  # huỷ lần 2

    # --- Người dùng (cho broadcast) ---
    assert database.count_users() == 0
    database.touch_user(111, "tester")
    database.touch_user(222, "b")
    database.touch_user(111, "tester2")          # upsert: không tạo bản ghi mới
    assert database.count_users() == 2
    assert sorted(database.all_user_chat_ids()) == [111, 222]

    # --- Ví: ledger bất biến, admin cộng/trừ/đặt số dư và không cho âm ---
    assert database.adjust_wallet_balance(111, 100_000, 1, "Thưởng") == (
        "applied", 100_000,
    )
    assert database.adjust_wallet_balance(111, -25_000, 1, "Điều chỉnh") == (
        "applied", 75_000,
    )
    assert database.adjust_wallet_balance(111, -100_000, 1, "Không đủ") == (
        "insufficient", 75_000,
    )
    assert database.set_wallet_balance(111, 50_000, 1, "Chốt số dư") == (
        "applied", 50_000,
    )
    assert database.set_wallet_balance(111, 50_000, 1, "Không đổi") == (
        "unchanged", 50_000,
    )
    wallet_history = database.list_wallet_transactions(111)
    assert [transaction.kind for transaction in wallet_history] == [
        "admin_set", "admin_debit", "admin_credit",
    ]
    assert database.wallet_balance(111) == 50_000
    wallet_customer = database.get_wallet_customer(111)
    assert wallet_customer is not None
    assert wallet_customer.username == "tester2"
    assert wallet_customer.balance == 50_000
    assert wallet_customer.transaction_count == 3

    topup = database.create_wallet_topup(
        "NAPTEST1234", 222, 100_000, username="b", buyer_name="Buyer B",
    )
    assert topup is not None
    assert database.complete_wallet_topup(topup.topup_code, "SEPAY-TOPUP-1") == (
        "credited", 100_000,
    )
    assert database.complete_wallet_topup(topup.topup_code, "SEPAY-TOPUP-1") == (
        "duplicate", 100_000,
    )
    assert database.complete_wallet_topup(topup.topup_code, "SEPAY-TOPUP-2") == (
        "already_paid", 100_000,
    )
    assert database.wallet_summary() == (2, 150_000)
    assert utils.parse_vnd_amount("50k") == 50_000
    assert utils.parse_vnd_amount("0", allow_zero=True) == 0
    assert utils.parse_vnd_amount("không có tiền") is None

    # --- Chờ hàng (báo khi có hàng) ---
    assert database.add_stock_alert(333, 200_000)
    assert not database.add_stock_alert(333, 200_000)  # đăng ký trùng
    assert database.add_stock_alert(444, 200_000)
    waiters = database.pop_stock_alerts(200_000)
    assert waiters == [333, 444], waiters
    assert database.pop_stock_alerts(200_000) == []    # đã xoá sau khi lấy

    # --- Giao đơn + thống kê ---
    assert database.prepare_order_payment(order.id, "TRAN1") == "ready"
    prepared_order = database.get_order_by_code(order.order_code)
    assert prepared_order.status == "paid_pending"
    assert prepared_order.tran_id == "TRAN1"
    assert database.complete_order_delivery(order.id)
    assert not database.complete_order_delivery(order.id)
    assert not database.mark_order_paid_delivered(order.id, "TRAN1")  # chống giao 2 lần
    assert order.order_code not in {
        item.order_code for item in database.list_manual_confirmation_orders()
    }
    details = database.get_order_voucher_details(order.id)
    assert len(details) == 3 and len(details[0]) == 4  # có kèm denom

    assert database.customer_stats(111) == (1, 300_000)
    assert database.count_buyers() == 1
    buyers = database.list_buyer_summaries()
    assert len(buyers) == 1
    assert buyers[0].chat_id == 111
    assert buyers[0].buyer_username == "tester"
    assert buyers[0].order_count == 1
    assert buyers[0].voucher_quantity == 3
    assert buyers[0].total_spent == 300_000
    assert database.sold_qty_by_denom() == {100_000: 2, 200_000: 1}

    # --- Nội dung giao voucher đã duyệt ---
    confirmation = payments._delivery_confirmation_text(order)
    assert "THANH TOÁN THÀNH CÔNG" in confirmation
    assert "<blockquote>🧾 Đơn hàng" in confirmation
    assert "2 × 100K" in confirmation and "1 × 200K" in confirmation
    assert "không cần thanh toán lại" in confirmation
    assert "đang kiểm tra" not in confirmation.lower()

    checking = payments._delivery_checking_text(order, 3)
    assert "HỆ THỐNG ĐANG KIỂM TRA MÃ" in checking
    assert "Đang kiểm tra: <b>03 voucher</b>" in checking
    assert "Vui lòng đợi" in checking

    voucher_text = payments._delivery_voucher_text(
        1, 3, "UB100TEST0", 100_000,
        SimpleNamespace(valid_until="10-12-2026"),
    )
    assert "VOUCHER 01 / 03" in voucher_text
    assert "Trạng thái: <b>Đã kiểm tra — sử dụng được</b>" in voucher_text
    assert "<blockquote><code>UB100TEST0</code></blockquote>" in voucher_text
    assert "Hạn sử dụng" not in voucher_text
    assert "urbox.vn" not in voucher_text
    assert voucher_text.count("━━━━━━━━━━━━━━━━━━") == 3

    complete_text = payments._delivery_complete_text(
        order, 3, "@YourSupportBot", "0900000000",
    )
    assert "ĐÃ GIAO ĐỦ 03 / 03 VOUCHER" in complete_text
    assert "☎️ <b>LIÊN HỆ HỖ TRỢ</b>" in complete_text
    assert "Điện thoại: <code>0900000000</code>" in complete_text
    assert "Không chia sẻ" not in complete_text
    cnt, rev, by_denom = database.delivered_stats_between(0, int(time.time()) + 10)
    assert (cnt, rev) == (1, 300_000)
    assert by_denom[100_000]["qty"] == 2 and by_denom[200_000]["qty"] == 1

    # --- Render HTML admin ---
    orders_data = [{
        "order_code": "JBTEST01", "time": "18:15", "date": "05/07/2026",
        "buyer": "@tester", "quantity": 3, "denom": 0, "total_amount": 300_000,
        "vouchers": database.get_order_vouchers(order.id),
    }]
    html_admin = report.render_history_html(
        orders_data, 1, 300_000, "Shop Test",
        sold_by_denom=database.sold_qty_by_denom(),
    )
    for needle in ("Đơn đã bán", "Tổng doanh thu", "Mã 100k đã bán", "Mã 200k đã bán"):
        assert needle in html_admin, needle

    # --- Render HTML khách ---
    cust_data = [{
        "order_code": "JBTEST01", "time": "18:15", "date": "05/07/2026",
        "items_label": utils.items_label(lines), "total_amount": 300_000,
        "vouchers": database.get_order_vouchers(order.id),
    }]
    html_cust = report.render_customer_history_html(cust_data, 1, 300_000, "Shop Test")
    for needle in (
        "Đơn đã mua", "Tổng chi tiêu", "UB100TEST0",
        "Barcode Code 128 cho mã UB100TEST0", "v-barcode",
    ):
        assert needle in html_cust, needle

    # --- Render HTML barcode giao diện UrBox (chỉ hiển thị, không cho tạo mã) ---
    assert report.encode_code128b("UB100K49D1399")[-2] == 80
    barcode_single_html = report.render_order_vouchers_barcode_html(
        "JBTEST01",
        [("UB100K49D1399", 100_000)],
    )
    barcode_order_html = report.render_order_vouchers_barcode_html(
        "JBTEST01",
        [("UB100K49D1399", 100_000), ("UB200TEST0", 200_000)],
    )
    assert "JBTEST01" in barcode_order_html
    assert "UB100K49D1399" in barcode_order_html
    assert "UB200TEST0" in barcode_order_html
    assert "100.000 đ" in barcode_order_html
    assert "200.000 đ" in barcode_order_html
    assert barcode_order_html.count('<article class="voucher-card"') == 2
    assert len(barcode_order_html) - len(barcode_single_html) < 20_000
    assert "mã vạch" in handlers_customer._guide_direct_text().lower()

    # --- Tự động nâng cấp JOLLIBEE_APP_VERSION khi Jollibee yêu cầu bản mới ---
    assert checker.jollibee.DEFAULT_APP_VERSION == "4.10"
    assert checker.jollibee.extract_required_app_version(
        ["Vui lòng cập nhật lên phiên bản mới nhất 4.12"]
    ) == "4.12"
    os.environ["JOLLIBEE_APP_VERSION"] = "4.09"
    try:
        # Dù .env cũ để 4.09, vẫn tự ưu tiên bản cao hơn (4.10)
        assert checker.current_app_version() == "4.10"
        checker.jollibee.set_active_app_version("4.15")
        assert checker.current_app_version() == "4.15"
        assert database.get_config("jollibee_app_version") == "4.15"
    finally:
        os.environ.pop("JOLLIBEE_APP_VERSION", None)
        database.set_config("jollibee_app_version", "4.10")
        checker.jollibee.set_active_app_version("4.10", allow_lower=True)

    # --- Lịch sử khách theo từng mã đơn (phân trang + chọn đơn xem mã & barcode) ---
    assert len(database.list_customer_delivered(111, limit=1, offset=0)) == 1
    assert database.list_customer_delivered(111, limit=1, offset=1) == []
    hist_text, hist_kb, hist_page, hist_pages = (
        handlers_customer._customer_history_page_data(111, 0)
    )
    assert (hist_page, hist_pages) == (0, 1)
    assert "ĐƠN HÀNG ĐÃ MUA" in hist_text
    assert "300.000đ" in hist_text
    hist_callbacks = {
        button.callback_data
        for row in hist_kb.inline_keyboard
        for button in row
        if button.callback_data
    }
    assert "hist:order:JBTEST01:0" in hist_callbacks
    assert "menu:home" in hist_callbacks
    order_hist_text = handlers_customer._customer_order_history_text(
        database.get_order_by_code("JBTEST01")
    )
    for needle in ("JBTEST01", "UB100TEST0", "UB100TEST1", "UB200TEST0"):
        assert needle in order_hist_text, needle

    # --- Chữ ký + form checkout SePay ---
    fields = sepay.build_checkout_fields(
        order_code="INV_20231201_001",
        amount=100_000,
        description="Thanh toán đơn hàng #12345",
    )
    assert list(fields) == [
        "order_amount", "merchant", "currency", "operation",
        "order_description", "order_invoice_number", "payment_method",
        "success_url", "error_url", "cancel_url", "signature",
    ]
    assert fields["signature"] == "pYnxmEYXYQQAEuN0PHhtf4QDRCzUhvRAad+W5qkVirk="
    checkout_html = sepay.render_checkout_form(fields)
    assert 'method="post"' in checkout_html
    assert config.SEPAY_CHECKOUT_URL in checkout_html
    assert checkout_html.index('name="order_amount"') < checkout_html.index('name="merchant"')

    # --- QR động + xác thực Bank Webhook SePay ---
    qr_url = utils.build_sepay_qr_url(
        config.BANK_ACCOUNT_NO,
        config.BANK_CODE,
        order.total_amount,
        order.order_code,
        config.BANK_ACCOUNT_NAME,
    )
    qr_params = parse_qs(urlparse(qr_url).query)
    assert qr_params["acc"] == ["0123456789"]
    assert qr_params["bank"] == ["MB"]
    assert qr_params["amount"] == ["300000"]
    assert qr_params["des"] == ["JBTEST01"]
    assert qr_params["holder"] == ["NGUYEN VAN A"]

    raw_body = b'{"id":123,"transferType":"in"}'
    timestamp = "1700000000"
    signature = payments.build_bank_webhook_signature(
        config.SEPAY_WEBHOOK_SECRET, timestamp, raw_body,
    )
    assert payments.verify_bank_webhook_signature(
        config.SEPAY_WEBHOOK_SECRET, timestamp, raw_body, signature,
        now=int(timestamp),
    )
    assert not payments.verify_bank_webhook_signature(
        config.SEPAY_WEBHOOK_SECRET, timestamp, raw_body + b" ", signature,
        now=int(timestamp),
    )
    assert not payments.verify_bank_webhook_signature(
        config.SEPAY_WEBHOOK_SECRET, timestamp, raw_body, signature,
        now=int(timestamp) + 301,
    )

    bank_payload = {
        "id": 123,
        "accountNumber": config.BANK_ACCOUNT_NO,
        "transferType": "in",
        "transferAmount": order.total_amount,
        "content": f"Thanh toan {order.order_code}",
    }
    assert payments.extract_bank_order_code(bank_payload) == order.order_code
    assert payments.bank_transaction_matches_order(bank_payload, order)
    assert not payments.bank_transaction_matches_order(
        {**bank_payload, "transferAmount": order.total_amount - 1}, order,
    )
    assert not payments.bank_transaction_matches_order(
        {**bank_payload, "content": "giao dich ca nhan"}, order,
    )

    # --- Bảng điều khiển admin bằng nút ---
    menu = handlers_admin.admin_menu_keyboard()
    menu_callbacks = {
        button.callback_data
        for row in menu.inline_keyboard
        for button in row
        if button.callback_data
    }
    assert {
        "admin:stock", "admin:import", "admin:delete_stock", "admin:barcode",
        "admin:report", "admin:history", "admin:prices", "admin:settings",
        "admin:broadcast", "admin:confirm", "admin:buyers", "admin:wallets",
        "admin:status", "admin:checker", "admin:test", "admin:home",
    } <= menu_callbacks
    test_callbacks = {
        button.callback_data
        for row in handlers_admin._test_menu_keyboard().inline_keyboard
        for button in row
        if button.callback_data
    }
    assert {
        "test:deliver:1", "test:deliver:2", "test:deliver:3", "test:html", "admin:home",
    } <= test_callbacks
    checker_callbacks = {
        button.callback_data
        for row in handlers_admin._checker_keyboard().inline_keyboard
        for button in row
        if button.callback_data
    }
    assert "admin:proxies" in checker_callbacks
    assert handlers_admin._parse_money("75k") == 75_000
    assert handlers_admin._parse_money("80.000đ") == 80_000
    assert handlers_admin._parse_money("không phải giá") is None
    dashboard = handlers_admin.admin_dashboard_text()
    assert "BẢNG ĐIỀU KHIỂN ADMIN" in dashboard
    assert "Doanh thu" in dashboard
    assert "Ví khách" in dashboard and "150.000đ" in dashboard
    buyers_text, buyers_page, buyers_pages = handlers_admin._buyers_page_text(0)
    assert (buyers_page, buyers_pages) == (0, 1)
    assert "KHÁCH ĐÃ MUA" in buyers_text
    assert "3</b> voucher" in buyers_text
    assert "300.000đ" in buyers_text
    wallets_text, wallets_page, wallets_pages, wallet_customers = (
        handlers_admin._wallets_page_data(0)
    )
    assert (wallets_page, wallets_pages) == (0, 1)
    assert "VÍ KHÁCH HÀNG" in wallets_text
    assert "150.000đ" in wallets_text
    assert {customer.chat_id for customer in wallet_customers} == {111, 222}
    wallet_detail = handlers_admin._wallet_customer_detail_data(111, 0)
    assert wallet_detail is not None
    assert "Chốt số dư" in wallet_detail[0]
    assert "50.000đ" in wallet_detail[0]

    # --- Menu chính và gian hàng đã duyệt ---
    database.set_config("shop_title", "Shop Voucher Jollibee 🐥")
    customer_menu = handlers_customer._menu_text()
    assert "🐣 <b>SHOP VOUCHER JOLLIBEE</b> 🐥" in customer_menu
    assert "Voucher chính hãng • Giá tốt • Giao tự động" in customer_menu
    assert "Thanh toán tự động — nhận mã nhanh chóng" in customer_menu
    assert "🛡 Bảo hành 24 giờ" in customer_menu
    assert "Mã và đường dẫn được gửi ngay sau xác nhận" not in customer_menu

    customer_menu_buttons = [
        button.text
        for row in handlers_customer._menu_keyboard().inline_keyboard
        for button in row
    ]
    assert "🛒 MUA VOUCHER NGAY" in customer_menu_buttons
    assert "💰 Ví của tôi" in customer_menu_buttons
    assert "📖 Cách sử dụng" in customer_menu_buttons
    assert "🧾 Đơn đã mua" in customer_menu_buttons
    wallet_text = handlers_customer._wallet_text(111)
    assert "VÍ CỦA TÔI" in wallet_text
    assert "50.000đ" in wallet_text
    assert "Admin điều chỉnh" in wallet_text

    shop_chat_id = 9090
    handlers_customer._carts.pop(shop_chat_id, None)
    empty_shop = handlers_customer._shop_text(shop_chat_id)
    assert "🛒 <b>GIAN HÀNG VOUCHER JOLLIBEE</b>" in empty_shop
    assert "🧺 Giỏ hàng đang trống." in empty_shop
    assert "Bấm nút <b>➕</b> để thêm voucher vào giỏ" in empty_shop
    assert empty_shop.count("<blockquote>") == 2

    handlers_customer._carts[shop_chat_id] = {100_000: 1}
    filled_shop = handlers_customer._shop_text(shop_chat_id)
    assert "🧺 <b>Giỏ hàng của bạn:</b>" in filled_shop
    assert "1 × 100.000đ = 75.000đ" in filled_shop
    assert "💰 Tổng thanh toán: <b>75.000đ</b>" in filled_shop
    assert "Chỉnh số lượng bằng nút ➕/➖" in filled_shop
    assert filled_shop.count("<blockquote>") == 3
    handlers_customer._carts.pop(shop_chat_id, None)

    # --- Tạo đơn và nội dung QR đã duyệt ---
    detail = "   • 2 × 100.000đ = 150.000đ"
    created_text = handlers_customer._order_created_text(order, detail)
    assert "Đã tạo đơn <code>JBTEST01</code>" in created_text
    assert "Chọn thanh toán bằng ví hoặc chuyển khoản ngân hàng" in created_text
    assert "Số dư ví: <b>50.000đ</b>" in created_text
    assert "QR SePay" not in created_text
    assert created_text.count("<blockquote>") == 2
    payment_callbacks = {
        button.callback_data
        for row in handlers_customer._payment_method_keyboard(order).inline_keyboard
        for button in row
        if button.callback_data
    }
    assert f"pay:bank:{order.order_code}" in payment_callbacks
    assert f"pay:wallet:{order.order_code}" not in payment_callbacks

    original_bank_code = config.BANK_CODE
    original_account_name = config.BANK_ACCOUNT_NAME
    original_account_no = config.BANK_ACCOUNT_NO
    config.BANK_CODE = "TPB"
    config.BANK_ACCOUNT_NAME = "NGUYEN VAN A"
    config.BANK_ACCOUNT_NO = "0123456789"
    try:
        payment_caption = handlers_customer._payment_caption(order, detail)
    finally:
        config.BANK_CODE = original_bank_code
        config.BANK_ACCOUNT_NAME = original_account_name
        config.BANK_ACCOUNT_NO = original_account_no
    assert "Ngân hàng: <b>TP BANK</b>" in payment_caption
    assert "bot tự gửi mã ngay khi hệ thống xác nhận" in payment_caption
    assert "SePay xác nhận" not in payment_caption
    assert "Số tài khoản: <code>0123456789</code>" in payment_caption
    assert payment_caption.count("<blockquote>") == 4

    # --- Báo thanh toán + tiến trình, xóa tiến trình rồi giao 2 mã + hỗ trợ ---
    assert database.add_voucher(100_000, "UB100DELIVERY1", "https://urbox.vn/delivery1")
    delivery_order = database.create_order_multi(
        "JBDELIV1", 3333, [(100_000, 2, 75_000)], 15,
        buyer_username="delivery", buyer_name="Delivery Test",
    )
    assert delivery_order is not None

    class FakeBot:
        def __init__(self):
            self.messages = []
            self.documents = []
            self.deleted_messages = []
            self.edited_messages = []
            self.events = []
            self.next_message_id = 1

        async def send_message(self, chat_id, text, **kwargs):
            message_id = self.next_message_id
            self.next_message_id += 1
            self.messages.append((chat_id, text, kwargs))
            self.events.append(("send", chat_id, message_id, text))
            return SimpleNamespace(message_id=message_id)

        async def send_document(self, chat_id, document, **kwargs):
            message_id = self.next_message_id
            self.next_message_id += 1
            self.documents.append((chat_id, document, kwargs))
            self.events.append(("document", chat_id, message_id, getattr(document, "filename", "")))
            return SimpleNamespace(message_id=message_id)

        async def delete_message(self, chat_id, message_id):
            self.deleted_messages.append((chat_id, message_id))
            self.events.append(("delete", chat_id, message_id, ""))

        async def edit_message_text(self, text, chat_id, message_id, **kwargs):
            self.edited_messages.append((chat_id, message_id, text, kwargs))
            self.events.append(("edit", chat_id, message_id, text))

    fake_bot = FakeBot()
    original_verify_and_swap = payments._verify_and_swap

    async def fake_verify_and_swap(bot, incoming_order):
        customer_messages = [item for item in bot.messages if item[0] == 3333]
        assert len(customer_messages) == 2
        assert "THANH TOÁN THÀNH CÔNG" in customer_messages[0][1]
        assert "HỆ THỐNG ĐANG KIỂM TRA MÃ" in customer_messages[1][1]
        return ([
            (
                "UB100DELIVERY0", "https://urbox.vn/delivery0", 100_000,
                SimpleNamespace(status="VALID", valid_until="10-12-2026"),
            ),
            (
                "UB100DELIVERY1", "https://urbox.vn/delivery1", 100_000,
                SimpleNamespace(status="VALID", valid_until="10-12-2026"),
            ),
        ], True)

    payments._verify_and_swap = fake_verify_and_swap
    try:
        assert asyncio.run(payments.deliver_order(fake_bot, delivery_order, "TRAN-DELIVERY"))
    finally:
        payments._verify_and_swap = original_verify_and_swap

    customer_messages = [item for item in fake_bot.messages if item[0] == 3333]
    assert len(customer_messages) == 5
    assert "THANH TOÁN THÀNH CÔNG" in customer_messages[0][1]
    assert "HỆ THỐNG ĐANG KIỂM TRA MÃ" in customer_messages[1][1]
    assert "VOUCHER 01 / 02" in customer_messages[2][1]
    assert "VOUCHER 02 / 02" in customer_messages[3][1]
    assert "ĐÃ GIAO ĐỦ 02 / 02 VOUCHER" in customer_messages[4][1]
    assert all("urbox.vn" not in item[1] for item in customer_messages)
    assert customer_messages[2][2].get("reply_markup") is None
    assert customer_messages[4][2].get("reply_markup") is not None
    assert fake_bot.deleted_messages == [(3333, 2)]
    customer_events = [event for event in fake_bot.events if event[1] == 3333]
    assert [event[0] for event in customer_events] == [
        "send", "send", "delete", "send", "send", "send", "document",
    ]
    customer_docs = [item for item in fake_bot.documents if item[0] == 3333]
    assert len(customer_docs) == 1
    doc_file = customer_docs[0][1]
    assert doc_file.filename == "JBDELIV1.html"
    barcode_html = doc_file.data.decode("utf-8")
    assert barcode_html.count('<article class="voucher-card"') == 2
    assert "UB100DELIVERY0" in barcode_html
    assert "UB100DELIVERY1" in barcode_html
    assert "100.000 đ" in barcode_html
    assert "Barcode Code 128 cho mã UB100DELIVERY0" in barcode_html
    assert "Barcode Code 128 cho mã UB100DELIVERY1" in barcode_html
    assert "data:image/png;base64," in barcode_html
    for forbidden in (
        'class="builder"',
        'id="voucher-form"',
        'id="voucher-code"',
        "URLSearchParams",
        "location.search",
        "Tạo giao diện mã",
    ):
        assert forbidden not in barcode_html, forbidden

    # --- Admin giả lập giao đơn mẫu: không trừ kho thật, không cộng doanh thu ---
    stock_before = database.stock_summary()
    delivered_before = database.count_delivered()
    revenue_before = database.revenue_delivered()
    sim_bot = FakeBot()
    asyncio.run(
        payments.simulate_order_delivery(
            sim_bot,
            chat_id=1,
            vouchers=[("UB100KTEST01", 100_000), ("UB200KTEST02", 200_000)],
            order_code="JBTEST02",
            check_delay=0.0,
        )
    )
    assert database.stock_summary() == stock_before
    assert database.count_delivered() == delivered_before
    assert database.revenue_delivered() == revenue_before
    sim_events = [event for event in sim_bot.events if event[1] == 1]
    assert [event[0] for event in sim_events] == [
        "send", "send", "delete", "send", "send", "send", "document",
    ]
    sim_docs = [item for item in sim_bot.documents if item[0] == 1]
    assert len(sim_docs) == 1
    assert sim_docs[0][1].filename == "JBTEST02.html"
    sim_html = sim_docs[0][1].data.decode("utf-8")
    assert sim_html.count('<article class="voucher-card"') == 2
    assert "UB100KTEST01" in sim_html and "UB200KTEST02" in sim_html

    # --- Admin tạo file HTML barcode từ chuỗi mã (mỗi mã 1 dòng) ---
    parsed_barcodes = handlers_admin._parse_barcode_input_lines(
        "UB100K49D1399\nUB200KABC123\nub100kxyz789"
    )
    assert parsed_barcodes == [
        ("UB100K49D1399", 100_000),
        ("UB200KABC123", 200_000),
        ("UB100KXYZ789", 100_000),
    ]

    # --- Admin nhập kho bằng text & xóa mã đã thêm vào kho ---
    assert database.add_voucher(100_000, "UB100WRONG1", "")
    assert database.add_voucher(200_000, "UB200WRONG2", "")
    stock_100_before_del = database.count_available(100_000)
    stock_200_before_del = database.count_available(200_000)
    deleted_vouchers, not_deleted_codes = database.delete_available_vouchers([
        "UB100WRONG1",
        "ub200wrong2",
        "UB100DELIVERY0",  # mã đã bán -> không được xóa
        "UBNOTEXIST99",    # mã không có trong kho
    ])
    assert deleted_vouchers == [("UB100WRONG1", 100_000), ("UB200WRONG2", 200_000)]
    assert not_deleted_codes == ["UB100DELIVERY0", "UBNOTEXIST99"]
    assert database.count_available(100_000) == stock_100_before_del - 1
    assert database.count_available(200_000) == stock_200_before_del - 1

    # --- Handler webhook: xác thực, giao 1 lần và nhận retry an toàn ---
    assert database.add_voucher(100_000, "UB100WEB", "https://urbox.vn/web")
    web_order = database.create_order_multi(
        "JBWEB001", 888, [(100_000, 1, 75_000)], 15,
    )
    assert web_order is not None
    webhook_payload = {
        "id": 456,
        "gateway": "MBBank",
        "accountNumber": config.BANK_ACCOUNT_NO,
        "transferType": "in",
        "transferAmount": web_order.total_amount,
        "content": f"Thanh toan {web_order.order_code}",
        "referenceCode": "FT123456",
    }
    webhook_body = json.dumps(webhook_payload, separators=(",", ":")).encode()
    webhook_timestamp = str(int(time.time()))
    webhook_signature = payments.build_bank_webhook_signature(
        config.SEPAY_WEBHOOK_SECRET, webhook_timestamp, webhook_body,
    )

    class FakeRequest:
        def __init__(self, signature_value: str):
            self.headers = {
                "X-SePay-Timestamp": webhook_timestamp,
                "X-SePay-Signature": signature_value,
            }
            self.app = {"bot": object()}

        async def read(self) -> bytes:
            return webhook_body

    original_deliver_order = payments.deliver_order

    async def fake_deliver_order(bot, incoming_order, tran_id):
        return database.settle_order_payment(incoming_order.id, tran_id) == "delivered"

    payments.deliver_order = fake_deliver_order
    try:
        unauthorized = asyncio.run(payments._bank_webhook_handler(FakeRequest("wrong")))
        assert unauthorized.status == 401
        accepted = asyncio.run(payments._bank_webhook_handler(FakeRequest(webhook_signature)))
        assert accepted.status == 200 and '"success": true' in accepted.text
        duplicate = asyncio.run(payments._bank_webhook_handler(FakeRequest(webhook_signature)))
        assert duplicate.status == 200 and '"duplicate": true' in duplicate.text
        assert database.get_order_by_code(web_order.order_code).tran_id == "SEPAY-BANK-456"
    finally:
        payments.deliver_order = original_deliver_order

    # --- IPN đến trễ: cấp lại mã sau khi đơn đã huỷ ---
    late_dir = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(late_dir, "late.db")
    database.init_db()
    assert database.add_voucher(100_000, "UB100LATE", "https://urbox.vn/late")
    late = database.create_order_multi(
        "JBLATE01", 555, [(100_000, 1, 75_000)], 15,
    )
    assert late is not None
    assert database.cancel_customer_order(late.order_code, late.chat_id) is not None
    assert database.settle_order_payment(late.id, "SEPAY-LATE-1") == "delivered"
    assert database.get_order_by_code(late.order_code).tran_id == "SEPAY-LATE-1"

    # Nếu voucher đã bị đơn khác giữ, ghi nhận đã trả tiền và chờ admin bổ sung kho.
    shortage_dir = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(shortage_dir, "shortage.db")
    database.init_db()
    assert database.add_voucher(100_000, "UB100ONLY", "https://urbox.vn/only")
    late = database.create_order_multi(
        "JBLATE02", 666, [(100_000, 1, 75_000)], 15,
    )
    assert late is not None
    assert database.cancel_customer_order(late.order_code, late.chat_id) is not None
    occupying = database.create_order_multi(
        "JBOCCUPY", 777, [(100_000, 1, 75_000)], 15,
    )
    assert occupying is not None
    assert database.settle_order_payment(late.id, "SEPAY-LATE-2") == "stock_shortage"
    paid_pending = database.get_order_by_code(late.order_code)
    assert paid_pending.status == "paid_pending"
    assert paid_pending.tran_id == "SEPAY-LATE-2"
    assert database.add_voucher(100_000, "UB100RESTOCK", "https://urbox.vn/restock")
    assert database.settle_order_payment(late.id, "MANUAL-IGNORED") == "delivered"
    recovered = database.get_order_by_code(late.order_code)
    assert recovered.tran_id == "SEPAY-LATE-2"

    # --- Rate-limit: giữ kết quả cũ, tạo cart mới và retry đúng mã lỗi ---
    original_open_cart = checker._open_cart
    original_load_proxies = checker.load_proxies
    original_check_voucher = checker.jollibee.check_voucher
    opened_carts = []
    checked_routes = []

    class CheckerSession:
        def __init__(self, route):
            self.route = route

        def close(self):
            pass

    def fake_open_cart(routes, start=0):
        cart = SimpleNamespace(
            cart_id=f"cart-{len(opened_carts) + 1}",
            applied_codes=set(),
        )
        opened_carts.append((start, cart.cart_id))
        return start, CheckerSession(routes[start]), cart, "", ""

    def fake_check_voucher(session, code, cart, **kwargs):
        route = "proxy" if session.route else "direct"
        checked_routes.append((route, code, cart.cart_id))
        if route == "direct" and code == "UBRATEB2":
            return {"status": "RATE_LIMITED", "message": "Thử lại sau"}
        cart.applied_codes = {code.casefold()}
        return {"status": "APPLIED", "active_in_cart": True}

    checker._open_cart = fake_open_cart
    checker.load_proxies = lambda: ["http://proxy.example:8080"]
    checker.jollibee.check_voucher = fake_check_voucher
    try:
        rate_results = checker.check_inputs(["UBRATEA1", "UBRATEB2", "UBRATEC3"])
    finally:
        checker._open_cart = original_open_cart
        checker.load_proxies = original_load_proxies
        checker.jollibee.check_voucher = original_check_voucher

    assert [result.status for result in rate_results] == ["VALID", "VALID", "VALID"]
    assert opened_carts == [(0, "cart-1"), (1, "cart-2")]
    assert checked_routes == [
        ("direct", "UBRATEA1", "cart-1"),
        ("direct", "UBRATEB2", "cart-1"),
        ("proxy", "UBRATEB2", "cart-2"),
        ("proxy", "UBRATEC3", "cart-2"),
    ]
    assert not rate_results[0].proxy_used
    assert rate_results[1].proxy_used and rate_results[2].proxy_used
    assert checker.route_cooldown_remaining(None) >= checker.ROUTE_COOLDOWN_SECONDS - 1

    # Route direct cooldown thì bỏ qua hẳn và tạo cart bằng proxy ngay từ đầu.
    original_create_cart_for_route = checker._create_cart_for_route
    created_routes = []

    def fake_create_cart_for_route(route):
        created_routes.append(route)
        return (
            CheckerSession(route),
            SimpleNamespace(cart_id="cooldown-proxy-cart", applied_codes=set()),
        )

    checker._create_cart_for_route = fake_create_cart_for_route
    try:
        route_index, route_session, route_cart, route_status, _ = checker._open_cart([
            None, "http://proxy.example:8080",
        ])
    finally:
        checker._create_cart_for_route = original_create_cart_for_route
        checker._clear_route_cooldowns()

    assert route_index == 1 and route_status == ""
    assert route_session.route == "http://proxy.example:8080"
    assert route_cart.cart_id == "cooldown-proxy-cart"
    assert created_routes == ["http://proxy.example:8080"]

    # --- Quản lý proxy: lưu nguyên credential, bỏ trùng, xóa và render admin ---
    proxy_dir = tempfile.mkdtemp()
    original_proxy_file = checker.PROXY_FILE
    checker.PROXY_FILE = os.path.join(proxy_dir, "proxy.txt")
    try:
        assert checker.proxy_entries() == []
        assert checker.add_proxies([
            "10.0.0.1:8080:user:pass",
            "http://10.0.0.2:3128",
            "10.0.0.1:8080:user:pass",
            "not-a-proxy",
        ]) == (2, 1, 1)
        entries = checker.proxy_entries()
        assert entries[0][0] == "10.0.0.1:8080:user:pass"
        assert entries[0][1] == "http://user:pass@10.0.0.1:8080"
        assert entries[1][0] == "http://10.0.0.2:3128"
        cooldown_now = time.monotonic()
        checker._mark_route_cooldown(entries[0][1], now=cooldown_now)
        assert checker.route_cooldown_remaining(entries[0][1], now=cooldown_now) == (
            checker.ROUTE_COOLDOWN_SECONDS
        )
        proxy_text, _, _, proxy_page_entries = handlers_admin._proxy_page_data(0)
        assert "10.0.0.1:8080:user:pass" in proxy_text
        assert "Cooldown 30 phút" in proxy_text
        assert len(proxy_page_entries) == 2
        original_create_cart_for_route = checker._create_cart_for_route

        def fake_proxy_route_cart(route):
            return CheckerSession(route), SimpleNamespace(
                cart_id=f"live-{route or 'direct'}", applied_codes=set(),
            )

        checker._create_cart_for_route = fake_proxy_route_cart
        try:
            route_results = checker.test_routes()
        finally:
            checker._create_cart_for_route = original_create_cart_for_route
        assert [result["route"] for result in route_results] == [
            None, "http://user:pass@10.0.0.1:8080", "http://10.0.0.2:3128",
        ]
        assert all(result["ok"] for result in route_results)
        assert route_results[1]["cooldown_seconds"] > 0
        assert checker.remove_proxy(0) == (
            "10.0.0.1:8080:user:pass", "http://user:pass@10.0.0.1:8080",
        )
        assert [raw for raw, _ in checker.proxy_entries()] == ["http://10.0.0.2:3128"]
    finally:
        checker.PROXY_FILE = original_proxy_file
        checker._clear_route_cooldowns()

    # --- Mã thay thế tiếp tục trên cùng guest cart ---
    replacement_carts = []
    replacement_calls = []

    def fake_replacement_open(routes, start=0):
        cart = SimpleNamespace(cart_id="replacement-cart", applied_codes=set())
        replacement_carts.append(cart)
        return 0, CheckerSession(None), cart, "", ""

    def fake_replacement_check(session, code, cart, **kwargs):
        replacement_calls.append((code, cart.cart_id))
        if code == "UBBAD001":
            return {"status": "USED", "message": "Đã sử dụng"}
        cart.applied_codes = {code.casefold()}
        return {"status": "APPLIED", "active_in_cart": True}

    replaced = []

    def provide_replacement(index, result):
        replaced.append((index, result.code, result.status))
        return "UBGOOD01"

    checker._open_cart = fake_replacement_open
    checker.load_proxies = lambda: []
    checker.jollibee.check_voucher = fake_replacement_check
    try:
        replacement_results = checker.check_codes_with_replacements(
            ["UBBAD001"], provide_replacement,
        )
    finally:
        checker._open_cart = original_open_cart
        checker.load_proxies = original_load_proxies
        checker.jollibee.check_voucher = original_check_voucher

    assert len(replacement_carts) == 1
    assert replacement_calls == [
        ("UBBAD001", "replacement-cart"),
        ("UBGOOD01", "replacement-cart"),
    ]
    assert replaced == [(0, "UBBAD001", "USED")]
    assert replacement_results[0].code == "UBGOOD01"
    assert replacement_results[0].status == "VALID"

    # --- Mỗi mã lỗi bị loại khỏi kho, báo admin và đổi tới mã tốt ---
    swap_dir = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(swap_dir, "swap.db")
    database.init_db()
    for code in ("UBSWAP01", "UBSWAP02", "UBSWAP03"):
        assert database.add_voucher(100_000, code, "")
    swap_order = database.create_order_multi(
        "JBSWAP01", 4242, [(100_000, 1, 75_000)], 15,
        buyer_username="swap-test",
    )
    assert swap_order is not None
    assert database.prepare_order_payment(swap_order.id, "TRAN-SWAP") == "ready"
    original_check_codes = checker.check_codes_with_replacements

    def fake_check_codes(values, replace_bad):
        assert values == ["UBSWAP01"]
        second = replace_bad(
            0,
            checker.CheckResult("UBSWAP01", "USED", code="UBSWAP01"),
        )
        assert second == "UBSWAP02"
        third = replace_bad(
            0,
            checker.CheckResult("UBSWAP02", "EXPIRED", code="UBSWAP02"),
        )
        assert third == "UBSWAP03"
        return [
            checker.CheckResult(
                "UBSWAP03", "VALID", code="UBSWAP03", jollibee_status="APPLIED",
            )
        ]

    checker.check_codes_with_replacements = fake_check_codes
    swap_bot = FakeBot()
    try:
        final, verified = asyncio.run(payments._verify_and_swap(swap_bot, swap_order))
    finally:
        checker.check_codes_with_replacements = original_check_codes

    assert verified
    assert final[0][0] == "UBSWAP03"
    assert database.get_order_vouchers(swap_order.id) == [("UBSWAP03", "")]
    voucher_statuses = dict(
        database._conn.execute("SELECT code, status FROM vouchers").fetchall()
    )
    assert voucher_statuses == {
        "UBSWAP01": "used_bad",
        "UBSWAP02": "used_bad",
        "UBSWAP03": "reserved",
    }
    admin_alerts = [text for chat_id, text, _ in swap_bot.messages if chat_id == 1]
    assert any("UBSWAP01" in text and "Đã sử dụng" in text for text in admin_alerts)
    assert any("UBSWAP02" in text and "Hết hạn" in text for text in admin_alerts)

    # Không còn mã thay thế vẫn phải loại mã hỏng khỏi đơn/kho bán.
    no_stock_dir = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(no_stock_dir, "no-stock.db")
    database.init_db()
    assert database.add_voucher(100_000, "UBONLY01", "")
    no_stock_order = database.create_order_multi(
        "JBNOSTK1", 5151, [(100_000, 1, 75_000)], 15,
    )
    assert no_stock_order is not None
    assert database.prepare_order_payment(no_stock_order.id, "TRAN-NOSTOCK") == "ready"
    bad_id = database.get_order_voucher_details(no_stock_order.id)[0][0]
    assert database.replace_voucher_in_order(no_stock_order.id, bad_id) is None
    assert database.get_order_voucher_details(no_stock_order.id) == []
    only_status = database._conn.execute(
        "SELECT status FROM vouchers WHERE code = 'UBONLY01'"
    ).fetchone()[0]
    assert only_status == "used_bad"
    assert database.get_order_by_code(no_stock_order.order_code).status == "paid_pending"

    # --- Thanh toán ví: trừ đúng một lần, giao đơn và giữ nguyên khi thiếu tiền ---
    wallet_dir = tempfile.mkdtemp()
    config.DB_PATH = os.path.join(wallet_dir, "wallet.db")
    database.init_db()
    database.touch_user(7001, "walletbuyer")
    assert database.adjust_wallet_balance(7001, 100_000, 1, "Nạp thử") == (
        "applied", 100_000,
    )
    assert database.add_voucher(100_000, "UBWALLET01", "")
    wallet_order = database.create_order_multi(
        "JBWAL001", 7001, [(100_000, 1, 75_000)], 15,
        buyer_username="walletbuyer", buyer_name="Wallet Buyer",
    )
    assert wallet_order is not None
    wallet_payment_callbacks = {
        button.callback_data
        for row in handlers_customer._payment_method_keyboard(wallet_order).inline_keyboard
        for button in row
        if button.callback_data
    }
    assert f"pay:wallet:{wallet_order.order_code}" in wallet_payment_callbacks
    assert f"pay:bank:{wallet_order.order_code}" in wallet_payment_callbacks

    wallet_status, wallet_balance, wallet_tran_id = database.pay_order_with_wallet(
        wallet_order.order_code, wallet_order.chat_id,
    )
    assert (wallet_status, wallet_balance, wallet_tran_id) == (
        "paid", 25_000, "WALLET-JBWAL001",
    )
    assert database.pay_order_with_wallet(wallet_order.order_code, wallet_order.chat_id) == (
        "invalid_status", 25_000, "WALLET-JBWAL001",
    )
    wallet_transactions = database.list_wallet_transactions(7001)
    assert [transaction.kind for transaction in wallet_transactions] == [
        "purchase", "admin_credit",
    ]
    assert wallet_transactions[0].external_id == wallet_tran_id
    paid_wallet_order = database.get_order_by_code(wallet_order.order_code)
    assert paid_wallet_order.status == "paid_pending"

    async def fake_wallet_verify(bot, incoming_order):
        details = database.get_order_voucher_details(incoming_order.id)
        return ([
            (
                code, link, denom,
                SimpleNamespace(status="VALID", valid_until="10-12-2026"),
            )
            for _, code, link, denom in details
        ], True)

    wallet_bot = FakeBot()
    original_verify_and_swap = payments._verify_and_swap
    payments._verify_and_swap = fake_wallet_verify
    try:
        assert asyncio.run(
            payments.deliver_order(
                wallet_bot,
                paid_wallet_order,
                wallet_tran_id,
                announce_payment=True,
            )
        )
    finally:
        payments._verify_and_swap = original_verify_and_swap
    assert database.get_order_by_code(wallet_order.order_code).status == "delivered"
    assert any(
        chat_id == 7001 and "THANH TOÁN THÀNH CÔNG" in text
        for chat_id, text, _ in wallet_bot.messages
    )

    database.touch_user(7002, "emptywallet")
    assert database.add_voucher(100_000, "UBWALLET02", "")
    insufficient_order = database.create_order_multi(
        "JBWAL002", 7002, [(100_000, 1, 75_000)], 15,
    )
    assert insufficient_order is not None
    assert database.pay_order_with_wallet(
        insufficient_order.order_code, insufficient_order.chat_id,
    ) == ("insufficient", 0, "")
    assert database.get_order_by_code(insufficient_order.order_code).status == "pending"

    # --- Webhook nạp ví tự động: cộng đúng một lần và gửi thông báo ---
    webhook_topup = database.create_wallet_topup(
        "NAPABCD2345", 7002, 120_000,
        username="emptywallet", buyer_name="Topup Buyer",
    )
    assert webhook_topup is not None
    topup_payload = {
        "id": 9001,
        "accountNumber": config.BANK_ACCOUNT_NO,
        "transferType": "in",
        "transferAmount": webhook_topup.amount,
        "content": f"Nap vi {webhook_topup.topup_code}",
    }
    assert payments.extract_wallet_topup_code(topup_payload) == webhook_topup.topup_code
    assert payments.bank_transaction_matches_wallet_topup(topup_payload, webhook_topup)
    topup_body = json.dumps(topup_payload, separators=(",", ":")).encode()
    topup_timestamp = str(int(time.time()))
    topup_signature = payments.build_bank_webhook_signature(
        config.SEPAY_WEBHOOK_SECRET, topup_timestamp, topup_body,
    )
    topup_bot = FakeBot()

    class WalletWebhookRequest:
        def __init__(self):
            self.headers = {
                "X-SePay-Timestamp": topup_timestamp,
                "X-SePay-Signature": topup_signature,
            }
            self.app = {"bot": topup_bot}

        async def read(self) -> bytes:
            return topup_body

    topup_response = asyncio.run(payments._bank_webhook_handler(WalletWebhookRequest()))
    assert topup_response.status == 200 and '"success": true' in topup_response.text
    assert database.wallet_balance(7002) == 120_000
    duplicate_topup = asyncio.run(payments._bank_webhook_handler(WalletWebhookRequest()))
    assert duplicate_topup.status == 200 and '"duplicate": true' in duplicate_topup.text
    assert database.wallet_balance(7002) == 120_000
    assert any(
        chat_id == 7002 and "NẠP VÍ THÀNH CÔNG" in text
        for chat_id, text, _ in topup_bot.messages
    )
    topup_detail = handlers_admin._wallet_customer_detail_data(7002, 0)
    assert topup_detail is not None
    assert "Nạp tiền SePay" in topup_detail[0]
    assert "NAPABCD2345" in topup_detail[0]

    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
