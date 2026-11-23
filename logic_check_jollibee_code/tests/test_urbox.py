from pathlib import Path
import urbox

FX = Path(__file__).parent / "fixtures"


def read(name):
    return (FX / name).read_text(encoding="utf-8")


def test_parse_valid():
    code, status, valid_until, effective = urbox.parse_urbox_page(read("valid.html"))
    assert code == "UB7K2M9XQT4D"
    assert status == "VALID"
    assert valid_until == "31/12/2026"
    assert effective == "01/01/2026"


def test_parse_used():
    _, status, _, _ = urbox.parse_urbox_page(read("used.html"))
    assert status == "USED"


def test_parse_broken_returns_none():
    assert urbox.parse_urbox_page(read("broken.html")) is None


def test_rate_limiter_spaces_requests():
    import time
    rl = urbox.RateLimiter(0.05)
    t0 = time.monotonic()
    rl.wait(); rl.wait(); rl.wait()
    assert time.monotonic() - t0 >= 0.10  # 3 lần cách nhau >= 2*0.05
