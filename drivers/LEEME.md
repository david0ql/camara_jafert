# Drivers de la cámara (Thorlabs DCx / IDS uEye)

`camara.exe` y `polarimetro.exe` traen dentro las DLL y los paquetes de driver de
Thorlabs e IDS, así que no hace falta descargar nada. Lo único que hay que hacer en
un PC nuevo es registrar el driver en Windows, una vez, con **una** de estas opciones:

| Opción | Qué instala |
|---|---|
| Botón **Instalar drivers** de cualquiera de los dos programas | Thorlabs e IDS |
| [`Instalar_driver_ThorCam.bat`](Instalar_driver_ThorCam.bat) | Thorlabs DCx (uc480) 4.80.5, desde `DCx_USB_Driver_Package/` |
| [`Instalar_driver_uEye.bat`](Instalar_driver_uEye.bat) | IDS uEye: el SDK de IDS del PC si lo hay; si no, 4.2.11 desde `uEye_USB_Driver_Package/` |

Cualquiera de los dos drivers sirve para las dos marcas.

## Instalación rápida

1. Con la cámara **desconectada**, pulsar **Instalar drivers** y aceptar el permiso de
   administrador de Windows. Con un `.bat`: click derecho → **Ejecutar como
   administrador**. Por dentro es `pnputil /add-driver … /install` sobre cada `.inf`.
2. Conectar la cámara por USB. En el Administrador de dispositivos debe aparecer
   bajo *Cámaras uEye* / *Thorlabs DCx*, sin signo de advertencia.
3. Pulsar **Detectar cámara**: muestra de qué tipo es (Thorlabs o IDS uEye), con
   modelo y número de serie.

## Por qué no basta con la DLL

La DLL es la API que cargan los programas, y el driver de kernel es el que le habla al
USB y le sube el firmware a la cámara. Hacen falta los dos:

- **DLL:** los programas usan la de `System32` si existe, porque coincide con el driver
  instalado; si no, la copia embebida. El `.inf` solo la copia a `System32` cuando
  Windows instala la cámara, no al registrar el driver sin ella conectada.
- **Driver de kernel:** solo se puede instalar con permiso de administrador. Sin él,
  la cámara aparece como dispositivo desconocido aunque la DLL cargue.

## Driver de IDS uEye

El paquete 4.2.11 es de 2012 y no tiene firma WHQL de Microsoft, solo la de IDS. Windows
podría rechazarlo con *Integridad de memoria* activa; en ese caso el driver de Thorlabs
sigue sirviendo. Para una versión más nueva, instalar
[IDS Software Suite](https://en.ids-imaging.com/downloads.html).

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

`uEye_USB_Driver_Package/` es software de IDS, copiado de una instalación de su SDK, y
no trae una licencia que permita redistribuirlo. Los `.exe` llevan dentro ambos paquetes,
así que estos avisos también se aplican a ellos.

## Reparar los drivers

Si **Detectar cámara** no encuentra la cámara y Windows muestra un problema de driver,
el programa dice cuál es, con el código del Administrador de dispositivos y la versión
del driver, y ofrece dos opciones:

- **Reinstalar los drivers incluidos** (`camara.exe --reparar-drivers`): quita las
  versiones de uc480 y uEye del PC y deja enlazado uc480, que sirve para las dos marcas.
  Otro programa de Thorlabs o IDS podría necesitar reinstalarse después.
- **Buscar en internet** (`camara.exe --buscar-drivers`): busca en Windows Update, con
  la cámara conectada, y solo instala drivers de estas cámaras (`VID_1409`). Si no hay,
  abre las descargas oficiales de Thorlabs e IDS para instalarlas a mano: el programa
  no ejecuta instaladores descargados de otras fuentes. IDS pide una cuenta gratuita.

No se ofrece si el problema es otro: sin cámara visible, o con un dispositivo USB que no
arrancó (código 43), se indica revisar el cable y el puerto.

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
