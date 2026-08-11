@echo off
rem Nhay dup vao file nay de tao shortcut docindex ngoai Desktop
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tao-shortcut.ps1"
pause
