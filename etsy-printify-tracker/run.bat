@echo off
setlocal
cd /d "%~dp0"

set "PYEXE="
py -3 --version >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE python --version >nul 2>nul && set "PYEXE=python"
if not defined PYEXE (
    echo.
    echo ============================================================
    echo  Python was not found. Install it from python.org and tick
    echo  "Add python.exe to PATH" during setup, then run this again.
    echo ============================================================
    echo.
    pause
    exit /b 1
)
echo Using Python via: %PYEXE%

if not exist ".venv\Scripts\activate.bat" (
    echo Creating virtual environment...
    %PYEXE% -m venv .venv
    if errorlevel 1 ( echo. & echo Could not create the virtual environment. & pause & exit /b 1 )
)
call ".venv\Scripts\activate.bat"

python -c "import flask, dateutil, requests" >nul 2>nul
if errorlevel 1 (
    echo.
    echo Installing dependencies, this takes a minute...
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    if errorlevel 1 ( echo. & echo Dependency install failed - see messages above. & pause & exit /b 1 )
)

echo.
echo ============================================================
echo  Bayou Bill Tracker is starting. Open this in your browser:
echo.
echo      http://localhost:2298
echo.
echo  Keep this window open. Press Ctrl+C here to stop.
echo ============================================================
echo.
python app.py

echo.
echo The app has stopped. Read any message above for the reason.
pause
