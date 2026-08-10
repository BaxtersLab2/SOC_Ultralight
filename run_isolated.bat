@echo off
:: ============================================================================
::  SOC Ultralight - run on an ISOLATED virtual desktop (Vi_minimizer)
:: ============================================================================
::  Operator-triggered. This launches SOC Ultralight onto a private Win32
::  desktop ("soc_vi") so its clicks / keystrokes / OCR act THERE, leaving your
::  real mouse, keyboard and screen free.
::
::  A control console opens on THIS (your real) desktop:
::      h = health    (list SOC's windows on the isolated desktop)
::      p = peek      (screen switches to soc_vi, then AUTO-RETURNS after 8s)
::      s = shutdown  (kill SOC on the isolated desktop and quit)
::  Closing this window also tears the isolated SOC down cleanly.
::
::  NOTE: this is Vi_minimizer's CreateDesktop INPUT isolation. It is SEPARATE
::  from SOC's Parsec-VDD "VDesk" (virtual MONITOR) button. Don't use both at
::  once - leave VDesk off when running isolated this way.
:: ============================================================================
cd /d "%~dp0"

set "VI_EXE=C:\Users\user\Desktop\constitution\build-cache\release\vi_minimizer.exe"
set "LAUNCHER=C:\Users\user\Desktop\Vi_minimizer\python\isolated_launcher.py"

if not exist "%VI_EXE%" (
    echo ERROR: vi_minimizer.exe not found at:
    echo   %VI_EXE%
    echo Build it first:  cd /d C:\Users\user\Desktop\Vi_minimizer ^&^& cargo build --release
    pause
    exit /b 1
)
if not exist "%LAUNCHER%" (
    echo ERROR: isolated_launcher.py not found at:
    echo   %LAUNCHER%
    pause
    exit /b 1
)

echo Launching SOC Ultralight on isolated desktop 'soc_vi'...
echo (SOC's window will NOT appear on this desktop - use 'p' to peek.)
echo.
py -3 "%LAUNCHER%" soc_vi --exe "%VI_EXE%" --cwd "%CD%" -- pythonw soc_ultralight.py

echo.
echo SOC isolated session ended.
pause
