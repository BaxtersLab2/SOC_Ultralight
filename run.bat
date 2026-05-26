@echo off
cd /d "%~dp0"
set PYTHON=py
echo Installing dependencies...
"%PYTHON%" -m pip install -r requirements.txt -q
echo.
echo Compiling...
"%PYTHON%" -m py_compile soc_ultralight.py 2> compile_err.txt
if errorlevel 1 (
	echo Compilation failed. See compile_err.txt
	type compile_err.txt
	pause
	exit /b 1
)
echo Launching SOC Ultralight...
for /f "delims=" %%i in ('"%PYTHON%" -c "import sys,os; print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set PYWEXE=%%i
start "" "%PYWEXE%" soc_ultralight.py
