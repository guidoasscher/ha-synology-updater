@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul

:: ==============================================================================
:: Configuration of run_update.bat
:: ==============================================================================
cd /d "%~dp0"

:: Default mode when double-clicking without arguments:
:: Leave empty ("") to open the Graphical Desktop Interface (GUI),
:: or set to "--auto" to run an unattended update immediately.
set "DEFAULT_ARGS="

set "VENV_DIR=%~dp0.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"

:: ==============================================================================
:: 1. Ensure .env exists
:: ==============================================================================
if exist "%~dp0.env" goto :check_venv
if not exist "%~dp0.env.example" goto :check_venv
echo [INFO] .env not found. Copying .env.example to .env...
copy /Y "%~dp0.env.example" "%~dp0.env" >nul

:: ==============================================================================
:: 2. Check if Virtual Environment (.venv) exists
:: ==============================================================================
:check_venv
if exist "%PYTHON_EXE%" goto :run_app

echo [INFO] Searching for Python 3 installation on this system...
set "SYS_PYTHON="

:: Check Windows Python Launcher (py.exe) first
where py >nul 2>nul
if not errorlevel 1 (
    py -3 --version >nul 2>nul
    if not errorlevel 1 set "SYS_PYTHON=py -3"
)

:: Check standard python.exe (ignoring the empty Windows Store alias)
if "%SYS_PYTHON%"=="" (
    python --version >nul 2>nul
    if not errorlevel 1 set "SYS_PYTHON=python"
)

:: Search common Windows Python installation directories
if "%SYS_PYTHON%"=="" (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "C:\Python3*" "%ProgramFiles%\Python3*") do (
        if exist "%%~fD\python.exe" set "SYS_PYTHON="%%~fD\python.exe""
    )
)

if not "%SYS_PYTHON%"=="" goto :create_venv

echo [WARNING] No working Python 3 installation found on this PC.
echo [INFO] Attempting to install Python 3.12 automatically via winget...
where winget >nul 2>nul
if errorlevel 1 goto :no_python_error

winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%~fD\python.exe" set "SYS_PYTHON="%%~fD\python.exe""
)
if not "%SYS_PYTHON%"=="" goto :create_venv

:no_python_error
echo.
echo [ERROR] Python 3 is not installed on this Windows PC.
echo Please install Python 3.10+ from https://www.python.org/downloads/
echo Make sure to check "Add python.exe to PATH" and run run_update.bat again.
pause
exit /b 1

:create_venv
echo [INFO] Creating Python virtual environment in .venv...
%SYS_PYTHON% -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo [ERROR] Failed to create Python virtual environment.
    pause
    exit /b 1
)

echo [INFO] Installing dependencies from requirements.txt...
"%PYTHON_EXE%" -m pip install --upgrade pip >nul
"%PYTHON_EXE%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies from requirements.txt.
    pause
    exit /b 1
)

:: ==============================================================================
:: 3. Launch the Home Assistant Synology Updater
:: ==============================================================================
:run_app
set "RUN_ARGS=%*"
if "%RUN_ARGS%"=="" set "RUN_ARGS=%DEFAULT_ARGS%"

"%PYTHON_EXE%" "%~dp0ha_updater.py" %RUN_ARGS%
set "EXIT_CODE=%ERRORLEVEL%"

echo "%RUN_ARGS%" | findstr /I "\-\-auto" >nul
if not errorlevel 1 exit /b %EXIT_CODE%

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [WARNING] Process exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
