@echo off
setlocal
cd /d "%~dp0"
set "URL=http://127.0.0.1:8765"

rem Si el panel ya esta abierto, solo abre el navegador.
powershell -NoProfile -Command "try{Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%URL%/api/summary' | Out-Null; exit 0}catch{exit 1}" >nul 2>&1
if not errorlevel 1 goto abrir

if not exist "%~dp0venv\Scripts\pythonw.exe" (
  echo No encuentro el entorno de Python del bot ^(carpeta venv^).
  pause
  exit /b 1
)
start "" "%~dp0venv\Scripts\pythonw.exe" "%~dp0panel_server.py"

echo Abriendo el panel...
rem Espera hasta 30 segundos a que el panel responda.
powershell -NoProfile -Command "for($i=0;$i -lt 60;$i++){try{Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%URL%/api/summary' | Out-Null; exit 0}catch{Start-Sleep -Milliseconds 500}}; exit 1" >nul 2>&1
if errorlevel 1 (
  echo.
  echo El panel no respondio. Probando en modo visible para ver el error:
  echo.
  "%~dp0venv\Scripts\python.exe" "%~dp0panel_server.py"
  pause
  exit /b 1
)

:abrir
start "" %URL%
endlocal
