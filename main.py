"""
BeMyShipper — Product Extraction API  v3.5
============================================
Supports: Amazon, eBay, Walmart, Zara, SHEIN (auto-detected from URL)
Any other URL → caller should use manual mode (no scraping attempted).
Run: uvicorn main:app --reload --port 8000
Docs: http://127.0.0.1:8000/docs
"""

import logging
import re
import requests as _requests
import urllib.parse
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator

from scraper import AmazonScraper, extract_asin, ANTI_CAPTCHA_KEY, SCRAPER_API_KEY, APIFY_API_TOKEN
from zara_scraper import scrape_zara, _scrape_zara_apify
from shein_scraper import scrape_shein
from ebay_scraper import scrape_ebay, extract_ebay_item_id
from walmart_scraper import scrape_walmart, extract_walmart_product_id

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="BeMyShipper — Product Extractor",
    description=(
        "Extracts product data (title, price, image, variants) from e-commerce URLs.\n\n"
        "**Supported sites:** Amazon, Zara, SHEIN\n\n"
        "**Auto-fallback chain (all sites when SCRAPER_API_KEY is set):**\n"
        "0. ScraperAPI Layer 0 — JS render, bypasses WAF/CAPTCHA/Cloudflare *(2–8s)*\n\n"
        "**Amazon fallback (no ScraperAPI):**\n"
        "1. HTTP + cookie pre-warming *(1–2s)*\n"
        "2. Anti-Captcha solve if blocked *(15–30s)*\n"
        "3. Playwright stealth browser *(5–10s)*\n\n"
        "**Zara fallback:** Playwright with XHR interception *(5–12s)*\n\n"
        "**SHEIN:** ScraperAPI raw GET → save `{username}_shein.html` → JSON-LD parse *(~2–5s)*"
    ),
    version="3.2.0",
)

APP_DIR = Path(__file__).resolve().parent

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the test UI from the same service (works on Render).
# - UI:  /ui  (frontend.html)
# - Files: /ui/static (if you add assets later)
app.mount("/ui/static", StaticFiles(directory=str(APP_DIR), html=False), name="ui-static")

amazon_scraper = AmazonScraper(max_retries=3, api_key=ANTI_CAPTCHA_KEY)

SUPPORTED_SITES = [
    "amazon.", "ebay.", "walmart.com",
    "zara.com", "shein.com", "sheinindia.in", "sheglam.com",
]

# Sites that use API-only extraction (no Playwright / HTML scraping)
API_ONLY_SITES = {"ebay", "walmart"}


def detect_site(url: str) -> str:
    """
    Return one of: 'amazon' | 'ebay' | 'walmart' | 'zara' | 'shein' | 'unknown'.
    Only the first five trigger automatic API extraction.
    Anything else should use manual mode on the client side.
    """
    url_lower = url.lower()
    if "amazon." in url_lower:
        return "amazon"
    if "ebay." in url_lower:
        return "ebay"
    if "walmart.com" in url_lower:
        return "walmart"
    if "zara.com" in url_lower:
        return "zara"
    if "shein.com" in url_lower or "sheglam.com" in url_lower or "sheinindia.in" in url_lower:
        return "shein"
    return "unknown"


# ── Models ────────────────────────────────────────────────────────────────────
class ExtractRequest(BaseModel):
    url: str
    force_playwright: Optional[bool] = False
    username: Optional[str] = "user"
    """
    Username of the requesting user. Used to name the saved HTML file for
    SHEIN pages: html_cache/{username}_shein.html. Defaults to 'user'.
    """

    class Config:
        json_schema_extra = {
            "example": {
                "url": "https://www.amazon.com/dp/B09G9FPHY6",
                "force_playwright": False,
                "username": "john_doe",
            }
        }

    @validator("url")
    def validate_supported_url(cls, v):
        v = v.strip()
        if not v.startswith("http"):
            v = "https://" + v
        # Unknown sites are allowed — endpoint returns manual_mode: true
        return v


class ExtractResponse(BaseModel):
    success: bool
    site: Optional[str] = None
    asin: Optional[str] = None
    title: Optional[str] = None
    price: Optional[str] = None
    currency: Optional[str] = None
    main_image: Optional[str] = None
    variants: list = []
    rating: Optional[str] = None
    review_count: Optional[str] = None
    availability: Optional[str] = None
    extraction_method: Optional[str] = None
    error: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "site": "amazon",
                "asin": "B09G9FPHY6",
                "title": "Apple AirPods Pro (2nd Generation)",
                "price": "189.99",
                "currency": "$",
                "main_image": "https://m.media-amazon.com/images/I/61SUj2aKoEL._AC_SL1500_.jpg",
                "variants": [{"group": "Style", "options": ["With MagSafe Case"]}],
                "rating": "4.4",
                "review_count": "67,432 ratings",
                "availability": "In Stock",
                "extraction_method": "http",
                "error": None,
            }
        }


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health", tags=["System"])
def health_check():
    scraper_api_on = bool(SCRAPER_API_KEY)
    apify_on = bool(APIFY_API_TOKEN)
    anti_captcha_on = bool(ANTI_CAPTCHA_KEY)
    ok = "✅ configured"
    miss_scraper = "❌ needs SCRAPER_API_KEY"
    miss_apify = "❌ needs APIFY_API_TOKEN"
    return {
        "status": "ok",
        "service": "BeMyShipper Product Extractor v3.5",
        "supported_sites": ["amazon", "ebay", "walmart", "zara", "shein"],
        "unsupported_sites": "manual_mode (client handles entry)",
        "scraper_api_configured": scraper_api_on,
        "apify_configured": apify_on,
        "anti_captcha_configured": anti_captcha_on,
        "extraction_methods": {
            "amazon":  ok + " (ScraperAPI Structured)" if scraper_api_on else miss_scraper,
            "ebay":    ok + " (ScraperAPI Structured)" if scraper_api_on else miss_scraper,
            "walmart": ok + " (ScraperAPI Structured)" if scraper_api_on else miss_scraper,
            "zara":    ok + " (Apify datasaurus~zara)" if apify_on else miss_apify,
            "shein":   ok + " (ScraperAPI raw GET + JSON-LD)" if scraper_api_on else miss_scraper,
            "other":   "manual_mode → no API call made",
        },
        "recommendation": (
            "✅ Fully configured"
            if scraper_api_on and apify_on
            else "⚠ Add SCRAPER_API_KEY + APIFY_API_TOKEN to .env for all sites"
        ),
    }


@app.get("/", include_in_schema=False)
def root():
    return {"service": "BeMyShipper Product Extractor", "docs": "/docs", "ui": "/ui"}


@app.get("/ui", include_in_schema=False, response_class=HTMLResponse)
def ui():
    html_path = APP_DIR / "frontend.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="frontend.html not found on server.")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.post(
    "/extract",
    response_model=ExtractResponse,
    tags=["Extraction"],
    summary="Extract product data from Amazon, Zara, or SHEIN",
    responses={
        200: {"description": "Product data extracted successfully"},
        422: {"description": "Extraction failed — all methods exhausted"},
        400: {"description": "Unsupported site URL"},
    },
)
def extract_from_url(request: ExtractRequest):
    """
    Extract product data from an Amazon, Zara, or SHEIN product URL.

    - **Amazon**: ScraperAPI Layer 0 → HTTP → Anti-Captcha → Playwright fallback
    - **Zara**: ScraperAPI Layer 0 → Playwright with XHR interception
    - **SHEIN**: ScraperAPI raw GET → save `{username}_shein.html` → JSON-LD parse (no Apify)

    Returns: title, price, currency, main_image, variants, availability.
    """
    url = request.url
    site = detect_site(url)
    logger.info("POST /extract → site=%s url=%s", site, url)

    # Unknown sites → tell the frontend to use manual mode (no scraping attempted)
    if site == "unknown":
        return {
            "success": False,
            "manual_mode": True,
            "site": "unknown",
            "error": "This website is not supported for automatic extraction. "
                     "Please enter product details manually.",
        }

    try:
        if site == "amazon":
            amazon_scraper.force_playwright = bool(request.force_playwright)
            result = amazon_scraper.extract(url)
        elif site == "zara":
            result = scrape_zara(url)
        elif site == "shein":
            result = scrape_shein(url, username=request.username or "user")
        elif site == "ebay":
            if not SCRAPER_API_KEY:
                return {
                    "success": False,
                    "site": "ebay",
                    "error": "SCRAPER_API_KEY not configured. Add it to .env to enable eBay extraction.",
                }
            result = scrape_ebay(url, SCRAPER_API_KEY)
        elif site == "walmart":
            if not SCRAPER_API_KEY:
                return {
                    "success": False,
                    "site": "walmart",
                    "error": "SCRAPER_API_KEY not configured. Add it to .env to enable Walmart extraction.",
                }
            result = scrape_walmart(url, SCRAPER_API_KEY)
        else:
            raise HTTPException(status_code=400, detail="Unsupported site.")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected extraction error")
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")

    result["site"] = site

    if not result.get("success") and not result.get("title"):
        # Return the error dict with 200 so the frontend can display it gracefully
        return result

    return result


@app.get(
    "/extract/{asin}",
    response_model=ExtractResponse,
    tags=["Extraction"],
    summary="Extract Amazon product by ASIN",
)
def extract_by_asin(
    asin: str,
    marketplace: str = Query(default="com", description="Amazon marketplace (com, co.uk, ca, in…)"),
    force_playwright: bool = Query(default=False),
):
    """Extract Amazon product by ASIN. Builds URL automatically."""
    asin = asin.strip().upper()
    if not re.match(r"^[A-Z0-9]{10}$", asin):
        raise HTTPException(status_code=400, detail="Invalid ASIN — must be 10 alphanumeric chars.")

    url = f"https://www.amazon.{marketplace}/dp/{asin}"
    amazon_scraper.force_playwright = force_playwright

    try:
        result = amazon_scraper.extract(url)
    except Exception as e:
        logger.exception("Unexpected extraction error")
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")

    result["site"] = "amazon"

    if not result.get("success") and not result.get("title"):
        raise HTTPException(
            status_code=422,
            detail=result.get("error", "Extraction failed."),
        )
    return result


SCRAPERAPI_STRUCTURED_URL = "https://api.scraperapi.com/structured/amazon/product/v1"

_TLD_TO_COUNTRY_CODE = {
    "com": "us",
    "in": "in",
    "co.uk": "gb",
    "ca": "ca",
    "de": "de",
    "fr": "fr",
    "it": "it",
    "es": "es",
    "nl": "nl",
    "se": "se",
    "pl": "pl",
    "com.mx": "mx",
    "com.br": "br",
    "com.au": "au",
    "co.jp": "jp",
    "sg": "sg",
    "ae": "ae",
    "sa": "sa",
    "com.tr": "tr",
}


def _amazon_tld_from_url(url: str) -> Optional[str]:
    """
    Infer Amazon TLD from a product URL.
    Example: https://www.amazon.in/dp/... -> in
    """
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    if not host.startswith("amazon."):
        return None
    return host.replace("amazon.", "", 1) or None


class StructuredExtractRequest(BaseModel):
    url: str
    results_wanted: Optional[int] = 20
    username: Optional[str] = "user"

    @validator("url")
    def validate_supported_url(cls, v):
        v = v.strip()
        if not v.startswith("http"):
            v = "https://" + v
        # Unknown sites are handled gracefully — returns manual_mode: true
        return v


@app.post(
    "/extract/structured",
    tags=["Extraction"],
    summary="Rich product data: Amazon/eBay/Walmart→ScraperAPI, Zara→Apify, SHEIN→ScraperAPI JSON-LD",
)
def extract_structured(request: StructuredExtractRequest):
    """
    Extract rich product data using third-party APIs.

    Supported sites and their extraction methods:
    - **Amazon**:  ScraperAPI Structured Data (gallery, bullets, specs, reviews)
    - **eBay**:    ScraperAPI Structured Data (title, price, images, seller, reviews)
    - **Walmart**: ScraperAPI Structured Data (title, price, image, description)
    - **Zara**:    Apify datasaurus~zara actor (colors, sizes, images)
    - **SHEIN**:   ScraperAPI raw GET + JSON-LD parse (saves {username}_shein.html)
    - **Other**:   Returns manual_mode: true — client should show manual entry form
    """
    url = request.url
    site = detect_site(url)
    logger.info("POST /extract/structured → site=%s url=%s", site, url)

    # ── Unknown site → tell frontend to use manual mode ───────────────────────
    if site == "unknown":
        return {
            "success": False,
            "manual_mode": True,
            "site": "unknown",
            "error": "This website is not supported for automatic extraction. "
                     "Please enter product details manually.",
        }

    # ── eBay → ScraperAPI Structured ─────────────────────────────────────────
    if site == "ebay":
        if not SCRAPER_API_KEY:
            return {
                "success": False,
                "site": "ebay",
                "error": "SCRAPER_API_KEY not configured. Add it to .env to enable eBay extraction.",
            }
        result = scrape_ebay(url, SCRAPER_API_KEY)
        result["site"] = "ebay"
        return result

    # ── Walmart → ScraperAPI Structured ──────────────────────────────────────
    if site == "walmart":
        if not SCRAPER_API_KEY:
            return {
                "success": False,
                "site": "walmart",
                "error": "SCRAPER_API_KEY not configured. Add it to .env to enable Walmart extraction.",
            }
        result = scrape_walmart(url, SCRAPER_API_KEY)
        result["site"] = "walmart"
        return result

    # ── Zara → Apify ──────────────────────────────────────────────────────────
    if site == "zara":
        if not APIFY_API_TOKEN:
            return {
                "success": False,
                "site": "zara",
                "error": "APIFY_API_TOKEN not configured. Add it to .env to enable Zara extraction.",
            }
        try:
            result = _scrape_zara_apify(url)
        except Exception as e:
            logger.exception("Apify Zara error")
            raise HTTPException(status_code=502, detail=f"Apify request error: {e}")

        if not result:
            return {
                "success": False,
                "site": "zara",
                "error": "Apify returned no data for this Zara URL.",
            }

        d = result.to_dict()
        d["site"] = "zara"
        return d

    # ── SHEIN → ScraperAPI JSON-LD only (always save HTML) ───────────────────
    if site == "shein":
        result = scrape_shein(url, username=request.username or "user")
        result["site"] = "shein"
        if not result.get("success"):
            raise HTTPException(status_code=422, detail=result.get("error") or "SHEIN extraction failed.")
        return result

    # ── Amazon → ScraperAPI Structured ────────────────────────────────────────
    if site != "amazon":
        raise HTTPException(status_code=400, detail="Unsupported site for structured extraction.")

    if not SCRAPER_API_KEY:
        raise HTTPException(
            status_code=503,
            detail=(
                "SCRAPER_API_KEY not configured. "
                "Add it to amazon_scraper/.env to use API mode for Amazon."
            ),
        )

    asin = extract_asin(url)
    if not asin:
        raise HTTPException(
            status_code=400,
            detail=(
                "Could not extract ASIN from URL. "
                "Use a direct Amazon product URL (amazon.com/dp/ASIN)."
            ),
        )
    asin = asin.strip().upper()

    tld = _amazon_tld_from_url(url) or "com"
    params = {"api_key": SCRAPER_API_KEY, "asin": asin, "tld": tld}
    cc = _TLD_TO_COUNTRY_CODE.get(tld)
    if cc:
        params["country_code"] = cc

    try:
        r = _requests.get(SCRAPERAPI_STRUCTURED_URL, params=params, timeout=70)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ScraperAPI request error: {e}")

    if r.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"ScraperAPI returned HTTP {r.status_code}. Check your API key.",
        )

    try:
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="ScraperAPI returned non-JSON response.")

    if not isinstance(data, dict) or (
        not data.get("name") and not data.get("product_information")
    ):
        raise HTTPException(
            status_code=422,
            detail="ScraperAPI returned no product data. Check your API key and ASIN.",
        )

    # Normalise price + currency
    pricing_str = data.get("pricing") or ""
    price = None
    currency = "$"
    if pricing_str:
        m_price = re.search(r"[\d,.]+", pricing_str)
        if m_price:
            price = m_price.group(0).replace(",", "")
        if "₹" in pricing_str or "INR" in pricing_str:
            currency = "₹"
        elif "£" in pricing_str:
            currency = "£"
        elif "€" in pricing_str:
            currency = "€"

    images = data.get("high_res_images") or data.get("images") or []
    main_image = images[0] if images else None

    avg_rating = data.get("average_rating")
    total_reviews = data.get("total_reviews") or data.get("total_ratings")

    # Build star percentages
    sp = data.get("star_percentages") or {}
    star_percentages = {
        "5": sp.get("5") or sp.get("five_star") or 0,
        "4": sp.get("4") or sp.get("four_star") or 0,
        "3": sp.get("3") or sp.get("three_star") or 0,
        "2": sp.get("2") or sp.get("two_star") or 0,
        "1": sp.get("1") or sp.get("one_star") or 0,
    }

    return {
        # ── Standard fields (same shape as /extract) ──────────────────────────
        "success": True,
        "site": "amazon",
        "asin": asin,
        "title": data.get("name"),
        "price": price,
        "currency": currency,
        "pricing": data.get("pricing"),
        "main_image": main_image,
        "variants": [],
        "rating": str(avg_rating) if avg_rating else None,
        "review_count": str(total_reviews) if total_reviews else None,
        "availability": data.get("availability_status"),
        "extraction_method": "scraperapi-structured",
        "error": None,
        # ── Rich extra fields ─────────────────────────────────────────────────
        "images": data.get("images") or [],
        "high_res_images": data.get("high_res_images") or [],
        "list_price": data.get("list_price"),
        "product_information": data.get("product_information") or {},
        "feature_bullets": data.get("feature_bullets") or [],
        "reviews": data.get("reviews") or [],
        "star_percentages": star_percentages,
    }
 