@echo off
setlocal
set "MIL_ROOT=%~dp0.."
if not exist "%MIL_ROOT%\lib\mil\__init__.py" (
  echo mil: expected Python package at %MIL_ROOT%\lib\mil 1>&2
  exit /b 2
)
if defined PYTHONPATH (
  set "PYTHONPATH=%MIL_ROOT%\lib;%PYTHONPATH%"
) else (
  set "PYTHONPATH=%MIL_ROOT%\lib"
)
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONIOENCODING=utf-8"
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 (
  py -3 -m mil %*
  exit /b
)
python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 (
  python3 -m mil %*
  exit /b
)
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 (
  python -m mil %*
  exit /b
)
echo mil: Python 3.10+ (py -3, python3, or python) not found on PATH 1>&2
exit /b 2
