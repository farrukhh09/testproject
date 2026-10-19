@echo off
rem Запуск проверщика номеров. Двойной щелчок по этому файлу.
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Project environment .venv not found in %~dp0
  pause
  exit /b 1
)
".venv\Scripts\python.exe" checker\server.py
pause
