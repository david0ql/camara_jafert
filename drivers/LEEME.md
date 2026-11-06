# Drivers de la cámara (Thorlabs DCx / IDS uEye)

Los programas de `dist/` hablan con la cámara a través de un driver USB, que puede
ser el de Thorlabs o el de IDS. Basta con instalar uno:

| Instalador | Driver | De dónde salen los archivos |
|---|---|---|
| [`Instalar_driver_ThorCam.bat`](Instalar_driver_ThorCam.bat) | Thorlabs DCx (uc480) 4.80.5 | `DCx_USB_Driver_Package/`, incluido en este repositorio |
| [`Instalar_driver_uEye.bat`](Instalar_driver_uEye.bat) | IDS uEye | el SDK de IDS ya instalado en el PC; no se incluye aquí |

Sirven para cualquiera de las dos marcas. Si no hay un motivo para preferir el de
IDS, lo más directo es el de ThorCam, que no requiere descargar nada.

## Instalación rápida

1. Con la cámara **desconectada**, click derecho en el instalador elegido →
   **Ejecutar como administrador**. Ambos ejecutan `pnputil /add-driver … /install`
   sobre el `.inf` del driver.
2. Conectar la cámara por USB. En el Administrador de dispositivos debe aparecer
   bajo *Cámaras uEye* / *Thorlabs DCx*, sin signo de advertencia.
3. Abrir `camara.exe` o `polarimetro.exe`: detecta la cámara sola y muestra de qué
   tipo es (Thorlabs o IDS uEye), con modelo y número de serie. **Detectar cámara**
   vuelve a buscar sin reiniciar.

## Driver de IDS uEye

`Instalar_driver_uEye.bat` busca
`C:\Program Files\IDS\uEye\USB driver package\ueye_64.inf`. Si no está, indica dónde
descargar [IDS Software Suite](https://en.ids-imaging.com/downloads.html). El driver
de IDS debe ser de 64 bits, igual que los programas.

Windows enlaza el mismo identificador USB (`VID_1409`) a uc480 o a uEye y elige el
driver mejor clasificado, así que con ambos instalados puede seguir usando uc480 aunque
se acabe de instalar uEye. No es un error: los programas leen la marca de la propia
cámara y usan la DLL que corresponda, sin cambiar nada.

## Instalación manual

Administrador de dispositivos → clic derecho en la cámara → *Actualizar controlador* →
buscar en `DCx_USB_Driver_Package` o en `C:\Program Files\IDS\uEye\USB driver package`.

## Contenido de `DCx_USB_Driver_Package/`

| Archivo | Para qué |
|---|---|
| `uc480_64.inf`, `uc480_64.cat` | descriptor y firma del driver |
| `uc480_64.sys`, `uc480_boot_64.sys` | driver de kernel; sube el firmware al conectar la cámara |
| `uc480_64.dll` | API que cargan `camara.exe` / `polarimetro.exe` (el `.inf` la copia a `System32`) |
| `uc480_tools_64.dll` | utilidades del SDK (también a `System32`) |
| `Combined_ThorCam_SDK_EULA.rtf` | licencia de Thorlabs que acompaña al software |

Son idénticos a los de `USB Driver Package` en una instalación de *DCx Camera Support*.

## Aviso de licencia

Los archivos de `DCx_USB_Driver_Package/` son software de Thorlabs/IDS y se rigen por
`Combined_ThorCam_SDK_EULA.rtf`. Esa licencia permite usarlos con equipos Thorlabs y
hacer copias de respaldo, pero excluye la reventa, transferencia o distribución salvo
lo que permite expresamente (*«not for resale, transfer or distribution»*); la
transferencia solo se contempla junto con el equipo. Se incluyen por comodidad del
laboratorio, pero este repositorio es público, así que conviene reemplazarlos por un
enlace a la [descarga oficial de ThorCam](https://www.thorlabs.com/software-pages/ThorCam),
que instala *DCx Camera Support* (la cámara DCU224C está descontinuada desde 2021).

Por eso el driver de IDS no se incluye: `Instalar_driver_uEye.bat` usa la copia
instalada en cada PC.

## Si algo falla

- **«No se encontró el SDK»** al abrir el programa: el driver no se instaló, o
  se instaló una versión de 32 bits (hace falta la de 64).
- **`pnputil` termina con error**: el instalador muestra el código. Revisar que se
  ejecutó como administrador y que la carpeta `drivers` se copió completa.
- **Cámara no detectada**: probar otro puerto USB y otro cable; el PID cambia de
  `1000` a `2240` cuando el driver le sube el firmware, así que puede tardar unos
  segundos tras conectarla.
- **Errores de transferencia intermitentes**: ver «Nota sobre el enlace USB» en
  el README y `camara.exe --benchmark`.
- Sin cámara, ambos programas funcionan con `--demo`.
