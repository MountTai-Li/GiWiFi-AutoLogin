@echo off
rem ============================================================
rem  Launch the GiWiFi auto-login control panel
rem  (uses pythonw.exe so no console window stays open)
rem ============================================================
cd /d "%~dp0"
setlocal

set "PYW="
rem 1) the interpreter that was used to set this project up
for %%P in ("C:\Users\dell\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe") do (
    if exist %%P set "PYW=%%~fP"
)
rem 2) fall back to pythonw.exe on PATH
if not defined PYW (
    for /f "delims=" %%P in ('where pythonw 2^>nul') do (
        if not defined PYW set "PYW=%%P"
    )
)
if not defined PYW (
    echo [ERROR] pythonw.exe not found.
    echo         Install Python 3.9+ or edit this file and set PYW manually.
    pause
    exit /b 1
)

start "" "%PYW%" "%~dp0GiwifiAutoLogin.pyw"
endlocal
