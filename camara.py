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
"""Visor y fotometro RGB para camaras Thorlabs DCx.

Software de camara: visor en vivo, lectura RGB con decimales, perfiles de
intensidad, dark frame y guardado de imagenes. Probado sobre una DCU224C
(1280x1024, CCD color, 4.65 um/px, 8 bits).

La cadena de imagen se fuerza a respuesta lineal: gamma 1.0, sin auto-ganancia,
auto-exposicion ni balance de blancos, y sin correccion de color. Cualquiera de
esas etapas rompe la proporcionalidad entre señal e intensidad incidente, que es
lo que hace cuantitativa a una medida de fotometria.

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
import csv
import ctypes as C
import os
import sys
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

# Constantes de uc480.h v4.20, del SDK "DCx Camera Support".
IS_SUCCESS = 0
IS_WAIT = 0x0001
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

# is_SetAutoParameter: todos van a 0.0 para dejar el sensor en manual.
AUTO_PARAMS = (0x8800, 0x8802, 0x8804, 0x8806)  # ganancia, shutter, WB, framerate

# Solo binning vertical: es lo unico que declara la DCU224C (mascara 0x0015).
BINNING_MODES = {1: 0x00, 2: 0x0001, 3: 0x0010, 4: 0x0004}

# SNR medido sobre una DCU224C a 20 ms, relativo a sin binning.
BINNING_SNR = {1: 1.00, 2: 1.62, 3: 2.22, 4: 2.34}

PIXEL_CLOCKS = (12, 16, 20, 24, 30)  # MHz, los que barre el diagnostico USB


class UC480Error(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    exposure_ms: float = 20.0
    gain: int = 0                 # ganancia master del hardware, 0..100
    binning: int = 1              # 1 = sin binning; 2, 3 o 4 = vertical
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
        if self.pixel_clock and not 5 <= self.pixel_clock <= 30:
            raise ValueError("pixel_clock debe estar entre 5 y 30 MHz, o 0")
        if self.retries < 0:
            raise ValueError("retries debe ser >= 0")


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
    def close(self) -> None: ...


def _cargar_dll():
    """Carga uc480_64.dll. La comparten la deteccion y la captura."""
    if sys.platform != "win32":
        raise UC480Error(
            f"la captura solo funciona en Windows (aca: {sys.platform}). "
            "Thorlabs publica el SDK DCx para Windows y Linux/amd64 unicamente; "
            "no hay build para macOS. Usa el modo demo para trabajar sin camara.")
    try:
        return C.WinDLL("uc480_64" if sys.maxsize > 2 ** 32 else "uc480")
    except OSError as e:
        raise UC480Error("falta uc480_64.dll; instala 'DCx Camera Support'") from e


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


def _info_camaras(entradas) -> list[dict]:
    """Traduce las entradas del SDK a dicts, separado para poder testearlo."""
    return [{"id": int(c.dwCameraID), "device": int(c.dwDeviceID),
             "modelo": (c.FullModelName or c.Model).decode(errors="replace").strip(),
             "serie": c.SerNo.decode(errors="replace").strip(),
             "en_uso": bool(c.dwInUse)}
            for c in entradas]


def detectar_camaras() -> list[dict]:
    """Enumera las camaras DCx conectadas sin abrirlas.

    Sirve para enganchar una camara que se conecto despues de arrancar el
    programa, sin tener que reiniciarlo. Devuelve lista vacia si el SDK esta
    instalado pero no hay ninguna camara enchufada; si falta el SDK o no es
    Windows, levanta UC480Error con el motivo.
    """
    dll = _cargar_dll()
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
    return _info_camaras(lista.uci[:n.value])


class UC480Camera:
    """Acceso por ctypes a uc480_64.dll."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.transfer_errors = 0
        self.dll = _cargar_dll()

        self.h = C.c_uint32(0)
        self._check(self.dll.is_InitCamera(C.byref(self.h), None), "is_InitCamera")

        info = self._sensor_info()
        self.width = int(info.nMaxWidth)
        self.full_height = int(info.nMaxHeight)
        self.model = info.strSensorName.decode(errors="replace")
        self.pixel_um = info.wPixelSize / 100.0

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

        # El binning suma carga dentro del CCD, antes del ADC, y reduce el alto.
        # Hay que fijarlo antes de reservar memoria o el buffer queda mal medido.
        self._check(self.dll.is_SetBinning(self.h, BINNING_MODES[cfg.binning]),
                    "is_SetBinning")
        self.height = self.full_height // cfg.binning

        self.mem, self.mem_id = C.c_char_p(), C.c_int()
        self._check(self.dll.is_AllocImageMem(self.h, self.width, self.height, bpp,
                                              C.byref(self.mem), C.byref(self.mem_id)),
                    "is_AllocImageMem")
        self._check(self.dll.is_SetImageMem(self.h, self.mem, self.mem_id), "is_SetImageMem")
        pitch = C.c_int()
        self._check(self.dll.is_GetImageMemPitch(self.h, C.byref(pitch)), "is_GetImageMemPitch")
        self.pitch = int(pitch.value)

        # El orden importa: cada paso reajusta el rango valido del siguiente.
        if cfg.pixel_clock:
            self.set_pixel_clock(cfg.pixel_clock)
        self.pixel_clock = self.get_pixel_clock()
        self.fps = self.set_fps(cfg.fps)
        self.exposure_ms = self.set_exposure(cfg.exposure_ms)
        self.dll.is_SetTimeout(self.h, IS_TRIGGER_TIMEOUT, 3000)

    def _check(self, rc: int, what: str) -> None:
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
        if getattr(self, "h", None) and self.h.value:
            self.dll.is_FreeImageMem(self.h, self.mem, self.mem_id)
            self.dll.is_ExitCamera(self.h)
            self.h = C.c_uint32(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class DemoCamera:
    """Patron sintetico con franjas cos^2, para trabajar en la UI sin camara."""

    model, pixel_clock, fps = "DEMO", 0, 30.0

    def __init__(self, cfg: Config):
        self.cfg, self.exposure_ms, self._t = cfg, cfg.exposure_ms, 0
        self.width, self.full_height = 640, 480
        self.height = self.full_height // cfg.binning
        self.transfer_errors = 0

    def grab(self) -> np.ndarray:
        self._t += 1
        _, xx = np.mgrid[0:self.height, 0:self.width]
        base = 120 * np.cos(np.deg2rad(self._t * 2) + xx / 180.0) ** 2 + 20
        img = np.stack([base * 0.9, base, base * 0.75], axis=2)
        ruido = np.random.default_rng(self._t).normal(0, 1.5, img.shape)
        img = img * (self.exposure_ms / 20.0) * self.cfg.binning + ruido
        return np.clip(img, 0, 255).astype(np.uint8)

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
    return DemoCamera(cfg) if cfg.demo else UC480Camera(cfg)


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
    with UC480Camera(cfg) as cam:
        mb = cam.width * cam.height * cam.channels / 1e6
        for clk in PIXEL_CLOCKS:
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


def _remuestrear(vals: np.ndarray, n: int) -> np.ndarray:
    """Baja la cantidad de puntos a n para dibujarlos en un canvas angosto.

    Submuestrea en vez de promediar a proposito: un promedio esconderia los
    picos angostos, que son justo lo que uno busca en un perfil de intensidad.
    """
    if n <= 0 or len(vals) <= n:
        return vals
    return vals[np.linspace(0, len(vals) - 1, n).astype(int)]


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
        # La exposicion se aplica desde el hilo de captura, entre frames, para
        # no tocar el DLL mientras is_FreezeVideo esta bloqueado en el otro.
        # exp_aplicada es lo que devolvio la camara, que redondea al reloj real.
        self.exp_pendiente: float | None = None
        self.exp_aplicada: float | None = None
        self._ajustando = False     # evita realimentar el callback de la barra

        root.title("Camara - fotometria RGB para Thorlabs DCx")
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
        self.btn_detectar = ttk.Button(f, text="Detectar camara ThorCam",
                                       style="Neon.TButton", command=self.detectar)
        self.btn_detectar.pack(fill="x", pady=(6, 0))

        f = ttk.LabelFrame(izq, text="Captura", padding=6)
        f.pack(fill="x", pady=(6, 0))
        self.var_exp = tk.StringVar(value=f"{self.cfg.exposure_ms:g}")
        self.var_gain = tk.IntVar(value=self.cfg.gain)
        self.var_bin = tk.IntVar(value=self.cfg.binning)
        self.var_avg = tk.IntVar(value=self.cfg.average)
        self.var_roi = tk.IntVar(value=self.cfg.roi_radius)
        campos = (
            ("Exposicion (ms)", ttk.Entry(f, textvariable=self.var_exp, width=8)),
            ("Ganancia (0..100)", ttk.Spinbox(f, from_=0, to=100, width=6,
                                              textvariable=self.var_gain)),
            ("Binning vertical", ttk.Spinbox(f, from_=min(BINNING_MODES),
                                             to=max(BINNING_MODES), width=6,
                                             textvariable=self.var_bin)),
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
        """Abre la camara segun self.cfg y refresca la ficha. Propaga UC480Error."""
        self.cam = open_camera(self.cfg)
        self.pos = (self.cam.width // 2, self.cam.height // 2)
        self.lbl_cam.config(
            text=("[DEMO] " if self.cfg.demo else "")
                 + f"{getattr(self.cam, 'model', '?')}  "
                   f"{self.cam.width}x{self.cam.height}\n"
                   f"exp {self.cam.exposure_ms:.2f} ms   "
                   f"clk {getattr(self.cam, 'pixel_clock', 0)} MHz   "
                   f"bin {self.cfg.binning}x",
            foreground=CYBER["neon2"] if self.cfg.demo else CYBER["texto"])
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
                    "Podes conectar la camara despues y usar 'Detectar camara ThorCam'."):
                self.lbl_cam.config(text="sin camara", foreground=CYBER["alerta"])
                return
            self.cfg = replace(self.cfg, demo=True)
            self._abrir_camara_real()

    def _cerrar_camara(self) -> None:
        if self.cam is not None:
            self.cam.close()
            self.cam = None

    def detectar(self) -> None:
        """Busca camaras DCx enchufadas y engancha la primera que este libre.

        Es la salida para el caso normal: se abrio el programa antes de
        prender la camara, o se cambio de puerto. Evita tener que reiniciar.
        """
        from tkinter import messagebox
        estaba = self.corriendo.is_set()
        self.detener()
        self.btn_detectar.config(state="disabled", text="buscando...")
        self.root.update_idletasks()
        try:
            camaras = detectar_camaras()
        except UC480Error as e:
            self.btn_detectar.config(state="normal", text="Detectar camara ThorCam")
            messagebox.showerror("Deteccion", str(e))
            if estaba:
                self.iniciar()
            return
        self.btn_detectar.config(state="normal", text="Detectar camara ThorCam")

        if not camaras:
            messagebox.showwarning(
                "Deteccion", "El SDK responde pero no hay ninguna camara conectada.\n\n"
                "Revisa el cable USB y que la camara este prendida.")
            if estaba:
                self.iniciar()
            return

        detalle = "\n".join(
            f"  - {c['modelo'] or '?'}  serie {c['serie'] or '?'}"
            f"{'  (en uso)' if c['en_uso'] else ''}" for c in camaras)
        libres = [c for c in camaras if not c["en_uso"]]
        if not libres and not self.cfg.demo:
            # Ya la tenemos abierta nosotros: no hay nada que reconectar.
            messagebox.showinfo("Deteccion", f"{len(camaras)} camara(s):\n{detalle}")
            if estaba:
                self.iniciar()
            return

        self._cerrar_camara()
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
        # El binning y la ganancia se fijan al abrir la camara y el binning
        # ademas cambia el alto del buffer, asi que hay que reabrirla.
        reabrir = (nueva.binning, nueva.gain) != (self.cfg.binning, self.cfg.gain)
        estaba = self.corriendo.is_set()
        self.detener()
        self.cfg = nueva
        if reabrir:
            self._cerrar_camara()
            self._abrir_camara()
        elif self.cam is not None:
            self.var_exp.set(f"{self.cam.set_exposure(nueva.exposure_ms):g}")
            self._rango_exposicion()
        with self.lock:
            self.frame = None
        if estaba:
            self.iniciar()

    # --- visor ---

    def iniciar(self) -> None:
        if self.cam is None or self.corriendo.is_set():
            return
        self.perdidos = self.totales = 0
        self.corriendo.set()
        self.hilo = threading.Thread(target=self._bucle, daemon=True)
        self.hilo.start()
        self._refrescar_estado()

    def detener(self) -> None:
        self.corriendo.clear()
        hilo, self.hilo = self.hilo, None
        if hilo is not None:
            hilo.join(timeout=5.0)
        self._refrescar_estado()

    def _bucle(self) -> None:
        while self.corriendo.is_set():
            cam, cfg = self.cam, self.cfg
            if cam is None:
                return
            if self.exp_pendiente is not None:
                ms, self.exp_pendiente = self.exp_pendiente, None
                try:
                    self.exp_aplicada = cam.set_exposure(ms)
                except UC480Error:
                    pass
            pila = []
            for _ in range(cfg.average):
                if not self.corriendo.is_set():
                    return
                self.totales += 1
                try:
                    pila.append(cam.grab().astype(np.float64))
                except UC480Error:
                    self.perdidos += 1
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
        self.lbl_lectura.config(
            text="\n".join(format_sample(s, self.cfg.decimals, self.cfg.normalize))
                 + ("\n[FIJADO] click para soltar" if self.fijado else ""))

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
        if self.exp_aplicada is not None:
            ms, self.exp_aplicada = self.exp_aplicada, None
            self.lbl_exp.config(text=f"exposicion {ms:.2f} ms")
        if self.totales:
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
        if self.corriendo.is_set():
            self.exp_pendiente = ms          # lo aplica el hilo entre frames
        elif self.cam is not None:
            real = self.cam.set_exposure(ms)
            self.lbl_exp.config(text=f"exposicion {real:.2f} ms")

    def _rango_exposicion(self) -> None:
        """Ajusta la barra al rango que declara la camara abierta."""
        if self.cam is None:
            return
        lo, hi = self.cam.exposure_range()
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
        "[bold cyan]Camara[/]  ·  fotometria RGB para Thorlabs DCx\n"
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
        description="Visor y fotometro RGB para camaras Thorlabs DCx. "
                    "Sin argumentos abre un menu interactivo.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    d = Config()
    p.add_argument("-e", "--exposure-ms", type=float, default=d.exposure_ms)
    p.add_argument("-g", "--gain", type=int, default=d.gain,
                   help="ganancia master 0..100; para fotometria conviene 0")
    p.add_argument("-b", "--binning", type=int, default=d.binning,
                   choices=sorted(BINNING_MODES),
                   help="binning vertical por hardware; sube el SNR")
    p.add_argument("-c", "--pixel-clock", type=int, default=d.pixel_clock,
                   help="MHz, 5..30; 0 usa el valor por defecto del driver")
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
    return p


MODE_FLAGS = {"self_test", "benchmark", "viewer", "gui", "menu"}


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

    cfg = config_from_args(args)
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
        print(f"{cam.model}  {cam.width}x{cam.height}  exp {cam.exposure_ms:.2f} ms  "
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
             {"pixel_clock": 31}, {"pixel_clock": 4}, {"retries": -1},
             {"binning": 5}, {"binning": 0}]
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
                       "serie": "4002851234", "en_uso": False}
    assert (info[1]["modelo"], info[1]["en_uso"]) == ("DCC1545M", True)


def test_detectar_camaras_devuelve_lista_o_explica_por_que_no():
    """Con SDK enumera; sin SDK o fuera de Windows, el error dice el motivo."""
    try:
        camaras = detectar_camaras()
    except UC480Error as e:
        assert "uc480" in str(e).lower() or "Windows" in str(e)
    else:
        assert all({"id", "modelo", "serie", "en_uso"} <= set(c) for c in camaras)


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
    args = build_parser().parse_args(["--viewer", "--benchmark", "--gui", "--menu"])
    cfg = config_from_args(args)              # no debe lanzar TypeError
    assert not any(hasattr(cfg, f) for f in MODE_FLAGS)


if __name__ == "__main__":
    raise SystemExit(main())
