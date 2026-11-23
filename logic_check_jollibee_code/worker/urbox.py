"""Trích mã UrBox cho luồng kiểm tra voucher của bot.

QUAN TRỌNG (§7.6): CODE_RE bám vào id="qrCopy", USED_RE bám vào showOverlay(1).
urbox đổi giao diện là cả hai vô dụng và worker sẽ báo parse_failed. Khi cầu dao
ngắt tạm dừng vì parse_failed, sửa hai regex Ở ĐÂY.
"""
from __future__ import annotations

import html
import random
import re
import threading
import time
from typing import Optional

import requests

CODE_RE = re.compile(r'id="qrCopy"[^>]*>\s*([^<]+?)\s*<', re.IGNORECASE)
USED_RE = re.compile(r"showOverlay\s*\(\s*1\b")
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
DATE_RE = re.compile(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class RateLimiter:
    """Giãn mọi request ra >= min_interval giây trên toàn bộ tiến trình."""

    def __init__(self, min_interval: float):
        self._lock = threading.Lock()
        self._next = 0.0
        self._min = max(0.0, min_interval)

    def set_interval(self, min_interval: float) -> None:
        with self._lock:
            self._min = max(0.0, min_interval)

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._min
        if sleep_for > 0:
            time.sleep(sleep_for + random.uniform(0, 0.05))


def _clean(s: str) -> str:
    return WS_RE.sub(" ", TAG_RE.sub(" ", s).replace("&nbsp;", " ")).strip()


def _value_after(html_text: str, label: str, max_chars: int = 160) -> str:
    idx = html_text.find(label)
    if idx < 0:
        return ""
    m = DATE_RE.search(_clean(html_text[idx: idx + max_chars]))
    return m.group(0) if m else ""


def parse_urbox_page(html_text: str) -> Optional[tuple[str, str, str, str]]:
    m = CODE_RE.search(html_text)
    if not m:
        return None
    code = html.unescape(m.group(1).strip())
    status = "USED" if USED_RE.search(html_text) else "VALID"
    valid_until = _value_after(html_text, "Hạn sử dụng")
    effective = _value_after(html_text, "Hiệu lực sử dụng")
    return code, status, valid_until, effective


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi,en;q=0.9",
    })
    return s


def fetch_html(url: str, session: requests.Session,
               connect_timeout: float = 6.0, read_timeout: float = 20.0,
               retries: int = 2) -> str:
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, timeout=(connect_timeout, read_timeout))
            resp.raise_for_status()
            resp.encoding = "utf-8"
            return resp.text
        except (requests.Timeout, requests.ConnectionError) as exc:
            last = exc
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
    raise last  # type: ignore[misc]
