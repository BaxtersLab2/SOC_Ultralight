@echo off
cd /d "%~dp0"
set PYTHON=py
echo Installing dependencies...
"%PYTHON%" -m pip install -r requirements.txt
echo.
echo Compiling python files...
"%PYTHON%" -m py_compile soc_ultralight.py 2> compile_err.txt
if errorlevel 1 (
	echo Compilation failed. See compile_err.txt
	type compile_err.txt
	pause
	exit /b 1
)
echo Starting SOC Ultralight...
"%PYTHON%" soc_ultralight.py
