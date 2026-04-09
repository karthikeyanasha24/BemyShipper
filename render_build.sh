#!/usr/bin/env bash
set -euo pipefail

# Render (non-root) build script for FastAPI + Playwright.
# Installs dependencies and downloads Chromium into a project-local path
# that is available to the runtime.

export PLAYWRIGHT_BROWSERS_PATH="/opt/render/project/src/amazon_scraper/ms-playwright"

python -m pip install --upgrade pip
pip install -r requirements.txt

# Download browser binaries (no OS deps install here; Render doesn't allow sudo).
python -m playwright install chromium

