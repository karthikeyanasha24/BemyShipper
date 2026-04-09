"""
Zara Product Scraper
=====================
Strategy:
  Layer 1 → Intercept Zara's internal XHR API call via Playwright
            (most reliable — gets structured JSON directly)
  Layer 2 → Parse rendered HTML as fallback

Zara URL format:
  https://www.zara.com/{country}/{lang}/{product-slug}-p{product-id}.html

Zara is fully JS-rendered — simple HTTP + BeautifulSoup won't work.
Playwright is required for both layers.
"""

import os
import re
import json
import logging
import random
import sys
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
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


# ---------------------------------------------------------------------------
# Result dataclass (same shape as Amazon's ProductData)
# ---------------------------------------------------------------------------
@dataclass
class ProductData:
    title: Optional[str] = None
    price: Optional[str] = None
    currency: Optional[str] = None
    main_image: Optional[str] = None
    variants: list = field(default_factory=list)
    asin: Optional[str] = None          # used as generic product_id field
    rating: Optional[str] = None
    review_count: Optional[str] = None
    availability: Optional[str] = None
    source: str = "playwright"
    success: bool = False
    error: Optional[str] = None
    # Merged into API/UI responses (Apify datasaurus rich payload, etc.)
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        base = {
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
        if self.extras:
            base.update(self.extras)
        return base


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def extract_product_id(url: str) -> Optional[str]:
    """Extract Zara product ID from URL (the digits after -p)."""
    match = re.search(r"-p(\d{5,12})(?:\.html|$|\?)", url, re.I)
    return match.group(1) if match else None


def extract_country_lang(url: str) -> tuple:
    """
    Extract (country, lang) from Zara URL.
    e.g. https://www.zara.com/us/en/... → ('us', 'en')
    """
    match = re.search(r"zara\.com/([a-z]{2})/([a-z]{2})/", url, re.I)
    if match:
        return match.group(1).lower(), match.group(2).lower()
    return "us", "en"


def _zara_locale_and_timezone(url: str) -> tuple:
    """Match Playwright locale/timezone to storefront (helps with regional WAF / cookies)."""
    country, lang = extract_country_lang(url)
    loc = f"{lang}-{country.upper()}"
    tz_by_country = {
        "in": "Asia/Kolkata",
        "us": "America/New_York",
        "gb": "Europe/London",
        "ie": "Europe/Dublin",
        "au": "Australia/Sydney",
        "jp": "Asia/Tokyo",
        "kr": "Asia/Seoul",
        "cn": "Asia/Shanghai",
        "mx": "America/Mexico_City",
        "br": "America/Sao_Paulo",
        "es": "Europe/Madrid",
        "fr": "Europe/Paris",
        "it": "Europe/Rome",
        "de": "Europe/Berlin",
        "nl": "Europe/Amsterdam",
        "pl": "Europe/Warsaw",
        "tr": "Europe/Istanbul",
        "ae": "Asia/Dubai",
    }
    return loc, tz_by_country.get(country, "Europe/Madrid")


def _is_zara_bot_wall_title(title: Optional[str]) -> bool:
    """Titles WAF / Akamai pages use instead of a product name."""
    if not title:
        return False
    t = title.strip().lower()
    needles = (
        "access denied",
        "access forbidden",
        "forbidden",
        "403",
        "request blocked",
        "you don't have permission",
        "attention required",
        "just a moment",
        "enable javascript",
        "verify you are human",
    )
    return any(n in t for n in needles)


def _zara_html_suggests_block(html: str) -> Optional[str]:
    """Detect bot wall HTML before parsing (avoid returning block page as product)."""
    if not html or len(html) < 300:
        return "empty or truncated response"
    low = html.lower()
    has_pdp = "product-detail-view" in low or "product-detail-info__header-name" in low
    has_ld = "application/ld+json" in low and bool(
        re.search(r'"@type"\s*:\s*"Product"', html, re.I)
    )
    if has_pdp or has_ld:
        return None
    block_snips = (
        "access denied",
        "request blocked",
        "errors.edgesuite.net",
        "akamai",
        "you don't have permission to access",
    )
    for s in block_snips:
        if s in low:
            return f"block marker: {s!r}"
    return None


def _normalize_zara_page_url(u: str) -> str:
    """Strip query + fragment for matching JSON-LD offers.url to the current page."""
    if not u:
        return ""
    u = u.strip().split("#")[0]
    if "?" in u:
        u = u.split("?")[0]
    return u.rstrip("/").lower()


def _currency_symbol(code: Optional[str]) -> str:
    if not code:
        return "$"
    c = str(code).strip().upper()
    return {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}.get(c, c if len(c) <= 3 else "$")


def _json_ld_offer_url(product: dict) -> str:
    off = product.get("offers")
    if isinstance(off, dict):
        return str(off.get("url") or "")
    if isinstance(off, list) and off:
        o0 = off[0]
        if isinstance(o0, dict):
            return str(o0.get("url") or "")
    return ""


def _json_ld_product_image(product: dict) -> Optional[str]:
    img = product.get("image")
    if isinstance(img, list) and img:
        img = img[0]
    if isinstance(img, str) and img:
        return img
    return None


def _is_usable_zara_image_url(src: str) -> bool:
    if not src:
        return False
    s = src.lower()
    if "transparent-background" in s:
        return False
    return "static.zara.net" in s


def _collect_json_ld_products(soup) -> list:
    """Zara often uses a top-level JSON array of Product objects (see zara_2.html)."""
    found = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = (script.string or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    found.append(item)
        elif isinstance(data, dict) and data.get("@type") == "Product":
            found.append(data)
    return found


def _apply_json_ld_bundle(result: "ProductData", ld_products: list, page_url: str) -> None:
    """Fill title, price, currency, image, variants from one or many Product nodes."""
    if not ld_products:
        return
    page_key = _normalize_zara_page_url(page_url)
    same_page = [
        p
        for p in ld_products
        if page_key and _normalize_zara_page_url(_json_ld_offer_url(p)) == page_key
    ]
    use = same_page if same_page else ld_products

    first = use[0]
    if not result.title and first.get("name"):
        result.title = str(first["name"]).strip()

    # Price + currency from first matching offer
    off = first.get("offers")
    price_val = None
    cur_code = None
    if isinstance(off, dict):
        price_val = off.get("price")
        cur_code = off.get("priceCurrency")
    elif isinstance(off, list) and off and isinstance(off[0], dict):
        price_val = off[0].get("price")
        cur_code = off[0].get("priceCurrency")

    if price_val is not None and not result.price:
        result.price = str(price_val).replace(",", "")
    if cur_code and not result.currency:
        result.currency = _currency_symbol(cur_code)

    if not result.main_image:
        for p in use:
            u = _json_ld_product_image(p)
            if u and _is_usable_zara_image_url(u):
                result.main_image = u
                break

    # Aggregate Color / Size across sibling @Product entries (kid SKUs, etc.)
    colors = []
    sizes = []
    seen_c, seen_s = set(), set()
    for p in use:
        c = p.get("color")
        if c and str(c).strip() and str(c).strip() not in seen_c:
            seen_c.add(str(c).strip())
            colors.append(str(c).strip())
        s = p.get("size")
        if s and str(s).strip() and str(s).strip() not in seen_s:
            seen_s.add(str(s).strip())
            sizes.append(str(s).strip())

    variants = list(result.variants) if result.variants else []
    if colors:
        variants.append({"group": "Color", "options": colors})
    if sizes:
        variants.append({"group": "Size", "options": sizes})
    result.variants = variants

    # Availability hint
    avail = ""
    off0 = off if isinstance(off, dict) else (off[0] if isinstance(off, list) and off else {})
    if isinstance(off0, dict):
        a = str(off0.get("availability") or "")
        if "InStock" in a:
            avail = "In Stock"
        elif "OutOfStock" in a:
            avail = "Out of Stock"
    if avail and not result.availability:
        result.availability = avail


def _parse_price(raw_price) -> tuple:
    """
    Zara stores price as integer cents (e.g. 2995 = $29.95).
    Returns (price_str, currency_symbol).
    """
    if raw_price is None:
        return None, "$"
    try:
        val = int(raw_price)
        return f"{val / 100:.2f}", "$"
    except (TypeError, ValueError):
        # Already formatted string
        txt = str(raw_price).strip()
        match = re.search(r"[\d,.]+", txt)
        if match:
            return match.group(0).replace(",", ""), "$"
        return txt, "$"


# ---------------------------------------------------------------------------
# Layer 1: Parse Zara product JSON (from intercepted API or embedded script)
# ---------------------------------------------------------------------------
def _parse_zara_json(data: dict, url: str) -> Optional[ProductData]:
    """
    Parse a Zara product JSON blob into ProductData.
    Handles both the /detail.json API format and embedded window data.
    """
    result = ProductData(source="zara-api")

    # Product ID
    result.asin = str(data.get("id", "") or data.get("productId", "") or extract_product_id(url) or "")

    # Title — try multiple field names
    result.title = (
        data.get("name")
        or data.get("displayName")
        or data.get("productName")
        or data.get("seoProductName")
    )
    if result.title:
        result.title = result.title.strip()

    # Price
    raw_price = (
        data.get("price")
        or data.get("displayPrice")
        or data.get("originalPrice")
        or (data.get("prices") or [{}])[0].get("value") if isinstance(data.get("prices"), list) else None
    )
    result.price, result.currency = _parse_price(raw_price)
    pc = data.get("priceCurrency") or data.get("currency")
    if pc:
        result.currency = _currency_symbol(str(pc))

    # Main image — try detail.json media structure first
    media = data.get("media") or data.get("xmedia") or []
    if isinstance(media, list) and media:
        # Find first IMAGE type with highest resolution
        for m in media:
            if isinstance(m, dict):
                mtype = (m.get("type") or m.get("mediaType") or "").upper()
                if "IMAGE" in mtype or not mtype:
                    path = m.get("path") or m.get("url") or ""
                    if path:
                        # Build full URL if relative
                        if not path.startswith("http"):
                            path = f"https://static.zara.net/photos/{path}/w/750/aspect_ratio=1.jpg"
                        result.main_image = path
                        break

    # Variants — colors and sizes
    variants = []
    colors = data.get("detail", {}).get("colors") if isinstance(data.get("detail"), dict) else None
    if not colors:
        colors = data.get("colors") or data.get("variants") or []

    if isinstance(colors, list):
        color_names = []
        size_names_all = set()
        for color in colors:
            if not isinstance(color, dict):
                continue
            cname = color.get("name") or color.get("id") or ""
            if cname:
                color_names.append(str(cname).strip())

            # Collect images from first color if not already found
            if not result.main_image:
                xmedia = color.get("xmedia") or color.get("media") or []
                for m in xmedia:
                    if isinstance(m, dict):
                        path = m.get("path") or m.get("url") or ""
                        if path:
                            if not path.startswith("http"):
                                path = f"https://static.zara.net/photos/{path}/w/750/aspect_ratio=1.jpg"
                            result.main_image = path
                            break

            # Sizes per color
            sizes = color.get("sizes") or color.get("sizeGroups") or []
            if isinstance(sizes, list):
                for s in sizes:
                    if isinstance(s, dict):
                        sname = s.get("name") or s.get("id") or ""
                        avail = s.get("availability") or s.get("stock") or ""
                        if sname and str(avail).upper() not in ("OUT_OF_STOCK", "0", "DISABLED"):
                            size_names_all.add(str(sname).strip())

        if color_names:
            variants.append({"group": "Color", "options": color_names})
        if size_names_all:
            variants.append({"group": "Size", "options": sorted(size_names_all)})

    result.variants = variants

    # Availability
    avail_raw = data.get("availability") or data.get("stockLevel") or ""
    if avail_raw:
        result.availability = str(avail_raw).replace("_", " ").title()
    elif variants:
        result.availability = "In Stock"

    result.success = bool(result.title)
    if not result.success:
        result.error = "Could not extract title from Zara API response"

    return result


# ---------------------------------------------------------------------------
# Layer 2: HTML fallback selectors for rendered Zara page
# ---------------------------------------------------------------------------
def _parse_zara_html(html: str, url: str) -> ProductData:
    """Parse Zara rendered HTML when API interception fails (matches current zara.com DOM)."""
    from bs4 import BeautifulSoup

    result = ProductData(source="zara-html")
    soup = BeautifulSoup(html, "html.parser")

    result.asin = extract_product_id(url)

    # JSON-LD first: Zara uses either one Product or an array (multi-SKU PDP — zara_2.html).
    ld_products = _collect_json_ld_products(soup)
    _apply_json_ld_bundle(result, ld_products, url)

    # Title — DOM (reliable; JSON-LD name can be long/SEO)
    for sel in [
        "h1.product-detail-info__header-name",
        "[data-qa-qualifier='product-detail-info-name']",
        "[class*='product-detail-info__header-name']",
        "[class*='product-detail__name']",
        "h1[class*='product']",
        "h1",
    ]:
        el = soup.select_one(sel)
        if el and el.text.strip():
            result.title = el.text.strip()
            break

    # Price from visible PDP (e.g. money-amount__main — zara.html)
    for sel in [
        "[data-qa-qualifier='price-amount-current']",
        ".money-amount__main",
        "[class*='money-amount__main']",
        "[class*='price__amount']",
        "[class*='price-current']",
    ]:
        el = soup.select_one(sel)
        if el and el.text.strip():
            txt = el.text.strip()
            match = re.search(r"[\d,.]+", txt)
            if match:
                result.price = match.group(0).replace(",", "")
                if "₹" in txt or "Rs" in txt or "INR" in txt.upper():
                    result.currency = "₹"
                elif "$" in txt:
                    result.currency = "$"
                elif "€" in txt:
                    result.currency = "€"
                elif "£" in txt:
                    result.currency = "£"
                elif not result.currency:
                    result.currency = "$"
                break

    # Main image — skip lazy transparent placeholder (zara uses it in picture/src)
    for img in soup.select(
        "picture img.media-image__image, img.media-image__image, "
        "img[class*='media-image'], .product-detail-image img"
    ):
        src = (img.get("src") or img.get("data-src") or "").strip()
        if _is_usable_zara_image_url(src):
            result.main_image = src
            break

    # DOM variants: volume (perfume — zara.html) and colors (apparel — zara_2.html)
    variant_groups = {v["group"]: v["options"] for v in (result.variants or []) if v.get("group")}

    vol_labels = []
    for btn in soup.select(
        "button.product-detail-volume-selector__volume-button, "
        "ul.product-detail-volume-selector__volumes button"
    ):
        t = btn.get_text(strip=True)
        if t and t not in vol_labels:
            vol_labels.append(t)
    if vol_labels:
        variant_groups["Volume"] = vol_labels

    color_labels = []
    for li in soup.select(".product-detail-color-selector__colors li"):
        sr = li.select_one(".screen-reader-text")
        if sr:
            t = sr.get_text(strip=True)
            if t and t not in color_labels:
                color_labels.append(t)
    if color_labels:
        # Prefer DOM color names when JSON-LD also listed colors (keep DOM order)
        variant_groups["Color"] = color_labels

    ordered = [
        ("Color", variant_groups.get("Color")),
        ("Size", variant_groups.get("Size")),
        ("Volume", variant_groups.get("Volume")),
    ]
    # Perfume PDPs: JSON-LD includes size "ONE SIZE ONLY" while real choice is Volume — drop noise.
    if variant_groups.get("Volume"):
        sz = variant_groups.get("Size")
        if sz == ["ONE SIZE ONLY"] or sz == ["One Size Only"]:
            ordered = [(g, o) for g, o in ordered if g != "Size"]

    result.variants = [{"group": g, "options": opts} for g, opts in ordered if opts]

    if result.title and _is_zara_bot_wall_title(result.title):
        result.error = (
            "Zara served a bot-protection page (e.g. Access Denied), not the product. "
            "Try another network/VPN or open the URL in a normal browser."
        )
        result.title = None
        result.price = None
        result.main_image = None
        result.variants = []
        result.success = False
        return result

    result.success = bool(result.title)
    if not result.success:
        result.error = "Could not extract title from Zara HTML"

    return result


# ---------------------------------------------------------------------------
# Main Zara scraper — Playwright + network interception
# ---------------------------------------------------------------------------
def _scrape_zara_playwright_impl(url: str) -> dict:
    """
    Playwright run (must execute off the Uvicorn thread on Windows — same as Amazon).
    """
    _prepare_asyncio_for_playwright_thread()

    result = ProductData(source="playwright")

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        result.error = "Playwright not installed. Run: pip install playwright && playwright install chromium"
        return result.to_dict()

    captured_product_json: list = []   # mutable container for closure

    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    locale, timezone_id = _zara_locale_and_timezone(url)
    country, lang = extract_country_lang(url)
    nav_langs = f"{locale},{lang};q=0.9,en;q=0.8"

    # Classic `--headless` is easy for CDNs (e.g. Akamai) to fingerprint vs a real window.
    # Playwright switches to Chrome's `--headless=new` when this env var is set (see chromium.js).
    _prev_pw_headless_new = os.environ.get("PLAYWRIGHT_CHROMIUM_USE_HEADLESS_NEW")
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
                ],
            )
            context = browser.new_context(
                user_agent=UA,
                locale=locale,
                timezone_id=timezone_id,
                viewport={"width": 1440, "height": 900},
                color_scheme="light",
                java_script_enabled=True,
            )

            lang_array = (
                "['en-IN','en','en-US']"
                if country == "in"
                else "['en-US','en']"
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                "Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});"
                f"Object.defineProperty(navigator, 'languages', {{get: () => {lang_array}}});"
                "window.chrome = { runtime: {} };"
            )

            page = context.new_page()
            page.set_extra_http_headers({
                "Accept-Language": nav_langs,
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

            # ── Intercept Zara's product API responses ────────────────────────
            def handle_response(response):
                url_r = response.url
                if response.status != 200:
                    return
                # Zara catalog APIs (paths evolve; keep hints broad).
                hints = (
                    "itxrest",
                    "detail.json",
                    "/product/",
                    "catalog/store",
                    "product-detail",
                    "product?",
                    "products?",
                    "/experience/",
                    "ecom",
                )
                if not any(kw in url_r for kw in hints):
                    return
                ct = (response.headers.get("content-type") or "").lower()
                looks_json = "json" in ct or url_r.endswith(".json") or "itxrest" in url_r
                if not looks_json:
                    return
                try:
                    body = response.json()
                    captured_product_json_local.append(body)
                    logger.info("Intercepted Zara API response from: %s", url_r)
                except Exception:
                    pass

            page.on("response", handle_response)

            parsed_u = urlparse(url)
            origin = f"{parsed_u.scheme}://{parsed_u.netloc}"

            # Cookie / session warm-up (same idea as Amazon Playwright path).
            logger.info("Playwright: pre-warming Zara origin %s …", origin)
            page.goto(f"{origin}/", wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(random.randint(1800, 3200))

            page.set_extra_http_headers({
                "Accept-Language": nav_langs,
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

            logger.info("Playwright: loading Zara product URL…")
            page.goto(url, wait_until="domcontentloaded", timeout=35000)

            try:
                page.wait_for_selector(
                    "h1, [class*='product-detail'], [class*='product-name']",
                    timeout=12000,
                )
            except PWTimeout:
                logger.warning("Zara: timeout waiting for product content selector")

            page.wait_for_timeout(4000)

            page_html = page.content()
            browser.close()
            return page_html, captured_product_json_local

    try:
        page_html, captured_product_json = _run_playwright(headless=True)

        block_hint = _zara_html_suggests_block(page_html)
        if block_hint:
            logger.warning("Zara bot / WAF page detected (%s) — headless only, not retrying headed", block_hint)
            blocked = ProductData(source="playwright")
            blocked.error = (
                f"Zara blocked automated access ({block_hint}). "
                "Try another network or residential IP."
            )
            return blocked.to_dict()

        # ── Try to parse from intercepted JSON ────────────────────────────────
        if captured_product_json:
            logger.info("Trying %d intercepted JSON responses…", len(captured_product_json))
            for blob in reversed(captured_product_json):  # latest first
                parsed = _try_parse_any_zara_blob(blob, url)
                if parsed and parsed.success:
                    logger.info("Successfully parsed Zara product from API JSON.")
                    return parsed.to_dict()

        # ── HTML fallback ─────────────────────────────────────────────────────
        logger.info("Zara: API interception failed — falling back to HTML parsing.")
        result = _parse_zara_html(page_html, url)

    except Exception as e:
        detail = _format_exception(e)
        result.error = f"Zara Playwright error: {detail}"
        logger.exception("Zara Playwright failed — %s", detail)
    finally:
        if _prev_pw_headless_new is None:
            os.environ.pop("PLAYWRIGHT_CHROMIUM_USE_HEADLESS_NEW", None)
        else:
            os.environ["PLAYWRIGHT_CHROMIUM_USE_HEADLESS_NEW"] = _prev_pw_headless_new

    return result.to_dict()


# Default: Apify Store "Zara" by datasaurus — rich PDP when scrape_product_page=true
# Override: APIFY_ZARA_ACTOR=easyapi~zara-product-scraper (legacy input shape)
_APIFY_ZARA_ACTOR_DEFAULT = "datasaurus~zara"
_APIFY_RUN_SYNC = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"


def _apify_zara_actor_id() -> str:
    return (os.getenv("APIFY_ZARA_ACTOR") or _APIFY_ZARA_ACTOR_DEFAULT).strip()


def _fix_zara_media_url(u: Optional[str]) -> Optional[str]:
    if not u:
        return None
    return str(u).replace("{width}", "750")


def _strip_html_simple(text: str) -> str:
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", str(text))
    return " ".join(t.split()).strip()


def _zara_country_from_url(url: str) -> str:
    m = re.search(r"zara\.com/([a-z]{2})/", url, re.I)
    return (m.group(1) or "us").lower() if m else "us"


def _format_zara_price_display(price, country: str) -> tuple[Optional[str], str]:
    """Datasaurus returns integer prices (typically minor units like cents/paise)."""
    if price is None:
        return None, ""
    try:
        p = int(price)
    except (TypeError, ValueError):
        return str(price), ""
    c = country.lower()
    # Minor-unit storefronts (pence, cents, euro cents, paise)
    if c in (
        "in", "uk", "us", "ie", "ca", "au", "es", "fr", "de", "it", "nl", "pt", "pl",
        "se", "be", "at", "ch", "mx", "kr", "jp", "ae", "sa", "tr", "cz", "ro",
    ):
        # Return ISO 4217 codes so NestJS / frontend don't need to re-normalise
        iso = {
            "in": "INR",
            "uk": "GBP", "us": "USD", "ie": "EUR", "ca": "CAD", "au": "AUD",
            "mx": "MXN", "es": "EUR", "fr": "EUR", "de": "EUR", "it": "EUR",
            "nl": "EUR", "pt": "EUR", "be": "EUR", "at": "EUR",
            "pl": "PLN", "se": "SEK", "cz": "CZK", "ro": "RON",
            "ae": "AED", "sa": "SAR", "tr": "TRY",
        }.get(c, "EUR")
        return f"{p / 100:.2f}", iso
    return str(p), ""


def _pick_datasaurus_item(items: list, url: str) -> Optional[dict]:
    """Pick dataset row matching the pasted product URL (v1= color id or product page)."""
    if not items:
        return None
    v1 = None
    m = re.search(r"[?&]v1=(\d+)", url)
    if m:
        v1 = m.group(1)
    norm = _normalize_zara_page_url(url)
    slug_hint = norm.split("/")[-1] if norm else ""

    for it in items:
        if not isinstance(it, dict):
            continue
        if v1:
            if str(it.get("id", "")) == v1:
                return it
            for c in it.get("colorsSizesImagesJSON") or []:
                if isinstance(c, dict) and str(c.get("productId", "")) == v1:
                    return it
        pp = (it.get("productPage") or "").lower().rstrip("/")
        if pp and norm and norm in pp:
            return it
        if pp and slug_hint and slug_hint in pp:
            return it
    first = next((x for x in items if isinstance(x, dict)), None)
    return first


def _flatten_datasaurus_composition(dc: dict) -> dict:
    out: dict = {}
    for part in dc.get("parts") or []:
        if not isinstance(part, dict):
            continue
        label = str(part.get("description") or "composition").strip()
        comps = part.get("components") or []
        bits = []
        for c in comps:
            if isinstance(c, dict) and c.get("material"):
                bits.append(f"{c.get('material')}: {c.get('percentage', '')}".strip())
        if bits:
            key = label.lower().replace(" ", "_")[:40]
            out[key] = ", ".join(bits)
    return out


def _parse_datasaurus_zara_item(item: dict, url: str) -> Optional[ProductData]:
    """Map datasaurus/zara actor item → ProductData + extras for UI (gallery, specs, care)."""
    if not isinstance(item, dict):
        return None

    country = _zara_country_from_url(url)
    result = ProductData(source="apify-zara")

    raw_name = str(item.get("name") or "").strip()
    if raw_name:
        result.title = raw_name
    else:
        # Build title from available metadata when name is empty
        keyword = str(item.get("keyword") or "").strip().title()
        category = str(item.get("category") or "").strip().title()
        csj_raw = item.get("colorsSizesImagesJSON") or []
        first_color_name = ""
        if isinstance(csj_raw, list) and csj_raw and isinstance(csj_raw[0], dict):
            first_color_name = str(csj_raw[0].get("name") or "").strip().title()
        # Prefer keyword, then category, then "Zara {color}" or plain "Zara Product"
        if keyword:
            result.title = keyword
        elif category:
            result.title = f"Zara {category}"
        elif first_color_name:
            result.title = f"Zara Product ({first_color_name})"
        else:
            result.title = "Zara Product"
    result.asin = str(item.get("id") or item.get("reference") or extract_product_id(url) or "")

    price_raw = item.get("price")
    pstr, sym = _format_zara_price_display(price_raw, country)
    result.price = pstr
    result.currency = sym

    main = _fix_zara_media_url(item.get("mainImage"))
    if main:
        result.main_image = main

    # Variants — colors and sizes from summary strings OR colorsSizesImagesJSON detail
    variants: list = []
    csj = item.get("colorsSizesImagesJSON") or []

    # Colors: prefer top-level summary string; fall back to colorsSizesImagesJSON names
    color_str = str(item.get("colors") or "").strip()
    if color_str:
        color_opts = [x.strip() for x in color_str.split(",") if x.strip()]
    else:
        color_opts = [
            str(c["name"]) for c in (csj if isinstance(csj, list) else [])
            if isinstance(c, dict) and c.get("name")
        ]
    if color_opts:
        variants.append({"group": "Color", "options": color_opts})

    # Sizes: prefer top-level summary string; fall back to colorsSizesImagesJSON[0].sizes
    size_str = str(item.get("sizes") or "").strip()
    if size_str:
        size_opts = [x.strip() for x in size_str.split(",") if x.strip()]
    else:
        # Collect unique size names from the first colour's sizes list
        seen: list = []
        first_color = next(
            (c for c in (csj if isinstance(csj, list) else []) if isinstance(c, dict)), None
        )
        for sz in (first_color or {}).get("sizes") or []:
            if isinstance(sz, dict):
                name = str(sz.get("name") or "").strip()
                avail = str(sz.get("availability") or "").upper()
                if name and avail not in ("OUT_OF_STOCK", "DISABLED") and name not in seen:
                    seen.append(name)
        size_opts = seen
    if size_opts:
        variants.append({"group": "Size", "options": size_opts})

    result.variants = variants

    avail = item.get("availability")
    if isinstance(avail, str):
        result.availability = avail.replace("_", " ").title()
    elif avail is not None:
        result.availability = str(avail)

    gallery: list = []
    if main:
        gallery.append(main)
    for c in csj if isinstance(csj, list) else []:
        if not isinstance(c, dict):
            continue
        for xu in c.get("xmedia") or []:
            fu = _fix_zara_media_url(xu if isinstance(xu, str) else None)
            if fu and fu not in gallery:
                gallery.append(fu)
        pm = c.get("pdpMedia")
        if isinstance(pm, dict):
            ei = pm.get("extraInfo")
            deliv = ei.get("deliveryUrl") if isinstance(ei, dict) else None
            fu = _fix_zara_media_url(pm.get("url") or deliv)
            if fu and fu not in gallery:
                gallery.append(fu)

    desc = str(item.get("description") or "").strip()
    bullets: list = []
    if desc:
        bullets.append(desc)
    pc = item.get("productCare") or {}
    if isinstance(pc, dict):
        for line in pc.get("description") or []:
            t = _strip_html_simple(str(line))
            if t:
                bullets.append(t)
    origin = item.get("origin")
    if isinstance(origin, list):
        for o in origin:
            t = _strip_html_simple(str(o))
            if t:
                bullets.append(t)
    elif isinstance(origin, str) and origin.strip():
        bullets.append(origin.strip())

    spec_info: dict = {}
    spec_info.update(_flatten_datasaurus_composition(item.get("detailedComposition") or {}))
    if item.get("displayReference"):
        spec_info["display_reference"] = str(item["displayReference"])
    if item.get("reference"):
        spec_info["reference"] = str(item["reference"])
    if item.get("keyword"):
        spec_info["keyword"] = str(item["keyword"])
    if item.get("category"):
        spec_info["category"] = str(item["category"])
    if item.get("productPage"):
        spec_info["product_page"] = str(item["productPage"])
    if item.get("website"):
        spec_info["website"] = str(item["website"])

    result.extras = {
        "description": desc or None,
        "images": gallery,
        "high_res_images": gallery,
        "feature_bullets": bullets[:24],
        "product_information": spec_info,
        "colorsSizesImagesJSON": csj if isinstance(csj, list) else [],
        "productCare": pc if isinstance(pc, dict) else {},
        "zara_brand": item.get("brand"),
        "list_price": str(item["oldPrice"]) if item.get("oldPrice") is not None else None,
        "pricing": pstr,
    }

    # Consider success if we have at least a title OR a price — partial data is still useful
    result.success = bool(result.title or result.price)
    if not result.success:
        result.error = "Apify returned no usable data (no title or price) for this URL"
    elif not result.title:
        result.title = "Zara Product"   # placeholder so UI always has something to show
    return result


def _parse_apify_zara_item(item: dict, url: str) -> Optional[ProductData]:
    """
    Parse a single item from the Apify Zara actor dataset into ProductData.
    Field names vary across actor versions — we try all common variants.
    """
    if not isinstance(item, dict):
        return None

    if item.get("colorsSizesImagesJSON") is not None:
        return _parse_datasaurus_zara_item(item, url)

    result = ProductData(source="apify-zara")

    # ── Title ──────────────────────────────────────────────────────────────────
    result.title = str(
        item.get("name") or item.get("title") or
        item.get("productName") or item.get("displayName") or ""
    ).strip() or None

    # ── Product ID ─────────────────────────────────────────────────────────────
    pid = (
        item.get("id") or item.get("productId") or
        item.get("reference") or item.get("sku") or
        extract_product_id(url) or ""
    )
    result.asin = str(pid)

    # ── Price ──────────────────────────────────────────────────────────────────
    raw_price = (
        item.get("price") or item.get("currentPrice") or
        item.get("salePrice") or item.get("regularPrice") or
        item.get("priceFormatted") or item.get("priceValue")
    )
    if raw_price is not None:
        result.price, result.currency = _parse_price(raw_price)

    cur_code = item.get("currency") or item.get("priceCurrency") or item.get("currencyCode")
    if cur_code:
        result.currency = _currency_symbol(str(cur_code))

    # ── Main image ─────────────────────────────────────────────────────────────
    images_raw = item.get("images") or item.get("image") or item.get("photos") or []
    if isinstance(images_raw, list) and images_raw:
        first = images_raw[0]
        if isinstance(first, dict):
            img_url = first.get("url") or first.get("src") or first.get("href") or ""
        else:
            img_url = str(first)
        if img_url and _is_usable_zara_image_url(img_url):
            result.main_image = img_url
        elif img_url:
            result.main_image = img_url   # accept even if not static.zara.net
    elif isinstance(images_raw, str) and images_raw:
        result.main_image = images_raw

    # ── Variants ───────────────────────────────────────────────────────────────
    variants: list = []

    colors_raw = item.get("colors") or item.get("color") or item.get("colourOptions") or []
    if isinstance(colors_raw, list) and colors_raw:
        color_names = []
        for c in colors_raw:
            name = (c.get("name") or c.get("label") or c.get("id") or str(c)) if isinstance(c, dict) else str(c)
            if name and name not in color_names:
                color_names.append(str(name).strip())
        if color_names:
            variants.append({"group": "Color", "options": color_names})

    sizes_raw = item.get("sizes") or item.get("size") or item.get("sizeOptions") or []
    if isinstance(sizes_raw, list) and sizes_raw:
        size_names = []
        for s in sizes_raw:
            name = (s.get("name") or s.get("value") or s.get("label") or str(s)) if isinstance(s, dict) else str(s)
            avail = (s.get("availability") or s.get("available") or "") if isinstance(s, dict) else ""
            if name and str(avail).upper() not in ("OUT_OF_STOCK", "DISABLED", "FALSE", "0"):
                if name not in size_names:
                    size_names.append(str(name).strip())
        if size_names:
            variants.append({"group": "Size", "options": size_names})

    result.variants = variants

    # ── Availability ───────────────────────────────────────────────────────────
    avail_raw = item.get("availability") or item.get("available") or item.get("inStock")
    if avail_raw is not None:
        if isinstance(avail_raw, bool):
            result.availability = "In Stock" if avail_raw else "Out of Stock"
        else:
            result.availability = str(avail_raw).replace("_", " ").title()
    elif result.variants or result.price:
        result.availability = "In Stock"

    result.success = bool(result.title)
    if not result.success:
        result.error = "Apify Zara actor returned no title — check actor output for this URL"
    return result


def _scrape_zara_apify(url: str) -> Optional[ProductData]:
    """
    Apify Layer: default actor `datasaurus~zara` (rich PDP via scrape_product_page).

    Legacy: set APIFY_ZARA_ACTOR=easyapi~zara-product-scraper and the old startUrls payload is used.

    Requires APIFY_API_TOKEN in .env.
    PDP scrape can take 1–3+ minutes — timeout is generous.
    """
    if not APIFY_API_TOKEN:
        return None

    actor = _apify_zara_actor_id()
    api_url = _APIFY_RUN_SYNC.format(actor=actor)
    legacy = "easyapi" in actor.lower()

    if legacy:
        payload = {"startUrls": [{"url": url}]}
        timeout_s = 120
    else:
        # datasaurus~zara — scrape_product_page=False is faster and still returns
        # full colorsSizesImagesJSON (colors, sizes, images, price).
        payload = {
            "deduplicate_across_all_start_urls": True,
            "max_results": 5,
            "max_subcategories": 3,
            "scrape_product_page": False,
            "start_urls": [{"url": url}],
        }
        timeout_s = int(os.getenv("APIFY_ZARA_TIMEOUT", "180"))

    logger.info("Zara Apify: actor=%s url=%s", actor, url[:80])

    try:
        resp = requests.post(
            api_url,
            params={"token": APIFY_API_TOKEN},
            json=payload,
            timeout=timeout_s,
            headers={"Content-Type": "application/json"},
        )
    except Exception as exc:
        logger.warning("Zara Apify request error: %s", exc)
        return None

    if resp.status_code not in (200, 201):
        logger.warning("Zara Apify HTTP %s: %s", resp.status_code, resp.text[:300])
        return None

    try:
        items = resp.json()
    except Exception:
        logger.warning("Zara Apify: non-JSON response")
        return None

    if not items or not isinstance(items, list):
        logger.warning("Zara Apify: empty dataset returned")
        return None

    logger.info("Zara Apify: got %d item(s)", len(items))
    pick = _pick_datasaurus_item(items, url) if not legacy else items[0]
    if not pick:
        return None
    result = _parse_apify_zara_item(pick, url)
    if result and result.success:
        logger.info("Zara Apify: success — title=%r price=%r", result.title, result.price)
    return result


def _scrape_zara_scraperapi(url: str) -> Optional[ProductData]:
    """
    Layer 0: Attempt extraction via ScraperAPI (render_js=True).
    ScraperAPI handles Akamai/WAF/bot-detection at the infrastructure level.
    Returns a populated ProductData on success, or None to fall through to Playwright.
    """
    if not SCRAPER_API_KEY:
        return None

    country, _ = extract_country_lang(url)
    country_code = country if len(country) == 2 else "us"

    api_url = _build_scraperapi_url(url, render_js=True, country_code=country_code)
    logger.info("Zara ScraperAPI Layer 0: %s (country=%s)", url[:80], country_code)

    try:
        resp = requests.get(api_url, timeout=60)
        html = resp.text
    except Exception as e:
        logger.warning("Zara ScraperAPI request failed: %s", e)
        return None

    block = _zara_html_suggests_block(html)
    if block:
        logger.warning("Zara ScraperAPI response looks blocked (%s) — falling through to Playwright", block)
        return None

    result = _parse_zara_html(html, url)
    if result.success:
        result.source = "scraperapi"
        logger.info("Zara: ScraperAPI extraction successful (title=%r)", result.title)
        return result

    logger.info("Zara: ScraperAPI HTML parsed but incomplete — falling through to Playwright")
    return None


def scrape_zara(url: str, *, use_apify: bool = False) -> dict:
    """
    Scrape a Zara product page.

    Strategy: Apify datasaurus~zara actor only — no Playwright, no ScraperAPI.
    Actor config: scrape_product_page=False, max_results=5 (fast, returns full
    colorsSizesImagesJSON with sizes, colors, images).

    Returns a success=False error dict if APIFY_API_TOKEN is not set or the
    actor returns no data.
    """
    if not APIFY_API_TOKEN:
        err = ProductData(source="apify-zara")
        err.error = (
            "APIFY_API_TOKEN is not configured. "
            "Set it in .env to enable Zara extraction."
        )
        return err.to_dict()

    apify_result = _scrape_zara_apify(url)
    if apify_result and apify_result.success:
        return apify_result.to_dict()

    # Apify ran but returned no usable data
    err = ProductData(source="apify-zara")
    err.error = (
        apify_result.error
        if apify_result and apify_result.error
        else "Apify actor returned no data for this URL. Please enter product details manually."
    )
    return err.to_dict()


def _try_parse_any_zara_blob(blob, url: str) -> Optional[ProductData]:
    """
    Zara API responses come in several shapes — try each.
    """
    if isinstance(blob, list):
        for item in reversed(blob):
            if isinstance(item, dict):
                parsed = _try_parse_any_zara_blob(item, url)
                if parsed and parsed.success:
                    return parsed
        return None
    if not isinstance(blob, dict):
        return None

    # Shape 1: { product: { id, name, price, ... } }
    product = blob.get("product") or blob.get("productDetail")
    if isinstance(product, dict):
        parsed = _parse_zara_json(product, url)
        if parsed and parsed.title:
            return parsed

    # Shape 2: { id, name, price, ... } directly
    if blob.get("name") or blob.get("id"):
        parsed = _parse_zara_json(blob, url)
        if parsed and parsed.title:
            return parsed

    # Shape 3: list of products in a "products" key
    products = blob.get("products") or blob.get("productItems") or []
    if isinstance(products, list):
        pid = extract_product_id(url)
        for p in products:
            if not isinstance(p, dict):
                continue
            if pid and str(p.get("id", "")) == pid:
                parsed = _parse_zara_json(p, url)
                if parsed and parsed.title:
                    return parsed
        # No ID match — try first item
        if products and isinstance(products[0], dict):
            parsed = _parse_zara_json(products[0], url)
            if parsed and parsed.title:
                return parsed

    return None


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Test Zara product extraction (Playwright; runs headless — no browser window).",
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=(
            "https://www.zara.com/in/en/seoul-532-8-sinsa-dong-gangnam-gu-edt-100ml--3-04-fl--oz--"
            "p20210026.html?v1=495684754"
        ),
        help="Zara product page URL (default: Seoul EDT India PDP)",
    )
    cli_args = parser.parse_args()
    payload = scrape_zara(cli_args.url.strip())
    print(json.dumps(payload, indent=2, ensure_ascii=False))
