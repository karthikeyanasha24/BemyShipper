"""
Walmart Product Scraper — ScraperAPI Structured endpoint
=========================================================
Endpoint: https://api.scraperapi.com/structured/walmart/product/v1
Params:   api_key, product_id (Walmart item ID, digits only)

Usage:
    from walmart_scraper import scrape_walmart
    result = scrape_walmart("https://www.walmart.com/ip/.../5253396052", SCRAPER_API_KEY)
"""

import re
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

SCRAPERAPI_WALMART_URL = "https://api.scraperapi.com/structured/walmart/product/v1"


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def extract_walmart_product_id(url: str) -> Optional[str]:
    """
    Extract Walmart product ID from various URL formats.
    Examples:
      https://www.walmart.com/ip/Galaxy-S24/5253396052           → 5253396052
      https://www.walmart.com/ip/5253396052                      → 5253396052
      https://www.walmart.com/ip/Galaxy-S24/5253396052?some=q   → 5253396052
    """
    # /ip/slug/DIGITS  or  /ip/DIGITS (6+ digits)
    m = re.search(r"/ip/(?:[^/?#]+/)?(\d{6,})", url)
    if m:
        return m.group(1)
    # ?itemId=DIGITS or ?item_id=DIGITS
    m = re.search(r"[?&](?:itemId|item_id|id)=(\d{6,})", url, re.I)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------

def scrape_walmart(url: str, api_key: str) -> dict:
    """
    Scrape Walmart product via ScraperAPI Structured Data endpoint.

    Returns a normalised dict matching the shape used by Amazon / eBay / Zara
    scrapers so main.py can return it directly.
    """
    product_id = extract_walmart_product_id(url)
    if not product_id:
        return {
            "success": False,
            "site": "walmart",
            "error": (
                "Could not extract Walmart product ID from URL. "
                "Expected format: walmart.com/ip/product-name/PRODUCT_ID"
            ),
        }

    logger.info("Walmart ScraperAPI: product_id=%s url=%s", product_id, url[:80])

    try:
        resp = requests.get(
            SCRAPERAPI_WALMART_URL,
            params={"api_key": api_key, "product_id": product_id},
            timeout=60,
        )
    except Exception as exc:
        logger.warning("Walmart ScraperAPI request error: %s", exc)
        return {"success": False, "site": "walmart", "error": f"Request error: {exc}"}

    if resp.status_code >= 400:
        logger.warning(
            "Walmart ScraperAPI HTTP %s: %s", resp.status_code, resp.text[:300]
        )
        return {
            "success": False,
            "site": "walmart",
            "error": f"ScraperAPI returned HTTP {resp.status_code}. Check your API key.",
        }

    try:
        data = resp.json()
    except Exception:
        return {
            "success": False,
            "site": "walmart",
            "error": "Non-JSON response from ScraperAPI Walmart endpoint.",
        }

    if not data or not data.get("product_name"):
        return {
            "success": False,
            "site": "walmart",
            "error": "No product data returned for this Walmart URL.",
        }

    # ── Offers / availability / price ─────────────────────────────────────────
    offers = data.get("offers") or []
    offer = offers[0] if offers else {}

    avail_raw = str(offer.get("availability") or "")
    if "InStock" in avail_raw or "in_stock" in avail_raw.lower():
        availability = "In Stock"
    elif "OutOfStock" in avail_raw or "out_of_stock" in avail_raw.lower():
        availability = "Out of Stock"
    elif avail_raw:
        availability = avail_raw.replace("Condition", "").strip()
    else:
        availability = "Check listing"

    price = data.get("price")
    if price is None:
        price = offer.get("price")

    # ── Image ─────────────────────────────────────────────────────────────────
    # Try multiple fields: 'image', 'thumbnail', first entry of 'images' list
    img = (
        data.get("image")
        or data.get("thumbnail")
        or (data.get("images") or [None])[0]
    )
    images = [img] if img else []

    # ── Description / bullets ─────────────────────────────────────────────────
    desc = str(data.get("product_description") or "").strip()
    bullets: list = []
    if desc:
        # Split long description into sensible chunks for the bullet list
        sentences = re.split(r"(?<=[.!?])\s+", desc)
        bullets = [s.strip() for s in sentences if len(s.strip()) > 20][:6]
        if not bullets:
            bullets = [desc[:300]]

    # ── Spec dict ─────────────────────────────────────────────────────────────
    spec_dict: dict = {}
    if data.get("brand"):
        spec_dict["Brand"] = data["brand"]

    # ── Variants (Walmart rarely returns structured variants) ─────────────────
    variants: list = []

    return {
        # ── Standard fields ───────────────────────────────────────────────────
        "success": True,
        "site": "walmart",
        "asin": product_id,
        "title": data.get("product_name"),
        "price": str(price) if price is not None else None,
        "currency": "USD",
        "main_image": img,
        "variants": variants,
        "rating": None,
        "review_count": None,
        "availability": availability,
        "extraction_method": "scraperapi-walmart",
        "error": None,
        # ── Rich fields ───────────────────────────────────────────────────────
        "images": images,
        "high_res_images": images,
        "feature_bullets": bullets,
        "product_information": spec_dict,
        "description": desc or None,
        "brand": data.get("brand"),
        "reviews": [],
    }
