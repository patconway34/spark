@echo off
REM ===========================================================================
REM  Spark - START EVERYTHING
REM
REM  One script, two halves, in the order they depend on each other:
REM
REM    1. WSL  : tmux sessions spark1-7 + a ttyd terminal each (ports 7682-7688)
REM    2. Win  : the Flask app on 5023, which is also prod - Radar launches the
REM              same file from the same folder
REM
REM  The terminals come first because the app's tab iframes point at them, and
REM  a tab whose ttyd is not listening renders blank.
REM
REM  Existing tmux sessions are never touched, so a Claude conversation that is
REM  mid-task survives a restart of either half.
REM
REM  To stop: kill.bat (frees 5023). To restart just the app: Radar, or
REM  radar_start.bat.
REM ===========================================================================
title Spark - start all
cd /d C:\dev\spark

echo.
echo [1/2] Terminals (tmux + ttyd, in WSL)...
wsl bash /mnt/c/dev/spark/start.sh
if errorlevel 1 (
    echo.
    echo   WSL step failed. The app will still start, but tabs may be blank.
    echo.
)

echo.
echo [2/2] Spark app on port 5023...
REM Free the port first so a stale process cannot keep the new one from binding.
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :5023 ^| findstr LISTENING') do (
    echo   killing stale PID %%a
    taskkill /F /PID %%a /T >nul 2>&1
)

echo.
echo   Local    : http://localhost:5023
echo   Phone    : your tunnel hostname (cloudflared must be running)
echo   Dashboard: http://localhost:5023/dashboard
echo.
C:\Users\Patrick\miniconda3\python.exe app.py
