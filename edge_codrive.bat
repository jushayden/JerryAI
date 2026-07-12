@echo off
REM Jerry Pocket Agent co-drive launcher.
REM Relaunches YOUR real Edge with a debug port so the agent can act inside your
REM persistent Jerry Edge profile (it never spoofs or bypasses checks). Modern Edge
REM ignores remote-debugging flags on the default profile, so a dedicated profile is required.
echo.
echo  Jerry Pocket Agent co-drive setup
echo  ---------------------------
echo  This will CLOSE Microsoft Edge and reopen it with co-drive enabled.
echo  Jerry uses a persistent co-drive profile under LocalAppData.
echo  Its tabs and logins persist, but you may need to sign in the first time.
echo  Save anything unsaved in currently open Edge windows first.
echo.
choice /M "Close and relaunch Edge now"
if errorlevel 2 goto :cancel

echo Closing Edge...
taskkill /IM msedge.exe /F >nul 2>&1
REM give Edge a moment to fully exit so the profile lock releases
ping -n 3 127.0.0.1 >nul

echo Relaunching Edge with co-drive...
set "JERRY_EDGE_PROFILE=%LOCALAPPDATA%\JerryAI\EdgeProfile"
if not exist "%JERRY_EDGE_PROFILE%" mkdir "%JERRY_EDGE_PROFILE%"
start "" msedge.exe --remote-debugging-port=9222 --user-data-dir="%JERRY_EDGE_PROFILE%" --restore-last-session --no-first-run

echo.
echo  Done. Edge is now co-drivable on port 9222.
echo  Leave this window; the agent connects automatically on its next browser task.
echo  This is Jerry's persistent Edge profile. Your normal Edge profile is unchanged.
echo.
pause
goto :eof

:cancel
echo Cancelled. Edge was not changed.
pause
