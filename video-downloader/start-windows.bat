@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
title Tahmeeli

set PY=
where py >nul 2>nul && set PY=py
if not defined PY ( where python >nul 2>nul && set PY=python )
if not defined PY (
  echo.
  echo  [X] Python is not installed.  بايثون غير مثبت
  echo      Install it from python.org and tick "Add Python to PATH"
  start https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo  First run: installing, please wait...  أول تشغيل: نجهّز التطبيق، انتظر شوي...
  %PY% -m venv .venv || goto :fail
)
".venv\Scripts\python.exe" -m pip install -q -U --disable-pip-version-check -r requirements.txt || goto :fail

set OPEN_BROWSER=1
".venv\Scripts\python.exe" app.py
pause
exit /b 0

:fail
echo.
echo  [X] Setup failed. Check your internet connection and try again.
pause
exit /b 1
