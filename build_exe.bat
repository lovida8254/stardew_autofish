@echo off
REM Build the assist-mode GUI into a single Windows exe (no console).
REM Requires: pip install pyinstaller
cd /d "%~dp0"
pyinstaller --onefile --windowed --name StardewAutoFishing --add-data "config.json;." assist_gui.py
echo.
echo Done. Output: dist\StardewAutoFishing.exe
echo Keep config.json next to the exe if you want to tweak settings without rebuilding.
