@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0auto_setup.ps1"
echo.
echo === Done. See auto_setup.log and auto_setup.status ===
timeout /t 8 >nul
