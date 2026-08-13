@echo off
cd /d "%~dp0"
set "PYTHON_EXE=D:\DevelopTools\Python\Python310\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"
"%PYTHON_EXE%" tools\trading_web_panel.py
pause
