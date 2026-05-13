"""
Stock Checker — single-file edition for GitHub Actions

학습된 사이트 (8 종):
  Nike US/JP, Uniqlo JP, Adidas US, Adidas JP, Swim2000,
  top4running.de, runningwarehouse.com, Rakuten (다중 패턴)

실행:
    python stock_checker.py --grades A,B --slot morning-AB

환경 변수:
    GOOGLE_CREDENTIALS   서비스 계정 JSON 전체 (또는 파일 경로)
    GOOGLE_SHEET_ID      스프레드시트 ID
    SLOT_NAME            run 라벨 (예: morning-A, weekly-C)
    SMTP_HOST            (선택) 이메일 알림용 SMTP 호스트
    SMTP_USER            (선택) 발신 계정
    SMTP_PASS            (선택) 발신 비밀번호 / 앱 비밀번호
    ALERT_TO             (선택) 수신자 이메일 (기본: lay5byur@gmail.com)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from playwright.sync_api import sync_playwright

KST = timezone(timedelta(hours=9))
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ──────────────────────────────────────────────────────────────────────
# 데이터 모델
# ──────────────────────────────────────────────────────────────────────

class S:
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    LOW_STOCK = "LOW_STOCK"


@dataclass
class SizeVariant:
    size: str
    status: str
    qty: int = 0


@dataclass
class ColorResult:
    color: str
    sizes: list[SizeVariant] = field(default_factory=list)
    price: Optional[float] = None
    currency: str = "USD"
    extra: dict = field(default_factory=dict)


@dataclass
class ProductResult:
    product_no: str
    url: str
    colors: list[ColorResult] = field(default_factory=list)
    error: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────
# 사이트별 추출 로직
# ──────────────────────────────────────────────────────────────────────

OOS_JP_PATTERNS = re.compile(r"売り切れ|在庫切れ|品切れ|再入荷|入荷待ち|入荷時に通知|SOLD\s*OUT", re.IGNORECASE)

# Uniqlo 색상 코드 매핑 — API 가 colorName 을 null 로 반환할 때 displayCode 로 사람-읽기 좋은 이름 매핑.
# 시즌마다 색상 코드 의미가 약간씩 바뀔 수 있으니 알 수 없는 코드는 "색상XX" 로 fallback.
UNIQLO_COLOR_MAP = {
    "00": "WHITE",      "01": "OFF WHITE",     "02": "NATURAL",
    "03": "BEIGE",      "04": "BROWN",         "05": "KHAKI",
    "06": "OLIVE",      "07": "GRAY",          "08": "DARK GRAY",
    "09": "BLACK",      "10": "PURPLE",        "11": "PINK",
    "12": "RED",        "13": "BORDEAUX",      "14": "ORANGE",
    "15": "ORANGE",     "16": "MUSTARD",       "17": "YELLOW",
    "20": "GREEN",      "22": "GREEN",         "23": "LIGHT GREEN",
    "24": "GREEN",      "26": "GREEN",         "27": "EMERALD GREEN",
    "30": "YELLOW",     "32": "DARK YELLOW",   "35": "GOLD",
    "40": "PEACH",      "50": "PURPLE",        "55": "WINE",
    "57": "WINE",       "58": "BURGUNDY",
    "60": "BLUE",       "61": "LIGHT BLUE",    "62": "SKY BLUE",
    "63": "DARK BLUE",  "64": "BLUE",          "65": "BLUE",
    "66": "BLUE",       "67": "LIGHT BLUE",    "68": "INDIGO BLUE",
    "69": "NAVY",       "70": "TURQUOISE",
}


def uniqlo_color_label(color_name: str | None, display_code: str) -> str:
    """API 의 colorName 우선, 없으면 매핑, 그래도 없으면 '색상XX'."""
    if color_name:
        return color_name
    code = (display_code or "").strip()
    if code in UNIQLO_COLOR_MAP:
        return UNIQLO_COLOR_MAP[code]
    return f"색상{code}" if code else "색상미상"


# Uniqlo 사이즈 코드 매핑 — API 의 sizeName 이 null/빈값일 때 SMA code → 사람-읽기 좋은 사이즈 라벨.
# Uniqlo 일본 사이트의 일반적인 성인 의류 사이즈 코드. 일부 상품은 sizeName 도 비어있을 수 있어 fallback 필수.
UNIQLO_SIZE_MAP = {
    "SMA001": "XXS", "SMA002": "XS",  "SMA003": "S",   "SMA004": "M",
    "SMA005": "L",   "SMA006": "XL",  "SMA007": "XXL", "SMA008": "3XL",
    "SMA009": "4XL", "SMA010": "5XL",
    # 일부 카테고리 (니트/스웨터 등) 는 다른 SMA 블록 사용
    # 다음은 V-neck 카디건 등 상품에서 관찰된 패턴:
    "SMA021": "XS",  "SMA022": "S",   "SMA023": "M",   "SMA024": "L",
    "SMA025": "XL",  "SMA026": "XXL", "SMA027": "3XL",
    # 여성 사이즈 블록 예시 (SMA0xx 의 다른 prefix 가 나오면 추가)
    "SMA101": "XS",  "SMA102": "S",   "SMA103": "M",   "SMA104": "L",
    "SMA105": "XL",
}


def uniqlo_size_label(api_name: str | None, size_code: str, display_code: str = "") -> str:
    """sizeName 우선 → SMA 매핑 → displayCode → raw code 순으로 fallback."""
    if api_name and api_name.strip():
        return api_name.strip()
    sc = (size_code or "").strip()
    if sc in UNIQLO_SIZE_MAP:
        return UNIQLO_SIZE_MAP[sc]
    dc = (display_code or "").strip()
    if dc:
        return f"사이즈{dc}"
    return sc or "사이즈?"


def fetch_nike(url: str, product_no: str, browser, *, config: dict) -> ProductResult:
    """Nike US/JP — __NEXT_DATA__ + JSON-LD ProductGroup."""
    r = ProductResult(product_no=product_no, url=url)
    ctx = browser.new_context(user_agent=DEFAULT_UA)
    try:
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(config.get("wait_ms", 6500))
        data = page.evaluate(
            """() => {
                const nd = document.getElementById('__NEXT_DATA__');
                const ld = document.querySelector('script[type="application/ld+json"]');
                return {next: nd?.textContent || null, ld: ld?.textContent || null};
            }"""
        )
        next_data = json.loads(data["next"]) if data["next"] else None
        ld_arr = json.loads(data["ld"]) if data["ld"] else []
        if not isinstance(ld_arr, list):
            ld_arr = [ld_arr]
        pg = next((o for o in ld_arr if isinstance(o, dict) and o.get("@type") == "ProductGroup"), None)
        if not next_data or not pg:
            r.error = "no __NEXT_DATA__ or ProductGroup"
            return r
        sel = next_data.get("props", {}).get("pageProps", {}).get("selectedProduct", {})
        all_sizes = [s.get("label") for s in sel.get("sizes", []) if s.get("label")]
        variants = [v for v in pg.get("hasVariant", []) if v.get("color")]
        in_stock_set = {
            v.get("size")
            for v in variants
            if "InStock" in (v.get("offers", {}).get("availability") or "")
        }
        color = (variants[0].get("color") if variants else None) or sel.get("colorDescription") or "Default"
        price = (variants[0].get("offers", {}) if variants else {}).get("price")
        currency = (variants[0].get("offers", {}) if variants else {}).get("priceCurrency", "USD")
        sizes = [
            SizeVariant(s, S.IN_STOCK if s in in_stock_set else S.OUT_OF_STOCK, 1 if s in in_stock_set else 0)
            for s in all_sizes
        ]
        r.colors.append(ColorResult(color=color, sizes=sizes, price=price, currency=currency or "USD"))
    except Exception as e:
        r.error = f"nike error: {e}"
    finally:
        ctx.close()
    return r


def fetch_uniqlo(url: str, product_no: str, browser, *, config: dict) -> ProductResult:
    """Uniqlo JP — public commerce v5 API."""
    r = ProductResult(product_no=product_no, url=url)
    m = re.search(r"/products/(E\d{6}-\d{3})", url)
    if not m:
        r.error = "could not extract Uniqlo product ID"
        return r
    pid = m.group(1)
    sess = requests.Session()
    sess.headers["User-Agent"] = DEFAULT_UA
    try:
        prod = sess.get(
            f"https://www.uniqlo.com/jp/api/commerce/v5/ja/products?productIds={pid}&httpFailure=true",
            timeout=20,
        ).json()
        stock = sess.get(
            f"https://www.uniqlo.com/jp/api/commerce/v5/ja/products/{pid}/price-groups/00/l2s"
            "?withPrices=true&withStocks=true&httpFailure=true",
            timeout=20,
        ).json()
    except Exception as e:
        r.error = f"uniqlo api error: {e}"
        return r
    item = (prod.get("result") or {}).get("items", [{}])[0]
    colors = item.get("colors") or []
    sizes_meta = item.get("sizes") or []
    plds = item.get("plds") or []
    l2s = (stock.get("result") or {}).get("l2s") or []
    stocks = (stock.get("result") or {}).get("stocks") or {}
    prices = (stock.get("result") or {}).get("prices") or {}

    def _color_label(color_code: str, display_code: str, pld_code: str) -> str:
        # 1. API 가 제공한 colorName 우선 사용
        cname = next((c.get("name") for c in colors if c.get("code") == color_code), None)
        # 2. None / 빈 문자열이면 displayCode 기반 매핑 fallback
        cname = uniqlo_color_label(cname, display_code)
        # 3. 길이(pld) 메타가 있을 때 색상명 뒤에 표기 (예: "BLUE (65)")
        if not plds or pld_code in ("PTB000", ""):
            return cname
        pname = next((p.get("name") for p in plds if p.get("code") == pld_code), None)
        return f"{cname} ({pname})" if pname else cname

    by_color: dict[str, ColorResult] = {}
    for l in l2s:
        l2id = l.get("l2Id")
        color_label = _color_label(
            l.get("color", {}).get("code"),
            l.get("color", {}).get("displayCode", ""),
            l.get("pld", {}).get("code", ""),
        )
        size_code = l.get("size", {}).get("code")
        size_display_code = l.get("size", {}).get("displayCode", "")
        # sizes_meta 에서 매칭되는 항목의 name 을 찾되, name 이 null/빈값일 가능성을 안전하게 처리
        matching_size = next((s for s in sizes_meta if s.get("code") == size_code), {})
        size_api_name = matching_size.get("name")
        size_meta_display = matching_size.get("displayCode") or size_display_code
        size_name = uniqlo_size_label(size_api_name, size_code, size_meta_display)
        stk = stocks.get(l2id, {})
        prc = prices.get(l2id, {})
        status = stk.get("statusCode") or "UNK"
        qty = stk.get("quantity") or 0
        mapped = S.IN_STOCK if status == "IN_STOCK" else (S.LOW_STOCK if status == "LOW_STOCK" else S.OUT_OF_STOCK)
        cr = by_color.setdefault(color_label, ColorResult(color=color_label, currency="JPY"))
        cr.sizes.append(SizeVariant(size_name, mapped, qty))
        base = (prc.get("base") or {}).get("value")
        if base and cr.price is None:
            cr.price = base
            cr.currency = (prc.get("base") or {}).get("currency", {}).get("code", "JPY")
    r.colors = list(by_color.values())
    return r


def fetch_adidas_us(url: str, product_no: str, browser, *, config: dict) -> ProductResult:
    """Adidas US — JSON-LD Product + DOM 'unavailable' class.

    한국 IP / Accept-Language 등에서 접속 시 adidas.co.kr 로 server-redirect 되어
    JSON-LD 의 `color` 가 비거나 사이즈 라벨이 'J/XS' 같은 일본 표기로 바뀌는
    문제가 있다. 다음 4단계로 강제 US 사이트 유지:
      1) en-US locale + America/New_York timezone + 뉴욕 geolocation 설정한 context
      2) Accept-Language / Referer / sec-ch-ua-* 등 US 브라우저 헤더 주입
      3) adidas.com US 도메인에 미리 region/locale 쿠키 심기
         (geoCountry=US, locale=en_US, akacd_*=US, geoip=US-…)
      4) `page.route('**/adidas.co.kr/**')` 로 .co.kr 호스트 네트워크 요청 abort
         + 최종 URL 이 여전히 adidas.com 인지 검증
    색상은 JSON-LD `color` (full name, e.g. 'Pure Tangerine / Pure Orange') →
    title 패턴 ('adidas <name> - <Color> | ...') → __NEXT_DATA__ 의
    `attribute_list.color` 순으로 fallback 한다.
    """
    r = ProductResult(product_no=product_no, url=url)
    ctx = browser.new_context(
        user_agent=DEFAULT_UA,
        locale="en-US",
        timezone_id="America/New_York",
        geolocation={"latitude": 40.7128, "longitude": -74.0060},
        permissions=["geolocation"],
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.adidas.com/us",
            "sec-ch-ua-platform": '"Windows"',
        },
    )
    # 1) 모든 adidas 도메인에 region/locale 강제 쿠키.
    ctx.add_cookies([
        {"name": "geoCountry",   "value": "US",     "domain": ".adidas.com", "path": "/"},
        {"name": "geoRedirect",  "value": "false",  "domain": ".adidas.com", "path": "/"},
        {"name": "country",      "value": "US",     "domain": ".adidas.com", "path": "/"},
        {"name": "locale",       "value": "en_US",  "domain": ".adidas.com", "path": "/"},
        {"name": "language",     "value": "en",     "domain": ".adidas.com", "path": "/"},
        {"name": "default_country", "value": "US",  "domain": ".adidas.com", "path": "/"},
    ])
    page = ctx.new_page()
    # 2) .co.kr / .jp 호스트로 가는 네트워크 요청은 전부 차단해 redirect 자체를 무력화.
    def _block_locale_redirect(route, request):
        host = urlparse(request.url).netloc.lower()
        if "adidas.co.kr" in host or "adidas.jp" in host:
            try:
                route.abort()
            except Exception:
                pass
        else:
            route.continue_()
    page.route("**/*", _block_locale_redirect)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # 사이즈 버튼이 렌더될 때까지 대기 — 안 그러면 size_btns=[] 로 잡혀
        # '정상' 으로 잘못 표기됨. 12초 안에 안 나오면 시도만 한 채 진행.
        try:
            page.wait_for_selector('[class*="size-selector_size__"]', timeout=12000)
        except Exception:
            pass
        page.wait_for_timeout(config.get("wait_ms", 5500))

        final_host = urlparse(page.url).netloc.lower()
        if "adidas.com" not in final_host or "co.kr" in final_host:
            r.error = f"adidas_us redirected to {final_host} despite block; check region cookies/IP"
            return r

        data = page.evaluate(
            """() => {
                const ld = document.querySelector('script[type="application/ld+json"]');
                const btns = Array.from(document.querySelectorAll('[class*="size-selector_size__"]'))
                    .map(b => ({text: (b.textContent||'').trim(), cls: String(b.className)}));
                // __NEXT_DATA__ 의 attribute_list.color (full color name) 와 search_col (short)
                let ndColor = null, ndSearchCol = null;
                try {
                    const nd = document.getElementById('__NEXT_DATA__');
                    if (nd) {
                        const data = JSON.parse(nd.textContent);
                        const q = data?.props?.pageProps?.dehydratedState?.queries?.[0];
                        const attrs = q?.state?.data?.attribute_list || {};
                        ndColor = attrs.color || null;
                        ndSearchCol = attrs.search_col || null;
                    }
                } catch (e) {}
                return {
                    ld: ld?.textContent || null,
                    sizes: btns,
                    title: document.title || '',
                    ndColor, ndSearchCol,
                };
            }"""
        )
        ld = json.loads(data["ld"]) if data["ld"] else {}

        # 색상 추출: JSON-LD → __NEXT_DATA__ → title 정규식 → 'Default'
        color = (
            (ld.get("color") if isinstance(ld, dict) else None)
            or data.get("ndColor")
            or _adidas_color_from_title(data.get("title") or "")
            or data.get("ndSearchCol")
            or "Default"
        )

        offers = ld.get("offers") or {} if isinstance(ld, dict) else {}
        price = offers.get("price")
        currency = offers.get("priceCurrency") or "USD"
        sizes: list[SizeVariant] = []
        for s in data["sizes"]:
            t = s.get("text", "").strip()
            if not re.match(r"^[A-Z0-9]{1,4}$", t):
                continue
            oos = "unavailable" in (s.get("cls") or "")
            sizes.append(SizeVariant(t, S.OUT_OF_STOCK if oos else S.IN_STOCK, 0 if oos else 1))
        r.colors.append(ColorResult(color=color, sizes=sizes, price=price, currency=currency))
    except Exception as e:
        r.error = f"adidas_us error: {e}"
    finally:
        ctx.close()
    return r


def _adidas_color_from_title(title: str) -> Optional[str]:
    """'adidas <product name> - <Color> | Free Shipping with adiClub' → '<Color>'.

    JSON-LD 가 없거나 region redirect 직후 색상을 비웠을 때 fallback.
    'adidas' / 'adidas Performance' 등 brand prefix 처리.
    """
    if not title:
        return None
    m = re.match(r"^\s*adidas(?:\s+\w+)?\s+.+?\s+-\s+(.+?)\s+\|\s*", title, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _adidas_jp_color_from_title(title: str) -> Optional[str]:
    """'<product> - <Color> | アディダス ジャパン' → '<Color>'.

    Adidas JP 의 title 는 '- ' 로 분리된 색상명이 포함된 패턴. JSON-LD 가 비거나
    색상이 truncated (e.g. 'Mint Ton') 일 때 fallback 으로 사용.
    """
    if not title:
        return None
    # 일본어 제목 패턴: "아디다스 <제품명> - <색상> | アディダス ジャパン"
    m = re.search(r"-\s*([^|\-]+?)\s*\|\s*アディダス", title)
    if m:
        return m.group(1).strip()
    # 영문 제목 패턴: "adidas <product> - <Color> | ..."
    m = re.search(r"-\s*([^|\-]+?)\s*\|", title)
    return m.group(1).strip() if m else None


def fetch_adidas_jp(url: str, product_no: str, browser, *, config: dict) -> ProductResult:
    """Adidas JP — JSON-LD ProductGroup for metadata + DOM 'unavailable' class for OOS.

    색상 추출 fallback 순서:
      1. 색상 변형 (hasVariant 의 `!v.offers` 항목) 의 첫 번째 `color` 값
      2. JSON-LD `pg.color` (있는 경우)
      3. title 의 ' - <Color> | ...' 패턴
      4. hasVariant[0].color (마지막 fallback)
      5. 'Default'

    사이즈 검출 전 `[class*="size-selector_size__"]` 가 실제로 렌더될 때까지
    `page.wait_for_selector` 로 대기 → 사이즈 0개로 잡혀 '정상' 표기 방지.
    """
    r = ProductResult(product_no=product_no, url=url)
    ctx = browser.new_context(user_agent=DEFAULT_UA, locale="ja-JP")
    try:
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_selector('[class*="size-selector_size__"]', timeout=12000)
        except Exception:
            pass  # 사이즈 셀렉터가 없는 상품도 있음 — 진행
        page.wait_for_timeout(config.get("wait_ms", 6000))
        data = page.evaluate(
            """() => {
                const lds = document.querySelectorAll('script[type="application/ld+json"]');
                let pg = null;
                lds.forEach(s => { try { const j = JSON.parse(s.textContent); if (j && j['@type'] === 'ProductGroup') pg = j; } catch(e) {} });
                const sizes = Array.from(document.querySelectorAll('[class*="size-selector_size__"]'))
                    .filter(b => { const t = (b.textContent||'').trim(); return t.length > 0 && t.length <= 8 && /\\d|[A-Z]/.test(t); })
                    .map(b => ({size: b.textContent.trim(), cls: String(b.className)}));
                return {pg, sizes, title: document.title || ''};
            }"""
        )
        pg = data.get("pg") or {}
        size_variants = [v for v in (pg.get("hasVariant") or []) if v.get("offers")]
        color_variants = [v for v in (pg.get("hasVariant") or []) if not v.get("offers")]
        price = (size_variants[0].get("offers", {}) if size_variants else {}).get("price")
        currency = (size_variants[0].get("offers", {}) if size_variants else {}).get("priceCurrency") or "JPY"
        # 색상 — 다단계 fallback
        color = (
            (color_variants[0].get("color") if color_variants else None)
            or pg.get("color")
            or _adidas_jp_color_from_title(data.get("title") or "")
            or (pg.get("hasVariant", [{}])[0].get("color") if pg.get("hasVariant") else None)
            or "Default"
        )
        sizes = []
        for s in data["sizes"]:
            oos = "unavailable" in (s.get("cls") or "")
            sizes.append(SizeVariant(s["size"], S.OUT_OF_STOCK if oos else S.IN_STOCK, 0 if oos else 1))
        r.colors.append(ColorResult(color=color, sizes=sizes, price=price, currency=currency))
    except Exception as e:
        r.error = f"adidas_jp error: {e}"
    finally:
        ctx.close()
    return r


def fetch_swim2000(url: str, product_no: str, browser, *, config: dict) -> ProductResult:
    """Swim2000 — Shopify /products/<handle>.js endpoint."""
    r = ProductResult(product_no=product_no, url=url)
    m = re.search(r"/products/([^/?#]+)", url)
    if not m:
        r.error = "no handle"
        return r
    handle = m.group(1)
    sess = requests.Session()
    sess.headers["User-Agent"] = DEFAULT_UA
    try:
        data = sess.get(f"https://www.swim2000.com/products/{handle}.js", timeout=20).json()
    except Exception as e:
        r.error = f"swim2000 error: {e}"
        return r
    by_color: dict[str, ColorResult] = {}
    for v in data.get("variants", []):
        color = v.get("option1") or "Default"
        size = v.get("option2") or "OS"
        avail = bool(v.get("available"))
        cr = by_color.setdefault(color, ColorResult(color=color, currency="USD"))
        cr.sizes.append(SizeVariant(size, S.IN_STOCK if avail else S.OUT_OF_STOCK, 1 if avail else 0))
        if cr.price is None and v.get("price") is not None:
            cr.price = v["price"] / 100.0
    r.colors = list(by_color.values())
    return r


def fetch_top4running(url: str, product_no: str, browser, *, config: dict) -> ProductResult:
    """top4running.de — JSON-LD ProductGroup, size from offers.url ?size= param."""
    r = ProductResult(product_no=product_no, url=url)
    ctx = browser.new_context(user_agent=DEFAULT_UA, locale="de-DE")
    try:
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(config.get("wait_ms", 4000))
        ld_text = page.evaluate(
            """() => document.querySelector('script[type="application/ld+json"]')?.textContent || null"""
        )
    except Exception as e:
        r.error = f"top4running error: {e}"
        ctx.close()
        return r
    finally:
        try: ctx.close()
        except: pass
    if not ld_text:
        r.error = "no JSON-LD"
        return r
    try:
        ld = json.loads(ld_text)
    except Exception as e:
        r.error = f"top4running JSON parse: {e}"
        return r
    arr = ld if isinstance(ld, list) else [ld]
    pg = next((o for o in arr if isinstance(o, dict) and o.get("@type") == "ProductGroup"), None)
    if not pg or not pg.get("hasVariant"):
        r.error = "no ProductGroup"
        return r
    color = pg.get("color") or "Default"
    sizes: list[SizeVariant] = []
    price = None
    currency = "EUR"
    for v in pg["hasVariant"]:
        offers = v.get("offers") or {}
        size = ""
        try:
            size = (parse_qs(urlparse(offers.get("url") or "").query).get("size", [""])[0]).upper()
        except Exception:
            pass
        is_in = "InStock" in (offers.get("availability") or "")
        sizes.append(SizeVariant(size, S.IN_STOCK if is_in else S.OUT_OF_STOCK, 1 if is_in else 0))
        if is_in and price is None:
            try: price = float(offers.get("price"))
            except: pass
            currency = offers.get("priceCurrency") or "EUR"
    if price is None:
        try: price = float(pg["hasVariant"][0].get("offers", {}).get("price"))
        except: price = None
    r.colors.append(ColorResult(color=color, sizes=sizes, price=price, currency=currency))
    return r


def fetch_runningwarehouse(url: str, product_no: str, browser, *, config: dict) -> ProductResult:
    """runningwarehouse.com — schema.org/Offer microdata (only IN_STOCK sizes are listed)."""
    r = ProductResult(product_no=product_no, url=url)
    ctx = browser.new_context(user_agent=DEFAULT_UA, locale="en-US")
    try:
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(config.get("wait_ms", 4500))
        data = page.evaluate(
            """() => {
                const color = document.querySelector('[class*="style_ordering-image_wrap"] img')?.alt;
                const offers = document.querySelectorAll('[itemtype*="Offer"]');
                const sizes = Array.from(offers).map(o => {
                    const props = {};
                    o.querySelectorAll('[itemprop]').forEach(p => {
                        props[p.getAttribute('itemprop')] = p.tagName === 'META' ? p.content : (p.textContent || '').trim();
                    });
                    return props;
                });
                return {color, sizes};
            }"""
        )
    except Exception as e:
        r.error = f"runningwarehouse error: {e}"
        ctx.close()
        return r
    finally:
        try: ctx.close()
        except: pass

    color = data.get("color") or "Default"
    sizes_data = data.get("sizes") or []
    sizes: list[SizeVariant] = []
    price = None
    currency = "USD"
    for s in sizes_data:
        offered = (s.get("itemOffered") or "").strip()
        m = re.search(r"\b(XXS|XS|S|M|L|XL|XXL|XXXL|2XL|3XL|4XL|\d{1,2}(?:\.\d)?)\b", offered)
        size = m.group(1) if m else "UNKNOWN"
        try:
            p = float(s.get("price"))
            if price is None:
                price = p
        except Exception:
            pass
        currency = s.get("priceCurrency") or currency
        sizes.append(SizeVariant(size, S.IN_STOCK, 1))
    cr = ColorResult(color=color, sizes=sizes, price=price, currency=currency)
    cr.extra["only_in_stock_visible"] = True
    r.colors.append(cr)
    return r


def fetch_rakuten(url: str, product_no: str, browser, *, config: dict) -> ProductResult:
    """Rakuten unified — grid buttons with various OOS markers."""
    r = ProductResult(product_no=product_no, url=url)
    ctx = browser.new_context(user_agent=DEFAULT_UA, locale="ja-JP")
    try:
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(config.get("wait_ms", 5500))
        data = page.evaluate(
            """() => {
                const grids = Array.from(document.querySelectorAll('[class*="grid-cols-"]'));
                const grid = grids.find(g => {
                    const btns = g.querySelectorAll('button');
                    return btns.length >= 2 && Array.from(btns).some(b => /円/.test((b.textContent||'')));
                });
                if (!grid) return {err: 'no variant grid'};
                const scripts = Array.from(document.querySelectorAll('script:not([src])'));
                const inv = scripts.find(s => /variantSelectors/.test(s.textContent));
                const km = inv ? inv.textContent.match(/"variantSelectors":\\[\\{"key":"([^"]+)"/) : null;
                const variantKey = km ? km[1] : 'variant';
                const OOS = /売り切れ|在庫切れ|品切れ|再入荷|入荷待ち|入荷時に通知|SOLD\\s*OUT/i;
                const variants = Array.from(grid.querySelectorAll('button')).map(b => {
                    const txt = (b.textContent || '').trim();
                    const label = txt.split(/[\\d,]+円/)[0].trim();
                    const pm = txt.match(/([\\d,]+)円/);
                    const price = pm ? parseInt(pm[1].replace(/,/g, '')) : null;
                    const m = txt.match(OOS);
                    return {label, price, status: m ? 'OUT_OF_STOCK' : 'IN_STOCK', marker: m ? m[0] : null};
                });
                return {variantKey, variants, title: document.title};
            }"""
        )
    except Exception as e:
        r.error = f"rakuten error: {e}"
        ctx.close()
        return r
    finally:
        try: ctx.close()
        except: pass
    if data.get("err"):
        r.error = data["err"]
        return r
    variant_key = data.get("variantKey", "variant")
    variants = data.get("variants", [])
    if variant_key in ("サイズ", "size", "Size"):
        m = re.search(r"FN\d+\s*(\d{3})", data.get("title") or "") or re.search(r"-(\d{3})/?$", url)
        color = m.group(1) if m else "Default"
        sizes = [SizeVariant(v["label"], v["status"], 1 if v["status"] == S.IN_STOCK else 0) for v in variants]
        price = next((v["price"] for v in variants if v["price"]), None)
        r.colors.append(ColorResult(color=color, sizes=sizes, price=price, currency="JPY"))
    else:
        for v in variants:
            cr = ColorResult(
                color=v["label"],
                sizes=[SizeVariant("OS", v["status"], 1 if v["status"] == S.IN_STOCK else 0)],
                price=v.get("price"),
                currency="JPY",
            )
            cr.extra["marker"] = v.get("marker")
            r.colors.append(cr)
    return r


SITE_FETCHERS: dict[str, callable] = {
    "nike.com": fetch_nike,
    "uniqlo.com": fetch_uniqlo,
    "adidas.com": fetch_adidas_us,
    "adidas.jp": fetch_adidas_jp,
    "swim2000.com": fetch_swim2000,
    "top4running.de": fetch_top4running,
    "runningwarehouse.com": fetch_runningwarehouse,
    "rakuten.co.jp": fetch_rakuten,
}


def pick_fetcher(url: str):
    host = urlparse(url).netloc.lower().replace("www.", "")
    for key, fn in SITE_FETCHERS.items():
        if host == key or host.endswith("." + key) or host.endswith(key):
            return fn, key
    return None, host


# ──────────────────────────────────────────────────────────────────────
# 가격 포맷 + 분류 로직
# ──────────────────────────────────────────────────────────────────────

def format_price(price: Optional[float], currency: str = "USD") -> str:
    if price is None:
        return ""
    currency = (currency or "USD").upper()
    if currency == "USD":
        return f"${int(price)}" if float(price) == int(price) else f"${price:.2f}"
    if currency == "JPY":
        return f"¥{int(round(float(price))):,}"
    if currency == "EUR":
        s = f"€{float(price):.2f}".rstrip("0").rstrip(".")
        return s if s != "€" else "€0"
    if currency == "KRW":
        return f"₩{int(round(float(price))):,}"
    return f"{currency} {price:.2f}"


def parse_price_str(s: str):
    if not s: return None
    s = s.strip()
    for sym in ("$", "¥", "€", "₩"):
        if sym in s:
            try:
                return float(s.replace(sym, "").replace(",", "").strip())
            except ValueError:
                return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def parse_prev_oos(prev_d: str) -> set[str]:
    if not prev_d:
        return set()
    result = set()
    parts = re.split(r"/", prev_d)
    for part in parts:
        if "품절" not in part and "OOS" not in part.upper() and "재고적음" not in part:
            continue
        for t in re.findall(r"\b(?:J/)?(?:XXS|XS|S|M|L|XL|XXL|XXXL|2XL|3XL|4XL|\d{1,3}(?:\.\d)?(?:cm)?)\b", part):
            result.add(t)
    return result


COLOR_NEW_OOS = "#F4CCCC"
COLOR_RESTOCK = "#D9EAD3"
COLOR_CONT_OOS = "#FCE5CD"
COLOR_LOW = "#FFF2CC"
COLOR_PRICE_DOWN = "#CFE2F3"
COLOR_PRICE_UP = "#F4CCCC"


def classify_stock(prev_d: str, sizes: list[SizeVariant]):
    prev_oos = parse_prev_oos(prev_d)
    today_oos = {s.size for s in sizes if s.status == S.OUT_OF_STOCK}
    today_in = {s.size for s in sizes if s.status == S.IN_STOCK}
    today_low = [s for s in sizes if s.status == S.LOW_STOCK or (s.status == S.IN_STOCK and 0 < s.qty <= 3)]
    new_oos = sorted(today_oos - prev_oos)
    cont_oos = sorted(today_oos & prev_oos)
    restocked = sorted(prev_oos & today_in)
    low_sizes = sorted({s.size for s in today_low if s.size not in today_oos})

    parts, color = [], None
    if new_oos:
        parts.append(f"🔴 {', '.join(new_oos)} 신규 품절"); color = COLOR_NEW_OOS
    if restocked:
        parts.append(f"🎉 {', '.join(restocked)} 재입고"); color = color or COLOR_RESTOCK
    if cont_oos:
        parts.append(f"🟠 {', '.join(cont_oos)} 계속 품절"); color = color or COLOR_CONT_OOS
    if low_sizes:
        qmap = {s.size: s.qty for s in today_low}
        parts.append(f"⚠️ [재고적음] " + ", ".join(f"{s} (qty={qmap.get(s, 0)})" for s in low_sizes))
        color = color or COLOR_LOW
    if not parts:
        return "✅ 정상", None, [], []
    return " / ".join(parts), color, new_oos, restocked


def classify_price(prev_e: str, current_price: Optional[float], currency: str):
    if current_price is None:
        return "", None, None
    current_text = format_price(current_price, currency)
    if not prev_e or not prev_e.strip():
        return current_text, None, None
    arrow = re.search(r"→\s*(.+?)\s*\(", prev_e)
    prev_str = arrow.group(1) if arrow else re.sub(r"[💰💸⬆️⬇️]", "", prev_e).strip()
    prev_val = parse_price_str(prev_str)
    if prev_val is None or prev_val == 0:
        return current_text, None, None
    pct = (current_price - prev_val) / prev_val * 100.0
    if abs(pct) < 2.0:
        return current_text, None, pct
    prev_text = format_price(prev_val, currency)
    if pct <= -2:
        return f"💰 {prev_text} → {current_text} ({round(pct):+d}% ⬇️)", COLOR_PRICE_DOWN, pct
    return f"💸 {prev_text} → {current_text} ({round(pct):+d}% ⬆️)", COLOR_PRICE_UP, pct


# ──────────────────────────────────────────────────────────────────────
# Google Sheets
# ──────────────────────────────────────────────────────────────────────

class Sheets:
    def __init__(self, spreadsheet_id: str, credentials_json_or_path: str):
        creds_raw = credentials_json_or_path
        if creds_raw.strip().startswith("{"):
            info = json.loads(creds_raw)
        else:
            with open(creds_raw, "r", encoding="utf-8") as f:
                info = json.load(f)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        self.svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.ss = self.svc.spreadsheets()
        self.sid = spreadsheet_id
        self._gid: dict[str, int] = {}

    def read(self, range_a1: str) -> list[list[str]]:
        resp = self.ss.values().get(spreadsheetId=self.sid, range=range_a1).execute()
        return resp.get("values", []) or []

    def batch_update_values(self, updates: list[dict]):
        if not updates:
            return
        self.ss.values().batchUpdate(
            spreadsheetId=self.sid,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()

    def append(self, tab: str, rows: list[list]):
        if not rows:
            return
        self.ss.values().append(
            spreadsheetId=self.sid,
            range=f"{tab}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()

    def sheet_gid(self, tab: str) -> int:
        if tab in self._gid:
            return self._gid[tab]
        meta = self.ss.get(spreadsheetId=self.sid).execute()
        for sh in meta.get("sheets", []):
            props = sh.get("properties", {})
            if props.get("title") == tab:
                self._gid[tab] = props.get("sheetId")
                return self._gid[tab]
        raise RuntimeError(f"sheet not found: {tab}")

    def batch_backgrounds(self, tab: str, cells: list[tuple[int, str, Optional[str]]]):
        if not cells:
            return
        gid = self.sheet_gid(tab)
        reqs = []
        for row, col_letter, hex_color in cells:
            col_idx = _col_idx(col_letter)
            if hex_color:
                h = hex_color.lstrip("#")
                rgb = {"red": int(h[0:2], 16)/255, "green": int(h[2:4], 16)/255, "blue": int(h[4:6], 16)/255}
            else:
                rgb = {"red": 1, "green": 1, "blue": 1}
            reqs.append({
                "updateCells": {
                    "range": {
                        "sheetId": gid, "startRowIndex": row-1, "endRowIndex": row,
                        "startColumnIndex": col_idx, "endColumnIndex": col_idx+1,
                    },
                    "rows": [{"values": [{"userEnteredFormat": {"backgroundColor": rgb}}]}],
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })
        self.ss.batchUpdate(spreadsheetId=self.sid, body={"requests": reqs}).execute()


def _col_idx(letter: str) -> int:
    n = 0
    for ch in letter.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


# ──────────────────────────────────────────────────────────────────────
# 이메일 알림
# ──────────────────────────────────────────────────────────────────────

def send_email(subject: str, body: str):
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    to = os.environ.get("ALERT_TO", "lay5byur@gmail.com")
    if not (host and user and pw):
        print("[email] SMTP not configured; skipping notification", flush=True)
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Stock Checker", user))
    msg["To"] = to
    try:
        with smtplib.SMTP_SSL(host, 465) as s:
            s.login(user, pw)
            s.send_message(msg)
        print(f"[email] sent to {to}", flush=True)
    except Exception as e:
        print(f"[email] error: {e}", flush=True)


# ──────────────────────────────────────────────────────────────────────
# 메인 실행 흐름
# ──────────────────────────────────────────────────────────────────────

def kst_now() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def normalize(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--grades", default="A,B")
    parser.add_argument("--slot", default=os.environ.get("SLOT_NAME", "manual"))
    parser.add_argument("--config", default="site_config.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    grades = {g.strip().upper() for g in args.grades.split(",") if g.strip()}
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    creds = os.environ.get("GOOGLE_CREDENTIALS")
    if not sheet_id or not creds:
        print("ERROR: GOOGLE_CREDENTIALS and GOOGLE_SHEET_ID env vars are required", file=sys.stderr)
        sys.exit(2)

    # site_config.json (선택사항 — 없으면 빈 dict)
    site_cfg: dict = {}
    if os.path.exists(args.config):
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                site_cfg = json.load(f).get("sites", {})
        except Exception as e:
            print(f"[warn] failed to load {args.config}: {e}", flush=True)

    print(f"[stock-checker] grades={sorted(grades)} slot={args.slot}", flush=True)
    sheets = Sheets(sheet_id, creds)

    # 1. 상품 리스트 읽기 (등급 필터)
    rows = sheets.read("상품 리스트!A2:H")
    products = []
    for r in rows:
        if not r or not r[0]: continue
        no = str(r[0]).strip()
        grade = (r[4] if len(r) > 4 else "").strip()
        if grade not in grades: continue
        products.append({
            "no": no,
            "name": (r[1] if len(r) > 1 else "").strip(),
            "url": (r[2] if len(r) > 2 else "").strip(),
            "grade": grade,
        })
    print(f"[stock-checker] {len(products)} products to check", flush=True)

    # 2. 전체 현황 현재 상태 인덱스
    status_rows = sheets.read("전체 현황!A2:G")
    status_idx: dict[tuple[str, str], dict] = {}
    for i, row in enumerate(status_rows, start=2):
        if not row or not row[0]: continue
        key = (str(row[0]).strip(), normalize(row[2] if len(row) > 2 else ""))
        status_idx[key] = {
            "row": i, "no": str(row[0]).strip(),
            "color": (row[2] if len(row) > 2 else "").strip(),
            "d_text": (row[3] if len(row) > 3 else "").strip(),
            "e_text": (row[4] if len(row) > 4 else "").strip(),
        }

    # 3. 사이트별 fetch
    value_updates: list[dict] = []
    bg_updates: list[tuple[int, str, Optional[str]]] = []
    new_rows: list[list] = []
    new_oos_events: list[dict] = []
    price_events: list[dict] = []
    counts = {"checked": 0, "new_oos": 0, "restocked": 0, "low": 0, "price_up": 0, "price_down": 0, "errors": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-gpu", "--no-sandbox"])
        try:
            for prod in products:
                try:
                    fn, host = pick_fetcher(prod["url"])
                    if fn is None:
                        print(f"  [#{prod['no']}] no fetcher for host: {host}", flush=True)
                        counts["errors"] += 1
                        continue
                    site_key = next((k for k in site_cfg if k in host), host)
                    cfg = site_cfg.get(site_key, {})
                    print(f"  [#{prod['no']}] {prod['name'][:40]} ({prod['grade']}) {host}", flush=True)
                    res = fn(prod["url"], prod["no"], browser, config=cfg)
                    counts["checked"] += 1
                    if res.error:
                        print(f"    error: {res.error}", flush=True)
                        counts["errors"] += 1
                        continue
                    ts = kst_now()
                    for cr in res.colors:
                        key = (prod["no"], normalize(cr.color))
                        existing = status_idx.get(key)
                        if existing is None:
                            for (no, ck), row in status_idx.items():
                                if no == prod["no"] and normalize(ck) == normalize(cr.color):
                                    existing = row; break
                        d_text, d_color, new_oos, restocked = classify_stock(
                            existing["d_text"] if existing else "", cr.sizes
                        )
                        e_text, e_color, pct = classify_price(
                            existing["e_text"] if existing else "", cr.price, cr.currency
                        )
                        if new_oos:
                            counts["new_oos"] += 1
                            new_oos_events.append({"no": prod["no"], "name": prod["name"], "color": cr.color, "sizes": new_oos})
                        if restocked:
                            counts["restocked"] += 1
                        if any(s.status == S.LOW_STOCK or (s.status == S.IN_STOCK and 0 < s.qty <= 3) for s in cr.sizes):
                            counts["low"] += 1
                        if pct is not None and abs(pct) >= 2:
                            if pct > 0: counts["price_up"] += 1
                            else: counts["price_down"] += 1
                            price_events.append({
                                "no": prod["no"], "name": prod["name"], "color": cr.color,
                                "from": existing["e_text"] if existing else "", "to": e_text, "pct": pct
                            })
                        if existing:
                            value_updates.extend([
                                {"range": f"전체 현황!D{existing['row']}", "values": [[d_text]]},
                                {"range": f"전체 현황!E{existing['row']}", "values": [[e_text]]},
                                {"range": f"전체 현황!G{existing['row']}", "values": [[ts]]},
                            ])
                            bg_updates.append((existing['row'], "D", d_color))
                            bg_updates.append((existing['row'], "E", e_color))
                        else:
                            new_rows.append([prod["no"], prod["name"], cr.color, d_text, e_text, "", ts])
                except Exception as e:
                    counts["errors"] += 1
                    print(f"    EXCEPTION: {e}\n{traceback.format_exc()}", flush=True)
        finally:
            browser.close()

    # 4. 시트 일괄 쓰기
    if args.dry_run:
        print(f"[dry-run] would write: {len(value_updates)} updates, {len(new_rows)} new rows, {len(bg_updates)} bg colors", flush=True)
    else:
        sheets.batch_update_values(value_updates)
        sheets.batch_backgrounds("전체 현황", bg_updates)
        if new_rows:
            sheets.append("전체 현황", new_rows)

        # 5. 변동 알림 탭 append
        link = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        alert_rows = []
        for ev in new_oos_events:
            alert_rows.append([kst_now(), f"#{ev['no']} {ev['name']}", ev['color'], "STOCK",
                              ", ".join(ev['sizes']), link])
        for ev in price_events:
            alert_rows.append([kst_now(), f"#{ev['no']} {ev['name']}", ev['color'], "PRICE",
                              f"{ev['from']} -> {ev['to']} ({ev['pct']:+.1f}%)", link])
        if alert_rows:
            sheets.append("변동 알림", alert_rows)

        # 6. 사용량 모니터링 append
        note = (f"{kst_now()} | slot={args.slot} | 체크={counts['checked']} "
                f"🔴={counts['new_oos']} 🎉={counts['restocked']} ⚠️={counts['low']} "
                f"💸={counts['price_up']} 💰={counts['price_down']} err={counts['errors']}")
        sheets.append("사용량 모니터링", [[f"run-{args.slot}", "", counts["checked"], "-", "-", "-", note]])

        # 7. 이메일 알림
        if new_oos_events or price_events:
            subject = f"[재고/가격 알림] 변동 {len(new_oos_events) + len(price_events)}건 (slot={args.slot})"
            lines = []
            if new_oos_events:
                lines.append(f"📦 재고 변동 {len(new_oos_events)}건")
                for ev in new_oos_events:
                    lines.append(f"  - #{ev['no']} {ev['name']} / {ev['color']} / 신규 OOS: {', '.join(ev['sizes'])}")
            if price_events:
                lines.append(f"\n💱 가격 변동 {len(price_events)}건")
                for ev in price_events:
                    lines.append(f"  - #{ev['no']} {ev['name']} / {ev['color']} / {ev['from']} → {ev['to']} ({ev['pct']:+.1f}%)")
            lines.append(f"\n시트: {link}")
            send_email(subject, "\n".join(lines))

    print(f"[stock-checker] done counts={counts}", flush=True)


if __name__ == "__main__":
    main()
