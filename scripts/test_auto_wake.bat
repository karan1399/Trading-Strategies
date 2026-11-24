@echo off
title Test Antigravity Auto Wake (Immediate Trigger)
cd /d "%~dp0\.."
echo Testing Antigravity Auto Wake immediately...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\antigravity_auto_wake.ps1" -TestNow
pause
