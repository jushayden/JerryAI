@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Tora AI local agent launcher. Checks the prerequisites, explains what is
REM missing, then runs main.py from the project's virtual environment.

set "VPY=%~dp0.venv\Scripts\python.exe"

if not exist "%VPY%" (
  echo [start-agent] .venv was not found. Run this once from the project folder:
  echo     python scripts\setup.py
  echo   It creates .venv, installs the requirements and Playwright Chromium, and creates .env and .local-token.
  exit /b 1
)
if not exist ".env" (
  echo [start-agent] .env was not found. Run:   copy .env.example .env
  echo   then put the token from @BotFather in it as   BOT_TOKEN=...
  exit /b 1
)
findstr /B /C:"BOT_TOKEN=" ".env" >nul || (
  echo [start-agent] .env has no BOT_TOKEN line. Add   BOT_TOKEN=^<token from @BotFather^>
  exit /b 1
)
findstr /B /C:"BOT_TOKEN=123456:ABC-your-token-from-BotFather" ".env" >nul && (
  echo [start-agent] BOT_TOKEN in .env is still the placeholder. Create a bot with @BotFather
  echo   ^(/newbot^) and paste its token into .env as   BOT_TOKEN=...
  exit /b 1
)
findstr /B /R /C:"ALLOWED_CHAT_ID=0 *$" ".env" >nul && (
  echo [start-agent] Not paired yet. After Tora starts, send /start to your bot, copy the chat ID
  echo   it shows into .env as ALLOWED_CHAT_ID=..., then restart this script.
)
if not exist "profile.yaml" (
  echo [start-agent] Note: profile.yaml is missing. Copy profile.example.yaml to profile.yaml and fill it in,
  echo   or form-filling tasks will keep asking you for your details.
)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:"TCP  *127\.0\.0\.1:8765  *.*LISTENING"') do (
  echo [start-agent] Port 8765 is already in use by process ID %%p. Is Tora already running? Stop it first.
  exit /b 1
)
curl -s -m 3 http://localhost:11434/api/version >nul 2>&1 || (
  echo [start-agent] Note: Ollama is not reachable at http://localhost:11434. Start Ollama for tasks;
  echo   Telegram pairing still works without it.
)

echo.
echo  Starting Tora AI ... press Ctrl+C to stop.
echo.
"%VPY%" main.py
exit /b %ERRORLEVEL%
