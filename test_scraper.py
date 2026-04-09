"""
Diagnostic Test Script
=======================
Run this BEFORE starting the API to verify each layer works.

Usage:
  python test_scraper.py

Tests:
  1. Playwright installation check
  2. HTTP scraping (with cookie pre-warm)
  3. Anti-Captcha API key validity
  4. Full extraction flow
"""

import sys
import json
import time
import base64
import traceback

TEST_URL = "https://www.amazon.com/dp/B09G9FPHY6"   # AirPods Pro
ANTI_CAPTCHA_KEY = "46f013c718af563a30db24eac8f6f29f"

PASS = "✅ PASS"
FAIL = "❌ FAIL"
WARN = "⚠️  WARN"


# ── Test 1: Playwright installed? ────────────────────────────────────────────
def test_playwright():
    print("\n── Test 1: Playwright install ──────────────────────────")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto("https://example.com", timeout=15000)
            title = page.title()
            browser.close()
        print(f"{PASS}  Playwright works. Loaded page title: '{title}'")
        return True
    except ImportError:
        print(f"{FAIL}  Playwright not installed.")
        print("       Run:  pip install playwright  &&  playwright install chromium")
        return False
    except Exception as e:
        print(f"{FAIL}  Playwright installed but failed: {e}")
        print("       Run:  playwright install chromium")
        return False


# ── Test 2: HTTP scraping (no CAPTCHA scenario) ──────────────────────────────
def test_http():
    print("\n── Test 2: HTTP scraping + cookie pre-warm ─────────────")
    try:
        import requests
        from bs4 import BeautifulSoup
        import random

        session = requests.Session()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Referer": "https://www.google.com/",
        }

        print("   Pre-warming cookies on amazon.com homepage…")
        r = session.get("https://www.amazon.com", headers=headers, timeout=12)
        cookies = dict(session.cookies)
        print(f"   Cookies received: {list(cookies.keys())[:5]}")

        time.sleep(1.5)
        print(f"   Fetching product page: {TEST_URL}")
        r = session.get(TEST_URL, headers=headers, timeout=15)

        soup = BeautifulSoup(r.text, "html.parser")
        title_el = soup.find("span", {"id": "productTitle"})

        if "captcha" in r.text.lower():
            print(f"{WARN}  CAPTCHA detected — Anti-Captcha solver will kick in at runtime.")
            return "captcha"
        elif title_el:
            title = title_el.text.strip()[:60]
            print(f"{PASS}  HTTP scraping works. Title: '{title}…'")
            return True
        else:
            print(f"{WARN}  Product page loaded but title not found (layout shift?).")
            print(f"   HTTP status: {r.status_code}")
            return False
    except Exception as e:
        print(f"{FAIL}  HTTP scraping error: {e}")
        return False


# ── Test 3: Anti-Captcha API key ─────────────────────────────────────────────
def test_anticaptcha_key():
    print("\n── Test 3: Anti-Captcha API key validation ─────────────")
    try:
        import requests

        # Use getBalance to validate the key without spending credits
        payload = {"clientKey": ANTI_CAPTCHA_KEY}
        resp = requests.post(
            "https://api.anti-captcha.com/getBalance",
            json=payload,
            timeout=10,
        )
        data = resp.json()

        if data.get("errorId") == 0:
            balance = data.get("balance", 0)
            print(f"{PASS}  API key valid. Account balance: ${balance:.4f}")
            if balance < 0.001:
                print(f"{WARN}  Balance is very low — top up at https://anti-captcha.com")
            return True
        else:
            print(f"{FAIL}  API key error: {data.get('errorDescription', 'Unknown error')}")
            return False
    except Exception as e:
        print(f"{FAIL}  Could not reach Anti-Captcha API: {e}")
        print("       Check your internet connection.")
        return False


# ── Test 4: Full extraction via scraper.py ────────────────────────────────────
def test_full_extraction():
    print("\n── Test 4: Full extraction flow ────────────────────────")
    try:
        from scraper import AmazonScraper
        s = AmazonScraper(max_retries=2)
        print(f"   Running extraction on: {TEST_URL}")
        result = s.extract(TEST_URL)

        if result.get("success"):
            print(f"{PASS}  Extraction succeeded via: {result.get('extraction_method')}")
            print(f"   Title    : {str(result.get('title', ''))[:55]}…")
            print(f"   Price    : {result.get('currency', '')} {result.get('price', 'N/A')}")
            print(f"   Image    : {str(result.get('main_image', ''))[:55]}…")
            print(f"   Variants : {len(result.get('variants', []))} group(s)")
        else:
            print(f"{FAIL}  Extraction failed: {result.get('error')}")
        return result.get("success", False)
    except Exception as e:
        print(f"{FAIL}  scraper.py error: {e}")
        traceback.print_exc()
        return False


# ── Summary ───────────────────────────────────────────────────────────────────
def main():
    print("=" * 58)
    print("  BeMyShipper Scraper — Diagnostic Tests")
    print("=" * 58)

    r1 = test_playwright()
    r2 = test_http()
    r3 = test_anticaptcha_key()
    r4 = test_full_extraction()

    print("\n" + "=" * 58)
    print("  Results Summary")
    print("=" * 58)
    print(f"  Playwright      : {PASS if r1 else FAIL}")
    print(f"  HTTP scraping   : {PASS if r2 is True else (WARN + ' (CAPTCHA)' if r2 == 'captcha' else FAIL)}")
    print(f"  Anti-Captcha    : {PASS if r3 else FAIL}")
    print(f"  Full extraction : {PASS if r4 else FAIL}")
    print()

    if not r1:
        print("ACTION REQUIRED:")
        print("  pip install playwright")
        print("  playwright install chromium")
        print()
    if not r3:
        print("ACTION REQUIRED:")
        print("  Check internet access to api.anti-captcha.com")
        print("  Or top up your Anti-Captcha account balance")
        print()
    if r1 or r2 is True:
        print("✅  At least one extraction layer is working.")
        print("   Start the server:  uvicorn main:app --reload --port 8000")
    else:
        print("❌  No extraction layer is fully working yet.")
        print("   Fix the issues above, then restart the server.")


if __name__ == "__main__":
    main()
