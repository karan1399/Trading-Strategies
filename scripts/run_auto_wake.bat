@echo off
title Antigravity Auto Wake Daemon (:00 and :30)
cd /d "%~dp0\.."
echo Starting Antigravity Auto Wake Daemon...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\antigravity_auto_wake.ps1"
pause
