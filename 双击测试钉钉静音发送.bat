@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "%~dp0test_dingtalk_silent_gui.py"
) else if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "%~dp0test_dingtalk_silent_gui.py"
) else (
    python "%~dp0test_dingtalk_silent_gui.py"
)
