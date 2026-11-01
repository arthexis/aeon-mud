@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0aeon-mud-wezterm.ps1" %*
exit /b %ERRORLEVEL%
