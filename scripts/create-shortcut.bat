@echo off
rem Double-click this file to create the docindex shortcuts on your Desktop.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create-shortcut.ps1"
pause
