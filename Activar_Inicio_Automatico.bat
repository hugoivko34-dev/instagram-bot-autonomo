@echo off
setlocal
cd /d "%~dp0"
set "ACCESO=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Bot Instagram ivkomaderas.lnk"
set "PYW=%~dp0venv\Scripts\pythonw.exe"
set "ARGS="%~dp0main.py" run --archivo-log"
set "DIR=%~dp0"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:ACCESO);$s.TargetPath=$env:PYW;$s.Arguments=$env:ARGS;$s.WorkingDirectory=$env:DIR;$s.WindowStyle=7;$s.Save()"
if not exist "%ACCESO%" (
  echo No se pudo crear el inicio automatico.
  pause
  exit /b 1
)
start "" "%PYW%" "%~dp0main.py" run --archivo-log
echo Listo. El bot ya esta corriendo en segundo plano y arrancara solo cada vez que inicies sesion.
echo El registro queda en salida\bot.log. Para desactivarlo usa Desactivar_Inicio_Automatico.bat
pause
endlocal
