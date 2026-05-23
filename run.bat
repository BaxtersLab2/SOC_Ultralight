@echo off
cd /d "%~dp0"
set PYTHON=py
echo Installing dependencies...
"%PYTHON%" -m pip install -r requirements.txt
echo.
echo Starting SOC Ultralight...
"%PYTHON%" soc_ultralight.py
pause
