@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "APP_PYTHON=.venv\Scripts\python.exe"
set "APP_PYTHONW=.venv\Scripts\pythonw.exe"

if exist "%APP_PYTHON%" (
    "%APP_PYTHON%" -c "import sys; assert sys.version_info >= (3, 12); import collection_manager, PySide6, sqlalchemy, alembic" >nul 2>&1
    if not errorlevel 1 goto launch
    "%APP_PYTHON%" -c "import sys; assert sys.version_info >= (3, 12)" >nul 2>&1
    if not errorlevel 1 goto install
)

where py >nul 2>&1
if errorlevel 1 goto no_python
py -3.12 -c "import sys; assert sys.version_info >= (3, 12)" >nul 2>&1
if errorlevel 1 goto no_python

echo Preparing Collection Manager for first use...
if exist "%APP_PYTHON%" (
    py -m venv --upgrade .venv
) else (
    py -m venv .venv
)
if errorlevel 1 goto setup_failed

:install
echo Installing Collection Manager dependencies. This is only needed once...
"%APP_PYTHON%" -m pip install -e .
if errorlevel 1 goto setup_failed

:launch
if not exist "%APP_PYTHONW%" set "APP_PYTHONW=%APP_PYTHON%"
start "" "%APP_PYTHONW%" -m collection_manager
exit /b 0

:no_python
echo.
echo Collection Manager requires Python 3.12 or newer.
echo Install Python 3.12 from https://www.python.org/downloads/windows/
echo Make sure the Python launcher option is enabled, then double-click this file again.
echo.
pause
exit /b 1

:setup_failed
echo.
echo Collection Manager could not finish its one-time setup.
echo Check your internet connection and try this launcher again.
echo.
pause
exit /b 1

