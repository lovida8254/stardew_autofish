@echo off
REM Stardew auto-fishing - minigame assist mode launcher.
REM You cast and hook manually; the bot controls the catch bar.
REM Press ESC (works anywhere, global hook) to quit.

REM Relaunch self minimized so the console does NOT cover the fullscreen game.
if not "%~1"=="min" (
    start "" /min cmd /c "%~f0" min
    exit /b
)

chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  Stardew Auto-Fishing - ASSIST MODE  (this window is minimized)
echo  You cast and hook (!) yourself. Bot controls the bar.
echo  Press ESC anytime to stop (global hotkey).
echo ============================================================
python -u assist_run.py
echo.
echo [stopped] Press any key to close.
pause >nul
