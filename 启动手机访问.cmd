@echo off
setlocal
set "WB_ROOT=%~dp0"
set "WB_PY=%LocalAppData%\Programs\Python\Python312\python.exe"
cd /d "%WB_ROOT%"
if not exist "%WB_PY%" (
    py -3.12 "%WB_ROOT%launch_mobile.py"
) else (
"%WB_PY%" "%WB_ROOT%launch_mobile.py"
)
if errorlevel 1 pause
endlocal
