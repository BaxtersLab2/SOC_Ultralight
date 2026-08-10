@echo off
:: SOC Ultralight — Virtual Display Setup (run ONCE as Administrator)
:: Installs the Parsec Virtual Display Driver (MIT licence, open source).
:: After this runs once, the SOC Ultralight app controls the virtual monitor
:: with no further elevation needed.

cd /d "%~dp0"

:: Check for admin rights
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ERROR: This script must be run as Administrator.
    echo  Right-click setup_vdd.bat and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

if not exist "parsec-vdd-setup.exe" (
    echo Downloading Parsec Virtual Display Driver...
    set PYTHON=py
    "%PYTHON%" -c "import urllib.request; urllib.request.urlretrieve('https://github.com/nomi-san/parsec-vdd/releases/download/v0.45.1/ParsecVDisplay-v0.45-setup.exe', 'parsec-vdd-setup.exe')"
    if errorlevel 1 (
        echo Download failed. Check your internet connection and try again.
        pause
        exit /b 1
    )
)

echo Installing Parsec Virtual Display Driver...
start /wait "" "parsec-vdd-setup.exe" /S

echo.
echo Checking installation...
where vdd >nul 2>&1
if errorlevel 1 (
    echo.
    echo  WARNING: 'vdd' command not found in PATH.
    echo  The installer may have placed it in a non-PATH location.
    echo  Check: C:\Program Files\Parsec\
    echo  You may need to add that folder to your PATH, or restart and try again.
) else (
    echo   OK — vdd command found.
    vdd -v
)

echo.
echo Done. You can now use the Virtual Desktop button in SOC Ultralight.
echo No further admin rights required for normal use.
echo.
pause
