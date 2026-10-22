@echo off
rem Absolute Flight (alpha): start No Man's Sky through Steam with the mod loaded. Keep this window open while you play.
cd /d "%~dp0"
"%~dp0python\python.exe" -E -s "%~dp0play.py"
if errorlevel 1 pause
