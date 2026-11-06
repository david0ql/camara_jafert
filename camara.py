# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "numpy>=1.26",
#     "opencv-python>=4.9",
#     "questionary>=2.0",
#     "rich>=13.7",
#     "pytest>=8.0",
# ]
# ///
"""Visor y fotometro RGB para camaras Thorlabs DCx e IDS uEye.

Software de camara: visor en vivo, lectura RGB con decimales, perfiles de
intensidad, dark frame, guardado de imagenes y los ajustes de camara de ThorCam.
Probado sobre una Thorlabs DCU224C (1280x1024, CCD color, 4.65 um/px, 8 bits) y
una IDS uEye C1280R12C (1280x1024, CMOS color rolling, 5.20 um/px, 8 bits).

Las DCx son uEye con otra marca: uc480_64.dll y uEye_api_64.dll exportan la misma
API C con las mismas constantes, asi que un solo binding sirve para las dos. Lo
que cambia es que driver tiene Windows enlazado a la camara, y eso no depende de
la marca: una uEye enlazada al driver uc480 solo aparece por uc480_64.dll. Por
eso se enumeran las dos DLL y la marca se lee de la propia camara.

Al abrir, la cadena de imagen se fuerza a respuesta lineal: gamma 1.0, sin
auto-ganancia, auto-exposicion ni balance de blancos, y sin correccion de color.
Cualquiera de esas etapas rompe la proporcionalidad entre señal e intensidad
incidente, que es lo que hace cuantitativa a una medida de fotometria. La ventana
de ajustes permite salir de ahi, y la interfaz avisa mientras se este fuera.

    uv run camara.py                 interfaz grafica
    uv run camara.py --viewer        visor de OpenCV por linea de comandos
    uv run camara.py --self-test     tests

El sensor entrega enteros de 0 a 255; no existe mayor profundidad (los modos de
10/12/16 bits los rechaza con IS_INVALID_COLOR_FORMAT). La precision por debajo
de 1 nivel se consigue sumando señal, en este orden de eficacia:

  1. binning por hardware, que suma carga en el CCD antes del ADC
  2. promediado espacial sobre el ROI
  3. promediado temporal sobre varios frames

Este modulo tambien es la base del polarimetro: polarimetro.py importa de aca
la capa de camara, el guardado y el estilo de la interfaz.
"""
from __future__ import annotations

import argparse
import base64
import csv
import ctypes as C
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

import numpy as np

# Los tipos de Windows se declaran a mano en vez de importar ctypes.wintypes,
# que solo existe en Windows. Asi el modulo se importa en cualquier sistema y
# el analisis, el modo demo y los tests corren tambien en macOS o Linux.
WORD, DWORD, BOOL = C.c_uint16, C.c_uint32, C.c_int32

# Constantes de uc480.h (DCx Camera Support) y uEye.h (IDS): coinciden en ambos.
IS_SUCCESS = 0
IS_WAIT = 0x0001
IS_TIMED_OUT = 122
IS_NOT_SUPPORTED = 155
IS_TRANSFER_ERROR = 178
IS_IGNORE_PARAMETER = -1
IS_CM_BGR8_PACKED = 1
IS_CM_MONO8 = 6
IS_CM_SENSOR_RAW8 = 11
IS_SET_DM_DIB = 1
IS_SET_GAMMA_OFF = 0
IS_SET_GAINBOOST_OFF = 0
IS_CCOR_DISABLE = 0
IS_BL_COMPENSATION_DISABLE = 0
IS_TRIGGER_TIMEOUT = 0
IS_PIXELCLOCK_CMD_GET = 5
IS_PIXELCLOCK_CMD_SET = 6
IS_EXPOSURE_CMD_RANGE_MIN = 3
IS_EXPOSURE_CMD_RANGE_MAX = 4
IS_EXPOSURE_CMD_GET = 7
IS_EXPOSURE_CMD_SET = 12
IS_EXPOSURE_CMD_CAPS = 1
IS_EXPOSURE_CMD_INC = 5
IS_EXPOSURE_CMD_LARGA_RANGO = 16
IS_EXPOSURE_CMD_LARGA_SET = 18
IS_EXPOSURE_CAP_LARGA = 0x4
IS_PIXELCLOCK_CMD_RANGO = 3
IS_GET_RGB = (0x8001, 0x8002, 0x8003)
IS_GET_DEFAULT_RGB = (0x8005, 0x8006, 0x8007)
IS_GET_MASTER_GAIN = 0x8000
IS_GET_SUPPORTED = 0x8001        # binning y submuestreo: mascara de lo que admite
IS_GET_SUPPORTED_GAINBOOST = 0x0002
IS_SET_GAINBOOST_ON = 0x0001
IS_GET_BL_SUPPORTED_MODE = 0x8004
IS_BL_COMPENSATION_ENABLE = 1
IS_BL_COMPENSATION_OFFSET = 32
IS_GET_HW_SUPPORTED_GAMMA = 0x8001
IS_SET_HW_GAMMA_ON = 0x0001
IS_SET_ROP_MIRROR_UPDOWN = 8
IS_SET_ROP_MIRROR_LEFTRIGHT = 64
IS_AOI_IMAGE_GET_AOI = 2
IS_EDGE_RANGO, IS_EDGE_SET = 1, 4
IS_SET_ENABLE_AUTO_GAIN = 0x8800
IS_SET_ENABLE_AUTO_SHUTTER = 0x8802
IS_SET_ENABLE_AUTO_WB = 0x8804
IS_SET_TRIGGER_OFF = 0x0000
IS_GET_TRIGGER_STATUS = 0x8001
IS_GET_MIN_TRIGGER_DELAY, IS_GET_MAX_TRIGGER_DELAY = 0x8001, 0x8002
IS_FEATURE_GET, IS_SHUTTER_SET = 1, 6
IS_LOG_MODO_SET, IS_LOG_VALOR_RANGO, IS_LOG_VALOR_SET = 13, 15, 17
IS_LOG_GAIN_RANGO, IS_LOG_GAIN_SET = 19, 21
IS_FEATURE_LOG = 0x20
IS_IO_FLASH_MIN, IS_IO_FLASH_MAX, IS_IO_FLASH_SET_PARAMS = 14, 15, 18
IS_IO_FLASH_SET_MODO = 20
IS_IO_GPIOS_SOPORTADOS, IS_IO_GPIO_SET = 1, 30
IS_IO_PWM_GPIOS, IS_IO_PWM_MIN, IS_IO_PWM_MAX, IS_IO_PWM_SET = 21, 22, 23, 26

# is_SetAutoParameter: todos van a 0.0 para dejar el sensor en manual.
AUTO_PARAMS = (0x8800, 0x8802, 0x8804, 0x8806)  # ganancia, shutter, WB, framerate

# Binning vertical del arranque por CLI (Config.binning). Lo que admite de verdad
# cada camara se lee de su mascara: la DCU224C declara 0x0015 y la uEye C1280R12C
# no admite ninguno.
BINNING_MODES = {1: 0x00, 2: 0x0001, 3: 0x0010, 4: 0x0004}

# SNR medido sobre una DCU224C a 20 ms, relativo a sin binning.
BINNING_SNR = {1: 1.00, 2: 1.62, 3: 2.22, 4: 2.34}

# Bit de cada factor en las mascaras de is_SetBinning e is_SetSubSampling.
FACTORES_V = {2: 0x0001, 3: 0x0010, 4: 0x0004, 5: 0x0040, 6: 0x0100, 8: 0x0400, 16: 0x1000}
FACTORES_H = {2: 0x0002, 3: 0x0020, 4: 0x0008, 5: 0x0080, 6: 0x0200, 8: 0x0800, 16: 0x2000}

PIXEL_CLOCKS = (12, 16, 20, 24, 30)  # MHz, los que barre el diagnostico USB

# Las dos APIs exportan las mismas funciones; solo cambia que DLL se carga.
DRIVERS = {
    "uc480": {"dll": "uc480_64", "dll32": "uc480",
              "sdk": "DCx Camera Support (Thorlabs)",
              "paquete": "DCx_USB_Driver_Package", "inf": "uc480_64.inf"},
    "ueye": {"dll": "uEye_api_64", "dll32": "uEye_api",
             "sdk": "IDS Software Suite (uEye)",
             "paquete": "uEye_USB_Driver_Package", "inf": "ueye_64.inf"},
}


class UC480Error(RuntimeError):
    pass


class TriggerTimeout(UC480Error):
    """Paso el tiempo de espera del trigger sin que llegara ningun pulso."""


class CapturaCancelada(UC480Error):
    """Se detuvo el visor mientras una captura esperaba un trigger."""


@dataclass(frozen=True)
class Config:
    exposure_ms: float = 20.0
    gain: int = 0                 # ganancia master del hardware, 0..100
    binning: int = 1              # 1 = sin binning; 2, 3 o 4 = vertical
    driver: str = "auto"          # auto, uc480 (Thorlabs) o ueye (IDS)
    camera_id: int = 0            # 0 toma la primera camara libre
    pixel_clock: int = 0          # MHz; 0 deja el valor por defecto del driver
    fps: float = 0.0              # 0 pide el maximo que permita el pixel clock
    retries: int = 8              # reintentos ante IS_TRANSFER_ERROR
    roi_radius: int = 3           # se promedia un parche de (2r+1)^2 pixeles
    decimals: int = 3
    average: int = 1              # frames promediados temporalmente
    mono: bool = False
    raw: bool = False             # Bayer sin interpolar
    normalize: bool = False       # reporta 0..1 en lugar de 0..255
    sat_level: int = 255
    csv_path: Path = field(default_factory=lambda: Path("medidas.csv"))
    dark_path: Path = field(default_factory=lambda: Path("dark.npy"))
    use_dark: bool = False
    scale: float = 0.75
    demo: bool = False

    def __post_init__(self) -> None:
        if self.roi_radius < 0:
            raise ValueError("roi_radius debe ser >= 0")
        if not 0 <= self.decimals <= 6:
            raise ValueError("decimals debe estar entre 0 y 6")
        if self.average < 1:
            raise ValueError("average debe ser >= 1")
        if not 0 <= self.gain <= 100:
            raise ValueError("gain debe estar entre 0 y 100")
        if self.binning not in BINNING_MODES:
            raise ValueError(f"binning debe ser uno de {sorted(BINNING_MODES)}")
        # Cota amplia a proposito: el rango real lo declara cada camara (5-30 en
        # la DCU224C, 5-43 en la C1280R12C) y el SDK rechaza lo que no admite.
        if self.pixel_clock and not 1 <= self.pixel_clock <= 500:
            raise ValueError("pixel_clock debe estar entre 1 y 500 MHz, o 0")
        if self.retries < 0:
            raise ValueError("retries debe ser >= 0")
        if self.driver != "auto" and self.driver not in DRIVERS:
            raise ValueError(f"driver debe ser auto o uno de {sorted(DRIVERS)}")
        if self.camera_id < 0:
            raise ValueError("camera_id debe ser >= 0")


@dataclass(frozen=True)
class Sample:
    """Medida de un parche, con los canales en orden R, G, B."""

    x: int
    y: int
    n: int
    mean: tuple[float, float, float]
    std: tuple[float, float, float]
    sat_frac: float

    @property
    def intensity(self) -> float:
        return sum(self.mean) / 3.0

    @property
    def sem(self) -> tuple[float, float, float]:
        """Error estandar de la media: la incertidumbre real de la medida."""
        k = self.n ** 0.5
        return tuple(s / k for s in self.std)


def sample_patch(frame_bgr: np.ndarray, x: int, y: int, radius: int,
                 sat_level: int = 255) -> Sample:
    """Promedia un parche cuadrado centrado en (x, y), recortado contra el borde.

    Un pixel suelto es un entero 0..255 y no tiene decimales que ofrecer. El
    promedio sobre (2r+1)^2 pixeles si, y con el se resuelve modulacion por
    debajo de 1 LSB, que es la escala de una señal debil sobre el piso de ruido.
    """
    h, w = frame_bgr.shape[:2]
    if not (0 <= x < w and 0 <= y < h):
        raise ValueError(f"({x},{y}) cae fuera de la imagen {w}x{h}")
    x0, x1 = max(0, x - radius), min(w, x + radius + 1)
    y0, y1 = max(0, y - radius), min(h, y + radius + 1)
    patch = frame_bgr[y0:y1, x0:x1].astype(np.float64)
    if patch.ndim == 2:
        patch = np.repeat(patch[:, :, None], 3, axis=2)
    flat = patch.reshape(-1, 3)
    mean_bgr, std_bgr = flat.mean(axis=0), flat.std(axis=0)
    return Sample(
        x=x, y=y, n=int(patch.shape[0] * patch.shape[1]),
        mean=(mean_bgr[2], mean_bgr[1], mean_bgr[0]),
        std=(std_bgr[2], std_bgr[1], std_bgr[0]),
        sat_frac=float((patch >= sat_level).any(axis=2).mean()),
    )


def canales_recortados(s: Sample) -> list[str]:
    """Canales cuyo parche entero quedo en 0: la señal cae por debajo del ADC.

    Pasa en sensores cuyo piso de oscuridad queda bajo cero con el nivel de negro
    en 0: la C1280R12C recorta R y B (la DCU224C no, su piso es 25/19/13). Ahi una
    señal debil se pierde y restar el dark no la recupera. Se arregla subiendo el
    nivel de negro, que suma una constante, y midiendo el dark con ese mismo valor.
    """
    return [n for n, m, sd in zip("RGB", s.mean, s.std) if m == 0.0 and sd == 0.0]


def format_sample(s: Sample, decimals: int, normalize: bool) -> list[str]:
    k = 1.0 / 255.0 if normalize else 1.0
    unit = "" if normalize else "/255"
    head = f"({s.x},{s.y})  n={s.n}px"
    if s.sat_frac:
        head += f"  SAT {s.sat_frac * 100:.0f}%"
    lines = [head]
    for name, m, sd in zip("RGB", s.mean, s.std):
        lines.append(f"{name} {m * k:>{decimals + 5}.{decimals}f}{unit}  sd {sd * k:.{decimals}f}")
    lines.append(f"I {s.intensity * k:.{decimals}f}{unit}")
    return lines


class Camera(Protocol):
    width: int
    height: int
    exposure_ms: float

    def grab(self) -> np.ndarray: ...
    def set_exposure(self, ms: float) -> float: ...
    def exposure_range(self) -> tuple[float, float]: ...
    def ajustes(self) -> dict: ...
    def aplicar_ajuste(self, clave: str, valor): ...
    def restablecer(self) -> list[str]: ...
    def close(self) -> None: ...


# uc480_64.dll y uEye_api_64.dll no pueden convivir en un proceso: la segunda en
# cargarse falla con WinError 1114 en su DllMain, sea cual sea el orden. Por eso
# hay una sola DLL viva a la vez y se libera antes de cambiar a la otra, lo que
# solo es posible si ninguna camara la esta usando.
_dll_lock = threading.RLock()
_dll_activa: tuple[str, object] | None = None
_camaras_abiertas = 0


def _liberar_dll() -> None:
    global _dll_activa
    if _dll_activa is None:
        return
    k32 = C.WinDLL("kernel32")
    k32.FreeLibrary.argtypes = [C.c_void_p]
    k32.FreeLibrary(_dll_activa[1]._handle)
    _dll_activa = None


def _cargar_dll(driver: str = "uc480"):
    """Carga la DLL de ese driver, liberando la otra si hace falta."""
    global _dll_activa
    if sys.platform != "win32":
        raise UC480Error(
            f"la captura solo funciona en Windows (aca: {sys.platform}). "
            "Los SDK de Thorlabs e IDS no tienen build para macOS. "
            "Usa el modo demo para trabajar sin camara.")
    d = DRIVERS[driver]
    with _dll_lock:
        if _dll_activa is not None and _dll_activa[0] == driver:
            return _dll_activa[1]
        if _dll_activa is not None:
            if _camaras_abiertas:
                raise UC480Error(
                    f"hay una camara abierta con el driver {_dll_activa[0]}; "
                    "cierrala antes de usar el otro driver")
            _liberar_dll()
        nombre = d["dll"] if sys.maxsize > 2 ** 32 else d["dll32"]
        # Primero la del sistema, que coincide con el driver de kernel instalado.
        # La embebida va en una subcarpeta para que esa busqueda no la encuentre
        # antes: PyInstaller agrega _MEIPASS a las rutas de busqueda de DLL.
        try:
            dll = C.WinDLL(nombre)
        except OSError as e:
            copia = _dir_drivers() / d["paquete"] / f"{nombre}.dll"
            if not copia.is_file():
                raise UC480Error(f"falta {nombre}.dll; instala '{d['sdk']}'") from e
            try:
                dll = C.WinDLL(str(copia))
            except OSError as e2:
                raise UC480Error(f"no se pudo cargar {copia.name}: {e2}") from e2
        _dll_activa = (driver, dll)
        return dll


# pnputil: 0 instalado; 259 paquete agregado, sin dispositivo que actualizar (lo
# normal con la camara desconectada); 3010 instalado, falta reiniciar.
PNPUTIL_OK = {0, 259}
PNPUTIL_REINICIO = 3010


def _dir_drivers() -> Path:
    """La carpeta drivers: junto al script, o la embebida en el ejecutable."""
    base = (Path(sys._MEIPASS) if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent)
    return base / "drivers"


def script_instalacion(infs: dict[str, Path], salida: Path) -> str:
    """Script de PowerShell que instala cada .inf y anota el codigo de cada uno."""
    def comillas(ruta: Path) -> str:
        return "'" + str(ruta).replace("'", "''") + "'"
    lineas = ["$r = @()"]
    for driver, inf in infs.items():
        lineas.append(f"& pnputil.exe /add-driver {comillas(inf)} /install | Out-Null")
        lineas.append(f'$r += "{driver}=$LASTEXITCODE"')
    lineas.append(f"$r | Set-Content -Encoding ASCII -LiteralPath {comillas(salida)}")
    return "\n".join(lineas)


def orden_elevada(script: str) -> str:
    """Orden de PowerShell que corre el script como administrador y espera.

    Va codificado en base64 para no depender de como se escapan las comillas y
    los espacios de las rutas al pasar por dos PowerShell seguidos.
    """
    codificado = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ("Start-Process powershell -Verb RunAs -WindowStyle Hidden -Wait "
            f"-ArgumentList '-NoProfile','-EncodedCommand','{codificado}'")


def leer_resultados(texto: str) -> dict[str, int]:
    """Los codigos que anoto script_instalacion, por driver."""
    resultados = {}
    for linea in texto.splitlines():
        driver, _, codigo = linea.strip().partition("=")
        if driver and codigo.lstrip("-").isdigit():
            resultados[driver] = int(codigo)
    return resultados


def resumir_instalacion(resultados: dict[str, int]) -> str:
    """Texto para el usuario. Basta con un driver: ambos sirven para las dos marcas."""
    lineas = []
    for driver, rc in resultados.items():
        estado = ("instalado" if rc in PNPUTIL_OK or rc == PNPUTIL_REINICIO
                  else f"fallo (codigo {rc})")
        lineas.append(f"{DRIVERS[driver]['sdk']}: {estado}")
    if not any(rc in PNPUTIL_OK or rc == PNPUTIL_REINICIO for rc in resultados.values()):
        lineas.append("No se instalo ningun driver.")
    elif PNPUTIL_REINICIO in resultados.values():
        lineas.append("Reinicia Windows antes de usar la camara.")
    else:
        lineas.append("Conecta la camara por USB y pulsa Detectar camara.")
    return "\n".join(lineas)


def instalar_drivers() -> dict[str, int]:
    """Instala los paquetes incluidos con pnputil, pidiendo permiso de administrador."""
    if sys.platform != "win32":
        raise UC480Error("los drivers solo se instalan en Windows")
    infs = {k: _dir_drivers() / d["paquete"] / d["inf"] for k, d in DRIVERS.items()}
    infs = {k: inf for k, inf in infs.items() if inf.is_file()}
    if not infs:
        raise UC480Error(f"no se encontraron los paquetes de driver en {_dir_drivers()}")
    with tempfile.TemporaryDirectory() as carpeta:
        salida = Path(carpeta) / "resultado.txt"
        orden = orden_elevada(script_instalacion(infs, salida))
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", orden],
                           capture_output=True, text=True, timeout=600,
                           stdin=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as e:
            raise UC480Error(f"no se pudo ejecutar el instalador: {e}") from e
        if not salida.is_file():    # sin resultado: se rechazo el aviso de Windows
            raise UC480Error("no se instalo: hace falta aceptar el permiso de administrador")
        return leer_resultados(salida.read_text(encoding="ascii"))


def accion_instalar_drivers(root) -> None:
    """Boton de las dos GUIs. Espera en un hilo para no congelar la ventana."""
    from tkinter import messagebox
    if not messagebox.askyesno(
            "Instalar drivers", "Se instalaran los drivers USB de Thorlabs e IDS. "
            "Windows pedira permiso de administrador.\n\nContinuar?"):
        return

    def fin(ok: bool, texto: str) -> None:
        (messagebox.showinfo if ok else messagebox.showerror)("Instalar drivers", texto)

    def trabajo() -> None:
        try:
            res = resumir_instalacion(instalar_drivers())
            root.after(0, lambda: fin("No se instalo" not in res, res))
        except UC480Error as e:
            msg = str(e)
            root.after(0, lambda: fin(False, msg))

    threading.Thread(target=trabajo, daemon=True).start()


class UC480_CAMERA_INFO(C.Structure):
    """Una entrada de la lista del SDK (uc480.h)."""

    _fields_ = [("dwCameraID", DWORD), ("dwDeviceID", DWORD), ("dwSensorID", DWORD),
                ("dwInUse", DWORD), ("SerNo", C.c_char * 16), ("Model", C.c_char * 16),
                ("dwStatus", DWORD), ("dwReserved", DWORD * 2),
                ("FullModelName", C.c_char * 32), ("dwReserved2", DWORD * 5)]


def _tipo_lista_camaras(n: int):
    """UC480_CAMERA_LIST tiene un array de largo variable al final."""

    class UC480_CAMERA_LIST(C.Structure):
        _fields_ = [("dwCount", DWORD), ("uci", UC480_CAMERA_INFO * n)]

    return UC480_CAMERA_LIST


class BOARDINFO(C.Structure):
    """Identidad grabada en la camara (is_GetCameraInfo). ID trae el fabricante."""

    _fields_ = [("SerNo", C.c_char * 12), ("ID", C.c_char * 20),
                ("Version", C.c_char * 10), ("Date", C.c_char * 12),
                ("Select", C.c_ubyte), ("Type", C.c_ubyte), ("Reserved", C.c_char * 8)]


class IS_RECT(C.Structure):
    _fields_ = [("x", C.c_int32), ("y", C.c_int32), ("w", C.c_int32), ("h", C.c_int32)]


class IO_FLASH_PARAMS(C.Structure):
    _fields_ = [("s32Delay", C.c_int32), ("u32Duration", C.c_uint32)]


class IO_PWM_PARAMS(C.Structure):
    _fields_ = [("dblFrequency_Hz", C.c_double), ("dblDutyCycle", C.c_double)]


class IO_GPIO_CONFIGURATION(C.Structure):
    _fields_ = [("u32Gpio", C.c_uint32), ("u32Caps", C.c_uint32),
                ("u32Configuration", C.c_uint32), ("u32State", C.c_uint32),
                ("u32Reserved", C.c_uint32 * 12)]


MARCA_THORLABS, MARCA_IDS, MARCA_DESCONOCIDA = "Thorlabs", "IDS uEye", "desconocida"


def clasificar_marca(fabricante: str, modelo: str, driver: str) -> str:
    """Decide que tipo de camara es, de la evidencia mas fuerte a la mas debil.

    El fabricante grabado en la camara manda: una uEye enlazada al driver de
    Thorlabs sigue diciendo "IDS GmbH". El modelo solo sirve con los prefijos
    comerciales, porque C1280R12C o D1280G12C son codigos de sensor que no
    distinguen marca. El driver es el ultimo recurso, y solo uEye dice algo: el
    driver uc480 tambien toma camaras IDS, asi que no prueba que sea Thorlabs.
    """
    fab, mod = fabricante.lower(), modelo.upper()
    if "thorlabs" in fab:
        return MARCA_THORLABS
    if "ids" in fab.split() or fab.startswith("ids"):
        return MARCA_IDS
    if mod.startswith(("DCC", "DCU")):
        return MARCA_THORLABS
    if mod.startswith("UI"):
        return MARCA_IDS
    return MARCA_IDS if driver == "ueye" else MARCA_DESCONOCIDA


def _info_camaras(entradas, driver: str = "uc480") -> list[dict]:
    """Traduce las entradas del SDK a dicts, separado para poder testearlo."""
    filas = []
    for c in entradas:
        modelo = (c.FullModelName or c.Model).decode(errors="replace").strip()
        filas.append({"id": int(c.dwCameraID), "device": int(c.dwDeviceID),
                      "modelo": modelo,
                      "serie": c.SerNo.decode(errors="replace").strip(),
                      "en_uso": bool(c.dwInUse), "driver": driver, "fabricante": "",
                      "marca": clasificar_marca("", modelo, driver)})
    return filas


def _leer_fabricante(dll, camara_id: int) -> str:
    """Abre la camara un instante para leer su fabricante; "" si no se puede."""
    h = C.c_uint32(camara_id)
    if dll.is_InitCamera(C.byref(h), None) != IS_SUCCESS:
        return ""
    try:
        b = BOARDINFO()
        if dll.is_GetCameraInfo(h, C.byref(b)) != IS_SUCCESS:
            return ""
        return b.ID.decode(errors="replace").strip()
    finally:
        dll.is_ExitCamera(h)


def _enumerar(driver: str) -> list[dict]:
    dll = _cargar_dll(driver)
    n = C.c_int()
    rc = dll.is_GetNumberOfCameras(C.byref(n))
    if rc != IS_SUCCESS:
        raise UC480Error(f"is_GetNumberOfCameras devolvio rc={rc}")
    if n.value <= 0:
        return []
    lista = _tipo_lista_camaras(n.value)()
    lista.dwCount = n.value
    rc = dll.is_GetCameraList(C.byref(lista))
    if rc != IS_SUCCESS:
        raise UC480Error(f"is_GetCameraList devolvio rc={rc}")
    camaras = _info_camaras(lista.uci[:n.value], driver)
    for c in camaras:
        if not c["en_uso"]:
            c["fabricante"] = _leer_fabricante(dll, c["id"])
            c["marca"] = clasificar_marca(c["fabricante"], c["modelo"], driver)
    return camaras


def detectar_camaras(driver: str = "auto") -> list[dict]:
    """Enumera las camaras conectadas por los dos drivers y dice que tipo es cada una.

    Una camara libre se abre un instante, porque el fabricante solo se puede leer
    con la camara abierta. Devuelve lista vacia si hay SDK pero ninguna camara; si
    no hay ningun SDK o no es Windows, levanta UC480Error con el motivo.

    Con una camara abierta no se puede cargar la otra DLL, asi que solo se enumera
    el driver que esta en uso.
    """
    drivers = tuple(DRIVERS) if driver == "auto" else (driver,)
    with _dll_lock:
        if _camaras_abiertas and _dll_activa is not None:
            drivers = (_dll_activa[0],)
        camaras, errores = [], []
        for d in drivers:
            try:
                camaras += _enumerar(d)
            except UC480Error as e:
                errores.append(str(e))
    if len(errores) == len(drivers):
        raise UC480Error("; ".join(errores))
    return camaras


def elegir_camara(camaras: list[dict], cfg: Config) -> dict | None:
    """La camara que corresponde abrir segun cfg: la pedida o la primera libre."""
    for c in camaras:
        if cfg.driver not in ("auto", c["driver"]) or c["en_uso"]:
            continue
        if cfg.camera_id in (0, c["id"]):
            return c
    return None


def describir_camara(c: dict) -> str:
    """Una linea legible: tipo, modelo, serie y driver."""
    return (f"{c['marca']} - {c['modelo'] or '?'}  s/n {c['serie'] or '?'}  "
            f"(driver {c['driver']})" + ("  [en uso]" if c["en_uso"] else ""))


def interpretar_pnp(lineas: list[str]) -> str:
    """Traduce el estado USB de Windows a una pista, cuando el SDK no ve nada.

    El SDK no distingue "no hay camara" de "la camara fallo al arrancar". Windows
    si: un USB\\VID_0000&PID_0002 es un dispositivo que no respondio al pedido de
    descriptor (codigo 43), que es como queda una camara cuyo arranque de firmware
    fallo. Solo es una pista: puede ser otro aparato.
    """
    texto = "\n".join(lineas).upper()
    if "VID_0000&PID_000" in texto:
        return ("Windows ve un dispositivo USB que no arranco (codigo 43). Si es la "
                "camara, desconectala unos 10 s y probala en otro puerto o con otro "
                "cable: es un problema del enlace, no del programa.")
    if "VID_1409" in texto:
        return ("Windows ve una camara IDS/Thorlabs, pero el SDK no la lista. Si no "
                "tiene driver, pulsa Instalar drivers; si lo tiene, revisa que otro "
                "programa no la tenga tomada.")
    return "Windows no ve ninguna camara en el USB: revisa el cable y el puerto."


def diagnostico_usb() -> str:
    """Le pregunta a Windows que hay en el USB. Vacio si no se puede saber."""
    if sys.platform != "win32":
        return ""
    orden = ("Get-PnpDevice -PresentOnly | Where-Object InstanceId -Match "
             "'VID_1409|VID_0000&PID_000' | ForEach-Object InstanceId")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", orden],
                           capture_output=True, text=True, timeout=10,
                           stdin=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    return interpretar_pnp(r.stdout.splitlines())


# --- ajustes de camara ------------------------------------------------------
# Las mismas pestañas que Camera Settings de ThorCam. La tabla es de datos: la
# ventana se arma leyendola y lo que una camara no soporta sale desactivado, como
# en ThorCam, con la diferencia de que aca se comprueba contra lo que la camara
# declara (ThorCam muestra activa la ganancia master de la C1280R12C, que no hace
# nada: el SDK acepta el valor y la lectura sigue en 0).

@dataclass(frozen=True)
class Ajuste:
    """Un control de la ventana de ajustes, con el rango que admite esta camara."""

    clave: str
    pestana: str
    grupo: str
    etiqueta: str
    tipo: str                     # rango, opciones, bool o accion
    valor: object = None
    minimo: float = 0.0
    maximo: float = 1.0
    paso: float = 1.0
    opciones: tuple = ()
    activo: bool = True
    nota: str = ""


P_CAMARA, P_IMAGEN, P_MEJORA = "Camara", "Imagen", "Mejora de imagen"
P_GEOM, P_OBTURADOR, P_TRIGGER, P_IO = ("Binning/Submuestreo", "Obturador", "Trigger",
                                        "Entrada/Salida")
PESTANAS = (P_CAMARA, P_IMAGEN, P_MEJORA, P_GEOM, P_OBTURADOR, P_TRIGGER, P_IO)

MODOS_TRIGGER = {"libre": 0x0000, "software": 0x1008, "flanco de bajada": 0x1001,
                 "flanco de subida": 0x1002}
MODOS_FLASH = {"apagado": 0, "constante alto": 3, "constante bajo": 4,
               "libre activo alto": 6, "libre activo bajo": 5}
# En el obturador, el bit de capacidad es tambien el valor que se le pasa al SDK.
MODOS_OBTURADOR = {"global": 0x02, "global (tiempo alterno)": 0x80,
                   "rolling": 0x01, "rolling (inicio global)": 0x40}
MODOS_LOG = {"de fabrica (antiblooming)": 0, "apagado (sin antiblooming)": 1, "manual": 2}
MODOS_GPIO = {"entrada": (0x01, 0), "salida baja": (0x02, 0), "salida alta": (0x02, 1),
              "PWM": (0x08, 0), "flash": (0x04, 0)}

# Lo que deja la cadena de imagen lineal. "Restablecer" vuelve aca.
DEFECTOS = {
    "fps_hold": True, "fps_max": False, "exp_hold": True, "exp_max": False,
    "exp_auto": False, "exp_larga": False, "ganancia": 0, "ganancia_auto": False,
    "gain_boost": False, "negro": 0, "negro_auto": False, "awb": False,
    "gamma": 1.0, "gamma_sensor": False, "borde": 0, "sat_u": 100, "sat_v": 100,
    "sat_combinar": True, "bin_h": "1x", "bin_v": "1x", "sub_h": "1x", "sub_v": "1x",
    "espejo_lr": False, "espejo_ud": False, "log_modo": "de fabrica (antiblooming)",
    "log_valor": 0, "log_ganancia": 0, "trigger_modo": "libre",
    "trigger_timeout_ms": 10000, "trigger_delay_on": False, "trigger_delay_us": 0,
    "flash_modo": "apagado", "flash_delay_us": 0, "flash_dur_us": 0,
    "flash_auto": True, "pwm_hz": 0.0, "pwm_duty": 0.0,
    "gpio1": "entrada", "gpio2": "entrada",
}

# Orden al cargar un perfil: cada paso reajusta el rango valido del siguiente.
ORDEN_PERFIL = ("sub_h", "sub_v", "bin_h", "bin_v", "espejo_lr", "espejo_ud",
                "pixel_clock", "fps_hold", "fps_max", "fps", "exp_hold", "exp_max",
                "exp_larga", "exposicion", "exp_auto")
# Los que cambian el tamaño de la imagen: hay que rehacer el buffer.
CLAVES_GEOMETRIA = {"bin_h", "bin_v", "sub_h", "sub_v"}


def opciones_de_mascara(mascara: int, factores: dict[int, int]) -> tuple[str, ...]:
    """Los factores que declara una mascara del SDK, como opciones '1x', '2x'..."""
    if mascara <= 0:
        return ("1x",)
    return ("1x", *(f"{n}x" for n, bit in sorted(factores.items()) if mascara & bit))


def factor_de_opcion(valor) -> int:
    """Pasa '2x' o 2 a 2."""
    return int(str(valor).rstrip("x"))


def avisos_no_lineales(v: dict) -> list[str]:
    """Que ajustes activos rompen la proporcionalidad entre señal e intensidad.

    Malus y Stokes suponen esa proporcionalidad, asi que no se prohibe salir de la
    cadena lineal pero se avisa mientras dure. Las ganancias manuales no aparecen:
    multiplicar por una constante no deja de ser lineal.
    """
    avisos = []
    if abs(float(v.get("gamma", 1.0)) - 1.0) > 1e-9:
        avisos.append(f"gamma {float(v['gamma']):.2f}")
    for clave, texto in (("gamma_sensor", "gamma de sensor"),
                         ("exp_auto", "exposicion automatica"),
                         ("ganancia_auto", "ganancia automatica"),
                         ("awb", "balance de blancos automatico"),
                         ("negro_auto", "nivel de negro automatico"),
                         ("gain_boost", "gain boost")):
        if v.get(clave):
            avisos.append(texto)
    if v.get("borde"):
        avisos.append("realce de bordes")
    if int(v.get("sat_u", 100)) != 100 or int(v.get("sat_v", 100)) != 100:
        avisos.append("saturacion de color")
    if v.get("log_modo", DEFECTOS["log_modo"]) == "manual":
        avisos.append("modo logaritmico")
    return avisos


def construir_ajustes(cap: dict, v: dict) -> dict[str, Ajuste]:
    """Arma la tabla de ajustes a partir de lo que declara la camara y sus valores."""

    filas: list[Ajuste] = []

    def add(clave, pestana, grupo, etiqueta, tipo, **kw):
        filas.append(Ajuste(clave, pestana, grupo, etiqueta, tipo, v.get(clave), **kw))

    def rango(clave, pestana, grupo, etiqueta, limites, soportado=True, nota=""):
        lo, hi, paso = limites if limites else (0, 1, 1)
        add(clave, pestana, grupo, etiqueta, "rango", minimo=lo, maximo=hi, paso=paso,
            activo=bool(soportado and limites and hi > lo), nota=nota)

    def geom(clave, grupo, etiqueta, opciones):
        add(clave, P_GEOM, grupo, etiqueta, "opciones", opciones=opciones,
            activo=len(opciones) > 1)

    # Camara
    rango("pixel_clock", P_CAMARA, "Reloj", "Pixel clock (MHz)", cap.get("reloj"),
          nota="bajarlo reduce los cuadros perdidos")
    rango("fps", P_CAMARA, "Cuadros", "Cuadros por segundo", cap.get("fps"))
    add("fps_hold", P_CAMARA, "Cuadros", "Mantener al cambiar el reloj", "bool")
    add("fps_max", P_CAMARA, "Cuadros", "Maximo", "bool")
    rango("exposicion", P_CAMARA, "Exposicion", "Exposicion (ms)", cap.get("exposicion"),
          nota="la exposicion larga exige bajar antes los cuadros por segundo")
    add("exp_hold", P_CAMARA, "Exposicion", "Mantener al cambiar el reloj", "bool")
    add("exp_max", P_CAMARA, "Exposicion", "Maximo", "bool")
    add("exp_auto", P_CAMARA, "Exposicion", "Automatica", "bool")
    add("exp_larga", P_CAMARA, "Exposicion", "Exposicion larga", "bool",
        activo=bool(cap.get("exp_larga")))
    # Imagen
    rango("ganancia", P_IMAGEN, "Ganancia", "Ganancia master", (0, 100, 1),
          cap.get("master"), nota="" if cap.get("master") else "este sensor no la tiene")
    add("ganancia_auto", P_IMAGEN, "Ganancia", "Automatica", "bool",
        activo=bool(cap.get("master")))
    add("gain_boost", P_IMAGEN, "Ganancia", "Gain boost", "bool", activo=bool(cap.get("boost")))
    rango("negro", P_IMAGEN, "Ganancia", "Nivel de negro (offset)", (0, 255, 1),
          cap.get("negro_offset"))
    add("negro_auto", P_IMAGEN, "Ganancia", "Negro automatico", "bool",
        activo=bool(cap.get("negro_auto")))
    add("awb", P_IMAGEN, "Ganancia de color", "Balance de blancos automatico", "bool",
        activo=bool(cap.get("rgb")))
    for canal, texto in (("r", "Rojo"), ("g", "Verde"), ("b", "Azul")):
        rango(f"ganancia_{canal}", P_IMAGEN, "Ganancia de color", f"Ganancia {texto.lower()}",
              (0, 100, 1), cap.get("rgb"))
    rango("gamma", P_IMAGEN, "Gamma", "Gamma por software", (1.0, 2.2, 0.01))
    add("gamma_sensor", P_IMAGEN, "Gamma", "Gamma de sensor", "bool",
        activo=bool(cap.get("gamma_sensor")))
    # Mejora de imagen
    rango("borde", P_MEJORA, "Realce de bordes", "Realce", cap.get("borde"))
    rango("sat_u", P_MEJORA, "Saturacion de color", "Saturacion U", (0, 200, 1), cap.get("color"))
    add("sat_combinar", P_MEJORA, "Saturacion de color", "Combinar U y V", "bool",
        activo=bool(cap.get("color")))
    rango("sat_v", P_MEJORA, "Saturacion de color", "Saturacion V", (0, 200, 1), cap.get("color"))
    # Binning y submuestreo
    bin_m, sub_m = cap.get("bin_mask", 0), cap.get("sub_mask", 0)
    geom("bin_h", "Binning", "Horizontal", opciones_de_mascara(bin_m, FACTORES_H))
    geom("bin_v", "Binning", "Vertical", opciones_de_mascara(bin_m, FACTORES_V))
    geom("sub_h", "Submuestreo", "Horizontal", opciones_de_mascara(sub_m, FACTORES_H))
    geom("sub_v", "Submuestreo", "Vertical", opciones_de_mascara(sub_m, FACTORES_V))
    add("espejo_lr", P_GEOM, "Espejo", "Izquierda/derecha", "bool")
    add("espejo_ud", P_GEOM, "Espejo", "Arriba/abajo", "bool")
    # Obturador
    obt = tuple(n for n, bit in MODOS_OBTURADOR.items() if cap.get("features", 0) & bit)
    add("obturador", P_OBTURADOR, "Area", "Modo de obturador", "opciones", opciones=obt,
        activo=len(obt) > 1)
    log_on = bool(cap.get("features", 0) & IS_FEATURE_LOG)
    add("log_modo", P_OBTURADOR, "Modo logaritmico", "Modo", "opciones",
        opciones=tuple(MODOS_LOG), activo=log_on)
    rango("log_valor", P_OBTURADOR, "Modo logaritmico", "Valor", cap.get("log_valor"), log_on)
    rango("log_ganancia", P_OBTURADOR, "Modo logaritmico", "Ganancia", cap.get("log_ganancia"),
          log_on)
    # Trigger
    add("trigger_modo", P_TRIGGER, "Modo", "Modo", "opciones", opciones=tuple(MODOS_TRIGGER))
    rango("trigger_timeout_ms", P_TRIGGER, "Modo", "Tiempo de espera (ms)", (100, 3600000, 100))
    add("trigger_delay_on", P_TRIGGER, "Entrada", "Retardo de trigger", "bool",
        activo=bool(cap.get("trigger_delay")))
    rango("trigger_delay_us", P_TRIGGER, "Entrada", "Retardo (us)", cap.get("trigger_delay"))
    add("trigger_estado", P_TRIGGER, "Entrada", "Ver estado de la entrada", "accion")
    # Entrada/Salida
    add("flash_modo", P_IO, "Salida de flash", "Modo", "opciones", opciones=tuple(MODOS_FLASH),
        activo=bool(cap.get("flash")))
    rango("flash_delay_us", P_IO, "Salida de flash", "Retardo (us)", cap.get("flash_delay"))
    rango("flash_dur_us", P_IO, "Salida de flash", "Duracion (us)", cap.get("flash_dur"))
    add("flash_auto", P_IO, "Salida de flash", "Duracion automatica", "bool",
        activo=bool(cap.get("flash")))
    rango("pwm_hz", P_IO, "Modulacion PWM", "Frecuencia (Hz)", cap.get("pwm_hz"))
    rango("pwm_duty", P_IO, "Modulacion PWM", "Ciclo de trabajo", cap.get("pwm_duty"))
    gpios = cap.get("gpios", 0)
    add("gpio1", P_IO, "GPIO", "GPIO 1", "opciones", opciones=tuple(MODOS_GPIO),
        activo=bool(gpios & 1))
    add("gpio2", P_IO, "GPIO", "GPIO 2", "opciones", opciones=tuple(MODOS_GPIO),
        activo=bool(gpios & 2))
    return {a.clave: a for a in filas}


def valor_valido(a: Ajuste, valor):
    """Normaliza lo que llega de la GUI o de un perfil al tipo y rango del ajuste."""
    if a.tipo == "bool":
        return bool(valor)
    if a.tipo == "opciones":
        if valor not in a.opciones:
            raise ValueError(f"{a.etiqueta}: '{valor}' no es una opcion de esta camara")
        return valor
    if a.tipo == "rango":
        x = min(max(float(valor), a.minimo), a.maximo)
        return int(round(x)) if float(a.paso).is_integer() and float(a.minimo).is_integer() else x
    return valor


class AjustesMixin:
    """Tabla de ajustes y aplicacion, comun a la camara real y a la demo.

    ajustes() no llama al SDK: lee lo que quedo guardado la ultima vez. Asi el
    hilo de Tk puede consultarla mientras la captura esta bloqueada en el DLL.
    """

    def ajustes(self) -> dict[str, Ajuste]:
        return construir_ajustes(self.cap, self.valores)

    def aplicar_ajuste(self, clave: str, valor):
        """Aplica un ajuste y devuelve el valor que quedo de verdad en la camara."""
        a = self.ajustes().get(clave)
        if a is None:
            raise ValueError(f"ajuste desconocido: {clave}")
        if not a.activo:
            raise UC480Error(f"esta camara no admite '{a.etiqueta}'")
        if a.tipo == "accion":
            return self._hw(clave, None)
        nuevo = valor_valido(a, valor)
        anterior = self.valores.get(clave)
        self.valores[clave] = nuevo
        try:
            self._hw(clave, nuevo)
        except UC480Error:
            self.valores[clave] = anterior
            raise
        self._acoplar(clave, nuevo)
        self._releer()
        return self.valores.get(clave)

    def _acoplar(self, clave: str, valor) -> None:
        """Efectos entre ajustes, igual que las casillas de ThorCam."""
        if clave in ("sat_u", "sat_v") and self.valores.get("sat_combinar"):
            otro = "sat_v" if clave == "sat_u" else "sat_u"
            self.valores[otro] = valor
            self._hw(otro, valor)
        elif clave == "sat_combinar" and valor:
            self.valores["sat_v"] = self.valores["sat_u"]
            self._hw("sat_v", self.valores["sat_u"])
        elif clave == "pixel_clock":
            self._reaplicar_tiempos()
        elif clave == "fps":
            self._reaplicar_exposicion()
        elif clave == "fps_max" and valor:
            self._reaplicar_tiempos()
        elif clave in ("exp_max", "exp_larga"):
            self._reaplicar_exposicion()

    def restablecer(self) -> list[str]:
        """Vuelve a la cadena lineal. Devuelve lo que no se pudo restablecer."""
        objetivo = {**DEFECTOS, **self.defectos_camara}
        return self.cargar_valores(objetivo)

    def cargar_valores(self, valores: dict) -> list[str]:
        """Aplica un conjunto de ajustes, en el orden que respeta sus dependencias."""
        tabla = self.ajustes()
        claves = [k for k in ORDEN_PERFIL if k in valores]
        claves += [k for k in valores if k not in claves and k in tabla]
        errores = []
        for k in claves:
            a = self.ajustes().get(k)
            # Lo inactivo se salta en silencio: un perfil de otra camara puede
            # traer ajustes que esta no tiene, y no es un error.
            if a is None or not a.activo or a.tipo == "accion" or a.valor == valores[k]:
                continue
            try:
                self.aplicar_ajuste(k, valores[k])
            except (UC480Error, ValueError, TypeError) as e:
                errores.append(f"{k}: {e}")
        return errores

    def _reaplicar_tiempos(self) -> None:
        """Tras cambiar el reloj, respeta Mantener y Maximo de los cuadros."""
        if self.valores.get("fps_max"):
            self.valores["fps"] = self.set_fps(0)
        elif self.valores.get("fps_hold"):
            self.valores["fps"] = self.set_fps(self.valores["fps"])
        self._reaplicar_exposicion()

    def _reaplicar_exposicion(self) -> None:
        self._releer()
        if self.valores.get("exp_max"):
            self.valores["exposicion"] = self.set_exposure(self.exposure_range()[1])
        elif self.valores.get("exp_hold"):
            self.valores["exposicion"] = self.set_exposure(self.valores["exposicion"])


def guardar_perfil(cam, ruta: Path) -> Path:
    """Guarda los ajustes de la camara en JSON, como Save Camera Settings de ThorCam."""
    datos = {"marca": getattr(cam, "marca", ""), "modelo": getattr(cam, "model", ""),
             "ajustes": {k: v for k, v in cam.valores.items()
                         if k in cam.ajustes() and v is not None}}
    ruta.write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")
    return ruta


def leer_perfil(ruta: Path) -> dict:
    """Lee un perfil guardado. ValueError si el archivo no es un perfil."""
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        return dict(datos["ajustes"])
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise ValueError(f"{ruta.name} no es un perfil de ajustes") from e


class ColaAjustes:
    """Pedidos de la GUI, que aplica el hilo de captura entre dos frames.

    Ninguna llamada al DLL puede caer mientras is_FreezeVideo esta bloqueado en el
    otro hilo. Arrastrar un slider genera decenas de pedidos: se conserva solo el
    ultimo de cada clave, porque los intermedios ya no le importan a nadie.
    """

    RESTABLECER, PERFIL = "__restablecer__", "__perfil__"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pedidos: dict[str, object] = {}
        self._hechos: list[tuple[str, object]] = []

    def pedir(self, clave: str, valor=None) -> None:
        with self._lock:
            self._pedidos.pop(clave, None)     # al final, para respetar el orden
            self._pedidos[clave] = valor

    def drenar(self, cam) -> bool:
        """Aplica lo pendiente. Devuelve True si cambio la geometria o el conjunto."""
        with self._lock:
            pedidos, self._pedidos = self._pedidos, {}
        for clave, valor in pedidos.items():
            try:
                if clave == self.RESTABLECER:
                    res = cam.restablecer()
                elif clave == self.PERFIL:
                    res = cam.cargar_valores(valor)
                else:
                    res = cam.aplicar_ajuste(clave, valor)
            except (UC480Error, ValueError, TypeError) as e:
                res = e
            with self._lock:
                self._hechos.append((clave, res))
        return bool(pedidos)

    def hechos(self) -> list[tuple[str, object]]:
        """Lo aplicado desde la ultima consulta, para que lo muestre el hilo de Tk."""
        with self._lock:
            hechos, self._hechos = self._hechos, []
        return hechos


class UC480Camera(AjustesMixin):
    """Acceso por ctypes a la API uc480/uEye, por el driver de Thorlabs."""

    DRIVER = "uc480"

    def __init__(self, cfg: Config, info: dict | None = None):
        global _camaras_abiertas
        self.cfg = cfg
        self.transfer_errors = 0
        self.driver = self.DRIVER
        self._cancelar = threading.Event()
        self.dll = _cargar_dll(self.driver)

        self.h = C.c_uint32((info or {}).get("id", cfg.camera_id))
        with _dll_lock:
            rc = self.dll.is_InitCamera(C.byref(self.h), None)
            if rc == IS_SUCCESS:
                _camaras_abiertas += 1
        if rc != IS_SUCCESS:
            self.h = C.c_uint32(0)
            raise UC480Error(f"is_InitCamera devolvio rc={rc}; si ThorCam o uEye Cockpit "
                             "estan abiertos, cierralos")
        try:
            self._configurar(cfg)
        except UC480Error:
            self.close()
            raise

    def _configurar(self, cfg: Config) -> None:
        info = self._sensor_info()
        self.width = int(info.nMaxWidth)
        self.full_height = int(info.nMaxHeight)
        self.model = info.strSensorName.decode(errors="replace").strip()
        self.pixel_um = info.wPixelSize / 100.0
        self.serie, self.fabricante = self._identidad()
        self.marca = clasificar_marca(self.fabricante, self.model, self.driver)

        if cfg.raw:
            mode, bpp = IS_CM_SENSOR_RAW8, 8
        elif cfg.mono:
            mode, bpp = IS_CM_MONO8, 8
        else:
            mode, bpp = IS_CM_BGR8_PACKED, 24
        self.channels = 1 if bpp == 8 else 3
        self._check(self.dll.is_SetColorMode(self.h, mode), "is_SetColorMode")
        self._check(self.dll.is_SetDisplayMode(self.h, IS_SET_DM_DIB), "is_SetDisplayMode")
        self._set_manual()
        self.bpp = bpp

        # El binning suma carga dentro del CCD, antes del ADC, y reduce el alto.
        # Hay que fijarlo antes de reservar memoria o el buffer queda mal medido.
        self.cap = self._leer_capacidades(info)
        if cfg.binning > 1 and not self.cap["bin_mask"] & FACTORES_V.get(cfg.binning, 0):
            raise UC480Error(f"esta camara no admite binning vertical {cfg.binning}x")
        self._check(self.dll.is_SetBinning(self.h, BINNING_MODES[cfg.binning]),
                    "is_SetBinning")
        self.mem, self.mem_id = C.c_char_p(), C.c_int()
        self._reservar_buffer()

        # El orden importa: cada paso reajusta el rango valido del siguiente.
        if cfg.pixel_clock:
            self.set_pixel_clock(cfg.pixel_clock)
        self.pixel_clock = self.get_pixel_clock()
        self.fps = self.set_fps(cfg.fps)
        self.exposure_ms = self.set_exposure(cfg.exposure_ms)
        self.dll.is_SetTimeout(self.h, IS_TRIGGER_TIMEOUT, 3000)
        self._iniciar_valores()

    def _check(self, rc: int, what: str) -> None:
        if rc == IS_NOT_SUPPORTED:
            raise UC480Error(f"esta camara no soporta {what} (rc={rc})")
        if rc != IS_SUCCESS:
            raise UC480Error(f"{what} devolvio rc={rc}")

    def _sensor_info(self):
        class SENSORINFO(C.Structure):
            _fields_ = [("SensorID", WORD), ("strSensorName", C.c_char * 32),
                        ("nColorMode", C.c_char), ("nMaxWidth", DWORD),
                        ("nMaxHeight", DWORD), ("bMasterGain", BOOL),
                        ("bRGain", BOOL), ("bGGain", BOOL), ("bBGain", BOOL),
                        ("bGlobShutter", BOOL), ("wPixelSize", WORD),
                        ("Reserved", C.c_char * 14)]

        si = SENSORINFO()
        self._check(self.dll.is_GetSensorInfo(self.h, C.byref(si)), "is_GetSensorInfo")
        return si

    def _identidad(self) -> tuple[str, str]:
        b = BOARDINFO()
        if self.dll.is_GetCameraInfo(self.h, C.byref(b)) != IS_SUCCESS:
            return "", ""
        return (b.SerNo.decode(errors="replace").strip(),
                b.ID.decode(errors="replace").strip())

    def _reservar_buffer(self) -> None:
        """Mide el AOI actual y reserva el buffer para ese tamaño."""
        r = IS_RECT()
        self._check(self.dll.is_AOI(self.h, IS_AOI_IMAGE_GET_AOI, C.byref(r), C.sizeof(r)),
                    "is_AOI")
        if self.mem:
            self.dll.is_FreeImageMem(self.h, self.mem, self.mem_id)
            self.mem, self.mem_id = C.c_char_p(), C.c_int()
        self.width, self.height = int(r.w), int(r.h)
        self._check(self.dll.is_AllocImageMem(self.h, self.width, self.height, self.bpp,
                                              C.byref(self.mem), C.byref(self.mem_id)),
                    "is_AllocImageMem")
        self._check(self.dll.is_SetImageMem(self.h, self.mem, self.mem_id), "is_SetImageMem")
        pitch = C.c_int()
        self._check(self.dll.is_GetImageMemPitch(self.h, C.byref(pitch)), "is_GetImageMemPitch")
        self.pitch = int(pitch.value)

    def _leer_capacidades(self, si) -> dict:
        """Pregunta que admite esta camara, sin suponer nada por el modelo."""
        d, h = self.dll, self.h
        bl = d.is_SetBlCompensation(h, IS_GET_BL_SUPPORTED_MODE, 0, 0)
        features, gpios, exp_caps = C.c_uint(0), C.c_uint(0), C.c_uint(0)
        d.is_DeviceFeature(h, IS_FEATURE_GET, C.byref(features), 4)
        d.is_IO(h, IS_IO_GPIOS_SOPORTADOS, C.byref(gpios), 4)
        d.is_Exposure(h, IS_EXPOSURE_CMD_CAPS, C.byref(exp_caps), 4)
        fmin, fmax = IO_FLASH_PARAMS(), IO_FLASH_PARAMS()
        flash = (d.is_IO(h, IS_IO_FLASH_MIN, C.byref(fmin), C.sizeof(fmin)) == IS_SUCCESS
                 and d.is_IO(h, IS_IO_FLASH_MAX, C.byref(fmax), C.sizeof(fmax)) == IS_SUCCESS)
        d_min = d.is_SetTriggerDelay(h, IS_GET_MIN_TRIGGER_DELAY)
        d_max = d.is_SetTriggerDelay(h, IS_GET_MAX_TRIGGER_DELAY)
        cap = {
            "master": bool(si.bMasterGain),
            "rgb": bool(si.bRGain or si.bGGain or si.bBGain),
            "color": si.nColorMode != b"\x01",        # 1 = sensor monocromo
            "boost": d.is_SetGainBoost(h, IS_GET_SUPPORTED_GAINBOOST) == IS_SET_GAINBOOST_ON,
            "negro_auto": bool(bl & IS_BL_COMPENSATION_ENABLE) if bl > 0 else False,
            "negro_offset": bool(bl & IS_BL_COMPENSATION_OFFSET) if bl > 0 else False,
            "gamma_sensor": d.is_SetHardwareGamma(h, IS_GET_HW_SUPPORTED_GAMMA) == IS_SET_HW_GAMMA_ON,
            "bin_mask": max(d.is_SetBinning(h, IS_GET_SUPPORTED), 0),
            "sub_mask": max(d.is_SetSubSampling(h, IS_GET_SUPPORTED), 0),
            "features": features.value,
            "gpios": gpios.value,
            "exp_larga": bool(exp_caps.value & IS_EXPOSURE_CAP_LARGA),
            "flash": flash,
            "flash_delay": (fmin.s32Delay, fmax.s32Delay, 1) if flash else None,
            "flash_dur": (fmin.u32Duration, fmax.u32Duration, 1) if flash else None,
            "trigger_delay": (d_min, d_max, 1) if 0 <= d_min < d_max else None,
            "borde": self._rango_uint(d.is_EdgeEnhancement, IS_EDGE_RANGO),
            "reloj": self._rango_uint(d.is_PixelClock, IS_PIXELCLOCK_CMD_RANGO),
            "log_valor": self._rango_uint(d.is_DeviceFeature, IS_LOG_VALOR_RANGO),
            "log_ganancia": self._rango_uint(d.is_DeviceFeature, IS_LOG_GAIN_RANGO),
        }
        pwm_gpios, pmin, pmax = C.c_uint(0), IO_PWM_PARAMS(), IO_PWM_PARAMS()
        if (d.is_IO(h, IS_IO_PWM_GPIOS, C.byref(pwm_gpios), 4) == IS_SUCCESS and pwm_gpios.value
                and d.is_IO(h, IS_IO_PWM_MIN, C.byref(pmin), C.sizeof(pmin)) == IS_SUCCESS
                and d.is_IO(h, IS_IO_PWM_MAX, C.byref(pmax), C.sizeof(pmax)) == IS_SUCCESS):
            cap["pwm_hz"] = (pmin.dblFrequency_Hz, pmax.dblFrequency_Hz, 0.01)
            cap["pwm_duty"] = (pmin.dblDutyCycle, pmax.dblDutyCycle, 0.01)
        return cap

    def _rango_uint(self, funcion, comando: int) -> tuple[int, int, int] | None:
        """Lee un (minimo, maximo, paso) de los comandos que lo devuelven en 3 UINT."""
        r = (C.c_uint * 3)()
        if funcion(self.h, comando, r, C.sizeof(r)) != IS_SUCCESS or r[1] <= r[0]:
            return None
        return int(r[0]), int(r[1]), max(int(r[2]), 1)

    def _leer_ganancia(self, comando: int) -> int:
        return self.dll.is_SetHardwareGain(self.h, comando, IS_IGNORE_PARAMETER,
                                           IS_IGNORE_PARAMETER, IS_IGNORE_PARAMETER)

    def _iniciar_valores(self) -> None:
        """Foto inicial de los ajustes: lo que quedo en la camara despues de abrirla."""
        r, g, b = (self._leer_ganancia(c) for c in IS_GET_DEFAULT_RGB)
        # La ganancia por canal se deja en la que trae el driver: es una constante
        # por canal y no rompe la linealidad. Restablecer vuelve a ese valor.
        self.defectos_camara = {"ganancia_r": r, "ganancia_g": g, "ganancia_b": b}
        ar, ag, ab = (self._leer_ganancia(c) for c in IS_GET_RGB)
        self.valores = {
            **DEFECTOS, "ganancia_r": ar, "ganancia_g": ag, "ganancia_b": ab,
            "pixel_clock": self.pixel_clock, "fps": self.fps,
            "exposicion": self.exposure_ms, "ganancia": self.cfg.gain,
            "bin_v": f"{self.cfg.binning}x", "obturador": None, "trigger_delay_us": (self.cap["trigger_delay"] or (0,))[0],
            "flash_delay_us": (self.cap["flash_delay"] or (0,))[0],
            "flash_dur_us": (self.cap["flash_dur"] or (0,))[0],
        }
        self._releer()

    def _releer(self) -> None:
        """Actualiza los rangos que dependen de otros ajustes y lo que quedo real."""
        tmin, tmax, paso = C.c_double(), C.c_double(), C.c_double()
        if self.dll.is_GetFrameTimeRange(self.h, C.byref(tmin), C.byref(tmax),
                                         C.byref(paso)) == IS_SUCCESS and tmin.value > 0:
            self.cap["fps"] = (1.0 / tmax.value, 1.0 / tmin.value, 0.01)
        lo, hi = self.exposure_range()
        self.cap["exposicion"] = (lo, hi, max(self._exp_incremento(), 0.001))
        self.valores["pixel_clock"] = self.pixel_clock = self.get_pixel_clock()
        self.valores["fps"] = self.fps
        got = C.c_double()
        self.dll.is_Exposure(self.h, IS_EXPOSURE_CMD_GET, C.byref(got), 8)
        self.exposure_ms = self.valores["exposicion"] = got.value

    def _exp_incremento(self) -> float:
        inc = C.c_double()
        self.dll.is_Exposure(self.h, IS_EXPOSURE_CMD_INC, C.byref(inc), 8)
        return inc.value

    # --- cada ajuste, contra el SDK ---

    def _hw(self, clave: str, v):
        d, h = self.dll, self.h
        if clave in CLAVES_GEOMETRIA:
            return self._aplicar_geometria()
        if clave == "pixel_clock":
            return self.set_pixel_clock(int(v))
        if clave == "fps":
            self.set_fps(float(v))
        elif clave == "exposicion":
            self.set_exposure(float(v))
        elif clave == "exp_auto":
            self._auto(IS_SET_ENABLE_AUTO_SHUTTER, v)
        elif clave == "exp_larga":
            e = C.c_uint(int(v))
            self._check(d.is_Exposure(h, IS_EXPOSURE_CMD_LARGA_SET, C.byref(e), 4), "exposicion larga")
        elif clave == "ganancia":
            self._check(d.is_SetHardwareGain(h, int(v), IS_IGNORE_PARAMETER, IS_IGNORE_PARAMETER,
                                             IS_IGNORE_PARAMETER), "ganancia")
        elif clave == "ganancia_auto":
            self._auto(IS_SET_ENABLE_AUTO_GAIN, v)
        elif clave == "gain_boost":
            self._check(d.is_SetGainBoost(h, IS_SET_GAINBOOST_ON if v else IS_SET_GAINBOOST_OFF),
                        "gain boost")
        elif clave in ("negro", "negro_auto"):
            modo = (IS_BL_COMPENSATION_ENABLE if self.valores.get("negro_auto")
                    else IS_BL_COMPENSATION_DISABLE)
            offset = int(self.valores.get("negro") or 0)
            self._check(d.is_SetBlCompensation(h, modo, offset, 0), "nivel de negro")
        elif clave == "awb":
            self._auto(IS_SET_ENABLE_AUTO_WB, v)
        elif clave in ("ganancia_r", "ganancia_g", "ganancia_b"):
            canales = [IS_IGNORE_PARAMETER] * 3
            canales["rgb".index(clave[-1])] = int(v)
            self._check(d.is_SetHardwareGain(h, IS_IGNORE_PARAMETER, *canales), "ganancia de color")
        elif clave == "gamma":
            self._check(d.is_SetGamma(h, int(round(float(v) * 100))), "gamma")
        elif clave == "gamma_sensor":
            self._check(d.is_SetHardwareGamma(h, IS_SET_HW_GAMMA_ON if v else IS_SET_GAMMA_OFF),
                        "gamma de sensor")
        elif clave == "borde":
            e = C.c_uint(int(v))
            self._check(d.is_EdgeEnhancement(h, IS_EDGE_SET, C.byref(e), 4), "realce de bordes")
        elif clave in ("sat_u", "sat_v"):
            self._check(d.is_SetSaturation(h, int(self.valores["sat_u"]),
                                           int(self.valores["sat_v"])), "saturacion")
        elif clave == "espejo_lr":
            self._check(d.is_SetRopEffect(h, IS_SET_ROP_MIRROR_LEFTRIGHT, int(v), 0), "espejo")
        elif clave == "espejo_ud":
            self._check(d.is_SetRopEffect(h, IS_SET_ROP_MIRROR_UPDOWN, int(v), 0), "espejo")
        elif clave == "obturador":
            m = C.c_uint(MODOS_OBTURADOR[v])
            self._check(d.is_DeviceFeature(h, IS_SHUTTER_SET, C.byref(m), 4), "obturador")
        elif clave == "log_modo":
            m = C.c_uint(MODOS_LOG[v])
            self._check(d.is_DeviceFeature(h, IS_LOG_MODO_SET, C.byref(m), 4), "modo log")
        elif clave in ("log_valor", "log_ganancia"):
            comando = IS_LOG_VALOR_SET if clave == "log_valor" else IS_LOG_GAIN_SET
            m = C.c_uint(int(v))
            self._check(d.is_DeviceFeature(h, comando, C.byref(m), 4), "modo log")
        elif clave == "trigger_modo":
            self._check(d.is_SetExternalTrigger(h, MODOS_TRIGGER[v]), "trigger")
        elif clave in ("trigger_delay_on", "trigger_delay_us"):
            us = int(self.valores["trigger_delay_us"]) if self.valores["trigger_delay_on"] else 0
            self._check(d.is_SetTriggerDelay(h, us), "retardo de trigger")
        elif clave == "trigger_estado":
            return d.is_SetExternalTrigger(h, IS_GET_TRIGGER_STATUS)
        elif clave == "flash_modo":
            m = C.c_uint(MODOS_FLASH[v])
            self._check(d.is_IO(h, IS_IO_FLASH_SET_MODO, C.byref(m), 4), "flash")
        elif clave in ("flash_delay_us", "flash_dur_us", "flash_auto"):
            # Duracion 0 es como el SDK pide que el flash dure lo que la exposicion.
            dur = 0 if self.valores["flash_auto"] else int(self.valores["flash_dur_us"])
            p = IO_FLASH_PARAMS(int(self.valores["flash_delay_us"]), dur)
            self._check(d.is_IO(h, IS_IO_FLASH_SET_PARAMS, C.byref(p), C.sizeof(p)), "flash")
        elif clave in ("pwm_hz", "pwm_duty"):
            p = IO_PWM_PARAMS(float(self.valores["pwm_hz"]), float(self.valores["pwm_duty"]))
            self._check(d.is_IO(h, IS_IO_PWM_SET, C.byref(p), C.sizeof(p)), "PWM")
        elif clave in ("gpio1", "gpio2"):
            conf, estado = MODOS_GPIO[v]
            g = IO_GPIO_CONFIGURATION()
            g.u32Gpio, g.u32Configuration, g.u32State = (1 if clave == "gpio1" else 2), conf, estado
            self._check(d.is_IO(h, IS_IO_GPIO_SET, C.byref(g), C.sizeof(g)), "GPIO")
        # fps_hold, fps_max, exp_hold, exp_max, sat_combinar y trigger_timeout_ms no
        # le dicen nada a la camara: los usa el programa.
        return v

    def _auto(self, param: int, activo) -> None:
        on, aux = C.c_double(1.0 if activo else 0.0), C.c_double(0.0)
        self._check(self.dll.is_SetAutoParameter(self.h, param, C.byref(on), C.byref(aux)),
                    "modo automatico")

    def _aplicar_geometria(self) -> None:
        """Binning y submuestreo: cambian el tamaño, asi que se rehace el buffer."""
        v = self.valores
        def mascara(clave, factores):
            return factores.get(factor_de_opcion(v[clave]), 0)
        binning = mascara("bin_h", FACTORES_H) | mascara("bin_v", FACTORES_V)
        submuestreo = mascara("sub_h", FACTORES_H) | mascara("sub_v", FACTORES_V)
        # Se limpia uno antes de fijar el otro: el SDK rechaza combinaciones que
        # dejarian los dos activos a medio camino.
        self._check(self.dll.is_SetSubSampling(self.h, 0), "submuestreo")
        self._check(self.dll.is_SetBinning(self.h, binning), "binning")
        self._check(self.dll.is_SetSubSampling(self.h, submuestreo), "submuestreo")
        self._reservar_buffer()
        self.cfg = replace(self.cfg, binning=factor_de_opcion(v["bin_v"]))
        # El cuadro maximo depende de cuantas filas se leen.
        self.set_fps(v["fps"])
        self.set_exposure(v["exposicion"])

    def cancelar(self) -> None:
        """Corta una espera de trigger. Se puede llamar desde cualquier hilo."""
        self._cancelar.set()

    def reanudar(self) -> None:
        self._cancelar.clear()

    def _set_manual(self) -> None:
        """Deja la cadena de imagen lineal y bajo control manual."""
        for param in AUTO_PARAMS:
            off, aux = C.c_double(0.0), C.c_double(0.0)
            self.dll.is_SetAutoParameter(self.h, param, C.byref(off), C.byref(aux))
        self.dll.is_SetGamma(self.h, 100)  # en centesimas: 100 = 1.00
        self.dll.is_SetHardwareGamma(self.h, IS_SET_GAMMA_OFF)
        self.dll.is_SetColorCorrection(self.h, IS_CCOR_DISABLE, C.byref(C.c_double(0.0)))
        self.dll.is_SetGainBoost(self.h, IS_SET_GAINBOOST_OFF)
        self.dll.is_SetBlCompensation(self.h, IS_BL_COMPENSATION_DISABLE, 0, 0)
        self.dll.is_SetHardwareGain(self.h, self.cfg.gain, IS_IGNORE_PARAMETER,
                                    IS_IGNORE_PARAMETER, IS_IGNORE_PARAMETER)

    def get_pixel_clock(self) -> int:
        clk = C.c_uint()
        self.dll.is_PixelClock(self.h, IS_PIXELCLOCK_CMD_GET, C.byref(clk), 4)
        return int(clk.value)

    def set_pixel_clock(self, mhz: int) -> int:
        self._check(self.dll.is_PixelClock(self.h, IS_PIXELCLOCK_CMD_SET,
                                           C.byref(C.c_uint(mhz)), 4), "is_PixelClock")
        self.pixel_clock = self.get_pixel_clock()
        return self.pixel_clock

    def set_fps(self, fps: float) -> float:
        got = C.c_double()
        self.dll.is_SetFrameRate(self.h, C.c_double(fps or 1000.0), C.byref(got))
        self.fps = got.value
        return self.fps

    def _grab_con_trigger(self) -> None:
        """Espera un pulso externo sin dejar el hilo clavado en el DLL.

        Con flanco externo is_FreezeVideo no vuelve hasta que llegue el pulso o
        venza su espera. Se le pide de a un segundo, para que Detener y cerrar la
        ventana respondan aunque el tiempo de espera configurado sea de una hora.
        """
        limite = time.monotonic() + float(self.valores["trigger_timeout_ms"]) / 1000.0
        while True:
            if self._cancelar.is_set():
                raise CapturaCancelada("captura cancelada")
            resta = limite - time.monotonic()
            if resta <= 0:
                raise TriggerTimeout("no llego ningun pulso de trigger")
            # is_FreezeVideo toma la espera en unidades de 10 ms.
            rc = self.dll.is_FreezeVideo(self.h, max(1, min(100, int(resta * 100))))
            if rc == IS_SUCCESS:
                return
            if rc != IS_TIMED_OUT:
                self._check(rc, "is_FreezeVideo")

    def exposure_range(self) -> tuple[float, float]:
        lo, hi = C.c_double(), C.c_double()
        self.dll.is_Exposure(self.h, IS_EXPOSURE_CMD_RANGE_MIN, C.byref(lo), 8)
        self.dll.is_Exposure(self.h, IS_EXPOSURE_CMD_RANGE_MAX, C.byref(hi), 8)
        return lo.value, hi.value

    def set_exposure(self, ms: float) -> float:
        lo, hi = self.exposure_range()
        val = C.c_double(min(max(ms, lo), hi))
        self._check(self.dll.is_Exposure(self.h, IS_EXPOSURE_CMD_SET, C.byref(val), 8),
                    "is_Exposure")
        got = C.c_double()
        self.dll.is_Exposure(self.h, IS_EXPOSURE_CMD_GET, C.byref(got), 8)
        self.exposure_ms = got.value
        return self.exposure_ms

    def grab(self) -> np.ndarray:
        """Captura un frame completo.

        is_FreezeVideo bloquea hasta tener la imagen entera, asi que no hay
        riesgo de leer un buffer a medio escribir. Sobre USB 2.0 falla de forma
        intermitente con IS_TRANSFER_ERROR; de ahi los reintentos.
        """
        if self.valores["trigger_modo"].startswith("flanco"):
            self._grab_con_trigger()
        else:
            rc = IS_TRANSFER_ERROR
            for _ in range(self.cfg.retries + 1):
                rc = self.dll.is_FreezeVideo(self.h, IS_WAIT)
                if rc == IS_SUCCESS:
                    break
                self.transfer_errors += 1
            self._check(rc, "is_FreezeVideo")

        raw = C.string_at(self.mem, self.pitch * self.height)
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(self.height, self.pitch)
        arr = arr[:, : self.width * self.channels]
        arr = arr.reshape(self.height, self.width, self.channels)
        return arr[:, :, 0] if self.channels == 1 else arr

    def close(self) -> None:
        global _camaras_abiertas
        if getattr(self, "h", None) and self.h.value:
            if getattr(self, "mem", None):
                self.dll.is_FreeImageMem(self.h, self.mem, self.mem_id)
            self.dll.is_ExitCamera(self.h)
            self.h = C.c_uint32(0)
            with _dll_lock:
                _camaras_abiertas -= 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class UEyeCamera(UC480Camera):
    """La misma API por el driver de IDS, para una uEye enlazada a uEye_api_64.dll."""

    DRIVER = "ueye"


CLASES_CAMARA = {"uc480": UC480Camera, "ueye": UEyeCamera}


class DemoCamera(AjustesMixin):
    """Patron sintetico con franjas cos^2, para trabajar en la UI sin camara."""

    model, pixel_clock, fps = "DEMO", 0, 30.0
    marca, driver, serie, fabricante = "DEMO", "demo", "", ""

    def __init__(self, cfg: Config):
        self.cfg, self.exposure_ms, self._t = cfg, cfg.exposure_ms, 0
        self.width, self.full_height = 640, 480
        self.full_width = self.width
        self.height = self.full_height // cfg.binning
        self.transfer_errors = 0
        # Declara todo como soportado: asi la ventana de ajustes se puede probar
        # entera sin hardware.
        self.cap = {
            "master": True, "rgb": True, "color": True, "boost": True, "negro_auto": True,
            "negro_offset": True, "gamma_sensor": True, "exp_larga": True,
            "bin_mask": 0x3f, "sub_mask": 0x0f, "features": 0xE3, "gpios": 3,
            "flash": True, "flash_delay": (0, 100000, 1), "flash_dur": (0, 100000, 1),
            "trigger_delay": (15, 4000000, 1), "borde": (0, 9, 1), "reloj": (5, 43, 1),
            "log_valor": (0, 12, 1), "log_ganancia": (0, 4, 1), "fps": (0.5, 30.0, 0.01),
            "exposicion": (*self.exposure_range(), 0.01), "pwm_hz": (1.0, 10000.0, 0.01),
            "pwm_duty": (0.0, 1.0, 0.01),
        }
        self.defectos_camara = {"ganancia_r": 0, "ganancia_g": 0, "ganancia_b": 0}
        self.valores = {**DEFECTOS, **self.defectos_camara, "pixel_clock": 24,
                        "fps": self.fps, "exposicion": self.exposure_ms,
                        "ganancia": cfg.gain, "bin_v": f"{cfg.binning}x",
                        "obturador": "global", "trigger_delay_us": 15}

    def grab(self) -> np.ndarray:
        self._t += 1
        _, xx = np.mgrid[0:self.height, 0:self.width]
        base = 120 * np.cos(np.deg2rad(self._t * 2) + xx / 180.0) ** 2 + 20
        img = np.stack([base * 0.9, base, base * 0.75], axis=2)
        ruido = np.random.default_rng(self._t).normal(0, 1.5, img.shape)
        ganancia = 1.0 + float(self.valores["ganancia"]) / 100.0 * 3.0
        img = (img * (self.exposure_ms / 20.0) * factor_de_opcion(self.valores["bin_v"])
               * ganancia + ruido)
        gamma = float(self.valores["gamma"])
        if gamma != 1.0:
            img = 255.0 * (np.clip(img, 0, 255) / 255.0) ** (1.0 / gamma)
        if self.valores["espejo_lr"]:
            img = img[:, ::-1]
        if self.valores["espejo_ud"]:
            img = img[::-1]
        return np.clip(img, 0, 255).astype(np.uint8)

    def _hw(self, clave: str, v):
        if clave in CLAVES_GEOMETRIA:
            bin_h, bin_v, sub_h, sub_v = (factor_de_opcion(self.valores[k])
                                          for k in ("bin_h", "bin_v", "sub_h", "sub_v"))
            self.width = self.full_width // (bin_h * sub_h)
            self.height = self.full_height // (bin_v * sub_v)
        elif clave == "fps":
            self.fps = float(v)
        elif clave == "exposicion":
            self.set_exposure(float(v))
        elif clave == "trigger_estado":
            return 0
        return v

    def _releer(self) -> None:
        self.valores["exposicion"] = self.exposure_ms
        self.valores["fps"] = self.fps

    def set_fps(self, fps: float) -> float:
        self.fps = fps or self.cap["fps"][1]
        return self.fps

    def cancelar(self) -> None: ...

    def reanudar(self) -> None: ...

    def set_exposure(self, ms: float) -> float:
        lo, hi = self.exposure_range()
        self.exposure_ms = min(max(ms, lo), hi)
        return self.exposure_ms

    def exposure_range(self) -> tuple[float, float]:
        return 0.01, 500.0

    def close(self) -> None: ...

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def open_camera(cfg: Config) -> Camera:
    """Detecta que hay conectado y abre la camara con el driver que le corresponde."""
    if cfg.demo:
        return DemoCamera(cfg)
    camaras = detectar_camaras(cfg.driver)
    elegida = elegir_camara(camaras, cfg)
    if elegida is None:
        if camaras:
            raise UC480Error("todas las camaras estan ocupadas; cierra ThorCam o uEye "
                             "Cockpit:\n" + "\n".join(describir_camara(c) for c in camaras))
        pista = diagnostico_usb()
        raise UC480Error("ninguna camara Thorlabs ni uEye responde." + (f"\n\n{pista}" if pista else ""))
    return CLASES_CAMARA[elegida["driver"]](cfg, elegida)


def ficha_camara(cam) -> str:
    """El texto de la ficha de camara: que tipo es, antes que nada."""
    if getattr(cam, "driver", "") == "demo":
        return "Tipo: DEMO (sin camara)"
    return (f"Tipo: {cam.marca}\n"
            f"Modelo: {cam.model}  s/n {cam.serie or '?'}\n"
            f"Driver: {DRIVERS[cam.driver]['dll']}")


# --- dark frame -----------------------------------------------------------

def capture_dark(cam: Camera, n: int = 32, progress=None) -> np.ndarray:
    """Promedia n frames a oscuras. Se captura con la tapa puesta.

    El sensor tiene un piso distinto en cada canal (del orden de 25/19/13 en la
    DCU224C). Ese offset no se va promediando: es sistematico y hay que
    restarlo, o aplasta el contraste cerca de la extincion.
    """
    acc, got = None, 0
    for i in range(n):
        try:
            f = cam.grab().astype(np.float64)
        except UC480Error:
            continue
        acc = f if acc is None else acc + f
        got += 1
        if progress:
            progress(i + 1, n)
    if not got:
        raise UC480Error("no se pudo capturar ningun frame para el dark")
    return acc / got


def apply_dark(frame: np.ndarray, dark: np.ndarray | None) -> np.ndarray:
    """Resta el dark y recorta en cero. Si no coincide la forma, no hace nada."""
    if dark is None or dark.shape != frame.shape:
        return frame
    return np.clip(frame.astype(np.float64) - dark, 0, None)


def load_dark(path: Path) -> np.ndarray | None:
    try:
        return np.load(path)
    except (OSError, ValueError):
        return None


# --- captura de frame completo ---------------------------------------------

# Formatos que guardan el valor por pixel como decimal: los decimales salen de
# promediar (binning, ROI, frames), no del ADC, y son lo que hace cuantitativa
# a la medida.
FORMATOS_FLOAT = {
    ".npy": "NumPy float64 (exacto)",
    ".tiff": "TIFF 32 bits flotante",
    ".tif": "TIFF 32 bits flotante",
}

# Formatos de imagen comun. Redondean a entero de 0..255 y pierden los
# decimales; sirven para mirar, mostrar o pegar en un informe, no para medir.
FORMATOS_ENTEROS = {
    ".png": "PNG 8 bits (sin perdida, pero redondea)",
    ".jpg": "JPEG 8 bits (comprime con perdida)",
    ".jpeg": "JPEG 8 bits (comprime con perdida)",
}

FORMATOS = {**FORMATOS_FLOAT, **FORMATOS_ENTEROS}
FORMATO_POR_DEFECTO = ".npy"


def save_frame_as(frame: np.ndarray, path: Path) -> Path:
    """Guarda el frame en el formato que pida la extension.

    Los de FORMATOS_FLOAT conservan el valor por pixel con decimales: el .npy
    los float64 exactos y el TIFF de 32 bits el mismo dato en algo que abren
    ImageJ/Fiji. Los de FORMATOS_ENTEROS recortan a 0..255 y redondean, que es
    lo que uno quiere para mirar la imagen, no para medir sobre ella.
    """
    suf = path.suffix.lower()
    if suf not in FORMATOS:
        raise ValueError(f"'{suf or 'sin extension'}' no es un formato conocido; "
                         f"usa {', '.join(sorted(FORMATOS))}")
    arr = np.asarray(frame, dtype=np.float64)
    if suf == ".npy":
        np.save(path, arr)
        return path

    import cv2
    datos = (arr.astype(np.float32) if suf in FORMATOS_FLOAT
             else np.clip(arr, 0, 255).round().astype(np.uint8))
    if not cv2.imwrite(str(path), datos):
        raise OSError(f"no se pudo escribir {path}")
    return path


def save_capture(frame: np.ndarray, out_dir: Path = Path(".")) -> tuple[Path, Path]:
    """Guardado rapido: el mismo frame en .npy y en TIFF flotante, con timestamp."""
    stem = out_dir / time.strftime("captura_%Y%m%d_%H%M%S")
    return (save_frame_as(frame, stem.with_suffix(".npy")),
            save_frame_as(frame, stem.with_suffix(".tiff")))


# --- visor ----------------------------------------------------------------

def run_viewer(cam: Camera, cfg: Config, dark: np.ndarray | None = None) -> None:
    import cv2

    win = f"Camara - {getattr(cam, 'model', '?')}"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, int(cam.width * cfg.scale), int(cam.height * cfg.scale))

    state = {"pos": (cam.width // 2, cam.height // 2), "pinned": False,
             "radius": cfg.roi_radius, "avg": cfg.average, "exp": cam.exposure_ms,
             "dark_on": dark is not None}

    def on_mouse(event, mx, my, flags, _):
        if event == cv2.EVENT_LBUTTONDOWN:
            state["pinned"] = not state["pinned"]
        if not state["pinned"] and event == cv2.EVENT_MOUSEMOVE:
            _, _, ww, wh = cv2.getWindowImageRect(win)
            if ww > 0 and wh > 0:
                state["pos"] = (int(mx * cam.width / ww), int(my * cam.height / wh))

    cv2.setMouseCallback(win, on_mouse)

    def cerrada() -> bool:
        """La X de la ventana no llega por waitKey.

        Sin esto el bucle sigue vivo y el proximo imshow vuelve a crear la
        ventana, con lo que parece que el visor se reabre solo.
        """
        try:
            return cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1
        except cv2.error:
            return True

    fh = writer = last_bgr = None
    lost = streak = total = 0
    max_streak = 200

    try:
        while True:
            # Un frame perdido no puede tumbar el visor: sobre un enlace USB
            # degradado se pierden muchos. Se descarta y se reusa el anterior.
            stack = []
            for _ in range(state["avg"]):
                total += 1
                try:
                    stack.append(cam.grab().astype(np.float64))
                    streak = 0
                except UC480Error:
                    lost += 1
                    streak += 1
                    if streak >= max_streak:
                        print(f"enlace caido: {streak} frames seguidos perdidos",
                              file=sys.stderr)
                        return
            if stack:
                frame = np.mean(stack, axis=0)
                if state["dark_on"]:
                    frame = apply_dark(frame, dark)
                last_bgr = (frame if frame.ndim == 3
                            else np.repeat(frame[:, :, None], 3, axis=2))
            elif last_bgr is None:
                if cv2.waitKey(30) & 0xFF in (ord("q"), 27) or cerrada():
                    return
                continue

            bgr = last_bgr
            x = min(max(state["pos"][0], 0), cam.width - 1)
            y = min(max(state["pos"][1], 0), cam.height - 1)
            s = sample_patch(bgr, x, y, state["radius"], cfg.sat_level)

            disp = np.clip(bgr, 0, 255).astype(np.uint8).copy()
            r = state["radius"]
            cv2.rectangle(disp, (x - r - 1, y - r - 1), (x + r + 1, y + r + 1),
                          (0, 0, 255) if s.sat_frac else (0, 255, 0), 1)

            hud = [f"exp {state['exp']:.2f} ms   gain {cfg.gain}   "
                   f"bin {cfg.binning}x   avg {state['avg']}x   ROI {2 * r + 1}px",
                   f"USB perdidos {lost}/{total} ({lost / max(total, 1) * 100:.0f}%)"
                   + ("   DARK ON" if state["dark_on"] else ""),
                   "c: capturar frame (.npy+.tiff, floats sin cuantizar)"]
            if state["pinned"]:
                hud.append("[FIJADO]  click para soltar")
            for i, line in enumerate(hud + format_sample(s, cfg.decimals, cfg.normalize)):
                org = (10, 22 + 20 * i)
                cv2.putText(disp, line, org, cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                            (0, 0, 0), 3, cv2.LINE_AA)
                cv2.putText(disp, line, org, cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                            (255, 255, 255), 1, cv2.LINE_AA)
            cv2.imshow(win, disp)

            k = cv2.waitKey(1) & 0xFF
            if k in (ord("q"), 27) or cerrada():
                break
            elif k in (ord("+"), ord("=")):
                state["exp"] = cam.set_exposure(state["exp"] * 1.25)
            elif k == ord("-"):
                state["exp"] = cam.set_exposure(state["exp"] / 1.25)
            elif k == ord("]"):
                state["radius"] = min(state["radius"] + 1, 64)
            elif k == ord("["):
                state["radius"] = max(state["radius"] - 1, 0)
            elif k == ord("a"):
                state["avg"] = 1 if state["avg"] >= 16 else state["avg"] * 2
            elif k == ord("d") and dark is not None:
                state["dark_on"] = not state["dark_on"]
            elif k == ord("c"):
                npy_path, tiff_path = save_capture(bgr)
                print(f"captura guardada: {npy_path.name} "
                      f"(+ {tiff_path.name}, floats sin cuantizar)")
            elif k == ord("r"):
                state.update(radius=cfg.roi_radius, avg=cfg.average, pinned=False)
                state["exp"] = cam.set_exposure(cfg.exposure_ms)
            elif k == ord("s"):
                if writer is None:
                    nuevo = not cfg.csv_path.exists()
                    fh = cfg.csv_path.open("a", newline="", encoding="utf-8")
                    writer = csv.writer(fh)
                    if nuevo:
                        writer.writerow(["x", "y", "n_px", "R", "G", "B",
                                         "sd_R", "sd_G", "sd_B", "I", "sat_frac",
                                         "exp_ms", "gain", "binning", "avg"])
                writer.writerow([s.x, s.y, s.n,
                                 *(f"{v:.4f}" for v in s.mean),
                                 *(f"{v:.4f}" for v in s.std),
                                 f"{s.intensity:.4f}", f"{s.sat_frac:.4f}",
                                 f"{state['exp']:.3f}", cfg.gain, cfg.binning,
                                 state["avg"]])
                fh.flush()
                print(f"{cfg.csv_path}: R={s.mean[0]:.4f} "
                      f"G={s.mean[1]:.4f} B={s.mean[2]:.4f}")
    finally:
        if fh is not None:
            fh.close()
        cv2.destroyAllWindows()


def benchmark(cfg: Config, n: int = 25) -> list[dict]:
    """Tasa de error de transferencia USB para cada pixel clock.

    Sirve para comparar cables y puertos con numeros. Un enlace sano da ~0% en
    todas las filas; si el error crece con los MB por frame en vez de con la
    velocidad, el problema es el enlace fisico y no el ancho de banda.
    """
    rows = []
    with open_camera(replace(cfg, demo=False)) as cam:
        mb = cam.width * cam.height * cam.channels / 1e6
        lo, hi, _ = cam.cap["reloj"] or (0, 0, 1)
        for clk in (c for c in PIXEL_CLOCKS if lo <= c <= hi):
            cam.set_pixel_clock(clk)
            cam.set_fps(cfg.fps)
            cam.set_exposure(cfg.exposure_ms)
            t0, ok, err = time.perf_counter(), 0, 0
            for _ in range(n):
                if cam.dll.is_FreezeVideo(cam.h, IS_WAIT) == IS_SUCCESS:
                    ok += 1
                else:
                    err += 1
            dt = time.perf_counter() - t0
            rows.append({"clk": clk, "fps_max": cam.fps, "ok": ok, "err": err,
                         "rate": err / n, "fps_ef": ok / dt, "mbs": ok / dt * mb})
    return rows


# --- interfaz grafica -------------------------------------------------------

# Paleta nocturna tipo cyberpunk: fondo casi negro azulado y acentos de neon.
# Ademas de gusto, en un laboratorio de optica se trabaja a oscuras y una
# ventana blanca encandila y contamina la medida.
CYBER = {
    "fondo": "#080b14",
    "panel": "#101828",
    "hundido": "#0b1120",
    "borde": "#1e3a5f",
    "texto": "#cbf5ff",
    "tenue": "#5f819b",
    "neon": "#00f0ff",       # cian, acento principal
    "neon2": "#ff2bd1",      # magenta, acciones destacadas
    "ok": "#39ff14",
    "alerta": "#ff3860",
}

# Los trazos sobre la imagen se dibujan con cv2, que espera BGR.
ROI_BGR = (255, 240, 0)        # cian neon
ROI_SAT_BGR = (209, 43, 255)   # magenta, cuando hay saturacion
PERFIL_H_BGR = (255, 240, 0)   # cian, igual que su panel
PERFIL_V_BGR = (209, 43, 255)  # magenta, igual que el suyo


def _aplicar_estilo(root) -> None:
    """Viste la ventana con la paleta de CYBER.

    Usa el tema 'clam' porque los nativos de Windows ignoran casi todos los
    colores que se les pidan; clam si deja repintar fondo, borde y foco.
    """
    from tkinter import ttk

    st = ttk.Style(root)
    st.theme_use("clam")
    root.configure(background=CYBER["fondo"])

    st.configure(".", background=CYBER["fondo"], foreground=CYBER["texto"],
                 fieldbackground=CYBER["panel"], bordercolor=CYBER["borde"],
                 lightcolor=CYBER["borde"], darkcolor=CYBER["hundido"],
                 focuscolor=CYBER["neon"], insertcolor=CYBER["neon"])
    st.configure("TLabelframe", background=CYBER["fondo"], bordercolor=CYBER["borde"])
    st.configure("TLabelframe.Label", background=CYBER["fondo"],
                 foreground=CYBER["neon"], font=("Consolas", 9, "bold"))
    st.configure("TButton", background=CYBER["panel"], foreground=CYBER["neon"],
                 bordercolor=CYBER["borde"], padding=5, font=("Consolas", 9))
    st.map("TButton",
           background=[("active", CYBER["neon"]), ("disabled", CYBER["hundido"])],
           foreground=[("active", CYBER["fondo"]), ("disabled", "#33465c")],
           bordercolor=[("active", CYBER["neon"])])
    # Variante magenta para las acciones que uno busca primero.
    st.configure("Neon.TButton", foreground=CYBER["neon2"])
    st.map("Neon.TButton",
           background=[("active", CYBER["neon2"]), ("disabled", CYBER["hundido"])],
           foreground=[("active", CYBER["fondo"]), ("disabled", "#33465c")],
           bordercolor=[("active", CYBER["neon2"])])
    for clase in ("TEntry", "TSpinbox", "TCombobox"):
        st.configure(clase, fieldbackground=CYBER["hundido"], foreground=CYBER["texto"],
                     bordercolor=CYBER["borde"], arrowcolor=CYBER["neon"],
                     insertcolor=CYBER["neon"], selectbackground=CYBER["neon"],
                     selectforeground=CYBER["fondo"])
    # Un combobox readonly se dibuja "seleccionado" siempre, y con los colores
    # de seleccion queda un rectangulo claro encima del tema oscuro.
    st.map("TCombobox", fieldbackground=[("readonly", CYBER["hundido"])],
           selectbackground=[("readonly", CYBER["hundido"])],
           selectforeground=[("readonly", CYBER["texto"])],
           foreground=[("readonly", CYBER["texto"])])
    st.configure("TNotebook", background=CYBER["fondo"], bordercolor=CYBER["borde"])
    st.configure("TNotebook.Tab", background=CYBER["panel"], foreground=CYBER["tenue"],
                 bordercolor=CYBER["borde"], padding=(6, 3), font=("Consolas", 8))
    st.map("TNotebook.Tab", background=[("selected", CYBER["hundido"])],
           foreground=[("selected", CYBER["neon"])])
    st.configure("Horizontal.TScale", background=CYBER["panel"], troughcolor=CYBER["hundido"],
                 bordercolor=CYBER["borde"])
    # Clam pinta de blanco una barra desactivada, que encandila en el tema oscuro.
    st.map("Horizontal.TScale",
           background=[("disabled", CYBER["hundido"]), ("active", CYBER["neon"])],
           troughcolor=[("disabled", CYBER["fondo"])],
           bordercolor=[("disabled", CYBER["hundido"])],
           lightcolor=[("disabled", CYBER["hundido"])],
           darkcolor=[("disabled", CYBER["hundido"])])
    st.configure("TCheckbutton", background=CYBER["fondo"], foreground=CYBER["texto"],
                 indicatorcolor=CYBER["hundido"], focuscolor=CYBER["fondo"])
    st.map("TCheckbutton", indicatorcolor=[("selected", CYBER["neon"])],
           background=[("active", CYBER["fondo"]), ("disabled", CYBER["fondo"])],
           foreground=[("disabled", "#33465c")])
    st.configure("Vertical.TScrollbar", background=CYBER["panel"],
                 troughcolor=CYBER["hundido"], bordercolor=CYBER["borde"],
                 arrowcolor=CYBER["neon"])
    st.map("Vertical.TScrollbar", background=[("active", CYBER["neon"])])
    st.configure("Treeview", background=CYBER["hundido"], fieldbackground=CYBER["hundido"],
                 foreground=CYBER["texto"], bordercolor=CYBER["borde"], rowheight=20)
    st.configure("Treeview.Heading", background=CYBER["borde"], foreground=CYBER["neon"],
                 font=("Consolas", 9, "bold"))
    st.map("Treeview", background=[("selected", CYBER["neon2"])],
           foreground=[("selected", CYBER["fondo"])])
    # El desplegable del combobox es un Listbox de Tk clasico: no lo alcanza ttk.
    for opcion, valor in (("background", CYBER["hundido"]), ("foreground", CYBER["texto"]),
                          ("selectBackground", CYBER["neon"]),
                          ("selectForeground", CYBER["fondo"])):
        root.option_add(f"*TCombobox*Listbox.{opcion}", valor)


def panel_con_scroll(padre, ancho: int = 268):
    """Columna de controles con scroll vertical. Devuelve el frame interno.

    La pila de secciones no entra entera en una pantalla de portatil (768 de
    alto), asi que en vez de recortar secciones cada vez que se agrega una, la
    columna scrollea. Lo usan tanto el visor de camara como el polarimetro.
    """
    import tkinter as tk
    from tkinter import ttk

    lienzo = tk.Canvas(padre, width=ancho, highlightthickness=0,
                       background=CYBER["fondo"])
    scroll = ttk.Scrollbar(padre, orient="vertical", command=lienzo.yview)
    lienzo.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    lienzo.pack(side="left", fill="y", expand=True)

    interno = ttk.Frame(lienzo, padding=8)
    ventana = lienzo.create_window((0, 0), window=interno, anchor="nw")
    interno.bind("<Configure>",
                 lambda _e: lienzo.configure(scrollregion=lienzo.bbox("all")))
    lienzo.bind("<Configure>", lambda e: lienzo.itemconfigure(ventana, width=e.width))

    def rueda(ev):
        lienzo.yview_scroll(-ev.delta // 120, "units")

    # Solo mientras el puntero esta encima, asi la rueda sigue sirviendo para
    # los perfiles, la tabla y el resto de la ventana.
    interno.bind("<Enter>", lambda _e: lienzo.bind_all("<MouseWheel>", rueda))
    interno.bind("<Leave>", lambda _e: lienzo.unbind_all("<MouseWheel>"))
    return interno


def _ppm(rgb: np.ndarray) -> bytes:
    """Codifica un RGB de 8 bits como PPM binario.

    Tk lee PPM sin ayuda de nadie, asi que el visor no necesita Pillow: una
    dependencia menos para instalar y para empaquetar en el .exe.
    """
    h, w = rgb.shape[:2]
    return b"P6\n%d %d\n255\n" % (w, h) + np.ascontiguousarray(rgb).tobytes()


def perfil_horizontal(frame: np.ndarray, y: int) -> np.ndarray:
    """Fila y completa, en orden R, G, B: el corte a lo ancho de la imagen."""
    return np.asarray(frame)[y, :, ::-1]


def perfil_vertical(frame: np.ndarray, x: int) -> np.ndarray:
    """Columna x completa, en orden R, G, B: el corte a lo alto."""
    return np.asarray(frame)[:, x, ::-1]


TEXTO_DETECTAR = "Detectar camara (Thorlabs/uEye)"


def _remuestrear(vals: np.ndarray, n: int) -> np.ndarray:
    """Baja la cantidad de puntos a n para dibujarlos en un canvas angosto.

    Submuestrea en vez de promediar a proposito: un promedio esconderia los
    picos angostos, que son justo lo que uno busca en un perfil de intensidad.
    """
    if n <= 0 or len(vals) <= n:
        return vals
    return vals[np.linspace(0, len(vals) - 1, n).astype(int)]


class VentanaAjustes:
    """Los ajustes de camara de ThorCam, en una ventana aparte.

    Se arma leyendo construir_ajustes: agregar un ajuste a la tabla alcanza para
    que aparezca. Los cambios no tocan el DLL desde aca: van a app.cola y los
    aplica el hilo de captura entre dos frames, porque con la camara capturando el
    SDK esta ocupado. Con el visor detenido los aplica procesar_ajustes.
    """

    def __init__(self, root, app) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk, self.app = tk, ttk, app
        self.top = tk.Toplevel(root, background=CYBER["fondo"])
        self.top.title("Ajustes de camara")
        alto = min(660, root.winfo_screenheight() - 120)
        self.top.geometry(f"700x{alto}")
        self.top.protocol("WM_DELETE_WINDOW", self.cerrar)
        self._ajustando = False
        self._cam = None
        self._controles: dict[str, dict] = {}
        self.libro = None
        self.lbl_aviso = ttk.Label(self.top, foreground=CYBER["alerta"], wraplength=670,
                                   justify="left", font=("Consolas", 8))
        self.lbl_aviso.pack(side="bottom", fill="x", padx=8, pady=(0, 6))
        self.lbl_perdidos = ttk.Label(self.top, foreground=CYBER["tenue"],
                                      font=("Consolas", 8))
        self.lbl_perdidos.pack(side="bottom", anchor="w", padx=8)
        fila = ttk.Frame(self.top)
        fila.pack(side="bottom", fill="x", padx=8, pady=6)
        ttk.Button(fila, text="Cargar perfil...", command=self.cargar).pack(
            side="left", fill="x", expand=True)
        ttk.Button(fila, text="Guardar perfil...", command=self.guardar).pack(
            side="left", fill="x", expand=True, padx=4)
        ttk.Button(fila, text="Restablecer todo", style="Neon.TButton",
                   command=self.restablecer).pack(side="left", fill="x", expand=True)
        self.reconstruir()

    def existe(self) -> bool:
        return bool(self.top.winfo_exists())

    def cerrar(self) -> None:
        self.app.win_ajustes = None
        self.top.destroy()

    def reconstruir(self) -> None:
        """Rearma las pestañas para la camara abierta: cada una declara lo suyo."""
        ttk = self.ttk
        if self.libro is not None:
            self.libro.destroy()
        self._controles.clear()
        self._cam = self.app.cam
        self.libro = ttk.Notebook(self.top)
        self.libro.pack(side="top", fill="both", expand=True, padx=8, pady=(8, 0))
        if self._cam is None:
            self.lbl_aviso.config(text="no hay camara abierta")
            return
        self.top.title(f"Ajustes de camara - {self._cam.marca} {self._cam.model}")
        tabla = self._cam.ajustes()
        for pestana in PESTANAS:
            hoja = ttk.Frame(self.libro)
            self.libro.add(hoja, text=pestana)
            interno = panel_con_scroll(hoja, ancho=650)
            grupos: dict[str, object] = {}
            for a in tabla.values():
                if a.pestana != pestana:
                    continue
                if a.grupo not in grupos:
                    grupos[a.grupo] = ttk.LabelFrame(interno, text=a.grupo, padding=6)
                    grupos[a.grupo].pack(fill="x", pady=(0, 6))
                    grupos[a.grupo].columnconfigure(0, weight=1)
                self._crear_control(grupos[a.grupo], a)
        self.refrescar()

    def _crear_control(self, padre, a: Ajuste) -> None:
        tk, ttk = self.tk, self.ttk
        fila = padre.grid_size()[1]
        c: dict = {"ajuste": a}
        if a.tipo == "rango":
            ttk.Label(padre, text=a.etiqueta).grid(row=fila, column=0, sticky="w")
            c["var"] = tk.StringVar()
            c["spin"] = ttk.Spinbox(padre, width=11, textvariable=c["var"], from_=a.minimo,
                                    to=a.maximo, increment=a.paso,
                                    command=lambda k=a.clave: self._desde_spin(k))
            c["spin"].grid(row=fila, column=1, sticky="e", pady=1)
            c["spin"].bind("<Return>", lambda _e, k=a.clave: self._desde_spin(k))
            c["spin"].bind("<FocusOut>", lambda _e, k=a.clave: self._desde_spin(k))
            c["escala"] = ttk.Scale(padre, orient="horizontal", from_=a.minimo, to=a.maximo,
                                    command=lambda v, k=a.clave: self._desde_escala(k, v))
            c["escala"].grid(row=fila + 1, column=0, columnspan=2, sticky="ew")
            c["rango"] = ttk.Label(padre, font=("Consolas", 8), foreground=CYBER["tenue"],
                                   wraplength=600, justify="left")
            c["rango"].grid(row=fila + 2, column=0, columnspan=2, sticky="w", pady=(0, 4))
        elif a.tipo == "opciones":
            ttk.Label(padre, text=a.etiqueta).grid(row=fila, column=0, sticky="w")
            c["var"] = tk.StringVar()
            c["combo"] = ttk.Combobox(padre, textvariable=c["var"], state="readonly",
                                      values=a.opciones, width=26)
            c["combo"].grid(row=fila, column=1, sticky="e", pady=2)
            c["combo"].bind("<<ComboboxSelected>>",
                            lambda _e, k=a.clave: self._pedir(k, self._controles[k]["var"].get()))
        elif a.tipo == "bool":
            c["var"] = tk.BooleanVar()
            c["check"] = ttk.Checkbutton(
                padre, text=a.etiqueta, variable=c["var"],
                command=lambda k=a.clave: self._pedir(k, self._controles[k]["var"].get()))
            c["check"].grid(row=fila, column=0, columnspan=2, sticky="w", pady=1)
        elif a.tipo == "accion":
            c["boton"] = ttk.Button(padre, text=a.etiqueta,
                                    command=lambda k=a.clave: self._pedir(k, None))
            c["boton"].grid(row=fila, column=0, sticky="w", pady=2)
            c["salida"] = ttk.Label(padre, text="--", font=("Consolas", 9))
            c["salida"].grid(row=fila, column=1, sticky="e")
        self._controles[a.clave] = c

    def refrescar(self) -> None:
        """Pone cada control al dia con la camara: valor, rango y si esta activo."""
        cam = self.app.cam
        if cam is None:
            return
        self._ajustando = True
        try:
            for clave, a in cam.ajustes().items():
                c = self._controles.get(clave)
                if c is None:
                    continue
                c["ajuste"] = a
                estado = "normal" if a.activo else "disabled"
                if a.tipo == "rango":
                    # No se le pisa el texto a quien esta escribiendo en la casilla.
                    if self.top.focus_get() is not c["spin"]:
                        c["var"].set(f"{a.valor:.6g}" if a.valor is not None else "")
                    c["spin"].config(from_=a.minimo, to=a.maximo, increment=a.paso, state=estado)
                    c["escala"].config(from_=a.minimo, to=a.maximo)
                    c["escala"].state(["!disabled"] if a.activo else ["disabled"])
                    if a.valor is not None:
                        c["escala"].set(a.valor)
                    c["rango"].config(text=(f"{a.minimo:.8g} .. {a.maximo:.8g}"
                                            + (f"   {a.nota}" if a.nota else ""))
                                      if a.activo else (a.nota or "esta camara no lo admite"))
                elif a.tipo == "opciones":
                    c["var"].set("" if a.valor is None else str(a.valor))
                    c["combo"].config(values=a.opciones,
                                      state="readonly" if a.activo else "disabled")
                elif a.tipo == "bool":
                    c["var"].set(bool(a.valor))
                    c["check"].state(["!disabled"] if a.activo else ["disabled"])
        finally:
            self._ajustando = False
        self._mostrar_avisos()

    def _mostrar_avisos(self, error: str = "") -> None:
        cam = self.app.cam
        if cam is None:
            return
        avisos = avisos_no_lineales(cam.valores)
        texto = error
        if avisos:
            texto += ("\n" if texto else "") + "Fuera de la cadena lineal: " + ", ".join(avisos)
        self.lbl_aviso.config(text=texto)

    def notificar(self, clave: str, res) -> None:
        """Llega lo que aplico el hilo de captura."""
        if clave == "trigger_estado" and clave in self._controles:
            self._controles[clave]["salida"].config(
                text=str(res) if not isinstance(res, Exception) else "--")
        self.refrescar()
        if isinstance(res, Exception):
            self._mostrar_avisos(str(res))
        elif isinstance(res, list) and res:
            self._mostrar_avisos("; ".join(res))

    def mostrar_perdidos(self) -> None:
        """Los cuadros perdidos del visor, como el Dropped Frames de ThorCam."""
        total = getattr(self.app, "totales", 0)
        if total:
            perdidos = getattr(self.app, "perdidos", 0)
            self.lbl_perdidos.config(
                text=f"Cuadros perdidos: {perdidos}/{total} ({perdidos / total * 100:.0f}%)")

    def _pedir(self, clave: str, valor) -> None:
        if not self._ajustando:
            self.app.cola.pedir(clave, valor)

    def _desde_escala(self, clave: str, valor: str) -> None:
        if self._ajustando:
            return
        c = self._controles[clave]
        c["var"].set(f"{float(valor):.6g}")
        self._pedir(clave, float(valor))

    def _desde_spin(self, clave: str) -> None:
        c = self._controles[clave]
        if self._ajustando or not c["ajuste"].activo:
            return
        try:
            valor = float(c["var"].get().replace(",", "."))
        except ValueError:
            self.refrescar()
            return
        # FocusOut salta tambien sin cambios, y no hay que molestar al SDK por eso.
        if c["ajuste"].valor is None or abs(valor - float(c["ajuste"].valor)) > 1e-9:
            self._pedir(clave, valor)

    def restablecer(self) -> None:
        self.app.cola.pedir(ColaAjustes.RESTABLECER)

    def guardar(self) -> None:
        from tkinter import filedialog, messagebox
        if self.app.cam is None:
            return
        ruta = filedialog.asksaveasfilename(
            parent=self.top, title="Guardar perfil de ajustes", defaultextension=".json",
            initialfile=f"ajustes_{self.app.cam.model}.json".replace(" ", "_"),
            filetypes=[("Perfil de ajustes", "*.json")])
        if ruta:
            try:
                guardar_perfil(self.app.cam, Path(ruta))
            except OSError as e:
                messagebox.showerror("No se pudo guardar", str(e), parent=self.top)

    def cargar(self) -> None:
        from tkinter import filedialog, messagebox
        ruta = filedialog.askopenfilename(parent=self.top, title="Cargar perfil de ajustes",
                                          filetypes=[("Perfil de ajustes", "*.json")])
        if ruta:
            try:
                self.app.cola.pedir(ColaAjustes.PERFIL, leer_perfil(Path(ruta)))
            except (OSError, ValueError) as e:
                messagebox.showerror("No se pudo cargar", str(e), parent=self.top)


def abrir_ajustes(root, app) -> None:
    """Abre la ventana de ajustes de la app, o la trae al frente si ya esta."""
    win = getattr(app, "win_ajustes", None)
    if win is not None and win.existe():
        win.top.lift()
        return
    app.win_ajustes = VentanaAjustes(root, app)


def procesar_ajustes(app) -> None:
    """En cada cuadro de la GUI: aplica si la captura esta detenida y muestra el resultado."""
    if app.cam is not None and not app.corriendo.is_set():
        app.cola.drenar(app.cam)
    win = getattr(app, "win_ajustes", None)
    if win is not None and win.existe() and win._cam is not app.cam:
        win.reconstruir()
    for clave, res in app.cola.hechos():
        app.ajuste_aplicado(clave, res)
        if win is not None and win.existe():
            win.notificar(clave, res)
    if win is not None and win.existe():
        win.mostrar_perdidos()


class CamaraGUI:
    """Ventana unica: visor en vivo, lectura RGB y guardado en punto flotante.

    Es la cara por defecto del programa. El menu de texto sigue existiendo
    detras de --menu, pero ya no es lo que ve alguien que abre el ejecutable.

    La captura corre en un hilo aparte porque is_FreezeVideo bloquea hasta
    varios segundos sobre un enlace USB degradado, y Tk no puede quedarse
    congelado mientras tanto. El hilo solo deja el ultimo frame en self.frame;
    dibujar es siempre tarea del hilo de Tk.
    """

    INTERVALO_MS = 33

    def __init__(self, root, cfg: Config) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.root, self.cfg = root, cfg
        self.cam: Camera | None = None
        self.dark = load_dark(cfg.dark_path)
        self.frame: np.ndarray | None = None
        self.lock = threading.Lock()
        self.hilo: threading.Thread | None = None
        self.corriendo = threading.Event()
        # Espejo plano del checkbox: las variables de Tk no son thread-safe y
        # el hilo de captura necesita leer esto en cada vuelta.
        self.dark_on = self.dark is not None and cfg.use_dark
        self.pos = (0, 0)
        self.fijado = False
        self.perdidos = self.totales = 0
        self.ultima: Sample | None = None
        self.photo = None
        self._img_id = None
        self._disp = (0, 0, 0, 0)   # ox, oy, ancho, alto de la imagen dibujada
        # Perfiles de intensidad: la recta se planta donde uno hace click.
        self.perfil_pos: tuple[int, int] | None = None
        self.perfil_h_on = False
        self.perfil_v_on = False
        # Los ajustes se aplican desde el hilo de captura, entre frames, para no
        # tocar el DLL mientras is_FreezeVideo esta bloqueado en el otro.
        self.cola = ColaAjustes()
        self.win_ajustes: VentanaAjustes | None = None
        self.sin_trigger = False
        self._ajustando = False     # evita realimentar el callback de la barra

        root.title("Camara - fotometria RGB para Thorlabs DCx e IDS uEye")
        # Medido contra la pantalla: en un portatil de 1366x768 una ventana fija
        # de 760 de alto queda por debajo de la barra de tareas y corta el panel.
        ancho = min(1180, root.winfo_screenwidth() - 60)
        alto = min(760, root.winfo_screenheight() - 90)
        root.geometry(f"{ancho}x{alto}")
        root.minsize(900, 560)
        root.protocol("WM_DELETE_WINDOW", self.cerrar)

        self._construir()
        self._abrir_camara()
        self._refrescar_estado()
        self._tick()
        # Con una camara de verdad, se prende sola: es a lo que se vino.
        if self.cam is not None and not self.cfg.demo:
            self.iniciar()

    # --- construccion de la ventana ---

    def _construir(self) -> None:
        tk, ttk = self.tk, self.ttk
        # El panel de controles va dentro de un canvas con scroll: en un
        # portatil de 768 de alto no entra entero, y asi no se corta ni obliga
        # a recortar secciones cada vez que se agrega una.
        contenedor = ttk.Frame(self.root)
        contenedor.pack(side="left", fill="y")
        izq = panel_con_scroll(contenedor)

        der = ttk.Frame(self.root, padding=(0, 8, 8, 8))
        der.pack(side="right", fill="both", expand=True)

        f = ttk.LabelFrame(izq, text="Camara", padding=6)
        f.pack(fill="x")
        self.lbl_cam = ttk.Label(f, text="...", justify="left", wraplength=250,
                                 font=("Consolas", 9))
        self.lbl_cam.pack(anchor="w")
        self.lbl_aviso = ttk.Label(f, text="", justify="left", wraplength=250,
                                   font=("Consolas", 8), foreground=CYBER["alerta"])
        self.lbl_aviso.pack(anchor="w")
        self.btn_detectar = ttk.Button(f, text=TEXTO_DETECTAR, style="Neon.TButton",
                                       command=self.detectar)
        self.btn_detectar.pack(fill="x", pady=(6, 0))
        ttk.Button(f, text="Instalar drivers",
                   command=lambda: accion_instalar_drivers(self.root)).pack(fill="x", pady=(4, 0))
        ttk.Button(f, text="Ajustes de camara...",
                   command=lambda: abrir_ajustes(self.root, self)).pack(fill="x", pady=(4, 0))

        f = ttk.LabelFrame(izq, text="Captura", padding=6)
        f.pack(fill="x", pady=(6, 0))
        self.var_exp = tk.StringVar(value=f"{self.cfg.exposure_ms:g}")
        self.var_gain = tk.IntVar(value=self.cfg.gain)
        self.var_bin = tk.IntVar(value=self.cfg.binning)
        self.var_avg = tk.IntVar(value=self.cfg.average)
        self.var_roi = tk.IntVar(value=self.cfg.roi_radius)
        self.spin_gain = ttk.Spinbox(f, from_=0, to=100, width=6, textvariable=self.var_gain)
        self.spin_bin = ttk.Spinbox(f, from_=min(BINNING_MODES), to=max(BINNING_MODES), width=6,
                                    textvariable=self.var_bin)
        campos = (
            ("Exposicion (ms)", ttk.Entry(f, textvariable=self.var_exp, width=8)),
            ("Ganancia (0..100)", self.spin_gain),
            ("Binning vertical", self.spin_bin),
            ("Frames promediados", ttk.Spinbox(f, from_=1, to=64, width=6,
                                               textvariable=self.var_avg)),
            ("Radio del ROI (px)", ttk.Spinbox(f, from_=0, to=64, width=6,
                                               textvariable=self.var_roi)),
        )
        for i, (texto, widget) in enumerate(campos):
            ttk.Label(f, text=texto).grid(row=i, column=0, sticky="w", pady=2)
            widget.grid(row=i, column=1, sticky="e", pady=2)
        f.columnconfigure(0, weight=1)
        # La barra aplica la exposicion en vivo, sin pasar por "Aplicar":
        # es el parametro que uno busca a tientas hasta que la señal entra en
        # escala, y con el visor corriendo se ve el efecto al instante.
        n = len(campos)
        self.slider_exp = ttk.Scale(f, orient="horizontal", from_=0.1, to=100.0,
                                    command=self._mover_exposicion)
        self.slider_exp.grid(row=n, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.lbl_exp = ttk.Label(f, text="exposicion en vivo", font=("Consolas", 8),
                                 foreground=CYBER["tenue"])
        self.lbl_exp.grid(row=n + 1, column=0, columnspan=2, sticky="w")
        ttk.Button(f, text="Aplicar", command=self.aplicar).grid(
            row=n + 2, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        f = ttk.LabelFrame(izq, text="Visor en vivo", padding=6)
        f.pack(fill="x", pady=(6, 0))
        fila = ttk.Frame(f)
        fila.pack(fill="x")
        self.btn_iniciar = ttk.Button(fila, text="Iniciar visor", style="Neon.TButton",
                                      command=self.iniciar)
        self.btn_iniciar.pack(side="left", fill="x", expand=True)
        self.btn_detener = ttk.Button(fila, text="Detener", command=self.detener)
        self.btn_detener.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.var_dark = tk.BooleanVar(value=self.dark_on)
        self.chk_dark = ttk.Checkbutton(f, text="Restar dark frame",
                                        variable=self.var_dark, command=self._cambiar_dark)
        self.chk_dark.pack(anchor="w", pady=(6, 0))
        fila = ttk.Frame(f)
        fila.pack(fill="x", pady=(4, 0))
        ttk.Button(fila, text="Capturar dark", command=self.capturar_dark).pack(
            side="left", fill="x", expand=True)
        self.btn_usb = ttk.Button(fila, text="Diagnostico USB",
                                  command=self.diagnostico_usb)
        self.btn_usb.pack(side="left", fill="x", expand=True, padx=(4, 0))

        f = ttk.LabelFrame(izq, text="Perfiles de intensidad", padding=6)
        f.pack(fill="x", pady=(6, 0))
        self.btn_perfil_h = ttk.Button(f, text="Perfil horizontal  --",
                                       command=self.alternar_perfil_h)
        self.btn_perfil_h.pack(fill="x")
        self.btn_perfil_v = ttk.Button(f, text="Perfil vertical    |",
                                       command=self.alternar_perfil_v)
        self.btn_perfil_v.pack(fill="x", pady=(4, 0))
        ttk.Label(f, justify="left", font=("Consolas", 8), foreground=CYBER["tenue"],
                  text="click en la imagen planta la recta;\n"
                       "click derecho suelta el ROI"
                  ).pack(anchor="w", pady=(6, 0))

        f = ttk.LabelFrame(izq, text="Guardar imagen", padding=6)
        f.pack(fill="x", pady=(6, 0))
        self.var_formato = tk.StringVar(value=FORMATO_POR_DEFECTO)
        fila = ttk.Frame(f)
        fila.pack(fill="x")
        ttk.Label(fila, text="Formato").pack(side="left")
        ttk.Combobox(fila, textvariable=self.var_formato, state="readonly", width=8,
                     values=[".npy", ".tiff", ".png", ".jpg"]).pack(side="right")
        self.btn_guardar = ttk.Button(f, text="Guardar imagen...",
                                      style="Neon.TButton", command=self.guardar_imagen)
        self.btn_guardar.pack(fill="x", pady=(6, 0))
        self.btn_rapido = ttk.Button(f, text="Guardado rapido (.npy + .tiff)",
                                     command=self.guardar_rapido)
        self.btn_rapido.pack(fill="x", pady=(4, 0))
        ttk.Label(f, justify="left", font=("Consolas", 8), foreground=CYBER["tenue"],
                  text=".npy (float64) y .tiff (float 32)\n"
                       "guardan el valor por pixel con\n"
                       "decimales: son los de medir.\n"
                       ".png y .jpg redondean a entero\n"
                       "0..255: son los de mirar."
                  ).pack(anchor="w", pady=(6, 0))
        self.lbl_guardado = ttk.Label(f, text="", foreground=CYBER["ok"], wraplength=250,
                                      justify="left", font=("Consolas", 8))
        self.lbl_guardado.pack(anchor="w")

        # La imagen se lleva el espacio que sobra; la columna de perfiles tiene
        # ancho fijo y solo aparece cuando hay algun perfil encendido.
        centro = ttk.Frame(der)
        centro.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(centro, background=CYBER["hundido"], highlightthickness=1,
                                highlightbackground=CYBER["borde"])
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Button-3>", self._on_click_derecho)

        self.col_perfiles = ttk.Frame(centro, width=330)
        self.col_perfiles.pack_propagate(False)
        self.canvas_perfil_h = tk.Canvas(self.col_perfiles, background=CYBER["hundido"],
                                         highlightthickness=1, height=150,
                                         highlightbackground=CYBER["neon"])
        self.canvas_perfil_v = tk.Canvas(self.col_perfiles, background=CYBER["hundido"],
                                         highlightthickness=1, height=150,
                                         highlightbackground=CYBER["neon2"])

        barra = ttk.Frame(der)
        barra.pack(fill="x", pady=(6, 0))
        self.lbl_lectura = ttk.Label(barra, text="inicia el visor para medir",
                                     font=("Consolas", 10), justify="left",
                                     foreground=CYBER["neon"])
        self.lbl_lectura.pack(side="left")
        self.lbl_estado = ttk.Label(barra, text="", foreground=CYBER["tenue"],
                                    justify="right", font=("Consolas", 9))
        self.lbl_estado.pack(side="right")

        f = ttk.LabelFrame(der, text="Medidas del ROI", padding=6)
        f.pack(fill="both", pady=(8, 0))
        fila = ttk.Frame(f)
        fila.pack(fill="x")
        ttk.Label(fila, text="Etiqueta").pack(side="left")
        self.var_etiqueta = tk.StringVar()
        ttk.Entry(fila, textvariable=self.var_etiqueta, width=14).pack(side="left", padx=6)
        self.btn_medir = ttk.Button(fila, text="Anotar medida en CSV", command=self.medir)
        self.btn_medir.pack(side="left")
        ttk.Label(fila, text=f"-> {self.cfg.csv_path}", foreground=CYBER["tenue"],
                  font=("Consolas", 8)).pack(side="left", padx=8)
        cols = ("etiqueta", "x", "y", "R", "G", "B", "I", "sem")
        self.tabla = ttk.Treeview(f, columns=cols, show="headings", height=5)
        for c, ancho in zip(cols, (110, 60, 60, 90, 90, 90, 90, 80)):
            self.tabla.heading(c, text=c)
            self.tabla.column(c, width=ancho, anchor="e")
        self.tabla.pack(fill="both", expand=True, pady=(6, 0))

    # --- camara ---

    def _abrir_camara_real(self) -> None:
        """Detecta y abre la camara, y refresca la ficha. Propaga UC480Error."""
        self.cam = open_camera(self.cfg)
        self.pos = (self.cam.width // 2, self.cam.height // 2)
        self.root.title(f"Camara - {self.cam.marca} {self.cam.model}")
        self._actualizar_ficha()
        self.var_exp.set(f"{self.cam.exposure_ms:g}")
        self._rango_exposicion()

    def _actualizar_ficha(self) -> None:
        cam = self.cam
        self.lbl_cam.config(
            text=f"{ficha_camara(cam)}\n{cam.width}x{cam.height}   "
                 f"exp {cam.exposure_ms:.2f} ms\n"
                 f"clk {getattr(cam, 'pixel_clock', 0)} MHz   {cam.fps:.1f} fps",
            foreground=CYBER["neon2"] if self.cfg.demo else CYBER["ok"])
        # Lo que esta camara no tiene no se ofrece: la uEye C1280R12C no tiene
        # binning ni ganancia master.
        tabla = cam.ajustes()
        self.spin_gain.config(state="normal" if tabla["ganancia"].activo else "disabled")
        self.spin_bin.config(state="normal" if tabla["bin_v"].activo else "disabled")
        avisos = avisos_no_lineales(cam.valores)
        self.lbl_aviso.config(text="Fuera de la cadena lineal: " + ", ".join(avisos)
                              if avisos else "")

    def ajuste_aplicado(self, clave: str, res) -> None:
        """El hilo de captura aplico un ajuste: se sincroniza el resto de la ventana."""
        if self.cam is None:
            return
        self._actualizar_ficha()
        if isinstance(res, Exception):
            self.lbl_aviso.config(text=str(res))
        v = self.cam.valores
        self.cfg = replace(self.cfg, gain=int(v["ganancia"]), exposure_ms=self.cam.exposure_ms,
                           binning=min(factor_de_opcion(v["bin_v"]), max(BINNING_MODES)))
        self.var_gain.set(self.cfg.gain)
        self.var_bin.set(self.cfg.binning)
        if clave == "exposicion":
            self.lbl_exp.config(text=f"exposicion {self.cam.exposure_ms:.2f} ms")
        else:             # con el dedo en la barra no se la mueve: daria saltos
            self.var_exp.set(f"{self.cam.exposure_ms:g}")
            self._rango_exposicion()

    def _abrir_camara(self) -> None:
        from tkinter import messagebox
        try:
            self._abrir_camara_real()
        except UC480Error as e:
            if not messagebox.askyesno(
                    "Sin camara",
                    f"{e}\n\n¿Seguir en modo demo?\n\n"
                    f"Podes conectar la camara despues y usar '{TEXTO_DETECTAR}'."):
                self.lbl_cam.config(text="sin camara", foreground=CYBER["alerta"])
                return
            self.cfg = replace(self.cfg, demo=True)
            self._abrir_camara_real()

    def _cerrar_camara(self) -> None:
        if self.cam is not None:
            self.cam.close()
            self.cam = None

    def detectar(self) -> None:
        """Busca camaras de las dos marcas, dice que tipo son y engancha una libre.

        Es la salida para el caso normal: se abrio el programa antes de
        prender la camara, o se cambio de puerto. Evita tener que reiniciar.

        La camara propia se cierra primero: abierta, retiene su DLL y la otra no
        se podria ni cargar (ver _cargar_dll), asi que no se veria la otra marca.
        """
        from tkinter import messagebox
        self.detener()
        self._cerrar_camara()
        self.btn_detectar.config(state="disabled", text="buscando...")
        self.root.update_idletasks()
        try:
            camaras = detectar_camaras(self.cfg.driver)
            detalle = "\n".join(f"  - {describir_camara(c)}" for c in camaras)
            if not camaras:
                pista = diagnostico_usb()
                messagebox.showwarning(
                    "Deteccion", "Ninguna camara Thorlabs ni uEye responde."
                    + (f"\n\n{pista}" if pista else ""))
                self._abrir_camara()
                return
            self.cfg = replace(self.cfg, demo=False)
            try:
                self._abrir_camara_real()
            except UC480Error as e:
                messagebox.showerror("Deteccion", f"Se detecto:\n{detalle}\n\n"
                                                  f"pero no se pudo abrir: {e}")
                self.cfg = replace(self.cfg, demo=True)
                self._abrir_camara()
                return
            messagebox.showinfo("Deteccion", f"Conectada:\n{detalle}")
            self.iniciar()
        except UC480Error as e:
            messagebox.showerror("Deteccion", str(e))
            self._abrir_camara()
        finally:
            self.btn_detectar.config(state="normal", text=TEXTO_DETECTAR)

    def aplicar(self) -> None:
        from tkinter import messagebox
        try:
            nueva = replace(self.cfg, exposure_ms=float(self.var_exp.get()),
                            gain=int(self.var_gain.get()),
                            binning=int(self.var_bin.get()),
                            average=int(self.var_avg.get()),
                            roi_radius=int(self.var_roi.get()))
        except (ValueError, TypeError) as e:
            messagebox.showerror("Parametros invalidos", str(e))
            return
        # Sin reabrir la camara: se perderian los demas ajustes de la ventana.
        if nueva.gain != self.cfg.gain:
            self.cola.pedir("ganancia", nueva.gain)
        if nueva.binning != self.cfg.binning:
            self.cola.pedir("bin_v", f"{nueva.binning}x")
        self.cola.pedir("exposicion", nueva.exposure_ms)
        self.cfg = replace(self.cfg, average=nueva.average, roi_radius=nueva.roi_radius)

    # --- visor ---

    def iniciar(self) -> None:
        if self.cam is None or self.corriendo.is_set():
            return
        self.perdidos = self.totales = 0
        self.cam.reanudar()
        self.corriendo.set()
        self.hilo = threading.Thread(target=self._bucle, daemon=True)
        self.hilo.start()
        self._refrescar_estado()

    def detener(self) -> None:
        self.corriendo.clear()
        if self.cam is not None:
            self.cam.cancelar()         # por si esta esperando un trigger externo
        hilo, self.hilo = self.hilo, None
        if hilo is not None:
            hilo.join(timeout=5.0)
        self._refrescar_estado()

    def _bucle(self) -> None:
        while self.corriendo.is_set():
            cam, cfg = self.cam, self.cfg
            if cam is None:
                return
            self.cola.drenar(cam)
            pila = []
            for _ in range(cfg.average):
                if not self.corriendo.is_set():
                    return
                try:
                    pila.append(cam.grab().astype(np.float64))
                    self.sin_trigger = False
                except CapturaCancelada:
                    return
                except TriggerTimeout:
                    # Sin pulso no se perdio nada: no cuenta como error de USB.
                    self.sin_trigger = True
                    continue
                except UC480Error:
                    self.perdidos += 1
                self.totales += 1
            if not pila:
                continue
            f = np.mean(pila, axis=0)
            if f.ndim == 2:
                f = np.repeat(f[:, :, None], 3, axis=2)
            if self.dark_on:
                f = apply_dark(f, self.dark)
            with self.lock:
                self.frame = f

    def _tick(self) -> None:
        with self.lock:
            f = self.frame
        if f is not None:
            self._dibujar(f)
        procesar_ajustes(self)
        self._refrescar_estado()
        self.root.after(self.INTERVALO_MS, self._tick)

    def _dibujar(self, f: np.ndarray) -> None:
        import cv2
        h, w = f.shape[:2]
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 10 or ch < 10:
            return
        k = min(cw / w, ch / h)
        dw, dh = max(1, int(w * k)), max(1, int(h * k))
        ox, oy = (cw - dw) // 2, (ch - dh) // 2
        self._disp = (ox, oy, dw, dh)

        x = min(max(self.pos[0], 0), w - 1)
        y = min(max(self.pos[1], 0), h - 1)
        s = sample_patch(f, x, y, self.cfg.roi_radius, self.cfg.sat_level)
        self.ultima = s

        disp = cv2.resize(np.clip(f, 0, 255).astype(np.uint8), (dw, dh),
                          interpolation=cv2.INTER_AREA)

        # Rectas de los perfiles, en la posicion del ultimo click.
        px, py = self.perfil_pos if self.perfil_pos is not None else (w // 2, h // 2)
        px, py = min(max(px, 0), w - 1), min(max(py, 0), h - 1)
        if self.perfil_h_on:
            fy = int(py * k)
            cv2.line(disp, (0, fy), (dw, fy), PERFIL_H_BGR, 1)
        if self.perfil_v_on:
            fx = int(px * k)
            cv2.line(disp, (fx, 0), (fx, dh), PERFIL_V_BGR, 1)

        r = self.cfg.roi_radius
        cv2.rectangle(disp, (int((x - r) * k) - 1, int((y - r) * k) - 1),
                      (int((x + r + 1) * k) + 1, int((y + r + 1) * k) + 1),
                      ROI_SAT_BGR if s.sat_frac else ROI_BGR, 1)

        if self.perfil_h_on:
            self._dibujar_perfil(self.canvas_perfil_h, perfil_horizontal(f, py),
                                 f"perfil horizontal  fila y={py}", px, w, CYBER["neon"])
        if self.perfil_v_on:
            self._dibujar_perfil(self.canvas_perfil_v, perfil_vertical(f, px),
                                 f"perfil vertical  columna x={px}", py, h, CYBER["neon2"])

        self.photo = self.tk.PhotoImage(data=_ppm(disp[:, :, ::-1]))
        if self._img_id is None:
            self._img_id = self.canvas.create_image(ox, oy, anchor="nw", image=self.photo)
        else:
            self.canvas.itemconfigure(self._img_id, image=self.photo)
            self.canvas.coords(self._img_id, ox, oy)
        recortados = canales_recortados(s) if self.dark_on is False else []
        self.lbl_lectura.config(
            text="\n".join(format_sample(s, self.cfg.decimals, self.cfg.normalize))
                 + ("\n[FIJADO] click para soltar" if self.fijado else "")
                 + (f"\n{'/'.join(recortados)} en 0: sube el nivel de negro" if recortados else ""))

    def _refrescar_estado(self) -> None:
        vivo = self.corriendo.is_set()
        hay_imagen = vivo and self.frame is not None
        # El guardado solo tiene sentido con el visor en vivo andando: recien
        # ahi hay un frame promediado para escribir.
        for b in (self.btn_guardar, self.btn_rapido, self.btn_medir):
            b.config(state="normal" if hay_imagen else "disabled")
        self.btn_iniciar.config(state="disabled" if vivo or self.cam is None else "normal")
        self.btn_detener.config(state="normal" if vivo else "disabled")
        self.chk_dark.config(state="normal" if self.dark is not None else "disabled")
        if self.sin_trigger:
            self.lbl_estado.config(text="esperando un pulso de trigger...")
        elif self.totales:
            self.lbl_estado.config(
                text=f"USB perdidos {self.perdidos}/{self.totales} "
                     f"({self.perdidos / self.totales * 100:.0f}%)"
                     + ("   DARK ON" if self.dark_on else ""))

    def _cambiar_dark(self) -> None:
        self.dark_on = bool(self.var_dark.get())

    def _coords_imagen(self, ev) -> tuple[int, int] | None:
        """Pasa un click del canvas a coordenadas de la imagen completa."""
        ox, oy, dw, dh = self._disp
        if self.cam is None or dw <= 0 or dh <= 0:
            return None
        x = (ev.x - ox) * self.cam.width / dw
        y = (ev.y - oy) * self.cam.height / dh
        return (int(min(max(x, 0), self.cam.width - 1)),
                int(min(max(y, 0), self.cam.height - 1)))

    def _on_motion(self, ev) -> None:
        if self.fijado:
            return
        p = self._coords_imagen(ev)
        if p is not None:
            self.pos = p

    def _on_click(self, ev) -> None:
        """Planta el ROI y las rectas de perfil donde se hizo click."""
        p = self._coords_imagen(ev)
        if p is None:
            return
        self.pos = self.perfil_pos = p
        self.fijado = True

    def _on_click_derecho(self, _ev) -> None:
        self.fijado = False

    # --- exposicion en vivo ---

    def _mover_exposicion(self, valor: str) -> None:
        if self._ajustando:
            return
        ms = float(valor)
        self.lbl_exp.config(text=f"exposicion {ms:.2f} ms")
        self.var_exp.set(f"{ms:g}")
        self.cola.pedir("exposicion", ms)    # lo aplica el hilo entre frames

    def _rango_exposicion(self) -> None:
        """Ajusta la barra al rango que declara la camara abierta."""
        if self.cam is None:
            return
        # De la tabla y no del SDK: el hilo de captura puede estar usando el DLL.
        rango = self.cam.ajustes()["exposicion"]
        lo, hi = rango.minimo, rango.maximo
        self._ajustando = True
        try:
            self.slider_exp.config(from_=lo, to=hi)
            self.slider_exp.set(self.cam.exposure_ms)
        finally:
            self._ajustando = False
        self.lbl_exp.config(text=f"exposicion {self.cam.exposure_ms:.2f} ms "
                                 f"(rango {lo:.2f}-{hi:.1f})")

    # --- perfiles ---

    def alternar_perfil_h(self) -> None:
        self.perfil_h_on = not self.perfil_h_on
        self._reacomodar_perfiles()

    def alternar_perfil_v(self) -> None:
        self.perfil_v_on = not self.perfil_v_on
        self._reacomodar_perfiles()

    def _reacomodar_perfiles(self) -> None:
        """Muestra, oculta y reparte la columna segun que perfiles esten activos."""
        for canvas in (self.canvas_perfil_h, self.canvas_perfil_v):
            canvas.pack_forget()
        if self.perfil_h_on:
            self.canvas_perfil_h.pack(fill="both", expand=True)
        if self.perfil_v_on:
            self.canvas_perfil_v.pack(fill="both", expand=True,
                                      pady=(4 if self.perfil_h_on else 0, 0))
        if self.perfil_h_on or self.perfil_v_on:
            self.col_perfiles.pack(side="right", fill="y", padx=(6, 0))
        else:
            self.col_perfiles.pack_forget()   # la imagen recupera todo el ancho
        self.btn_perfil_h.config(
            text=f"Perfil horizontal  {'ON ' if self.perfil_h_on else '--'}",
            style="Neon.TButton" if self.perfil_h_on else "TButton")
        self.btn_perfil_v.config(
            text=f"Perfil vertical    {'ON' if self.perfil_v_on else '|'}",
            style="Neon.TButton" if self.perfil_v_on else "TButton")

    def _dibujar_perfil(self, canvas, vals: np.ndarray, titulo: str,
                        marca: int, largo: int, acento: str) -> None:
        """Dibuja R, G y B de un corte, con grilla 0..255 y la marca del ROI."""
        canvas.delete("all")
        cw, ch = canvas.winfo_width(), canvas.winfo_height()
        if cw < 20 or ch < 20:
            return
        m = 4                                   # margen interno
        cab = 13                                # franja del titulo, sin trazo
        alto = ch - 2 * m - cab
        if alto < 10:
            return
        datos = _remuestrear(vals, cw - 2 * m)
        n = len(datos)
        if n < 2:
            return
        tope = m + cab                          # y del nivel 255

        for nivel in (64, 128, 192):
            y = tope + alto * (1 - nivel / 255.0)
            canvas.create_line(m, y, cw - m, y, fill=CYBER["borde"], dash=(2, 4))
        if largo > 1:                           # donde cae el ROI sobre el corte
            xm = m + (cw - 2 * m) * marca / (largo - 1)
            canvas.create_line(xm, tope, xm, ch - m, fill=acento, dash=(3, 3))

        paso = (cw - 2 * m) / (n - 1)
        for canal, color in enumerate(("#ff4d6d", "#39ff14", "#4da6ff")):
            pts = []
            for i, v in enumerate(datos[:, canal]):
                pts += [m + i * paso, tope + alto * (1 - min(max(v, 0), 255) / 255.0)]
            canvas.create_line(*pts, fill=color, width=1)
        canvas.create_text(m + 2, m, anchor="nw", text=titulo,
                           fill=acento, font=("Consolas", 8))

    # --- guardado ---

    def _frame_actual(self) -> np.ndarray | None:
        with self.lock:
            return None if self.frame is None else self.frame.copy()

    def guardar_imagen(self) -> None:
        from tkinter import filedialog, messagebox
        f = self._frame_actual()
        if f is None:
            messagebox.showinfo("Sin imagen", "Inicia el visor en vivo primero.")
            return
        ext = self.var_formato.get()
        ruta = filedialog.asksaveasfilename(
            title="Guardar imagen", defaultextension=ext,
            initialfile=time.strftime(f"captura_%Y%m%d_%H%M%S{ext}"),
            filetypes=[("NumPy float64 (exacto)", "*.npy"),
                       ("TIFF 32 bits flotante", "*.tiff *.tif"),
                       ("PNG 8 bits", "*.png"), ("JPEG 8 bits", "*.jpg *.jpeg")])
        if not ruta:
            return
        try:
            p = save_frame_as(f, Path(ruta))
        except (ValueError, OSError) as e:
            messagebox.showerror("No se pudo guardar", str(e))
            return
        self.lbl_guardado.config(text=f"guardado: {p.name}")

    def guardar_rapido(self) -> None:
        from tkinter import messagebox
        f = self._frame_actual()
        if f is None:
            return
        try:
            npy, tiff = save_capture(f)
        except (ValueError, OSError) as e:
            messagebox.showerror("No se pudo guardar", str(e))
            return
        self.lbl_guardado.config(text=f"guardado: {npy.name} + {tiff.name}")

    def medir(self) -> None:
        """Anota la lectura del ROI en el CSV, con una etiqueta libre."""
        s = self.ultima
        if s is None:
            return
        etiqueta = self.var_etiqueta.get().strip()
        nuevo = not self.cfg.csv_path.exists()
        with self.cfg.csv_path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if nuevo:
                w.writerow(["etiqueta", "x", "y", "R", "G", "B",
                            "sem_R", "sem_G", "sem_B", "I", "sat_frac",
                            "exp_ms", "gain", "binning", "avg", "n_px"])
            w.writerow([etiqueta, s.x, s.y, *(f"{v:.4f}" for v in s.mean),
                        *(f"{v:.4f}" for v in s.sem), f"{s.intensity:.4f}",
                        f"{s.sat_frac:.4f}", f"{self.cam.exposure_ms:.3f}",
                        self.cfg.gain, self.cfg.binning, self.cfg.average, s.n])
        self.tabla.insert("", "end", values=(
            etiqueta or "-", s.x, s.y, f"{s.mean[0]:.4f}", f"{s.mean[1]:.4f}",
            f"{s.mean[2]:.4f}", f"{s.intensity:.4f}", f"{sum(s.sem) / 3:.4f}"))
        self.tabla.yview_moveto(1.0)
        self.var_etiqueta.set("")

    # --- dark y diagnostico ---

    def capturar_dark(self) -> None:
        from tkinter import messagebox
        if self.cam is None:
            return
        if not messagebox.askokcancel("Dark frame", "Tapa la camara y no la muevas."):
            return
        estaba = self.corriendo.is_set()
        self.detener()
        self.root.config(cursor="watch")
        self.root.update_idletasks()
        try:
            d = capture_dark(self.cam, 32)
        except UC480Error as e:
            messagebox.showerror("Error", str(e))
        else:
            np.save(self.cfg.dark_path, d)
            self.dark = d
            self.var_dark.set(True)
            self.dark_on = True
            messagebox.showinfo("Dark frame", f"guardado en {self.cfg.dark_path}")
        finally:
            self.root.config(cursor="")
        if estaba:
            self.iniciar()

    def diagnostico_usb(self) -> None:
        from tkinter import messagebox
        if self.cfg.demo:
            messagebox.showinfo("Diagnostico USB", "Requiere la camara real.")
            return
        estaba = self.corriendo.is_set()
        self.detener()
        self._cerrar_camara()          # benchmark() abre la camara por su cuenta
        self.btn_usb.config(state="disabled", text="midiendo...")

        def trabajo():
            try:
                res = benchmark(self.cfg)
            except UC480Error as e:
                res = e
            self.root.after(0, lambda: self._fin_usb(res, estaba))

        threading.Thread(target=trabajo, daemon=True).start()

    def _fin_usb(self, res, estaba: bool) -> None:
        from tkinter import messagebox
        self.btn_usb.config(state="normal", text="Diagnostico USB")
        if isinstance(res, Exception):
            messagebox.showerror("Diagnostico USB", str(res))
        else:
            top = self.tk.Toplevel(self.root)
            top.title("Tasa de error por pixel clock")
            cols = ("MHz", "fps max", "ok", "err", "tasa", "fps ef", "MB/s")
            tv = self.ttk.Treeview(top, columns=cols, show="headings", height=len(res))
            for c in cols:
                tv.heading(c, text=c)
                tv.column(c, width=80, anchor="e")
            for r in res:
                tv.insert("", "end", values=(
                    r["clk"], f"{r['fps_max']:.1f}", r["ok"], r["err"],
                    f"{r['rate'] * 100:.0f}%", f"{r['fps_ef']:.1f}", f"{r['mbs']:.1f}"))
            tv.pack(fill="both", expand=True, padx=8, pady=8)
            self.ttk.Label(top, text="un enlace sano deberia dar ~0% en todas las filas",
                           foreground="#555").pack(pady=(0, 8))
        self._abrir_camara()
        if estaba:
            self.iniciar()

    def cerrar(self) -> None:
        self.detener()
        self._cerrar_camara()
        self.root.destroy()


def run_gui(cfg: Config) -> int:
    import tkinter as tk

    root = tk.Tk()
    try:
        _aplicar_estilo(root)
    except tk.TclError:
        pass          # sin el tema clam la ventana igual funciona, solo mas fea
    CamaraGUI(root, cfg)
    root.mainloop()
    return 0


# --- interfaz de linea de comandos ----------------------------------------

def _console():
    from rich.console import Console
    return Console()


def _banner(con, cam_info: str) -> None:
    from rich.panel import Panel
    con.print(Panel.fit(
        "[bold cyan]Camara[/]  ·  fotometria RGB para Thorlabs DCx e IDS uEye\n"
        f"[dim]{cam_info}[/]", border_style="cyan"))


def _tabla_binning(con) -> None:
    from rich.table import Table
    t = Table(title="Binning por hardware (medido en una DCU224C)",
              header_style="bold cyan")
    t.add_column("Modo"); t.add_column("Alto", justify="right")
    t.add_column("SNR relativo", justify="right")
    for b in sorted(BINNING_MODES):
        t.add_row(f"{b}x" if b > 1 else "sin binning",
                  str(1024 // b), f"{BINNING_SNR[b]:.2f}x")
    con.print(t)


def configurar(cfg: Config, con) -> Config | None:
    """Menu de parametros de captura. Devuelve None si el usuario cancela."""
    import questionary

    _tabla_binning(con)
    binning = questionary.select(
        "Binning (suma carga en el CCD antes del ADC)",
        choices=[questionary.Choice(
            f"{b}x vertical  ->  SNR {BINNING_SNR[b]:.2f}x, alto {1024 // b}"
            if b > 1 else "sin binning  ->  maxima resolucion",
            value=b) for b in sorted(BINNING_MODES)],
        default=cfg.binning).ask()
    if binning is None:
        return None

    exp = questionary.text(
        "Exposicion en ms (0.12 a 99.9)", default=str(cfg.exposure_ms),
        validate=lambda v: _es_float(v, 0.1, 100.0) or "numero entre 0.1 y 100").ask()
    if exp is None:
        return None

    gain = questionary.select(
        "Ganancia master",
        choices=[questionary.Choice("0   - recomendada para fotometria", value=0),
                 questionary.Choice("25  - escenas oscuras", value=25),
                 questionary.Choice("50  - muy oscuras, mas ruido", value=50)],
        default=cfg.gain if cfg.gain in (0, 25, 50) else 0).ask()
    if gain is None:
        return None

    roi = questionary.select(
        "Tamano del ROI (se promedian los pixeles del parche)",
        choices=[questionary.Choice(f"{2 * r + 1}x{2 * r + 1} = {(2 * r + 1) ** 2} px",
                                    value=r) for r in (1, 3, 5, 10, 20)],
        default=cfg.roi_radius if cfg.roi_radius in (1, 3, 5, 10, 20) else 5).ask()
    if roi is None:
        return None

    avg = questionary.select(
        "Frames promediados por medida",
        choices=[questionary.Choice(f"{n} frame{'s' if n > 1 else ''}"
                                    + (f"   ruido /{n ** 0.5:.1f}" if n > 1 else ""),
                                    value=n) for n in (1, 4, 8, 16, 32)],
        default=cfg.average if cfg.average in (1, 4, 8, 16, 32) else 8).ask()
    if avg is None:
        return None

    return replace(cfg, binning=binning, exposure_ms=float(exp), gain=gain,
                   roi_radius=roi, average=avg)


def _es_float(v: str, lo: float, hi: float) -> bool:
    try:
        return lo <= float(v) <= hi
    except ValueError:
        return False


def menu(cfg: Config) -> int:
    """Menu interactivo con flechas. Es lo que corre `uv run camara.py`."""
    import questionary
    from rich.table import Table

    con = _console()
    dark = load_dark(cfg.dark_path)

    try:
        cam = open_camera(cfg)
        info = (f"{cam.model}  {cam.width}x{cam.height}  "
                f"clk {cam.pixel_clock} MHz  exp {cam.exposure_ms:.2f} ms")
        cam.close()
    except UC480Error as e:
        con.print(f"[yellow]sin camara:[/] {e}")
        if not questionary.confirm("Seguir en modo demo?", default=True).ask():
            return 1
        cfg = replace(cfg, demo=True)
        info = "modo demo, patron sintetico"

    _banner(con, info)
    if dark is not None:
        con.print(f"[green]dark frame cargado[/] de {cfg.dark_path} {dark.shape}")

    while True:
        accion = questionary.select(
            "Que queres hacer?",
            choices=[
                questionary.Choice("Visor en vivo         medir con el mouse", "visor"),
                questionary.Choice("Capturar dark frame   con la tapa puesta", "dark"),
                questionary.Choice("Ajustar parametros    binning, exposicion, ROI", "cfg"),
                questionary.Choice("Diagnostico USB       tasa de error por pixel clock", "usb"),
                questionary.Choice("Salir", "salir"),
            ]).ask()

        if accion in (None, "salir"):
            return 0

        if accion == "cfg":
            nuevo = configurar(cfg, con)
            if nuevo:
                cfg = nuevo
                con.print(f"[green]listo:[/] bin {cfg.binning}x, exp {cfg.exposure_ms} ms, "
                          f"gain {cfg.gain}, ROI {2 * cfg.roi_radius + 1}px, "
                          f"avg {cfg.average}x")
            continue

        if accion == "usb":
            with con.status("midiendo el enlace USB..."):
                filas = benchmark(cfg)
            t = Table(title="Tasa de error por pixel clock", header_style="bold cyan")
            for c in ("MHz", "fps max", "ok", "err", "tasa", "fps efectivos", "MB/s"):
                t.add_column(c, justify="right")
            for r in filas:
                color = "green" if r["rate"] < 0.05 else "yellow" if r["rate"] < 0.3 else "red"
                t.add_row(str(r["clk"]), f"{r['fps_max']:.1f}", str(r["ok"]),
                          str(r["err"]), f"[{color}]{r['rate'] * 100:.0f}%[/]",
                          f"{r['fps_ef']:.1f}", f"{r['mbs']:.1f}")
            con.print(t)
            con.print("[dim]un enlace sano deberia dar ~0% en todas las filas[/]")
            continue

        try:
            with open_camera(cfg) as cam:
                if accion == "dark":
                    con.print("[yellow]tapa la camara[/] y no la muevas.")
                    if not questionary.confirm("Lista?", default=True).ask():
                        continue
                    with con.status("capturando dark frame...") as st:
                        d = capture_dark(cam, 32, lambda i, n: st.update(
                            f"capturando dark frame  {i}/{n}"))
                    np.save(cfg.dark_path, d)
                    dark = d
                    canales = d.reshape(-1, d.shape[-1]).mean(axis=0) if d.ndim == 3 else [d.mean()]
                    con.print(f"[green]guardado[/] en {cfg.dark_path}  "
                              f"piso por canal (BGR): "
                              + "  ".join(f"{v:.2f}" for v in canales))
                elif accion == "visor":
                    run_viewer(cam, cfg, dark)
        except UC480Error as e:
            con.print(f"[red]error:[/] {e}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Visor y fotometro RGB para camaras Thorlabs DCx e IDS uEye. "
                    "Sin argumentos abre la interfaz grafica.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    d = Config()
    p.add_argument("-e", "--exposure-ms", type=float, default=d.exposure_ms)
    p.add_argument("-g", "--gain", type=int, default=d.gain,
                   help="ganancia master 0..100; para fotometria conviene 0")
    p.add_argument("-b", "--binning", type=int, default=d.binning,
                   choices=sorted(BINNING_MODES),
                   help="binning vertical por hardware; sube el SNR")
    p.add_argument("--driver", default=d.driver, choices=["auto", *sorted(DRIVERS)],
                   help="con que driver abrir; auto prueba los dos")
    p.add_argument("--camera-id", type=int, default=d.camera_id,
                   help="id de la camara; 0 toma la primera libre")
    p.add_argument("-c", "--pixel-clock", type=int, default=d.pixel_clock,
                   help="MHz, segun la camara; 0 usa el valor por defecto del driver")
    p.add_argument("-f", "--fps", type=float, default=d.fps, help="0 pide el maximo")
    p.add_argument("--retries", type=int, default=d.retries,
                   help="reintentos ante error de transferencia USB")
    p.add_argument("-R", "--roi-radius", type=int, default=d.roi_radius,
                   help="radio del parche; se promedian (2r+1)^2 pixeles")
    p.add_argument("-d", "--decimals", type=int, default=d.decimals)
    p.add_argument("-a", "--average", type=int, default=d.average,
                   help="frames promediados por medida")
    p.add_argument("--mono", action="store_true", help="modo monocromo de 8 bits")
    p.add_argument("--raw", action="store_true", help="Bayer sin interpolar")
    p.add_argument("--normalize", action="store_true", help="reporta 0..1")
    p.add_argument("--sat-level", type=int, default=d.sat_level)
    p.add_argument("--csv-path", type=Path, default=d.csv_path)
    p.add_argument("--dark-path", type=Path, default=d.dark_path)
    p.add_argument("--use-dark", action="store_true",
                   help="resta el dark frame guardado")
    p.add_argument("--scale", type=float, default=d.scale, help="escala de la ventana")
    p.add_argument("--demo", action="store_true", help="patron sintetico, sin hardware")
    p.add_argument("--gui", action="store_true",
                   help="interfaz grafica; es lo que abre sin argumentos")
    p.add_argument("--menu", action="store_true", help="menu de texto en la terminal")
    p.add_argument("--viewer", action="store_true",
                   help="visor de OpenCV por linea de comandos, sin la GUI")
    p.add_argument("--self-test", action="store_true", help="corre los tests y sale")
    p.add_argument("--benchmark", action="store_true",
                   help="mide la tasa de error USB y sale")
    p.add_argument("--detectar", action="store_true",
                   help="dice que camaras hay conectadas y de que tipo, y sale")
    p.add_argument("--instalar-drivers", action="store_true",
                   help="instala los drivers USB incluidos (pide administrador) y sale")
    return p


MODE_FLAGS = {"self_test", "benchmark", "viewer", "gui", "menu", "detectar",
              "instalar_drivers"}


def config_from_args(args: argparse.Namespace) -> Config:
    return Config(**{k: v for k, v in vars(args).items() if k not in MODE_FLAGS})


def _enganchar_consola() -> None:
    """Reengancha stdout/stderr a la consola que lanzo el ejecutable, si la hubo.

    El .exe se compila sin consola para que abrir la GUI no muestre una
    terminal negra al lado. Pero si alguien lo corre con argumentos desde
    PowerShell, --self-test y --benchmark tienen que poder escribir ahi.
    """
    if not getattr(sys, "frozen", False):
        return
    if sys.platform == "win32":
        try:
            if C.windll.kernel32.AttachConsole(-1):   # -1 = consola del padre
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
                sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
                sys.stdin = open("CONIN$", "r", encoding="utf-8")
        except OSError:
            pass
    # Sin consola, en un build windowed, sys.stdout queda en None y pytest
    # revienta al primer write.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        _enganchar_consola()
    args = build_parser().parse_args(argv)

    if args.self_test:
        import pytest
        # Empaquetado con PyInstaller, __file__ apunta a una ruta que no existe
        # en disco (el .py no se descomprime, solo el bytecode); en ese caso el
        # build agrega el script como dato y queda extraido en sys._MEIPASS.
        self_path = (str(Path(sys._MEIPASS) / "camara.py")
                     if getattr(sys, "frozen", False) else __file__)
        return pytest.main(["-q", "--no-header", self_path])

    if args.instalar_drivers:
        try:
            resultados = instalar_drivers()
        except UC480Error as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(resumir_instalacion(resultados))
        return 0 if any(rc in PNPUTIL_OK or rc == PNPUTIL_REINICIO
                        for rc in resultados.values()) else 1

    cfg = config_from_args(args)
    if args.detectar:
        try:
            camaras = detectar_camaras(cfg.driver)
        except UC480Error as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        for c in camaras:
            print(describir_camara(c))
        if not camaras:
            print("ninguna camara Thorlabs ni uEye responde. " + diagnostico_usb())
        return 0 if camaras else 1
    # Sin argumentos abre la interfaz grafica: es lo que ve quien hace doble
    # click en el .exe. El menu de texto queda detras de --menu y el visor de
    # OpenCV detras de --viewer.
    if args.gui or not (args.menu or args.viewer or args.benchmark):
        return run_gui(cfg)
    if args.menu:
        return menu(cfg)

    try:
        if args.benchmark:
            for r in benchmark(cfg):
                print(f"  {r['clk']:3d} MHz  err {r['rate'] * 100:5.1f}%  "
                      f"{r['fps_ef']:5.2f} fps  {r['mbs']:5.1f} MB/s")
            return 0
        cam = open_camera(cfg)
    except UC480Error as e:
        print(f"error: {e}", file=sys.stderr)
        print("proba 'uv run camara.py --demo' para trabajar sin la camara",
              file=sys.stderr)
        return 1

    with cam:
        print(f"{cam.marca} {cam.model}  {cam.width}x{cam.height}  exp {cam.exposure_ms:.2f} ms  "
              f"gain {cfg.gain}  bin {cfg.binning}x")
        run_viewer(cam, cfg, load_dark(cfg.dark_path) if cfg.use_dark else None)
    return 0


# --- tests ---------------------------------------------------------------
# Corren sin camara: `uv run camara.py --self-test`

def _flat(value, w=32, h=16):
    return np.full((h, w, 3), value, dtype=np.uint8)


def test_parche_uniforme_da_la_media_exacta():
    s = sample_patch(_flat(100), 16, 8, radius=2)
    assert s.mean == (100.0, 100.0, 100.0)
    assert s.std == (0.0, 0.0, 0.0)
    assert s.n == 25


def test_orden_de_canales_es_rgb():
    img = np.zeros((8, 8, 3), dtype=np.uint8)
    img[:, :, 0], img[:, :, 1], img[:, :, 2] = 10, 20, 30
    assert sample_patch(img, 4, 4, radius=1).mean == (30.0, 20.0, 10.0)


def test_media_resuelve_por_debajo_de_un_lsb():
    img = _flat(10, w=4, h=1)
    img[0, 0, :] = 11
    assert sample_patch(img, 1, 0, radius=4).mean[0] == 10.25


def test_el_parche_se_recorta_en_los_bordes():
    assert sample_patch(_flat(50), 0, 0, radius=3).n == 16  # 4x4, no 7x7


def test_error_estandar_baja_con_la_cantidad_de_pixeles():
    img = np.random.default_rng(0).integers(90, 110, (40, 40, 3), dtype=np.uint8)
    chico, grande = sample_patch(img, 20, 20, 2), sample_patch(img, 20, 20, 10)
    assert grande.n > chico.n
    assert sum(grande.sem) < sum(chico.sem)


def test_deteccion_de_saturacion():
    img = _flat(200, w=4, h=4)
    img[0, 0, :] = 255
    assert sample_patch(img, 1, 1, radius=4).sat_frac == 1 / 16
    assert sample_patch(_flat(200), 5, 5, radius=1).sat_frac == 0.0


def test_posicion_fuera_de_imagen_falla():
    import pytest
    with pytest.raises(ValueError):
        sample_patch(_flat(10), 999, 0, radius=1)


def test_formato_respeta_los_decimales():
    s = sample_patch(_flat(128), 4, 4, radius=1)
    assert "128.000" in "".join(format_sample(s, decimals=3, normalize=False))
    assert "0.50" in "".join(format_sample(s, decimals=2, normalize=True))


def test_dark_se_resta_y_recorta_en_cero():
    frame = _flat(30).astype(np.float64)
    dark = _flat(20).astype(np.float64)
    assert apply_dark(frame, dark).max() == 10.0
    assert apply_dark(dark, frame).min() == 0.0            # nunca negativo
    assert apply_dark(frame, None) is frame                # sin dark, sin cambio
    assert apply_dark(frame, _flat(20, w=8)).max() == 30.0  # forma distinta, ignora


def test_save_frame_as_npy_conserva_los_decimales(tmp_path):
    frame = np.full((3, 3, 3), 10.0)
    frame[0, 0, 0] = 10.25  # valor no representable por un uint8
    guardado = np.load(save_frame_as(frame, tmp_path / "a.npy"))
    assert guardado.dtype == np.float64
    assert guardado[0, 0, 0] == 10.25


def test_save_frame_as_tiff_es_flotante_de_32_bits(tmp_path):
    import cv2
    frame = np.full((3, 3, 3), 10.0)
    frame[0, 0, 0] = 10.25
    leido = cv2.imread(str(save_frame_as(frame, tmp_path / "a.tiff")),
                       cv2.IMREAD_UNCHANGED)
    assert leido.dtype == np.float32
    assert abs(float(leido[0, 0, 0]) - 10.25) < 1e-3


def test_save_frame_as_png_redondea_a_entero(tmp_path):
    import cv2
    frame = np.full((3, 3, 3), 10.0)
    frame[0, 0, 0] = 10.75                       # se va a redondear a 11
    leido = cv2.imread(str(save_frame_as(frame, tmp_path / "a.png")),
                       cv2.IMREAD_UNCHANGED)
    assert leido.dtype == np.uint8
    assert leido[0, 0, 0] == 11


def test_save_frame_as_recorta_fuera_de_rango_en_formatos_enteros(tmp_path):
    import cv2
    frame = np.array([[[-20.0, 300.0, 128.0]]])   # por debajo de 0 y arriba de 255
    leido = cv2.imread(str(save_frame_as(frame, tmp_path / "a.png")),
                       cv2.IMREAD_UNCHANGED)
    assert list(leido[0, 0]) == [0, 255, 128]


def test_save_frame_as_rechaza_extensiones_desconocidas(tmp_path):
    import pytest
    for nombre in ("a.raw", "a"):
        with pytest.raises(ValueError, match="formato conocido"):
            save_frame_as(np.zeros((2, 2, 3)), tmp_path / nombre)


def test_perfiles_cortan_la_fila_y_la_columna_en_orden_rgb():
    img = np.zeros((6, 8, 3), dtype=np.float64)   # BGR, como entrega la camara
    img[2, :, 2] = 40.5      # fila 2, canal R
    img[:, 5, 0] = 10.25     # columna 5, canal B

    h = perfil_horizontal(img, 2)
    assert h.shape == (8, 3)                       # un punto por columna
    assert h[0, 0] == 40.5 and h[0, 2] == 0        # R cargado, B vacio
    assert h[5, 2] == 10.25                        # donde cruza la columna

    v = perfil_vertical(img, 5)
    assert v.shape == (6, 3)                       # un punto por fila
    assert v[0, 2] == 10.25 and v[2, 0] == 40.5


def test_remuestrear_acota_los_puntos_y_conserva_los_extremos():
    vals = np.arange(1000, dtype=np.float64).reshape(-1, 1)
    corto = _remuestrear(vals, 100)
    assert len(corto) == 100
    assert corto[0, 0] == 0 and corto[-1, 0] == 999
    assert _remuestrear(vals, 5000) is vals        # ya entra, no toca nada


def test_demo_declara_rango_de_exposicion():
    cam = DemoCamera(Config(demo=True))
    lo, hi = cam.exposure_range()
    assert lo < hi
    assert cam.set_exposure(hi * 10) == hi         # satura, no se pasa


def test_ppm_lleva_cabecera_y_los_bytes_de_la_imagen():
    ppm = _ppm(np.zeros((2, 3, 3), dtype=np.uint8))   # alto 2, ancho 3
    assert ppm.startswith(b"P6\n3 2\n255\n")
    assert len(ppm) == len(b"P6\n3 2\n255\n") + 2 * 3 * 3


def test_save_capture_conserva_floats_sin_cuantizar(tmp_path):
    frame = np.full((4, 4, 3), 10.0)
    frame[0, 0, 0] = 10.25  # valor no representable por un uint8
    npy_path, tiff_path = save_capture(frame, tmp_path)

    recargado = np.load(npy_path)
    assert recargado.dtype == np.float64
    assert recargado[0, 0, 0] == 10.25

    import cv2
    tiff = cv2.imread(str(tiff_path), cv2.IMREAD_UNCHANGED)
    assert tiff.dtype == np.float32
    assert abs(float(tiff[0, 0, 0]) - 10.25) < 1e-3


def test_capture_dark_promedia_los_frames():
    cam = DemoCamera(Config(demo=True))
    d = capture_dark(cam, 4)
    assert d.shape == (cam.height, cam.width, 3) and d.dtype == np.float64


def test_config_valida_sus_rangos():
    import pytest
    malos = [{"roi_radius": -1}, {"decimals": 9}, {"average": 0}, {"gain": 101},
             {"pixel_clock": 501}, {"pixel_clock": -1}, {"retries": -1},
             {"binning": 5}, {"binning": 0}, {"driver": "basler"}, {"camera_id": -1}]
    for kwargs in malos:
        with pytest.raises(ValueError):
            Config(**kwargs)
    assert Config(pixel_clock=0).pixel_clock == 0


def test_binning_reduce_el_alto_del_frame():
    for b in sorted(BINNING_MODES):
        cam = DemoCamera(Config(demo=True, binning=b))
        assert cam.height == cam.full_height // b
        assert cam.grab().shape[0] == cam.height


def test_layout_de_la_lista_de_camaras_coincide_con_el_sdk():
    """Si el struct no mide lo que dice uc480.h, is_GetCameraList lee corrido."""
    assert C.sizeof(UC480_CAMERA_INFO) == 112
    assert C.sizeof(_tipo_lista_camaras(3)) == 4 + 3 * C.sizeof(UC480_CAMERA_INFO)


def test_info_camaras_traduce_la_lista_del_sdk():
    entradas = (UC480_CAMERA_INFO * 2)()
    entradas[0].dwCameraID, entradas[0].dwDeviceID = 1, 0
    entradas[0].FullModelName, entradas[0].SerNo = b"DCU224C", b"4002851234"
    entradas[1].dwCameraID, entradas[1].dwInUse = 2, 1
    entradas[1].Model = b"DCC1545M"          # sin FullModelName, cae al corto

    info = _info_camaras(entradas)
    assert info[0] == {"id": 1, "device": 0, "modelo": "DCU224C",
                       "serie": "4002851234", "en_uso": False, "driver": "uc480",
                       "fabricante": "", "marca": MARCA_THORLABS}
    assert (info[1]["modelo"], info[1]["en_uso"]) == ("DCC1545M", True)


def test_detectar_camaras_devuelve_lista_o_explica_por_que_no():
    """Con SDK enumera; sin SDK o fuera de Windows, el error dice el motivo."""
    try:
        camaras = detectar_camaras()
    except UC480Error as e:
        assert "uc480" in str(e).lower() or "Windows" in str(e)
    else:
        assert all({"id", "modelo", "serie", "en_uso", "driver", "marca"} <= set(c)
                   for c in camaras)


def test_la_marca_sale_de_lo_que_dice_la_camara_no_del_driver():
    # Caso real: la uEye de 2008 enlazada al driver de Thorlabs.
    assert clasificar_marca("IDS GmbH", "C1280R12C", "uc480") == MARCA_IDS
    assert clasificar_marca("Thorlabs GmbH", "D1280G12C", "uc480") == MARCA_THORLABS
    # Sin fabricante (camara en uso), solo los prefijos comerciales dicen algo.
    assert clasificar_marca("", "DCU224C", "uc480") == MARCA_THORLABS
    assert clasificar_marca("", "UI-1540SE-C", "uc480") == MARCA_IDS
    # Un codigo de sensor por uc480 no prueba nada: no se inventa la marca.
    assert clasificar_marca("", "C1280R12C", "uc480") == MARCA_DESCONOCIDA
    assert clasificar_marca("", "C1280R12C", "ueye") == MARCA_IDS


def test_elegir_camara_respeta_driver_id_y_en_uso():
    uc = {"id": 1, "driver": "uc480", "en_uso": True}
    ue = {"id": 2, "driver": "ueye", "en_uso": False}
    camaras = [uc, ue]
    assert elegir_camara(camaras, Config()) is ue                  # la primera libre
    assert elegir_camara(camaras, Config(driver="uc480")) is None  # esa esta ocupada
    assert elegir_camara(camaras, Config(camera_id=2)) is ue
    assert elegir_camara(camaras, Config(camera_id=9)) is None
    assert elegir_camara([], Config()) is None


def test_open_camera_usa_la_clase_del_driver_detectado(monkeypatch):
    abiertas = []
    for driver in DRIVERS:
        monkeypatch.setitem(CLASES_CAMARA, driver,
                            lambda cfg, info, d=driver: abiertas.append((d, info["id"])))
    monkeypatch.setitem(globals(), "detectar_camaras",
                        lambda _d: [{"id": 7, "driver": "ueye", "en_uso": False}])
    open_camera(Config())
    assert abiertas == [("ueye", 7)]


def test_sin_camara_el_error_explica_que_ve_windows(monkeypatch):
    import pytest
    monkeypatch.setitem(globals(), "detectar_camaras", lambda _d: [])
    monkeypatch.setitem(globals(), "diagnostico_usb", lambda: "PISTA USB")
    with pytest.raises(UC480Error, match="PISTA USB"):
        open_camera(Config())


def test_interpretar_pnp_distingue_arranque_fallido_de_ausencia():
    assert "codigo 43" in interpretar_pnp([r"USB\VID_0000&PID_0002\6&336BE19D&0&2"])
    assert "driver" in interpretar_pnp([r"USB\VID_1409&PID_1540\6&336BE19D&0&2"])
    assert "no ve ninguna" in interpretar_pnp([])


def test_las_dos_dlls_se_cargan_de_a_una(monkeypatch):
    """Juntas en un proceso, la segunda falla con WinError 1114."""
    import pytest
    if sys.platform != "win32":
        pytest.skip("solo Windows")
    cargadas = []
    monkeypatch.setitem(globals(), "_liberar_dll", lambda: cargadas.append("libera"))
    monkeypatch.setitem(globals(), "_dll_activa", ("uc480", object()))
    monkeypatch.setitem(globals(), "_camaras_abiertas", 1)
    with pytest.raises(UC480Error, match="cierrala"):      # con camara abierta, no
        _cargar_dll("ueye")
    assert cargadas == []
    monkeypatch.setitem(globals(), "_camaras_abiertas", 0)
    falsa = C.WinDLL
    monkeypatch.setattr(C, "WinDLL", lambda n: cargadas.append(n) or object())
    try:
        _cargar_dll("ueye")
    finally:
        C.WinDLL = falsa
    assert cargadas == ["libera", "uEye_api_64"]


def test_sin_dll_en_el_sistema_usa_la_embebida(monkeypatch):
    import pytest
    if sys.platform != "win32":
        pytest.skip("solo Windows")
    cargadas = []

    def falsa(nombre):
        cargadas.append(nombre)
        if nombre == "uc480_64":
            raise OSError("no esta en el sistema")
        return object()

    monkeypatch.setitem(globals(), "_dll_activa", None)
    monkeypatch.setitem(globals(), "_camaras_abiertas", 0)
    monkeypatch.setattr(C, "WinDLL", falsa)
    _cargar_dll("uc480")
    # La del sistema se prueba antes: es la que coincide con el driver instalado.
    assert cargadas[0] == "uc480_64"
    assert Path(cargadas[1]) == _dir_drivers() / "DCx_USB_Driver_Package" / "uc480_64.dll"


def test_sin_dll_ni_copia_embebida_dice_que_falta(monkeypatch, tmp_path):
    import pytest
    if sys.platform != "win32":
        pytest.skip("solo Windows")

    def falsa(nombre):
        raise OSError("no esta")

    monkeypatch.setitem(globals(), "_dll_activa", None)
    monkeypatch.setitem(globals(), "_camaras_abiertas", 0)
    monkeypatch.setitem(globals(), "_dir_drivers", lambda: tmp_path)
    monkeypatch.setattr(C, "WinDLL", falsa)
    with pytest.raises(UC480Error, match="falta uc480_64.dll"):
        _cargar_dll("uc480")


def test_los_paquetes_de_driver_estan_completos():
    """Lo que hace falta sin nada instalado: la DLL, el .inf, su firma y los .sys."""
    uc480 = _dir_drivers() / "DCx_USB_Driver_Package"
    for nombre in ("uc480_64.dll", "uc480_64.inf", "uc480_64.cat",
                   "drivers/uc480_64.sys", "drivers/uc480_boot_64.sys"):
        assert (uc480 / nombre).is_file(), nombre
    ueye = _dir_drivers() / "uEye_USB_Driver_Package"
    for nombre in ("ueye_api_64.dll", "ueye_64.inf", "ueye_64.cat",
                   "drivers/uEye_usb_64.sys", "drivers/ueye_boot_64.sys"):
        assert (ueye / nombre).is_file(), nombre


def test_script_de_instalacion_escapa_rutas_y_anota_cada_driver(tmp_path):
    infs = {"uc480": Path("C:/Mi usuario/d'Arc/a.inf"), "ueye": Path("C:/b.inf")}
    script = script_instalacion(infs, tmp_path / "r.txt")
    assert "d''Arc" in script                   # la comilla no corta la cadena
    assert script.count("/add-driver") == 2
    assert '$r += "uc480=$LASTEXITCODE"' in script
    orden = orden_elevada(script)
    assert "-Verb RunAs" in orden
    codificado = orden.split("'-EncodedCommand','")[1].rstrip("'")
    assert base64.b64decode(codificado).decode("utf-16-le") == script


def test_resultados_de_instalacion_se_interpretan():
    assert leer_resultados("uc480=259\r\nueye=-5\r\nbasura\r\n") == {"uc480": 259, "ueye": -5}
    # Basta con un driver: ambos sirven para las dos marcas.
    assert "Conecta la camara" in resumir_instalacion({"uc480": 259, "ueye": 5})
    assert "fallo (codigo 5)" in resumir_instalacion({"uc480": 259, "ueye": 5})
    assert "Reinicia" in resumir_instalacion({"uc480": 3010})
    assert "No se instalo" in resumir_instalacion({"uc480": 5, "ueye": 5})


def test_opciones_de_mascara_salen_de_lo_que_declara_la_camara():
    assert opciones_de_mascara(0, FACTORES_V) == ("1x",)            # la C1280R12C
    assert opciones_de_mascara(0x0015, FACTORES_V) == ("1x", "2x", "3x", "4x")  # DCU224C
    assert opciones_de_mascara(0x0015, FACTORES_H) == ("1x",)
    assert opciones_de_mascara(0x0c0f, FACTORES_H) == ("1x", "2x", "4x", "8x")  # C1280R12C
    assert factor_de_opcion("8x") == 8 and factor_de_opcion(2) == 2


def test_canales_recortados_detecta_el_parche_entero_en_cero():
    img = _flat(0)
    img[:, :, 1] = 6                 # BGR: lo que entrega la C1280R12C a oscuras
    assert canales_recortados(sample_patch(img, 4, 4, 2)) == ["R", "B"]
    assert canales_recortados(sample_patch(_flat(3), 4, 4, 2)) == []
    # Un solo pixel con señal basta: ya no esta todo por debajo del ADC.
    img[4, 4, 2] = 5
    assert canales_recortados(sample_patch(img, 4, 4, 2)) == ["B"]


def test_tabla_de_ajustes_cubre_las_siete_pestanas_de_thorcam():
    tabla = DemoCamera(Config(demo=True)).ajustes()
    assert {a.pestana for a in tabla.values()} == set(PESTANAS) and len(PESTANAS) == 7
    assert all(a.activo for a in tabla.values())       # la demo declara todo


def test_lo_que_la_camara_no_declara_sale_desactivado():
    # Las capacidades medidas sobre la uEye C1280R12C.
    cap = {"master": False, "rgb": True, "color": True, "bin_mask": 0, "sub_mask": 0x0c0f,
           "features": 0, "gpios": 0, "flash": True, "flash_delay": (40, 4000000, 1),
           "flash_dur": (40, 4000000, 1), "trigger_delay": (15, 4000000, 1),
           "borde": (0, 9, 1), "reloj": (5, 43, 1), "fps": (4.9, 14.0, 0.01),
           "exposicion": (0.07, 99.9, 0.07)}
    tabla = construir_ajustes(cap, dict(DEFECTOS))
    for apagado in ("ganancia", "bin_h", "bin_v", "obturador", "log_modo", "gpio1", "pwm_hz"):
        assert not tabla[apagado].activo, apagado
    for prendido in ("pixel_clock", "ganancia_r", "sub_h", "flash_modo", "trigger_modo", "borde"):
        assert tabla[prendido].activo, prendido


def test_ajuste_no_soportado_se_rechaza_sin_tocar_el_valor():
    import pytest
    cam = DemoCamera(Config(demo=True))
    cam.cap["master"] = False
    with pytest.raises(UC480Error, match="no admite"):
        cam.aplicar_ajuste("ganancia", 50)
    assert cam.valores["ganancia"] == 0
    with pytest.raises(ValueError, match="desconocido"):
        cam.aplicar_ajuste("no_existe", 1)


def test_valores_fuera_de_rango_saturan_y_opciones_invalidas_fallan():
    import pytest
    cam = DemoCamera(Config(demo=True))
    assert cam.aplicar_ajuste("ganancia", 500) == 100
    assert cam.aplicar_ajuste("gamma", 9.0) == 2.2
    with pytest.raises(ValueError):
        cam.aplicar_ajuste("trigger_modo", "telepatia")


def test_submuestreo_cambia_el_tamano_real_del_frame():
    cam = DemoCamera(Config(demo=True))
    cam.aplicar_ajuste("sub_h", "2x")
    cam.aplicar_ajuste("sub_v", "4x")
    assert cam.grab().shape[:2] == (480 // 4, 640 // 2)
    cam.restablecer()
    assert cam.grab().shape[:2] == (480, 640)


def test_espejo_invierte_la_imagen():
    cam = DemoCamera(Config(demo=True))
    normal = cam.grab().astype(np.float64)
    cam.aplicar_ajuste("espejo_lr", True)
    cam._t = 0
    espejada = cam.grab().astype(np.float64)
    cam2 = DemoCamera(Config(demo=True))
    assert normal.shape == espejada.shape
    assert np.abs(espejada[:, ::-1] - cam2.grab()).mean() < 1e-9


def test_combinar_saturacion_acopla_u_y_v():
    cam = DemoCamera(Config(demo=True))
    cam.aplicar_ajuste("sat_u", 150)
    assert cam.valores["sat_v"] == 150
    cam.aplicar_ajuste("sat_combinar", False)
    cam.aplicar_ajuste("sat_u", 80)
    assert cam.valores["sat_v"] == 150


def test_avisa_solo_lo_que_rompe_la_linealidad():
    """Invariante de la fotometria: multiplicar por una constante sigue siendo lineal."""
    base = dict(DEFECTOS)
    assert avisos_no_lineales(base) == []
    assert avisos_no_lineales({**base, "ganancia": 50, "ganancia_r": 80, "exposicion": 5}) == []
    for clave, valor in (("gamma", 2.2), ("exp_auto", True), ("awb", True), ("borde", 3),
                         ("sat_u", 150), ("gain_boost", True), ("negro_auto", True)):
        assert avisos_no_lineales({**base, clave: valor}), clave


def test_restablecer_vuelve_a_la_cadena_lineal():
    cam = DemoCamera(Config(demo=True))
    for clave, valor in (("gamma", 2.0), ("awb", True), ("borde", 4), ("exp_auto", True)):
        cam.aplicar_ajuste(clave, valor)
    assert avisos_no_lineales(cam.valores)
    assert cam.restablecer() == []
    assert avisos_no_lineales(cam.valores) == []


def test_perfil_ida_y_vuelta(tmp_path):
    cam = DemoCamera(Config(demo=True))
    cam.aplicar_ajuste("ganancia_r", 42)
    cam.aplicar_ajuste("trigger_modo", "software")
    cam.aplicar_ajuste("sub_h", "2x")
    ruta = guardar_perfil(cam, tmp_path / "p.json")
    otra = DemoCamera(Config(demo=True))
    assert otra.cargar_valores(leer_perfil(ruta)) == []
    assert otra.valores["ganancia_r"] == 42
    assert otra.valores["trigger_modo"] == "software"
    assert otra.width == cam.width


def test_perfil_de_otra_camara_salta_lo_que_esta_no_tiene(tmp_path):
    cam = DemoCamera(Config(demo=True))
    cam.aplicar_ajuste("ganancia", 30)
    ruta = guardar_perfil(cam, tmp_path / "p.json")
    otra = DemoCamera(Config(demo=True))
    otra.cap["master"] = False
    assert otra.cargar_valores(leer_perfil(ruta)) == []
    assert otra.valores["ganancia"] == 0


def test_leer_perfil_rechaza_lo_que_no_es_un_perfil(tmp_path):
    import pytest
    malo = tmp_path / "x.json"
    malo.write_text("[1, 2]")
    with pytest.raises(ValueError, match="no es un perfil"):
        leer_perfil(malo)


def test_cola_aplica_el_ultimo_pedido_de_cada_clave_y_reporta():
    cam = DemoCamera(Config(demo=True))
    cola = ColaAjustes()
    for g in (10, 20, 30):
        cola.pedir("ganancia", g)
    cola.pedir("trigger_modo", "telepatia")
    assert cola.drenar(cam) is True
    hechos = dict(cola.hechos())
    assert hechos["ganancia"] == 30 and cam.valores["ganancia"] == 30
    assert isinstance(hechos["trigger_modo"], ValueError)
    assert cola.drenar(cam) is False and cola.hechos() == []


def test_cola_restablece_desde_el_hilo_de_captura():
    cam = DemoCamera(Config(demo=True))
    cam.aplicar_ajuste("gamma", 2.0)
    cola = ColaAjustes()
    cola.pedir(ColaAjustes.RESTABLECER)
    cola.drenar(cam)
    assert cam.valores["gamma"] == 1.0
    assert cola.hechos() == [(ColaAjustes.RESTABLECER, [])]


def test_structs_coinciden_con_el_sdk():
    assert C.sizeof(BOARDINFO) == 64
    assert C.sizeof(IS_RECT) == 16
    assert C.sizeof(IO_FLASH_PARAMS) == 8
    assert C.sizeof(IO_PWM_PARAMS) == 16
    assert C.sizeof(IO_GPIO_CONFIGURATION) == 64


def test_el_modulo_no_depende_de_windows():
    """El analisis, el modo demo y los tests corren en macOS y Linux.

    Los tipos se declaran a mano, asi que tienen que coincidir con el ABI de
    Windows o SENSORINFO se leeria desalineado.
    """
    assert (C.sizeof(WORD), C.sizeof(DWORD), C.sizeof(BOOL)) == (2, 4, 4)


def test_captura_falla_con_mensaje_claro_fuera_de_windows():
    import pytest
    if sys.platform == "win32":
        pytest.skip("solo aplica fuera de Windows")
    with pytest.raises(UC480Error, match="solo funciona en Windows"):
        UC480Camera(Config())


def test_cli_mapea_a_config():
    args = build_parser().parse_args(["-e", "5", "-R", "7", "--normalize"])
    cfg = config_from_args(args)
    assert (cfg.exposure_ms, cfg.roi_radius, cfg.normalize) == (5.0, 7, True)


def test_cli_expone_binning_y_parametros_de_usb():
    args = build_parser().parse_args(["-b", "4", "-c", "24", "--retries", "2"])
    cfg = config_from_args(args)
    assert (cfg.binning, cfg.pixel_clock, cfg.retries) == (4, 24, 2)


def test_los_flags_de_modo_no_llegan_a_config():
    args = build_parser().parse_args(["--viewer", "--benchmark", "--gui", "--menu", "--detectar"])
    cfg = config_from_args(args)              # no debe lanzar TypeError
    assert not any(hasattr(cfg, f) for f in MODE_FLAGS)


if __name__ == "__main__":
    raise SystemExit(main())
