@echo off
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
set "SHIPS_LOG_DIR=C:\ProgramData\SHIPS\logs"
"%~dp0.venv\Scripts\python.exe" -m ships_mcp %*
