
@echo off
cd /d E:\NEXUS

echo Starting NEXUS Core...

".venv\Scripts\python.exe" -m uvicorn nexus.core.main:app --reload --app-dir src

pause
