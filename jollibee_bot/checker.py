"""Kiểm tra voucher bằng UrBox và guest cart Jollibee.

Mỗi lượt dùng một guest cart cho tới khi bị rate-limit. Khi đó checker giữ kết
quả đã có, đổi proxy, tạo cart mới và thử lại từ chính mã vừa bị giới hạn. Link
UrBox được đọc để lấy mã/ngày hạn; mã chỉ được xem là dùng được khi áp thành
công vào cart Jollibee.
"""

from __future__ import annotations

import html
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import requests

from logic_check_jollibee_code.worker import jollibee, urbox

from . import database

CHECK_INTERVAL = float(os.getenv("VOUCHER_CHECK_INTERVAL", "1.5"))
PROXY_FILE = os.getenv("JOLLIBEE_PROXY_FILE", "proxy.txt")
ROUTE_COOLDOWN_SECONDS = max(
    60, int(os.getenv("JOLLIBEE_ROUTE_COOLDOWN_SECONDS", "1800")),
)

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
UB_CODE_RE = re.compile(r"(?<![A-Z0-9])UB[A-Z0-9]{6,}(?![A-Z0-9])", re.IGNORECASE)
URBOX_LINK_RE = re.compile(r"https?://[^\"'\s<>]+urbox\.vn[^\"'\s<>]*", re.IGNORECASE)

BAD_STATUSES = {"USED", "EXPIRED", "NOT_ACTIVE", "INVALID", "REJECTED", "NOT_APPLIED"}
FALLBACK_STATUSES = {"RATE_LIMITED", "AUTH_OR_BLOCKED"}

STATUS_LABELS = {
    "VALID": "Sử dụng được",
    "USED": "Đã sử dụng",
    "EXPIRED": "Hết hạn",
    "NOT_ACTIVE": "Chưa có hiệu lực",
    "INVALID": "Không hợp lệ",
    "REJECTED": "Bị từ chối",
    "NOT_APPLIED": "Không áp được",
    "RATE_LIMITED": "Bị giới hạn lượt kiểm tra",
    "AUTH_OR_BLOCKED": "API chặn kết nối",
    "NETWORK_ERROR": "Lỗi mạng",
    "SETUP_FAILED": "Không tạo được giỏ kiểm tra",
    "ROUTES_COOLDOWN": "Tất cả route đang cooldown",
    "NOT_TESTED": "Chưa kiểm tra",
    "ERROR": "Lỗi kiểm tra",
}

_limiter = urbox.RateLimiter(max(0.5, CHECK_INTERVAL))
_jollibee_lock = threading.Lock()
_proxy_lock = threading.RLock()
_route_cooldown_lock = threading.Lock()
_route_cooldowns: dict[str, float] = {}


@dataclass
class CheckResult:
    input_url: str
    status: str
    code: str = ""
    urbox_url: str = ""
    valid_until: str = ""
    effective_from: str = ""
    error: str = ""
    jollibee_status: str = ""
    message: str = ""
    proxy_used: bool = False
    active_in_cart: bool | None = None
    cart_status: str = ""

    @property
    def usable(self) -> bool:
        return self.status == "VALID"


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status or "Không rõ")


def extract_inputs(text: str) -> list[str]:
    """Lấy link hoặc mã UB từ nội dung dán/file, giữ thứ tự và bỏ trùng."""
    matches: list[tuple[int, str]] = []
    matches.extend((m.start(), m.group(0).rstrip(".,);]")) for m in URL_RE.finditer(text))
    matches.extend((m.start(), m.group(0).upper()) for m in UB_CODE_RE.finditer(text))
    seen: set[str] = set()
    output: list[str] = []
    for _, value in sorted(matches):
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            output.append(value)
    return output


def _proxy_path() -> Path:
    candidates = [Path(PROXY_FILE)]
    if PROXY_FILE == "proxy.txt":
        candidates.append(Path("logic_check_jollibee_code/proxy.txt"))
    return next((path for path in candidates if path.is_file()), candidates[0])


def _format_proxy(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    candidate = value
    if not value.casefold().startswith(("http://", "https://", "socks4://", "socks5://")):
        if "@" in value:
            candidate = "http://" + value
        else:
            parts = value.split(":", 3)
            if len(parts) == 4:
                host, port, username, password = parts
                candidate = (
                    f"http://{quote(username, safe='')}:{quote(password, safe='')}"
                    f"@{host}:{port}"
                )
            elif len(parts) == 2:
                candidate = "http://" + value
            else:
                return ""
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https", "socks4", "socks5"}
        or not parsed.hostname
        or port is None
        or not 1 <= port <= 65535
    ):
        return ""
    return candidate


def proxy_file_path() -> Path:
    return _proxy_path()


def _read_proxy_file() -> tuple[Path, list[str]]:
    path = _proxy_path()
    if not path.is_file():
        return path, []
    return path, path.read_text(encoding="utf-8", errors="ignore").splitlines()


def _proxy_records(lines: list[str]) -> list[tuple[int, str, str]]:
    records: list[tuple[int, str, str]] = []
    for line_index, raw in enumerate(lines):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        proxy = _format_proxy(value)
        if proxy:
            records.append((line_index, value, proxy))
    return records


def _write_proxy_file(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines).rstrip()
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        if content:
            handle.write(content + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def proxy_entries() -> list[tuple[str, str]]:
    """Danh sách (dòng gốc, URL proxy được checker sử dụng), không che credential."""
    with _proxy_lock:
        _, lines = _read_proxy_file()
        return [(raw, proxy) for _, raw, proxy in _proxy_records(lines)]


def load_proxies() -> list[str]:
    proxies: list[str] = []
    for _, proxy in proxy_entries():
        if proxy and proxy not in proxies:
            proxies.append(proxy)
    return proxies


def proxy_count() -> int:
    return len(load_proxies())


def add_proxies(values: list[str]) -> tuple[int, int, int]:
    """Thêm proxy, trả về (đã thêm, trùng, không hợp lệ)."""
    with _proxy_lock:
        path, lines = _read_proxy_file()
        existing = {proxy for _, _, proxy in _proxy_records(lines)}
        added = duplicate = invalid = 0
        for raw in values:
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            proxy = _format_proxy(value)
            if not proxy:
                invalid += 1
            elif proxy in existing:
                duplicate += 1
            else:
                lines.append(value)
                existing.add(proxy)
                added += 1
        if added:
            _write_proxy_file(path, lines)
        return added, duplicate, invalid


def remove_proxy(index: int) -> tuple[str, str] | None:
    """Xóa proxy theo vị trí đang hiển thị, trả về (dòng gốc, URL chuẩn hóa)."""
    with _proxy_lock:
        path, lines = _read_proxy_file()
        records = _proxy_records(lines)
        if index < 0 or index >= len(records):
            return None
        line_index, raw, proxy = records[index]
        del lines[line_index]
        _write_proxy_file(path, lines)
    clear_route_cooldown(proxy)
    return raw, proxy


def _route_key(proxy: str | None) -> str:
    return proxy or "__direct__"


def _mark_route_cooldown(proxy: str | None, now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    with _route_cooldown_lock:
        _route_cooldowns[_route_key(proxy)] = current + ROUTE_COOLDOWN_SECONDS


def clear_route_cooldown(proxy: str | None) -> None:
    with _route_cooldown_lock:
        _route_cooldowns.pop(_route_key(proxy), None)


def route_cooldown_remaining(proxy: str | None, now: float | None = None) -> int:
    current = time.monotonic() if now is None else now
    key = _route_key(proxy)
    with _route_cooldown_lock:
        deadline = _route_cooldowns.get(key, 0.0)
        remaining = int(max(0.0, deadline - current))
        if remaining == 0:
            _route_cooldowns.pop(key, None)
        return remaining


def cooldown_route_count() -> int:
    return sum(
        route_cooldown_remaining(route) > 0
        for route in [None, *load_proxies()]
    )


def _clear_route_cooldowns() -> None:
    with _route_cooldown_lock:
        _route_cooldowns.clear()


def _resolve_urbox_url(input_url: str, session: requests.Session) -> str:
    if "urbox.vn" in input_url.casefold():
        return input_url
    _limiter.wait()
    source = urbox.fetch_html(input_url, session)
    match = URBOX_LINK_RE.search(source)
    return html.unescape(match.group(0)) if match else input_url


def _read_input(value: str, session: requests.Session | None) -> CheckResult:
    code_match = UB_CODE_RE.fullmatch(value.strip())
    if code_match:
        return CheckResult(input_url=value, status="PENDING", code=code_match.group(0).upper())
    if session is None:
        return CheckResult(input_url=value, status="ERROR", error="Đầu vào không hợp lệ")
    try:
        url = _resolve_urbox_url(value, session)
        _limiter.wait()
        parsed = urbox.parse_urbox_page(urbox.fetch_html(url, session))
        if parsed is None:
            return CheckResult(
                input_url=value, status="ERROR", urbox_url=url,
                error="Không đọc được mã từ trang UrBox",
            )
        code, urbox_status, valid_until, effective = parsed
        if urbox_status == "USED":
            return CheckResult(
                input_url=value, status="USED", code=code, urbox_url=url,
                valid_until=valid_until, effective_from=effective,
                jollibee_status="SKIPPED",
                message="UrBox đã đánh dấu mã được sử dụng",
            )
        return CheckResult(
            input_url=value, status="PENDING", code=code, urbox_url=url,
            valid_until=valid_until, effective_from=effective,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            input_url=value, status="ERROR",
            error=f"{type(exc).__name__}: {exc}"[:200],
        )


def _set_route(session: requests.Session, proxy: str | None) -> None:
    session.proxies.clear()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})


def _persist_app_version(version: str) -> None:
    try:
        database.set_config("jollibee_app_version", version)
    except Exception:
        pass


jollibee.register_version_listener(_persist_app_version)


def current_app_version() -> str:
    """Chọn phiên bản app Jollibee cao nhất giữa mặc định, runtime, .env và DB."""
    candidates = [
        jollibee.DEFAULT_APP_VERSION,
        jollibee.get_active_app_version(),
        os.getenv("JOLLIBEE_APP_VERSION", "").strip(),
    ]
    try:
        db_ver = database.get_config("jollibee_app_version", "").strip()
        if db_ver:
            candidates.append(db_ver)
    except Exception:
        pass

    valid = [ver for ver in candidates if jollibee.version_key(ver) != (0,)]
    best = max(valid, key=jollibee.version_key) if valid else jollibee.DEFAULT_APP_VERSION
    jollibee.set_active_app_version(best)
    return best


def _make_jollibee_session(proxy: str | None) -> requests.Session:
    session = jollibee.make_session(
        app_version=current_app_version(),
        store=os.getenv("JOLLIBEE_STORE", "default"),
        lang=os.getenv("JOLLIBEE_LANG", "vi"),
    )
    _set_route(session, proxy)
    return session


def _error_status(message: str) -> str:
    status = jollibee.classify_api_error(message)
    if status == "API_ERROR":
        rejected = jollibee.classify_rejection(message)
        if rejected != "REJECTED":
            return rejected
    return status


def _create_cart_for_route(proxy: str | None):
    session = _make_jollibee_session(proxy)
    try:
        options = jollibee.fetch_default_bundle_options(
            session,
            sku=os.getenv("JOLLIBEE_TEST_PRODUCT_SKU", jollibee.DEFAULT_TEST_SKU),
            endpoint=os.getenv("JOLLIBEE_GRAPHQL_URL", jollibee.DEFAULT_ENDPOINT),
            before_request=_limiter.wait,
        )
        cart = jollibee.create_voucher_cart(
            session,
            options,
            sku=os.getenv("JOLLIBEE_TEST_PRODUCT_SKU", jollibee.DEFAULT_TEST_SKU),
            endpoint=os.getenv("JOLLIBEE_GRAPHQL_URL", jollibee.DEFAULT_ENDPOINT),
            before_request=_limiter.wait,
        )
        return session, cart
    except Exception:
        session.close()
        raise


def _open_cart(routes: list[str | None], start: int = 0):
    last_status = "SETUP_FAILED"
    last_message = "Không tạo được guest cart"
    available_route = False
    for route_index in range(start, len(routes)):
        proxy = routes[route_index]
        if route_cooldown_remaining(proxy) > 0:
            continue
        available_route = True
        try:
            session, cart = _create_cart_for_route(proxy)
            return route_index, session, cart, "", ""
        except jollibee.JollibeeApiError as exc:
            last_message = str(exc)
            last_status = _error_status(last_message)
            if last_status == "RATE_LIMITED":
                _mark_route_cooldown(proxy)
            if last_status not in FALLBACK_STATUSES:
                break
    if routes and not available_route:
        return (
            len(routes), None, None, "ROUTES_COOLDOWN",
            "Tất cả route đang cooldown 30 phút do rate-limit",
        )
    return len(routes), None, None, last_status, last_message


def _apply_all(
    results: list[CheckResult],
    indexes: list[int],
    routes: list[str | None],
    replace_bad: Callable[[int, CheckResult], str | None] | None = None,
) -> None:
    route_index, session, cart, setup_status, setup_message = _open_cart(routes)
    if session is None or cart is None:
        for index in indexes:
            results[index].status = "ERROR"
            results[index].jollibee_status = setup_status
            results[index].error = setup_message or status_label(setup_status)
        return

    checked_in_cart: list[int] = []
    try:
        for position, index in enumerate(indexes):
            stop_remaining = False
            while True:
                api_result: dict[str, Any] = jollibee.check_voucher(
                    session,
                    results[index].code,
                    cart,
                    endpoint=os.getenv("JOLLIBEE_GRAPHQL_URL", jollibee.DEFAULT_ENDPOINT),
                    before_request=_limiter.wait,
                )
                api_status = str(api_result.get("status") or "ERROR")
                if api_status == "RATE_LIMITED" and route_index < len(routes):
                    _mark_route_cooldown(routes[route_index])
                if api_status in FALLBACK_STATUSES and route_index + 1 < len(routes):
                    session.close()
                    route_index, session, cart, setup_status, setup_message = _open_cart(
                        routes, route_index + 1,
                    )
                    checked_in_cart = []
                    if session is None or cart is None:
                        api_status = setup_status
                        api_result = {"status": setup_status, "message": setup_message}
                    else:
                        continue

                result = results[index]
                result.jollibee_status = api_status
                result.message = str(api_result.get("message") or "")[:300]
                result.proxy_used = (
                    route_index < len(routes) and routes[route_index] is not None
                )
                result.active_in_cart = api_result.get("active_in_cart")
                if api_status == "APPLIED":
                    result.status = "VALID"
                    checked_in_cart.append(index)
                    for checked_index in checked_in_cart:
                        active = results[checked_index].code.casefold() in cart.applied_codes
                        results[checked_index].active_in_cart = active
                        results[checked_index].cart_status = (
                            "ACTIVE" if active else "REPLACED_OR_REMOVED"
                        )
                    break

                result.status = api_status if api_status in BAD_STATUSES else "ERROR"
                if result.status == "ERROR":
                    result.error = result.message or status_label(api_status)
                if result.status in BAD_STATUSES and replace_bad is not None:
                    replacement = replace_bad(index, result)
                    if replacement:
                        code = replacement.strip().upper()
                        results[index] = CheckResult(
                            input_url=code,
                            status="PENDING",
                            code=code,
                        )
                        continue
                if session is None or cart is None:
                    for remaining in indexes[position + 1:]:
                        results[remaining].status = "ERROR"
                        results[remaining].jollibee_status = "NOT_TESTED"
                        results[remaining].error = (
                            "Dừng vì không tạo được cart qua proxy kế tiếp"
                        )
                    stop_remaining = True
                elif api_status in FALLBACK_STATUSES or api_status == "NOT_TESTED":
                    for remaining in indexes[position + 1:]:
                        results[remaining].status = "ERROR"
                        results[remaining].jollibee_status = "NOT_TESTED"
                        results[remaining].error = "Dừng vì API đã giới hạn lượt kiểm tra"
                    stop_remaining = True
                break
            if stop_remaining:
                break
    finally:
        if session is not None:
            session.close()


def check_inputs(values: list[str]) -> list[CheckResult]:
    """Kiểm tra không giới hạn đầu vào, chỉ đổi cart khi phải fallback."""
    urbox_session = urbox.make_session() if any("http" in value.casefold() for value in values) else None
    try:
        results = [_read_input(value.strip(), urbox_session) for value in values]
    finally:
        if urbox_session is not None:
            urbox_session.close()

    pending = [index for index, result in enumerate(results) if result.status == "PENDING"]
    if pending:
        routes: list[str | None] = [None, *load_proxies()]
        with _jollibee_lock:
            _apply_all(results, pending, routes)
    return results


def check_codes_with_replacements(
    values: list[str],
    replace_bad: Callable[[int, CheckResult], str | None],
) -> list[CheckResult]:
    """Check mã theo từng vị trí và đổi mã hỏng ngay trên cart đang dùng."""
    results = [_read_input(value.strip(), None) for value in values]
    pending = [index for index, result in enumerate(results) if result.status == "PENDING"]
    if pending:
        routes: list[str | None] = [None, *load_proxies()]
        with _jollibee_lock:
            _apply_all(results, pending, routes, replace_bad)
    return results


def read_inputs(values: list[str]) -> list[CheckResult]:
    """Chỉ đọc mã từ đầu vào để nhập kho, không tạo cart hay kiểm tra sử dụng."""
    urbox_session = urbox.make_session() if any("http" in value.casefold() for value in values) else None
    try:
        results = [_read_input(value.strip(), urbox_session) for value in values]
    finally:
        if urbox_session is not None:
            urbox_session.close()
    for result in results:
        if result.code:
            result.status = "UNVERIFIED"
            result.jollibee_status = ""
            result.message = ""
    return results


def check_link(input_url: str, session: requests.Session | None = None) -> CheckResult:
    del session
    return check_inputs([input_url])[0]


def check_code(code: str) -> CheckResult:
    return check_inputs([code])[0]


def check_links(urls: list[str]) -> list[CheckResult]:
    return check_inputs(urls)


def probe() -> dict[str, Any]:
    """Kiểm tra khả năng tạo guest cart, có fallback proxy nhưng không áp mã."""
    routes: list[str | None] = [None, *load_proxies()]
    with _jollibee_lock:
        route_index, session, cart, status, message = _open_cart(routes)
    if session is None or cart is None:
        return {"ok": False, "status": status, "message": message, "proxy_count": len(routes) - 1}
    session.close()
    return {
        "ok": True,
        "status": "READY",
        "proxy_used": routes[route_index] is not None,
        "proxy_count": len(routes) - 1,
        "app_version": os.getenv("JOLLIBEE_APP_VERSION", jollibee.DEFAULT_APP_VERSION),
        "sku": os.getenv("JOLLIBEE_TEST_PRODUCT_SKU", jollibee.DEFAULT_TEST_SKU),
    }


def test_routes() -> list[dict[str, Any]]:
    """Tạo guest cart riêng qua từng route để đo trạng thái live và độ trễ."""
    routes: list[str | None] = [None, *load_proxies()]
    results: list[dict[str, Any]] = []
    with _jollibee_lock:
        for index, proxy in enumerate(routes):
            started = time.monotonic()
            session = None
            status = "READY"
            message = ""
            ok = False
            try:
                session, _ = _create_cart_for_route(proxy)
                ok = True
            except jollibee.JollibeeApiError as exc:
                message = str(exc)[:200]
                status = _error_status(message)
                if status == "RATE_LIMITED":
                    _mark_route_cooldown(proxy)
            except Exception as exc:  # noqa: BLE001
                status = "NETWORK_ERROR"
                message = f"{type(exc).__name__}: {exc}"[:200]
            finally:
                if session is not None:
                    session.close()
            results.append({
                "index": index,
                "route": proxy,
                "ok": ok,
                "status": status,
                "message": message,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "cooldown_seconds": route_cooldown_remaining(proxy),
            })
    return results
