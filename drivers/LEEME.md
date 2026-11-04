# Driver de la cámara (Thorlabs DCx / uEye)

Los programas de `dist/` hablan con la cámara a través del driver USB de
**DCx Camera Support** de Thorlabs. Este repositorio incluye el paquete de
driver, copiado de una instalación de DCx Camera Support, para poder instalar
sin descargar nada.

## Instalación rápida

1. Con la cámara **desconectada**, click derecho en
   [`Instalar_driver.bat`](Instalar_driver.bat) → **Ejecutar como administrador**.
   Ejecuta `pnputil /add-driver DCx_USB_Driver_Package쒀_64.inf /install`.
2. Conectar la cámara por USB. En el Administrador de dispositivos debe aparecer
   bajo *Cámaras uEye* / *Thorlabs DCx*, sin signo de advertencia.
3. Abrir `camara.exe` o `polarimetro.exe` y pulsar **Detectar cámara ThorCam**:
   si todo está bien, muestra modelo y número de serie.

Instalación manual equivalente: Administrador de dispositivos → clic derecho en
la cámara → *Actualizar controlador* → buscar en `DCx_USB_Driver_Package`.

## Contenido de `DCx_USB_Driver_Package/`

| Archivo | Para qué |
|---|---|
| `uc480_64.inf`, `uc480_64.cat` | descriptor y firma del driver |
| `uc480_64.sys`, `uc480_boot_64.sys` | driver de kernel; sube el firmware al conectar la cámara |
| `uc480_64.dll` | API que cargan `camara.exe` / `polarimetro.exe` (el `.inf` la copia a `System32`) |
| `uc480_tools_64.dll` | utilidades del SDK (también a `System32`) |
| `Combined_ThorCam_SDK_EULA.rtf` | licencia de Thorlabs que acompaña al software |

## Aviso de licencia

Estos archivos son software de Thorlabs/IDS y se rigen por su licencia
(`Combined_ThorCam_SDK_EULA.rtf`), que concede derecho de uso y de copia de
respaldo pero **no menciona redistribución**. Se incluyen aquí por comodidad del
laboratorio; si el repositorio se hace público o se comparte fuera del grupo,
conviene reemplazarlos por un enlace a la descarga oficial de Thorlabs
(*DCx Camera Support*, instalado junto con *ThorCam*; la cámara DCU224C está
descontinuada desde 2021).

## Si algo falla

- **«No se encontró el SDK»** al abrir el programa: el driver no se instaló, o
  se instaló una versión de 32 bits (hace falta la de 64).
- **Cámara no detectada**: probar otro puerto USB y otro cable; el PID cambia de
  `1000` a `2240` cuando el driver le sube el firmware, así que puede tardar unos
  segundos tras conectarla.
- **Errores de transferencia intermitentes**: ver «Nota sobre el enlace USB» en
  el README y `camara.exe --benchmark`.
- Sin cámara, ambos programas funcionan con `--demo`.
