@echo off
cd /d "%~dp0"

echo ========================================
echo   SheetMate Data Automation
echo ========================================
echo.

echo [1/3] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Python not found!
    echo Please install Python 3.8+ from:
    echo https://www.python.org/downloads/
    echo Remember to check "Add Python to PATH"
    echo.
    pause
    exit /b 1
)
echo Python OK
echo.

echo [2/3] Installing packages...
python -m pip install -r requirements.txt -q
python -m playwright install chromium
echo Done
echo.

echo [3/3] Starting SheetMate...
where pythonw >nul 2>&1
if not errorlevel 1 (
    start "" pythonw auto_report.py
) else (
    start "" python auto_report.py
)
exit /b 0
