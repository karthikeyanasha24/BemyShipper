"""
eBay Product Scraper — ScraperAPI Structured endpoint
=====================================================
Endpoint: https://api.scraperapi.com/structured/ebay/product/v1
Params:   api_key, product_id (eBay item ID, digits only)

Usage:
    from ebay_scraper import scrape_ebay
    result = scrape_ebay("https://www.ebay.com/itm/145757054266", SCRAPER_API_KEY)
"""

import re
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

SCRAPERAPI_EBAY_URL = "https://api.scraperapi.com/structured/ebay/product/v1"


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def extract_ebay_item_id(url: str) -> Optional[str]:
    """
    Extract eBay item ID from various URL formats.
    Examples:
      https://www.ebay.com/itm/145757054266?...           → 145757054266
      https://www.ebay.com/itm/145757054266               → 145757054266
      https://www.ebay.co.uk/itm/145757054266?...         → 145757054266
    """
    # /itm/DIGITS (9+ digits to avoid false matches)
    m = re.search(r"/itm/(\d{9,})", url)
    if m:
        return m.group(1)
    # Query param ?item=DIGITS or ?itemId=DIGITS
    m = re.search(r"[?&](?:item|itemId|iid)=(\d{9,})", url, re.I)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------

def scrape_ebay(url: str, api_key: str) -> dict:
    """
    Scrape eBay product via ScraperAPI Structured Data endpoint.

    Returns a normalised dict matching the shape used by Amazon / Zara / SHEIN
    scrapers so main.py can return it directly.
    """
    item_id = extract_ebay_item_id(url)
    if not item_id:
        return {
            "success": False,
            "site": "ebay",
            "error": (
                "Could not extract eBay item ID from URL. "
                "Expected format: ebay.com/itm/ITEM_ID"
            ),
        }

    logger.info("eBay ScraperAPI: item_id=%s url=%s", item_id, url[:80])

    try:
        resp = requests.get(
            SCRAPERAPI_EBAY_URL,
            params={"api_key": api_key, "product_id": item_id},
            timeout=60,
        )
    except Exception as exc:
        logger.warning("eBay ScraperAPI request error: %s", exc)
        return {"success": False, "site": "ebay", "error": f"Request error: {exc}"}

    if resp.status_code >= 400:
        logger.warning(
            "eBay ScraperAPI HTTP %s: %s", resp.status_code, resp.text[:300]
        )
        return {
            "success": False,
            "site": "ebay",
            "error": f"ScraperAPI returned HTTP {resp.status_code}. Check your API key.",
        }

    try:
        data = resp.json()
    except Exception:
        return {
            "success": False,
            "site": "ebay",
            "error": "Non-JSON response from ScraperAPI eBay endpoint.",
        }

    if not data or not data.get("title"):
        return {
            "success": False,
            "site": "ebay",
            "error": "No product data returned for this eBay listing.",
        }

    # ── Price ──────────────────────────────────────────────────────────────────
    price_info = data.get("price") or {}
    price_val = price_info.get("value")
    currency = price_info.get("currency") or "USD"

    # ── Images ─────────────────────────────────────────────────────────────────
    images = [img for img in (data.get("images") or []) if img]
    main_image = images[0] if images else None

    # ── item_specifics → spec dict + bullets ──────────────────────────────────
    item_specs = data.get("item_specifics") or []
    spec_dict: dict = {}
    for spec in item_specs:
        if not isinstance(spec, dict):
            continue
        label = str(spec.get("label") or "").strip()
        val = str(spec.get("value") or "").strip()
        if label and val and val not in ("—", "Does not apply", "N/A"):
            spec_dict[label] = val

    # ── Variants ──────────────────────────────────────────────────────────────
    variants = []
    color = data.get("color") or spec_dict.get("Color") or spec_dict.get("Colour")
    if color:
        variants.append({"group": "Color", "options": [color]})
    size = (
        spec_dict.get("Size")
        or spec_dict.get("Shoe Size")
        or spec_dict.get("US Shoe Size")
        or spec_dict.get("Clothing Size")
    )
    if size:
        variants.append({"group": "Size", "options": [size]})

    # ── Availability ──────────────────────────────────────────────────────────
    qty_str = str(data.get("available_quantity") or "")
    availability = "In Stock" if qty_str else "Check listing"

    # ── Shipping ──────────────────────────────────────────────────────────────
    ship_info = data.get("shipping_costs") or {}
    ship_cost = ship_info.get("value")
    ship_notes = str(data.get("shipping_notes") or "").strip()
    if ship_cost == 0:
        shipping_price = "FREE"
    elif ship_cost is not None:
        shipping_price = str(ship_cost)
    else:
        shipping_price = None

    # ── Seller ────────────────────────────────────────────────────────────────
    seller = data.get("seller") or {}

    # ── Feature bullets from specs ────────────────────────────────────────────
    bullets = [f"{k}: {v}" for k, v in list(spec_dict.items())[:12]]

    # ── Reviews ───────────────────────────────────────────────────────────────
    reviews = []
    for r in (data.get("reviews") or [])[:5]:
        if not isinstance(r, dict):
            continue
        reviews.append({
            "stars": r.get("stars"),
            "username": str(r.get("author") or "").replace(" by ", "").strip(),
            "title": str(r.get("title") or "").strip(),
            "review": str(r.get("content") or "").strip(),
            "date": str(r.get("review_date") or "").strip(),
        })

    return {
        # ── Standard fields ───────────────────────────────────────────────────
        "success": True,
        "site": "ebay",
        "asin": item_id,              # re-using 'asin' field as generic product_id
        "title": data.get("title"),
        "price": str(price_val) if price_val is not None else None,
        "currency": currency,
        "main_image": main_image,
        "variants": variants,
        "rating": str(data["rating"]) if data.get("rating") is not None else None,
        "review_count": str(data["review_count"]) if data.get("review_count") else None,
        "availability": availability,
        "extraction_method": "scraperapi-ebay",
        "error": None,
        # ── Rich fields ───────────────────────────────────────────────────────
        "images": images,
        "high_res_images": images,
        "feature_bullets": bullets,
        "product_information": spec_dict,
        "shipping_price": shipping_price,
        "shipping_notes": ship_notes or None,
        "sold_by": seller.get("name"),
        "seller_review": seller.get("seller_review"),
        "condition": data.get("condition"),
        "brand": data.get("brand"),
        "model": data.get("model"),
        "description": None,
        "reviews": reviews,
    }
