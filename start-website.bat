@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Tora AI product website - a static site. Needs only Python 3; never touches
REM Telegram, Ollama, or the local agent. Usage: start-website.bat [port]

set "PORT=%~1"
if "%PORT%"=="" set "PORT=4173"
set "ROOT=%~dp0website\dist"

if not exist "%ROOT%\index.html" (
  echo [start-website] website\dist\index.html was not found next to this script.
  exit /b 1
)

REM Refuse to start if the IPv4 side of the port is taken. Never stop the other process.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:"TCP  *127\.0\.0\.1:%PORT%  *.*LISTENING" /C:"TCP  *0\.0\.0\.0:%PORT%  *.*LISTENING"') do (
  echo [start-website] Port %PORT% on 127.0.0.1 is already in use by process ID %%p.
  echo   Close that program, or use another port:   start-website.bat 5173
  exit /b 1
)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:"TCP  *\[::1*\]:%PORT%  *.*LISTENING"') do (
  echo [start-website] Note: process ID %%p is listening on the IPv6 side of port %PORT%.
  echo   Open the 127.0.0.1 address printed below; http://localhost:%PORT%/ may reach that other program.
)

echo.
echo  Tora AI website
echo  Serving "%ROOT%"
echo  Open:   http://127.0.0.1:%PORT%/
echo  Press Ctrl+C to stop.
echo.

if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -m http.server %PORT% --bind 127.0.0.1 --directory "%ROOT%"
  exit /b %ERRORLEVEL%
)
where py >nul 2>&1 && (
  py -3 -m http.server %PORT% --bind 127.0.0.1 --directory "%ROOT%"
  exit /b %ERRORLEVEL%
)
where python >nul 2>&1 && (
  python -m http.server %PORT% --bind 127.0.0.1 --directory "%ROOT%"
  exit /b %ERRORLEVEL%
)
echo [start-website] Python 3 was not found. Install it from https://www.python.org/downloads/ or run scripts\setup.py.
exit /b 1
