@echo off
setlocal
cd /d "%~dp0"

echo Conectando el bot unicamente con @ivkomaderas_ mediante Instagram Login...
echo No hace falta vincular una Pagina de Facebook.
echo No compartas capturas que muestren tokens ni claves.
echo.

if not exist ".env" (
  echo Falta el archivo .env. No se guardo ningun dato.
  pause
  exit /b 1
)
if not exist ".\venv\Scripts\python.exe" (
  echo Falta el entorno de Python del bot.
  pause
  exit /b 1
)

rem El token anterior fue revocado. El asistente pedira el nuevo token de Meta sin mostrarlo.
set "META_SHORT_TOKEN="
".\venv\Scripts\python.exe" -B ".\obtener_token.py"
if errorlevel 1 (
  echo.
  echo Meta no pudo completar la conexion. Revisa el mensaje anterior.
  pause
  exit /b 1
)

echo.
echo Comprobando la cuenta conectada...
".\venv\Scripts\python.exe" -B ".\main.py" init
if errorlevel 1 (
  echo.
  echo La conexion se guardo, pero la comprobacion no termino.
  pause
  exit /b 1
)

echo.
echo Meta quedo conectada a @ivkomaderas_.
pause
endlocal
