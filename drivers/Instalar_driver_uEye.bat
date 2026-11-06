@echo off
setlocal
rem Instala el driver USB de IDS uEye. Requiere administrador.
rem Prefiere el SDK de IDS de este PC, si lo hay; si no, usa el paquete incluido.

set "PF=%ProgramFiles%"
if defined ProgramW6432 set "PF=%ProgramW6432%"
set "INF=%PF%\IDS\uEye\USB driver package\ueye_64.inf"
if not exist "%INF%" set "INF=%~dp0uEye_USB_Driver_Package\ueye_64.inf"
if not exist "%INF%" goto sin_paquete

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

:sin_paquete
echo ERROR: no se encontro el paquete del driver:
echo   %INF%
echo Copia la carpeta drivers completa, no solo este archivo.
pause
exit /b 1

:sin_admin
echo Ejecuta este archivo con click derecho - Ejecutar como administrador.
pause
exit /b 1
