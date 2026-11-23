"""Lớp truy cập SQLite: kho voucher, đơn hàng, cấu hình gian hàng.

Toàn bộ thao tác đi qua một kết nối dùng chung + khoá, đủ nhanh cho quy mô 1 shop.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

from . import config

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


# =========================
# MODELS
# =========================
@dataclass
class Order:
    id: int
    order_code: str
    chat_id: int
    denom: int
    quantity: int
    unit_price: int
    total_amount: int
    status: str            # pending / delivered / expired / cancelled / paid_pending
    created_at: int
    expires_at: int
    tran_id: str = ""
    buyer_username: str = ""
    buyer_name: str = ""
    delivered_at: int = 0


@dataclass
class BuyerSummary:
    chat_id: int
    buyer_username: str
    buyer_name: str
    order_count: int
    voucher_quantity: int
    total_spent: int
    latest_purchase_at: int


@dataclass
class WalletTransaction:
    id: int
    chat_id: int
    amount: int
    kind: str
    note: str
    reference: str
    external_id: str
    admin_id: int
    created_at: int


@dataclass
class WalletTopup:
    id: int
    topup_code: str
    chat_id: int
    amount: int
    status: str
    created_at: int
    paid_at: int
    tran_id: str
    username: str
    buyer_name: str


@dataclass
class WalletCustomer:
    chat_id: int
    username: str
    buyer_name: str
    balance: int
    transaction_count: int
    latest_transaction_at: int


# =========================
# KHỞI TẠO
# =========================
def init_db() -> None:
    global _conn
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    with _lock:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS vouchers (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                denom       INTEGER NOT NULL,
                code        TEXT NOT NULL,
                link        TEXT NOT NULL DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'available',  -- available/reserved/sold
                imported_at INTEGER NOT NULL,
                UNIQUE(code)
            );

            CREATE TABLE IF NOT EXISTS orders (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                order_code     TEXT NOT NULL UNIQUE,
                chat_id        INTEGER NOT NULL,
                denom          INTEGER NOT NULL,
                quantity       INTEGER NOT NULL,
                unit_price     INTEGER NOT NULL,
                total_amount   INTEGER NOT NULL,
                status         TEXT NOT NULL DEFAULT 'pending',
                created_at     INTEGER NOT NULL,
                expires_at     INTEGER NOT NULL,
                tran_id        TEXT NOT NULL DEFAULT '',
                buyer_username TEXT NOT NULL DEFAULT '',
                buyer_name     TEXT NOT NULL DEFAULT '',
                delivered_at   INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS order_items (
                order_id   INTEGER NOT NULL,
                voucher_id INTEGER NOT NULL,
                PRIMARY KEY (order_id, voucher_id)
            );

            -- Chi tiết từng mệnh giá trong đơn (1 đơn có thể gồm nhiều mệnh giá).
            CREATE TABLE IF NOT EXISTS order_lines (
                order_id   INTEGER NOT NULL,
                denom      INTEGER NOT NULL,
                quantity   INTEGER NOT NULL,
                unit_price INTEGER NOT NULL,
                PRIMARY KEY (order_id, denom)
            );

            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- Khách đăng ký "báo tôi khi có hàng" cho 1 mệnh giá đang hết.
            CREATE TABLE IF NOT EXISTS stock_alerts (
                chat_id    INTEGER NOT NULL,
                denom      INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, denom)
            );

            -- Mọi khách đã từng mở bot (để gửi thông báo hàng loạt).
            CREATE TABLE IF NOT EXISTS users (
                chat_id    INTEGER PRIMARY KEY,
                username   TEXT NOT NULL DEFAULT '',
                first_seen INTEGER NOT NULL,
                last_seen  INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS wallet_transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                amount      INTEGER NOT NULL CHECK(amount <> 0),
                kind        TEXT NOT NULL,
                note        TEXT NOT NULL DEFAULT '',
                reference   TEXT NOT NULL DEFAULT '',
                external_id TEXT NOT NULL DEFAULT '',
                admin_id    INTEGER NOT NULL DEFAULT 0,
                created_at  INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS wallet_topups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                topup_code  TEXT NOT NULL UNIQUE,
                chat_id     INTEGER NOT NULL,
                amount      INTEGER NOT NULL CHECK(amount > 0),
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  INTEGER NOT NULL,
                paid_at     INTEGER NOT NULL DEFAULT 0,
                tran_id     TEXT NOT NULL DEFAULT '',
                username    TEXT NOT NULL DEFAULT '',
                buyer_name  TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_vouchers_stock ON vouchers(denom, status);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_wallet_transactions_chat
            ON wallet_transactions(chat_id, created_at DESC, id DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_wallet_transactions_external
            ON wallet_transactions(external_id) WHERE external_id <> '';
            CREATE INDEX IF NOT EXISTS idx_wallet_topups_chat
            ON wallet_topups(chat_id, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_wallet_topups_status
            ON wallet_topups(status, created_at DESC);
            """
        )
        _conn.commit()

    _migrate_orders()

    # Giá trị cấu hình mặc định (chỉ set nếu chưa có).
    _init_default("shop_title", "Gian Hàng Voucher Jollibee")
    _init_default("shop_link", "")
    _init_default("support_contact", "@YourSupportBot")
    _init_default("support_phone", "0900000000")
    for denom, price in config.DEFAULT_PRICES.items():
        _init_default(f"price_{denom}", str(price))


def _migrate_orders() -> None:
    """Thêm các cột mới vào bảng orders cho DB tạo từ phiên bản cũ."""
    with _lock:
        existing = {
            row["name"]
            for row in _conn.execute("PRAGMA table_info(orders)").fetchall()
        }
        alters = {
            "buyer_username": "TEXT NOT NULL DEFAULT ''",
            "buyer_name": "TEXT NOT NULL DEFAULT ''",
            "delivered_at": "INTEGER NOT NULL DEFAULT 0",
        }
        for col, decl in alters.items():
            if col not in existing:
                _conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {decl}")
        # Backfill order_lines cho các đơn cũ (mỗi đơn cũ chỉ có 1 mệnh giá).
        _conn.execute(
            "INSERT OR IGNORE INTO order_lines(order_id, denom, quantity, unit_price) "
            "SELECT id, denom, quantity, unit_price FROM orders WHERE denom > 0"
        )
        _conn.commit()


def _init_default(key: str, value: str) -> None:
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO config(key, value) VALUES (?, ?)", (key, value)
        )
        _conn.commit()


# =========================
# CONFIG (cấu hình gian hàng)
# =========================
def get_config(key: str, default: str = "") -> str:
    with _lock:
        row = _conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_config(key: str, value: str) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO config(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        _conn.commit()


def get_price(denom: int) -> int:
    return int(get_config(f"price_{denom}", str(config.DEFAULT_PRICES.get(denom, 0))))


def set_price(denom: int, price: int) -> None:
    set_config(f"price_{denom}", str(price))


# =========================
# KHO VOUCHER
# =========================
def add_voucher(denom: int, code: str, link: str) -> bool:
    """Thêm 1 mã vào kho. Trả về False nếu mã đã tồn tại (trùng)."""
    with _lock:
        try:
            _conn.execute(
                "INSERT INTO vouchers(denom, code, link, status, imported_at) "
                "VALUES (?, ?, ?, 'available', ?)",
                (denom, code, link, int(time.time())),
            )
            _conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def count_available(denom: int) -> int:
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) c FROM vouchers WHERE denom = ? AND status = 'available'",
            (denom,),
        ).fetchone()
    return row["c"]


def stock_summary() -> dict[int, int]:
    return {denom: count_available(denom) for denom in config.DENOMINATIONS}


def list_available_vouchers() -> list[tuple[int, str, str]]:
    """Các mã CÒN TRONG KHO (available): list (denom, code, link), gom theo mệnh giá."""
    with _lock:
        rows = _conn.execute(
            "SELECT denom, code, link FROM vouchers WHERE status = 'available' "
            "ORDER BY denom DESC, id"
        ).fetchall()
    return [(r["denom"], r["code"], r["link"]) for r in rows]


# =========================
# NGƯỜI DÙNG (để gửi thông báo hàng loạt)
# =========================
def touch_user(chat_id: int, username: str = "") -> None:
    """Ghi nhận 1 khách đã tương tác (upsert). Cập nhật last_seen mỗi lần."""
    now = int(time.time())
    with _lock:
        _conn.execute(
            "INSERT INTO users(chat_id, username, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "username = excluded.username, last_seen = excluded.last_seen",
            (chat_id, username, now, now),
        )
        _conn.commit()


def all_user_chat_ids() -> list[int]:
    """Tất cả chat_id đã từng mở bot — dùng cho broadcast."""
    with _lock:
        rows = _conn.execute("SELECT chat_id FROM users").fetchall()
    return [r["chat_id"] for r in rows]


def count_users() -> int:
    with _lock:
        row = _conn.execute("SELECT COUNT(*) c FROM users").fetchone()
    return row["c"]


# =========================
# VÍ KHÁCH HÀNG
# =========================
def _wallet_balance_locked(chat_id: int) -> int:
    row = _conn.execute(
        "SELECT COALESCE(SUM(amount), 0) balance FROM wallet_transactions "
        "WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    return int(row["balance"])


def _insert_wallet_transaction_locked(
    chat_id: int,
    amount: int,
    kind: str,
    note: str = "",
    reference: str = "",
    external_id: str = "",
    admin_id: int = 0,
) -> None:
    _conn.execute(
        "INSERT INTO wallet_transactions("
        "chat_id, amount, kind, note, reference, external_id, admin_id, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            chat_id,
            amount,
            kind,
            note.strip()[:300],
            reference.strip()[:100],
            external_id.strip()[:100],
            admin_id,
            int(time.time()),
        ),
    )


def wallet_balance(chat_id: int) -> int:
    with _lock:
        return _wallet_balance_locked(chat_id)


def wallet_summary() -> tuple[int, int]:
    """Trả về (số khách đã có giao dịch ví, tổng số dư đang lưu)."""
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(DISTINCT chat_id) customers, "
            "COALESCE(SUM(amount), 0) balance FROM wallet_transactions"
        ).fetchone()
    return int(row["customers"]), int(row["balance"])


def wallet_customer_exists(chat_id: int) -> bool:
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM users WHERE chat_id = ? "
            "UNION SELECT 1 FROM orders WHERE chat_id = ? "
            "UNION SELECT 1 FROM wallet_transactions WHERE chat_id = ? LIMIT 1",
            (chat_id, chat_id, chat_id),
        ).fetchone()
    return row is not None


def _row_to_wallet_transaction(row: sqlite3.Row) -> WalletTransaction:
    return WalletTransaction(
        id=row["id"],
        chat_id=row["chat_id"],
        amount=row["amount"],
        kind=row["kind"],
        note=row["note"],
        reference=row["reference"],
        external_id=row["external_id"],
        admin_id=row["admin_id"],
        created_at=row["created_at"],
    )


def list_wallet_transactions(
    chat_id: int, limit: int = 10, offset: int = 0,
) -> list[WalletTransaction]:
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM wallet_transactions WHERE chat_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (chat_id, max(1, limit), max(0, offset)),
        ).fetchall()
    return [_row_to_wallet_transaction(row) for row in rows]


def count_wallet_transactions(chat_id: int) -> int:
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) count FROM wallet_transactions WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
    return int(row["count"])


def _row_to_wallet_customer(row: sqlite3.Row) -> WalletCustomer:
    return WalletCustomer(
        chat_id=row["chat_id"],
        username=row["username"] or "",
        buyer_name=row["buyer_name"] or "",
        balance=int(row["balance"]),
        transaction_count=int(row["transaction_count"]),
        latest_transaction_at=int(row["latest_transaction_at"]),
    )


def count_wallet_customers() -> int:
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) count FROM ("
            "SELECT chat_id FROM users "
            "UNION SELECT chat_id FROM orders "
            "UNION SELECT chat_id FROM wallet_transactions"
            ")"
        ).fetchone()
    return int(row["count"])


def list_wallet_customers(limit: int = 8, offset: int = 0) -> list[WalletCustomer]:
    with _lock:
        rows = _conn.execute(
            "WITH customer_ids AS ("
            "SELECT chat_id FROM users "
            "UNION SELECT chat_id FROM orders "
            "UNION SELECT chat_id FROM wallet_transactions"
            ") "
            "SELECT c.chat_id, "
            "COALESCE(NULLIF((SELECT u.username FROM users u WHERE u.chat_id = c.chat_id), ''), "
            "(SELECT o.buyer_username FROM orders o WHERE o.chat_id = c.chat_id "
            "ORDER BY o.created_at DESC, o.id DESC LIMIT 1), '') username, "
            "COALESCE((SELECT o.buyer_name FROM orders o WHERE o.chat_id = c.chat_id "
            "ORDER BY o.created_at DESC, o.id DESC LIMIT 1), '') buyer_name, "
            "COALESCE((SELECT SUM(w.amount) FROM wallet_transactions w "
            "WHERE w.chat_id = c.chat_id), 0) balance, "
            "(SELECT COUNT(*) FROM wallet_transactions w WHERE w.chat_id = c.chat_id) "
            "transaction_count, "
            "COALESCE((SELECT MAX(w.created_at) FROM wallet_transactions w "
            "WHERE w.chat_id = c.chat_id), 0) latest_transaction_at, "
            "COALESCE((SELECT u.last_seen FROM users u WHERE u.chat_id = c.chat_id), 0) "
            "last_seen FROM customer_ids c "
            "ORDER BY CASE WHEN transaction_count > 0 THEN 0 ELSE 1 END, "
            "latest_transaction_at DESC, last_seen DESC, c.chat_id "
            "LIMIT ? OFFSET ?",
            (max(1, limit), max(0, offset)),
        ).fetchall()
    return [_row_to_wallet_customer(row) for row in rows]


def get_wallet_customer(chat_id: int) -> Optional[WalletCustomer]:
    with _lock:
        row = _conn.execute(
            "WITH customer_ids AS ("
            "SELECT chat_id FROM users WHERE chat_id = ? "
            "UNION SELECT chat_id FROM orders WHERE chat_id = ? "
            "UNION SELECT chat_id FROM wallet_transactions WHERE chat_id = ?"
            ") "
            "SELECT c.chat_id, "
            "COALESCE(NULLIF((SELECT u.username FROM users u WHERE u.chat_id = c.chat_id), ''), "
            "(SELECT o.buyer_username FROM orders o WHERE o.chat_id = c.chat_id "
            "ORDER BY o.created_at DESC, o.id DESC LIMIT 1), '') username, "
            "COALESCE((SELECT o.buyer_name FROM orders o WHERE o.chat_id = c.chat_id "
            "ORDER BY o.created_at DESC, o.id DESC LIMIT 1), '') buyer_name, "
            "COALESCE((SELECT SUM(w.amount) FROM wallet_transactions w "
            "WHERE w.chat_id = c.chat_id), 0) balance, "
            "(SELECT COUNT(*) FROM wallet_transactions w WHERE w.chat_id = c.chat_id) "
            "transaction_count, "
            "COALESCE((SELECT MAX(w.created_at) FROM wallet_transactions w "
            "WHERE w.chat_id = c.chat_id), 0) latest_transaction_at "
            "FROM customer_ids c",
            (chat_id, chat_id, chat_id),
        ).fetchone()
    return _row_to_wallet_customer(row) if row else None


def adjust_wallet_balance(
    chat_id: int, delta: int, admin_id: int, note: str = "",
) -> tuple[str, int]:
    """Admin cộng/trừ ví. Không cho số dư âm."""
    with _lock:
        current = _wallet_balance_locked(chat_id)
        if delta == 0:
            return "unchanged", current
        if current + delta < 0:
            return "insufficient", current
        now = int(time.time())
        _conn.execute(
            "INSERT INTO users(chat_id, username, first_seen, last_seen) "
            "VALUES (?, '', ?, ?) ON CONFLICT(chat_id) DO NOTHING",
            (chat_id, now, now),
        )
        _insert_wallet_transaction_locked(
            chat_id,
            delta,
            "admin_credit" if delta > 0 else "admin_debit",
            note or "Admin điều chỉnh số dư",
            admin_id=admin_id,
        )
        _conn.commit()
        return "applied", current + delta


def set_wallet_balance(
    chat_id: int, target: int, admin_id: int, note: str = "",
) -> tuple[str, int]:
    if target < 0:
        return "invalid", wallet_balance(chat_id)
    with _lock:
        current = _wallet_balance_locked(chat_id)
        delta = target - current
        if delta == 0:
            return "unchanged", current
        now = int(time.time())
        _conn.execute(
            "INSERT INTO users(chat_id, username, first_seen, last_seen) "
            "VALUES (?, '', ?, ?) ON CONFLICT(chat_id) DO NOTHING",
            (chat_id, now, now),
        )
        _insert_wallet_transaction_locked(
            chat_id,
            delta,
            "admin_set",
            note or f"Admin đặt số dư thành {target}",
            admin_id=admin_id,
        )
        _conn.commit()
        return "applied", target


def wallet_topup_code_exists(topup_code: str) -> bool:
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM wallet_topups WHERE topup_code = ?", (topup_code,)
        ).fetchone()
    return row is not None


def _row_to_wallet_topup(row: sqlite3.Row) -> WalletTopup:
    return WalletTopup(
        id=row["id"],
        topup_code=row["topup_code"],
        chat_id=row["chat_id"],
        amount=row["amount"],
        status=row["status"],
        created_at=row["created_at"],
        paid_at=row["paid_at"],
        tran_id=row["tran_id"],
        username=row["username"],
        buyer_name=row["buyer_name"],
    )


def create_wallet_topup(
    topup_code: str,
    chat_id: int,
    amount: int,
    username: str = "",
    buyer_name: str = "",
) -> Optional[WalletTopup]:
    if amount <= 0:
        return None
    now = int(time.time())
    with _lock:
        try:
            _conn.execute(
                "INSERT INTO users(chat_id, username, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                "username = excluded.username, last_seen = excluded.last_seen",
                (chat_id, username, now, now),
            )
            cur = _conn.execute(
                "INSERT INTO wallet_topups("
                "topup_code, chat_id, amount, created_at, username, buyer_name"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (topup_code, chat_id, amount, now, username, buyer_name),
            )
            _conn.commit()
        except sqlite3.IntegrityError:
            _conn.rollback()
            return None
        row = _conn.execute(
            "SELECT * FROM wallet_topups WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return _row_to_wallet_topup(row)


def get_wallet_topup(topup_code: str) -> Optional[WalletTopup]:
    with _lock:
        row = _conn.execute(
            "SELECT * FROM wallet_topups WHERE topup_code = ?", (topup_code,)
        ).fetchone()
    return _row_to_wallet_topup(row) if row else None


def list_pending_wallet_topups(chat_id: int, limit: int = 5) -> list[WalletTopup]:
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM wallet_topups WHERE chat_id = ? AND status = 'pending' "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (chat_id, max(1, limit)),
        ).fetchall()
    return [_row_to_wallet_topup(row) for row in rows]


def complete_wallet_topup(topup_code: str, tran_id: str) -> tuple[str, int]:
    """Cộng tiền đúng một lần khi webhook xác nhận giao dịch nạp ví."""
    if not tran_id:
        return "invalid", 0
    with _lock:
        row = _conn.execute(
            "SELECT * FROM wallet_topups WHERE topup_code = ?", (topup_code,)
        ).fetchone()
        if row is None:
            return "not_found", 0
        current = _wallet_balance_locked(row["chat_id"])
        if row["status"] == "paid":
            status = "duplicate" if row["tran_id"] == tran_id else "already_paid"
            return status, current
        duplicate = _conn.execute(
            "SELECT 1 FROM orders WHERE tran_id = ? "
            "UNION SELECT 1 FROM wallet_transactions WHERE external_id = ? LIMIT 1",
            (tran_id, tran_id),
        ).fetchone()
        if duplicate is not None:
            return "duplicate_transaction", current
        try:
            _insert_wallet_transaction_locked(
                row["chat_id"],
                row["amount"],
                "topup",
                f"Nạp ví {topup_code}",
                reference=topup_code,
                external_id=tran_id,
            )
            _conn.execute(
                "UPDATE wallet_topups SET status = 'paid', paid_at = ?, tran_id = ? "
                "WHERE id = ? AND status = 'pending'",
                (int(time.time()), tran_id, row["id"]),
            )
            _conn.commit()
        except sqlite3.IntegrityError:
            _conn.rollback()
            return "duplicate_transaction", current
        return "credited", current + row["amount"]


def pay_order_with_wallet(
    order_code: str, chat_id: int,
) -> tuple[str, int, str]:
    """Trừ ví và ghi nhận đơn đã thanh toán trong cùng một giao dịch DB."""
    now = int(time.time())
    with _lock:
        row = _conn.execute(
            "SELECT * FROM orders WHERE order_code = ? AND chat_id = ?",
            (order_code, chat_id),
        ).fetchone()
        current = _wallet_balance_locked(chat_id)
        if row is None:
            return "not_found", current, ""
        if row["status"] != "pending":
            return "invalid_status", current, row["tran_id"]
        if row["expires_at"] <= now:
            _conn.execute(
                "UPDATE orders SET status = 'expired' WHERE id = ? AND status = 'pending'",
                (row["id"],),
            )
            _conn.execute(
                "UPDATE vouchers SET status = 'available' WHERE status = 'reserved' "
                "AND id IN (SELECT voucher_id FROM order_items WHERE order_id = ?)",
                (row["id"],),
            )
            _conn.execute("DELETE FROM order_items WHERE order_id = ?", (row["id"],))
            _conn.commit()
            return "expired", current, ""
        if current < row["total_amount"]:
            return "insufficient", current, ""
        tran_id = f"WALLET-{row['order_code']}"
        _insert_wallet_transaction_locked(
            chat_id,
            -row["total_amount"],
            "purchase",
            f"Thanh toán đơn {row['order_code']}",
            reference=row["order_code"],
            external_id=tran_id,
        )
        _conn.execute(
            "UPDATE orders SET status = 'paid_pending', tran_id = ? "
            "WHERE id = ? AND status = 'pending'",
            (tran_id, row["id"]),
        )
        _conn.commit()
        return "paid", current - row["total_amount"], tran_id


# =========================
# CHỜ HÀNG (báo khách khi nhập kho)
# =========================
def add_stock_alert(chat_id: int, denom: int) -> bool:
    """Đăng ký báo khi có hàng. False nếu khách đã đăng ký loại này rồi."""
    with _lock:
        try:
            _conn.execute(
                "INSERT INTO stock_alerts(chat_id, denom, created_at) VALUES (?, ?, ?)",
                (chat_id, denom, int(time.time())),
            )
            _conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def pop_stock_alerts(denom: int) -> list[int]:
    """Lấy danh sách chat_id đang chờ mệnh giá này rồi xoá (mỗi người báo 1 lần)."""
    with _lock:
        rows = _conn.execute(
            "SELECT chat_id FROM stock_alerts WHERE denom = ? ORDER BY created_at",
            (denom,),
        ).fetchall()
        _conn.execute("DELETE FROM stock_alerts WHERE denom = ?", (denom,))
        _conn.commit()
    return [r["chat_id"] for r in rows]


# =========================
# ĐƠN HÀNG
# =========================
def create_order_multi(
    order_code: str, chat_id: int, items: list[tuple[int, int, int]],
    timeout_minutes: int, buyer_username: str = "", buyer_name: str = "",
) -> Optional[Order]:
    """Tạo đơn gồm nhiều mệnh giá + giữ (reserve) đủ mã trong 1 giao dịch.

    items: list (denom, quantity, unit_price). None nếu kho thiếu bất kỳ loại nào.
    Với đơn 1 mệnh giá, orders.denom/unit_price giữ giá trị đó (tương thích cũ);
    đơn nhiều mệnh giá thì denom=0, unit_price=0 — chi tiết nằm ở order_lines.
    """
    items = [(d, q, p) for d, q, p in items if q > 0]
    if not items:
        return None
    now = int(time.time())
    expires_at = now + timeout_minutes * 60
    total_amount = sum(q * p for _, q, p in items)
    total_qty = sum(q for _, q, _ in items)
    denom = items[0][0] if len(items) == 1 else 0
    unit_price = items[0][2] if len(items) == 1 else 0

    with _lock:
        # Kiểm tra & gom đủ mã cho TẤT CẢ mệnh giá trước khi ghi bất cứ gì.
        reserved: list[int] = []
        for d, q, _ in items:
            cur = _conn.execute(
                "SELECT id FROM vouchers WHERE denom = ? AND status = 'available' "
                "ORDER BY id LIMIT ?",
                (d, q),
            )
            ids = [r["id"] for r in cur.fetchall()]
            if len(ids) < q:
                return None  # kho không đủ loại này
            reserved.extend(ids)

        cur = _conn.execute(
            "INSERT INTO orders(order_code, chat_id, denom, quantity, unit_price, "
            "total_amount, status, created_at, expires_at, buyer_username, buyer_name) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (order_code, chat_id, denom, total_qty, unit_price,
             total_amount, now, expires_at, buyer_username, buyer_name),
        )
        order_id = cur.lastrowid
        for d, q, p in items:
            _conn.execute(
                "INSERT INTO order_lines(order_id, denom, quantity, unit_price) "
                "VALUES (?, ?, ?, ?)",
                (order_id, d, q, p),
            )
        for vid in reserved:
            _conn.execute(
                "UPDATE vouchers SET status = 'reserved' WHERE id = ?", (vid,)
            )
            _conn.execute(
                "INSERT INTO order_items(order_id, voucher_id) VALUES (?, ?)",
                (order_id, vid),
            )
        _conn.commit()

        return Order(
            id=order_id, order_code=order_code, chat_id=chat_id, denom=denom,
            quantity=total_qty, unit_price=unit_price,
            total_amount=total_amount, status="pending",
            created_at=now, expires_at=expires_at,
            buyer_username=buyer_username, buyer_name=buyer_name,
        )


def get_order_lines(order_id: int) -> list[tuple[int, int, int]]:
    """Chi tiết mệnh giá của đơn: list (denom, quantity, unit_price)."""
    with _lock:
        rows = _conn.execute(
            "SELECT denom, quantity, unit_price FROM order_lines "
            "WHERE order_id = ? ORDER BY denom DESC",
            (order_id,),
        ).fetchall()
    return [(r["denom"], r["quantity"], r["unit_price"]) for r in rows]


def _row_to_order(row: sqlite3.Row) -> Order:
    return Order(
        id=row["id"], order_code=row["order_code"], chat_id=row["chat_id"],
        denom=row["denom"], quantity=row["quantity"], unit_price=row["unit_price"],
        total_amount=row["total_amount"], status=row["status"],
        created_at=row["created_at"], expires_at=row["expires_at"],
        tran_id=row["tran_id"], buyer_username=row["buyer_username"],
        buyer_name=row["buyer_name"], delivered_at=row["delivered_at"],
    )


def get_order_by_code(order_code: str) -> Optional[Order]:
    with _lock:
        row = _conn.execute(
            "SELECT * FROM orders WHERE order_code = ?", (order_code,)
        ).fetchone()
    return _row_to_order(row) if row else None


def list_pending_orders() -> list[Order]:
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM orders WHERE status = 'pending'"
        ).fetchall()
    return [_row_to_order(r) for r in rows]


def list_manual_confirmation_orders(limit: int = 20) -> list[Order]:
    """Các đơn admin có thể xác nhận thủ công, mới nhất trước."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM orders WHERE status IN ('pending', 'paid_pending') "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_order(row) for row in rows]


def order_code_exists(order_code: str) -> bool:
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM orders WHERE order_code = ?", (order_code,)
        ).fetchone()
    return row is not None


def list_delivered_orders(limit: int = 10) -> list[Order]:
    """Các đơn đã giao gần đây nhất (mới nhất trước)."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM orders WHERE status = 'delivered' "
            "ORDER BY delivered_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_order(r) for r in rows]


def count_delivered() -> int:
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(total_amount),0) s "
            "FROM orders WHERE status = 'delivered'"
        ).fetchone()
    return row["c"]


def revenue_delivered() -> int:
    with _lock:
        row = _conn.execute(
            "SELECT COALESCE(SUM(total_amount),0) s FROM orders WHERE status = 'delivered'"
        ).fetchone()
    return row["s"]


def count_buyers() -> int:
    """Số khách riêng biệt đã có ít nhất một đơn giao thành công."""
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(DISTINCT chat_id) c FROM orders WHERE status = 'delivered'"
        ).fetchone()
    return row["c"]


def list_buyer_summaries(limit: int = 8, offset: int = 0) -> list[BuyerSummary]:
    """Tổng hợp lịch sử mua theo khách, ưu tiên khách chi tiêu nhiều nhất."""
    with _lock:
        rows = _conn.execute(
            "SELECT o.chat_id, COUNT(*) order_count, "
            "COALESCE(SUM(o.quantity), 0) voucher_quantity, "
            "COALESCE(SUM(o.total_amount), 0) total_spent, "
            "MAX(CASE WHEN o.delivered_at > 0 THEN o.delivered_at "
            "ELSE o.created_at END) latest_purchase_at, "
            "(SELECT latest.buyer_username FROM orders latest "
            " WHERE latest.chat_id = o.chat_id AND latest.status = 'delivered' "
            " ORDER BY CASE WHEN latest.delivered_at > 0 THEN latest.delivered_at "
            " ELSE latest.created_at END DESC, latest.id DESC LIMIT 1) buyer_username, "
            "(SELECT latest.buyer_name FROM orders latest "
            " WHERE latest.chat_id = o.chat_id AND latest.status = 'delivered' "
            " ORDER BY CASE WHEN latest.delivered_at > 0 THEN latest.delivered_at "
            " ELSE latest.created_at END DESC, latest.id DESC LIMIT 1) buyer_name "
            "FROM orders o WHERE o.status = 'delivered' GROUP BY o.chat_id "
            "ORDER BY total_spent DESC, latest_purchase_at DESC, o.chat_id "
            "LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [
        BuyerSummary(
            chat_id=row["chat_id"],
            buyer_username=row["buyer_username"] or "",
            buyer_name=row["buyer_name"] or "",
            order_count=row["order_count"],
            voucher_quantity=row["voucher_quantity"],
            total_spent=row["total_spent"],
            latest_purchase_at=row["latest_purchase_at"],
        )
        for row in rows
    ]


def delivered_stats_between(start_ts: int, end_ts: int):
    """Thống kê đơn đã giao trong [start_ts, end_ts).

    Trả về (count, revenue, by_denom) với by_denom[denom] = {count, qty, revenue}.
    """
    with _lock:
        rows = _conn.execute(
            "SELECT ol.denom, COUNT(DISTINCT o.id) c, COALESCE(SUM(ol.quantity),0) q, "
            "COALESCE(SUM(ol.quantity * ol.unit_price),0) r "
            "FROM orders o JOIN order_lines ol ON ol.order_id = o.id "
            "WHERE o.status = 'delivered' "
            "AND o.delivered_at >= ? AND o.delivered_at < ? GROUP BY ol.denom",
            (start_ts, end_ts),
        ).fetchall()
        total = _conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(total_amount),0) r "
            "FROM orders WHERE status = 'delivered' "
            "AND delivered_at >= ? AND delivered_at < ?",
            (start_ts, end_ts),
        ).fetchone()
    by_denom: dict[int, dict[str, int]] = {}
    for row in rows:
        by_denom[row["denom"]] = {
            "count": row["c"], "qty": row["q"], "revenue": row["r"],
        }
    return total["c"], total["r"], by_denom


def sold_qty_by_denom() -> dict[int, int]:
    """Tổng số MÃ đã bán (đơn delivered) theo từng mệnh giá — cho báo cáo /lichsu."""
    with _lock:
        rows = _conn.execute(
            "SELECT ol.denom, COALESCE(SUM(ol.quantity),0) q "
            "FROM orders o JOIN order_lines ol ON ol.order_id = o.id "
            "WHERE o.status = 'delivered' GROUP BY ol.denom"
        ).fetchall()
    return {r["denom"]: r["q"] for r in rows}


def get_order_vouchers(order_id: int) -> list[tuple[str, str]]:
    """Trả về danh sách (code, link) các voucher thuộc đơn."""
    with _lock:
        rows = _conn.execute(
            "SELECT v.code, v.link FROM order_items oi "
            "JOIN vouchers v ON v.id = oi.voucher_id WHERE oi.order_id = ? "
            "ORDER BY v.id",
            (order_id,),
        ).fetchall()
    return [(r["code"], r["link"]) for r in rows]


def get_order_voucher_details(order_id: int) -> list[tuple[int, str, str, int]]:
    """Như get_order_vouchers nhưng kèm id voucher + mệnh giá (để đổi mã hỏng)."""
    with _lock:
        rows = _conn.execute(
            "SELECT v.id, v.code, v.link, v.denom FROM order_items oi "
            "JOIN vouchers v ON v.id = oi.voucher_id WHERE oi.order_id = ? "
            "ORDER BY v.denom DESC, v.id",
            (order_id,),
        ).fetchall()
    return [(r["id"], r["code"], r["link"], r["denom"]) for r in rows]


def replace_voucher_in_order(
    order_id: int, bad_voucher_id: int
) -> Optional[tuple[int, str, str]]:
    """Đổi 1 mã hỏng trong đơn sang mã khác còn tốt (cùng mệnh giá).

    Mã hỏng luôn bị loại khỏi đơn và đánh dấu 'used_bad', kể cả khi kho chưa
    có mã thay thế. Mã mới chỉ thành 'sold' khi đơn đã giao.
    """
    with _lock:
        row = _conn.execute(
            "SELECT v.denom, o.status FROM vouchers v "
            "JOIN order_items oi ON oi.voucher_id = v.id "
            "JOIN orders o ON o.id = oi.order_id "
            "WHERE v.id = ? AND o.id = ?",
            (bad_voucher_id, order_id),
        ).fetchone()
        if row is None:
            return None
        denom = row["denom"]
        _conn.execute(
            "UPDATE vouchers SET status = 'used_bad' WHERE id = ?", (bad_voucher_id,)
        )
        _conn.execute(
            "DELETE FROM order_items WHERE order_id = ? AND voucher_id = ?",
            (order_id, bad_voucher_id),
        )
        new = _conn.execute(
            "SELECT id, code, link FROM vouchers "
            "WHERE denom = ? AND status = 'available' ORDER BY id LIMIT 1",
            (denom,),
        ).fetchone()
        if new is None:
            _conn.commit()
            return None
        next_status = "sold" if row["status"] == "delivered" else "reserved"
        _conn.execute(
            "UPDATE vouchers SET status = ? WHERE id = ?", (next_status, new["id"])
        )
        _conn.execute(
            "INSERT INTO order_items(order_id, voucher_id) VALUES (?, ?)",
            (order_id, new["id"]),
        )
        _conn.commit()
    return new["id"], new["code"], new["link"]


def list_customer_delivered(
    chat_id: int, limit: int = 10, offset: int = 0,
) -> list[Order]:
    """Các đơn đã giao của 1 khách (mới nhất trước) — cho lệnh /donhang."""
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM orders WHERE chat_id = ? AND status = 'delivered' "
            "ORDER BY delivered_at DESC, id DESC LIMIT ? OFFSET ?",
            (chat_id, max(1, limit), max(0, offset)),
        ).fetchall()
    return [_row_to_order(r) for r in rows]


def prepare_order_payment(order_id: int, tran_id: str) -> str:
    """Ghi nhận đã nhận tiền và giữ mã, nhưng chưa đánh dấu đã giao."""
    with _lock:
        row = _conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if row is None:
            return "not_found"

        if row["status"] == "delivered":
            return "already_delivered"

        effective_tran_id = row["tran_id"] or tran_id
        if not effective_tran_id:
            return "invalid_status"
        duplicate = _conn.execute(
            "SELECT 1 FROM orders WHERE tran_id = ? AND id <> ?",
            (effective_tran_id, order_id),
        ).fetchone()
        if duplicate is not None:
            return "duplicate_transaction"

        if row["status"] not in {"pending", "expired", "cancelled", "paid_pending"}:
            return "invalid_status"

        if row["status"] in {"expired", "cancelled"}:
            _conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))

        lines = _conn.execute(
            "SELECT denom, quantity FROM order_lines WHERE order_id = ?",
            (order_id,),
        ).fetchall()
        current = _conn.execute(
            "SELECT v.id, v.denom FROM vouchers v "
            "JOIN order_items oi ON oi.voucher_id = v.id "
            "WHERE oi.order_id = ? AND v.status = 'reserved'",
            (order_id,),
        ).fetchall()
        current_counts: dict[int, int] = {}
        for voucher in current:
            current_counts[voucher["denom"]] = current_counts.get(voucher["denom"], 0) + 1
        if all(current_counts.get(line["denom"], 0) == line["quantity"] for line in lines):
            _conn.execute(
                "UPDATE orders SET status = 'paid_pending', tran_id = ? WHERE id = ?",
                (effective_tran_id, order_id),
            )
            _conn.commit()
            return "ready"

        allocated: list[int] = []
        for line in lines:
            vouchers = _conn.execute(
                "SELECT id FROM vouchers WHERE denom = ? AND "
                "(status = 'available' OR (status = 'reserved' AND id IN "
                "(SELECT voucher_id FROM order_items WHERE order_id = ?))) "
                "ORDER BY id LIMIT ?",
                (line["denom"], order_id, line["quantity"]),
            ).fetchall()
            if len(vouchers) < line["quantity"]:
                _conn.execute(
                    "UPDATE orders SET status = 'paid_pending', tran_id = ? WHERE id = ?",
                    (effective_tran_id, order_id),
                )
                _conn.commit()
                return "stock_shortage"
            allocated.extend(voucher["id"] for voucher in vouchers)

        _conn.execute(
            "UPDATE vouchers SET status = 'available' WHERE status = 'reserved' AND id IN "
            "(SELECT voucher_id FROM order_items WHERE order_id = ?)",
            (order_id,),
        )
        _conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
        for voucher_id in allocated:
            _conn.execute(
                "UPDATE vouchers SET status = 'reserved' WHERE id = ?",
                (voucher_id,),
            )
            _conn.execute(
                "INSERT INTO order_items(order_id, voucher_id) VALUES (?, ?)",
                (order_id, voucher_id),
            )
        _conn.execute(
            "UPDATE orders SET status = 'paid_pending', tran_id = ? WHERE id = ?",
            (effective_tran_id, order_id),
        )
        _conn.commit()
        return "ready"


def complete_order_delivery(order_id: int) -> bool:
    """Đánh dấu đã giao sau khi checker xác nhận toàn bộ mã sử dụng được."""
    now = int(time.time())
    with _lock:
        row = _conn.execute(
            "SELECT COALESCE(SUM(quantity), 0) quantity FROM order_lines WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        reserved = _conn.execute(
            "SELECT COUNT(*) quantity FROM vouchers v "
            "JOIN order_items oi ON oi.voucher_id = v.id "
            "WHERE oi.order_id = ? AND v.status = 'reserved'",
            (order_id,),
        ).fetchone()
        if row["quantity"] == 0 or reserved["quantity"] != row["quantity"]:
            return False
        cur = _conn.execute(
            "UPDATE orders SET status = 'delivered', delivered_at = ? "
            "WHERE id = ? AND status = 'paid_pending' AND tran_id <> ''",
            (now, order_id),
        )
        if cur.rowcount == 0:
            _conn.commit()
            return False
        _conn.execute(
            "UPDATE vouchers SET status = 'sold' WHERE id IN "
            "(SELECT voucher_id FROM order_items WHERE order_id = ?)",
            (order_id,),
        )
        _conn.commit()
        return True


def settle_order_payment(order_id: int, tran_id: str) -> str:
    """API tương thích cũ: chuẩn bị rồi hoàn tất giao ngay."""
    prepared = prepare_order_payment(order_id, tran_id)
    if prepared != "ready":
        return prepared
    return "delivered" if complete_order_delivery(order_id) else "invalid_status"


def mark_order_paid_delivered(order_id: int, tran_id: str) -> bool:
    """Tương thích API cũ: True chỉ khi lần gọi này vừa giao thành công."""
    return settle_order_payment(order_id, tran_id) == "delivered"


def cancel_customer_order(order_code: str, chat_id: int) -> Optional[Order]:
    """Khách tự huỷ đơn đang chờ thanh toán của chính mình, trả mã về kho.

    Trả về Order nếu huỷ thành công, None nếu đơn không tồn tại / không phải
    của khách này / không còn ở trạng thái pending.
    """
    with _lock:
        row = _conn.execute(
            "SELECT * FROM orders WHERE order_code = ?", (order_code,)
        ).fetchone()
        if row is None or row["chat_id"] != chat_id:
            return None
        cur = _conn.execute(
            "UPDATE orders SET status = 'cancelled' "
            "WHERE id = ? AND status = 'pending'",
            (row["id"],),
        )
        if cur.rowcount == 0:
            _conn.commit()
            return None
        _conn.execute(
            "UPDATE vouchers SET status = 'available' WHERE id IN "
            "(SELECT voucher_id FROM order_items WHERE order_id = ?)",
            (row["id"],),
        )
        _conn.execute("DELETE FROM order_items WHERE order_id = ?", (row["id"],))
        _conn.commit()
        return _row_to_order(row)


def customer_stats(chat_id: int) -> tuple[int, int]:
    """(số đơn đã mua, tổng tiền đã chi) của 1 khách — cho trang lịch sử."""
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(total_amount),0) s "
            "FROM orders WHERE chat_id = ? AND status = 'delivered'",
            (chat_id,),
        ).fetchone()
    return row["c"], row["s"]


def expire_order(order_id: int) -> bool:
    """Huỷ đơn quá hạn, trả voucher về kho. False nếu đơn không còn 'pending'."""
    with _lock:
        cur = _conn.execute(
            "UPDATE orders SET status = 'expired' WHERE id = ? AND status = 'pending'",
            (order_id,),
        )
        if cur.rowcount == 0:
            _conn.commit()
            return False
        _conn.execute(
            "UPDATE vouchers SET status = 'available' WHERE id IN "
            "(SELECT voucher_id FROM order_items WHERE order_id = ?)",
            (order_id,),
        )
        _conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
        _conn.commit()
    return True


def tran_already_used(tran_id: str) -> bool:
    """Giao dịch ngân hàng này đã được gán cho đơn nào chưa (chống trùng)."""
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM orders WHERE tran_id = ?", (tran_id,)
        ).fetchone()
    return row is not None
