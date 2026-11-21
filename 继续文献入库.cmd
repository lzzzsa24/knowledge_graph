@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" -X utf8 -u scripts\run_native_library.py --resume --wait-for-reset
pause
