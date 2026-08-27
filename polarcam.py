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
"""Visor y fotometro RGB para camaras Thorlabs DCx, orientado a polarimetria.

Probado sobre una DCU224C (1280x1024, CCD color, 4.65 um/px, 8 bits).

La cadena de imagen se fuerza a respuesta lineal: gamma 1.0, sin auto-ganancia,
auto-exposicion ni balance de blancos, y sin correccion de color. Cualquiera de
esas etapas rompe la proporcionalidad entre señal e intensidad incidente, que
es lo que se ajusta en una ley de Malus.

    uv run polarcam.py                 menu interactivo
    uv run polarcam.py --viewer        salta el menu y abre el visor
    uv run polarcam.py --self-test     tests

El sensor entrega enteros de 0 a 255; no existe mayor profundidad (los modos de
10/12/16 bits los rechaza con IS_INVALID_COLOR_FORMAT). La precision por debajo
de 1 nivel se consigue sumando señal, en este orden de eficacia:

  1. binning por hardware, que suma carga en el CCD antes del ADC
  2. promediado espacial sobre el ROI
  3. promediado temporal sobre varios frames
"""
from __future__ import annotations

import argparse
import csv
import ctypes as C
import sys
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
    csv_path: Path = field(default_factory=lambda: Path("muestras_polarizacion.csv"))
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
    debajo de 1 LSB, que es la escala en la que se mueve una medida de
    polarizacion cerca de la extincion.
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
    def close(self) -> None: ...


class UC480Camera:
    """Acceso por ctypes a uc480_64.dll."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.transfer_errors = 0
        if sys.platform != "win32":
            raise UC480Error(
                f"la captura solo funciona en Windows (aca: {sys.platform}). "
                "Thorlabs publica el SDK DCx para Windows y Linux/amd64 unicamente; "
                "no hay build para macOS. Usa el modo demo para trabajar sin camara.")
        try:
            self.dll = C.WinDLL("uc480_64" if sys.maxsize > 2 ** 32 else "uc480")
        except OSError as e:
            raise UC480Error("falta uc480_64.dll; instala 'DCx Camera Support'") from e

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
    """Patron sintetico tipo Malus, para trabajar en la UI sin la camara."""

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
        self.exposure_ms = min(max(ms, 0.01), 500.0)
        return self.exposure_ms

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


# --- visor ----------------------------------------------------------------

def run_viewer(cam: Camera, cfg: Config, dark: np.ndarray | None = None) -> None:
    import cv2

    win = f"PolarCam - {getattr(cam, 'model', '?')}"
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
                   + ("   DARK ON" if state["dark_on"] else "")]
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


# --- interfaz de linea de comandos ----------------------------------------

def _console():
    from rich.console import Console
    return Console()


def _banner(con, cam_info: str) -> None:
    from rich.panel import Panel
    con.print(Panel.fit(
        "[bold cyan]PolarCam[/]  ·  fotometria RGB para Thorlabs DCx\n"
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


def sesion_medicion(cam: Camera, cfg: Config, con,
                    dark: np.ndarray | None = None) -> None:
    """Serie de medidas para una curva de Malus: un punto por angulo."""
    import questionary
    from rich.table import Table

    x, y = cam.width // 2, cam.height // 2
    con.print(f"[dim]midiendo en el centro ({x},{y}), ROI "
              f"{2 * cfg.roi_radius + 1}px, {cfg.average} frames por punto[/]")

    filas = []
    nuevo = not cfg.csv_path.exists()
    with cfg.csv_path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if nuevo:
            w.writerow(["angulo_deg", "R", "G", "B", "sem_R", "sem_G", "sem_B",
                        "I", "sat_frac", "exp_ms", "gain", "binning", "avg", "n_px"])
        while True:
            ang = questionary.text(
                "Angulo del analizador en grados (Enter vacio para terminar)").ask()
            if ang is None or not ang.strip():
                break
            try:
                ang = float(ang)
            except ValueError:
                con.print("[red]angulo invalido[/]")
                continue

            stack = []
            for _ in range(cfg.average):
                try:
                    stack.append(cam.grab().astype(np.float64))
                except UC480Error:
                    pass
            if not stack:
                con.print("[red]no llego ningun frame; revisa el enlace USB[/]")
                continue
            frame = apply_dark(np.mean(stack, axis=0), dark)
            if frame.ndim == 2:
                frame = np.repeat(frame[:, :, None], 3, axis=2)
            s = sample_patch(frame, x, y, cfg.roi_radius, cfg.sat_level)

            w.writerow([f"{ang:.2f}", *(f"{v:.4f}" for v in s.mean),
                        *(f"{v:.4f}" for v in s.sem), f"{s.intensity:.4f}",
                        f"{s.sat_frac:.4f}", f"{cam.exposure_ms:.3f}", cfg.gain,
                        cfg.binning, cfg.average, s.n])
            fh.flush()
            filas.append((ang, s))
            aviso = "  [red]SATURADO[/]" if s.sat_frac else ""
            con.print(f"  {ang:7.2f}deg   R {s.mean[0]:8.4f}   G {s.mean[1]:8.4f}   "
                      f"B {s.mean[2]:8.4f}   I {s.intensity:8.4f}{aviso}")

    if not filas:
        return
    t = Table(title=f"{len(filas)} puntos guardados en {cfg.csv_path}",
              header_style="bold cyan")
    for c in ("angulo", "R", "G", "B", "I", "+-(I)"):
        t.add_column(c, justify="right")
    for ang, s in filas:
        t.add_row(f"{ang:.2f}", f"{s.mean[0]:.4f}", f"{s.mean[1]:.4f}",
                  f"{s.mean[2]:.4f}", f"{s.intensity:.4f}",
                  f"{sum(s.sem) / 3:.4f}")
    con.print(t)


def menu(cfg: Config) -> int:
    """Menu interactivo con flechas. Es lo que corre `uv run polarcam.py`."""
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
                questionary.Choice("Serie de medidas      un punto por angulo", "serie"),
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
                elif accion == "serie":
                    sesion_medicion(cam, cfg, con, dark)
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
    p.add_argument("--viewer", action="store_true", help="abre el visor y salta el menu")
    p.add_argument("--self-test", action="store_true", help="corre los tests y sale")
    p.add_argument("--benchmark", action="store_true",
                   help="mide la tasa de error USB y sale")
    return p


MODE_FLAGS = {"self_test", "benchmark", "viewer"}


def config_from_args(args: argparse.Namespace) -> Config:
    return Config(**{k: v for k, v in vars(args).items() if k not in MODE_FLAGS})


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)

    if args.self_test:
        import pytest
        return pytest.main(["-q", "--no-header", __file__])

    cfg = config_from_args(args)
    if not argv:
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
        print("proba 'uv run polarcam.py --demo' para trabajar sin la camara",
              file=sys.stderr)
        return 1

    with cam:
        print(f"{cam.model}  {cam.width}x{cam.height}  exp {cam.exposure_ms:.2f} ms  "
              f"gain {cfg.gain}  bin {cfg.binning}x")
        run_viewer(cam, cfg, load_dark(cfg.dark_path) if cfg.use_dark else None)
    return 0


# --- tests ---------------------------------------------------------------
# Corren sin camara: `uv run polarcam.py --self-test`

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
    args = build_parser().parse_args(["--viewer", "--benchmark"])
    cfg = config_from_args(args)              # no debe lanzar TypeError
    assert not hasattr(cfg, "viewer")


if __name__ == "__main__":
    raise SystemExit(main())
