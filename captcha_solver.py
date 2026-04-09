"""
Anti-Captcha.com integration for Amazon bot-check pages.

Set ANTI_CAPTCHA_API_KEY in the environment or in .env (see .env.example).
Never commit API keys to source control.
"""

import base64
import logging
import os
import time
import re
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass


def _load_anti_captcha_key() -> str:
    return (
        os.environ.get("ANTI_CAPTCHA_API_KEY", "").strip()
        or os.environ.get("ANTI_CAPTCHA_KEY", "").strip()
    )


ANTI_CAPTCHA_KEY = _load_anti_captcha_key()

ANTI_CAPTCHA_CREATE = "https://api.anti-captcha.com/createTask"
ANTI_CAPTCHA_RESULT = "https://api.anti-captcha.com/getTaskResult"


# ── Anti-Captcha REST client ──────────────────────────────────────────────────
class AntiCaptchaClient:
    """Thin wrapper around Anti-Captcha REST API (ImageToTextTask)."""

    def __init__(self, api_key: str):
        self.api_key = (api_key or "").strip()

    @property
    def configured(self) -> bool:
        """True when a non-empty API key is available."""
        return bool(self.api_key)

    def solve_image(self, image_bytes: bytes, max_wait: int = 120) -> Optional[str]:
        """
        Submit an image CAPTCHA and return the solved text.
        Returns None on failure or timeout.
        """
        if not self.configured:
            logger.warning("Anti-Captcha: no API key — cannot solve image CAPTCHA.")
            return None

        b64 = base64.b64encode(image_bytes).decode("utf-8")

        payload = {
            "clientKey": self.api_key,
            "task": {
                "type": "ImageToTextTask",
                "body": b64,
                "phrase": False,
                "case": False,
                "numeric": 0,
                "math": False,
                "minLength": 0,
                "maxLength": 0,
            },
            "softId": 0,
        }

        try:
            resp = requests.post(ANTI_CAPTCHA_CREATE, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("AntiCaptcha createTask failed: %s", e)
            return None

        if data.get("errorId", 0) != 0:
            logger.error("AntiCaptcha API error: %s", data.get("errorDescription"))
            return None

        task_id = data.get("taskId")
        logger.info("AntiCaptcha task created: %s", task_id)

        # Poll for result
        deadline = time.time() + max_wait
        while time.time() < deadline:
            time.sleep(3)
            try:
                result_resp = requests.post(
                    ANTI_CAPTCHA_RESULT,
                    json={"clientKey": self.api_key, "taskId": task_id},
                    timeout=30,
                )
                result_resp.raise_for_status()
                result = result_resp.json()
            except Exception as e:
                logger.warning("AntiCaptcha poll error: %s", e)
                continue

            if result.get("errorId", 0) != 0:
                logger.error("AntiCaptcha result error: %s", result.get("errorDescription"))
                return None

            status = result.get("status")
            if status == "ready":
                solution = result["solution"]["text"]
                logger.info("AntiCaptcha solved: %r", solution)
                return solution.strip()

            logger.debug("AntiCaptcha status: %s — waiting…", status)

        logger.error("AntiCaptcha timed out waiting for solution.")
        return None


# ── Amazon CAPTCHA handler ────────────────────────────────────────────────────
class AmazonCaptchaHandler:
    """
    Detects Amazon CAPTCHA pages, solves via Anti-Captcha API,
    submits the form, and returns the real product page response.
    Works for both HTTP (requests) and Playwright browser contexts.
    """

    CAPTCHA_INDICATORS = [
        "robot check",
        "captcha",
        "enter the characters you see below",
        "sorry, we just need to make sure you",
        "type the characters you see in this image",
        "opfcaptcha",
        "amzn-captcha",
    ]

    def __init__(self, session: requests.Session, api_key: str = ANTI_CAPTCHA_KEY):
        self.session = session
        self.solver = AntiCaptchaClient(api_key or "")

    def is_captcha_page(self, html: str) -> bool:
        lower = html.lower()
        return any(ind in lower for ind in self.CAPTCHA_INDICATORS)

    # ── HTTP path: extract image + form fields ────────────────────────────────
    def _extract_captcha_data(self, html: str, base_url: str) -> Tuple[Optional[str], dict]:
        """Parse CAPTCHA page for image URL + hidden form fields."""
        soup = BeautifulSoup(html, "html.parser")

        img_url = None
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if re.search(r"captcha|opfcaptcha|/errors/", src, re.I):
                img_url = src
                break
        if not img_url:
            for sel in (
                "#captchacharacters img",
                "img#captchacharacters",
                "form[action*='validateCaptcha'] img",
                "form[action*='captcha'] img",
            ):
                el = soup.select_one(sel)
                if el:
                    img_url = el.get("src") or el.get("data-src")
                    if img_url:
                        break

        # Form hidden fields
        form_data: dict = {}
        form = soup.find("form")
        if form:
            for inp in form.find_all("input"):
                name = inp.get("name")
                if not name:
                    continue
                itype = (inp.get("type") or "text").lower()
                if itype == "hidden":
                    form_data[name] = inp.get("value", "")

            action = form.get("action") or ""
            if action and not action.startswith("http"):
                domain_m = re.match(r"(https?://[^/]+)", base_url)
                domain = domain_m.group(1) if domain_m else "https://www.amazon.com"
                action = urljoin(domain + "/", action.lstrip("/"))
            form_data["_action_url"] = action or ""

        return img_url, form_data

    # ── HTTP path: full solve + retry ─────────────────────────────────────────
    def solve_and_retry(
        self,
        captcha_html: str,
        original_url: str,
        headers: dict,
    ) -> Optional[requests.Response]:
        """
        Full CAPTCHA solve flow for HTTP scraping:
          1. Extract CAPTCHA image + form fields
          2. Download image → solve via Anti-Captcha
          3. Submit solved form back to Amazon
          4. Return resulting response (should be product page)
        """
        if not self.solver.configured:
            logger.warning("CAPTCHA detected but API key not set — skipping solve.")
            return None

        logger.info("Starting Anti-Captcha solve (HTTP path)…")

        domain_match = re.match(r"(https?://[^/]+)", original_url)
        base_url = domain_match.group(1) if domain_match else "https://www.amazon.com"

        img_url, form_data = self._extract_captcha_data(captcha_html, base_url)

        if not img_url:
            logger.error("Could not find CAPTCHA image URL in page HTML.")
            return None

        # Make image URL absolute
        img_url = urljoin(original_url, img_url)

        # Download CAPTCHA image
        try:
            img_resp = self.session.get(img_url, headers=headers, timeout=15)
            img_resp.raise_for_status()
            image_bytes = img_resp.content
            logger.info("CAPTCHA image downloaded (%d bytes)", len(image_bytes))
        except Exception as e:
            logger.error("Failed to download CAPTCHA image: %s", e)
            return None

        # Solve via Anti-Captcha
        solution = self.solver.solve_image(image_bytes)
        if not solution:
            logger.error("Anti-Captcha could not return a solution.")
            return None

        # Determine form action URL
        action_url = form_data.pop("_action_url", "") or (base_url + "/errors/validateCaptcha")

        # Amazon's CAPTCHA answer field is usually "field-keywords"
        soup_check = BeautifulSoup(captcha_html, "html.parser")
        answer_field = "field-keywords"
        for name in ("field-keywords", "ca", "captcha", "input"):
            if soup_check.find("input", {"name": name}):
                answer_field = name
                break
        form_data[answer_field] = solution

        logger.info("Submitting CAPTCHA solution '%s' to %s", solution, action_url)

        try:
            post_resp = self.session.post(
                action_url,
                data=form_data,
                headers={**headers, "Referer": original_url, "Origin": base_url},
                timeout=20,
                allow_redirects=True,
            )
            # If redirect landed on product page
            if post_resp.url and "dp/" in post_resp.url and not self.is_captcha_page(post_resp.text):
                return post_resp

            # Re-fetch original URL now that the session is unblocked
            logger.info("Re-fetching original product URL after CAPTCHA solve…")
            time.sleep(1.2)
            retry_resp = self.session.get(original_url, headers=headers, timeout=20)
            return retry_resp

        except Exception as e:
            logger.error("CAPTCHA form submission failed: %s", e)
            return None

    # ── Playwright path: solve CAPTCHA inside browser session ─────────────────
    def solve_playwright_captcha(self, page: Any) -> bool:
        """
        If the current Playwright page shows a CAPTCHA, solve it in-browser:
          1. Extract CAPTCHA image src
          2. Download via requests (reuses session cookies)
          3. Solve via Anti-Captcha
          4. Type solution into browser input and submit
        Returns True if the page is no longer a CAPTCHA after submission.
        """
        if not self.solver.configured:
            logger.warning("Playwright CAPTCHA detected but API key not set — cannot solve.")
            return False

        try:
            page_html = page.content()
            if not self.is_captcha_page(page_html):
                return True   # Not a CAPTCHA — already on product page

            logger.info("Playwright: CAPTCHA detected — attempting in-browser solve…")

            img_el = None
            for sel in (
                "img[src*='opfcaptcha']",
                "img[src*='captcha']",
                "#captchacharacters img",
                "img#captchacharacters",
                "form img[src*='captcha']",
            ):
                img_el = page.query_selector(sel)
                if img_el:
                    break
            if not img_el:
                logger.error("Playwright: could not find CAPTCHA image element.")
                return False

            img_src = img_el.get_attribute("src") or ""
            if not img_src.startswith("http"):
                img_src = urljoin(page.url, img_src)

            # Use Playwright's request client so Amazon session cookies are sent
            try:
                img_resp = page.request.get(img_src, timeout=20000)
                if not img_resp.ok:
                    logger.error("Playwright: CAPTCHA image HTTP status %s", img_resp.status)
                    return False
                image_bytes = img_resp.body()
            except Exception as e:
                logger.error("Playwright: failed to download CAPTCHA image: %s", e)
                return False

            # Solve via Anti-Captcha
            solution = self.solver.solve_image(image_bytes)
            if not solution:
                logger.error("Playwright: Anti-Captcha returned no solution.")
                return False

            # Type solution into input field
            input_el = page.query_selector(
                "input[name='field-keywords'], input[name='ca'], input[type='text']"
            )
            if not input_el:
                logger.error("Playwright: could not find CAPTCHA input field.")
                return False

            input_el.fill(solution)
            logger.info("Playwright: typed CAPTCHA solution '%s'", solution)

            # Submit form
            submit_el = page.query_selector("button[type='submit'], input[type='submit']")
            if submit_el:
                submit_el.click()
            else:
                input_el.press("Enter")

            page.wait_for_load_state("domcontentloaded", timeout=15000)
            time.sleep(1.0)

            # Check if we cleared the CAPTCHA
            still_captcha = self.is_captcha_page(page.content())
            if still_captcha:
                logger.warning("Playwright: CAPTCHA still present after submission.")
                return False

            logger.info("Playwright: CAPTCHA cleared successfully.")
            return True

        except Exception as e:
            logger.error("Playwright CAPTCHA solve error: %s", e)
            return False
