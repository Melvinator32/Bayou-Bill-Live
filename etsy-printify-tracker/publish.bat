@echo off
REM Sync Etsy + Printify, then publish the public snapshot.
REM Run from the etsy-printify-tracker folder of your Bayou-Bill-Live clone;
REM data.json is written to the repo root, one folder up, then pushed.
set REPO=..

cd /d "%~dp0"
.venv\Scripts\python.exe publish_snapshot.py --sync --repo "%REPO%"
pause
