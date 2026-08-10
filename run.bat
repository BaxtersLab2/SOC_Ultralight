@echo off
cd /d "%~dp0"
set PYTHON=py

:: ── V plugin check ───────────────────────────────────────────────────────────
:: Warn if no VLM server is found, but never block — SOC Ultralight launches
:: regardless. Use the V:on/V:off toggle in Phase 1 to disable Agent 4 if
:: you want to run in autoclick-only mode without any vision server.
::
:: Checks port 8080 first (GGUF Chatbox main model proxy — loads whatever
:: model is in the tray), then port 8082 (standalone vision server).
set "V_PRESENT=0"
if exist "plugins\v_plugin.py"            set "V_PRESENT=1"
if exist "plugins\v_plugin\v_plugin.py"   set "V_PRESENT=1"

if "%V_PRESENT%"=="1" (
    echo.
    echo  V plugin detected — checking VLM servers ^(port 8080 / 8082^)...
    set "VLM_OK=0"
    curl -s --max-time 2 http://localhost:8080/v1/chat/completions >nul 2>&1
    if not errorlevel 1 set "VLM_OK=1"
    curl -s --max-time 2 http://localhost:8082/v1/models >nul 2>&1
    if not errorlevel 1 set "VLM_OK=1"

    if "%VLM_OK%"=="1" (
        echo  [OK] VLM server reachable — V plugin will activate on launch.
    ) else (
        echo.
        echo  ┌─────────────────────────────────────────────────────────┐
        echo  │  No VLM server found on port 8080 or 8082.              │
        echo  │  Agent 4 ^(vision^) will be offline — everything else OK. │
        echo  │                                                         │
        echo  │  To enable vision later:                                │
        echo  │    Option A — load a model in GGUF Chatbox ^(port 8080^)  │
        echo  │    Option B — run  plugins\start_vlm_server.py          │
        echo  │                                                         │
        echo  │  Or toggle  V:off  in the Phase 1 header to suppress    │
        echo  │  Agent 4 entirely for this session.                     │
        echo  └─────────────────────────────────────────────────────────┘
        echo.
        echo  Continuing without vision server...
    )
    echo.
)

:: ── Dependencies ─────────────────────────────────────────────────────────────
echo Installing dependencies...
"%PYTHON%" -m pip install -r requirements.txt -q
echo.

:: ── Syntax check ─────────────────────────────────────────────────────────────
echo Compiling...
"%PYTHON%" -m py_compile soc_ultralight.py 2> compile_err.txt
if errorlevel 1 (
    echo Compilation failed. See compile_err.txt
    type compile_err.txt
    pause
    exit /b 1
)

:: ── Launch ───────────────────────────────────────────────────────────────────
:: pyw has no console, so a startup crash (or the single-instance exit) is
:: otherwise invisible — cmd flashes and no GUI appears. Run via a minimized
:: wrapper that captures stderr to startup_err.txt; if no window shows up,
:: open that file to see why.
echo Launching SOC Ultralight...
if exist "%~dp0startup_err.txt" del "%~dp0startup_err.txt"
start "" /min cmd /c "pyw soc_ultralight.py 2>startup_err.txt"
