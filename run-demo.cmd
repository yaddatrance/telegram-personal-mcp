@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m ken_mcp demo --data-dir ".local\demo" --open
