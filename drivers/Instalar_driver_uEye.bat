@echo off
setlocal
rem Instala el driver USB de IDS uEye desde el SDK de IDS ya instalado en este PC.
rem Los archivos de IDS no van en el repositorio (ver LEEME.md). Requiere administrador.

set "PF=%ProgramFiles%"
if defined ProgramW6432 set "PF=%ProgramW6432%"
set "INF=%PF%\IDS\uEye\USB driver package\ueye_64.inf"
if not exist "%INF%" goto sin_sdk

net session >nul 2>&1
if errorlevel 1 goto sin_admin

pnputil /add-driver "%INF%" /install
set "RC=%errorlevel%"
echo.
rem 0: instalado. 259: paquete agregado, sin dispositivo que actualizar. 3010: falta reiniciar.
if "%RC%"=="0" goto listo
if "%RC%"=="259" goto listo
if "%RC%"=="3010" goto reiniciar
echo ERROR: pnputil termino con el codigo %RC%. El driver no se instalo.
pause
exit /b %RC%

:listo
echo Listo. Conecta la camara por USB y abre camara.exe o polarimetro.exe.
echo Si el driver de Thorlabs tambien esta instalado, Windows puede seguir usandolo:
echo es normal, y los programas funcionan con cualquiera de los dos.
pause
exit /b 0

:reiniciar
echo Listo, pero hay que reiniciar Windows antes de usar la camara.
pause
exit /b 0

:sin_sdk
echo No se encontro el SDK de IDS uEye en este PC. Se busco:
echo   %INF%
echo.
echo Descarga e instala IDS Software Suite desde el sitio de IDS:
echo   https://en.ids-imaging.com/downloads.html
echo y vuelve a ejecutar este archivo si la camara no aparece.
echo.
echo Alternativa sin descargas: Instalar_driver_ThorCam.bat tambien sirve para camaras uEye.
pause
exit /b 1

:sin_admin
echo Ejecuta este archivo con click derecho - Ejecutar como administrador.
pause
exit /b 1
