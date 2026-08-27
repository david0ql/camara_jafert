# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=1.26", "opencv-python>=4.9", "pytest>=8.0"]
# ///
"""Visor en vivo para cámaras Thorlabs DCx con lectura RGB de precisión decimal.

Probado sobre una DCU224C (1280x1024, sensor color, 4.65 um/px).

La cadena de imagen se fuerza a respuesta lineal: gamma 1.0, sin auto-ganancia,
auto-exposición ni balance de blancos automático, y sin corrección de color.
Cualquiera de esas etapas rompe la proporcionalidad entre señal e intensidad
incidente, que es justamente lo que se mide al ajustar una ley de Malus.

    uv run polarcam.py                 cámara real
    uv run polarcam.py --demo          patrón sintético, sin hardware
    uv run polarcam.py --self-test     tests
    uv run polarcam.py --benchmark     tasa de error del enlace USB

Controles: mouse mide, click fija el punto, +/- exposición, [/] tamaño del ROI,
a promediado temporal, s guarda muestra al CSV, r reinicia, q sale.
"""
from __future__ import annotations

import argparse
import csv
import ctypes as C
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

# Los tipos de Windows se declaran a mano en vez de importar ctypes.wintypes,
# que solo existe en Windows. Asi el modulo se importa en cualquier sistema y
# el analisis, el modo --demo y los tests corren tambien en macOS o Linux.
WORD, DWORD, BOOL = C.c_uint16, C.c_uint32, C.c_int32

# Constantes de uc480.h v4.20, del SDK "DCx Camera Support".
IS_SUCCESS = 0
IS_WAIT = 0x0001
IS_TRANSFER_ERROR = 178
IS_IGNORE_PARAMETER = -1
IS_CM_BGR8_PACKED = 1
IS_CM_MONO8 = 6
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

PIXEL_CLOCKS = (12, 16, 20, 24, 30)  # MHz, los que barre --benchmark


class UC480Error(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    exposure_ms: float = 20.0
    gain: int = 0                 # ganancia master del hardware, 0..100
    pixel_clock: int = 0          # MHz; 0 deja el valor por defecto del driver
    fps: float = 0.0              # 0 pide el máximo que permita el pixel clock
    retries: int = 8              # reintentos ante IS_TRANSFER_ERROR
    roi_radius: int = 3           # se promedia un parche de (2r+1)^2 píxeles
    decimals: int = 3
    average: int = 1              # frames promediados temporalmente
    mono: bool = False
    normalize: bool = False       # reporta 0..1 en lugar de 0..255
    sat_level: int = 255
    csv_path: Path = field(default_factory=lambda: Path("muestras_polarizacion.csv"))
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


def sample_patch(frame_bgr: np.ndarray, x: int, y: int, radius: int,
                 sat_level: int = 255) -> Sample:
    """Promedia un parche cuadrado centrado en (x, y), recortado contra el borde.

    Un píxel suelto es un entero 0..255 y no tiene decimales que ofrecer. El
    promedio sobre (2r+1)^2 píxeles sí, y con él se resuelve modulación por
    debajo de 1 LSB, que es la escala en la que se mueve una medida de
    polarización cerca de la extinción.
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
                "no hay build para macOS. Usa --demo para trabajar sin camara.")
        try:
            self.dll = C.WinDLL("uc480_64" if sys.maxsize > 2 ** 32 else "uc480")
        except OSError as e:
            raise UC480Error("falta uc480_64.dll; instala 'DCx Camera Support'") from e

        self.h = C.c_uint32(0)
        self._check(self.dll.is_InitCamera(C.byref(self.h), None), "is_InitCamera")

        info = self._sensor_info()
        self.width, self.height = int(info.nMaxWidth), int(info.nMaxHeight)
        self.model = info.strSensorName.decode(errors="replace")
        self.pixel_um = info.wPixelSize / 100.0

        bpp = 8 if cfg.mono else 24
        mode = IS_CM_MONO8 if cfg.mono else IS_CM_BGR8_PACKED
        self._check(self.dll.is_SetColorMode(self.h, mode), "is_SetColorMode")
        self._check(self.dll.is_SetDisplayMode(self.h, IS_SET_DM_DIB), "is_SetDisplayMode")
        self._set_manual()

        self.mem, self.mem_id = C.c_char_p(), C.c_int()
        self._check(self.dll.is_AllocImageMem(self.h, self.width, self.height, bpp,
                                              C.byref(self.mem), C.byref(self.mem_id)),
                    "is_AllocImageMem")
        self._check(self.dll.is_SetImageMem(self.h, self.mem, self.mem_id), "is_SetImageMem")
        pitch = C.c_int()
        self._check(self.dll.is_GetImageMemPitch(self.h, C.byref(pitch)), "is_GetImageMemPitch")
        self.pitch = int(pitch.value)

        # El orden importa: cada paso reajusta el rango válido del siguiente.
        if cfg.pixel_clock:
            self.set_pixel_clock(cfg.pixel_clock)
        self.pixel_clock = self.get_pixel_clock()
        self.fps = self.set_fps(cfg.fps)
        self.exposure_ms = self.set_exposure(cfg.exposure_ms)

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
        self.dll.is_SetGamma(self.h, 100)  # en centésimas: 100 = 1.00
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

        is_FreezeVideo bloquea hasta tener la imagen entera, así que no hay
        riesgo de leer un buffer a medio escribir. Sobre USB 2.0 falla de forma
        intermitente con IS_TRANSFER_ERROR; de ahí los reintentos.
        """
        rc = IS_TRANSFER_ERROR
        for _ in range(self.cfg.retries + 1):
            rc = self.dll.is_FreezeVideo(self.h, IS_WAIT)
            if rc == IS_SUCCESS:
                break
            self.transfer_errors += 1
        self._check(rc, "is_FreezeVideo")

        chans = 1 if self.cfg.mono else 3
        raw = C.string_at(self.mem, self.pitch * self.height)
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(self.height, self.pitch)
        arr = arr[:, : self.width * chans].reshape(self.height, self.width, chans)
        return arr[:, :, 0] if self.cfg.mono else arr

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
    """Patrón sintético tipo Malus, para trabajar en la UI sin la cámara."""

    width, height, model = 640, 480, "DEMO"

    def __init__(self, cfg: Config):
        self.cfg, self.exposure_ms, self._t = cfg, cfg.exposure_ms, 0

    def grab(self) -> np.ndarray:
        self._t += 1
        _, xx = np.mgrid[0:self.height, 0:self.width]
        base = 120 * np.cos(np.deg2rad(self._t * 2) + xx / 180.0) ** 2 + 20
        img = np.stack([base * 0.9, base, base * 0.75], axis=2)
        ruido = np.random.default_rng(self._t).normal(0, 1.5, img.shape)
        return np.clip(img * (self.exposure_ms / 20.0) + ruido, 0, 255).astype(np.uint8)

    def set_exposure(self, ms: float) -> float:
        self.exposure_ms = min(max(ms, 0.01), 500.0)
        return self.exposure_ms

    def close(self) -> None: ...

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def run_viewer(cam: Camera, cfg: Config) -> None:
    import cv2

    win = f"PolarCam - {getattr(cam, 'model', '?')}"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, int(cam.width * cfg.scale), int(cam.height * cfg.scale))

    state = {"pos": (cam.width // 2, cam.height // 2), "pinned": False,
             "radius": cfg.roi_radius, "avg": cfg.average, "exp": cam.exposure_ms}

    def on_mouse(event, mx, my, flags, _):
        if event == cv2.EVENT_LBUTTONDOWN:
            state["pinned"] = not state["pinned"]
        if not state["pinned"] and event == cv2.EVENT_MOUSEMOVE:
            _, _, ww, wh = cv2.getWindowImageRect(win)
            if ww > 0 and wh > 0:
                state["pos"] = (int(mx * cam.width / ww), int(my * cam.height / wh))

    cv2.setMouseCallback(win, on_mouse)

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
                last_bgr = (frame if frame.ndim == 3
                            else np.repeat(frame[:, :, None], 3, axis=2))
            elif last_bgr is None:
                if cv2.waitKey(30) & 0xFF in (ord("q"), 27):
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
                   f"avg {state['avg']}x   ROI {2 * r + 1}px",
                   f"USB perdidos {lost}/{total} ({lost / max(total, 1) * 100:.0f}%)"]
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
            if k in (ord("q"), 27):
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
                                         "exp_ms", "gain", "avg"])
                writer.writerow([s.x, s.y, s.n,
                                 *(f"{v:.4f}" for v in s.mean),
                                 *(f"{v:.4f}" for v in s.std),
                                 f"{s.intensity:.4f}", f"{s.sat_frac:.4f}",
                                 f"{state['exp']:.3f}", cfg.gain, state["avg"]])
                fh.flush()
                print(f"{cfg.csv_path}: R={s.mean[0]:.4f} "
                      f"G={s.mean[1]:.4f} B={s.mean[2]:.4f}")
    finally:
        if fh is not None:
            fh.close()
        cv2.destroyAllWindows()


def benchmark(cfg: Config, n: int = 25) -> list[dict]:
    """Tasa de error de transferencia USB para cada pixel clock.

    Sirve para comparar cables y puertos con números. Un enlace sano da ~0% en
    todas las filas; si el error crece con los MB por frame en vez de con la
    velocidad, el problema es el enlace físico y no el ancho de banda.
    """
    rows = []
    with UC480Camera(cfg) as cam:
        cam.dll.is_SetTimeout(cam.h, IS_TRIGGER_TIMEOUT, 2000)
        mb = cam.width * cam.height * (1 if cfg.mono else 3) / 1e6
        print(f"{cam.model}  {'mono' if cfg.mono else 'color'}  "
              f"{mb:.2f} MB/frame  {n} capturas por fila\n")
        print("  clk   fps_max    ok   err    tasa    fps_ef   MB/s")
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
            row = {"clk": clk, "fps_max": cam.fps, "ok": ok, "err": err,
                   "rate": err / n, "fps_ef": ok / dt, "mbs": ok / dt * mb}
            rows.append(row)
            print(f"  {clk:3d}   {row['fps_max']:6.2f}   {ok:3d}   {err:3d}   "
                  f"{row['rate'] * 100:5.1f}%   {row['fps_ef']:6.2f}  {row['mbs']:5.1f}")

    tasas = [r["rate"] for r in rows]
    print(f"\ntasa de error: mejor {min(tasas) * 100:.1f}%, peor {max(tasas) * 100:.1f}%")
    return rows


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Visor Thorlabs DCx con lectura RGB de precision decimal.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    d = Config()
    p.add_argument("-e", "--exposure-ms", type=float, default=d.exposure_ms)
    p.add_argument("-g", "--gain", type=int, default=d.gain,
                   help="ganancia master 0..100; para fotometria conviene 0")
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
    p.add_argument("--normalize", action="store_true", help="reporta 0..1")
    p.add_argument("--sat-level", type=int, default=d.sat_level)
    p.add_argument("--csv-path", type=Path, default=d.csv_path)
    p.add_argument("--scale", type=float, default=d.scale, help="escala de la ventana")
    p.add_argument("--demo", action="store_true", help="patron sintetico, sin hardware")
    p.add_argument("--self-test", action="store_true", help="corre los tests y sale")
    p.add_argument("--benchmark", action="store_true",
                   help="mide la tasa de error USB y sale")
    return p


def config_from_args(args: argparse.Namespace) -> Config:
    modes = {"self_test", "benchmark"}
    return Config(**{k: v for k, v in vars(args).items() if k not in modes})


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        import pytest
        return pytest.main(["-q", "--no-header", __file__])

    cfg = config_from_args(args)
    try:
        if args.benchmark:
            benchmark(cfg)
            return 0
        cam = DemoCamera(cfg) if cfg.demo else UC480Camera(cfg)
    except UC480Error as e:
        print(f"error: {e}", file=sys.stderr)
        print("proba 'uv run polarcam.py --demo' para trabajar sin la camara",
              file=sys.stderr)
        return 1

    with cam:
        detalle = (f"  clk {cam.pixel_clock} MHz  fps {cam.fps:.1f}"
                   if isinstance(cam, UC480Camera) else "")
        print(f"{cam.model}  {cam.width}x{cam.height}  "
              f"exp {cam.exposure_ms:.2f} ms  gain {cfg.gain}{detalle}")
        run_viewer(cam, cfg)
    return 0


# --- tests ---------------------------------------------------------------
# Corren sin cámara: `uv run polarcam.py --self-test`, o pytest polarcam.py

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


def test_config_valida_sus_rangos():
    import pytest
    malos = [{"roi_radius": -1}, {"decimals": 9}, {"average": 0}, {"gain": 101},
             {"pixel_clock": 31}, {"pixel_clock": 4}, {"retries": -1}]
    for kwargs in malos:
        with pytest.raises(ValueError):
            Config(**kwargs)
    assert Config(pixel_clock=0).pixel_clock == 0


def test_camara_demo_entrega_frames_usables():
    cam = DemoCamera(Config(demo=True))
    f = cam.grab()
    assert f.shape == (cam.height, cam.width, 3) and f.dtype == np.uint8
    assert cam.set_exposure(1e9) <= 500.0


def test_el_modulo_no_depende_de_windows():
    """El analisis, --demo y los tests deben correr en macOS y Linux.

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


def test_cli_expone_los_parametros_de_usb():
    args = build_parser().parse_args(["-c", "24", "-f", "10", "--retries", "2"])
    cfg = config_from_args(args)
    assert (cfg.pixel_clock, cfg.fps, cfg.retries) == (24, 10.0, 2)


if __name__ == "__main__":
    raise SystemExit(main())
