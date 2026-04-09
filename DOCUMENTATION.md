# BeMyShipper — Product Extraction API
## Complete Setup, Deployment & Architecture Guide

**Version:** 3.1.0
**Supported Sites:** Amazon · Zara · SHEIN
**Last Updated:** April 6, 2026

---

## Table of Contents
1. [What This System Does](#what-this-system-does)
2. [File Structure](#file-structure)
3. [Architecture Explained](#architecture-explained)
4. [Setup Guide (Fresh Machine)](#setup-guide-fresh-machine)
5. [Running the API](#running-the-api)
6. [Using the Test UI](#using-the-test-ui)
7. [API Reference](#api-reference)
8. [Deployment Guide (Cloud)](#deployment-guide-cloud)
9. [How to Add a New Website](#how-to-add-a-new-website)
10. [Troubleshooting](#troubleshooting)

---

## What This System Does

When a user pastes any Amazon, Zara, or SHEIN product URL, this system automatically extracts:

| Field | Description |
|---|---|
| `title` | Full product name |
| `price` | Sale/current price as a clean number |
| `currency` | Currency symbol (e.g. `$`, `₹`, `£`) |
| `main_image` | Direct URL to the primary product image |
| `variants` | Size, color, style options as structured lists |
| `availability` | In Stock / Out of Stock |
| `rating` | Star rating (where available) |

The API returns clean JSON — ready for the app to auto-fill the Buy-For-Me form without any manual entry.

---

## File Structure

```
amazon_scraper/
├── main.py              ← FastAPI server (entry point)
├── scraper.py           ← Amazon scraper (4-layer strategy)
├── zara_scraper.py      ← Zara scraper (Playwright + XHR)
├── shein_scraper.py     ← SHEIN scraper (Playwright + XHR)
├── captcha_solver.py    ← Anti-Captcha.com integration
├── frontend.html        ← Test UI (open in browser)
├── test_scraper.py      ← Diagnostic tests
├── requirements.txt     ← Python dependencies
├── setup.bat            ← Windows quick-setup script
├── .env.example         ← Environment variable template
└── .env                 ← Your API keys (do not commit)
```

---

## Architecture Explained

### Request Flow

```
App (Buy-For-Me button)
        ↓  POST /extract  { "url": "..." }
    FastAPI (main.py)
        ↓  detect_site(url)
   ┌────┴────────────────────┐
   │                         │
Amazon              Zara / SHEIN
   │                         │
Layer 1:            Layer 1:
HTTP + cookies      Playwright browser
                    + XHR interception
   │                         │
Layer 2:            Layer 2:
Anti-Captcha        window.__INITIAL_STATE__
   │                JSON-LD scripts
Layer 3:                     │
Playwright          Layer 3:
stealth browser     HTML DOM selectors
   │                         │
   └────────┬────────────────┘
            ↓
     JSON Response
   { title, price, image, variants }
        ↓
   App auto-fills form
```

### Why 3 Layers?
- **Layer 1 (fast)**: Works ~80% of the time in seconds. No browser overhead.
- **Layer 2 (medium)**: Kicks in when Layer 1 hits CAPTCHA or bot detection.
- **Layer 3 (slow but reliable)**: Full headless browser that looks like a real user.

### Amazon Strategy (scraper.py)
1. Pre-warm a real session by hitting amazon.com homepage (harvests cookies)
2. Fetch product page with rotating browser headers
3. Parse: JSON-LD structured data → DOM selectors → alternate selectors
4. If CAPTCHA detected → Anti-Captcha.com solves it automatically
5. If still blocked → Playwright stealth browser with navigator.webdriver spoofed

### Zara Strategy (zara_scraper.py)
1. Launch Playwright with stealth fingerprinting
2. Navigate to Zara homepage first (cookie pre-warm)
3. Load product page and **intercept all XHR responses**
4. Zara fires a JSON API call with full product data — capture it directly
5. Fallback: parse rendered HTML using DOM selectors + JSON-LD

### SHEIN Strategy (shein_scraper.py)
1. Launch Playwright with stealth fingerprinting (Cloudflare-aware)
2. Navigate to SHEIN homepage first (cookie/session pre-warm)
3. Load product page and intercept API responses matching product endpoints
4. Parse intercepted JSON: `goods_id`, `goods_name`, `salePrice`, `skc_sale_attr`
5. Fallback 1: Extract from `window.__INITIAL_STATE__` embedded in HTML
6. Fallback 2: Parse rendered HTML DOM selectors

---

## Setup Guide (Fresh Machine)

### Requirements
- Python 3.9 or higher
- Windows, Mac, or Linux
- Internet connection

### Step 1 — Clone/copy the project folder

Place the `amazon_scraper/` folder wherever you want on the machine.

### Step 2 — Install Python dependencies

```bash
cd amazon_scraper
pip install -r requirements.txt --break-system-packages
```

### Step 3 — Install Playwright browser

```bash
playwright install chromium
```

This downloads a ~150MB Chromium binary. Only needed once.

### Step 4 — Set your Anti-Captcha API key

Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Edit `.env` and add your key:
```
ANTI_CAPTCHA_KEY=your_key_here
```

Get a key at [https://anti-captcha.com](https://anti-captcha.com). Top up with $5–10 to start.

> **Note:** The system works without this key — Amazon CAPTCHA cases will use Playwright instead. The key just makes it faster (~15s vs ~10s) and more reliable.

### Step 5 — Run the diagnostic test

```bash
python test_scraper.py
```

Expected output:
```
✅ PASS  Playwright works.
✅ PASS  HTTP scraping works.
✅ PASS  API key valid. Account balance: $X.XX
✅ PASS  Extraction succeeded via: http
```

If any step fails, see [Troubleshooting](#troubleshooting).

### Windows Quick Setup

Double-click `setup.bat` — it runs all steps automatically.

---

## Running the API

### Start the server
```bash
uvicorn main:app --reload --port 8000
```

The API is now live at: `http://127.0.0.1:8000`

### Swagger docs
Open in browser: `http://127.0.0.1:8000/docs`

You can test every endpoint directly from the browser UI.

### Health check
```
GET http://127.0.0.1:8000/health
```
Returns:
```json
{
  "status": "ok",
  "service": "BeMyShipper Product Extractor v3.1",
  "supported_sites": ["amazon", "zara", "shein"],
  "anti_captcha_configured": true
}
```

---

## Using the Test UI

Open `frontend.html` in any browser while the API server is running.

1. Paste any Amazon, Zara, or SHEIN product URL into the input field
2. Click **Extract**
3. The result shows: product image, title, price, available sizes/colors

The extraction method is shown in the badge (e.g. "Fast HTTP", "SHEIN API", "Zara XHR API").

---

## API Reference

### POST /extract

Extract product data from any supported URL.

**Request body:**
```json
{
  "url": "https://www.amazon.com/dp/B09G9FPHY6",
  "force_playwright": false
}
```

**Response:**
```json
{
  "success": true,
  "site": "amazon",
  "asin": "B09G9FPHY6",
  "title": "Apple AirPods Pro (2nd Generation)",
  "price": "189.99",
  "currency": "$",
  "main_image": "https://m.media-amazon.com/images/I/...",
  "variants": [
    {
      "group": "Style",
      "options": ["With MagSafe Case", "With USB-C Case"]
    }
  ],
  "rating": "4.4",
  "review_count": "67,432 ratings",
  "availability": "In Stock",
  "extraction_method": "http",
  "error": null
}
```

### GET /extract/{asin}

Extract Amazon product by ASIN directly.

```
GET /extract/B09G9FPHY6?marketplace=com
```

Parameters:
- `asin` — 10-character Amazon product ID
- `marketplace` — `com`, `co.uk`, `ca`, `in`, `de`, etc. (default: `com`)

### GET /health

Returns service status and configuration info.

---

## Deployment Guide (Cloud)

### Option A — Railway (Easiest, ~5 minutes)

1. Push the `amazon_scraper/` folder to a GitHub repository
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Set environment variable: `ANTI_CAPTCHA_KEY=your_key`
4. Railway auto-detects Python and runs `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Add a `Procfile`:
   ```
   web: playwright install chromium && uvicorn main:app --host 0.0.0.0 --port $PORT
   ```

### Option B — Render (Free tier available)

1. Push to GitHub
2. Go to [render.com](https://render.com) → New Web Service
3. Build command: `pip install -r requirements.txt && playwright install chromium`
4. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Add `ANTI_CAPTCHA_KEY` in Environment tab

### Option C — VPS (Ubuntu 22.04)

```bash
# Install Python & deps
sudo apt update && sudo apt install python3-pip -y
cd /opt && git clone <your-repo> bemyshipper
cd bemyshipper/amazon_scraper
pip install -r requirements.txt --break-system-packages
playwright install chromium
playwright install-deps chromium

# Create .env
echo "ANTI_CAPTCHA_KEY=your_key" > .env

# Run with PM2 (keeps it alive)
npm install -g pm2
pm2 start "uvicorn main:app --host 0.0.0.0 --port 8000" --name bemyshipper
pm2 save && pm2 startup
```

### Important: Playwright on Cloud

Playwright requires system dependencies for Chromium. Run this on any Linux server:
```bash
playwright install-deps chromium
```

---

## How to Add a New Website

This is the most important section for scaling the system. Adding a new site takes about 2–3 hours.

### Step 1 — Create the scraper file

Copy `shein_scraper.py` to `newsite_scraper.py`. This is your template.

### Step 2 — Update the ProductData dataclass

The dataclass is already compatible with the API — keep the same fields. Just change the `source` default string:
```python
source: str = "newsite"
```

### Step 3 — Write URL helpers

Add functions to extract the product ID from the URL:
```python
def extract_product_id(url: str) -> Optional[str]:
    # Use regex to find the product ID in the URL pattern
    m = re.search(r"/product/(\d+)", url, re.I)
    return m.group(1) if m else None
```

### Step 4 — Identify the site's API

Open the site in Chrome DevTools → Network tab → filter by `Fetch/XHR`.
Load a product page and look for a response that contains `title`, `price`, and images in JSON format. Note the URL pattern.

### Step 5 — Wire up the Playwright interceptor

In the `handle_response()` closure inside your scraper, add the API URL hints:
```python
_NEWSITE_API_HINTS = (
    "api/product",
    "product-detail",
    "/goods/",
    # ...add the URL patterns you found in Step 4
)
```

### Step 6 — Write the JSON parser

Implement `_parse_newsite_json(data, url)` to extract fields from the API response. Use `_parse_shein_goods_detail()` as the reference implementation.

### Step 7 — Write HTML fallback

Implement `_parse_newsite_html(html, url)` using BeautifulSoup selectors. Use Chrome DevTools to inspect the product page DOM and find stable CSS selectors.

### Step 8 — Register in main.py

Add 3 lines to `main.py`:

```python
# At the top — import
from newsite_scraper import scrape_newsite

# In SUPPORTED_SITES list
SUPPORTED_SITES = ["amazon.", "zara.com", "shein.com", "newsite.com"]

# In detect_site()
if "newsite.com" in url_lower:
    return "newsite"

# In extract_from_url()
elif site == "newsite":
    result = scrape_newsite(url)
```

### Step 9 — Update the test UI

In `frontend.html`, add to `detectSite()`:
```javascript
if (url.includes("newsite.com")) return "newsite";
```

And add a badge style:
```css
.site-badge.newsite { background: #f0fff4; color: #006400; }
```

### Checklist for new sites
- [ ] Scraper file created from template
- [ ] URL ID extractor written and tested
- [ ] API response intercepted in DevTools
- [ ] JSON parser handles title, price, image, variants
- [ ] HTML fallback with 3+ selector options per field
- [ ] Bot detection handled (returns clear error, not empty data)
- [ ] Registered in `main.py`
- [ ] Tested with 5+ real product URLs
- [ ] Success rate documented

---

## Troubleshooting

### "Playwright not installed"
```bash
pip install playwright --break-system-packages
playwright install chromium
```

### "CAPTCHA every time on Amazon"
- Your IP may be flagged. Use a residential proxy.
- Add your Anti-Captcha key to `.env`
- Try `force_playwright: true` in the request body

### "SHEIN returns bot/Cloudflare error"
- SHEIN uses Cloudflare. On server deployments, use a residential proxy.
- Locally (your own IP/home network) it usually works fine.
- The system automatically retries with the HTML fallback.

### "Zara returns Access Denied"
- Zara uses Akamai bot detection.
- Try a different network (mobile hotspot often works).
- The `headless=new` Chrome mode is already enabled to help with this.

### Price is `null`
- This can happen with products that have complex pricing (e.g. "from $X" range prices).
- Check the `extraction_method` field — if it's `http`, try `force_playwright: true`.
- Report the URL so the selector can be updated.

### Server crashes on startup
- Ensure `requirements.txt` packages are all installed: `pip install -r requirements.txt`
- Check your `.env` file exists (even if empty — `ANTI_CAPTCHA_KEY=` is fine)

### Very slow responses (>60s)
- Normal for Playwright paths (Zara, SHEIN): 10–25s expected
- For Amazon HTTP path: should be 2–5s
- If Amazon always hits Playwright: your server IP is likely rate-limited

---

*For integration questions or to report a broken extraction, document the product URL, the site, and the API response you received.*
