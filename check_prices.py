#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_prices.py — App theo dõi giá đối thủ cho Vutech (vutechs.com)
====================================================================

Mục đích: tự động refresh giá đối thủ cho các SKU ĐÃ CÓ SẴN link tham khảo
trong Google Sheet "Khởi nghiệp" / tab "Website" (cột M), tính lại công thức
định giá + lợi nhuận thực, và ghi báo cáo vào 1 tab MỚI (không đụng tab
Website gốc) + xuất dashboard HTML.

KHÔNG dùng AI/LLM — chạy bằng requests + BeautifulSoup thuần, 0 token,
tốc độ vài chục giây cho toàn bộ danh mục.

GIỚI HẠN QUAN TRỌNG (đọc kỹ trước khi tin tưởng 100% kết quả):
- Chỉ refresh được SKU đã có link đối thủ sẵn trong cột M. Không tự tìm
  giá cho SKU MỚI (việc đó vẫn cần Claude + skill vutech-1-danh-gia-dinh-gia
  vì đòi hỏi phân biệt đúng biến thể / tên model theo thị trường VN / nhận
  diện giá mồi câu traffic).
- KHÔNG tự động ghi đè cột "Giá bán" trên tab Website — chỉ báo cáo lệch giá
  để Vũ tự quyết định có đổi giá hay không.
- Nếu 1 site đối thủ đổi giao diện, script có thể lấy sai/không lấy được giá
  — mỗi dòng đều có cột "Trạng thái lấy giá" để biết dòng nào tin được, dòng
  nào cần Vũ tự kiểm tra tay.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import gspread
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials

# --------------------------------------------------------------------------
# Cấu hình
# --------------------------------------------------------------------------

SHEET_ID = os.environ.get("SHEET_ID", "1nbSgLMnXfVAftlmU1MCYiAH0B1LPFPuPy5kXtFcP3YU")
SOURCE_TAB = "Website"
OUTPUT_TAB = "Theo dõi giá đối thủ (Auto)"

# Vùng đọc trên tab Website — khớp với header thật đã xác nhận 11/08/2026:
# B=STT C=Mã sản phẩm D=Tên sản phẩm E=Ngành hàng F=Hãng G=ID hãng
# H=Tên SP NPP I=Giá nhập J=Giá bán K=Lợi nhuận(formula) L=Biên(formula)
# M=Link tham khảo giá N=Số ảnh O=SEO số từ P=Link sản phẩm R=Khối lượng S=Trạng thái
SOURCE_RANGE = "C3:M1000"  # KHÔNG thêm tên sheet — ws.get() đã tự gắn tên sheet của chính nó
DATA_START_ROW = 3

SHIP_FEE = 35_000  # phí ship cố định cộng vào mỗi đơn, theo Bước 5 skill vutech-1-danh-gia-dinh-gia
REQUEST_TIMEOUT = 15
REQUEST_DELAY = 1.2  # giây, nghỉ giữa 2 lần tải trang để lịch sự với site đối thủ
VN_TZ = timezone(timedelta(hours=7))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 VutechPriceBot/1.0 "
                "(+https://vutechs.com - bot theo doi gia tham khao, chay 1 lan/ngay)"
    )
}

URL_RE = re.compile(r'https?://[^\s")]+')


# --------------------------------------------------------------------------
# Trích xuất giá từ 1 trang sản phẩm đối thủ — thử lần lượt nhiều cách,
# ưu tiên nguồn dữ liệu có cấu trúc (đáng tin) trước khi fallback qua text.
# --------------------------------------------------------------------------

def _clean_price(raw):
    """Bóc số từ chuỗi giá kiểu '3.590.000đ' hay '3590000' -> 3590000 (int)."""
    if raw is None:
        return None
    digits = re.sub(r"[^\d]", "", str(raw))
    if not digits:
        return None
    value = int(digits)
    # Giá VND thật luôn >= vài chục nghìn — loại số quá nhỏ (khả năng bóc nhầm SKU/mã khác)
    if value < 10_000:
        return None
    return value


def _clean_availability(raw):
    if not raw:
        return "không rõ"
    raw = str(raw).lower()
    if "instock" in raw:
        return "còn hàng"
    if "outofstock" in raw:
        return "hết hàng"
    if "preorder" in raw or "backorder" in raw:
        return "đặt trước"
    return "không rõ"


def extract_from_ldjson(soup):
    """Ưu tiên #1 — schema.org Product JSON-LD (xác nhận có ở gearvn.com, cellphones.com.vn)."""
    for script in soup.find_all("script", type="application/ld+json"):
        raw_text = script.string or script.get_text() or ""
        try:
            data = json.loads(raw_text)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            candidates = graph if isinstance(graph, list) else [item]
            for cand in candidates:
                if not isinstance(cand, dict) or cand.get("@type") != "Product":
                    continue
                offers = cand.get("offers")
                if isinstance(offers, list):
                    offers = offers[0] if offers else None
                if isinstance(offers, dict):
                    price = _clean_price(offers.get("price"))
                    if price:
                        return price, _clean_availability(offers.get("availability")), "json-ld"
    return None, None, None


def extract_from_meta(soup):
    """Ưu tiên #2 — meta og:price:amount / meta itemprop=price (xác nhận có ở bpstore.vn)."""
    meta = (
        soup.find("meta", attrs={"property": "og:price:amount"})
        or soup.find("meta", attrs={"itemprop": "price"})
        or soup.find("meta", attrs={"name": "price"})
    )
    if meta and meta.get("content"):
        price = _clean_price(meta["content"])
        if price:
            return price, None, "meta-tag"
    return None, None, None


def extract_from_microdata(soup):
    """Ưu tiên #3 — thẻ bất kỳ có itemprop=price (content hoặc text)."""
    el = soup.find(attrs={"itemprop": "price"})
    if el:
        val = el.get("content") or el.get_text()
        price = _clean_price(val)
        if price:
            return price, None, "microdata"
    return None, None, None


def extract_from_common_selectors(soup):
    """Ưu tiên #4 (thấp nhất, cần Vũ xác minh) — selector giá phổ biến của
    WooCommerce/Sapo/Haravan/theme tự code. Không dùng làm nguồn duy nhất
    để tự tin cao — luôn gắn nhãn 'cần xác minh' khi rơi vào nhánh này."""
    selectors = [
        ".price ins .amount",
        ".price .amount",
        ".product-price .price",
        ".product-price",
        ".current-price",
        ".woocommerce-Price-amount",
        "[class*='price'] .amount",
    ]
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            price = _clean_price(el.get_text())
            if price:
                return price, None, "fallback-selector (cần xác minh tay)"
    return None, None, None


EXTRACTORS = (extract_from_ldjson, extract_from_meta, extract_from_microdata, extract_from_common_selectors)


def fetch_competitor_price(url, session):
    try:
        resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        return {"ok": False, "error": f"Lỗi tải trang: {e}"}

    soup = BeautifulSoup(resp.text, "lxml")
    for extractor in EXTRACTORS:
        price, availability, source = extractor(soup)
        if price:
            return {
                "ok": True,
                "price": price,
                "availability": availability or "không rõ",
                "source": source,
            }
    return {"ok": False, "error": "Không tìm thấy giá bằng bất kỳ phương pháp nào đã thử"}


# --------------------------------------------------------------------------
# Công thức định giá + lợi nhuận (đúng theo skill vutech-1-danh-gia-dinh-gia)
# --------------------------------------------------------------------------

def gia_de_xuat_tu_doi_thu(gia_doi_thu):
    """Mốc 1 triệu (chốt 2026-07-27): <1tr trừ 1.000đ, >=1tr trừ 10.000đ."""
    if gia_doi_thu < 1_000_000:
        return gia_doi_thu - 1_000
    return gia_doi_thu - 10_000


def tinh_loi_nhuan_thuc(gia_ban, gia_nhap):
    """Công thức chốt 2026-07-13: Lợi nhuận thực = Giá bán - Giá nhập - 35.000đ ship."""
    if not gia_ban or not gia_nhap:
        return None, None, "missing"
    loi_nhuan = gia_ban - gia_nhap - SHIP_FEE
    bien = round(loi_nhuan / gia_ban * 100, 1) if gia_ban else None
    if gia_ban <= gia_nhap:
        flag = "gia_ban_duoi_von"
    elif loi_nhuan <= 0:
        flag = "danger"
    elif bien is not None and bien < 10:
        flag = "warn"
    else:
        flag = "ok"
    return loi_nhuan, bien, flag


def to_number(val):
    if val is None or val == "":
        return None
    try:
        return float(re.sub(r"[^\d.\-]", "", str(val)))
    except Exception:
        return None


def extract_url_and_label(cell_value):
    """Cột M có 2 dạng thật đã thấy trên Sheet: URL thô kèm ghi chú trong
    ngoặc, hoặc công thức =HYPERLINK("url","nhãn"). Đọc bằng value_render_option
    FORMULA nên cả 2 dạng đều là string thô ở đây — bóc URL bằng regex chung."""
    if not cell_value:
        return None, None
    text = str(cell_value)
    m = URL_RE.search(text)
    url = m.group(0).rstrip(').,;"') if m else None
    label = urlparse(url).netloc if url else None
    return url, label


# --------------------------------------------------------------------------
# Google Sheets I/O
# --------------------------------------------------------------------------

def get_gspread_client():
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_json:
        sys.exit("Thiếu biến môi trường GOOGLE_SERVICE_ACCOUNT_JSON (nội dung file service account key).")
    info = json.loads(raw_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def read_source_rows(sh):
    ws = sh.worksheet(SOURCE_TAB)
    # FORMULA render option: ô có công thức (=HYPERLINK, =J-I...) trả về chuỗi
    # công thức thô; ô thường trả về giá trị thô — khớp đúng cách mình đã
    # kiểm tra thật trên file Sheet trước khi viết script này.
    values = ws.get(SOURCE_RANGE, value_render_option="FORMULA")
    rows = []
    for i, row in enumerate(values):
        row = row + [""] * (11 - len(row))  # pad cho đủ 11 cột C..M
        sku = row[0]
        if not sku:
            continue
        rows.append(
            {
                "sheet_row": DATA_START_ROW + i,
                "sku": sku,
                "ten": row[1],
                "gia_nhap": to_number(row[6]),
                "gia_ban": to_number(row[7]),
                "link_raw": row[10],
            }
        )
    return rows


def write_output(sh, results):
    header = [
        "SKU",
        "Tên sản phẩm",
        "Giá nhập",
        "Giá bán Vutech hiện tại",
        "Giá đối thủ mới nhất",
        "Tồn kho đối thủ",
        "Nguồn",
        "Giá đề xuất theo công thức",
        "Chênh lệch (hiện tại - đề xuất)",
        "Lợi nhuận thực",
        "Biên thực %",
        "Cờ cảnh báo",
        "Trạng thái lấy giá",
        "Cập nhật lúc (giờ VN)",
    ]
    body = []
    for r in results:
        body.append(
            [
                r["sku"],
                r["ten"],
                r["gia_nhap"] or "",
                r["gia_ban"] or "",
                r["gia_doi_thu"] or "",
                r["ton_kho_doi_thu"] or "",
                (f'=HYPERLINK("{r["url"]}";"{r["nguon"]}")' if r["url"] else (r["nguon"] or "")),
                r["gia_de_xuat"] or "",
                r["chenh_lech"] if r["chenh_lech"] is not None else "",
                r["loi_nhuan_thuc"] if r["loi_nhuan_thuc"] is not None else "",
                r["bien_thuc"] if r["bien_thuc"] is not None else "",
                r["co_canh_bao"],
                r["trang_thai_lay_gia"],
                r["cap_nhat_luc"],
            ]
        )

    try:
        ws = sh.worksheet(OUTPUT_TAB)
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=OUTPUT_TAB, rows=max(len(body) + 10, 50), cols=len(header) + 2)

    ws.update([header] + body, value_input_option="USER_ENTERED")
    return ws


def format_vnd(v):
    if v is None or v == "":
        return "—"
    try:
        return f"{int(round(float(v))):,}đ".replace(",", ".")
    except Exception:
        return str(v)


def generate_dashboard_html(results, output_path):
    ts = datetime.now(VN_TZ).strftime("%d/%m/%Y %H:%M")
    total = len(results)
    with_link = [r for r in results if r["url"]]
    danger = [r for r in results if "🔴" in r["co_canh_bao"]]
    warn = [r for r in results if "🟡" in r["co_canh_bao"] or "🟠" in r["co_canh_bao"]]
    err = [r for r in with_link if not r["trang_thai_lay_gia"].startswith("OK")]

    rows_html = []
    for r in results:
        flag_class = "danger" if "🔴" in r["co_canh_bao"] else ("warn" if ("🟡" in r["co_canh_bao"] or "🟠" in r["co_canh_bao"]) else "ok")
        link_html = f'<a href="{r["url"]}" target="_blank" rel="noopener">{r["nguon"]}</a>' if r["url"] else "—"
        rows_html.append(
            f"""<tr class="{flag_class}"
                data-sku="{r['sku']}" data-ten="{r['ten']}"
                data-gianhap="{r['gia_nhap'] or 0}" data-giaban="{r['gia_ban'] or 0}"
                data-giadoithu="{r['gia_doi_thu'] or 0}" data-loinhuan="{r['loi_nhuan_thuc'] or 0}"
                data-bien="{r['bien_thuc'] if r['bien_thuc'] is not None else 0}"
                data-flag="{flag_class}">
                <td>{r['sku']}</td>
                <td>{r['ten']}</td>
                <td class="num">{format_vnd(r['gia_nhap'])}</td>
                <td class="num">{format_vnd(r['gia_ban'])}</td>
                <td class="num">{format_vnd(r['gia_doi_thu'])}</td>
                <td>{r['ton_kho_doi_thu'] or '—'}</td>
                <td>{link_html}</td>
                <td class="num">{format_vnd(r['gia_de_xuat'])}</td>
                <td class="num">{format_vnd(r['loi_nhuan_thuc'])}</td>
                <td class="num">{r['bien_thuc'] if r['bien_thuc'] is not None else '—'}%</td>
                <td>{r['co_canh_bao'] or ''}</td>
                <td class="muted">{r['trang_thai_lay_gia']}</td>
            </tr>"""
        )

    html = f"""<!DOCTYPE html>
<html lang="vi"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vutech — Theo dõi giá đối thủ (Auto)</title>
<style>
  :root {{ --bg:#0f1117; --card:#171a23; --border:#262a37; --text:#e6e8ef; --muted:#8b93a7;
           --danger:#ff5c6c; --warn:#f5c04a; --ok:#4ade80; --accent:#7dd3fc; }}
  * {{ box-sizing:border-box; }}
  body {{ background:var(--bg); color:var(--text); font-family:-apple-system,Segoe UI,Roboto,sans-serif; margin:0; padding:24px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:20px; }}
  .stats {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:20px; }}
  .stat {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:12px 16px; min-width:140px; }}
  .stat .n {{ font-size:22px; font-weight:700; }}
  .stat .l {{ color:var(--muted); font-size:12px; }}
  .stat.danger .n {{ color:var(--danger); }}
  .stat.warn .n {{ color:var(--warn); }}
  .stat.ok .n {{ color:var(--ok); }}
  .controls {{ display:flex; gap:10px; margin-bottom:12px; flex-wrap:wrap; }}
  input, select {{ background:var(--card); border:1px solid var(--border); color:var(--text); padding:8px 12px; border-radius:8px; font-size:13px; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border-radius:10px; overflow:hidden; font-size:13px; }}
  th, td {{ padding:8px 10px; border-bottom:1px solid var(--border); text-align:left; vertical-align:top; }}
  th {{ cursor:pointer; user-select:none; color:var(--accent); position:sticky; top:0; background:var(--card); white-space:nowrap; }}
  th:hover {{ color:#fff; }}
  td.num {{ text-align:right; white-space:nowrap; }}
  td.muted {{ color:var(--muted); font-size:12px; }}
  tr.danger {{ background:rgba(255,92,108,0.07); }}
  tr.warn {{ background:rgba(245,192,74,0.06); }}
  a {{ color:var(--accent); }}
</style>
</head><body>
  <h1>Vutech — Dashboard theo dõi giá đối thủ (Auto, 0 token)</h1>
  <div class="sub">Cập nhật lúc {ts} (giờ VN) · Nguồn: Google Sheet "Khởi nghiệp" / tab Website · Script chạy tự động, không dùng AI</div>

  <div class="stats">
    <div class="stat"><div class="n">{total}</div><div class="l">Tổng SKU</div></div>
    <div class="stat"><div class="n">{len(with_link)}</div><div class="l">Có link đối thủ</div></div>
    <div class="stat danger"><div class="n">{len(danger)}</div><div class="l">Cờ đỏ (lỗ / dưới vốn / mất cạnh tranh)</div></div>
    <div class="stat warn"><div class="n">{len(warn)}</div><div class="l">Cờ vàng (biên mỏng / cần xác minh)</div></div>
    <div class="stat"><div class="n">{len(err)}</div><div class="l">Lỗi lấy giá tự động</div></div>
  </div>

  <div class="controls">
    <input id="search" type="text" placeholder="Tìm SKU / tên sản phẩm..." style="min-width:260px">
    <select id="filterFlag">
      <option value="">Tất cả cờ</option>
      <option value="danger">Chỉ cờ đỏ</option>
      <option value="warn">Chỉ cờ vàng</option>
      <option value="ok">Chỉ ổn</option>
    </select>
  </div>

  <table id="tbl">
    <thead><tr>
      <th data-k="sku">SKU</th><th data-k="ten">Tên sản phẩm</th>
      <th data-k="gianhap" class="num">Giá nhập</th><th data-k="giaban" class="num">Giá bán hiện tại</th>
      <th data-k="giadoithu" class="num">Giá đối thủ mới nhất</th><th>Tồn kho đối thủ</th><th>Nguồn</th>
      <th class="num">Giá đề xuất</th><th data-k="loinhuan" class="num">Lợi nhuận thực</th>
      <th data-k="bien" class="num">Biên %</th><th>Cờ cảnh báo</th><th>Trạng thái lấy giá</th>
    </tr></thead>
    <tbody>
      {''.join(rows_html)}
    </tbody>
  </table>

<script>
  const search = document.getElementById('search');
  const filterFlag = document.getElementById('filterFlag');
  const rows = Array.from(document.querySelectorAll('#tbl tbody tr'));

  function applyFilters() {{
    const q = search.value.toLowerCase();
    const flag = filterFlag.value;
    rows.forEach(r => {{
      const text = (r.dataset.sku + ' ' + r.dataset.ten).toLowerCase();
      const matchQ = text.includes(q);
      const matchFlag = !flag || r.dataset.flag === flag;
      r.style.display = (matchQ && matchFlag) ? '' : 'none';
    }});
  }}
  search.addEventListener('input', applyFilters);
  filterFlag.addEventListener('change', applyFilters);

  document.querySelectorAll('th[data-k]').forEach(th => {{
    let asc = true;
    th.addEventListener('click', () => {{
      const k = th.dataset.k;
      const tbody = document.querySelector('#tbl tbody');
      const sorted = rows.slice().sort((a, b) => {{
        const av = a.dataset[k], bv = b.dataset[k];
        const an = parseFloat(av), bn = parseFloat(bv);
        let cmp;
        if (!isNaN(an) && !isNaN(bn)) cmp = an - bn;
        else cmp = String(av).localeCompare(String(bv), 'vi');
        return asc ? cmp : -cmp;
      }});
      sorted.forEach(r => tbody.appendChild(r));
      asc = !asc;
    }});
  }});
</script>
</body></html>"""

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return output_path


# --------------------------------------------------------------------------
# Xử lý chính
# --------------------------------------------------------------------------

def process_row(row, session):
    now_str = datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M")
    url, domain_label = extract_url_and_label(row["link_raw"])

    result = {
        "sku": row["sku"],
        "ten": row["ten"],
        "gia_nhap": row["gia_nhap"],
        "gia_ban": row["gia_ban"],
        "gia_doi_thu": None,
        "ton_kho_doi_thu": None,
        "nguon": domain_label,
        "url": url,
        "gia_de_xuat": None,
        "chenh_lech": None,
        "loi_nhuan_thuc": None,
        "bien_thuc": None,
        "co_canh_bao": "",
        "trang_thai_lay_gia": "",
        "cap_nhat_luc": now_str,
    }

    loi_nhuan, bien, flag_margin = tinh_loi_nhuan_thuc(row["gia_ban"], row["gia_nhap"])
    result["loi_nhuan_thuc"] = loi_nhuan
    result["bien_thuc"] = bien

    flags = []
    if flag_margin == "gia_ban_duoi_von":
        flags.append("🔴 GIÁ BÁN ≤ GIÁ VỐN — lỗi định giá nội bộ, ưu tiên xử lý trước")
    elif flag_margin == "danger":
        flags.append("🔴 lỗ sau ship")
    elif flag_margin == "warn":
        flags.append("🟡 biên mỏng (<10%)")
    elif flag_margin == "missing":
        flags.append("⚪ thiếu Giá nhập/Giá bán, chưa tính được")

    if not url:
        result["trang_thai_lay_gia"] = "bỏ qua — chưa có link đối thủ trong cột M"
        result["co_canh_bao"] = "; ".join(flags)
        return result

    scrape = fetch_competitor_price(url, session)
    if not scrape["ok"]:
        result["trang_thai_lay_gia"] = f"LỖI: {scrape['error']}"
        flags.append("⚠️ không lấy được giá tự động — cần Vũ kiểm tra tay")
        result["co_canh_bao"] = "; ".join(flags)
        return result

    gia_doi_thu = scrape["price"]
    result["gia_doi_thu"] = gia_doi_thu
    result["ton_kho_doi_thu"] = scrape["availability"]
    result["trang_thai_lay_gia"] = f"OK ({scrape['source']})"

    de_xuat = gia_de_xuat_tu_doi_thu(gia_doi_thu)
    result["gia_de_xuat"] = de_xuat

    if row["gia_ban"] is not None:
        result["chenh_lech"] = round(row["gia_ban"] - de_xuat)

    if "cần xác minh" in scrape["source"]:
        flags.append("🟠 nguồn giá độ tin cậy thấp (fallback selector) — nên xác minh tay")

    if scrape["availability"] == "hết hàng":
        flags.append("⚪ đối thủ đang HẾT HÀNG — giá lấy được có thể không phản ánh thị trường thực")

    if row["gia_ban"] is not None:
        if row["gia_ban"] > gia_doi_thu:
            flags.append(f"🔴 Vutech đang CAO HƠN đối thủ {int(row['gia_ban'] - gia_doi_thu):,}đ — mất cạnh tranh".replace(",", "."))
        elif result["chenh_lech"] is not None and result["chenh_lech"] < -20_000:
            flags.append("🟢 đối thủ đã tăng giá — Vutech có thể tăng giá bán theo công thức để tăng lợi nhuận")

    result["co_canh_bao"] = "; ".join(flags)
    return result


def main():
    print(f"[{datetime.now(VN_TZ).isoformat()}] Bắt đầu check giá đối thủ Vutech...")
    client = get_gspread_client()
    sh = client.open_by_key(SHEET_ID)

    rows = read_source_rows(sh)
    print(f"Đọc được {len(rows)} SKU từ tab '{SOURCE_TAB}'.")

    with_link = [r for r in rows if extract_url_and_label(r["link_raw"])[0]]
    print(f"  → {len(with_link)} SKU có link đối thủ sẽ được refresh giá.")
    print(f"  → {len(rows) - len(with_link)} SKU chưa có link, chỉ tính lại lợi nhuận từ Giá nhập/Giá bán có sẵn.")

    session = requests.Session()
    results = []
    ok_count, err_count = 0, 0
    domain_stats = {}

    for idx, row in enumerate(rows, start=1):
        r = process_row(row, session)
        results.append(r)
        if r["url"]:
            time.sleep(REQUEST_DELAY)  # lịch sự với site đối thủ, tránh bị chặn IP
            domain = r["nguon"] or "?"
            bucket = domain_stats.setdefault(domain, {"ok": 0, "err": 0})
            if r["trang_thai_lay_gia"].startswith("OK"):
                ok_count += 1
                bucket["ok"] += 1
            else:
                err_count += 1
                bucket["err"] += 1
        if idx % 20 == 0:
            print(f"  ...đã xử lý {idx}/{len(rows)}")

    print(f"\nKết quả scrape: {ok_count} OK / {err_count} lỗi (trên {len(with_link)} SKU có link).")
    print("Theo domain:")
    for domain, stat in sorted(domain_stats.items(), key=lambda x: -sum(x[1].values())):
        print(f"  {domain}: {stat['ok']} OK, {stat['err']} lỗi")

    ws_out = write_output(sh, results)
    print(f"\nĐã ghi kết quả vào tab '{OUTPUT_TAB}' ({ws_out.url}).")

    dashboard_path = generate_dashboard_html(results, os.path.join("docs", "index.html"))
    print(f"Đã xuất dashboard: {dashboard_path}")

    danger = [r for r in results if "🔴" in r["co_canh_bao"]]
    print(f"\n⚠️  {len(danger)} SKU có cờ đỏ cần chú ý.")

    return results


if __name__ == "__main__":
    main()
