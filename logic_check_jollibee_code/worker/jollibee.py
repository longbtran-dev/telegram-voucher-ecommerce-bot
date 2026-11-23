"""Client GraphQL tối thiểu để kiểm tra nhiều voucher trên cùng guest cart."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import re
from typing import Any

import requests

DEFAULT_ENDPOINT = "https://api.jollibee.com.vn/graphql"
DEFAULT_APP_VERSION = "4.10"
DEFAULT_TEST_SKU = "4000742"

_VERSION_TOKEN_RE = re.compile(r"^\d+(?:\.\d+)+$")
_VERSION_ERROR_RE = re.compile(
    r"(?:cập\s*nhật|phiên\s*bản|version|update)[^\d]{0,50}(\d+(?:\.\d+)+)",
    re.IGNORECASE,
)
_active_app_version: str = DEFAULT_APP_VERSION
_version_listeners: list[Callable[[str], None]] = []


def version_key(version: str) -> tuple[int, ...]:
    cleaned = str(version or "").strip()
    if not _VERSION_TOKEN_RE.match(cleaned):
        return (0,)
    return tuple(int(part) for part in cleaned.split("."))


def get_active_app_version() -> str:
    return _active_app_version


def register_version_listener(listener: Callable[[str], None]) -> None:
    if listener not in _version_listeners:
        _version_listeners.append(listener)


def set_active_app_version(version: str, *, allow_lower: bool = False) -> str:
    global _active_app_version
    cleaned = str(version or "").strip()
    if not _VERSION_TOKEN_RE.match(cleaned):
        return _active_app_version
    if allow_lower or version_key(cleaned) >= version_key(_active_app_version):
        _active_app_version = cleaned
        for listener in list(_version_listeners):
            try:
                listener(cleaned)
            except Exception:
                pass
    return _active_app_version


def next_app_version(version: str) -> str:
    cleaned = str(version or "").strip()
    parts = cleaned.split(".")
    if len(parts) < 2 or not all(part.isdigit() for part in parts):
        return DEFAULT_APP_VERSION
    last = parts[-1]
    bumped = str(int(last) + 1).zfill(len(last))
    return ".".join([*parts[:-1], bumped])


def extract_required_app_version(
    messages: list[str] | str,
    current_version: str | None = None,
) -> str | None:
    items = [messages] if isinstance(messages, str) else list(messages)
    for item in items:
        text = str(item or "")
        match = _VERSION_ERROR_RE.search(text)
        if match:
            return match.group(1)
        lowered = text.casefold()
        if "cập nhật" in lowered and ("phiên bản" in lowered or "ứng dụng" in lowered):
            return next_app_version(current_version or _active_app_version)
    return None


PRODUCT_OPTIONS_QUERY = """
query BundleOptions($sku: String!) {
  products(filter: {sku: {eq: $sku}}) {
    items {
      sku
      name
      ... on BundleProduct {
        items {
          option_id
          title
          required
          options {
            id
            is_default
          }
        }
      }
    }
  }
}
"""

CREATE_EMPTY_CART = """
mutation CreateEmptyCart {
  createEmptyCart
}
"""

ADD_BUNDLE_PRODUCT = """
mutation AddBundleProducts(
  $cart_id: String!
  $cart_items: [BundleProductCartItemInput!]!
) {
  addBundleProductsToCart(
    input: {cart_id: $cart_id, cart_items: $cart_items}
  ) {
    cart {
      items {
        product {
          name
          sku
        }
        quantity
      }
      prices {
        grand_total {
          value
          currency
        }
      }
    }
  }
}
"""

APPLY_VOUCHER = """
mutation ApplyVoucherOrCouponToCart($cart_id: String!, $code: String!) {
  applyVoucherOrCouponToCart(input: {cart_id: $cart_id, code: $code}) {
    applied_method
    cart {
      applied_vouchers {
        list {
          code
          discount_amount
        }
        total_discount_amount
      }
      applied_coupons {
        code
      }
      prices {
        grand_total {
          value
          currency
        }
      }
      bonus_point
    }
  }
}
"""


class JollibeeApiError(RuntimeError):
    pass


@dataclass
class VoucherCart:
    cart_id: str
    item_count: int
    current_total: Any = None
    currency: str | None = None
    applied_codes: set[str] = field(default_factory=set)


def make_session(
    app_version: str | None = None,
    store: str = "default",
    lang: str = "vi",
) -> requests.Session:
    resolved_version = (app_version or "").strip() or get_active_app_version()
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Language": "vi-VN,vi;q=0.9",
            "app-version": resolved_version,
            "store": store,
            "lang": lang,
            "User-Agent": "Jollibee%20Vietnam/1 CFNetwork/1498.700.2 Darwin/23.6.0",
        }
    )
    return session


def redact(text: str, secrets: list[str]) -> str:
    result = str(text)
    for secret in secrets:
        if secret:
            result = result.replace(secret, "<redacted>")
    return result


def graph_errors(body: dict[str, Any]) -> list[str]:
    errors = body.get("errors")
    if not isinstance(errors, list):
        return []
    return [
        error["message"]
        for error in errors
        if isinstance(error, dict) and isinstance(error.get("message"), str)
    ]


def _post_graphql_once(
    session: requests.Session,
    endpoint: str,
    query: str,
    variables: dict[str, Any] | None = None,
    timeout: float = 20.0,
    before_request: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if before_request is not None:
        before_request()
    try:
        response = session.post(
            endpoint,
            json={"query": query, "variables": variables or {}},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise JollibeeApiError(f"network:{type(exc).__name__}") from exc

    if response.status_code == 429:
        raise JollibeeApiError("http_429")
    if response.status_code in (401, 403):
        raise JollibeeApiError(f"http_{response.status_code}")
    if response.status_code >= 400:
        raise JollibeeApiError(f"http_{response.status_code}")

    try:
        body = response.json()
    except ValueError as exc:
        raise JollibeeApiError("non_json_response") from exc
    if not isinstance(body, dict):
        raise JollibeeApiError("invalid_graphql_response")
    return body


def post_graphql(
    session: requests.Session,
    endpoint: str,
    query: str,
    variables: dict[str, Any] | None = None,
    timeout: float = 20.0,
    before_request: Callable[[], None] | None = None,
) -> dict[str, Any]:
    for _ in range(3):
        body = _post_graphql_once(
            session, endpoint, query, variables, timeout, before_request,
        )
        errors = graph_errors(body)
        if not errors:
            return body
        headers = getattr(session, "headers", None)
        current_ver = (
            str(headers.get("app-version") or _active_app_version)
            if hasattr(headers, "get")
            else _active_app_version
        )
        required_ver = extract_required_app_version(errors, current_version=current_ver)
        if not required_ver or required_ver == current_ver:
            return body
        set_active_app_version(required_ver)
        if hasattr(headers, "__setitem__"):
            headers["app-version"] = required_ver
        else:
            return body
    return body


def build_default_bundle_options(body: dict[str, Any], sku: str) -> list[dict[str, Any]]:
    products = (body.get("data") or {}).get("products") or {}
    items = products.get("items") or []
    product = next(
        (
            item
            for item in items
            if isinstance(item, dict) and str(item.get("sku")) == sku
        ),
        None,
    )
    if not isinstance(product, dict):
        raise JollibeeApiError("test_product_not_found")

    option_rows = product.get("items")
    if not isinstance(option_rows, list):
        raise JollibeeApiError("test_product_not_bundle")

    bundle_options: list[dict[str, Any]] = []
    for option in option_rows:
        if not isinstance(option, dict) or not option.get("required"):
            continue
        values = option.get("options") or []
        selected = next(
            (
                value
                for value in values
                if isinstance(value, dict) and value.get("is_default")
            ),
            None,
        )
        if not isinstance(selected, dict):
            title = str(option.get("title") or "unknown")
            raise JollibeeApiError(f"required_option_without_default:{title}")

        option_id = int(option["option_id"])
        selection_id = int(selected["id"])
        bundle_options.append(
            {
                "id": option_id,
                "value": [str(selection_id)],
                "quantity": [
                    {
                        "option_value": selection_id,
                        "selection_id": selection_id,
                        "qty": 1.0,
                    }
                ],
            }
        )

    if not bundle_options:
        raise JollibeeApiError("no_required_bundle_options")
    return bundle_options


def fetch_default_bundle_options(
    session: requests.Session,
    sku: str = DEFAULT_TEST_SKU,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: float = 20.0,
    before_request: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    body = post_graphql(
        session,
        endpoint,
        PRODUCT_OPTIONS_QUERY,
        {"sku": sku},
        timeout,
        before_request,
    )
    errors = graph_errors(body)
    if errors:
        raise JollibeeApiError("bundle_options:" + "; ".join(errors))
    return build_default_bundle_options(body, sku)


def create_cart(
    session: requests.Session,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: float = 20.0,
    before_request: Callable[[], None] | None = None,
) -> str:
    body = post_graphql(
        session,
        endpoint,
        CREATE_EMPTY_CART,
        timeout=timeout,
        before_request=before_request,
    )
    cart_id = (body.get("data") or {}).get("createEmptyCart")
    if isinstance(cart_id, str) and cart_id:
        return cart_id

    errors = graph_errors(body)
    raise JollibeeApiError("create_cart:" + ("; ".join(errors) or "missing_cart_id"))


def add_bundle_product(
    session: requests.Session,
    cart_id: str,
    bundle_options: list[dict[str, Any]],
    sku: str = DEFAULT_TEST_SKU,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: float = 20.0,
    before_request: Callable[[], None] | None = None,
) -> dict[str, Any]:
    body = post_graphql(
        session,
        endpoint,
        ADD_BUNDLE_PRODUCT,
        {
            "cart_id": cart_id,
            "cart_items": [
                {
                    "data": {"quantity": 1.0, "sku": sku},
                    "bundle_options": bundle_options,
                }
            ],
        },
        timeout,
        before_request,
    )
    result = (body.get("data") or {}).get("addBundleProductsToCart")
    if not isinstance(result, dict):
        message = "; ".join(graph_errors(body)) or "missing_add_result"
        raise JollibeeApiError("add_item:" + redact(message, [cart_id]))
    return result


def create_voucher_cart(
    session: requests.Session,
    bundle_options: list[dict[str, Any]],
    sku: str = DEFAULT_TEST_SKU,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: float = 20.0,
    before_request: Callable[[], None] | None = None,
) -> VoucherCart:
    cart_id = create_cart(session, endpoint, timeout, before_request)
    added = add_bundle_product(
        session,
        cart_id,
        bundle_options,
        sku,
        endpoint,
        timeout,
        before_request,
    )
    cart = added.get("cart") if isinstance(added.get("cart"), dict) else {}
    total = ((cart.get("prices") or {}).get("grand_total") or {})
    return VoucherCart(
        cart_id=cart_id,
        item_count=len(cart.get("items") or []),
        current_total=total.get("value"),
        currency=total.get("currency"),
    )


def classify_rejection(message: str) -> str:
    normalized = message.casefold()
    if "quá nhiều lần" in normalized or "thử lại sau" in normalized:
        return "RATE_LIMITED"
    if "đã sử dụng" in normalized:
        return "USED"
    if "hết hạn" in normalized:
        return "EXPIRED"
    if "chưa có hiệu lực" in normalized or "chưa đến" in normalized:
        return "NOT_ACTIVE"
    if "không hợp lệ" in normalized or "không tồn tại" in normalized:
        return "INVALID"
    return "REJECTED"


def classify_api_error(message: str) -> str:
    if message == "http_429":
        return "RATE_LIMITED"
    if message in ("http_401", "http_403"):
        return "AUTH_OR_BLOCKED"
    if message.startswith("network:"):
        return "NETWORK_ERROR"
    if message.startswith("create_cart:"):
        return "CREATE_CART_FAILED"
    if message.startswith("add_item:"):
        return "ADD_ITEM_FAILED"
    return "API_ERROR"


def cart_applied_codes(cart: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    applied_vouchers = cart.get("applied_vouchers")
    vouchers = applied_vouchers.get("list", []) if isinstance(applied_vouchers, dict) else []
    for voucher in vouchers:
        if isinstance(voucher, dict) and isinstance(voucher.get("code"), str):
            codes.add(voucher["code"].casefold())

    applied_coupons = cart.get("applied_coupons")
    if isinstance(applied_coupons, dict):
        coupon_code = applied_coupons.get("code")
        if isinstance(coupon_code, str):
            codes.add(coupon_code.casefold())
    elif isinstance(applied_coupons, list):
        for coupon in applied_coupons:
            if isinstance(coupon, dict) and isinstance(coupon.get("code"), str):
                codes.add(coupon["code"].casefold())
    return codes


def check_voucher(
    session: requests.Session,
    code: str,
    voucher_cart: VoucherCart,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: float = 20.0,
    before_request: Callable[[], None] | None = None,
) -> dict[str, Any]:
    display_code = code
    before_total = voucher_cart.current_total
    try:
        body = post_graphql(
            session,
            endpoint,
            APPLY_VOUCHER,
            {"cart_id": voucher_cart.cart_id, "code": code},
            timeout,
            before_request,
        )
        errors = graph_errors(body)
        result = (body.get("data") or {}).get("applyVoucherOrCouponToCart")
        if not isinstance(result, dict):
            message = redact(
                "; ".join(errors) or "missing_apply_result",
                [voucher_cart.cart_id],
            )
            return {
                "code": display_code,
                "status": classify_rejection(message),
                "message": message,
                "cart_item_count": voucher_cart.item_count,
                "before_total": before_total,
            }

        cart = result.get("cart") if isinstance(result.get("cart"), dict) else {}
        applied_vouchers = cart.get("applied_vouchers")
        vouchers = applied_vouchers.get("list", []) if isinstance(applied_vouchers, dict) else []
        expected = code.casefold()
        matched = next(
            (
                voucher
                for voucher in vouchers
                if isinstance(voucher, dict)
                and isinstance(voucher.get("code"), str)
                and voucher["code"].casefold() == expected
            ),
            None,
        )
        voucher_cart.applied_codes = cart_applied_codes(cart)
        total = ((cart.get("prices") or {}).get("grand_total") or {})
        if total.get("value") is not None:
            voucher_cart.current_total = total.get("value")
        if isinstance(total.get("currency"), str):
            voucher_cart.currency = total.get("currency")

        if expected not in voucher_cart.applied_codes:
            message = (
                redact("; ".join(errors), [voucher_cart.cart_id])
                if errors
                else "API không báo lỗi nhưng mã không xuất hiện trong giỏ"
            )
            return {
                "code": display_code,
                "status": classify_rejection(message) if errors else "NOT_APPLIED",
                "message": message,
                "cart_item_count": voucher_cart.item_count,
                "before_total": before_total,
                "final_total": voucher_cart.current_total,
                "currency": voucher_cart.currency,
                "active_in_cart": False,
            }

        output: dict[str, Any] = {
            "code": display_code,
            "status": "APPLIED",
            "method": result.get("applied_method"),
            "cart_item_count": voucher_cart.item_count,
            "before_total": before_total,
            "final_total": voucher_cart.current_total,
            "currency": voucher_cart.currency,
            "bonus_point": cart.get("bonus_point"),
            "active_in_cart": True,
            "cart_applied_voucher_count": len(voucher_cart.applied_codes),
        }
        if isinstance(matched, dict):
            output["discount_amount"] = matched.get("discount_amount")
            output["total_discount_amount"] = applied_vouchers.get("total_discount_amount")
        return output
    except JollibeeApiError as exc:
        message = redact(str(exc), [voucher_cart.cart_id])
        status = classify_api_error(message)
        return {"code": display_code, "status": status, "message": message}
