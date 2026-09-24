@echo off
rem ============================================================
rem  Build a standalone GiwifiAutoLogin.exe with PyInstaller
rem  Output: dist\GiwifiAutoLogin.exe
rem ============================================================
cd /d "%~dp0"
setlocal

set "PY="
for %%P in ("C:\Users\dell\.workbuddy\binaries\python\versions\3.13.12\python.exe") do (
    if exist %%P set "PY=%%~fP"
)
if not defined PY (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PY set "PY=%%P"
    )
)
if not defined PY (
    echo [ERROR] python.exe not found. Install Python 3.9+ first.
    pause
    exit /b 1
)

echo Using interpreter: %PY%
"%PY%" -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo Installing PyInstaller ...
    "%PY%" -m pip install --upgrade pyinstaller || (echo pip install failed & pause & exit /b 1)
)

echo Building ...
"%PY%" -m PyInstaller --noconfirm --clean --onefile --noconsole ^
    --name GiwifiAutoLogin ^
    --icon giwifi.ico ^
    --add-data "vendor;vendor" ^
    --add-data "giwifi.ico;." ^
    GiwifiAutoLogin.pyw

if errorlevel 1 (
    echo.
    echo [FAILED] Build error, see the log above.
    pause
    exit /b 1
)

echo.
echo [OK] Done -^> dist\GiwifiAutoLogin.exe
echo      Run it once, fill in your account, then tick the auto-start button.
echo.
echo NOTE: if Windows Smart App Control blocks the exe, code-sign it or
echo       keep using the plain Python script instead.
pause
endlocal
