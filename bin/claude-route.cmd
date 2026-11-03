@echo off
rem Run claude-route straight from a checkout, no install required.
rem Copy this file into a directory on PATH and keep the checkout at a fixed path.
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%~dp0..\src;%PYTHONPATH%"

set "PYEXE=python"
where python >nul 2>nul || set "PYEXE=py -3"

"%PYEXE%" -m claude_route %*
exit /b %errorlevel%
