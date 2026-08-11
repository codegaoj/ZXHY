@echo off
cd /d "%~dp0"
"D:\DevelopTools\Python\Python310\python.exe" tools\version_manager.py snapshot -m "手动版本快照"
pause
