@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 start.py
) else (
  python start.py
)
if errorlevel 1 (
  echo 启动未完成。请安装 Python 3.10 或更新版本，并勾选 Add Python to PATH。
  pause
)
