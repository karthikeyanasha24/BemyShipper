"""
SHEIN Product Scraper
======================
Strategy:
  Layer 1 → Intercept SHEIN's internal XHR/API calls via Playwright
            (most reliable — gets structured JSON with full product data)
  Layer 2 → JSON-LD / window.__INITIAL_STATE__ embedded script parsing
  Layer 3 → DOM HTML fallback

SHEIN URL formats:
  https://us.shein.com/{slug}-p-{goodsId}-cat-{catId}.html
  https://www.shein.com/{slug}-p-{goodsId}-cat-{catId}.html
  https://in.shein.com/{slug}-p-{goodsId}-cat-{catId}.html
  https://m.shein.com/{slug}-p-{goodsId}-cat-{catId}.html

SHEIN is fully JS-rendered (React SPA) — simple HTTP + BeautifulSoup alone won't work.
Playwright is required for all layers; API interception is the primary method.
"""

import os
import re
import json
import logging
import random
import sys
from urllib.parse import urlparse, urlencode
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple
from dataclasses import dataclass, field

import requests

from scraper import (
    _format_exception,
    _prepare_asyncio_for_playwright_thread,
    _build_scraperapi_url,
    SCRAPER_API_KEY,
    APIFY_API_TOKEN,
)

logger = logging.getLogger(__name__)

def _safe_filename_piece(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s)
    return s.strip("_") or "unknown"


def _save_html_debug(html: str, url: str, *, label: str) -> Optional[str]:
    """
    Persist HTML for later offline parsing/debugging.

    Controlled by env:
      - SHEIN_SAVE_HTML=1        → enable
      - SHEIN_SAVE_HTML_DIR=...  → output directory (default: ./saved_html)
    """
    if not html:
        return None
    if os.getenv("SHEIN_SAVE_HTML", "").strip() not in ("1", "true", "TRUE", "yes", "YES"):
        return None

    out_dir = (os.getenv("SHEIN_SAVE_HTML_DIR") or "saved_html").strip()
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:
        return None

    gid = extract_goods_id(url) or ""
    host = (urlparse(url).hostname or "").lower()
    fname = (
        f"shein_{_safe_filename_piece(label)}_"
        f"{_safe_filename_piece(host)}_"
        f"{_safe_filename_piece(gid)}_"
        f"{int(__import__('time').time())}.html"
    )
    path = os.path.join(out_dir, fname)
    try:
        with open(path, "wb") as f:
            f.write(html.encode("utf-8", errors="replace"))
        logger.info("Saved HTML (%s) to %s", label, path)
        return path
    except Exception:
        return None


def parse_shein_from_html(html: str, url: str) -> dict:
    """
    Offline/online: parse a saved HTML blob into ProductData.
    Tries ProductGroup/Product JSON-LD first, then window state, then DOM fallback.
    """
    # Prefer ProductGroup JSON-LD (common on sheinindia.in)
    jsonld_pg = _parse_shein_productgroup_jsonld(html, url)
    if jsonld_pg and jsonld_pg.success:
        return jsonld_pg.to_dict()

    # Fall back to the existing HTML pipeline (window state + JSON-LD + DOM)
    return _parse_shein_html(html, url).to_dict()



# ---------------------------------------------------------------------------
# Result dataclass  (same shape as Amazon / Zara ProductData)
# ---------------------------------------------------------------------------
@dataclass
class ProductData:
    title: Optional[str] = None
    price: Optional[str] = None
    currency: Optional[str] = None
    main_image: Optional[str] = None
    variants: list = field(default_factory=list)
    asin: Optional[str] = None          # re-used as generic product_id
    rating: Optional[str] = None
    review_count: Optional[str] = None
    availability: Optional[str] = None
    source: str = "playwright"
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
# URL / ID helpers
# ---------------------------------------------------------------------------
SHEIN_DOMAINS = (
    "shein.com",
    "sheglam.com",
    "sheinindia.in",
)


def extract_goods_id(url: str) -> Optional[str]:
    """
    Extract SHEIN goods_id from product URL.

    Formats handled:
      us.shein.com/slug-p-{id}-cat-{cat}.html      → standard SHEIN
      sheinindia.in/slug/p/{id}_{variant}           → sheinindia.in
      sheinindia.in/slug/p/{id}                     → sheinindia.in (no variant)
      ?goods_id={id}                                → query param
    """
    # sheinindia.in  /p/{digits}[_{variant}]  pattern
    m = re.search(r"/p/(\d{5,12})(?:[_/]|$)", url, re.I)
    if m:
        return m.group(1)
    # Standard SHEIN  -p-{id}-cat-  or  -p-{id}.html  pattern
    m = re.search(r"-p-(\d{5,12})(?:-cat-|-?\.html|$|\?)", url, re.I)
    if m:
        return m.group(1)
    # Query param fallback
    m = re.search(r"[?&]goods_id=(\d+)", url, re.I)
    if m:
        return m.group(1)
    return None


def extract_cat_id(url: str) -> Optional[str]:
    m = re.search(r"-cat-(\d+)", url, re.I)
    return m.group(1) if m else None


def extract_storefront(url: str) -> str:
    """Return subdomain prefix: 'us', 'in', 'uk', 'au', etc. Default 'us'."""
    u = (url or "").lower()
    if "sheinindia.in" in u:
        return "in"
    m = re.search(r"(?:https?://)?([a-z]{2,3})\.shein\.", url, re.I)
    if m:
        return m.group(1).lower()
    return "us"


def _shein_locale(storefront: str) -> tuple:
    """Map storefront code → (playwright_locale, timezone)."""
    mapping = {
        "us": ("en-US", "America/New_York"),
        "uk": ("en-GB", "Europe/London"),
        "in": ("en-IN", "Asia/Kolkata"),
        "au": ("en-AU", "Australia/Sydney"),
        "ca": ("en-CA", "America/Toronto"),
        "fr": ("fr-FR", "Europe/Paris"),
        "de": ("de-DE", "Europe/Berlin"),
        "es": ("es-ES", "Europe/Madrid"),
        "it": ("it-IT", "Europe/Rome"),
        "mx": ("es-MX", "America/Mexico_City"),
        "br": ("pt-BR", "America/Sao_Paulo"),
        "sa": ("ar-SA", "Asia/Riyadh"),
        "ae": ("ar-AE", "Asia/Dubai"),
    }
    return mapping.get(storefront, ("en-US", "America/New_York"))


def _currency_symbol(code: Optional[str]) -> str:
    if not code:
        return "$"
    c = str(code).strip().upper()
    return {
        "USD": "$", "GBP": "£", "EUR": "€",
        "INR": "₹", "AUD": "A$", "CAD": "C$",
        "MXN": "MX$", "BRL": "R$", "SAR": "﷼", "AED": "د.إ",
    }.get(c, c)


def _is_shein_bot_page(html: str) -> bool:
    """Detect Cloudflare / DDoS-Guard / bot challenge pages."""
    if not html or len(html) < 500:
        return True
    low = html.lower()
    # Strong positive signals that this IS a real product page
    has_product = any(k in low for k in (
        "goods_id", "productname", "product-intro", "j-expose-product-detail",
        "product__price", "goods-name", "application/ld+json",
        "prod-name", "prod-price", "sheinindia",  # sheinindia.in DOM markers
        "shein-product", "addtocart", "add-to-cart",
    ))
    if has_product:
        return False
    # CF / DDoS-Guard challenge markers
    bot_markers = (
        "just a moment",
        "enable javascript and cookies",
        "checking your browser",
        "cf-browser-verification",
        "ray id",
        "cloudflare",
        "ddos-guard",
        "please wait while",
        "verifying you are human",
        "challenge-platform",
        "turnstile",
        "__cf_chl",
    )
    # if the page body is tiny and has any bot marker → blocked
    if len(html) < 5000 and any(m in low for m in bot_markers):
        return True
    return any(m in low for m in bot_markers)


# ---------------------------------------------------------------------------
# Layer 1: Parse SHEIN product JSON from intercepted API or embedded state
# ---------------------------------------------------------------------------
def _parse_shein_goods_detail(data: dict, url: str) -> Optional[ProductData]:
    """
    Parse the main SHEIN goods detail JSON.
    SHEIN API returns a nested structure; key paths differ by endpoint version.
    """
    result = ProductData(source="shein-api")

    # ── Unwrap response envelope ───────────────────────────────────────────
    # Shape: { code:0, info:{ detail:{...}, skc_sale_attr:[...] } }
    # or   : { code:0, data:{ goods_info:{...} } }
    info = data.get("info") or data.get("data") or data
    detail = (
        info.get("detail")
        or info.get("goods_info")
        or info.get("productInfo")
        or info if isinstance(info, dict) else {}
    )
    if not isinstance(detail, dict):
        return None

    # ── Product ID ──────────────────────────────────────────────────────────
    gid = (
        str(detail.get("goods_id") or detail.get("id") or "")
        or extract_goods_id(url)
        or ""
    )
    result.asin = gid

    # ── Title ───────────────────────────────────────────────────────────────
    title = (
        detail.get("goods_name")
        or detail.get("title")
        or detail.get("name")
        or detail.get("productName")
    )
    if title:
        result.title = str(title).strip()

    # ── Price ────────────────────────────────────────────────────────────────
    # SHEIN typically gives price in USD cents or as a float dict
    price_info = detail.get("salePrice") or detail.get("retailPrice") or detail.get("price") or {}
    if isinstance(price_info, dict):
        amount = price_info.get("amount") or price_info.get("value") or price_info.get("usdAmount")
        currency_code = price_info.get("currencyCode") or price_info.get("currency") or "USD"
        if amount is not None:
            result.price = str(amount).replace(",", "")
            result.currency = _currency_symbol(currency_code)
    elif isinstance(price_info, (int, float)):
        result.price = f"{price_info:.2f}"
        result.currency = "$"
    elif isinstance(price_info, str):
        m = re.search(r"[\d,.]+", price_info)
        if m:
            result.price = m.group(0).replace(",", "")
            result.currency = "$"

    # Fallback: raw numeric price field
    if not result.price:
        for key in ("goods_price", "retailPrice", "price_local", "price"):
            val = detail.get(key)
            if val is not None and val != "" and val != 0:
                try:
                    result.price = f"{float(str(val).replace(',','')):.2f}"
                    result.currency = "$"
                    break
                except (ValueError, TypeError):
                    pass

    # ── Main Image ───────────────────────────────────────────────────────────
    img_url = (
        detail.get("goods_img")
        or detail.get("main_image")
        or detail.get("cover_image")
        or detail.get("image_url")
    )
    if img_url:
        img_url = str(img_url).strip()
        if img_url.startswith("//"):
            img_url = "https:" + img_url
        result.main_image = img_url

    # Try image from list
    if not result.main_image:
        images = detail.get("images") or detail.get("goods_imgs") or []
        if isinstance(images, list) and images:
            first_img = images[0]
            if isinstance(first_img, dict):
                src = first_img.get("src") or first_img.get("url") or first_img.get("origin_image") or ""
            else:
                src = str(first_img)
            if src:
                if src.startswith("//"):
                    src = "https:" + src
                result.main_image = src

    # ── Variants ─────────────────────────────────────────────────────────────
    variants = []

    # Colors — SHEIN color selector lives in skc_sale_attr or goods_range
    skc_attrs = info.get("skc_sale_attr") or detail.get("skc_sale_attr") or []
    sku_list = info.get("sku_list") or detail.get("skuList") or detail.get("sku_list") or []

    # Build from skc_sale_attr first (most reliable)
    if isinstance(skc_attrs, list):
        for attr_group in skc_attrs:
            if not isinstance(attr_group, dict):
                continue
            group_name = (
                attr_group.get("attr_name")
                or attr_group.get("name")
                or attr_group.get("attribute_name")
                or "Option"
            )
            values_raw = (
                attr_group.get("attr_value_list")
                or attr_group.get("values")
                or attr_group.get("attr_values")
                or []
            )
            options = []
            for v in values_raw:
                if isinstance(v, dict):
                    label = (
                        v.get("attr_value")
                        or v.get("name")
                        or v.get("label")
                        or str(v.get("attr_value_id", ""))
                    )
                else:
                    label = str(v)
                if label and label not in options:
                    options.append(str(label).strip())
            if group_name and options:
                variants.append({"group": str(group_name).strip(), "options": options})

    # Fallback: Parse from sku_list attributes
    if not variants and isinstance(sku_list, list):
        attr_map: dict = {}
        for sku in sku_list:
            if not isinstance(sku, dict):
                continue
            for attr in sku.get("sku_sale_attr") or sku.get("attributes") or []:
                if not isinstance(attr, dict):
                    continue
                group = str(attr.get("attr_name") or attr.get("name") or "Option").strip()
                val = str(
                    attr.get("attr_value") or attr.get("value") or attr.get("label") or ""
                ).strip()
                if group and val:
                    attr_map.setdefault(group, [])
                    if val not in attr_map[group]:
                        attr_map[group].append(val)
        for group_name, opts in attr_map.items():
            variants.append({"group": group_name, "options": opts})

    result.variants = variants

    # ── Rating & Reviews ─────────────────────────────────────────────────────
    rating = detail.get("goods_score") or detail.get("rating") or ""
    if rating:
        result.rating = str(rating)
    review_count = detail.get("comment_num") or detail.get("reviewCount") or ""
    if review_count:
        result.review_count = str(review_count)

    # ── Availability ──────────────────────────────────────────────────────────
    stock = detail.get("stock") or detail.get("is_on_sale")
    if stock is not None:
        if str(stock) in ("1", "True", "true", "IN_STOCK"):
            result.availability = "In Stock"
        elif str(stock) in ("0", "False", "false", "OUT_OF_STOCK"):
            result.availability = "Out of Stock"
        else:
            result.availability = str(stock)
    elif result.variants or result.price:
        result.availability = "In Stock"

    result.success = bool(result.title)
    if not result.success:
        result.error = "Could not extract title from SHEIN API response"

    return result


def _parse_shein_json_ld(soup, url: str) -> Optional[ProductData]:
    """Extract product data from JSON-LD script tags."""
    for script in soup.find_all("script", type="application/ld+json"):
        raw = (script.string or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            result = ProductData(source="json-ld")
            result.asin = extract_goods_id(url)
            result.title = str(data.get("name") or "").strip() or None

            # Price
            offers = data.get("offers")
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("lowPrice")
                currency = offers.get("priceCurrency")
                avail = str(offers.get("availability") or "")
                if price is not None:
                    result.price = str(price).replace(",", "")
                if currency:
                    result.currency = _currency_symbol(currency)
                if "InStock" in avail:
                    result.availability = "In Stock"
                elif "OutOfStock" in avail:
                    result.availability = "Out of Stock"
            elif isinstance(offers, list) and offers:
                o0 = offers[0]
                if isinstance(o0, dict):
                    price = o0.get("price")
                    currency = o0.get("priceCurrency")
                    if price is not None:
                        result.price = str(price).replace(",", "")
                    if currency:
                        result.currency = _currency_symbol(currency)

            # Image
            img = data.get("image")
            if isinstance(img, list) and img:
                img = img[0]
            if isinstance(img, str) and img:
                if img.startswith("//"):
                    img = "https:" + img
                result.main_image = img

            # Rating
            agg = data.get("aggregateRating")
            if isinstance(agg, dict):
                result.rating = str(agg.get("ratingValue") or "")
                result.review_count = str(agg.get("reviewCount") or "")

            result.success = bool(result.title)
            if result.success:
                return result
    return None


def _parse_shein_window_state(html: str, url: str) -> Optional[ProductData]:
    """
    SHEIN React apps embed product state in window.__INITIAL_STATE__ or
    window.__sheinPageInfo__ — parse it before full DOM.
    """
    patterns = [
        r"window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*;?\s*</script>",
        r"window\.__sheinPageInfo__\s*=\s*(\{.+?\})\s*;?\s*</script>",
        r'"productInfo"\s*:\s*(\{[^<]{50,}\})',
        r'"goods"\s*:\s*(\{[^<]{50,}\})',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.DOTALL | re.S)
        if not match:
            continue
        raw = match.group(1)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Trim to nearest valid JSON by finding balanced braces
            try:
                # Try to find a complete JSON object
                brace_count = 0
                end_idx = 0
                for i, ch in enumerate(raw):
                    if ch == "{":
                        brace_count += 1
                    elif ch == "}":
                        brace_count -= 1
                    if brace_count == 0 and i > 0:
                        end_idx = i + 1
                        break
                if end_idx:
                    data = json.loads(raw[:end_idx])
                else:
                    continue
            except Exception:
                continue

        if not isinstance(data, dict):
            continue

        # Try to find product detail inside state
        for key in ("productInfo", "goods", "detail", "goodsDetail", "productDetail"):
            sub = data.get(key)
            if isinstance(sub, dict) and sub:
                parsed = _parse_shein_goods_detail({key: sub}, url)
                if parsed and parsed.success:
                    parsed.source = "window-state"
                    return parsed

        # Try top-level parse
        parsed = _parse_shein_goods_detail(data, url)
        if parsed and parsed.success:
            parsed.source = "window-state"
            return parsed

    return None


# ---------------------------------------------------------------------------
# Layer 3: HTML DOM fallback
# ---------------------------------------------------------------------------
def _parse_shein_html(html: str, url: str) -> ProductData:
    """Parse SHEIN rendered HTML when API interception fails."""
    from bs4 import BeautifulSoup

    result = ProductData(source="shein-html")
    soup = BeautifulSoup(html, "html.parser")
    is_shein_india = "sheinindia.in" in (url or "").lower()

    result.asin = extract_goods_id(url)

    # ── Try window state embedded in HTML ────────────────────────────────────
    state_result = _parse_shein_window_state(html, url)
    if state_result and state_result.success:
        return state_result

    # ── Try JSON-LD ──────────────────────────────────────────────────────────
    ld_result = _parse_shein_json_ld(soup, url)
    if ld_result:
        result.title = ld_result.title
        result.price = ld_result.price
        result.currency = ld_result.currency
        result.main_image = ld_result.main_image
        result.rating = ld_result.rating
        result.review_count = ld_result.review_count
        result.availability = ld_result.availability

    # ── Title — DOM selectors (SHEIN class names change often) ────────────────
    if not result.title:
        title_selectors = [
            # sheinindia.in
            "h1.prod-name",
            "h1.product-intro__head-name",
            ".product-intro__head-name",
            ".goods-name",
            "[class*='product-intro__head-name']",
            "[class*='productName']",
            "[class*='goods-name']",
            "h1[class*='product']",
            "h1",
        ]
        for sel in title_selectors:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                result.title = el.get_text(strip=True)
                break

    # ── Price — DOM selectors ─────────────────────────────────────────────────
    if not result.price:
        price_selectors = [
            # sheinindia.in
            ".prod-price-section .prod-sp",
            ".prod-price-section .sec-prod-sp",
            ".product-intro__head-price .price-amount",
            ".product__price .product__price-amount",
            "[class*='product-intro__head-price'] [class*='amount']",
            ".from.product-intro__price-current",
            ".S-product-item__price-text",
            "[class*='sale-price'] [class*='amount']",
            "[class*='product__price-amount']",
            ".product-intro__price",
        ]
        for sel in price_selectors:
            el = soup.select_one(sel)
            if el:
                txt = el.get_text(strip=True)
                m = re.search(r"[\d,.]+", txt)
                if m:
                    result.price = m.group(0).replace(",", "")
                    # Detect currency from symbol
                    if "₹" in txt or "Rs." in txt:
                        result.currency = "₹"
                    elif "£" in txt:
                        result.currency = "£"
                    elif "€" in txt:
                        result.currency = "€"
                    elif not result.currency:
                        result.currency = "$"
                    break

    # ── Main Image ────────────────────────────────────────────────────────────
    if not result.main_image:
        img_selectors = [
            # sheinindia.in
            ".image-slick-container img",
            ".product-image-gallery img",
            ".prod-container img",
            ".product-intro__main-img img",
            ".goods-img img",
            "[class*='product-intro__main'] img",
            "[class*='goods-img'] img",
            ".crop-image-container img",
            ".main-img img",
            ".product-image img",
        ]
        for sel in img_selectors:
            el = soup.select_one(sel)
            if el:
                src = (
                    el.get("src")
                    or el.get("data-src")
                    or el.get("data-original")
                    or ""
                ).strip()
                if src and not src.endswith("placeholder") and len(src) > 20:
                    if src.startswith("//"):
                        src = "https:" + src
                    result.main_image = src
                    break
        # sheinindia.in often uses small thumbnails first; prefer a larger one if present
        if is_shein_india and result.main_image:
            # try to swap thumbnail sizing like -78Wx98H- to -473Wx593H- when possible
            result.main_image = re.sub(r"-\d+Wx\d+H-", "-473Wx593H-", result.main_image)

    # ── Variants — size selector buttons ─────────────────────────────────────
    variants = []
    size_options = []
    size_selectors = [
        # sheinindia.in
        ".size-variant-item span",
        ".size-swatch .size-variant-item span",
        ".product-intro__size-choose .product-intro__size-item",
        "[class*='product-intro__size-item']",
        ".size-selector button",
        "[class*='size-item']",
        "[data-attr-name='Size'] [class*='item']",
    ]
    for sel in size_selectors:
        items = soup.select(sel)
        if items:
            for item in items:
                txt = item.get_text(strip=True)
                if txt and txt not in size_options and len(txt) < 30:
                    size_options.append(txt)
            if size_options:
                break

    if size_options:
        variants.append({"group": "Size", "options": size_options})

    # Color selector
    color_options = []
    color_selectors = [
        # sheinindia.in (sometimes empty)
        ".prod-color",
        ".color-variant-block [role='button']",
        ".product-intro__color-item span",
        "[class*='product-intro__color-item'] span",
        "[class*='color-item'] span",
        ".color-selector-v2__item span",
    ]
    for sel in color_selectors:
        items = soup.select(sel)
        if items:
            for item in items:
                txt = (item.get_text(strip=True) or item.get("title", "").strip()).strip()
                if txt and txt not in color_options:
                    # sheinindia.in sometimes has blank prod-color; skip empty
                    color_options.append(txt)
            if color_options:
                break

    if color_options:
        variants.append({"group": "Color", "options": color_options})

    if variants:
        result.variants = variants

    # ── Rating ────────────────────────────────────────────────────────────────
    if not result.rating:
        for sel in [".product-intro__head-reviews-score", "[class*='score-star']"]:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                result.rating = el.get_text(strip=True)
                break

    # ── Availability ──────────────────────────────────────────────────────────
    if not result.availability and (result.price or result.variants):
        result.availability = "In Stock"

    result.success = bool(result.title)
    if not result.success:
        result.error = "Could not extract title from SHEIN HTML"

    return result


# ---------------------------------------------------------------------------
# API interception helper — decide if a response URL looks like product data
# ---------------------------------------------------------------------------
_SHEIN_API_HINTS = (
    "api/productInfo",
    "api/product/info",
    "goods/detail",
    "/product/detail",
    "goodsDetail",
    "goods-detail",
    "api/spp/goods",
    "product_detail",
    "catalogs/products",
    "api/ccc-goods",
    "api/store-detail",
    "goods_id",
    "api/user/auth_token",       # ignore — auth not product
)

_SHEIN_API_IGNORE = (
    "recommend",
    "similar",
    "wishlist",
    "cart",
    "user",
    "auth",
    "session",
    "track",
    "log",
    "monitor",
    "beacon",
    "analytics",
    "fb.js",
    "gtm.js",
)


def _looks_like_shein_product_api(url_r: str, ct: str) -> bool:
    if "json" not in ct and not url_r.endswith(".json"):
        return False
    url_lower = url_r.lower()
    if any(ig in url_lower for ig in _SHEIN_API_IGNORE):
        return False
    return any(hint in url_lower for hint in _SHEIN_API_HINTS)


# ---------------------------------------------------------------------------
# Main Playwright runner
# ---------------------------------------------------------------------------
def _scrape_shein_playwright_impl(url: str) -> dict:
    """
    Playwright scraper for SHEIN — runs off the Uvicorn thread on Windows.
    Same pattern as zara_scraper._scrape_zara_playwright_impl.
    """
    _prepare_asyncio_for_playwright_thread()

    result = ProductData(source="playwright")

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        result.error = (
            "Playwright not installed. "
            "Run: pip install playwright && playwright install chromium"
        )
        return result.to_dict()

    captured_product_json: list = []  # mutable container for closure

    storefront = extract_storefront(url)
    locale, timezone_id = _shein_locale(storefront)

    UA = random.choice([
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
            "Gecko/20100101 Firefox/128.0"
        ),
    ])

    parsed_u = urlparse(url)
    origin = f"{parsed_u.scheme}://{parsed_u.netloc}"

    _prev_headless_new = os.environ.get("PLAYWRIGHT_CHROMIUM_USE_HEADLESS_NEW")
    os.environ["PLAYWRIGHT_CHROMIUM_USE_HEADLESS_NEW"] = "1"

    def _run_playwright(headless: bool) -> tuple[str, list]:
        """Run Playwright once and return (page_html, captured_json)."""
        captured_product_json_local: list = []

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                ignore_default_args=["--enable-automation"],
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--window-size=1440,900",
                    "--disable-web-security",                # helps with SHEIN XHR
                    "--lang=" + locale.replace("-", "_"),
                ],
            )
            context = browser.new_context(
                user_agent=UA,
                locale=locale,
                timezone_id=timezone_id,
                viewport={"width": 1440, "height": 900},
                color_scheme="light",
                java_script_enabled=True,
                # Ignore HTTPS errors occasionally seen on SHEIN CDN
                ignore_https_errors=True,
            )

            # ── Full stealth init script (replaces playwright-stealth package) ───
            # Patches every fingerprint vector Cloudflare checks.
            context.add_init_script(f"""
(function() {{
  // 1. Remove webdriver flag — most important check
  Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
  delete navigator.__proto__.webdriver;

  // 2. Realistic plugin list (Chrome has 3 built-in plugins)
  const makePlugin = (name, filename, description, mimeTypes) => {{
    const plugin = Object.create(Plugin.prototype);
    Object.defineProperty(plugin, 'name',        {{ get: () => name }});
    Object.defineProperty(plugin, 'filename',    {{ get: () => filename }});
    Object.defineProperty(plugin, 'description', {{ get: () => description }});
    Object.defineProperty(plugin, 'length',      {{ get: () => mimeTypes.length }});
    return plugin;
  }};
  const pluginList = [
    makePlugin('Chrome PDF Plugin',          'internal-pdf-viewer',    'Portable Document Format', []),
    makePlugin('Chrome PDF Viewer',          'mhjfbmdgcfjbbpaeojofohoefgiehjai', '', []),
    makePlugin('Native Client',              'internal-nacl-plugin',   '', []),
  ];
  Object.defineProperty(pluginList, 'item',   {{ value: i => pluginList[i] }});
  Object.defineProperty(pluginList, 'namedItem', {{ value: n => pluginList.find(p => p.name === n) || null }});
  Object.defineProperty(pluginList, 'refresh',   {{ value: () => {{}} }});
  Object.defineProperty(navigator, 'plugins', {{ get: () => pluginList }});
  Object.defineProperty(navigator, 'mimeTypes', {{ get: () => {{ const m = []; m.item = i => m[i]; m.namedItem = n => null; return m; }} }});

  // 3. Languages
  Object.defineProperty(navigator, 'languages', {{ get: () => ['{locale}', 'en-US', 'en'] }});

  // 4. chrome object — CF checks window.chrome.runtime
  window.chrome = {{
    app: {{ isInstalled: false, InstallState: {{ DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }}, RunningState: {{ CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }} }},
    runtime: {{
      OnInstalledReason: {{ CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' }},
      OnRestartRequiredReason: {{ APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' }},
      PlatformArch: {{ ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' }},
      PlatformNaclArch: {{ ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' }},
      PlatformOs: {{ ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' }},
      RequestUpdateCheckStatus: {{ NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' }},
      id: undefined
    }}
  }};

  // 5. Notification — CF checks if Notification is defined and can be queried
  const originalQuery = window.Notification ? window.Notification.requestPermission : undefined;
  if (!window.Notification) {{
    window.Notification = {{ permission: 'default', requestPermission: async () => 'default' }};
  }}

  // 6. Permissions API — CF sends a permissions query for 'notifications'
  const originalPermissions = navigator.permissions;
  if (originalPermissions) {{
    const origQuery = originalPermissions.query.bind(originalPermissions);
    navigator.permissions.query = (params) => {{
      if (params && params.name === 'notifications') {{
        return Promise.resolve({{ state: Notification.permission, onchange: null }});
      }}
      return origQuery(params);
    }};
  }}

  // 7. WebGL — spoof vendor/renderer to match a real GPU
  const getParam = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(param) {{
    if (param === 37445) return 'Intel Inc.';
    if (param === 37446) return 'Intel Iris OpenGL Engine';
    return getParam.call(this, param);
  }};
  if (typeof WebGL2RenderingContext !== 'undefined') {{
    const getParam2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(param) {{
      if (param === 37445) return 'Intel Inc.';
      if (param === 37446) return 'Intel Iris OpenGL Engine';
      return getParam2.call(this, param);
    }};
  }}

  // 8. Navigator hardware / memory — CF checks these
  Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => 8 }});
  Object.defineProperty(navigator, 'deviceMemory',        {{ get: () => 8 }});
  Object.defineProperty(navigator, 'platform',            {{ get: () => 'Win32' }});
  Object.defineProperty(navigator, 'vendor',              {{ get: () => 'Google Inc.' }});
  Object.defineProperty(navigator, 'appVersion',          {{ get: () => '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36' }});
  Object.defineProperty(navigator, 'maxTouchPoints',      {{ get: () => 0 }});

  // 9. Screen dimensions
  Object.defineProperty(screen, 'width',       {{ get: () => 1440 }});
  Object.defineProperty(screen, 'height',      {{ get: () => 900  }});
  Object.defineProperty(screen, 'availWidth',  {{ get: () => 1440 }});
  Object.defineProperty(screen, 'availHeight', {{ get: () => 860  }});
  Object.defineProperty(screen, 'colorDepth',  {{ get: () => 24   }});
  Object.defineProperty(screen, 'pixelDepth',  {{ get: () => 24   }});
  Object.defineProperty(window, 'outerWidth',  {{ get: () => 1440 }});
  Object.defineProperty(window, 'outerHeight', {{ get: () => 900  }});

  // 10. Connection (Network Information API)
  if (!navigator.connection) {{
    Object.defineProperty(navigator, 'connection', {{
      get: () => ({{ rtt: 50, downlink: 10, effectiveType: '4g', saveData: false }})
    }});
  }}

  // 11. Prevent iframe-based detection
  Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {{
    get: function() {{
      const win = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow').get.call(this);
      if (win) {{
        try {{ Object.defineProperty(win.navigator, 'webdriver', {{ get: () => undefined }}); }} catch(e) {{}}
      }}
      return win;
    }}
  }});

  // 12. toString cloaking — prevents detection of overridden native functions
  const nativeToString = Function.prototype.toString;
  Function.prototype.toString = function() {{
    if (this === Function.prototype.toString) return 'function toString() {{ [native code] }}';
    const s = nativeToString.call(this);
    return s;
  }};
}})();
""")

            page = context.new_page()
            page.set_extra_http_headers({
                "Accept-Language": f"{locale},en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            })

            # ── Intercept SHEIN API responses ─────────────────────────────────
            def handle_response(response):
                url_r = response.url
                if response.status != 200:
                    return
                ct = (response.headers.get("content-type") or "").lower()
                if not _looks_like_shein_product_api(url_r, ct):
                    return
                try:
                    body = response.json()
                    # Only keep if it looks like a product (not empty or error responses)
                    if isinstance(body, dict) and body.get("code") in (0, "0", None, ""):
                        captured_product_json_local.append(body)
                        logger.info("Intercepted SHEIN API response: %s", url_r[:120])
                    elif isinstance(body, dict) and (
                        body.get("goods_id") or body.get("id") or body.get("name")
                    ):
                        captured_product_json_local.append(body)
                        logger.info("Intercepted SHEIN product JSON: %s", url_r[:120])
                except Exception:
                    pass

            page.on("response", handle_response)

            # Cookie pre-warm (homepage first — same as Amazon / Zara)
            logger.info("Playwright: pre-warming SHEIN homepage %s …", origin)
            try:
                page.goto(f"{origin}/", wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(random.randint(1500, 2800))
            except Exception as e:
                logger.warning("SHEIN homepage pre-warm failed (non-fatal): %s", e)

            # Update referrer for product page navigation
            page.set_extra_http_headers({
                "Accept-Language": f"{locale},en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
                "Referer": f"{origin}/",
            })

            logger.info("Playwright: loading SHEIN product URL…")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except PWTimeout:
                logger.warning("SHEIN: goto timed out — trying to get content anyway")

            # ── Cloudflare challenge wait ──────────────────────────────────────
            # CF shows "Just a moment..." page for ~3–8s, then auto-redirects.
            # We loop up to 15s waiting for the title to leave the CF challenge.
            _cf_titles = ("just a moment", "please wait", "checking your browser",
                          "ddos-guard", "enable javascript")
            for _wait_i in range(15):
                try:
                    _title = page.title().lower()
                except Exception:
                    _title = ""
                if any(t in _title for t in _cf_titles):
                    logger.info("SHEIN: CF challenge active (title=%r), waiting 1s… (%d/15)", _title, _wait_i + 1)
                    page.wait_for_timeout(1000)
                else:
                    if _wait_i > 0:
                        logger.info("SHEIN: CF challenge cleared after %ds", _wait_i)
                    break

            # Wait for product content to load
            try:
                page.wait_for_selector(
                    "h1, [class*='product-intro'], [class*='goods-name'], "
                    "[class*='productName'], [class*='prod-name']",
                    timeout=15000,
                )
            except PWTimeout:
                logger.warning("SHEIN: timeout waiting for product selector")

            # Give XHR requests time to fire and be captured
            page.wait_for_timeout(random.randint(3000, 4500))

            # Simulate human scroll to trigger lazy-load / additional API calls
            try:
                page.mouse.move(
                    random.randint(400, 900),
                    random.randint(200, 500),
                )
                page.evaluate("window.scrollTo({top: 400, behavior: 'smooth'})")
                page.wait_for_timeout(random.randint(800, 1400))
                page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
                page.wait_for_timeout(600)
            except Exception:
                pass

            page_html = page.content()
            browser.close()
            return page_html, captured_product_json_local

    try:
        page_html, captured_product_json = _run_playwright(headless=True)
        _save_html_debug(page_html, url, label="playwright_headless")

        # ── Check for bot wall ────────────────────────────────────────────────
        if _is_shein_bot_page(page_html):
            logger.warning("SHEIN: headless still blocked after stealth — retrying headed")
            # Headed mode as last resort — run in its OWN thread with a fresh budget
            # so the 180s limit for the outer thread isn't already exhausted.
            try_headed = os.environ.get("SHEIN_TRY_HEADED", "1").strip() not in ("0", "false", "no")
            if try_headed:
                logger.info("SHEIN: launching headed Playwright (SHEIN_TRY_HEADED=1)…")
                with ThreadPoolExecutor(max_workers=1) as _headed_pool:
                    _hfut = _headed_pool.submit(_run_playwright, False)
                    try:
                        page_html, captured_product_json = _hfut.result(timeout=120)
                        _save_html_debug(page_html, url, label="playwright_headed")
                    except Exception as _he:
                        logger.error("SHEIN: headed retry failed — %s", _he)
                        page_html = ""
                        captured_product_json = []
            if _is_shein_bot_page(page_html):
                blocked = ProductData(source="playwright")
                blocked.error = (
                    "SHEIN blocked automated access (Cloudflare challenge). "
                    "Try a residential IP or proxy. The test UI works fine in a real browser."
                )
                return blocked.to_dict()

        # ── Try intercepted JSON responses ────────────────────────────────────
        if captured_product_json:
            logger.info("Trying %d intercepted SHEIN API responses…", len(captured_product_json))
            for blob in reversed(captured_product_json):  # most recent first
                parsed = _parse_shein_goods_detail(blob, url)
                if parsed and parsed.success:
                    logger.info("Successfully parsed SHEIN product from API JSON.")
                    return parsed.to_dict()

        # ── window.__INITIAL_STATE__ embedded in HTML ─────────────────────────
        logger.info("SHEIN: trying window state extraction from HTML…")
        state_result = _parse_shein_window_state(page_html, url)
        if state_result and state_result.success:
            logger.info("Successfully parsed SHEIN product from window state.")
            return state_result.to_dict()

        # ── HTML DOM fallback ─────────────────────────────────────────────────
        logger.info("SHEIN: falling back to HTML DOM parsing…")
        result = _parse_shein_html(page_html, url)

    except Exception as e:
        detail = _format_exception(e)
        result.error = f"SHEIN Playwright error: {detail}"
        logger.exception("SHEIN Playwright failed — %s", detail)
    finally:
        if _prev_headless_new is None:
            os.environ.pop("PLAYWRIGHT_CHROMIUM_USE_HEADLESS_NEW", None)
        else:
            os.environ["PLAYWRIGHT_CHROMIUM_USE_HEADLESS_NEW"] = _prev_headless_new

    return result.to_dict()


# ---------------------------------------------------------------------------
# Layer -1 (FASTEST): ScraperAPI raw HTML → JSON-LD ProductGroup / Product
# ---------------------------------------------------------------------------
# sheinindia.in (and many other SHEIN storefronts) embed complete product data
# as server-side JSON-LD in the initial HTML response.  No JS rendering needed —
# a plain HTTP GET through ScraperAPI is enough and returns in ~2-5 s vs the
# 10-30 s that render_js=True needs.
#
# JSON-LD shapes we handle:
#   { "@type": "ProductGroup", "name": ..., "image": ..., "offers": {...},
#     "hasVariant": [ {"size": "S"}, {"size": "M"}, ... ],
#     "variesBy": ["size"] }
#   { "@type": "Product", "name": ..., ... }   (standard single-product schema)
# ---------------------------------------------------------------------------

def _parse_shein_productgroup_jsonld(html: str, url: str) -> Optional[ProductData]:
    """
    Parse JSON-LD embedded in raw (non-JS-rendered) SHEIN HTML.

    Handles:
    - ``@type: ProductGroup`` — sheinindia.in and some SHEIN storefronts
      Extracts title, price, currency, availability, main image, and all
      size/color/style variants from ``hasVariant``.
    - ``@type: Product`` — falls back to the existing ``_parse_shein_json_ld``
      helper so we don't duplicate logic.

    Returns a populated ProductData (success=True) or None.
    """
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None

    for script in soup.find_all("script", type="application/ld+json"):
        raw = (script.string or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if not isinstance(data, dict):
            continue

        schema_type = str(data.get("@type") or "")

        # ── ProductGroup schema ───────────────────────────────────────────────
        if schema_type == "ProductGroup":
            result = ProductData(source="scraperapi-jsonld")
            result.asin = extract_goods_id(url)

            # Title
            name = str(data.get("name") or "").strip()
            result.title = name or None

            # Main image
            img = data.get("image")
            if isinstance(img, list) and img:
                img = img[0]
            if isinstance(img, str) and img:
                result.main_image = img if img.startswith("http") else "https:" + img

            # Price + currency + availability from top-level offers
            offers = data.get("offers")
            if isinstance(offers, dict):
                price_raw = offers.get("price") or offers.get("lowPrice")
                currency_raw = offers.get("priceCurrency")
                avail_raw = str(offers.get("availability") or "")
                if price_raw is not None:
                    result.price = str(price_raw).replace(",", "")
                if currency_raw:
                    result.currency = _currency_symbol(currency_raw)
                if "InStock" in avail_raw:
                    result.availability = "In Stock"
                elif "OutOfStock" in avail_raw:
                    result.availability = "Out of Stock"

            # Variants from hasVariant array
            # Each item is a Product node; the varying dimension is in variesBy.
            has_variants = data.get("hasVariant") or []
            # variesBy e.g. ["size"] or ["Color", "Size"]
            varies_by_raw = data.get("variesBy") or []
            varies_by = [v.lower() for v in varies_by_raw if isinstance(v, str)]

            if isinstance(has_variants, list) and has_variants:
                # Collect unique values for each variant dimension
                variant_groups: dict = {}  # lowercase_dim → [values in order seen]

                for variant in has_variants:
                    if not isinstance(variant, dict):
                        continue
                    # Check declared variesBy keys first, then common fallbacks
                    dims_to_check = varies_by if varies_by else ["size", "color", "colour", "style"]
                    for dim in dims_to_check:
                        # JSON-LD uses lowercase field names (size, color, …)
                        val = variant.get(dim) or variant.get(dim.capitalize())
                        if val and isinstance(val, str):
                            group_key = dim.lower()
                            if group_key not in variant_groups:
                                variant_groups[group_key] = []
                            if val not in variant_groups[group_key]:
                                variant_groups[group_key].append(val)

                if variant_groups:
                    result.variants = [
                        {"group": dim.capitalize(), "options": options}
                        for dim, options in variant_groups.items()
                        if options
                    ]

            result.success = bool(result.title)
            if result.success:
                logger.debug(
                    "SHEIN JSON-LD ProductGroup parsed: title=%r price=%r variants=%s",
                    result.title, result.price,
                    [v["group"] for v in result.variants],
                )
                return result

        # ── Plain Product schema ──────────────────────────────────────────────
        # Delegate to existing helper (handles price, image, rating, etc.)
        elif schema_type == "Product":
            result = ProductData(source="scraperapi-jsonld")
            result.asin = extract_goods_id(url)
            result.title = str(data.get("name") or "").strip() or None

            offers = data.get("offers")
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("lowPrice")
                currency = offers.get("priceCurrency")
                avail = str(offers.get("availability") or "")
                if price is not None:
                    result.price = str(price).replace(",", "")
                if currency:
                    result.currency = _currency_symbol(currency)
                if "InStock" in avail:
                    result.availability = "In Stock"
                elif "OutOfStock" in avail:
                    result.availability = "Out of Stock"

            img = data.get("image")
            if isinstance(img, list) and img:
                img = img[0]
            if isinstance(img, str) and img:
                result.main_image = img if img.startswith("http") else "https:" + img

            agg = data.get("aggregateRating")
            if isinstance(agg, dict):
                result.rating = str(agg.get("ratingValue") or "")
                result.review_count = str(agg.get("reviewCount") or "")

            result.success = bool(result.title)
            if result.success:
                return result

    return None


def _mobile_to_desktop_shein_url(url: str) -> str:
    """
    Convert m.shein.com mobile URLs to their desktop equivalents.

    m.shein.com/us/Slug-p-{id}.html  →  us.shein.com/Slug-p-{id}.html
    m.shein.com/uk/Slug-p-{id}.html  →  uk.shein.com/Slug-p-{id}.html
    m.shein.com/Slug-p-{id}.html     →  us.shein.com/Slug-p-{id}.html

    Mobile pages are far more aggressively guarded (JS-heavy SPA) and
    rarely have server-side JSON-LD, while the desktop equivalent usually
    does — and ScraperAPI raw GET can fetch it reliably.
    """
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() != "m.shein.com":
        return url
    path = parsed.path
    # /us/slug…  →  region="us", rest="/slug…"
    m = re.match(r"^/([a-z]{2})(/.*)", path)
    if m:
        region, rest = m.group(1), m.group(2)
        new_netloc = f"{region}.shein.com"
        new_url = parsed._replace(netloc=new_netloc, path=rest).geturl()
    else:
        new_url = parsed._replace(netloc="us.shein.com").geturl()
    logger.info("SHEIN: mobile → desktop URL: %s", new_url[:100])
    return new_url


def _anticaptcha_solve_turnstile(page_url: str, html: str) -> Optional[str]:
    """
    Use anti-captcha.com TurnstileTaskProxyless to solve a Cloudflare Turnstile
    challenge and return the cf-turnstile-response token.

    Returns the token string on success, None on any failure.
    """
    try:
        from captcha_solver import ANTI_CAPTCHA_KEY as _AC_KEY
    except ImportError:
        _AC_KEY = os.getenv("ANTI_CAPTCHA_API_KEY", "").strip()

    if not _AC_KEY:
        logger.debug("_anticaptcha_solve_turnstile: no API key — skipping")
        return None

    # Extract sitekey from the page
    sitekey: Optional[str] = None
    for pat in (
        r'data-sitekey=["\']([0-9A-Za-z_\-]{10,})["\']',
        r'"siteKey"\s*:\s*"([0-9A-Za-z_\-]{10,})"',
        r'cf_challenge_sitekey["\s:=]+["\']([0-9A-Za-z_\-]{10,})["\']',
    ):
        m = re.search(pat, html)
        if m:
            sitekey = m.group(1)
            break

    if not sitekey:
        logger.debug("_anticaptcha_solve_turnstile: could not extract sitekey")
        return None

    logger.info("_anticaptcha_solve_turnstile: sitekey=%r for %s", sitekey, page_url[:60])

    try:
        create_resp = requests.post(
            "https://api.anti-captcha.com/createTask",
            json={
                "clientKey": _AC_KEY,
                "task": {
                    "type": "TurnstileTaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": sitekey,
                },
            },
            timeout=30,
        )
        create_data = create_resp.json()
    except Exception as exc:
        logger.warning("_anticaptcha_solve_turnstile: createTask error: %s", exc)
        return None

    if create_data.get("errorId", 0) != 0:
        logger.warning(
            "_anticaptcha_solve_turnstile: API error: %s",
            create_data.get("errorDescription"),
        )
        return None

    task_id = create_data.get("taskId")
    logger.info("_anticaptcha_solve_turnstile: task_id=%s — polling…", task_id)

    import time as _time
    deadline = _time.time() + 120
    while _time.time() < deadline:
        _time.sleep(4)
        try:
            result = requests.post(
                "https://api.anti-captcha.com/getTaskResult",
                json={"clientKey": _AC_KEY, "taskId": task_id},
                timeout=30,
            ).json()
        except Exception:
            continue
        if result.get("errorId", 0) != 0:
            logger.warning(
                "_anticaptcha_solve_turnstile: result error: %s",
                result.get("errorDescription"),
            )
            return None
        if result.get("status") == "ready":
            token = result.get("solution", {}).get("token")
            logger.info(
                "_anticaptcha_solve_turnstile: solved! token=%r…", (token or "")[:20]
            )
            return token

    logger.warning("_anticaptcha_solve_turnstile: timed out")
    return None


def _scraperapi_fetch_shein(url: str, *, country_code: str, render: bool,
                             premium: bool = False, timeout: int = 60) -> Optional[str]:
    """Single ScraperAPI GET for a SHEIN page. Returns HTML string or None."""
    payload: dict = {
        "api_key": SCRAPER_API_KEY,
        "url": url,
        "country_code": country_code,
    }
    if render:
        payload["render"] = "true"
    if premium:
        payload["premium"] = "true"
    try:
        resp = requests.get("https://api.scraperapi.com/", params=payload, timeout=timeout)
        return resp.text if resp.text else None
    except Exception as exc:
        logger.warning("ScraperAPI fetch failed (render=%s premium=%s): %s", render, premium, exc)
        return None


def _scrape_shein_scraperapi_jsonld(
    url: str,
    *,
    save_path: Optional[str] = None,
) -> Optional[ProductData]:
    """
    Multi-layer ScraperAPI + JSON-LD extraction with anti-captcha fallback.

    Layer 1 — raw HTML, no JS rendering (fastest, cheapest, ~2-5 s):
        Works for sheinindia.in and most desktop SHEIN pages that embed
        JSON-LD server-side.

    Layer 2 — ScraperAPI render=true (JS rendering, ~15-30 s):
        Used when Layer 1 returns a bot-wall / no JSON-LD.
        ScraperAPI's headless browser handles most basic Cloudflare challenges.

    Layer 3 — ScraperAPI render=true + premium=true (residential IPs, ~20-40 s):
        Used when Layer 2 is still blocked. Residential proxies + full browser
        fingerprinting bypass tighter Cloudflare configurations.

    Layer 4 — anti-captcha Turnstile solve + direct request:
        If a Cloudflare Turnstile widget is visible in the blocked HTML,
        anti-captcha.com's TurnstileTaskProxyless is used to obtain the
        cf-turnstile-response token. The token is then submitted to the
        CF challenge endpoint to obtain a cf_clearance cookie, after which
        a direct requests.get with that cookie is attempted.

    Mobile URLs (m.shein.com) are automatically converted to their desktop
    equivalents before any fetch attempt.
    """
    if not SCRAPER_API_KEY:
        return None

    # ── 0. Normalise mobile → desktop URL ────────────────────────────────────
    fetch_url = _mobile_to_desktop_shein_url(url)

    storefront = extract_storefront(fetch_url)
    country_map = {
        "us": "us", "uk": "gb", "in": "in", "au": "au",
        "ca": "ca", "fr": "fr", "de": "de", "es": "es",
        "it": "it", "mx": "mx", "br": "br", "sa": "sa", "ae": "ae",
    }
    country_code = country_map.get(storefront, "us")

    def _try_parse(html: str) -> Optional[ProductData]:
        if not html or len(html) < 500:
            return None
        if _is_shein_bot_page(html):
            return None
        if 'application/ld+json' not in html:
            return None
        r = _parse_shein_productgroup_jsonld(html, fetch_url)
        return r if (r and r.success) else None

    def _save(html: str) -> None:
        if not save_path or not html:
            return
        try:
            save_dir = os.path.dirname(save_path)
            if save_dir:
                os.makedirs(save_dir, exist_ok=True)
            with open(save_path, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(html)
            logger.info("SHEIN: saved HTML (%d bytes) → %s", len(html), save_path)
        except Exception as exc:
            logger.warning("SHEIN: could not save HTML: %s", exc)

    # ── Layer 1: raw HTML (no JS render) ─────────────────────────────────────
    logger.info("SHEIN Layer 1 (raw): %s (storefront=%s)", fetch_url[:80], storefront)
    html = _scraperapi_fetch_shein(fetch_url, country_code=country_code,
                                   render=False, timeout=60)
    if html:
        result = _try_parse(html)
        if result:
            _save(html)
            logger.info("SHEIN Layer 1 SUCCESS — title=%r price=%r variants=%s",
                        result.title, result.price,
                        [v["group"] + ":" + str(len(v["options"])) for v in result.variants])
            return result
        logger.info("SHEIN Layer 1: no JSON-LD or bot-wall — escalating to Layer 2")
    else:
        logger.warning("SHEIN Layer 1: fetch failed/timeout")

    # ── Layer 2: render=true (JS rendering handles basic Cloudflare) ──────────
    logger.info("SHEIN Layer 2 (render=true): %s", fetch_url[:80])
    html = _scraperapi_fetch_shein(fetch_url, country_code=country_code,
                                   render=True, timeout=90)
    if html:
        result = _try_parse(html)
        if result:
            _save(html)
            logger.info("SHEIN Layer 2 SUCCESS — title=%r price=%r", result.title, result.price)
            return result
        logger.info("SHEIN Layer 2: still blocked — escalating to Layer 3")
    else:
        logger.warning("SHEIN Layer 2: fetch failed/timeout")

    # ── Layer 3: render=true + premium residential IPs ────────────────────────
    logger.info("SHEIN Layer 3 (render=true + premium): %s", fetch_url[:80])
    html = _scraperapi_fetch_shein(fetch_url, country_code=country_code,
                                   render=True, premium=True, timeout=120)
    if html:
        result = _try_parse(html)
        if result:
            _save(html)
            logger.info("SHEIN Layer 3 SUCCESS — title=%r price=%r", result.title, result.price)
            return result
        # ── Layer 4: anti-captcha Turnstile solve ─────────────────────────────
        if 'turnstile' in (html or "").lower() or 'cf-challenge' in (html or "").lower():
            logger.info("SHEIN Layer 4: Cloudflare Turnstile detected — invoking anti-captcha")
            token = _anticaptcha_solve_turnstile(fetch_url, html)
            if token:
                # Submit Turnstile token to CF's challenge endpoint to get cf_clearance
                import time as _time
                try:
                    # Detect the CF challenge form action
                    action_m = re.search(
                        r'action=["\'](/cdn-cgi/[^"\']+)["\']', html
                    )
                    action_path = action_m.group(1) if action_m else "/cdn-cgi/challenge-platform/h/g/flow/ov1"
                    cf_verify_url = f"https://{urlparse(fetch_url).hostname}{action_path}"

                    sess = requests.Session()
                    sess.headers.update({
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        "Referer": fetch_url,
                    })
                    verify_resp = sess.post(
                        cf_verify_url,
                        data={"cf-turnstile-response": token},
                        timeout=30,
                        allow_redirects=True,
                    )
                    # cf_clearance should now be in session cookies
                    if "cf_clearance" in sess.cookies:
                        logger.info("SHEIN Layer 4: got cf_clearance — fetching product page")
                        page_resp = sess.get(fetch_url, timeout=60)
                        html4 = page_resp.text
                        result = _try_parse(html4)
                        if result:
                            _save(html4)
                            logger.info("SHEIN Layer 4 SUCCESS — title=%r", result.title)
                            return result
                    else:
                        logger.warning("SHEIN Layer 4: CF verify did not set cf_clearance")
                except Exception as exc:
                    logger.warning("SHEIN Layer 4: anti-captcha bypass error: %s", exc)
        logger.warning("SHEIN Layer 3: still no usable JSON-LD after all layers")
    else:
        logger.warning("SHEIN Layer 3: fetch failed/timeout")

    # Save whatever we have for debugging
    if html:
        _save(html)

    logger.warning("SHEIN: all layers exhausted — no product data extracted from %s", fetch_url[:80])
    return None


_APIFY_SHEIN_PRODUCT_ACTOR_DEFAULT = "consummate_mandala~shein-product-scraper"
_APIFY_RUN_SYNC_SHEIN = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"


def _apify_shein_product_actor_id() -> str:
    return (os.getenv("APIFY_SHEIN_ACTOR") or _APIFY_SHEIN_PRODUCT_ACTOR_DEFAULT).strip()


def _apify_error_message(resp: requests.Response, *, label: str = "Apify") -> str:
    """Parse Apify JSON error body (403 insufficient-permissions, etc.)."""
    try:
        j = resp.json()
        if isinstance(j, dict):
            e = j.get("error")
            if isinstance(e, dict):
                typ = e.get("type", "error")
                msg = e.get("message", "")
                return f"{label} HTTP {resp.status_code} ({typ}): {msg}"
    except Exception:
        pass
    return f"{label} HTTP {resp.status_code}: {(resp.text or '')[:400]}"

# Listing/category actor (returns many items). Override via env.
# NOTE: This varies by your Apify actor choice. Set APIFY_SHEIN_LIST_ACTOR in `.env`.
_APIFY_SHEIN_LIST_ACTOR_DEFAULT = ""


def _apify_shein_list_actor_id() -> str:
    return (os.getenv("APIFY_SHEIN_LIST_ACTOR") or _APIFY_SHEIN_LIST_ACTOR_DEFAULT).strip()


def _is_shein_listing_url(url: str) -> bool:
    u = (url or "").lower()
    return (
        "/recommendselection/" in u
        or "-sc-" in u
        or "/category/" in u
    ) and ("-p-" not in u and "goods_id=" not in u)


def _normalize_shein_url(u: str) -> str:
    if not u:
        return ""
    s = str(u).strip()
    if s.startswith("//"):
        s = "https:" + s
    return s


def _parse_apify_shein_list_item(item: dict) -> Optional[dict]:
    """
    Shape a SHEIN listing item to the compact structure you want to display.
    Keeps original fields when present.
    """
    if not isinstance(item, dict):
        return None

    goods_id = str(item.get("goods_id") or item.get("goodsId") or item.get("product_id") or item.get("id") or "")
    title = item.get("goods_name") or item.get("title") or item.get("name") or ""
    url = _normalize_shein_url(item.get("url") or item.get("product_url") or item.get("detail_url") or "")
    image = _normalize_shein_url(item.get("image_url") or item.get("goods_img") or item.get("goodsImg") or "")

    sale_price = item.get("sale_price")
    if sale_price is None and isinstance(item.get("salePrice"), dict):
        sale_price = item["salePrice"].get("amount")
    original_price = item.get("original_price")
    if original_price is None and isinstance(item.get("retailPrice"), dict):
        original_price = item["retailPrice"].get("amount")

    sp = item.get("salePrice")
    rp = item.get("retailPrice")
    display_sale = sp.get("amountWithSymbol") if isinstance(sp, dict) else None
    display_retail = rp.get("amountWithSymbol") if isinstance(rp, dict) else None
    hb = item.get("homeBadge")
    hb_text = hb.get("text") if isinstance(hb, dict) else None

    out = {
        "goods_id": goods_id or None,
        "goods_name": str(title).strip() or None,
        "goods_img": image or None,
        "product_id": str(item.get("product_id") or goods_id or "").strip() or None,
        "title": str(title).strip() or None,
        "url": url or None,
        "image_url": image or None,
        "sale_price": sale_price,
        "original_price": original_price,
        "display_sale": display_sale,
        "display_retail": display_retail,
        "discount_text": item.get("discount_text") or item.get("unit_discount") or hb_text,
        "category_id": item.get("category_id") or item.get("cat_id"),
        "source_type": item.get("source_type") or "product",
        # Keep raw structures if present (useful for richer UI later)
        "salePrice": item.get("salePrice"),
        "retailPrice": item.get("retailPrice"),
        "flashPrice": item.get("flashPrice"),
        "detail_image": item.get("detail_image"),
    }
    return {k: v for k, v in out.items() if v is not None}


def _scrape_shein_apify_listing(
    url: str, *, results_wanted: int = 20
) -> Tuple[Optional[list], Optional[str]]:
    """
    Apify listing/category layer.
    Returns (items, None) on success, (None, error_message) on Apify/HTTP failure,
    (None, None) when APIFY_SHEIN_LIST_ACTOR is not configured.
    """
    if not APIFY_API_TOKEN:
        return None, None

    actor = _apify_shein_list_actor_id()
    if not actor:
        logger.warning("SHEIN listing actor not configured (APIFY_SHEIN_LIST_ACTOR is empty).")
        return None, None
    api_url = _APIFY_RUN_SYNC_SHEIN.format(actor=actor)
    payload = {
        "results_wanted": int(results_wanted) if results_wanted else 20,
        "startUrl": url,
        "proxyConfiguration": {"useApifyProxy": False},
    }
    timeout_s = int(os.getenv("APIFY_SHEIN_LIST_TIMEOUT", "180"))
    logger.info("SHEIN Apify listing: actor=%s url=%s", actor, url[:80])

    try:
        resp = requests.post(
            api_url,
            params={"token": APIFY_API_TOKEN},
            json=payload,
            timeout=timeout_s,
            headers={"Content-Type": "application/json"},
        )
    except Exception as exc:
        logger.warning("SHEIN Apify listing request error: %s", exc)
        return None, f"SHEIN listing Apify request error: {exc}"

    if resp.status_code not in (200, 201):
        msg = _apify_error_message(resp, label="SHEIN listing Apify")
        logger.warning("%s", msg[:400])
        return None, msg

    try:
        items = resp.json()
    except Exception:
        logger.warning("SHEIN Apify listing: non-JSON response")
        return None, "SHEIN listing Apify returned non-JSON response."

    if not items or not isinstance(items, list):
        logger.warning("SHEIN Apify listing: empty dataset returned")
        return None, "SHEIN listing Apify returned an empty dataset for this URL."

    mapped: list = []
    for it in items:
        m = _parse_apify_shein_list_item(it) if isinstance(it, dict) else None
        if m:
            mapped.append(m)
    return mapped or items, None


def _parse_apify_shein_item(item: dict, url: str) -> Optional[ProductData]:
    """
    Parse a single item from the Apify SHEIN actor dataset into ProductData.
    Handles field name variants across different actor versions.
    """
    if not isinstance(item, dict):
        return None

    result = ProductData(source="apify-shein")

    # ── Title ──────────────────────────────────────────────────────────────────
    result.title = str(
        item.get("name") or item.get("title") or
        item.get("productName") or item.get("goods_name") or ""
    ).strip() or None

    # ── Product ID ─────────────────────────────────────────────────────────────
    result.asin = str(
        item.get("id") or item.get("goodsId") or item.get("goods_id") or
        item.get("productId") or extract_goods_id(url) or ""
    )

    # ── Price ──────────────────────────────────────────────────────────────────
    raw_price = (
        item.get("price") or item.get("salePrice") or
        item.get("currentPrice") or item.get("retailPrice") or
        item.get("originalPrice") or item.get("priceFormatted")
    )
    if raw_price is not None:
        # Try to extract numeric value
        if isinstance(raw_price, (int, float)):
            result.price = f"{float(raw_price):.2f}"
            result.currency = "$"
        elif isinstance(raw_price, dict):
            amount = raw_price.get("amount") or raw_price.get("value") or ""
            cur = raw_price.get("currencyCode") or raw_price.get("currency") or "USD"
            result.price = str(amount).replace(",", "") if amount else None
            result.currency = _currency_symbol(cur)
        else:
            m = re.search(r"[\d,.]+", str(raw_price))
            if m:
                result.price = m.group(0).replace(",", "")
                txt = str(raw_price)
                if "₹" in txt or "Rs" in txt:
                    result.currency = "₹"
                elif "£" in txt:
                    result.currency = "£"
                elif "€" in txt:
                    result.currency = "€"
                else:
                    result.currency = "$"

    cur_code = item.get("currency") or item.get("currencyCode")
    if cur_code:
        result.currency = _currency_symbol(str(cur_code))

    # ── Main image ─────────────────────────────────────────────────────────────
    images_raw = item.get("images") or item.get("image") or item.get("goods_img") or []
    if isinstance(images_raw, list) and images_raw:
        first = images_raw[0]
        if isinstance(first, dict):
            img_url = first.get("url") or first.get("src") or first.get("origin_image") or ""
        else:
            img_url = str(first)
        if img_url:
            if img_url.startswith("//"):
                img_url = "https:" + img_url
            result.main_image = img_url
    elif isinstance(images_raw, str) and images_raw:
        if images_raw.startswith("//"):
            images_raw = "https:" + images_raw
        result.main_image = images_raw

    # ── Variants ───────────────────────────────────────────────────────────────
    variants: list = []

    sizes_raw = item.get("sizes") or item.get("size") or item.get("sizeOptions") or []
    if isinstance(sizes_raw, list) and sizes_raw:
        size_names = []
        for s in sizes_raw:
            name = (s.get("name") or s.get("value") or s.get("label") or str(s)) if isinstance(s, dict) else str(s)
            if name and name not in size_names:
                size_names.append(str(name).strip())
        if size_names:
            variants.append({"group": "Size", "options": size_names})

    colors_raw = item.get("colors") or item.get("color") or item.get("colorOptions") or []
    if isinstance(colors_raw, list) and colors_raw:
        color_names = []
        for c in colors_raw:
            name = (c.get("name") or c.get("label") or c.get("color_image") or str(c)) if isinstance(c, dict) else str(c)
            if name and len(name) < 40 and name not in color_names:
                color_names.append(str(name).strip())
        if color_names:
            variants.append({"group": "Color", "options": color_names})

    result.variants = variants

    # ── Rating & reviews ───────────────────────────────────────────────────────
    rating = item.get("rating") or item.get("averageRating") or item.get("stars")
    if rating is not None:
        result.rating = str(rating)
    review_count = item.get("reviewCount") or item.get("reviews") or item.get("totalReviews")
    if review_count is not None:
        result.review_count = str(review_count)

    # ── Availability ───────────────────────────────────────────────────────────
    avail_raw = item.get("availability") or item.get("available") or item.get("inStock")
    if avail_raw is not None:
        if isinstance(avail_raw, bool):
            result.availability = "In Stock" if avail_raw else "Out of Stock"
        else:
            result.availability = str(avail_raw).replace("_", " ").title()
    elif result.price or result.variants:
        result.availability = "In Stock"

    result.success = bool(result.title)
    if not result.success:
        result.error = "Apify SHEIN actor returned no title — check actor output for this URL"
    return result


def _scrape_shein_apify(url: str) -> Optional[ProductData]:
    """
    Apify Layer: call SHEIN product actor (default consummate_mandala~shein-product-scraper).

    When APIFY_API_TOKEN is set, always returns a ProductData (success or error) so callers
    can surface Apify 403 insufficient-permissions, etc.

    Override actor: APIFY_SHEIN_ACTOR in `.env`.
    """
    if not APIFY_API_TOKEN:
        return None

    actor = _apify_shein_product_actor_id()
    api_url = _APIFY_RUN_SYNC_SHEIN.format(actor=actor)
    logger.info("SHEIN Apify: actor=%s url=%s", actor, url[:80])

    try:
        resp = requests.post(
            api_url,
            params={"token": APIFY_API_TOKEN},
            json={"startUrls": [{"url": url}]},
            timeout=120,
            headers={"Content-Type": "application/json"},
        )
    except Exception as exc:
        logger.warning("SHEIN Apify request error: %s", exc)
        err = ProductData(source="apify-shein")
        err.error = f"SHEIN Apify request error: {exc}"
        return err

    if resp.status_code not in (200, 201):
        msg = _apify_error_message(resp, label="SHEIN Apify")
        logger.warning("%s", msg[:400])
        err = ProductData(source="apify-shein")
        err.error = msg
        return err

    try:
        items = resp.json()
    except Exception:
        logger.warning("SHEIN Apify: non-JSON response")
        err = ProductData(source="apify-shein")
        err.error = "SHEIN Apify returned a non-JSON response."
        return err

    if not items or not isinstance(items, list):
        logger.warning("SHEIN Apify: empty dataset returned")
        err = ProductData(source="apify-shein")
        err.error = "SHEIN Apify returned an empty dataset for this URL."
        return err

    logger.info("SHEIN Apify: got %d item(s)", len(items))
    result = _parse_apify_shein_item(items[0], url)
    if result and result.success:
        logger.info("SHEIN Apify: success — title=%r price=%r", result.title, result.price)
    return result


def _scrape_shein_scraperapi(url: str) -> Optional[ProductData]:
    """
    Layer 0: Attempt extraction via ScraperAPI (render_js=True).
    ScraperAPI handles Cloudflare bot detection at the infrastructure level,
    returning rendered HTML with window.__INITIAL_STATE__ and JSON-LD intact.
    Returns a populated ProductData on success, or None to fall through to Playwright.
    """
    if not SCRAPER_API_KEY:
        return None

    storefront = extract_storefront(url)
    # Map storefront to ScraperAPI country code
    country_map = {
        "us": "us", "uk": "gb", "in": "in", "au": "au",
        "ca": "ca", "fr": "fr", "de": "de", "es": "es",
        "it": "it", "mx": "mx", "br": "br", "sa": "sa", "ae": "ae",
    }
    country_code = country_map.get(storefront, "us")

    api_url = _build_scraperapi_url(url, render_js=True, country_code=country_code)
    logger.info("SHEIN ScraperAPI Layer 0: %s (storefront=%s, country=%s)", url[:80], storefront, country_code)

    try:
        resp = requests.get(api_url, timeout=75)
        html = resp.text
    except Exception as e:
        logger.warning("SHEIN ScraperAPI request failed: %s", e)
        return None

    _save_html_debug(html, url, label="scraperapi_layer_0_render_js")

    if _is_shein_bot_page(html):
        logger.warning("SHEIN ScraperAPI response is a bot/challenge page — falling through to Playwright")
        return None

    result = _parse_shein_html(html, url)
    if result.success:
        result.source = "scraperapi"
        logger.info("SHEIN: ScraperAPI extraction successful (title=%r)", result.title)
        return result

    logger.info("SHEIN: ScraperAPI HTML parsed but incomplete — falling through to Playwright")
    return None



def scrape_shein(url: str, *, username: str = "user") -> dict:
    """
    Scrape a SHEIN product page using ScraperAPI raw HTML + JSON-LD parsing.

    Strategy (~2-5 s, no browser required):
      1. Fetch the page HTML via ScraperAPI plain GET:
            requests.get("https://api.scraperapi.com/", params={"api_key": KEY, "url": url})
      2. Save the raw HTML to disk:
            - API callers  →  html_cache/{username}_shein.html
              (e.g. user "alice" → alice_shein.html, user "bob" → bob_shein.html)
            - frontend.html local test  →  html_cache/shein.html  (username="frontend")
      3. Parse JSON-LD ProductGroup from the saved HTML:
            <script type="application/ld+json"> … @type: "ProductGroup" … </script>
            Fields extracted: name, image, offers.price / priceCurrency / availability,
            hasVariant[].size  (sizes variant group)
      4. Return extracted data dict, or success=False on any failure.
    """
    import re as _re

    # Build a filesystem-safe filename component from the username.
    # Special case: "frontend" → fixed filename "shein" (no user prefix).
    safe_username = _re.sub(r"[^a-zA-Z0-9_\-@.]", "_", username or "user")
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "html_cache")

    if safe_username == "frontend":
        save_path = os.path.join(cache_dir, "shein.html")
    else:
        save_path = os.path.join(cache_dir, f"{safe_username}_shein.html")

    logger.info(
        "scrape_shein: ScraperAPI JSON-LD | user=%r | save=%s", safe_username, save_path,
    )

    result = _scrape_shein_scraperapi_jsonld(url, save_path=save_path)
    if result and result.success:
        return result.to_dict()

    # Extraction failed — return informative error dict
    err = ProductData(source="scraperapi-jsonld")
    if not SCRAPER_API_KEY:
        err.error = (
            "SCRAPER_API_KEY is not configured. "
            "Set it in .env to enable SHEIN extraction."
        )
    else:
        err.error = (
            "Could not extract product data from this SHEIN page. "
            "The page may not contain JSON-LD schema data, or ScraperAPI "
            "returned a bot-wall page. Please enter product details manually."
        )
    return err.to_dict()


# ---------------------------------------------------------------------------
# CLI runner (for direct testing)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Test SHEIN product extraction (ScraperAPI + JSON-LD)."
    )
    parser.add_argument(
        "url",
        nargs="?",
        default="https://www.sheinindia.in/shein-shein-drop-shoulder-spread-collar-short-shirt/p/443339230_white",
        help="SHEIN product URL to extract",
    )
    parser.add_argument(
        "--username",
        default="test",
        help="Username — sets the output HTML filename (default: test → test_shein.html)",
    )
    args = parser.parse_args()

    import json
    data = scrape_shein(args.url, username=args.username)
    print(json.dumps(data, indent=2, ensure_ascii=False))
