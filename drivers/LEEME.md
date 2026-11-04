# Driver de la cámara (Thorlabs DCx / uEye)

Los programas de `dist/` hablan con la cámara a través del SDK **DCx Camera
Support** de Thorlabs. Ese paquete trae el driver de kernel (el que hace que
Windows reconozca la cámara) y las librerías que el programa carga:

| Archivo | Dónde queda | Para qué |
|---|---|---|
| `uc480_64.dll` | `C:\Windows\System32` | API que usa `camara.exe` / `polarimetro.exe` |
| `uc480_tools_64.dll` | `C:\Windows\System32` | utilidades del SDK |
| `uc480_64.sys`, `uc480_boot_64.sys` | `C:\Windows\System32\drivers` | driver de kernel; sube el firmware al conectar la cámara |

## Instalación

1. Descargar **DCx Camera Support** (instalador de Windows de 64 bits) desde la
   página de software de Thorlabs, sección *DCx Cameras* (en
   <https://www.thorlabs.com> buscar "DCx Camera Support"). Sirve también el
   paquete *uEye* de IDS, el fabricante original.
2. Ejecutar el instalador **antes** de conectar la cámara y reiniciar si lo pide.
3. Conectar la cámara por USB. En el Administrador de dispositivos debe aparecer
   bajo *Cámaras uEye* / *Thorlabs DCx*, sin signo de advertencia.
4. Abrir `camara.exe` (o `polarimetro.exe`) y pulsar **Detectar cámara ThorCam**.
   Si el SDK está bien instalado y la cámara conectada, muestra modelo y número
   de serie.

## Por qué no están los archivos en este repositorio

Los `.dll` y `.sys` son software propietario de Thorlabs/IDS y no se
redistribuyen aquí. Además, copiar solo los archivos no sirve: el `.sys` hay que
registrarlo con el instalador oficial para que Windows reconozca la cámara.

## Si algo falla

- **«No se encontró el SDK»** al abrir el programa: falta instalar DCx Camera
  Support, o se instaló la versión de 32 bits (hace falta la de 64).
- **Cámara no detectada pese a tener el SDK**: probar otro puerto USB y otro
  cable; el PID de la cámara cambia de `1000` a `2240` cuando el driver le sube
  el firmware, así que puede tardar unos segundos tras conectarla.
- **Errores de transferencia intermitentes**: ver «Nota sobre el enlace USB» en
  el README, y `camara.exe --benchmark` para medirlos.
- Sin cámara, ambos programas funcionan con `--demo`.
