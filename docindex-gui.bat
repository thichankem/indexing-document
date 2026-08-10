@echo off
rem Mo giao dien docindex bang cach nhay doi vao file nay
cd /d "%~dp0"
start "" pythonw -m docindex.gui
