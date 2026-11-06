@echo off
setlocal
rem Instala el driver USB de Thorlabs DCx (uc480), incluido en este repositorio.
rem Requiere administrador. Sirve tambien para camaras IDS uEye.

set "INF=%~dp0DCx_USB_Driver_Package\uc480_64.inf"
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
