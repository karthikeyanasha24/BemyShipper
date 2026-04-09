@echo off
echo ============================================================
echo  BeMyShipper Scraper — Setup (Windows)
echo ============================================================
echo.

echo [1/4] Installing Python dependencies...
pip install fastapi uvicorn[standard] requests beautifulsoup4 lxml playwright pydantic --break-system-packages
if %ERRORLEVEL% NEQ 0 (
    echo     Trying without --break-system-packages flag...
    pip install fastapi "uvicorn[standard]" requests beautifulsoup4 lxml playwright pydantic
)

echo.
echo [2/4] Installing Playwright Chromium browser...
playwright install chromium
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Playwright install failed. Try running manually:
    echo   python -m playwright install chromium
)

echo.
echo [3/4] Running diagnostic tests...
python test_scraper.py

echo.
echo [4/4] Starting API server...
echo   Swagger UI will be at: http://127.0.0.1:8000/docs
echo   Press Ctrl+C to stop.
echo.
uvicorn main:app --reload --port 8000
