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

Mỗi SKU có thể theo dõi NHIỀU đối thủ cùng lúc — cột M cho phép dán nhiều
URL (cách nhau bởi dấu phẩy, xuống dòng, hoặc khoảng trắng). Script sẽ tải
hết các URL đó, và báo cáo GIÁ THẤP NHẤT trong số các đối thủ hiện đang
CÒN HÀNG (nếu tất cả đều hết hàng thì lấy giá thấp nhất trong số đã lấy
được, kèm cờ cảnh báo để Vũ biết giá đó không chắc phản ánh thị trường).

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

QUY TẮC TỐI THƯỢNG (thêm 22/08/2026, chốt với Vũ): KHÔNG ĐƯỢC LỖ, và biên lãi
tối thiểu phải là markup 5% trên giá vốn (Giá bán sàn = Giá nhập × 1.05). Giá
đề xuất theo đối thủ KHÔNG BAO GIỜ được để thấp hơn giá sàn này — nếu giá đối
thủ ép xuống dưới sàn, ưu tiên giữ sàn 5%, chấp nhận mất lợi thế giá rẻ nhất.
Script tự tính 3 chỉ số mới mỗi ngày (xem tab "Theo dõi giá đối thủ (Auto)" +
tab riêng "Đề xuất tăng giá (Auto)"): (1) số SKU sẽ LỖ nếu bán đúng giá đề
xuất theo đối thủ, (2) số SKU đang dưới sàn markup 5%, (3) danh sách SKU cần
tăng giá (bắt buộc do dưới sàn, hoặc cơ hội do đối thủ đã tăng giá).
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
SOURCE_RANGE = "C3:M5000"  # KHÔNG thêm tên sheet — ws.get() đã tự gắn tên sheet của chính nó
DATA_START_ROW = 3

SHIP_FEE = 0  # anh không còn chịu phí ship từ 22/08/2026 (cập nhật theo yêu cầu Vũ)

# QUY TẮC TỐI THƯỢNG (chốt 22/08/2026 với Vũ): KHÔNG ĐƯỢC LỖ + biên lãi tối thiểu 5%.
# Biên 5% tính theo MARKUP trên GIÁ VỐN (giá nhập) — không phải % trên giá bán:
#   Giá bán sàn = Giá nhập × 1.05, làm tròn LÊN bội số 1.000đ.
# Ví dụ: giá vốn 950.000đ → giá bán sàn tối thiểu = 997.500đ → làm tròn lên 998.000đ.
MARGIN_FLOOR_MARKUP = 0.05
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
                    if not price:
                        # Sự cố 22/08/2026: owlgaming.vn (và có thể site khác) không để
                        # giá trực tiếp ở offers.price, mà bọc trong
                        # offers.priceSpecification[].price (khai báo giá theo
                        # schema.org UnitPriceSpecification, thường kèm cờ
                        # valueAddedTaxIncluded cho yêu cầu hiển thị giá/VAT VN).
                        # Không đọc được path này khiến script rơi xuống tận
                        # fallback-selector và lấy NHẦM giá 1 sản phẩm khác hẳn
                        # (SKU MOU-COR-SABRE-V2PRO-CF-BLK bị lấy nhầm giá ghế gaming).
                        pspec = offers.get("priceSpecification")
                        if isinstance(pspec, list):
                            pspec = pspec[0] if pspec else None
                        if isinstance(pspec, dict):
                            price = _clean_price(pspec.get("price"))
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


# Sự cố 22/08/2026: selector "ưu tiên #4" từng lấy nhầm giá của 1 sản phẩm KHÁC
# (thường ở khối "Sản phẩm liên quan/gợi ý") vì soup.select_one() quét TOÀN
# TRANG, không chỉ khu vực sản phẩm chính. Trước khi tìm theo selector, loại bỏ
# hẳn các khối liên quan/gợi ý/upsell ra khỏi cây HTML để giảm rủi ro này.
_RELATED_SECTION_SELECTORS = [
    ".related", ".related-products", ".products.related",
    ".upsells", ".up-sells", ".cross-sells",
    "[class*='lien-quan']", "[class*='goi-y']", "[class*='tuong-tu']",
    "[class*='san-pham-khac']", "[id*='related']",
    "aside", "footer",
]


def extract_from_common_selectors(soup):
    """Ưu tiên #4 (thấp nhất, cần Vũ xác minh) — selector giá phổ biến của
    WooCommerce/Sapo/Haravan/theme tự code. Không dùng làm nguồn duy nhất
    để tự tin cao — luôn gắn nhãn 'cần xác minh' khi rơi vào nhánh này.
    Trước khi tìm, dọn bỏ các khối sản phẩm liên quan/gợi ý (xem
    _RELATED_SECTION_SELECTORS) để tránh lấy nhầm giá sản phẩm khác — sự cố
    thật đã xảy ra 22/08/2026 với SKU MOU-COR-SABRE-V2PRO-CF-BLK."""
    scoped = BeautifulSoup(str(soup), "lxml")
    for sel in _RELATED_SECTION_SELECTORS:
        for tag in scoped.select(sel):
            tag.decompose()
    selectors = [
        ".price .amount",
        ".price ins .amount",
        ".product-price .price",
        ".product-price",
        ".current-price",
        ".woocommerce-Price-amount",
        "[class*='price'] .amount",
    ]
    for sel in selectors:
        el = scoped.select_one(sel)
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


def gia_san_markup_5pct(gia_nhap):
    """Quy tắc tối thượng (chốt 22/08/2026 với Vũ): giá bán KHÔNG ĐƯỢC dưới mức
    markup 5% trên giá vốn, bất kể giá đối thủ rẻ hơn bao nhiêu. Trả về giá bán
    sàn tối thiểu, làm tròn LÊN bội số 1.000đ cho đẹp giá. None nếu chưa có giá nhập."""
    if not gia_nhap:
        return None
    raw = gia_nhap * (1 + MARGIN_FLOOR_MARKUP)
    return int((round(raw) + 999) // 1000 * 1000)


def tinh_loi_nhuan_thuc(gia_ban, gia_nhap):
    """Công thức cập nhật 2026-08-22: Lợi nhuận thực = Giá bán - Giá nhập (anh không còn chịu phí ship, SHIP_FEE=0)."""
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


def extract_urls_and_labels(cell_value):
    """Cột M giờ có thể chứa NHIỀU link đối thủ cho cùng 1 SKU — cách nhau
    bởi dấu phẩy, xuống dòng, hoặc khoảng trắng. Vẫn hỗ trợ cả 2 dạng cũ:
    URL thô kèm ghi chú trong ngoặc, hoặc công thức =HYPERLINK("url","nhãn").
    Đọc bằng value_render_option FORMULA nên mọi dạng đều là string thô ở
    đây — bóc TẤT CẢ URL bằng regex chung, bỏ trùng lặp.
    Trả về list [(url, domain_label), ...] — rỗng nếu ô không có URL nào."""
    if not cell_value:
        return []
    text = str(cell_value)
    result = []
    seen = set()
    for raw in URL_RE.findall(text):
        url = raw.rstrip(').,;"')
        if not url or url in seen:
            continue
        seen.add(url)
        result.append((url, urlparse(url).netloc))
    return result


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
        "Giá đối thủ thấp nhất",
        "Tồn kho đối thủ",
        "Nguồn (giá thấp nhất)",
        "Số nguồn đã so sánh",
        "Giá đề xuất theo công thức",
        "Chênh lệch (hiện tại - đề xuất)",
        "Lợi nhuận thực",
        "Biên thực %",
        "Giá sàn markup 5% (giá vốn)",
        "Giá đề xuất CUỐI (đã áp sàn, không lỗ)",
        "Lỗ nếu theo giá đối thủ?",
        "Dưới sàn markup 5%?",
        "Cần tăng giá?",
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
                r["so_nguon_kiem_tra"],
                r["gia_de_xuat"] or "",
                r["chenh_lech"] if r["chenh_lech"] is not None else "",
                r["loi_nhuan_thuc"] if r["loi_nhuan_thuc"] is not None else "",
                r["bien_thuc"] if r["bien_thuc"] is not None else "",
                r["gia_san_5pct"] or "",
                r["gia_de_xuat_cuoi"] or "",
                "Có" if r["lo_neu_theo_doi_thu"] else "",
                "Có" if r["duoi_san_5pct"] else "",
                r["ly_do_tang_gia"] if r["can_tang_gia"] else "",
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


PRICE_INCREASE_TAB = "Đề xuất tăng giá (Auto)"


def write_price_increase_list(sh, results):
    """Tab hành động riêng (chốt 22/08/2026): CHỈ liệt kê SKU cần tăng giá bán —
    hoặc BẮT BUỘC (đang dưới sàn markup 5% trên giá vốn), hoặc CƠ HỘI (đối thủ
    đã tăng giá). Sắp BẮT BUỘC lên đầu (nghiêm trọng hơn), trong mỗi nhóm sắp
    theo mức tăng cần thiết giảm dần. Đây là danh sách Vũ duyệt rồi Claude dùng
    skill vutech-1-danh-gia-dinh-gia để sửa giá trên Haravan."""
    header = [
        "SKU",
        "Tên sản phẩm (mở nhanh Haravan)",
        "Giá nhập",
        "Giá bán hiện tại",
        "Giá đề xuất mới",
        "Mức cần tăng",
        "Biên hiện tại theo giá vốn %",
        "Lý do",
        "Nguồn giá đối thủ tham khảo",
    ]
    rows = [r for r in results if r["can_tang_gia"]]

    def sort_key(r):
        muc_tang = (r["gia_de_xuat_cuoi"] or r["gia_san_5pct"] or 0) - (r["gia_ban"] or 0)
        bat_buoc = 0 if r["duoi_san_5pct"] else 1  # bắt buộc (0) lên trước cơ hội (1)
        return (bat_buoc, -muc_tang)

    rows.sort(key=sort_key)

    body = []
    for r in rows:
        gia_moi = r["gia_de_xuat_cuoi"] or r["gia_san_5pct"]
        muc_tang = round(gia_moi - r["gia_ban"]) if (gia_moi is not None and r["gia_ban"] is not None) else ""
        bien_von = (
            round((r["gia_ban"] - r["gia_nhap"]) / r["gia_nhap"] * 100, 1)
            if r["gia_nhap"] and r["gia_ban"] is not None
            else ""
        )
        ten_haravan_url = f"https://vutech.myharavan.com/admin/products?query={r['ten']}"
        body.append(
            [
                r["sku"],
                f'=HYPERLINK("{ten_haravan_url}";"{r["ten"]}")',
                r["gia_nhap"] or "",
                r["gia_ban"] or "",
                gia_moi or "",
                muc_tang,
                bien_von,
                r["ly_do_tang_gia"],
                (f'=HYPERLINK("{r["url"]}";"{r["nguon"]}")' if r["url"] else (r["nguon"] or "")),
            ]
        )

    try:
        ws = sh.worksheet(PRICE_INCREASE_TAB)
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=PRICE_INCREASE_TAB, rows=max(len(body) + 10, 50), cols=len(header) + 2)

    ws.update([header] + body, value_input_option="USER_ENTERED")
    return ws, len(body)


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
    with_link = [r for r in results if r["so_nguon_kiem_tra"] > 0]
    danger = [r for r in results if "🔴" in r["co_canh_bao"]]
    warn = [r for r in results if "🟡" in r["co_canh_bao"] or "🟠" in r["co_canh_bao"]]
    err = [r for r in with_link if not r["trang_thai_lay_gia"].startswith("OK")]
    lo_neu_theo_doi_thu = [r for r in results if r["lo_neu_theo_doi_thu"]]
    duoi_san_5pct = [r for r in results if r["duoi_san_5pct"]]
    can_tang_gia = [r for r in results if r["can_tang_gia"]]

    rows_html = []
    for r in results:
        flag_class = "danger" if "🔴" in r["co_canh_bao"] else ("warn" if ("🟡" in r["co_canh_bao"] or "🟠" in r["co_canh_bao"]) else "ok")
        link_html = f'<a href="{r["url"]}" target="_blank" rel="noopener">{r["nguon"]}</a>' if r["url"] else "—"
        nguon_count = f' <span class="muted">({r["so_nguon_kiem_tra"]} nguồn)</span>' if r["so_nguon_kiem_tra"] > 1 else ""
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
              <td>{link_html}{nguon_count}</td>
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
  .muted {{ color:var(--muted); font-size:12px; }}
  tr.danger {{ background:rgba(255,92,108,0.07); }}
  tr.warn {{ background:rgba(245,192,74,0.06); }}
  a {{ color:var(--accent); }}
</style>
</head><body>
<h1>Vutech — Dashboard theo dõi giá đối thủ (Auto, 0 token)</h1>
<div class="sub">Cập nhật lúc {ts} (giờ VN) · Nguồn: Google Sheet "Khởi nghiệp" / tab Website · Mỗi SKU có thể theo dõi nhiều đối thủ, dashboard hiển thị giá THẤP NHẤT trong số đối thủ còn hàng · Script chạy tự động, không dùng AI</div>

<div class="stats">
  <div class="stat"><div class="n">{total}</div><div class="l">Tổng SKU</div></div>
  <div class="stat"><div class="n">{len(with_link)}</div><div class="l">Có link đối thủ</div></div>
  <div class="stat danger"><div class="n">{len(danger)}</div><div class="l">Cờ đỏ (lỗ / dưới vốn / mất cạnh tranh)</div></div>
  <div class="stat warn"><div class="n">{len(warn)}</div><div class="l">Cờ vàng (biên mỏng / cần xác minh)</div></div>
  <div class="stat"><div class="n">{len(err)}</div><div class="l">Lỗi lấy giá tự động</div></div>
  <div class="stat danger"><div class="n">{len(lo_neu_theo_doi_thu)}</div><div class="l">Lỗ nếu theo giá đối thủ</div></div>
  <div class="stat danger"><div class="n">{len(duoi_san_5pct)}</div><div class="l">Dưới sàn markup 5% (giá vốn)</div></div>
  <div class="stat warn"><div class="n">{len(can_tang_gia)}</div><div class="l">Cần tăng giá hôm nay</div></div>
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
  <th data-k="giadoithu" class="num">Giá đối thủ thấp nhất</th><th>Tồn kho đối thủ</th><th>Nguồn</th>
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
    candidates = extract_urls_and_labels(row["link_raw"])

    result = {
        "sku": row["sku"],
        "ten": row["ten"],
        "gia_nhap": row["gia_nhap"],
        "gia_ban": row["gia_ban"],
        "gia_doi_thu": None,
        "ton_kho_doi_thu": None,
        "nguon": None,
        "url": None,
        "gia_de_xuat": None,
        "chenh_lech": None,
        "loi_nhuan_thuc": None,
        "bien_thuc": None,
        "so_nguon_kiem_tra": len(candidates),
        "co_canh_bao": "",
        "trang_thai_lay_gia": "",
        "cap_nhat_luc": now_str,
        # Quy tắc tối thượng 22/08/2026: không được lỗ + markup tối thiểu 5% trên giá vốn.
        "gia_san_5pct": None,
        "duoi_san_5pct": False,
        "gia_de_xuat_cuoi": None,
        "lo_neu_theo_doi_thu": False,
        "can_tang_gia": False,
        "ly_do_tang_gia": "",
    }

    loi_nhuan, bien, flag_margin = tinh_loi_nhuan_thuc(row["gia_ban"], row["gia_nhap"])
    result["loi_nhuan_thuc"] = loi_nhuan
    result["bien_thuc"] = bien

    # Giá sàn markup 5% + cờ "đang dưới sàn" — tính được ngay cả khi SKU chưa có
    # link đối thủ nào, vì chỉ cần Giá nhập + Giá bán hiện tại.
    gia_san = gia_san_markup_5pct(row["gia_nhap"])
    result["gia_san_5pct"] = gia_san
    if gia_san is not None and row["gia_ban"] is not None:
        result["duoi_san_5pct"] = row["gia_ban"] < gia_san

    flags = []
    if flag_margin == "gia_ban_duoi_von":
        flags.append("🔴 GIÁ BÁN ≤ GIÁ VỐN — lỗi định giá nội bộ, ưu tiên xử lý trước")
    elif flag_margin == "danger":
        flags.append("🔴 lỗ sau ship")
    elif flag_margin == "warn":
        flags.append("🟡 biên mỏng (<10%)")
    elif flag_margin == "missing":
        flags.append("⚪ thiếu Giá nhập/Giá bán, chưa tính được")

    if result["duoi_san_5pct"]:
        flags.append("🔴 DƯỚI SÀN MARKUP 5% trên giá vốn — bắt buộc tăng giá bán theo quy tắc tối thượng")

    if not candidates:
        result["trang_thai_lay_gia"] = "bỏ qua — chưa có link đối thủ trong cột M"
    else:
        # Tải hết các đối thủ đang theo dõi cho SKU này, giữ lại từng kết quả
        # để chọn ra giá THẤP NHẤT trong số đối thủ đang CÒN HÀNG.
        scraped = []
        for url, label in candidates:
            r = fetch_competitor_price(url, session)
            r["url"] = url
            r["label"] = label
            scraped.append(r)
            time.sleep(REQUEST_DELAY)  # lịch sự với từng site, tránh bị chặn IP

        ok_results = [r for r in scraped if r["ok"]]
        in_stock = [r for r in ok_results if r.get("availability") == "còn hàng"]
        winner_pool = in_stock if in_stock else ok_results

        if winner_pool:
            winner = min(winner_pool, key=lambda r: r["price"])
            result["gia_doi_thu"] = winner["price"]
            result["ton_kho_doi_thu"] = winner.get("availability") or "không rõ"
            result["nguon"] = winner["label"]
            result["url"] = winner["url"]

            total = len(candidates)
            if in_stock:
                result["trang_thai_lay_gia"] = f"OK ({winner['source']}) — thấp nhất trong {len(in_stock)}/{total} nguồn còn hàng"
            else:
                result["trang_thai_lay_gia"] = f"OK ({winner['source']}) — {len(ok_results)}/{total} nguồn lấy được giá nhưng TẤT CẢ đều hết hàng"
                flags.append("⚪ tất cả đối thủ đang theo dõi đều HẾT HÀNG — giá lấy được có thể không phản ánh thị trường thực")

            if "cần xác minh" in winner["source"]:
                flags.append("🟠 nguồn giá độ tin cậy thấp (fallback selector) — nên xác minh tay")

            de_xuat = gia_de_xuat_tu_doi_thu(winner["price"])
            result["gia_de_xuat"] = de_xuat

            # Quy tắc tối thượng: giá đề xuất cuối cùng không bao giờ được thấp hơn
            # giá sàn markup 5% — nếu giá theo đối thủ thấp hơn sàn, ƯU TIÊN sàn,
            # chấp nhận mất lợi thế giá rẻ nhất để không bán lỗ/lãi quá mỏng.
            if gia_san is not None:
                result["gia_de_xuat_cuoi"] = max(de_xuat, gia_san)
            else:
                result["gia_de_xuat_cuoi"] = de_xuat

            if row["gia_nhap"] is not None:
                result["lo_neu_theo_doi_thu"] = de_xuat < row["gia_nhap"]
                if result["lo_neu_theo_doi_thu"]:
                    flags.append("🔴 LỖ nếu bán đúng giá đề xuất theo đối thủ — giá đối thủ đang thấp hơn cả giá vốn")

            if row["gia_ban"] is not None:
                result["chenh_lech"] = round(row["gia_ban"] - de_xuat)
                if row["gia_ban"] > winner["price"]:
                    flags.append(
                        f"🔴 Vutech đang CAO HƠN đối thủ rẻ nhất {int(row['gia_ban'] - winner['price']):,}đ — mất cạnh tranh".replace(",", ".")
                    )
                elif result["chenh_lech"] is not None and result["chenh_lech"] < -20_000:
                    flags.append("🟢 đối thủ đã tăng giá — Vutech có thể tăng giá bán theo công thức để tăng lợi nhuận")
        else:
            err_detail = "; ".join(f"{r['label'] or r['url']}: {r.get('error', '?')}" for r in scraped)
            result["trang_thai_lay_gia"] = f"LỖI: cả {len(candidates)} nguồn đều lỗi ({err_detail})"
            flags.append("⚠️ không lấy được giá tự động từ bất kỳ nguồn nào — cần Vũ kiểm tra tay")

    # "Cần tăng giá?" — hợp nhất 2 lý do dưới 1 cờ hành động duy nhất:
    # (1) BẮT BUỘC — giá bán hiện tại dưới sàn markup 5% trên giá vốn;
    # (2) CƠ HỘI — đối thủ đã tăng giá nên giá đề xuất cuối cùng cao hơn giá đang bán.
    target_price = result["gia_de_xuat_cuoi"] if result["gia_de_xuat_cuoi"] is not None else result["gia_san_5pct"]
    if target_price is not None and row["gia_ban"] is not None and row["gia_ban"] < target_price:
        result["can_tang_gia"] = True
        if result["duoi_san_5pct"]:
            result["ly_do_tang_gia"] = "🔴 BẮT BUỘC — giá bán hiện tại dưới sàn markup 5% trên giá vốn"
        else:
            result["ly_do_tang_gia"] = "🟢 Cơ hội — đối thủ đã tăng giá, có thể tăng giá bán mà vẫn cạnh tranh"

    result["co_canh_bao"] = "; ".join(flags)
    return result


def main():
    print(f"[{datetime.now(VN_TZ).isoformat()}] Bắt đầu check giá đối thủ Vutech...")
    client = get_gspread_client()
    sh = client.open_by_key(SHEET_ID)

    rows = read_source_rows(sh)
    print(f"Đọc được {len(rows)} SKU từ tab '{SOURCE_TAB}'.")

    with_link = [r for r in rows if extract_urls_and_labels(r["link_raw"])]
    total_candidates = sum(len(extract_urls_and_labels(r["link_raw"])) for r in with_link)
    print(f" → {len(with_link)} SKU có link đối thủ sẽ được refresh giá ({total_candidates} link đối thủ sẽ được kiểm tra).")
    print(f" → {len(rows) - len(with_link)} SKU chưa có link, chỉ tính lại lợi nhuận từ Giá nhập/Giá bán có sẵn.")

    session = requests.Session()
    results = []
    ok_count, err_count = 0, 0
    domain_stats = {}

    for idx, row in enumerate(rows, start=1):
        r = process_row(row, session)
        results.append(r)
        domain = r["nguon"] or "?"
        if r["trang_thai_lay_gia"].startswith("OK"):
            ok_count += 1
            bucket = domain_stats.setdefault(domain, {"ok": 0, "err": 0})
            bucket["ok"] += 1
        elif r["trang_thai_lay_gia"].startswith("LỖI"):
            err_count += 1
        if idx % 20 == 0:
            print(f" ...đã xử lý {idx}/{len(rows)}")

    print(f"\nKết quả: {ok_count} SKU lấy được giá / {err_count} SKU lỗi hết (trên {len(with_link)} SKU có link).")
    print("Nguồn thắng (giá thấp nhất) theo domain:")
    for domain, stat in sorted(domain_stats.items(), key=lambda x: -sum(x[1].values())):
        print(f"  {domain}: {stat['ok']} lần là nguồn giá thấp nhất")

    ws_out = write_output(sh, results)
    print(f"\nĐã ghi kết quả vào tab '{OUTPUT_TAB}' ({ws_out.url}).")

    ws_tang_gia, so_can_tang_gia = write_price_increase_list(sh, results)
    print(f"Đã ghi danh sách cần tăng giá vào tab '{PRICE_INCREASE_TAB}' ({ws_tang_gia.url}).")

    dashboard_path = generate_dashboard_html(results, os.path.join("docs", "index.html"))
    print(f"Đã xuất dashboard: {dashboard_path}")

    danger = [r for r in results if "🔴" in r["co_canh_bao"]]
    lo_neu_theo_doi_thu = [r for r in results if r["lo_neu_theo_doi_thu"]]
    duoi_san_5pct = [r for r in results if r["duoi_san_5pct"]]
    print(f"\n⚠️ {len(danger)} SKU có cờ đỏ cần chú ý.")
    print(f"🔴 QUY TẮC TỐI THƯỢNG: {len(lo_neu_theo_doi_thu)} SKU sẽ LỖ nếu bán đúng giá đề xuất theo đối thủ.")
    print(f"🔴 QUY TẮC TỐI THƯỢNG: {len(duoi_san_5pct)} SKU đang dưới sàn markup 5% trên giá vốn.")
    print(f"🟢 {so_can_tang_gia} SKU cần tăng giá hôm nay (bắt buộc + cơ hội) — xem tab '{PRICE_INCREASE_TAB}'.")

    return results


if __name__ == "__main__":
    main()
