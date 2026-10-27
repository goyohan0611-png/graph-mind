@echo off
rem Turnbook setup: double-click. Installs Turnbook for this Windows user (no Python or
rem administrator rights needed) and connects Claude, Codex, Cursor and the other AI apps found.
chcp 65001 >nul
echo Turnbook setup
echo.
set /p GM_NAME=Your name, as teammates would see it (Enter to skip):
if "%GM_NAME%"=="" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/scripts/install.ps1 | iex"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/goyohan0611-png/turnbook/main/scripts/install.ps1))) --name ($env:GM_NAME)"
)
echo.
echo Done. Restart your AI apps (Claude, Codex, Cursor...) so they load the memory.
pause
