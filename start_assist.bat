@echo off
REM Stardew auto-fishing - assist mode GUI launcher.
REM You cast and hook (!) manually; the bot controls the catch bar.
REM A small window opens with a Start/Stop toggle button (top-left corner).
REM Click Start to begin, click Stop to end. It never auto-stops.
cd /d "%~dp0"
start "" pythonw assist_gui.py
