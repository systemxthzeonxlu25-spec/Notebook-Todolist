@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto failed
set "PYTHONUTF8=1"
set "NOTEBOOK_INSTALL_ATTEMPTED="
set "NOTEBOOK_PYTHON="

:find_python
py -3.12 -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>&1
if not errorlevel 1 goto use_launcher
python -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>&1
if not errorlevel 1 goto use_python
for %%P in ("%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312-32\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312-arm64\python.exe" "%ProgramFiles%\Python312\python.exe" "%ProgramFiles%\Python312-arm64\python.exe" "%ProgramFiles(x86)%\Python312-32\python.exe") do (
    if exist "%%~P" (
        "%%~P" -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>&1
        if not errorlevel 1 (
            set "NOTEBOOK_PYTHON=%%~P"
            goto use_path
        )
    )
)
if defined NOTEBOOK_INSTALL_ATTEMPTED goto no_python
where winget >nul 2>&1
if errorlevel 1 goto no_python
set "NOTEBOOK_INSTALL_ATTEMPTED=1"
echo Python 3.12 was not found. Installing it for the current user...
winget install --id Python.Python.3.12 --exact --source winget --scope user --silent --disable-interactivity --accept-package-agreements --accept-source-agreements --custom "Include_launcher=0 InstallLauncherAllUsers=0"
if errorlevel 1 goto no_python
goto find_python

:use_launcher
py -3.12 "%~dp0scripts\bootstrap_local.py" %*
goto finished

:use_python
python "%~dp0scripts\bootstrap_local.py" %*
goto finished

:use_path
"%NOTEBOOK_PYTHON%" "%~dp0scripts\bootstrap_local.py" %*
goto finished

:no_python
echo.
echo Python 3.12 could not be prepared automatically.
echo Install Python 3.12 for your user from https://www.python.org/downloads/windows/
echo Then run this file again. See LOCAL.md for help.
goto failed

:finished
set "NOTEBOOK_EXIT_CODE=%ERRORLEVEL%"
if "%NOTEBOOK_EXIT_CODE%"=="0" exit /b 0
echo.
echo The local app stopped or could not start. See the message above and LOCAL.md.
pause
exit /b %NOTEBOOK_EXIT_CODE%

:failed
echo.
echo Setup stopped. Your existing configuration and data were not replaced.
pause
exit /b 1
