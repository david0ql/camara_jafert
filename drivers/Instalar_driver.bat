@echo off
rem Instala el driver USB de las camaras Thorlabs DCx (uEye). Requiere administrador.
net session >nul 2>&1
if errorlevel 1 (
    echo Ejecuta este archivo con click derecho - Ejecutar como administrador.
    pause
    exit /b 1
)
pnputil /add-driver "%~dp0DCx_USB_Driver_Package\uc480_64.inf" /install
echo.
echo Listo. Conecta la camara por USB y abre camara.exe o polarimetro.exe.
pause
