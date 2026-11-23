"""Tạo file HTML báo cáo lịch sử bán hàng — bảng cao cấp, responsive (điện thoại & PC).

Thiết kế theo hướng "high-end": font premium, kiến trúc double-bezel, bo squircle,
đổ bóng mềm có màu, chuyển động cubic-bezier. Bảng trên desktop, thẻ trên mobile.
"""

from __future__ import annotations

import base64
import datetime
import functools
import html
from pathlib import Path

from . import config, utils


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def render_history_html(
    orders: list[dict], total_count: int, total_revenue: int, shop_title: str,
    sold_by_denom: dict[int, int] | None = None,
) -> str:
    """Dựng trang HTML tự chứa dạng bảng từ danh sách đơn đã bán.

    Mỗi order dict: order_code, time, date, buyer, quantity, denom,
    total_amount, vouchers (list[(code, link)]).
    sold_by_denom: tổng số MÃ đã bán theo mệnh giá (hiện thêm ô thống kê).
    """
    sold_by_denom = sold_by_denom or {}
    now = datetime.datetime.now(utils.VN_TZ).strftime("%H:%M · %d/%m/%Y")

    body_rows = []
    for idx, o in enumerate(orders):
        vouchers_html = []
        for code, link in o["vouchers"]:
            code_e = _esc(code)
            link_html = ""
            if link:
                link_e = _esc(link)
                link_html = (
                    f'<a class="v-link" href="{link_e}" target="_blank" '
                    f'rel="noopener">{link_e}</a>'
                )
            vouchers_html.append(
                f'<div class="v"><div class="v-top">'
                f'<code class="v-code" onclick="cp(this)" title="Bấm để sao chép">{code_e}</code>'
                f'<button class="v-copy" onclick="cp(this.previousElementSibling)" '
                f'aria-label="Sao chép mã"><span>Copy</span></button>'
                f'</div>{link_html}</div>'
            )
        detail = "".join(vouchers_html)

        body_rows.append(
            f'''<tr style="--i:{idx}">
  <td data-label="Mã đơn"><span class="oc">{_esc(o["order_code"])}</span></td>
  <td data-label="Giờ" class="c-time">{_esc(o["time"])}</td>
  <td data-label="Ngày" class="c-date">{_esc(o["date"])}</td>
  <td data-label="Số tiền"><span class="amt">{_esc(utils.format_vnd(o["total_amount"]))}</span></td>
  <td data-label="SL"><span class="qty">{o["quantity"]}</span></td>
  <td data-label="Người mua"><span class="buyer">{_esc(o["buyer"])}</span></td>
  <td data-label="Chi tiết đơn hàng" class="detail">{detail}</td>
</tr>'''
        )

    rows_html = "\n".join(body_rows) if body_rows else (
        '<tr><td colspan="7" class="empty">Chưa có đơn hàng nào.</td></tr>'
    )

    # Ô thống kê: đơn đã bán, tổng doanh thu + số mã đã bán theo từng mệnh giá.
    icon_cart = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M6 7h12l-1 12.5a1.5 1.5 0 0 1-1.5 1.4H8.5A1.5 1.5 0 0 1 7 19.5L6 7Z"/>'
        '<path d="M9 7V6a3 3 0 0 1 6 0v1"/></svg>'
    )
    icon_coin = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/>'
        '<path d="M9.5 10h5M9.5 13h5M12 7.5v9"/></svg>'
    )
    icon_ticket = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M4 9a2 2 0 0 0 0 6v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3a2 2 0 0 0 0-6V6'
        'a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1Z"/><path d="M13 5v2M13 11v2M13 17v2"/></svg>'
    )
    tiles = [
        f'<div class="stat"><div class="ico">{icon_cart}</div>'
        f'<div><b>{total_count}</b><span>Đơn đã bán</span></div></div>',
        f'<div class="stat"><div class="ico">{icon_coin}</div>'
        f'<div><b>{_esc(utils.format_vnd(total_revenue))}</b>'
        f'<span>Tổng doanh thu</span></div></div>',
    ]
    for denom in config.DENOMINATIONS:
        qty = sold_by_denom.get(denom, 0)
        tiles.append(
            f'<div class="stat"><div class="ico">{icon_ticket}</div>'
            f'<div><b>{qty}</b>'
            f'<span>Mã {_esc(utils.format_k(denom))} đã bán</span></div></div>'
        )
    tiles_html = "\n        ".join(tiles)

    return f'''<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Lịch sử bán hàng · {_esc(shop_title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --red:#e4002b; --red-2:#ff2d55; --red-dark:#b30022; --yellow:#ffc72c;
    --ink:#15171a; --muted:#6a7079; --hair:rgba(20,28,45,.09);
    --green:#0f8a4c; --bg:#eceef1; --card:#ffffff;
    --shell:#f4f5f7; --ease:cubic-bezier(.32,.72,0,1);
    --sh-soft:0 4px 24px rgba(22,28,45,.07), 0 1px 2px rgba(22,28,45,.04);
    --sh-lift:0 12px 34px rgba(22,28,45,.12);
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{
    font-family:"Plus Jakarta Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    color:var(--ink); line-height:1.5; -webkit-text-size-adjust:100%;
    background:
      radial-gradient(1200px 600px at 100% -10%, rgba(255,199,44,.14), transparent 60%),
      radial-gradient(1000px 500px at -10% 0%, rgba(228,0,43,.08), transparent 55%),
      var(--bg);
    padding:22px 16px 40px;
  }}
  .wrap {{ max-width:1200px; margin:0 auto; }}
  code, .mono {{ font-family:"JetBrains Mono",ui-monospace,Menlo,Consolas,monospace; }}

  /* ---------- Header (double-bezel) ---------- */
  .hero-shell {{
    background:var(--shell); border:1px solid var(--hair); border-radius:30px;
    padding:7px; box-shadow:var(--sh-soft); margin-bottom:22px;
    opacity:0; transform:translateY(14px); animation:rise .8s var(--ease) forwards;
  }}
  .hero {{
    position:relative; overflow:hidden; border-radius:24px; padding:26px 28px;
    color:#fff;
    background:linear-gradient(135deg,var(--red) 0%,var(--red-2) 100%);
    box-shadow:inset 0 1px 0 rgba(255,255,255,.22);
  }}
  .hero::after {{
    content:""; position:absolute; right:-60px; top:-70px; width:230px; height:230px;
    background:radial-gradient(circle,var(--yellow),transparent 68%); opacity:.4;
  }}
  .brand {{ font-size:12px; font-weight:700; letter-spacing:.22em; text-transform:uppercase;
            opacity:.9; }}
  .hero h1 {{ font-size:27px; font-weight:800; letter-spacing:-.015em; margin:4px 0 4px; }}
  .gen {{ font-size:12px; font-weight:500; opacity:.82; }}
  .stats {{ position:relative; z-index:1; display:grid; grid-template-columns:1fr 1fr;
            gap:12px; margin-top:22px; }}
  .stat {{ display:flex; align-items:center; gap:14px; padding:15px 16px; border-radius:18px;
           background:rgba(255,255,255,.15); border:1px solid rgba(255,255,255,.26);
           box-shadow:inset 0 1px 0 rgba(255,255,255,.3); }}
  .stat .ico {{ width:46px; height:46px; flex-shrink:0; border-radius:14px; background:#fff;
                color:var(--red); display:flex; align-items:center; justify-content:center;
                box-shadow:0 5px 14px rgba(0,0,0,.14); }}
  .stat .ico svg {{ width:24px; height:24px; }}
  .stat b {{ font-size:27px; font-weight:800; letter-spacing:-.02em; display:block;
             line-height:1.06; }}
  .stat span {{ font-size:12px; font-weight:600; opacity:.92; }}
  @media (max-width:520px) {{ .stats {{ grid-template-columns:1fr; }} }}

  /* ---------- Table container (double-bezel) ---------- */
  .tbl-shell {{
    background:var(--shell); border:1px solid var(--hair); border-radius:26px; padding:7px;
    box-shadow:var(--sh-soft);
    opacity:0; transform:translateY(14px); animation:rise .8s var(--ease) .12s forwards;
  }}
  .tbl-core {{ background:var(--card); border-radius:20px; overflow:hidden; }}
  table {{ width:100%; border-collapse:collapse; }}
  thead th {{
    background:#181b20; color:#eef0f3; text-align:left; font-size:11px; font-weight:700;
    letter-spacing:.09em; text-transform:uppercase; padding:15px 16px; white-space:nowrap;
  }}
  tbody td {{ padding:16px; border-top:1px solid var(--hair); vertical-align:top; font-size:14px; }}
  tbody tr {{ transition:background .3s var(--ease); }}
  tbody tr:hover {{ background:linear-gradient(0deg, rgba(255,199,44,.10), rgba(255,199,44,.10)); }}

  .oc {{ font-family:"JetBrains Mono",monospace; font-weight:700; font-size:12.5px;
         background:linear-gradient(180deg,#fff6dc,#ffedb8); color:#7a5600;
         padding:4px 10px; border-radius:8px; white-space:nowrap;
         box-shadow:inset 0 0 0 1px rgba(122,86,0,.14); }}
  .c-time {{ font-weight:700; }}
  .c-date {{ color:var(--muted); white-space:nowrap; }}
  .amt {{ color:var(--green); font-weight:800; font-size:15px; white-space:nowrap;
          letter-spacing:-.01em; }}
  .qty {{ display:inline-flex; align-items:center; justify-content:center; min-width:28px;
          height:28px; padding:0 8px; font-weight:700; font-size:13px; color:#fff;
          background:linear-gradient(135deg,var(--red),var(--red-2)); border-radius:999px;
          box-shadow:0 3px 10px rgba(228,0,43,.28), inset 0 1px 0 rgba(255,255,255,.3); }}
  .buyer {{ font-weight:600; }}

  .detail {{ min-width:280px; }}
  .v {{ background:var(--shell); border:1px solid var(--hair); border-left:3px solid var(--yellow);
        border-radius:12px; padding:10px 12px; margin-bottom:9px; }}
  .v:last-child {{ margin-bottom:0; }}
  .v-top {{ display:flex; align-items:center; gap:9px; }}
  .v-code {{ flex:1; font-weight:700; font-size:15px; color:var(--red); cursor:pointer;
             user-select:all; word-break:break-all; letter-spacing:.3px; }}
  .v-code.copied::after {{ content:" ✓"; color:var(--green); font-weight:700; }}
  .v-copy {{ border:none; cursor:pointer; background:linear-gradient(135deg,var(--red),var(--red-2));
             color:#fff; font-family:inherit; font-size:11px; font-weight:700; padding:6px 13px;
             border-radius:999px; white-space:nowrap;
             box-shadow:0 4px 12px rgba(228,0,43,.3), inset 0 1px 0 rgba(255,255,255,.28);
             transition:transform .25s var(--ease), box-shadow .25s var(--ease); }}
  .v-copy:hover {{ transform:translateY(-1px); box-shadow:0 7px 16px rgba(228,0,43,.36); }}
  .v-copy:active {{ transform:scale(.95); }}
  .v-link {{ display:block; margin-top:7px; font-size:11.5px; color:#1668d6; text-decoration:none;
             word-break:break-all; line-height:1.45; }}
  .v-link:hover {{ text-decoration:underline; }}
  .empty {{ text-align:center; color:var(--muted); padding:48px; }}

  footer {{ text-align:center; color:#969aa2; font-size:12px; font-weight:500; margin:20px 0 4px; }}

  @keyframes rise {{ to {{ opacity:1; transform:translateY(0); }} }}

  /* ---------- Mobile: bảng -> thẻ ---------- */
  @media (max-width:840px) {{
    body {{ padding:14px 12px 32px; }}
    .tbl-shell {{ background:transparent; border:none; box-shadow:none; padding:0; }}
    .tbl-core {{ background:transparent; border-radius:0; overflow:visible; }}
    thead {{ display:none; }}
    table, tbody, tr, td {{ display:block; width:100%; }}
    tbody tr {{
      background:var(--card); border:1px solid var(--hair); border-radius:20px;
      margin-bottom:14px; padding:8px 6px; box-shadow:var(--sh-soft);
      opacity:0; transform:translateY(12px);
      animation:rise .6s var(--ease) forwards;
      animation-delay:calc(min(var(--i),12) * 45ms + .1s);
    }}
    tbody td {{ border:none; display:flex; justify-content:space-between; align-items:center;
                gap:14px; padding:10px 16px; }}
    tbody td::before {{ content:attr(data-label); font-size:11.5px; font-weight:700;
                        color:var(--muted); text-transform:uppercase; letter-spacing:.04em;
                        flex-shrink:0; }}
    td.detail {{ display:block; }}
    td.detail::before {{ display:block; margin:2px 2px 10px; }}
    .oc {{ font-size:13.5px; }}
    .amt {{ font-size:16px; }}
  }}

  @media (prefers-reduced-motion: reduce) {{
    .hero-shell, .tbl-shell, tbody tr {{ animation:none; opacity:1; transform:none; }}
    * {{ transition:none !important; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <div class="hero-shell">
    <div class="hero">
      <div class="brand">{_esc(shop_title)}</div>
      <h1>Lịch sử bán hàng</h1>
      <div class="gen">Xuất lúc {now} · {len(orders)} đơn gần nhất</div>
      <div class="stats">
        {tiles_html}
      </div>
    </div>
  </div>

  <div class="tbl-shell">
    <div class="tbl-core">
      <table>
        <thead>
          <tr>
            <th>Mã đơn</th><th>Giờ</th><th>Ngày</th><th>Số tiền</th>
            <th>SL</th><th>Người mua</th><th>Chi tiết đơn hàng</th>
          </tr>
        </thead>
        <tbody>
{rows_html}
        </tbody>
      </table>
    </div>
  </div>

  <footer>Bấm vào mã hoặc nút Copy để sao chép · Bấm vào link để mở trang voucher</footer>
</div>
<script>
function cp(el){{
  const t = el.textContent.replace(/\\s*✓$/, '');
  if (navigator.clipboard) navigator.clipboard.writeText(t);
  el.classList.add('copied');
  setTimeout(()=>el.classList.remove('copied'), 1200);
}}
</script>
</body>
</html>'''


def render_customer_history_html(
    orders: list[dict], total_count: int, total_spent: int, shop_title: str
) -> str:
    """Trang HTML lịch sử mua hàng dành cho KHÁCH (gọn, thân thiện, dạng thẻ).

    Mỗi order dict: order_code, time, date, items_label, total_amount,
    vouchers (list[(code, link)]).
    """
    now = datetime.datetime.now(utils.VN_TZ).strftime("%H:%M · %d/%m/%Y")

    cards = []
    for idx, o in enumerate(orders):
        vouchers_html = []
        for code, _ in o["vouchers"]:
            normalized_code = code.strip().upper()
            code_e = _esc(normalized_code)
            barcode_svg = _render_code128b_svg(normalized_code)
            vouchers_html.append(
                f'<div class="v"><div class="v-top">'
                f'<code class="v-code" onclick="cp(this)" title="Bấm để sao chép">{code_e}</code>'
                f'<button class="v-qr" type="button" onclick="tb(this)" '
                f'aria-label="Hiện mã vạch"><span>Barcode</span></button>'
                f'<button class="v-copy" type="button" onclick="cp(this.parentElement.querySelector(\'.v-code\'))" '
                f'aria-label="Sao chép mã"><span>Copy</span></button>'
                f'</div>'
                f'<div class="v-barcode" hidden>{barcode_svg}</div>'
                f'</div>'
            )
        cards.append(
            f'''<div class="card" style="--i:{idx}">
  <div class="card-head">
    <span class="oc">{_esc(o["order_code"])}</span>
    <span class="when">{_esc(o["time"])} · {_esc(o["date"])}</span>
  </div>
  <div class="card-mid">
    <span class="items">🎟 {_esc(o["items_label"])}</span>
    <span class="amt">{_esc(utils.format_vnd(o["total_amount"]))}</span>
  </div>
  <div class="card-vouchers">{"".join(vouchers_html)}</div>
</div>'''
        )
    cards_html = "\n".join(cards) if cards else (
        '<div class="empty">Bạn chưa có đơn hàng nào.</div>'
    )

    return f'''<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Lịch sử mua hàng · {_esc(shop_title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --red:#e4002b; --red-2:#ff2d55; --yellow:#ffc72c;
    --ink:#15171a; --muted:#6a7079; --hair:rgba(20,28,45,.09);
    --green:#0f8a4c; --bg:#eceef1; --card:#ffffff; --shell:#f4f5f7;
    --ease:cubic-bezier(.32,.72,0,1);
    --sh-soft:0 4px 24px rgba(22,28,45,.07), 0 1px 2px rgba(22,28,45,.04);
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{
    font-family:"Plus Jakarta Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    color:var(--ink); line-height:1.5; -webkit-text-size-adjust:100%;
    background:
      radial-gradient(1200px 600px at 100% -10%, rgba(255,199,44,.14), transparent 60%),
      radial-gradient(1000px 500px at -10% 0%, rgba(228,0,43,.08), transparent 55%),
      var(--bg);
    padding:22px 16px 40px;
  }}
  .wrap {{ max-width:680px; margin:0 auto; }}

  .hero-shell {{
    background:var(--shell); border:1px solid var(--hair); border-radius:30px;
    padding:7px; box-shadow:var(--sh-soft); margin-bottom:22px;
    opacity:0; transform:translateY(14px); animation:rise .8s var(--ease) forwards;
  }}
  .hero {{
    position:relative; overflow:hidden; border-radius:24px; padding:26px 28px; color:#fff;
    background:linear-gradient(135deg,var(--red) 0%,var(--red-2) 100%);
    box-shadow:inset 0 1px 0 rgba(255,255,255,.22);
  }}
  .hero::after {{
    content:""; position:absolute; right:-60px; top:-70px; width:230px; height:230px;
    background:radial-gradient(circle,var(--yellow),transparent 68%); opacity:.4;
  }}
  .brand {{ font-size:12px; font-weight:700; letter-spacing:.22em; text-transform:uppercase; opacity:.9; }}
  .hero h1 {{ font-size:26px; font-weight:800; letter-spacing:-.015em; margin:4px 0 4px; }}
  .gen {{ font-size:12px; font-weight:500; opacity:.82; }}
  .stats {{ position:relative; z-index:1; display:grid; grid-template-columns:1fr 1fr;
            gap:12px; margin-top:20px; }}
  .stat {{ padding:14px 16px; border-radius:18px; background:rgba(255,255,255,.15);
           border:1px solid rgba(255,255,255,.26);
           box-shadow:inset 0 1px 0 rgba(255,255,255,.3); }}
  .stat b {{ font-size:24px; font-weight:800; letter-spacing:-.02em; display:block; line-height:1.1; }}
  .stat span {{ font-size:12px; font-weight:600; opacity:.92; }}

  .card {{
    background:var(--card); border:1px solid var(--hair); border-radius:22px;
    padding:18px; margin-bottom:14px; box-shadow:var(--sh-soft);
    opacity:0; transform:translateY(12px);
    animation:rise .6s var(--ease) forwards;
    animation-delay:calc(min(var(--i),12) * 45ms + .1s);
  }}
  .card-head {{ display:flex; justify-content:space-between; align-items:center; gap:10px;
                flex-wrap:wrap; }}
  .oc {{ font-family:"JetBrains Mono",monospace; font-weight:700; font-size:12.5px;
         background:linear-gradient(180deg,#fff6dc,#ffedb8); color:#7a5600;
         padding:4px 10px; border-radius:8px; white-space:nowrap;
         box-shadow:inset 0 0 0 1px rgba(122,86,0,.14); }}
  .when {{ font-size:12.5px; color:var(--muted); font-weight:600; }}
  .card-mid {{ display:flex; justify-content:space-between; align-items:center; gap:10px;
               margin:12px 0 10px; flex-wrap:wrap; }}
  .items {{ font-weight:700; font-size:14px; }}
  .amt {{ color:var(--green); font-weight:800; font-size:16px; letter-spacing:-.01em; }}

  .v {{ background:var(--shell); border:1px solid var(--hair); border-left:3px solid var(--yellow);
        border-radius:12px; padding:10px 12px; margin-bottom:9px; }}
  .v:last-child {{ margin-bottom:0; }}
  .v-top {{ display:flex; align-items:center; gap:9px; }}
  .v-code {{ flex:1; font-family:"JetBrains Mono",monospace; font-weight:700; font-size:15px;
             color:var(--red); cursor:pointer; user-select:all; word-break:break-all;
             letter-spacing:.3px; }}
  .v-code.copied::after {{ content:" ✓"; color:var(--green); font-weight:700; }}
  .v-copy {{ border:none; cursor:pointer; background:linear-gradient(135deg,var(--red),var(--red-2));
             color:#fff; font-family:inherit; font-size:11px; font-weight:700; padding:6px 13px;
             border-radius:999px; white-space:nowrap;
             box-shadow:0 4px 12px rgba(228,0,43,.3), inset 0 1px 0 rgba(255,255,255,.28); }}
  .v-copy:active, .v-qr:active {{ transform:scale(.95); }}
  .v-qr {{ border:1px solid var(--hair); cursor:pointer; background:#fff;
           color:var(--ink); font-family:inherit; font-size:11px; font-weight:700; padding:6px 12px;
           border-radius:999px; white-space:nowrap; }}
  .v-barcode {{ margin-top:10px; padding:10px 12px 8px; border-radius:10px; background:#fff;
                text-align:center; box-shadow:inset 0 0 0 1px var(--hair); overflow-x:auto; }}
  .v-barcode[hidden] {{ display:none; }}
  .v-barcode .barcode {{ display:block; max-width:100%; height:48px; margin:0 auto; }}
  .v-link {{ display:inline-block; margin-top:7px; font-size:12px; font-weight:600;
             color:#1668d6; text-decoration:none; }}
  .v-link:hover {{ text-decoration:underline; }}

  .tip {{
    background:linear-gradient(180deg,#fff6dc,#ffefc2); border:1px solid rgba(122,86,0,.15);
    border-radius:18px; padding:14px 18px; font-size:13px; color:#6a4d00;
    margin-bottom:18px; font-weight:500;
    opacity:0; animation:rise .8s var(--ease) .2s forwards;
  }}
  .empty {{ text-align:center; color:var(--muted); padding:48px;
            background:var(--card); border-radius:22px; }}
  footer {{ text-align:center; color:#969aa2; font-size:12px; font-weight:500; margin:20px 0 4px; }}
  @keyframes rise {{ to {{ opacity:1; transform:translateY(0); }} }}
  @media (prefers-reduced-motion: reduce) {{
    .hero-shell, .card, .tip {{ animation:none; opacity:1; transform:none; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <div class="hero-shell">
    <div class="hero">
      <div class="brand">{_esc(shop_title)}</div>
      <h1>Lịch sử mua hàng của bạn</h1>
      <div class="gen">Xuất lúc {now} · {len(orders)} đơn gần nhất</div>
      <div class="stats">
        <div class="stat"><b>{total_count}</b><span>Đơn đã mua</span></div>
        <div class="stat"><b>{_esc(utils.format_vnd(total_spent))}</b><span>Tổng chi tiêu</span></div>
      </div>
    </div>
  </div>

  <div class="tip">
    💡 Bấm vào <b>mã voucher</b> / nút <b>Copy</b> để sao chép, hoặc bấm <b>Barcode</b> để mở mã vạch đưa nhân viên quét tại quầy.
  </div>

{cards_html}

  <footer>Cần hỗ trợ? Nhắn bot qua nút ☎️ Hỗ trợ trong Telegram nhé.</footer>
</div>
<script>
function cp(el){{
  const t = el.textContent.replace(/\\s*✓$/, '');
  if (navigator.clipboard) navigator.clipboard.writeText(t);
  el.classList.add('copied');
  setTimeout(()=>el.classList.remove('copied'), 1200);
}}
function tb(btn){{
  const box = btn.closest('.v').querySelector('.v-barcode');
  if (!box) return;
  box.hidden = !box.hidden;
}}
</script>
</body>
</html>'''


CODE128_PATTERNS = (
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212", "221213",
    "221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221", "223211", "221132",
    "221231", "213212", "223112", "312131", "311222", "321122", "321221", "312212", "322112", "322211",
    "212123", "212321", "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
    "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121", "313121", "211331",
    "231131", "213113", "213311", "213131", "311123", "311321", "331121", "312113", "312311", "332111",
    "314111", "221411", "431111", "111224", "111422", "121124", "121421", "141122", "141221", "112214",
    "112412", "122114", "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
    "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112", "421211", "212141",
    "214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113", "411311", "113141",
    "114131", "311141", "411131", "211412", "211214", "211232", "2331112",
)

_BARCODE_ASSETS_DIR = Path(__file__).resolve().parent.parent / "create_jollibee_barcode" / "assets"


@functools.lru_cache(maxsize=None)
def _barcode_asset_data_uri(relative_path: str, mime_type: str) -> str:
    asset_path = _BARCODE_ASSETS_DIR / relative_path
    encoded = base64.b64encode(asset_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def encode_code128b(text: str) -> list[int]:
    """Mã hóa chuỗi ký tự sang dãy symbol Code 128B (kèm start, checksum, stop)."""
    values = [ord(char) - 32 for char in text]
    checksum = (104 + sum(val * (idx + 1) for idx, val in enumerate(values))) % 103
    return [104, *values, checksum, 106]


def _render_code128b_svg(text: str) -> str:
    quiet_zone = 10
    modules = "".join(CODE128_PATTERNS[value] for value in encode_code128b(text))
    width = quiet_zone * 2 + sum(int(digit) for digit in modules)
    x = quiet_zone
    is_bar = True
    bars: list[str] = []
    for digit in modules:
        module_width = int(digit)
        if is_bar:
            bars.append(f'<rect x="{x}" y="0" width="{module_width}" height="50" />')
        x += module_width
        is_bar = not is_bar
    code_e = _esc(text)
    return (
        f'<svg class="barcode" viewBox="0 0 {width} 50" width="{width}" height="50" '
        f'shape-rendering="crispEdges" role="img" '
        f'aria-label="Barcode Code 128 cho mã {code_e}">'
        f'<g fill="#111">{"".join(bars)}</g></svg>'
    )


def _format_voucher_denom(value: int) -> str:
    return f"{value:,}".replace(",", ".") + " đ"


def render_order_vouchers_barcode_html(
    order_code: str,
    vouchers: list[tuple[str, int]],
) -> str:
    """Tạo trang HTML tự chứa chỉ hiển thị thẻ giao diện + barcode các mã đã mua.

    Loại bỏ hoàn toàn form tạo mã và xử lý query URL để khách chỉ xem được đúng
    các mã trong đơn hàng.
    """
    font_uri = _barcode_asset_data_uri("fonts/Quicksand-Variable.ttf", "font/ttf")
    mark_uri = _barcode_asset_data_uri("jollibee-mark.png", "image/png")
    egift_uri = _barcode_asset_data_uri("jollibee-egift.png", "image/png")
    notch_left_uri = _barcode_asset_data_uri("ticket-notch-left.png", "image/png")
    notch_right_uri = _barcode_asset_data_uri("ticket-notch-right.png", "image/png")
    urbox_uri = _barcode_asset_data_uri("urbox-logo.svg", "image/svg+xml")

    cards_html: list[str] = []
    for code, denom in vouchers:
        normalized_code = code.strip().upper()
        code_e = _esc(normalized_code)
        price_e = _esc(_format_voucher_denom(denom))
        barcode_svg = _render_code128b_svg(normalized_code)
        cards_html.append(
            f'''      <article class="voucher-card">
        <div class="gift-image" role="img" aria-label="Jollibee eGift powered by UrBox"></div>
        <div class="gift-copy">
          <div class="gift-title">[UrBox Voucher] JOLLIBEE</div>
          <div class="gift-price">{price_e}</div>
        </div>
        <div class="ticket-divider" aria-hidden="true"><span></span></div>
        <div class="code-info">
          <div class="urbox-logo" role="img" aria-label="UrBox"></div>
          <div class="barcode-shell">
            {barcode_svg}
          </div>
          <button class="copy-code" type="button" data-code="{code_e}" aria-label="Sao chép mã voucher {code_e}">
            <span class="copy-hint">
              <svg class="copy-icon" viewBox="0 0 24 24" aria-hidden="true">
                <rect width="14" height="14" x="8" y="8" rx="2"></rect>
                <path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"></path>
              </svg>
              Nhấp vào mã để sao chép
            </span>
            <span class="code-value">{code_e}</span>
          </button>
        </div>
      </article>'''
        )

    cards_joined = "\n".join(cards_html)
    order_e = _esc(order_code)
    return f'''<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#ffffff">
  <link rel="icon" href="data:,">
  <title>Jollibee eGift · {order_e}</title>
  <style>
    @font-face {{
      font-family: "Quicksand";
      src: url("{font_uri}") format("truetype");
      font-style: normal;
      font-weight: 300 700;
      font-display: swap;
    }}

    :root {{
      --ink: #380606;
      --plum: #720d5d;
      --red: #ed1b2f;
      --panel: #fff;
      --canvas: #f0f3f5;
      --shadow: 0 16px 46px rgb(22 60 132 / 14%);
    }}

    * {{ box-sizing: border-box; }}
    html {{ min-width: 320px; background: var(--canvas); }}
    body {{
      min-height: 100vh;
      margin: 0;
      color: #1d2433;
      font-family: "Quicksand", sans-serif;
      background: var(--canvas);
    }}
    button {{ font: inherit; cursor: pointer; }}

    .site-header {{
      position: fixed;
      z-index: 10;
      inset: 0 0 auto;
      height: 60px;
      display: grid;
      place-items: center;
      background: #fff;
      box-shadow: 0 3px 6px rgb(22 60 132 / 16%);
    }}
    .site-header img {{ width: 40px; height: 40px; object-fit: contain; }}

    .workspace {{
      width: min(520px, calc(100% - 28px));
      margin: 0 auto;
      padding: 92px 0 54px;
      display: grid;
      grid-template-columns: 1fr;
      align-items: start;
    }}

    .preview-stage {{
      min-width: 0;
      display: grid;
      place-items: start center;
      gap: 24px;
    }}
    .voucher-card {{
      position: relative;
      width: min(360px, 100%);
      overflow: hidden;
      border-radius: 10px;
      padding: 12px;
      background: var(--panel);
      box-shadow: 0 3px 8px rgb(22 60 132 / 18%);
      animation: card-in 520ms cubic-bezier(.2, .75, .25, 1) both;
    }}
    .gift-image {{
      display: block;
      width: 100%;
      aspect-ratio: 640 / 335;
      border-radius: 7px;
      background: url("{egift_uri}") center / cover no-repeat;
    }}
    .gift-copy {{ padding: 10px 4px 3px; text-align: center; }}
    .gift-title {{ color: var(--ink); font-size: 16px; line-height: 1.35; }}
    .gift-price {{ margin-top: 2px; color: var(--plum); font-size: 22px; font-weight: 800; }}

    .ticket-divider {{
      position: relative;
      overflow: inherit;
      margin: 0 -20px;
      height: 16px;
      padding: 0 25px;
    }}
    .ticket-divider::before, .ticket-divider::after {{
      content: "";
      width: 18px;
      height: 26px;
      position: absolute;
      top: -6px;
    }}
    .ticket-divider::before {{
      left: -1px;
      background: url("{notch_left_uri}") no-repeat 0 0;
      background-size: cover;
    }}
    .ticket-divider::after {{
      right: 0;
      background: url("{notch_right_uri}") no-repeat 0 0;
      background-size: contain;
    }}
    .ticket-divider span {{
      display: block;
      border-bottom: 1px dashed #e0e0e0;
      padding: 7px 0 0;
    }}

    .code-info {{ padding: 7px 0 5px; text-align: center; }}
    .urbox-logo {{
      display: block;
      width: 100%;
      height: 37px;
      margin: 0 auto 9px;
      background: url("{urbox_uri}") center / contain no-repeat;
    }}
    .barcode-shell {{
      width: fit-content;
      max-width: 94%;
      margin: 0 auto;
      overflow: hidden;
      border-radius: 8px;
      padding: 8px 12px 6px;
      background: #fff;
      box-shadow: 0 3px 10px rgb(22 60 132 / 16%);
    }}
    .barcode {{ display: block; max-width: 100%; height: 50px; }}
    .copy-code {{
      display: inline-grid;
      justify-items: center;
      gap: 6px;
      margin-top: 12px;
      border: 0;
      padding: 0;
      color: inherit;
      background: transparent;
    }}
    .copy-hint {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      color: #292929;
      font-size: 11px;
      font-weight: 600;
    }}
    .copy-icon {{
      width: 12px;
      height: 12px;
      flex: none;
      fill: none;
      stroke: currentColor;
      stroke-linecap: round;
      stroke-linejoin: round;
      stroke-width: 1.7;
    }}
    .code-value {{
      color: var(--ink);
      font-size: 19px;
      font-weight: 900;
      letter-spacing: .02em;
      word-break: break-all;
    }}

    .toast {{
      position: fixed;
      z-index: 30;
      right: 22px;
      bottom: 22px;
      transform: translateY(18px);
      border-radius: 12px;
      padding: 12px 15px;
      color: #fff;
      background: #25202a;
      box-shadow: 0 14px 36px rgb(0 0 0 / 20%);
      font-size: 13px;
      font-weight: 800;
      opacity: 0;
      pointer-events: none;
      transition: opacity 180ms ease, transform 180ms ease;
    }}
    .toast.is-visible {{ transform: translateY(0); opacity: 1; }}

    @keyframes card-in {{
      from {{ transform: translateY(14px); opacity: 0; }}
      to {{ transform: translateY(0); opacity: 1; }}
    }}
    @media (max-width: 410px) {{
      .workspace {{ width: 100%; padding-inline: 9px; }}
      .voucher-card {{ width: 100%; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      *, *::before, *::after {{
        scroll-behavior: auto !important;
        animation-duration: .01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: .01ms !important;
      }}
    }}
    @media print {{
      .site-header, .toast {{ display: none !important; }}
      body, html {{ background: #fff; }}
      .workspace {{ display: block; width: 360px; margin: 0 auto; padding: 0; }}
      .voucher-card {{ box-shadow: none; margin-bottom: 20px; page-break-inside: avoid; }}
    }}
  </style>
</head>
<body class="view-only">
  <header class="site-header" aria-label="Jollibee">
    <img src="{mark_uri}" alt="Jollibee">
  </header>

  <main class="workspace">
    <section class="preview-stage" aria-label="Bản xem voucher">
{cards_joined}
    </section>
  </main>

  <div class="toast" id="toast" role="status" aria-live="polite"></div>
  <script>
    const toast = document.querySelector("#toast");
    let toastTimer;

    function showToast(message) {{
      clearTimeout(toastTimer);
      toast.textContent = message;
      toast.classList.add("is-visible");
      toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 1800);
    }}

    async function copyText(text, successMessage) {{
      try {{
        await navigator.clipboard.writeText(text);
      }} catch {{
        const temporary = document.createElement("textarea");
        temporary.value = text;
        temporary.style.position = "fixed";
        temporary.style.opacity = "0";
        document.body.appendChild(temporary);
        temporary.select();
        document.execCommand("copy");
        temporary.remove();
      }}
      showToast(successMessage);
    }}

    document.querySelectorAll(".copy-code").forEach((button) => {{
      button.addEventListener("click", () => {{
        const code = button.getAttribute("data-code") || "";
        copyText(code, "Đã sao chép mã");
      }});
    }});
  </script>
</body>
</html>'''
