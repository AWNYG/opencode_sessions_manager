@echo off
rem Build oc-sessions.exe with PyInstaller
cd /d "%~dp0"

python -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo Installing PyInstaller...
    python -m pip install pyinstaller
    python -m PyInstaller --version >nul 2>nul
    if errorlevel 1 goto :err
)

python -m PyInstaller --onefile --console --name oc-sessions --clean opencode_sessions.py
if errorlevel 1 goto :err
echo.
echo Build OK: dist\oc-sessions.exe
goto :eof
:err
echo Build failed.
exit /b 1
