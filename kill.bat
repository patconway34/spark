@echo off
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :5023 ^| findstr LISTENING') do (
    taskkill /F /PID %%a /T
    echo Killed PID %%a
)
