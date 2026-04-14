"""
Amazon Product Scraper — No API Required
=========================================
Strategy (4 layers):
  Layer 1 → HTTP + cookie pre-warming (fast, ~1-2s)
  Layer 2 → Anti-Captcha solve if blocked (~15-30s)
  Layer 3 → Playwright stealth browser fallback (~5-10s)
  Layer 4 → Retry + exponential backoff throughout

Anti-bot handling:
  - Session cookie harvesting via homepage pre-warm
  - Header pool rotation with realistic Sec-Fetch headers
  - Human-like random delays
  - Anti-Captcha.com integration for image CAPTCHAs
  - Playwright stealth: navigator.webdriver spoofed, plugin list faked
"""

import os
import sys
import random
import time
import re
import json
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Optional

import urllib.parse
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load env from this package directory so API keys work even when uvicorn is started
# from the repo root (cwd would not find amazon_scraper/.env otherwise).
_APP_DIR = Path(__file__).resolve().parent
load_dotenv(_APP_DIR / ".env")
load_dotenv()

from captcha_solver import ANTI_CAPTCHA_KEY, AmazonCaptchaHandler

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ScraperAPI — Layer 0 (most reliable; handles all anti-bot automatically)
# ---------------------------------------------------------------------------
# Support both env var names:
# - SCRAPER_API_KEY  (preferred in this codebase)
# - SCRAPERAPI_KEY   (legacy / used by some local scripts)
SCRAPER_API_KEY: str = (
    os.getenv("SCRAPERAPI_KEY")
    or os.getenv("SCRAPER_API_KEY")
    or ""
).strip()
APIFY_API_TOKEN: str = os.getenv("APIFY_API_TOKEN", "").strip()

def _build_scraperapi_url(
    target_url: str,
    render_js: bool = False,
    country_code: str = "us",
    premium: bool = False,
) -> str:
    """
    Wrap a target URL with ScraperAPI proxy.

    render_js=False  → raw HTML, ~2-3s   (Amazon, most static sites)
    render_js=True   → JS-executed HTML, ~8-15s  (Zara, SHEIN, SPAs)
    premium=True     → use premium residential IPs (higher success on tough sites)
    country_code     → "us", "in", "gb", "de" etc.  (match site's target region)
    """
    params: dict = {
        "api_key": SCRAPER_API_KEY,
        "url": target_url,
        "country_code": country_code,
    }
    if render_js:
        params["render"] = "true"
    if premium:
        params["premium"] = "true"
    return "http://api.scraperapi.com?" + urllib.parse.urlencode(params)

# ---------------------------------------------------------------------------
# Header pool — rotate to mimic real browsers
# ---------------------------------------------------------------------------
HEADERS_POOL = [
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Referer": "https://www.google.com/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-GB,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.google.com/",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
            "Gecko/20100101 Firefox/124.0"
        ),
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Referer": "https://www.google.com/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Referer": "https://www.amazon.com/",
        "Connection": "keep-alive",
    },
]

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class ProductData:
    title: Optional[str] = None
    price: Optional[str] = None
    currency: Optional[str] = None
    main_image: Optional[str] = None
    variants: list = field(default_factory=list)
    asin: Optional[str] = None
    rating: Optional[str] = None
    review_count: Optional[str] = None
    availability: Optional[str] = None
    source: str = "http"
    success: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "asin": self.asin,
            "title": self.title,
            "price": self.price,
            "currency": self.currency,
            "main_image": self.main_image,
            "variants": self.variants,
            "rating": self.rating,
            "review_count": self.review_count,
            "availability": self.availability,
            "extraction_method": self.source,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def extract_asin(url: str) -> Optional[str]:
    """
    Parse ASIN from any Amazon product URL format.
    Supports: /dp/, /product/, /gp/product/, /gp/aw/d/, /exec/obidos/ASIN/,
              ?asin= / &ASIN=, and bare 10-char ASIN at end of path segment.
    Matching is case-insensitive; returned ASIN is always uppercased.
    """
    patterns = [
        r"/dp/([A-Z0-9]{10})(?:[/?#&]|$)",
        r"/product/([A-Z0-9]{10})(?:[/?#&]|$)",
        r"/gp/product/([A-Z0-9]{10})(?:[/?#&]|$)",
        r"/gp/aw/d/([A-Z0-9]{10})(?:[/?#&]|$)",          # Mobile / app URL
        r"/exec/obidos/ASIN/([A-Z0-9]{10})(?:[/?#&]|$)", # Legacy URL format
        r"[?&](?:asin|ASIN)=([A-Z0-9]{10})(?:[&]|$)",    # Query-string ASIN
        # Broader fallbacks (no boundary check) in case URL is non-standard
        r"/dp/([A-Z0-9]{10})",
        r"/product/([A-Z0-9]{10})",
        r"/gp/product/([A-Z0-9]{10})",
        r"asin=([A-Z0-9]{10})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None


def _extract_json_object_after_key(text: str, key: str) -> Optional[str]:
    """Pull a balanced {...} after \"key\" or 'key' in embedded Amazon JS."""
    for quote in ('"', "'"):
        needle = f"{quote}{key}{quote}"
        i = text.find(needle)
        if i < 0:
            continue
        j = text.find("{", i)
        if j < 0:
            continue
        depth = 0
        for k in range(j, len(text)):
            c = text[k]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[j : k + 1]
    return None


def _extract_json_array_after_key(text: str, key: str) -> Optional[str]:
    """Pull a balanced [...] after the key (labels are sometimes an array)."""
    for quote in ('"', "'"):
        needle = f"{quote}{key}{quote}"
        i = text.find(needle)
        if i < 0:
            continue
        j = text.find("[", i)
        if j < 0:
            continue
        depth = 0
        for k in range(j, len(text)):
            c = text[k]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return text[j : k + 1]
    return None


def _normalize_price_text(text: str) -> tuple:
    """From '$329.00' or 'US$329,00' return (numeric_price, currency_symbol)."""
    if not text:
        return None, None
    t = " ".join(text.split()).replace("\u00a0", " ").strip()
    sym_m = re.match(r"^[^\d\s]+", t)
    currency = sym_m.group(0) if sym_m else None
    num_m = re.search(r"[\d,]+(?:\.\d+)?", t)
    if not num_m:
        return None, currency or "$"
    raw = num_m.group(0).replace(",", "")
    return raw or None, currency or "$"


def _extract_price(soup: BeautifulSoup, raw_html: str) -> tuple:
    """Best-effort price from buy box / twister JSON."""

    # 1) Hidden accessibility price in buy box (most reliable across all layouts)
    for sel in (
        # ── Newest layouts (2024–2025) ──────────────────────────────────────
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        ".priceToPay .a-offscreen",
        ".priceToPay span.a-offscreen",
        ".reinventPricePriceToPayMargin .a-offscreen",
        "span.reinventPricePriceToPayMargin .a-offscreen",
        # ── Apex / buy-box layouts ──────────────────────────────────────────
        "#apex_desktop .a-price .a-offscreen",
        "#apex_offerDisplay_desktop .a-price .a-offscreen",
        "#apex_offerDisplay_desktop_mbc .a-price .a-offscreen",
        "[data-feature-name='apex_desktop'] .a-price .a-offscreen",
        "[data-feature-name='corePriceDisplay_desktop_feature_div'] .a-price .a-offscreen",
        # ── Buy-box / cart area ─────────────────────────────────────────────
        "#buybox .a-price .a-offscreen",
        "#desktop_buybox .a-price .a-offscreen",
        "#desktop_accordion .a-price .a-offscreen",
        "form#addToCart .a-price .a-offscreen",
        "#addToCart .a-price .a-offscreen",
        "#addToCartFeature .a-price .a-offscreen",
        # ── Older / alternate layouts ───────────────────────────────────────
        "#corePrice_desktop .a-price .a-offscreen",
        "#twister-plus-price-data-price .a-offscreen",
        "#price_inside_buybox",                          # classic layout
        "#priceblock_ourprice",                          # legacy non-prime price
        "#priceblock_dealprice",                         # lightning deal price
        "#priceblock_saleprice",                         # sale price
        "#newBuyBoxPrice",                               # used-listing pages
        # ── Amazon India specific ───────────────────────────────────────────
        "#tp-tool-tip-price .a-offscreen",
        "#tp-tool-tip-price .a-price .a-offscreen",
        # ── Kindle / books ──────────────────────────────────────────────────
        "#booksHeaderSection .a-price .a-offscreen",
        "#kindle-price",
        # ── Subscribe & Save ────────────────────────────────────────────────
        "#sns-base-price",
        "#snsAccordionRowMiddle .a-price .a-offscreen",
    ):
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(strip=True)
            if txt and re.search(r"\d", txt):
                p, c = _normalize_price_text(txt)
                if p:
                    return p, c

    # 2) Split whole + fraction (covers layouts that render price in two spans)
    buy = soup.select_one(
        "#corePrice_feature_div .a-price, #corePrice_desktop .a-price, "
        "#apex_desktop .a-price, #buybox .a-price, #desktop_buybox .a-price, "
        "span.reinventPricePriceToPayMargin .a-price, .priceToPay .a-price, "
        "#apex_offerDisplay_desktop .a-price, #apex_offerDisplay_desktop_mbc .a-price"
    )
    if buy:
        whole = buy.select_one(".a-price-whole")
        if whole:
            w = whole.get_text(strip=True).replace(",", "").rstrip(".")
            frac_el = buy.select_one(".a-price-fraction")
            frac = frac_el.get_text(strip=True) if frac_el else ""
            price = f"{w}.{frac}" if frac else w
            if price and re.search(r"\d", price):
                sym_el = buy.select_one(".a-price-symbol")
                cur = sym_el.get_text(strip=True) if sym_el else "$"
                return price.rstrip("."), cur

    # 3) Any .a-price .a-offscreen with a currency sign
    for el in soup.select(".a-price .a-offscreen"):
        txt = el.get_text(strip=True)
        if re.search(r"\d", txt) and re.search(
            r"[$\u20ac\u00a3\u00a5\u20b9]|USD|EUR|GBP|INR", txt, re.I
        ):
            p, c = _normalize_price_text(txt)
            if p:
                return p, c

    # 3b) Main column only (avoid header/footer stray prices)
    main = soup.select_one("#centerCol, #dp, #dp-container, #ppd")
    if main:
        for el in main.select(".a-price .a-offscreen"):
            txt = el.get_text(strip=True)
            if re.search(r"\d", txt):
                p, c = _normalize_price_text(txt)
                if p:
                    return p, c

    # 3c) Hidden "customerVisiblePrice" inputs (seen on some intl/INR layouts)
    amount_el = soup.find(
        "input", {"name": re.compile(r"customerVisiblePrice\]\[amount\]")}
    )
    code_el = soup.find(
        "input", {"name": re.compile(r"customerVisiblePrice\]\[currencyCode\]")}
    )
    display_el = soup.find(
        "input", {"name": re.compile(r"customerVisiblePrice\]\[displayString\]")}
    )
    if display_el:
        display_val = display_el.get("value", "") or ""
        if display_val and re.search(r"\d", display_val):
            p, c = _normalize_price_text(display_val)
            if not c and code_el:
                c = (code_el.get("value") or "").strip() or None
            if p:
                return p, c
    if amount_el and code_el:
        amount_val = (amount_el.get("value") or "").strip()
        code_val = (code_el.get("value") or "").strip()
        if amount_val and re.search(r"\d", amount_val):
            return amount_val, code_val or None

    # 4) JSON in page scripts (price-specific keys first; avoid generic displayString)
    for pat in (
        r'"bbaDisplayPrice"\s*:\s*"([^"\\]+)"',
        r'"displayPrice"\s*:\s*"([^"\\]+)"',
        r'"formattedBuyingPrice"\s*:\s*"([^"\\]+)"',
        r'"displayAmount"\s*:\s*"([^"\\]+)"',
        r'"formattedPrice"\s*:\s*"([^"\\]+)"',
        r'"priceToPay"\s*:\s*\{[^}]*"rawValue"\s*:\s*([\d.]+)',
    ):
        m = re.search(pat, raw_html)
        if m:
            chunk = m.group(1)
            if "rawValue" in pat:
                if re.match(r"^\d", chunk):
                    return chunk, "$"
            else:
                p, c = _normalize_price_text(chunk)
                if p:
                    return p, c

    # 5) Any displayString that looks like a money amount (nested JSON safe)
    for m in re.finditer(r'"displayString"\s*:\s*"([^"\\]+)"', raw_html):
        chunk = m.group(1)
        if not re.search(r"\d", chunk):
            continue
        if not re.search(r"[$\u20ac\u00a3\u00a5\u20b9]|USD|EUR|GBP|INR", chunk):
            continue
        p, c = _normalize_price_text(chunk)
        if p:
            return p, c

    return None, None


def _normalize_variation_labels(parsed: Any) -> Optional[dict]:
    """Turn object or array of labels into index-keyed dict: 0 -> name, ..."""
    if parsed is None:
        return None
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        return {str(i): v for i, v in enumerate(parsed) if v is not None}
    return None


def _parse_variants_from_variation_values_block(text: str) -> list:
    """
    Newer PDP twister embeds variationValues + dimensions + variationDisplayLabels
    in one block. Labels are keyed by dimension id (e.g. style_name), not 0..N.
    """
    anchor = text.find('"variationValues"')
    if anchor < 0:
        anchor = text.find("'variationValues'")
    if anchor < 0:
        return []
    region = text[anchor : anchor + 20000]
    raw_vv = _extract_json_object_after_key(region, "variationValues")
    raw_dims = _extract_json_array_after_key(region, "dimensions")
    if not raw_vv or not raw_dims:
        return []
    try:
        vv = json.loads(raw_vv)
        dim_keys = json.loads(raw_dims)
    except json.JSONDecodeError:
        return []
    if not isinstance(vv, dict) or not isinstance(dim_keys, list):
        return []

    labels_obj: Optional[dict] = None
    if "variationDisplayLabels" in region:
        raw_lb = _extract_json_object_after_key(region, "variationDisplayLabels")
        if raw_lb:
            try:
                labels_obj = json.loads(raw_lb)
            except json.JSONDecodeError:
                labels_obj = None

    variants = []
    for dk in dim_keys:
        if dk not in vv:
            continue
        opts = vv[dk]
        if not isinstance(opts, list):
            continue
        group = None
        if labels_obj and isinstance(labels_obj, dict):
            group = labels_obj.get(dk)
        if not group:
            group = str(dk).replace("_", " ").strip().title()
        ordered: list[str] = []
        seen: set[str] = set()
        for o in opts:
            s = str(o).strip()
            if s and s not in seen:
                seen.add(s)
                ordered.append(s)
        if ordered:
            variants.append({"group": str(group).strip(), "options": ordered})
    return variants


def _parse_variants_from_twister_json(soup: BeautifulSoup) -> list:
    """
    Amazon stores ASIN->[dim0, dim1, ...] in dimensionValuesDisplayData.
    Labels live in variationDisplayLabels — keyed by dimension id (style_name, …)
    or legacy numeric indices. The dimensions[] array gives column order.
    """
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text or len(text) < 80:
            continue
        if "variationValues" in text and "dimensions" in text:
            vv_variants = _parse_variants_from_variation_values_block(text)
            if vv_variants:
                return vv_variants

    display_data: Optional[dict] = None
    labels_map: Optional[dict] = None
    dim_keys_ordered: Optional[list] = None

    for script in soup.find_all("script"):
        text = script.string or ""
        if not text or len(text) < 80:
            continue
        if display_data is None and "dimensionValuesDisplayData" in text:
            raw_dd = _extract_json_object_after_key(text, "dimensionValuesDisplayData")
            if raw_dd:
                try:
                    dd = json.loads(raw_dd)
                    if isinstance(dd, dict):
                        display_data = dd
                except json.JSONDecodeError:
                    pass
            if dim_keys_ordered is None and "dimensions" in text:
                raw_dims = _extract_json_array_after_key(text, "dimensions")
                if raw_dims:
                    try:
                        parsed = json.loads(raw_dims)
                        if isinstance(parsed, list):
                            dim_keys_ordered = parsed
                    except json.JSONDecodeError:
                        pass

        if labels_map is None and "variationDisplayLabels" in text:
            raw_lb = _extract_json_object_after_key(text, "variationDisplayLabels")
            if raw_lb:
                try:
                    labels_map = _normalize_variation_labels(json.loads(raw_lb))
                except json.JSONDecodeError:
                    pass
            if labels_map is None:
                raw_arr = _extract_json_array_after_key(text, "variationDisplayLabels")
                if raw_arr:
                    try:
                        labels_map = _normalize_variation_labels(json.loads(raw_arr))
                    except json.JSONDecodeError:
                        pass

        if labels_map is None and "dimensionDisplayLabels" in text:
            raw_alt = _extract_json_object_after_key(text, "dimensionDisplayLabels")
            if raw_alt:
                try:
                    labels_map = _normalize_variation_labels(json.loads(raw_alt))
                except json.JSONDecodeError:
                    pass

    if not display_data or not isinstance(display_data, dict):
        return []

    sample = next(
        (v for v in display_data.values() if isinstance(v, list) and v), None
    )
    if not sample:
        return []

    n_dim = len(sample)
    buckets = [set() for _ in range(n_dim)]
    for v in display_data.values():
        if not isinstance(v, list) or len(v) != n_dim:
            continue
        for i, part in enumerate(v):
            part = str(part).strip()
            if part:
                buckets[i].add(part)

    variants = []
    for i in range(n_dim):
        label = None
        if labels_map:
            label = labels_map.get(str(i)) or labels_map.get(i)
            if not label and dim_keys_ordered and i < len(dim_keys_ordered):
                dk = dim_keys_ordered[i]
                label = labels_map.get(dk)
        if not label:
            label = f"Option {i + 1}"
        options = sorted(buckets[i], key=lambda s: s.lower())
        if options:
            variants.append({"group": str(label).strip(), "options": options})

    return variants


def _twister_dimension_labels_from_html(soup: BeautifulSoup) -> list:
    """Ordered labels (Style, Size, Color, …) from legacy #twister or inline twister."""
    labels: list[str] = []

    section = soup.find("div", {"id": "twister"})
    if section:
        for group_div in section.find_all("div", {"class": re.compile(r"dimension")}):
            label_el = group_div.find("label")
            if not label_el:
                continue
            t = label_el.get_text(strip=True)
            t = re.sub(r"\s*:.*$", "", t).strip()
            if t:
                labels.append(t)
        if labels:
            return labels

    for row in soup.select(
        "#twister_feature_div div.inline-twister-row, "
        "#twister-plus-inline-twister div.inline-twister-row"
    ):
        lab = row.select_one("span.a-color-secondary")
        if not lab:
            continue
        t = lab.get_text(strip=True).rstrip(":").strip()
        if t:
            labels.append(t)
    return labels


def _parse_variants(soup: BeautifulSoup) -> list:
    """Extract variant groups: tries JSON first, then HTML swatches."""

    # Method A: embedded JS JSON (+ HTML label names if JSON only had Option 1..N)
    twister = _parse_variants_from_twister_json(soup)
    if twister:
        html_labels = _twister_dimension_labels_from_html(soup)
        if html_labels and len(html_labels) == len(twister):
            if all(g.get("group", "").startswith("Option ") for g in twister):
                for i, lab in enumerate(html_labels):
                    twister[i]["group"] = lab
        return twister

    # Method B: HTML swatch selectors
    variants = []
    swatch_section = soup.find("div", {"id": "twister"})
    if swatch_section:
        for group_div in swatch_section.find_all(
            "div", {"class": re.compile(r"dimension")}
        ):
            label_el = group_div.find("label")
            label_text = label_el.text.strip() if label_el else "variant"
            label_text = re.sub(r"\s*:.*$", "", label_text).strip()
            options = []
            for li in group_div.find_all("li"):
                title = li.get("title", "").strip()
                text = li.text.strip()
                options.append(title if title else text)
            options = [o for o in options if o]
            if options:
                variants.append({"group": label_text, "options": options})

    return variants


# ---------------------------------------------------------------------------
# Shared HTML parser
# ---------------------------------------------------------------------------
def parse_product_html(html: str, url: str, source: str = "http") -> ProductData:
    """Parse Amazon product page HTML into a ProductData object."""
    result = ProductData(source=source)
    soup = BeautifulSoup(html, "html.parser")

    # Title
    title_el = soup.find("span", {"id": "productTitle"})
    result.title = title_el.text.strip() if title_el else None

    # Price + currency
    price, currency = _extract_price(soup, html)
    result.price = price
    if currency:
        result.currency = currency
    else:
        sym_el = soup.select_one(
            "#corePrice_feature_div .a-price-symbol, #buybox .a-price-symbol"
        )
        result.currency = sym_el.get_text(strip=True) if sym_el else "$"

    # Main image — prefer highest resolution
    img = (
        soup.find("img", {"id": "landingImage"})
        or soup.find("img", {"id": "imgBlkFront"})
        or soup.find("img", {"class": re.compile("a-dynamic-image")})
    )
    if img:
        hires = img.get("data-old-hires")
        dynamic = img.get("data-a-dynamic-image")
        if hires:
            result.main_image = hires
        elif dynamic:
            try:
                urls = json.loads(dynamic)
                best = max(urls.items(), key=lambda kv: kv[1][0] * kv[1][1])
                result.main_image = best[0]
            except Exception:
                result.main_image = img.get("src")
        else:
            result.main_image = img.get("src")

    # Rating
    rating_el = soup.find("span", {"data-hook": "rating-out-of-text"}) or soup.find(
        "span", {"class": "a-icon-alt"}
    )
    if rating_el:
        result.rating = rating_el.text.strip().split(" ")[0]

    # Review count
    reviews_el = soup.find("span", {"id": "acrCustomerReviewText"})
    result.review_count = reviews_el.text.strip() if reviews_el else None

    # Availability
    avail_el = soup.find("div", {"id": "availability"})
    result.availability = " ".join(avail_el.text.split()).strip() if avail_el else None

    # Variants
    result.variants = _parse_variants(soup)

    # ASIN
    result.asin = extract_asin(url)

    result.success = bool(result.title)
    if not result.success:
        result.error = "Could not extract title — page may be blocked or layout changed"

    return result


# ---------------------------------------------------------------------------
# Layer 1+2 — HTTP scraper with session pre-warming + CAPTCHA solve
# ---------------------------------------------------------------------------
class HTTPScraper:
    """
    Fast path: requests + BeautifulSoup.
    Pre-warms session cookies to defeat bot detection.
    If CAPTCHA hit, delegates to AmazonCaptchaHandler for Anti-Captcha solve.
    """

    def __init__(self, api_key: str = ANTI_CAPTCHA_KEY):
        self.session = requests.Session()
        self.captcha_handler = AmazonCaptchaHandler(self.session, api_key=api_key)
        self._cookies_warmed = False

    def _warm_cookies(self, domain: str):
        headers = random.choice(HEADERS_POOL)
        try:
            logger.info("Pre-warming cookies on %s…", domain)
            self.session.get(domain, headers=headers, timeout=12)
            time.sleep(random.uniform(1.2, 2.8))
            self.session.get(f"{domain}/s?k=electronics", headers=headers, timeout=12)
            time.sleep(random.uniform(0.8, 1.8))
            self._cookies_warmed = True
            logger.info("Cookie pre-warming complete.")
        except Exception as e:
            logger.warning("Cookie warm-up failed (non-fatal): %s", e)

    def scrape_via_scraperapi(self, url: str) -> ProductData:
        """
        Layer 0 — ScraperAPI proxy.
        Handles CAPTCHA, IP rotation, and JS rendering automatically.
        Fastest path when SCRAPER_API_KEY is configured (~2-4s typical).
        """
        api_url = _build_scraperapi_url(url, render_js=False)
        headers = {
            "User-Agent": random.choice(HEADERS_POOL)["User-Agent"],
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            logger.info("ScraperAPI: fetching %s …", url[:80])
            resp = requests.get(api_url, headers=headers, timeout=70)
            resp.raise_for_status()
        except requests.RequestException as e:
            result = ProductData(source="scraperapi")
            result.error = f"ScraperAPI request failed: {e}"
            logger.warning(result.error)
            return result

        html = resp.text
        if self.captcha_handler.is_captcha_page(html):
            # ScraperAPI normally solves CAPTCHAs — if we still get one, retry
            # with JS rendering enabled (costs 5x credits but guarantees success)
            logger.warning("ScraperAPI: still got CAPTCHA — retrying with render=true …")
            api_url_rendered = _build_scraperapi_url(url, render_js=True)
            try:
                resp2 = requests.get(api_url_rendered, headers=headers, timeout=90)
                resp2.raise_for_status()
                html = resp2.text
            except requests.RequestException as e2:
                result = ProductData(source="scraperapi")
                result.error = f"ScraperAPI render retry failed: {e2}"
                return result

        if self.captcha_handler.is_captcha_page(html):
            result = ProductData(source="scraperapi")
            result.error = "ScraperAPI: CAPTCHA persists after render — escalating."
            result.source = "captcha_unsolved"
            return result

        return parse_product_html(html, url, source="scraperapi")

    def scrape(self, url: str) -> ProductData:
        # ── Layer 0: ScraperAPI (if key is configured) ─────────────────────────
        if SCRAPER_API_KEY:
            result = self.scrape_via_scraperapi(url)
            if result.success:
                return result
            # ScraperAPI failed for some reason — fall through to direct HTTP
            logger.warning("ScraperAPI failed (%s) — falling back to direct HTTP.", result.error)

        # ── Layer 1: Direct HTTP + cookie pre-warm ─────────────────────────────
        result = ProductData(source="http")

        domain_match = re.match(r"(https?://[^/]+)", url)
        domain = domain_match.group(1) if domain_match else "https://www.amazon.com"

        if not self._cookies_warmed:
            self._warm_cookies(domain)

        headers = random.choice(HEADERS_POOL)

        try:
            response = self.session.get(url, headers=headers, timeout=15)
            response.raise_for_status()
        except requests.RequestException as e:
            result.error = f"HTTP request failed: {e}"
            logger.error(result.error)
            return result

        html = response.text

        if self.captcha_handler.is_captcha_page(html):
            if not self.captcha_handler.solver.configured:
                result.error = (
                    "CAPTCHA detected — Anti-Captcha key not configured. "
                    "Escalating to Playwright."
                )
                result.source = "captcha_unsolved"
                logger.warning(result.error)
                return result

            logger.warning("CAPTCHA page — attempting Anti-Captcha solve…")
            solved_resp = self.captcha_handler.solve_and_retry(html, url, headers)

            if solved_resp and not self.captcha_handler.is_captcha_page(solved_resp.text):
                logger.info("CAPTCHA solved — parsing product page.")
                return parse_product_html(solved_resp.text, url, source="http+captcha")

            result.error = "Anti-Captcha solve failed — escalating to Playwright."
            result.source = "captcha_unsolved"
            logger.warning(result.error)
            return result

        return parse_product_html(html, url, source="http")


def _format_exception(exc: BaseException) -> str:
    """Playwright/OS errors on Windows often have an empty str(exc); keep logs useful."""
    name = type(exc).__name__
    msg = str(exc).strip()
    if msg:
        return f"{name}: {msg}"
    if exc.__cause__:
        return f"{name} (cause: {_format_exception(exc.__cause__)})"
    if exc.__context__ and exc.__context__ is not exc.__cause__:
        return f"{name} (context: {_format_exception(exc.__context__)})"
    args = getattr(exc, "args", None)
    if args:
        return f"{name}: {args!r}"
    if name == "NotImplementedError" and sys.platform == "win32":
        return (
            f"{name}: asyncio subprocess unsupported in this context "
            "(Playwright is run in a dedicated thread on Windows; retry after pulling latest scraper.py)"
        )
    return (
        f"{name}: (no message; run `python -m playwright install chromium` if the browser is missing)"
    )


# ---------------------------------------------------------------------------
# Layer 3 — Playwright stealth browser fallback
# ---------------------------------------------------------------------------
def _prepare_asyncio_for_playwright_thread() -> None:
    """
    Uvicorn/FastAPI on Windows may leave the process on an asyncio loop policy
    that cannot spawn subprocesses (Playwright's driver). Run Playwright in a
    worker thread with Proactor + a fresh loop so Chromium can launch.
    """
    if sys.platform != "win32":
        return
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)


def _scrape_with_playwright_impl(url: str, api_key: Optional[str]) -> ProductData:
    """
    Full browser fallback using Playwright (Chromium headless + stealth).
    Handles JS-rendered content — critical for variant extraction.
    Also solves CAPTCHAs in-browser via AmazonCaptchaHandler.
    """
    _prepare_asyncio_for_playwright_thread()

    result = ProductData(source="playwright")
    api_key = api_key or ANTI_CAPTCHA_KEY

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        result.error = (
            "Playwright not installed. "
            "Run: pip install playwright && playwright install chromium"
        )
        logger.error(result.error)
        return result

    domain_match = re.match(r"(https?://[^/]+)", url)
    domain = domain_match.group(1) if domain_match else "https://www.amazon.com"
    chosen_ua = random.choice(HEADERS_POOL)["User-Agent"]

    # Reuse the requests session for cookie sync (CAPTCHA image download)
    shared_session = requests.Session()
    captcha_handler = AmazonCaptchaHandler(shared_session, api_key=api_key)

    headless = os.environ.get("PLAYWRIGHT_HEADLESS", "1").lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    try:
        with sync_playwright() as p:
            logger.debug("Playwright Chromium path: %s", p.chromium.executable_path)

            browser = p.chromium.launch(
                headless=headless,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--window-size=1366,768",
                ],
            )

            context = browser.new_context(
                user_agent=chosen_ua,
                locale="en-US",
                timezone_id="America/New_York",
                viewport={"width": 1366, "height": 768},
                color_scheme="light",
                java_script_enabled=True,
            )

            # Stealth: override webdriver fingerprint
            stealth_js = (
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                "Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});"
                "Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});"
                "window.chrome = { runtime: {} };"
            )
            context.add_init_script(stealth_js)

            page = context.new_page()
            page.set_extra_http_headers({
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            })

            # ── Navigate directly to product (skip homepage pre-warm) ──────────
            # HTTP layer already warmed cookies; Playwright doesn't need to repeat
            # it. Going directly to the product saves 3–5 seconds per request.
            page.set_extra_http_headers({"Referer": domain + "/"})
            logger.info("Playwright: loading product URL directly…")
            page.goto(url, wait_until="domcontentloaded", timeout=35000)

            # CAPTCHA check + solve inside browser
            page_html = page.content()
            if captcha_handler.is_captcha_page(page_html):
                logger.info("Playwright: CAPTCHA detected — attempting in-browser solve…")
                solved = captcha_handler.solve_playwright_captcha(page)
                if not solved:
                    result.error = (
                        "Playwright: CAPTCHA present and could not be solved. "
                        "Try again or add rotating proxies."
                    )
                    browser.close()
                    return result

            # Wait for product title — abort early if blocked
            try:
                page.wait_for_selector("#productTitle", timeout=10000)
            except PWTimeout:
                # Before giving up, check if title appeared anyway (slower pages)
                page_html = page.content()
                parsed_early = parse_product_html(page_html, url, source="playwright")
                if parsed_early.title:
                    browser.close()
                    return parsed_early
                result.error = "Playwright: product title not found (page may be blocked)"
                browser.close()
                return result

            # Wait for JS-rendered variants (non-blocking — not all products have them)
            try:
                page.wait_for_selector(
                    "#twister, #twister_feature_div, #twister-plus-inline-twister",
                    timeout=3000,
                )
                page.wait_for_timeout(800)   # short settle time (was 1500ms)
            except Exception:
                pass  # No variants on this product — don't wait

            # Grab fully rendered HTML and parse
            page_html = page.content()
            browser.close()

        result = parse_product_html(page_html, url, source="playwright")

    except Exception as e:
        detail = _format_exception(e)
        result.error = f"Playwright error: {detail}"
        logger.exception("Playwright failed — %s", detail)

    return result


def scrape_with_playwright(url: str, api_key: Optional[str] = None) -> ProductData:
    """
    Run Playwright off the Uvicorn/asyncio thread on Windows so the browser
    subprocess can start (avoids NotImplementedError in asyncio subprocess).
    """
    api_key = api_key or ANTI_CAPTCHA_KEY
    if sys.platform == "win32":
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_scrape_with_playwright_impl, url, api_key)
            try:
                return fut.result(timeout=180)
            except Exception as e:
                err = ProductData(source="playwright")
                err.error = f"Playwright error: {_format_exception(e)}"
                logger.exception("Playwright thread failed — %s", err.error)
                return err
    return _scrape_with_playwright_impl(url, api_key)


# ---------------------------------------------------------------------------
# Orchestrator: retry + automatic fallback chain
# ---------------------------------------------------------------------------
class AmazonScraper:
    """
    Main entry point.
    Decision flow:
      URL → HTTP (fast) → CAPTCHA? → Anti-Captcha → retry
                                   → Still blocked? → Playwright
    """

    def __init__(
        self,
        max_retries: int = 3,
        force_playwright: bool = False,
        api_key: str = ANTI_CAPTCHA_KEY,
    ):
        self.max_retries = max_retries
        self.force_playwright = force_playwright
        self.api_key = api_key or ANTI_CAPTCHA_KEY
        self._http_scraper = HTTPScraper(api_key=self.api_key)

    def extract(self, url: str) -> dict:
        """
        Main extraction method. Returns a clean dict for API response.

        Decision tree:
          • force_playwright=True  → Playwright only
          • SCRAPER_API_KEY set    → ScraperAPI (fast) → Playwright on failure
          • No key                 → HTTP (1 attempt) → CAPTCHA? → Playwright
                                     (no wasted retries; one failure = escalate)
        """
        logger.info("Extraction start: %s", url)

        if self.force_playwright:
            result = scrape_with_playwright(url, api_key=self.api_key)
            return result.to_dict()

        # HTTP / ScraperAPI — only ONE clean attempt, no blind retries.
        # If CAPTCHA or blocked, escalate immediately to Playwright.
        logger.info("HTTP/ScraperAPI attempt …")
        result = self._http_scraper.scrape(url)
        last_result = result

        if result.success:
            logger.info("Extraction succeeded via %s", result.source)
            return result.to_dict()

        # Partial success: title extracted but price/image missing — still return it
        # rather than burning time on Playwright for a minor gap.
        if result.title and not result.price:
            logger.warning("Price missing after HTTP — attempting Playwright for price fix.")
        elif result.title:
            logger.info("Partial result from HTTP — escalating to Playwright.")
        else:
            logger.info("HTTP failed (%s) — escalating to Playwright.", result.error)

        # Playwright fallback
        logger.info("Escalating to Playwright browser…")
        playwright_result = scrape_with_playwright(url, api_key=self.api_key)

        if playwright_result.success:
            logger.info("Playwright extraction succeeded.")
            return playwright_result.to_dict()

        # Both paths failed — return best partial result
        best = playwright_result if playwright_result.title else last_result
        if best:
            best.error = f"All extraction methods failed. Last error: {best.error}"
            return best.to_dict()

        return {
            "success": False,
            "error": "All extraction methods failed — product could not be retrieved.",
            "extraction_method": "none",
        }
