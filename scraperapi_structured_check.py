"""
ScraperAPI Structured Data check (Amazon Product endpoint).

This script is intentionally standalone so you can run it to verify:
  - your ScraperAPI key works
  - the Structured Data endpoint is returning product JSON (not HTML / a welcome page)

Usage (PowerShell):
  python .\\amazon_scraper\\scraperapi_structured_check.py --asin B07FTKQ97Q
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional, Tuple

import requests
from dotenv import load_dotenv


ENDPOINT = "https://api.scraperapi.com/structured/amazon/product/v1"


def _looks_like_html(text: str) -> bool:
    t = (text or "").lstrip().lower()
    return t.startswith("<!doctype html") or t.startswith("<html") or "<body" in t[:5000]


def _parse_json_response(text: str) -> Tuple[Optional[Any], Optional[str]]:
    if not text:
        return None, "Empty response body"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        if _looks_like_html(text):
            return None, "Response looks like HTML (not JSON). This usually means the wrong endpoint or a proxy/welcome page was returned."
        return None, f"Response is not valid JSON: {e}"


def fetch_amazon_product_structured(
    *,
    api_key: str,
    asin: str,
    timeout_s: int = 60,
) -> Tuple[Optional[dict], dict]:
    """
    Returns (product_json_or_none, debug_info).
    debug_info always includes request/response metadata to help diagnose failures.
    """
    asin = asin.strip().upper()
    params = {"api_key": api_key, "asin": asin}

    debug: dict = {
        "endpoint": ENDPOINT,
        "asin": asin,
    }

    try:
        r = requests.get(ENDPOINT, params=params, timeout=timeout_s)
    except requests.RequestException as e:
        debug["request_error"] = str(e)
        return None, debug

    debug["status_code"] = r.status_code
    debug["content_type"] = r.headers.get("content-type")
    debug["x_request_id"] = r.headers.get("x-request-id") or r.headers.get("x_request_id")

    parsed, parse_error = _parse_json_response(r.text)
    if parse_error:
        debug["parse_error"] = parse_error
        debug["body_preview"] = (r.text or "")[:800]
        return None, debug

    if not isinstance(parsed, dict):
        debug["parse_error"] = f"Expected JSON object, got {type(parsed).__name__}"
        debug["body_preview"] = (r.text or "")[:800]
        return None, debug

    # ScraperAPI errors are usually JSON objects with helpful fields.
    if r.status_code >= 400:
        debug["api_error_payload"] = parsed
        return None, debug

    # Basic sanity: Structured product payloads typically include at least name + product_information.
    if not parsed.get("name") and not parsed.get("product_information"):
        debug["warning"] = "JSON returned, but it doesn't look like an Amazon product payload (missing 'name'/'product_information')."

    return parsed, debug


def main() -> int:
    # Avoid UnicodeEncodeError on Windows consoles configured as cp1252.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    load_dotenv()

    p = argparse.ArgumentParser(description="Check ScraperAPI Structured Data: Amazon Product endpoint.")
    p.add_argument("--asin", required=True, help="Amazon ASIN (10 chars), e.g. B07FTKQ97Q")
    p.add_argument(
        "--api-key",
        default=os.getenv("SCRAPER_API_KEY", "").strip(),
        help="ScraperAPI key. Defaults to SCRAPER_API_KEY from .env",
    )
    p.add_argument("--timeout", type=int, default=60, help="Request timeout seconds (default: 60)")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    p.add_argument(
        "--summary-only",
        action="store_true",
        help="Only print a short summary (no full JSON).",
    )

    args = p.parse_args()

    if not args.api_key:
        print("Missing ScraperAPI key.", file=sys.stderr)
        print("Set SCRAPER_API_KEY in amazon_scraper/.env (recommended) or pass --api-key.", file=sys.stderr)
        return 2

    product, debug = fetch_amazon_product_structured(
        api_key=args.api_key,
        asin=args.asin,
        timeout_s=args.timeout,
    )

    ok = product is not None
    status = debug.get("status_code", "n/a")

    if ok:
        name = str(product.get("name") or "").strip() or "(no name field)"
        asin = (
            (product.get("product_information") or {}).get("asin")
            if isinstance(product.get("product_information"), dict)
            else None
        ) or args.asin.strip().upper()

        print("✅ ScraperAPI Structured Amazon Product: OK")
        print(f"- ASIN: {asin}")
        print(f"- HTTP status: {status}")
        print(f"- Name: {name[:140]}")

        warn = debug.get("warning")
        if warn:
            print(f"- Warning: {warn}")

        if not args.summary_only:
            if args.pretty:
                print(json.dumps(product, indent=2, ensure_ascii=False))
            else:
                print(json.dumps(product, ensure_ascii=False))
        return 0

    print("❌ ScraperAPI Structured Amazon Product: FAILED")
    print(f"- HTTP status: {status}")
    if debug.get("request_error"):
        print(f"- Request error: {debug['request_error']}")
    if debug.get("parse_error"):
        print(f"- Parse error: {debug['parse_error']}")
    if debug.get("api_error_payload") is not None:
        print("- API error payload:")
        print(json.dumps(debug["api_error_payload"], indent=2, ensure_ascii=False))
    if debug.get("body_preview"):
        print("- Body preview (first ~800 chars):")
        print(debug["body_preview"])

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

