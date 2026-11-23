import jollibee
import pytest


def test_make_session_uses_current_app_version():
    session = jollibee.make_session()
    try:
        assert jollibee.DEFAULT_APP_VERSION == "4.10"
        assert session.headers["app-version"] == "4.10"
    finally:
        session.close()


def test_post_graphql_auto_updates_app_version_and_retries():
    session = jollibee.make_session(app_version="4.09")
    seen_versions = []

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def fake_post(url, json=None, timeout=None):
        ver = session.headers.get("app-version")
        seen_versions.append(ver)
        if ver != "4.10":
            return FakeResponse(
                200,
                {
                    "data": {"createEmptyCart": None},
                    "errors": [
                        {"message": "Vui lòng cập nhật lên phiên bản mới nhất 4.10"}
                    ],
                },
            )
        return FakeResponse(200, {"data": {"createEmptyCart": "cart-after-upgrade"}})

    session.post = fake_post
    try:
        cart_id = jollibee.create_cart(session)
        assert cart_id == "cart-after-upgrade"
        assert seen_versions == ["4.09", "4.10"]
        assert session.headers["app-version"] == "4.10"
        assert jollibee.get_active_app_version() == "4.10"
    finally:
        session.close()


def test_create_cart_preserves_create_empty_cart_error(monkeypatch):
    calls = []

    def post_graphql(session, endpoint, query, *args, **kwargs):
        calls.append(query)
        return {
            "data": {"createEmptyCart": None},
            "errors": [{"message": "Vui lòng cập nhật lên phiên bản mới nhất 4.07"}],
        }

    monkeypatch.setattr(jollibee, "post_graphql", post_graphql)

    with pytest.raises(
        jollibee.JollibeeApiError,
        match="Vui lòng cập nhật lên phiên bản mới nhất 4.07",
    ):
        jollibee.create_cart(object())

    assert calls == [jollibee.CREATE_EMPTY_CART]


def test_classify_rejection_messages():
    assert (
        jollibee.classify_rejection(
            "Bạn đã gửi quá nhiều lần. Vui lòng thử lại sau."
        )
        == "RATE_LIMITED"
    )
    assert jollibee.classify_rejection("Phiếu giảm giá đã sử dụng.") == "USED"
    assert jollibee.classify_rejection("Phiếu giảm giá hết hạn.") == "EXPIRED"
    assert jollibee.classify_rejection("Mã không hợp lệ") == "INVALID"
    assert jollibee.classify_rejection("Không áp dụng cho giỏ hàng") == "REJECTED"


def test_build_default_bundle_options_uses_required_defaults():
    body = {
        "data": {
            "products": {
                "items": [
                    {
                        "sku": "4000742",
                        "items": [
                            {
                                "option_id": 256,
                                "title": "Chọn Gà 1",
                                "required": True,
                                "options": [
                                    {"id": 5649, "is_default": True},
                                    {"id": 10976, "is_default": False},
                                ],
                            },
                            {
                                "option_id": 1872,
                                "title": "Thêm món",
                                "required": False,
                                "options": [{"id": 10271, "is_default": False}],
                            },
                        ],
                    }
                ]
            }
        }
    }

    assert jollibee.build_default_bundle_options(body, "4000742") == [
        {
            "id": 256,
            "value": ["5649"],
            "quantity": [
                {
                    "option_value": 5649,
                    "selection_id": 5649,
                    "qty": 1.0,
                }
            ],
        }
    ]


def test_create_voucher_cart_initializes_shared_cart(monkeypatch):
    create_calls = []
    add_calls = []

    def create_cart(*args, **kwargs):
        create_calls.append((args, kwargs))
        return "shared-cart"

    def add_bundle_product(session, cart_id, bundle_options, *args, **kwargs):
        add_calls.append((cart_id, bundle_options))
        return {
            "cart": {
                "items": [{"quantity": 1}],
                "prices": {
                    "grand_total": {"value": 125000, "currency": "VND"}
                },
            }
        }

    monkeypatch.setattr(jollibee, "create_cart", create_cart)
    monkeypatch.setattr(jollibee, "add_bundle_product", add_bundle_product)

    cart = jollibee.create_voucher_cart(object(), [{"id": 1}])

    assert len(create_calls) == 1
    assert add_calls == [("shared-cart", [{"id": 1}])]
    assert cart.cart_id == "shared-cart"
    assert cart.item_count == 1
    assert cart.current_total == 125000
    assert cart.currency == "VND"


def test_check_voucher_reuses_cart_and_updates_applied_codes(monkeypatch):
    responses = iter(
        [
            {
                "data": {
                    "applyVoucherOrCouponToCart": {
                        "applied_method": "voucher",
                        "cart": {
                            "applied_vouchers": {
                                "list": [
                                    {"code": "FIRST", "discount_amount": 10000}
                                ],
                                "total_discount_amount": 10000,
                            },
                            "applied_coupons": None,
                            "prices": {
                                "grand_total": {"value": 115000, "currency": "VND"}
                            },
                            "bonus_point": 0,
                        },
                    }
                }
            },
            {
                "data": {
                    "applyVoucherOrCouponToCart": {
                        "applied_method": "voucher",
                        "cart": {
                            "applied_vouchers": {
                                "list": [
                                    {"code": "SECOND", "discount_amount": 20000}
                                ],
                                "total_discount_amount": 20000,
                            },
                            "applied_coupons": None,
                            "prices": {
                                "grand_total": {"value": 105000, "currency": "VND"}
                            },
                            "bonus_point": 0,
                        },
                    }
                }
            },
        ]
    )
    sent_cart_ids = []

    def post_graphql(session, endpoint, query, variables, *args, **kwargs):
        sent_cart_ids.append(variables["cart_id"])
        return next(responses)

    monkeypatch.setattr(jollibee, "post_graphql", post_graphql)
    cart = jollibee.VoucherCart(
        cart_id="shared-cart",
        item_count=1,
        current_total=125000,
        currency="VND",
    )

    first = jollibee.check_voucher(object(), "FIRST", cart)
    second = jollibee.check_voucher(object(), "SECOND", cart)

    assert sent_cart_ids == ["shared-cart", "shared-cart"]
    assert first["status"] == "APPLIED"
    assert first["code"] == "FIRST"
    assert first["before_total"] == 125000
    assert first["final_total"] == 115000
    assert second["status"] == "APPLIED"
    assert second["code"] == "SECOND"
    assert second["before_total"] == 115000
    assert second["final_total"] == 105000
    assert cart.applied_codes == {"second"}
