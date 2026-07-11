@echo off
REM Pocket Agent co-drive launcher.
REM Relaunches YOUR real Edge with a debug port so the agent can act inside your
REM own logged-in session (it never spoofs, never bypasses checks). Your tabs and
REM logins are preserved via --restore-last-session.
echo.
echo  Pocket Agent co-drive setup
echo  ---------------------------
echo  This will CLOSE Microsoft Edge and reopen it with co-drive enabled.
echo  Your open tabs are restored on reopen. Save anything unsaved first.
echo.
choice /M "Close and relaunch Edge now"
if errorlevel 2 goto :cancel

echo Closing Edge...
taskkill /IM msedge.exe /F >nul 2>&1
REM give Edge a moment to fully exit so the profile lock releases
ping -n 3 127.0.0.1 >nul

echo Relaunching Edge with co-drive...
start "" msedge.exe --remote-debugging-port=9222 --restore-last-session

echo.
echo  Done. Edge is now co-drivable on port 9222.
echo  Leave this window; the agent connects automatically on its next browser task.
echo  (To go back to normal, just close and reopen Edge the usual way.)
echo.
pause
goto :eof

:cancel
echo Cancelled. Edge was not changed.
pause
