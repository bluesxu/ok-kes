@echo off
rem Start ok-kes from this repository (.venv + main.py), elevated like the official build.
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo .venv not found. Run: py -3.12 -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -WorkingDirectory '%~dp0' -Verb RunAs"
    exit /b
)

start "" ".venv\Scripts\pythonw.exe" main.py
