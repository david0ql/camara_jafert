# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "numpy>=1.26",
#     "opencv-python>=4.9",
#     "pytest>=8.0",
# ]
# ///
"""Polarimetro de imagen para camaras Thorlabs DCx e IDS uEye.

Arma la imagen de polarizacion pixel a pixel midiendo los cuatro parametros de
Stokes con el metodo clasico de Goldstein, "Polarized Light", cap. 6
(Measurement of the Stokes Polarization Parameters).

La capa de camara, el guardado y el estilo de la interfaz salen de camara.py;
aca esta solo la polarimetria.

    uv run polarimetro.py                interfaz grafica
    uv run polarimetro.py --self-test    tests

TEORIA
------
Con el haz atravesando primero un retardador de retardancia phi (eje rapido a
0 grados) y despues un analizador lineal a angulo theta, la intensidad que
llega al sensor es

    I(theta, phi) = 1/2 [S0 + S1 cos(2 theta)
                            + S2 sin(2 theta) cos(phi)
                            + S3 sin(2 theta) sin(phi)]

que es la ecuacion (6-6) de Goldstein. El orden importa: el cuarto de onda va
ANTES del polarizador en el camino del haz. Al reves, el analizador dejaria
pasar luz ya linealmente polarizada y el retardador no podria convertir la
componente circular en algo medible: S3 se volveria invisible.

De esa ecuacion salen las cuatro medidas clasicas (PASOS_ESTATICO): tres con el
cuarto de onda fuera (phi = 0) y el analizador a 0, 45 y 90 grados, y una cuarta
con el cuarto de onda puesto a 0 grados (phi = 90) y el analizador a 45. La
inversion es directa y exacta, sin ajuste ni minimos cuadrados:

    S0 = I1 + I3        S1 = I1 - I3
    S2 = 2 I2 - S0      S3 = 2 I4 - S0

METODO DINAMICO
---------------
El polarizador queda fijo a 0 grados y el cuarto de onda gira: en la toma k su eje
rapido esta a theta_k. Del producto de Mueller polarizador(0) x retardador(theta, 90)
sale la modulacion de Goldstein (cap. 6, cuarto de onda rotante):

    I(theta) = 1/2 [A - B sin(2 theta) + C cos(4 theta) + D sin(4 theta)]
    S0 = A - C      S1 = 2 C      S2 = 2 D      S3 = B

El usuario elige cuantas tomas N, el angulo inicial y el recorrido (360 o 180
grados); los angulos son inicio + k * recorrido / N, contados de nuevo desde 0 al
pasar de 360. Los Stokes salen por minimos cuadrados sobre las N tomas, que con
pasos iguales coincide con las sumas de Fourier del libro. No todo N sirve: sobre
360 grados fallan 4, 6 y 8 tomas, porque el termino en 4 theta se confunde con los
otros; la interfaz lo comprueba y sugiere el valor valido mas cercano.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from camara import (CYBER, TEXTO_DETECTAR, CapturaCancelada, Camera, ColaAjustes, Config,
                    DRIVERS, FORMATOS, Promedio, UC480Error, _aplicar_estilo,
                    _enganchar_consola, _ppm, abrir_ajustes,
                    accion_instalar_drivers, apply_dark, avisar_sin_camara, avisos_no_lineales,
                    describir_camara, detectar_camaras, ficha_camara, guardar_ultimos,
                    espera_hasta_proximo, imagen_de_pantalla, load_dark, open_camera,
                    panel_con_scroll, pedir_autoluz,
                    procesar_ajustes, restaurar_ultimos, save_frame_as, texto_ganancias, texto_negro)


@dataclass(frozen=True)
class Paso:
    """Una de las medidas de la serie: como poner los elementos y que mide."""

    n: int
    qwp: str            # posicion del cuarto de onda
    analizador: float   # angulo del polarizador lineal, en grados
    formula: str        # que combinacion de Stokes entrega


# Metodo clasico de Goldstein, cap. 6. El cuarto de onda va antes del
# analizador; en las tres primeras medidas no participa (se retira del camino).
PASOS_ESTATICO = (
    Paso(1, "fuera", 0.0, "I1 = (S0 + S1)/2"),
    Paso(2, "fuera", 45.0, "I2 = (S0 + S2)/2"),
    Paso(3, "fuera", 90.0, "I3 = (S0 - S1)/2"),
    Paso(4, "puesto, eje rapido a 0 deg", 45.0, "I4 = (S0 + S3)/2"),
)


def serie_compatible(foto: np.ndarray, previas: list[np.ndarray]) -> bool:
    """Las cuatro fotos de una serie se restan pixel a pixel: deben medir lo mismo."""
    return all(foto.shape == m.shape for m in previas)


def intensidad_teorica(stokes, theta_deg: float, phi_deg: float) -> np.ndarray:
    """I(theta, phi) de Goldstein (6-6). Se usa para verificar la inversion."""
    s0, s1, s2, s3 = (np.asarray(c, dtype=np.float64) for c in stokes)
    th, ph = np.deg2rad(theta_deg), np.deg2rad(phi_deg)
    return 0.5 * (s0 + s1 * np.cos(2 * th)
                  + s2 * np.sin(2 * th) * np.cos(ph)
                  + s3 * np.sin(2 * th) * np.sin(ph))


def aviso_de_offset(negro: int, dark) -> str:
    """El nivel de negro entra en las cuatro fotos: sin dark, S0 sube y el DOP baja."""
    if negro and dark is None:
        return (f"Nivel de negro {negro} sin dark frame: S0 y DOP quedan sesgados. "
                "Captura uno en Camara con la tapa puesta y reabre este programa.")
    return ""


def stokes_desde_intensidades(i1, i2, i3, i4) -> np.ndarray:
    """Invierte las cuatro medidas clasicas a (S0, S1, S2, S3).

    Acepta escalares o imagenes completas; con imagenes devuelve un array
    (4, alto, ancho) y la cuenta sale por pixel, que es la imagen de
    polarizacion.
    """
    i1, i2, i3, i4 = (np.asarray(v, dtype=np.float64) for v in (i1, i2, i3, i4))
    s0 = i1 + i3
    return np.stack([s0, i1 - i3, 2.0 * i2 - s0, 2.0 * i4 - s0])


# --- metodo dinamico: cuarto de onda rotante y polarizador lineal fijo ---------
# Goldstein, Polarized Light, cap. 6. La lamina gira y el polarizador queda fijo
# a 0 grados, siempre en el orden luz -> lamina -> polarizador -> camara. Con el
# eje rapido a theta y retardo de 90 grados:
#     I(theta) = 1/2 [A - B sin 2theta + C cos 4theta + D sin 4theta]
#     S0 = A - C,  S1 = 2C,  S2 = 2D,  S3 = B
# En vez de las sumas de Fourier se resuelve por minimos cuadrados con la fila de
# Mueller de cada toma: con pasos iguales da lo mismo que las sumas, y ademas
# admite una lamina que no retarda exactamente 90 grados y series incompletas.

RECORRIDOS = (360.0, 180.0)     # grados que gira la lamina en toda la serie
RETARDO_QWP = 90.0
COND_MAX = 1e6                  # por encima, las tomas no separan los cuatro Stokes


def mueller_polarizador(theta_deg: float) -> np.ndarray:
    """Polarizador lineal ideal con el eje de transmision a theta (Goldstein 4-?)."""
    c, s = np.cos(np.deg2rad(2 * theta_deg)), np.sin(np.deg2rad(2 * theta_deg))
    return 0.5 * np.array([[1, c, s, 0],
                           [c, c * c, c * s, 0],
                           [s, c * s, s * s, 0],
                           [0, 0, 0, 0]], dtype=np.float64)


def mueller_retardador(theta_deg: float, retardo_deg: float) -> np.ndarray:
    """Retardador lineal con el eje rapido a theta y retardo delta."""
    c, s = np.cos(np.deg2rad(2 * theta_deg)), np.sin(np.deg2rad(2 * theta_deg))
    cd, sd = np.cos(np.deg2rad(retardo_deg)), np.sin(np.deg2rad(retardo_deg))
    return np.array([[1, 0, 0, 0],
                     [0, c * c + cd * s * s, (1 - cd) * c * s, -sd * s],
                     [0, (1 - cd) * c * s, s * s + cd * c * c, sd * c],
                     [0, sd * s, -sd * c, cd]], dtype=np.float64)


def fila_dinamica(theta_deg: float, retardo_deg: float = RETARDO_QWP) -> np.ndarray:
    """Lo que ve la camara en una toma: I = fila . S, con la lamina antes del polarizador."""
    return (mueller_polarizador(0.0) @ mueller_retardador(theta_deg, retardo_deg))[0]


def angulos_dinamicos(n_tomas: int, inicio_deg: float = 0.0,
                      recorrido_deg: float = 360.0) -> np.ndarray:
    """El angulo de la lamina en cada toma: pasos iguales, de vuelta a 0 al pasar 360."""
    if n_tomas < 1:
        raise ValueError("hace falta al menos una toma")
    paso = recorrido_deg / n_tomas
    return (inicio_deg + paso * np.arange(n_tomas)) % 360.0


def matriz_dinamica(angulos_deg, retardo_deg: float = RETARDO_QWP) -> np.ndarray:
    """Una fila por toma: de los cuatro Stokes a la intensidad medida."""
    return np.array([fila_dinamica(t, retardo_deg) for t in np.atleast_1d(angulos_deg)])


def tomas_validas_cercanas(n: int, recorrido_deg: float, inicio_deg: float = 0.0,
                           retardo_deg: float = RETARDO_QWP) -> int:
    """El numero de tomas que sirve mas cercano a n, para sugerirlo.

    No hay un minimo a partir del cual todo sirve: sobre 360 grados fallan 4, 6 y 8
    pero 5 y 7 sirven, porque el termino en 4 theta se pliega sobre los otros.
    """
    for d in range(0, 361):
        for m in (n + d, n - d):
            if m >= 4 and not problema_de_tomas(
                    angulos_dinamicos(m, inicio_deg, recorrido_deg), retardo_deg):
                return m
    raise ValueError("ningun numero de tomas sirve con ese retardo")


def problema_de_tomas(angulos_deg, retardo_deg: float = RETARDO_QWP) -> str:
    """Vacio si las tomas alcanzan para separar S0..S3; si no, el motivo.

    Con pasos iguales sobre 360 grados el termino en 4 theta da cuatro vueltas: con
    8 tomas de 45 grados sin 4theta es 0 en todas y S2 no se puede medir.
    """
    w = matriz_dinamica(angulos_deg, retardo_deg)
    if len(w) < 4:
        return f"con {len(w)} tomas no alcanza: hacen falta al menos 4"
    sv = np.linalg.svd(w, compute_uv=False)
    if sv[-1] <= sv[0] / COND_MAX:
        return ("con esos angulos hay parametros de Stokes que no se distinguen: "
                "las tomas caen en simetrias de la lamina")
    return ""


def stokes_dinamico(intensidades, angulos_deg,
                    retardo_deg: float = RETARDO_QWP) -> tuple[np.ndarray, np.ndarray]:
    """(S0..S3, residuo relativo) a partir de las tomas, pixel a pixel.

    intensidades es una secuencia de escalares o imagenes, una por angulo. El
    residuo es la raiz del error cuadratico medio del ajuste dividida por S0: si
    las tomas siguen el modelo es casi 0, y crece si un angulo quedo mal puesto,
    la luz vario o algo se movio entre tomas.
    """
    problema = problema_de_tomas(angulos_deg, retardo_deg)
    if problema:
        raise ValueError(problema)
    w = matriz_dinamica(angulos_deg, retardo_deg)
    datos = np.stack([np.asarray(i, dtype=np.float64) for i in intensidades])
    forma = datos.shape[1:]
    planos = datos.reshape(len(w), -1)
    stokes = np.linalg.pinv(w) @ planos
    resto = planos - w @ stokes
    rms = np.sqrt(np.mean(resto ** 2, axis=0))
    with np.errstate(invalid="ignore", divide="ignore"):
        residuo = np.nan_to_num(rms / np.where(np.abs(stokes[0]) < 1e-12, np.nan, stokes[0]))
    return stokes.reshape((4, *forma)), residuo.reshape(forma)


def pasos_dinamicos(angulos_deg) -> tuple[Paso, ...]:
    """La serie como pasos de la guia: la lamina a cada angulo, el polarizador fijo."""
    return tuple(Paso(k + 1, f"eje rapido a {t:.1f} deg", 0.0, f"I{k + 1} = I(theta = {t:.1f} deg)")
                 for k, t in enumerate(angulos_deg))


def parametros(stokes) -> dict[str, np.ndarray]:
    """Grado de polarizacion, azimut y elipticidad a partir de Stokes.

    Formulas de Goldstein cap. 5. El azimut psi queda en 0..180 grados (una
    elipse orientada a 10 y a 190 grados es la misma) y la elipticidad chi en
    -45..45, con el signo dando el sentido de giro.
    """
    s0, s1, s2, s3 = (np.asarray(c, dtype=np.float64) for c in stokes)
    # Donde no llego luz, S0 es 0 y el cociente no esta definido: se informa 0
    # en vez de un nan que despues contamina la imagen entera.
    seguro = np.where(np.abs(s0) < 1e-12, np.nan, s0)
    lineal = np.sqrt(s1 ** 2 + s2 ** 2)
    total = np.sqrt(s1 ** 2 + s2 ** 2 + s3 ** 2)
    with np.errstate(invalid="ignore", divide="ignore"):
        dop = np.nan_to_num(total / seguro)
        dolp = np.nan_to_num(lineal / seguro)
        docp = np.nan_to_num(s3 / seguro)
        azimut = np.rad2deg(0.5 * np.arctan2(s2, s1)) % 180.0
        elipticidad = np.rad2deg(
            0.5 * np.arcsin(np.clip(np.nan_to_num(s3 / np.where(total < 1e-12, np.nan, total)),
                                    -1.0, 1.0)))
    return {"S0": s0, "S1": s1, "S2": s2, "S3": s3, "DOP": dop, "DOLP": dolp,
            "DOCP": docp, "azimut": azimut, "elipticidad": np.nan_to_num(elipticidad)}


def imagen_falso_color(p: dict[str, np.ndarray]) -> np.ndarray:
    """Mapa de polarizacion en color: tono = azimut, saturacion = DOLP, valor = S0.

    Es la representacion habitual: el color dice en que direccion vibra la luz
    y cuan saturado esta dice cuanto de esa luz esta polarizada. Devuelve BGR
    de 8 bits, listo para mostrar o guardar como PNG.
    """
    import cv2
    azimut, dolp, s0 = p["azimut"], p["DOLP"], p["S0"]
    tope = float(np.nanmax(s0)) if np.isfinite(s0).any() else 0.0
    valor = np.zeros_like(s0) if tope <= 0 else np.clip(s0 / tope, 0, 1)
    hsv = np.stack([
        (azimut % 180.0).astype(np.uint8),                  # OpenCV usa 0..179
        (np.clip(dolp, 0, 1) * 255).astype(np.uint8),
        (valor * 255).astype(np.uint8),
    ], axis=-1)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def elipse_puntos(s1: float, s2: float, s3: float, n: int = 181) -> np.ndarray:
    """Puntos (x, y) de la elipse de polarizacion de un pixel.

    Goldstein cap. 3: la elipse queda fijada por el azimut psi y la elipticidad
    chi. Los semiejes salen de A^2 + B^2 = Ipol junto con B/A = |tan chi|, y el
    signo de S3 da el sentido de giro, que es lo que distingue una elipse
    derecha de su espejo izquierdo.
    """
    import math
    ip = math.sqrt(s1 * s1 + s2 * s2 + s3 * s3)
    if ip <= 0:
        return np.zeros((n, 2))
    psi = 0.5 * math.atan2(s2, s1)
    chi = 0.5 * math.asin(max(-1.0, min(1.0, s3 / ip)))
    a, b = math.sqrt(ip) * math.cos(chi), math.sqrt(ip) * math.sin(abs(chi))
    t = np.linspace(0.0, 2.0 * np.pi, n)
    x, y = a * np.cos(t), b * np.sin(t) * (1.0 if s3 >= 0 else -1.0)
    return np.stack([x * math.cos(psi) - y * math.sin(psi),
                     x * math.sin(psi) + y * math.cos(psi)], axis=1)


# Clases del mapa de estados, en orden de codigo. El color va en BGR, que es lo
# que espera cv2 y lo que dibuja el visor.
ESTADOS_CLASES = (
    ("sin señal", (25, 25, 25)),
    ("sin polarizar", (110, 110, 110)),
    ("lineal 0 (H)", (60, 60, 255)),
    ("lineal 45", (60, 170, 255)),
    ("lineal 90 (V)", (60, 255, 120)),
    ("lineal 135", (255, 190, 60)),
    ("eliptica der.", (255, 80, 220)),
    ("eliptica izq.", (180, 80, 255)),
    ("circular der.", (60, 255, 255)),
    ("circular izq.", (255, 255, 90)),
)

# Umbrales de lectura, no de fisica: la polarizacion es un continuo y estos
# cortes solo sirven para ponerle nombre. Se declaran aca para que se vean.
UMBRAL_SENAL = 0.05      # fraccion de S0 maximo por debajo de la cual no hay dato
UMBRAL_DOP = 0.15        # debajo de esto se lee como no polarizada
UMBRAL_LINEAL = 10.0     # |chi| menor: se lee como lineal
UMBRAL_CIRCULAR = 35.0   # |chi| mayor: se lee como circular
UMBRAL_RESIDUO = 0.05    # residuo del ajuste dinamico (fraccion de S0) que se marca


def clasificar(p: dict[str, np.ndarray]) -> np.ndarray:
    """Codigo de ESTADOS_CLASES para cada pixel.

    Primero descarta los pixeles sin señal: con S0 en el piso, el DOP es un
    cociente de ruido y clasificarlos seria inventar. Despues separa por grado
    de polarizacion, por elipticidad y, dentro de lo lineal, por banda de
    azimut de +-22.5 grados alrededor de 0, 45, 90 y 135.
    """
    s0 = np.asarray(p["S0"], dtype=np.float64)
    dop = np.asarray(p["DOP"], dtype=np.float64)
    chi = np.abs(np.asarray(p["elipticidad"], dtype=np.float64))
    azim = np.asarray(p["azimut"], dtype=np.float64)
    derecha = np.asarray(p["S3"], dtype=np.float64) >= 0

    banda = np.floor(((azim + 22.5) % 180.0) / 45.0).astype(np.int64)
    lineal = np.choose(np.clip(banda, 0, 3), [2, 3, 4, 5])
    codigo = np.where(chi < UMBRAL_LINEAL, lineal,
                      np.where(chi > UMBRAL_CIRCULAR,
                               np.where(derecha, 8, 9),
                               np.where(derecha, 6, 7)))
    codigo = np.where(dop < UMBRAL_DOP, 1, codigo)
    tope = float(np.nanmax(s0)) if np.size(s0) else 0.0
    if tope > 0:
        codigo = np.where(s0 < tope * UMBRAL_SENAL, 0, codigo)
    return codigo.astype(np.uint8)


def imagen_estados(p: dict[str, np.ndarray]) -> np.ndarray:
    """Mapa categorico del estado de polarizacion, en BGR de 8 bits.

    Mas directo de leer que el falso color: cada color es un estado con nombre,
    no un punto de un continuo que hay que interpretar.
    """
    paleta = np.array([color for _, color in ESTADOS_CLASES], dtype=np.uint8)
    return paleta[clasificar(p)]


def exportar(destino: Path, medidas: dict[int, np.ndarray], p: dict[str, np.ndarray],
             formato_fotos: str = ".npy", extra: dict | None = None) -> list[Path]:
    """Guarda las fotografias y la imagen de polarizacion.

    Las fotos van en el formato que pida el usuario. Los mapas de Stokes van
    siempre a .npz de float64: son angulos en grados y cocientes entre -1 y 1,
    que en un PNG de 8 bits se destruirian. El falso color se agrega aparte,
    como PNG, para mirar.
    """
    destino.mkdir(parents=True, exist_ok=True)
    sello = time.strftime("%Y%m%d_%H%M%S")
    escritos = []
    for n, img in sorted(medidas.items()):
        escritos.append(save_frame_as(img, destino / f"medida{n}_{sello}{formato_fotos}"))

    npz = destino / f"polarizacion_{sello}_stokes.npz"
    # extra lleva como se midio: en el dinamico, el angulo de la lamina en cada toma.
    datos = {k: np.asarray(v, dtype=np.float64) for k, v in {**p, **(extra or {})}.items()}
    np.savez_compressed(npz, **datos)
    escritos.append(npz)
    escritos.append(save_frame_as(imagen_falso_color(p).astype(np.float64),
                                  destino / f"polarizacion_{sello}_falsocolor.png"))
    return escritos


# --- interfaz grafica -------------------------------------------------------

class PolarimetroGUI:
    """Ventana del polarimetro: metodos, guia paso a paso e imagen en vivo.

    Arranca en el menu de metodos; el estatico abre el visor con el panel de
    instrucciones, y cada medida tomada recalcula la imagen de polarizacion con
    lo que haya hasta el momento.
    """

    INTERVALO_MS = 40

    def __init__(self, root, cfg: Config) -> None:
        import threading
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk, self.threading = tk, ttk, threading
        self.root, self.cfg = root, cfg
        self.cam: Camera | None = None
        self.dark = load_dark(cfg.dark_path)
        self.frame: np.ndarray | None = None
        self.lock = threading.Lock()
        self.hilo: threading.Thread | None = None
        self.corriendo = threading.Event()
        self.realce_on = True
        self.cola = ColaAjustes()
        self.win_ajustes = None
        self.medidas: dict[int, np.ndarray] = {}
        self.paso_actual = 0
        # Metodo en curso: el estatico tiene sus cuatro pasos fijos; el dinamico,
        # uno por angulo de la lamina.
        self.metodo = "estatico"
        self.pasos: tuple[Paso, ...] = PASOS_ESTATICO
        self.angulos: np.ndarray | None = None
        self.retardo = RETARDO_QWP
        self.params: dict[str, np.ndarray] | None = None
        self.photo = self.photo_pol = None
        self._img_id = self._pol_id = None
        self._tick_id = None        # para cancelar el redibujo al cambiar de pantalla
        self._disp: dict[str, tuple] = {}     # rectangulo dibujado de cada canvas
        self.modo_estados = False
        self.modo_elipse = False
        self.pixel_sel: tuple[int, int] | None = None
        # Los mapas se recalculan solo cuando entra una medida, no en cada
        # cuadro: sobre 1280x1024 son varios ms que no hay por que gastar.
        self._version = 0
        self._ultimo_frame: np.ndarray | None = None   # el que se dibujo por ultima vez
        self._ultima_vista: tuple | None = None
        self._cache = {}

        root.title("Polarimetro - camara de polarizacion")
        ancho = min(1200, root.winfo_screenwidth() - 60)
        alto = min(780, root.winfo_screenheight() - 90)
        root.geometry(f"{ancho}x{alto}")
        root.minsize(900, 560)
        root.protocol("WM_DELETE_WINDOW", self.cerrar)

        self.contenedor = ttk.Frame(root)
        self.contenedor.pack(fill="both", expand=True)
        self._pantalla_metodos()

    # --- pantalla 1: eleccion de metodo ---

    def _limpiar(self) -> None:
        # Cancelar antes de destruir: si queda un after pendiente, se despierta
        # apuntando a widgets muertos y tira "bad window path name".
        self._parar_tick()
        for w in self.contenedor.winfo_children():
            w.destroy()

    def _parar_tick(self) -> None:
        if self._tick_id is not None:
            self.root.after_cancel(self._tick_id)
            self._tick_id = None

    def _pantalla_metodos(self) -> None:
        """Menu de metodos. Es la pantalla de entrada del programa."""
        self.detener()
        self._limpiar()
        ttk = self.ttk
        caja = ttk.Frame(self.contenedor, padding=30)
        caja.place(relx=0.5, rely=0.5, anchor="center")

        ttk.Label(caja, text="CAMARA DE POLARIZACION", font=("Consolas", 18, "bold"),
                  foreground=CYBER["neon"]).pack()
        ttk.Label(caja, text="elegi el metodo de medida de los parametros de Stokes",
                  foreground=CYBER["tenue"], font=("Consolas", 9)).pack(pady=(4, 20))

        ttk.Button(caja, text="Metodo estatico\npolarizador lineal y lamina cuarto de onda",
                   style="Neon.TButton", command=self._metodo_estatico).pack(
            fill="x", ipady=8)
        ttk.Label(caja, text="cuatro medidas, Goldstein cap. 6", foreground=CYBER["tenue"],
                  font=("Consolas", 8)).pack(pady=(2, 14))

        ttk.Button(caja, text="Metodo dinamico\ncuarto de onda rotante y polarizador lineal",
                   command=self._pantalla_config_dinamico).pack(fill="x", ipady=8)
        ttk.Label(caja, text="analisis de Fourier de la señal modulada",
                  foreground=CYBER["tenue"], font=("Consolas", 8)).pack(pady=(2, 0))

    def _metodo_estatico(self) -> None:
        self.metodo, self.pasos, self.angulos = "estatico", PASOS_ESTATICO, None
        self._pantalla_medida()

    # --- pantalla 2b: configuracion del metodo dinamico ---

    def _pantalla_config_dinamico(self) -> None:
        """Cuantas tomas, desde que angulo y cuanto gira la lamina."""
        tk, ttk = self.tk, self.ttk
        self.detener()
        self._limpiar()
        caja = ttk.Frame(self.contenedor, padding=30)
        caja.place(relx=0.5, rely=0.5, anchor="center")
        ttk.Label(caja, text="METODO DINAMICO", font=("Consolas", 16, "bold"),
                  foreground=CYBER["neon"]).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(caja, text="cuarto de onda rotante y polarizador lineal fijo a 0 grados\n"
                             "orden del haz: luz -> cuarto de onda -> polarizador -> camara",
                  foreground=CYBER["tenue"], font=("Consolas", 9), justify="left"
                  ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 16))

        self.var_tomas = tk.StringVar(value="16")
        self.var_inicio = tk.StringVar(value="0")
        self.var_recorrido = tk.StringVar(value="360")
        self.var_retardo = tk.StringVar(value=f"{RETARDO_QWP:g}")
        filas = (("Numero de tomas", ttk.Spinbox(caja, from_=4, to=360, width=8,
                                                 textvariable=self.var_tomas)),
                 ("Angulo inicial de la lamina (grados)",
                  ttk.Entry(caja, width=10, textvariable=self.var_inicio)),
                 ("Recorrido total (grados)",
                  ttk.Combobox(caja, width=8, state="readonly", textvariable=self.var_recorrido,
                               values=[f"{r:g}" for r in RECORRIDOS])),
                 ("Retardo de la lamina (grados)",
                  ttk.Entry(caja, width=10, textvariable=self.var_retardo)))
        for i, (texto, w) in enumerate(filas, start=2):
            ttk.Label(caja, text=texto).grid(row=i, column=0, sticky="w", pady=3)
            w.grid(row=i, column=1, sticky="w", padx=(12, 0), pady=3)

        self.lbl_config = ttk.Label(caja, text="", font=("Consolas", 9), justify="left",
                                    wraplength=520)
        self.lbl_config.grid(row=6, column=0, columnspan=2, sticky="w", pady=(14, 0))
        fila = ttk.Frame(caja)
        fila.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        ttk.Button(fila, text="< volver", command=self._pantalla_metodos).pack(side="left")
        self.btn_comenzar = ttk.Button(fila, text="Comenzar la serie", style="Neon.TButton",
                                       command=self._comenzar_dinamico)
        self.btn_comenzar.pack(side="right")
        for v in (self.var_tomas, self.var_inicio, self.var_recorrido, self.var_retardo):
            v.trace_add("write", lambda *_a: self._revisar_config())
        self._revisar_config()

    def _leer_config(self) -> tuple[np.ndarray, float, str]:
        """(angulos, retardo, problema). El problema es vacio si la serie sirve."""
        try:
            n = int(self.var_tomas.get())
            inicio = float(self.var_inicio.get().replace(",", "."))
            recorrido = float(self.var_recorrido.get())
            retardo = float(self.var_retardo.get().replace(",", "."))
        except ValueError:
            return np.array([]), RETARDO_QWP, "escribi numeros en todos los campos"
        if not 4 <= n <= 360:
            return np.array([]), retardo, "el numero de tomas va de 4 a 360"
        if not 0 < retardo < 180:
            return np.array([]), retardo, "el retardo tiene que estar entre 0 y 180 grados"
        angulos = angulos_dinamicos(n, inicio, recorrido)
        problema = problema_de_tomas(angulos, retardo)
        if problema:
            problema += (f". Con {recorrido:g} grados proba con "
                         f"{tomas_validas_cercanas(n, recorrido, inicio, retardo)} tomas.")
        return angulos, retardo, problema

    def _revisar_config(self) -> None:
        angulos, _retardo, problema = self._leer_config()
        if problema:
            self.lbl_config.config(text=problema, foreground=CYBER["alerta"])
            self.btn_comenzar.config(state="disabled")
            return
        paso = float(self.var_recorrido.get()) / len(angulos)
        lista = ", ".join(f"{t:g}" for t in np.round(angulos[:12], 2))
        if len(angulos) > 12:
            lista += f", ... ({len(angulos) - 12} mas)"
        self.lbl_config.config(
            text=f"{len(angulos)} tomas, paso de {paso:g} grados\n"
                 f"angulos de la lamina: {lista}\n"
                 "(al pasar de 360 se vuelve a contar desde 0)",
            foreground=CYBER["texto"])
        self.btn_comenzar.config(state="normal")

    def _comenzar_dinamico(self) -> None:
        angulos, retardo, problema = self._leer_config()
        if problema:
            return
        self.metodo, self.angulos, self.retardo = "dinamico", angulos, retardo
        self.pasos = pasos_dinamicos(angulos)
        self._pantalla_medida()

    # --- pantalla 3: guia de medida, para los dos metodos ---

    def _pantalla_medida(self) -> None:
        tk, ttk = self.tk, self.ttk
        self._limpiar()
        self.medidas.clear()
        self.paso_actual = 0
        self.params = None
        self._img_id = self._pol_id = None
        self.pixel_sel = None
        self._cache.clear()
        self._disp.clear()

        columna = ttk.Frame(self.contenedor)
        columna.pack(side="left", fill="y")
        izq = panel_con_scroll(columna, ancho=300)

        ttk.Button(izq, text="< volver a los metodos",
                   command=self._pantalla_metodos).pack(fill="x")
        ttk.Label(izq, text=(f"METODO DINAMICO  {len(self.pasos)} tomas"
                             if self.metodo == "dinamico" else "METODO ESTATICO  4 medidas"),
                  font=("Consolas", 9, "bold"), foreground=CYBER["neon2"]
                  ).pack(anchor="w", pady=(6, 0))

        f = ttk.LabelFrame(izq, text="Camara", padding=6)
        f.pack(fill="x", pady=(8, 0))
        self.lbl_cam = ttk.Label(f, text="...", font=("Consolas", 9), justify="left",
                                 wraplength=280)
        self.lbl_cam.pack(anchor="w")
        self.lbl_aviso = ttk.Label(f, text="", font=("Consolas", 8), justify="left",
                                   wraplength=280, foreground=CYBER["alerta"])
        self.lbl_aviso.pack(anchor="w")
        ttk.Button(f, text=TEXTO_DETECTAR, command=self.detectar).pack(fill="x", pady=(6, 0))
        ttk.Button(f, text="Instalar drivers",
                   command=lambda: accion_instalar_drivers(self.root)).pack(fill="x", pady=(4, 0))
        ttk.Button(f, text="Ajustes de camara...",
                   command=lambda: abrir_ajustes(self.root, self)).pack(fill="x", pady=(4, 0))
        ttk.Button(f, text="Autoajustar luz",
                   command=lambda: pedir_autoluz(self)).pack(fill="x", pady=(4, 0))
        fila = ttk.Frame(f)
        fila.pack(fill="x", pady=(6, 0))
        self.btn_iniciar = ttk.Button(fila, text="Iniciar visor", style="Neon.TButton",
                                      command=self.iniciar)
        self.btn_iniciar.pack(side="left", fill="x", expand=True)
        self.btn_detener = ttk.Button(fila, text="Detener", command=self.detener)
        self.btn_detener.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.var_realce = tk.BooleanVar(value=self.realce_on)
        self.chk_realce = ttk.Checkbutton(f, text="Realzar en pantalla",
                                          variable=self.var_realce, command=self._cambiar_realce)
        self.chk_realce.pack(anchor="w", pady=(6, 0))

        f = ttk.LabelFrame(izq, text="Montaje para esta medida", padding=6)
        f.pack(fill="x", pady=(8, 0))
        self.lbl_paso = ttk.Label(f, text="", font=("Consolas", 10, "bold"),
                                  foreground=CYBER["neon"], justify="left", wraplength=280)
        self.lbl_paso.pack(anchor="w")
        self.lbl_montaje = ttk.Label(f, text="", font=("Consolas", 9), justify="left",
                                     wraplength=280)
        self.lbl_montaje.pack(anchor="w", pady=(6, 0))
        ttk.Label(f, text="orden del haz:\nluz -> cuarto de onda -> "
                          + ("polarizador fijo" if self.metodo == "dinamico" else "analizador")
                          + " -> camara",
                  font=("Consolas", 8), foreground=CYBER["tenue"], justify="left",
                  wraplength=280
                  ).pack(anchor="w", pady=(8, 0))

        self.btn_tomar = ttk.Button(izq, text="Tomar fotografia", style="Neon.TButton",
                                    command=self.tomar)
        self.btn_tomar.pack(fill="x", pady=(8, 0))
        self.btn_repetir = ttk.Button(izq, text="Repetir este dato", command=self.repetir)
        self.btn_repetir.pack(fill="x", pady=(4, 0))

        f = ttk.LabelFrame(izq, text="Serie", padding=6)
        f.pack(fill="both", pady=(8, 0))
        cols = ("n", "qwp", "analiz", "estado")
        self.tabla = self.ttk.Treeview(f, columns=cols, show="headings",
                                       height=min(len(self.pasos), 8))
        for c, a in zip(cols, (30, 90, 60, 80)):
            self.tabla.heading(c, text=c)
            self.tabla.column(c, width=a, anchor="w")
        self.tabla.pack(fill="both", expand=True)

        f = ttk.LabelFrame(izq, text="Exportar", padding=6)
        f.pack(fill="x", pady=(8, 0))
        fila = ttk.Frame(f)
        fila.pack(fill="x")
        ttk.Label(fila, text="fotos en").pack(side="left")
        self.var_formato = tk.StringVar(value=".npy")
        ttk.Combobox(fila, textvariable=self.var_formato, state="readonly", width=8,
                     values=[".npy", ".tiff", ".png", ".jpg"]).pack(side="right")
        self.btn_exportar = ttk.Button(f, text="Exportar datos...", command=self.exportar)
        self.btn_exportar.pack(fill="x", pady=(6, 0))
        ttk.Label(f, text="Stokes siempre en .npz float64:\nson angulos y cocientes, no\n"
                          "sobreviven a 8 bits", font=("Consolas", 8),
                  foreground=CYBER["tenue"], justify="left").pack(anchor="w", pady=(6, 0))
        self.lbl_export = ttk.Label(f, text="", foreground=CYBER["ok"],
                                    font=("Consolas", 8), justify="left", wraplength=280)
        self.lbl_export.pack(anchor="w")

        der = ttk.Frame(self.contenedor, padding=(0, 8, 8, 8))
        der.pack(side="right", fill="both", expand=True)
        self.canvas = tk.Canvas(der, background=CYBER["hundido"], highlightthickness=1,
                                highlightbackground=CYBER["borde"])
        self.canvas.pack(side="left", fill="both", expand=True)

        col = ttk.Frame(der, width=345)
        col.pack(side="right", fill="y", padx=(6, 0))
        col.pack_propagate(False)
        ttk.Label(col, text="IMAGEN DE POLARIZACION", font=("Consolas", 9, "bold"),
                  foreground=CYBER["neon2"]).pack(anchor="w")
        fila = ttk.Frame(col)
        fila.pack(fill="x", pady=(4, 0))
        self.btn_estados = ttk.Button(fila, text="Mapa de estados",
                                      command=self.alternar_estados)
        self.btn_estados.pack(side="left", fill="x", expand=True)
        self.btn_elipse = ttk.Button(fila, text="Elipse por pixel",
                                     command=self.alternar_elipse)
        self.btn_elipse.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # Altura minima chica a proposito: expand=True le da todo lo que sobre,
        # pero si pide mucho, los paneles de abajo quedan cortados.
        self.canvas_pol = tk.Canvas(col, background=CYBER["hundido"], highlightthickness=1,
                                    highlightbackground=CYBER["neon2"], height=140)
        self.canvas_pol.pack(fill="both", expand=True, pady=(4, 0))
        self.canvas_pol.bind("<Button-1>", self._on_click_pol)
        self.lbl_pol = ttk.Label(col, text="faltan medidas", font=("Consolas", 8),
                                 foreground=CYBER["tenue"], justify="left", wraplength=325)
        self.lbl_pol.pack(anchor="w", pady=(4, 0))
        self.lbl_modo = ttk.Label(col, font=("Consolas", 8), foreground=CYBER["tenue"],
                                  justify="left", wraplength=325)
        self.lbl_modo.pack(anchor="w", pady=(2, 0))

        self.canvas_elipse = tk.Canvas(col, background=CYBER["hundido"], height=132,
                                       highlightthickness=1,
                                       highlightbackground=CYBER["neon"])
        self.canvas_leyenda = tk.Canvas(col, background=CYBER["fondo"], height=60,
                                        highlightthickness=0)

        if self.cam is None:
            self._abrir_camara()
        else:                  # se volvio del menu: los widgets de la ficha son nuevos
            self._actualizar_ficha()
        self._refrescar_paso()
        self._refrescar_modos()
        self._tick()
        self.iniciar()

    # --- camara ---

    def _abrir_camara(self) -> None:
        from tkinter import messagebox
        try:
            self.cam = open_camera(self.cfg)
        except UC480Error as e:
            if not messagebox.askyesno("Sin camara", f"{e}\n\n¿Seguir en modo demo?"):
                self.lbl_cam.config(text="sin camara")
                return
            from dataclasses import replace
            self.cfg = replace(self.cfg, demo=True)
            self.cam = open_camera(self.cfg)
        self.realce_on, aviso = restaurar_ultimos(self.cam, self.cfg)
        self.var_realce.set(self.realce_on)
        self.root.title(f"Polarimetro - {self.cam.marca} {self.cam.model}")
        self._actualizar_ficha()
        if aviso:
            actual = self.lbl_aviso.cget("text")
            self.lbl_aviso.config(text=f"{aviso}\n{actual}" if actual else aviso)

    def _cambiar_realce(self) -> None:
        self.realce_on = bool(self.var_realce.get())
        if not self.realce_on:
            self.chk_realce.config(text="Realzar en pantalla")
        if self.cam is not None:
            guardar_ultimos(self.cam, self.realce_on)

    def _actualizar_ficha(self) -> None:
        cam = self.cam
        if cam is None:
            return
        self.lbl_cam.config(
            text=f"{ficha_camara(cam)}\n{cam.width}x{cam.height}   "
                 f"exp {cam.exposure_ms:.2f} ms   {texto_negro(cam)}\n"
                 f"ganancia {texto_ganancias(cam)}",
            foreground=CYBER["neon2"] if self.cfg.demo else CYBER["ok"])
        avisos = avisos_no_lineales(cam.valores)
        # Stokes se calcula con diferencias de intensidad: sin linealidad no sirve.
        textos = ["Fuera de la cadena lineal, los Stokes no valen: " + ", ".join(avisos)
                  if avisos else "", aviso_de_offset(cam.valores["negro"], self.dark)]
        self.lbl_aviso.config(text="\n".join(t for t in textos if t))

    def ajuste_aplicado(self, clave: str, res) -> None:
        """El hilo de captura aplico un ajuste: se refresca la ficha."""
        if self.cam is not None:
            guardar_ultimos(self.cam, self.realce_on)
        self._actualizar_ficha()
        if isinstance(res, Exception):
            self.lbl_aviso.config(text=str(res))
        elif clave == ColaAjustes.AUTOLUZ and not self.medidas:
            texto = str(res)
            if self.dark is not None and self.cam.luz_cambio:
                # Se aplica siempre que exista: con otros ajustes falsearia los Stokes.
                self.dark = None
                texto += " Dark frame quitado: ya no corresponde."
            aviso = aviso_de_offset(self.cam.valores["negro"], self.dark)
            self.lbl_aviso.config(text=f"{texto} {aviso}".strip())
        elif self.medidas and self.paso_actual < len(self.pasos):
            # Las cuatro fotos tienen que salir de la misma cadena de imagen.
            self.lbl_aviso.config(text="Cambiaste un ajuste con la serie empezada: "
                                       "repite las medidas ya tomadas.")

    def detectar(self) -> None:
        """Vuelve a buscar camaras de las dos marcas y engancha la que haya.

        La camara propia se cierra primero: abierta, retiene su DLL y la de la
        otra marca no se podria ni cargar.
        """
        from dataclasses import replace
        from tkinter import messagebox
        self.detener()
        if self.cam is not None:
            guardar_ultimos(self.cam, self.realce_on)
            self.cam.close()
            self.cam = None
        try:
            camaras = detectar_camaras(self.cfg.driver)
        except UC480Error as e:
            avisar_sin_camara(self.root, str(e))
            camaras = []
        else:
            if not camaras:
                avisar_sin_camara(self.root)
        if camaras:
            self.cfg = replace(self.cfg, demo=False)
        self._abrir_camara()
        if camaras and self.cam is not None and not self.cfg.demo:
            messagebox.showinfo("Deteccion", "Conectada:\n" + "\n".join(
                f"  - {describir_camara(c)}" for c in camaras))
        self.iniciar()

    def iniciar(self) -> None:
        if self.cam is None or self.corriendo.is_set():
            return
        self.cam.reanudar()
        self.corriendo.set()
        self.hilo = self.threading.Thread(target=self._bucle, daemon=True)
        self.hilo.start()

    def detener(self) -> None:
        self.corriendo.clear()
        if self.cam is not None:
            self.cam.cancelar()         # por si esta esperando un trigger externo
        hilo, self.hilo = self.hilo, None
        if hilo is not None:
            hilo.join(timeout=5.0)

    def _bucle(self) -> None:
        while self.corriendo.is_set():
            cam, cfg = self.cam, self.cfg
            if cam is None:
                return
            self.cola.drenar(cam)
            prom = Promedio()
            for _ in range(cfg.average):
                if not self.corriendo.is_set():
                    return
                try:
                    prom.agregar(cam.grab())
                except CapturaCancelada:
                    return
                except UC480Error:
                    pass
            f = prom.resultado()
            if f is None:
                continue
            f = apply_dark(f, self.dark, en_sitio=True)
            with self.lock:
                self.frame = f

    # --- serie de medidas ---

    def _refrescar_paso(self) -> None:
        """Escribe la instruccion de montaje del paso que toca."""
        listo = len(self.medidas) == len(self.pasos)
        if listo:
            self.lbl_paso.config(text="serie completa")
            self.lbl_montaje.config(
                text=f"Ya estan las {len(self.pasos)} medidas.\nPodes repetir alguna o exportar.")
        else:
            paso = self.pasos[self.paso_actual]
            self.lbl_paso.config(text=f"MEDIDA {paso.n} de {len(self.pasos)}")
            if self.metodo == "dinamico":
                self.lbl_montaje.config(
                    text=f"Cuarto de onda (gira):\n  {paso.qwp}\n\n"
                         f"Polarizador lineal (fijo):\n  0 grados\n\n{paso.formula}")
            else:
                self.lbl_montaje.config(
                    text=f"Cuarto de onda:\n  {paso.qwp}\n\n"
                         f"Polarizador lineal (analizador):\n  {paso.analizador:.0f} grados\n\n"
                         f"{paso.formula}")
        self.btn_tomar.config(state="disabled" if listo else "normal")
        self.btn_repetir.config(state="normal" if self.medidas else "disabled")
        self.btn_exportar.config(state="normal" if listo else "disabled")

        self.tabla.delete(*self.tabla.get_children())
        for i, paso in enumerate(self.pasos):
            estado = ("tomada" if paso.n in self.medidas else
                      "<- ahora" if i == self.paso_actual and not listo else "pendiente")
            qwp = paso.qwp.replace("eje rapido a ", "").split(",")[0]
            self.tabla.insert("", "end", iid=str(paso.n), values=(
                paso.n, qwp, f"{paso.analizador:.0f}d", estado))
        if not listo:
            self.tabla.see(str(self.pasos[self.paso_actual].n))

    def tomar(self) -> None:
        from tkinter import messagebox
        with self.lock:
            f = None if self.frame is None else self.frame.copy()
        if f is None:
            messagebox.showinfo("Sin imagen", "Inicia el visor primero.")
            return
        paso = self.pasos[self.paso_actual]
        # Se guarda la intensidad escalar: los Stokes se definen sobre la
        # potencia total, no sobre cada canal de color por separado.
        intensidad = f.mean(axis=2) if f.ndim == 3 else f
        previas = [m for n, m in self.medidas.items() if n != paso.n]
        if not serie_compatible(intensidad, previas):
            messagebox.showerror(
                "Tamano distinto",
                "Esta foto no tiene el tamano de las anteriores: cambio el binning o "
                "el submuestreo. Vuelve a ese ajuste o repite la serie.")
            return
        self.medidas[paso.n] = intensidad
        if self.paso_actual < len(self.pasos) - 1:
            self.paso_actual += 1
        self._recalcular()
        self._refrescar_paso()

    def repetir(self) -> None:
        """Vuelve al ultimo paso tomado para medirlo de nuevo."""
        if not self.medidas:
            return
        n = max(self.medidas)
        del self.medidas[n]
        self.paso_actual = n - 1
        self._recalcular()
        self._refrescar_paso()

    def _recalcular(self) -> None:
        """Rearma la imagen de polarizacion con lo que haya medido hasta ahora.

        Con las tres primeras ya se puede mostrar la parte lineal; S3 recien
        aparece con la cuarta, asi que hasta entonces se pasa como cero y se
        avisa que lo que se ve es solo la polarizacion lineal.
        """
        self._version += 1          # invalida los mapas cacheados
        self._cache.clear()
        if self.metodo == "dinamico":
            self._recalcular_dinamico()
            return
        if len(self.medidas) < 3:
            self.params = None
            self.lbl_pol.config(
                text=f"faltan {3 - len(self.medidas)} medidas para la parte lineal")
            return
        i1, i2, i3 = (self.medidas[n] for n in (1, 2, 3))
        completa = 4 in self.medidas
        i4 = self.medidas[4] if completa else 0.5 * (i1 + i3)   # S3 = 0
        self.params = parametros(stokes_desde_intensidades(i1, i2, i3, i4))
        self._mostrar_resumen("completa: S0..S3 con circular" if completa
                              else "parcial: falta la medida 4, S3 = 0")

    def _recalcular_dinamico(self) -> None:
        """Ajusta los Stokes con las tomas que haya, si ya alcanzan para separarlos.

        No hace falta esperar la serie entera: en cuanto las tomas hechas separan
        los cuatro parametros se muestra la imagen, y mejora con cada toma.
        """
        tomadas = sorted(self.medidas)
        angulos = [self.angulos[n - 1] for n in tomadas]
        if len(tomadas) < 4 or problema_de_tomas(angulos, self.retardo):
            self.params = None
            self.lbl_pol.config(
                text=f"{len(tomadas)} de {len(self.pasos)} tomas: todavia no alcanzan "
                     "para separar S0..S3", foreground=CYBER["tenue"])
            return
        stokes, residuo = stokes_dinamico([self.medidas[n] for n in tomadas], angulos,
                                          self.retardo)
        self.params = parametros(stokes)
        val = self._pixeles_con_senal()
        res = float(np.nanmean(residuo[val])) if val is not None and val.any() else 0.0
        texto = ("completa" if len(tomadas) == len(self.pasos)
                 else f"parcial: {len(tomadas)} de {len(self.pasos)} tomas")
        aviso = (f"\nresiduo del ajuste {res * 100:.1f}%: revisa los angulos de la lamina "
                 "o si la luz cambio" if res > UMBRAL_RESIDUO else "")
        self._mostrar_resumen(f"{texto}, residuo {res * 100:.1f}%", aviso)

    def _pixeles_con_senal(self) -> np.ndarray | None:
        p = self.params
        return p["S0"] > np.nanmax(p["S0"]) * UMBRAL_SENAL if np.nanmax(p["S0"]) > 0 else None

    def _mostrar_resumen(self, estado: str, aviso: str = "") -> None:
        """DOLP y azimut medios, y los avisos de que la serie no es consistente."""
        p = self.params
        val = self._pixeles_con_senal()
        hay = val is not None and val.any()
        dolp = float(np.nanmean(p["DOLP"][val])) if hay else 0.0
        azim = float(np.nanmean(p["azimut"][val])) if hay else 0.0
        # Un DOP mayor que 1 es fisicamente imposible: si aparece, las fotos no
        # corresponden al mismo estado. Tipicamente porque la luz cambio entre
        # tomas, algo se movio, o hubo pixeles saturados.
        dop_max = float(np.nanmax(p["DOP"][val])) if hay else 0.0
        if dop_max > 1.05:
            aviso += ("\n¡DOP max %.2f > 1! imposible: luz variable, algo se movio "
                      "o saturacion" % dop_max)
        self.lbl_pol.config(
            text=f"DOLP medio {dolp:.3f}   azimut medio {azim:.1f} deg\n{estado}{aviso}",
            foreground=CYBER["alerta"] if aviso else CYBER["tenue"])

    # --- modos de visualizacion ---

    def alternar_estados(self) -> None:
        self.modo_estados = not self.modo_estados
        self._refrescar_modos()

    def alternar_elipse(self) -> None:
        self.modo_elipse = not self.modo_elipse
        if not self.modo_elipse:
            self.pixel_sel = None
        self._refrescar_modos()

    def _refrescar_modos(self) -> None:
        self.btn_estados.config(
            text=f"Mapa de estados{'  ON' if self.modo_estados else ''}",
            style="Neon.TButton" if self.modo_estados else "TButton")
        self.btn_elipse.config(
            text=f"Elipse por pixel{'  ON' if self.modo_elipse else ''}",
            style="Neon.TButton" if self.modo_elipse else "TButton")
        # Se reempaquetan los tres en orden para conservar la posicion. Con la
        # leyenda a la vista sobra la linea explicativa: la leyenda se explica
        # sola y el alto hace falta.
        self.lbl_modo.pack_forget()
        self.canvas_elipse.pack_forget()
        self.canvas_leyenda.pack_forget()
        if not self.modo_estados:
            self.lbl_modo.config(text="tono = azimut   saturacion = DOLP\n"
                                      "brillo = intensidad S0")
            self.lbl_modo.pack(anchor="w", pady=(2, 0))
        if self.modo_elipse:
            self.canvas_elipse.pack(fill="x", pady=(6, 0))
        if self.modo_estados:
            self.canvas_leyenda.pack(fill="x", pady=(4, 0))
        self.root.after_idle(self._dibujar_auxiliares)

    def _mapa_pol(self) -> np.ndarray:
        """Imagen de polarizacion del modo activo, cacheada por version."""
        clave = ("mapa", self._version, self.modo_estados)
        if clave not in self._cache:
            self._cache.clear()
            self._cache[clave] = (imagen_estados(self.params) if self.modo_estados
                                  else imagen_falso_color(self.params))
        return self._cache[clave]

    def _codigos(self) -> np.ndarray:
        clave = ("cod", self._version)
        if clave not in self._cache:
            self._cache[clave] = clasificar(self.params)
        return self._cache[clave]

    def _on_click_pol(self, ev) -> None:
        """Elige el pixel cuya elipse se dibuja."""
        rect = self._disp.get("_pol_id")
        if not self.modo_elipse or self.params is None or not rect:
            return
        ox, oy, dw, dh = rect
        alto, ancho = self.params["S0"].shape[:2]
        x, y = int((ev.x - ox) * ancho / dw), int((ev.y - oy) * alto / dh)
        if 0 <= x < ancho and 0 <= y < alto:
            self.pixel_sel = (x, y)
            self._dibujar_elipse()

    def _dibujar_auxiliares(self) -> None:
        if self.modo_elipse and self.canvas_elipse.winfo_exists():
            self._dibujar_elipse()
        if self.modo_estados and self.canvas_leyenda.winfo_exists():
            self._dibujar_leyenda()

    def _dibujar_elipse(self) -> None:
        """Dibuja la elipse de polarizacion del pixel elegido."""
        c = self.canvas_elipse
        c.delete("all")
        cw, ch = c.winfo_width(), c.winfo_height()
        if cw < 40 or ch < 40:
            return
        cx, cy = cw / 2, ch / 2 + 6
        c.create_line(0, cy, cw, cy, fill=CYBER["borde"])
        c.create_line(cx, 12, cx, ch, fill=CYBER["borde"])

        if self.params is None or self.pixel_sel is None:
            c.create_text(cx, cy, text="click en la imagen de polarizacion",
                          fill=CYBER["tenue"], font=("Consolas", 8))
            return
        x, y = self.pixel_sel
        p = self.params
        s1, s2, s3 = (float(p[k][y, x]) for k in ("S1", "S2", "S3"))
        pts = elipse_puntos(s1, s2, s3)
        radio = float(np.abs(pts).max())
        if radio <= 0:
            c.create_text(cx, cy, text=f"({x},{y}) sin luz polarizada",
                          fill=CYBER["tenue"], font=("Consolas", 8))
            return

        k = (min(cw, ch) / 2 - 16) / radio
        # En pantalla la y crece hacia abajo; se invierte para que el sentido de
        # giro que se ve sea el real y no su espejo.
        coords = [v for px, py in pts for v in (cx + px * k, cy - py * k)]
        c.create_line(*coords, fill=CYBER["neon2"], width=2)
        # Flecha sobre el trazo: es lo que distingue derecha de izquierda.
        i = len(pts) // 8
        c.create_line(cx + pts[i, 0] * k, cy - pts[i, 1] * k,
                      cx + pts[i + 2, 0] * k, cy - pts[i + 2, 1] * k,
                      fill=CYBER["neon"], width=2, arrow="last")

        nombre = ESTADOS_CLASES[int(self._codigos()[y, x])][0]
        c.create_text(4, 2, anchor="nw", font=("Consolas", 8), fill=CYBER["neon"],
                      text=f"({x},{y})  {nombre}")
        dop = float(p["DOP"][y, x])
        c.create_text(4, ch - 4, anchor="sw", font=("Consolas", 8),
                      fill=CYBER["alerta"] if dop > 1.05 else CYBER["texto"],
                      text=f"azimut {p['azimut'][y, x]:.1f} deg   "
                           f"elipt {p['elipticidad'][y, x]:+.1f} deg\n"
                           f"DOP {dop:.3f}   "
                           f"giro {'derecha' if s3 >= 0 else 'izquierda'}")

    def _dibujar_leyenda(self) -> None:
        c = self.canvas_leyenda
        c.delete("all")
        cw = c.winfo_width()
        if cw < 40:
            return
        presentes = set(np.unique(self._codigos())) if self.params is not None else set()
        filas, ancho_col = 4, cw / 3
        for i, (nombre, (b, g, r)) in enumerate(ESTADOS_CLASES):
            px = 2 + (i // filas) * ancho_col
            py = 2 + (i % filas) * 14
            c.create_rectangle(px, py + 3, px + 10, py + 12,
                               fill=f"#{r:02x}{g:02x}{b:02x}", outline="")
            # Lo que no aparece en esta imagen se atenua, para no distraer.
            c.create_text(px + 14, py, anchor="nw", text=nombre, font=("Consolas", 7),
                          fill=CYBER["texto"] if i in presentes else CYBER["tenue"])

    # --- dibujo ---

    def _tick(self) -> None:
        self._tick_id = None
        t0 = time.perf_counter()
        if not self.canvas.winfo_exists():
            return                     # cambiamos de pantalla: el bucle termina
        with self.lock:
            f = self.frame
        # Solo con cuadro nuevo o cambio de vista: redibujar lo mismo le quitaba
        # procesador a la captura.
        # El nombre del canvas cambia al rehacer la pantalla, y ahi hay que redibujar.
        vista = (f is not None and f is self._ultimo_frame, self.realce_on, str(self.canvas),
                 self.canvas.winfo_width(), self.canvas.winfo_height())
        if f is not None and vista != self._ultima_vista:
            self._ultimo_frame, self._ultima_vista = f, (True,) + vista[1:]
            # Un frame reducido a la mitad alcanza: el canvas mide menos que eso.
            disp, realce = imagen_de_pantalla(f[::2, ::2], self.realce_on)
            self._pintar(self.canvas, disp, "_img_id")
            texto = f"Realzar en pantalla  x{realce:.1f}"
            if self.realce_on and self.chk_realce.cget("text") != texto:
                self.chk_realce.config(text=texto)
        if self.params is not None and self.canvas_pol.winfo_exists():
            mapa = self._mapa_pol()
            if self.modo_elipse and self.pixel_sel is not None:
                import cv2
                mapa = mapa.copy()
                cv2.drawMarker(mapa, self.pixel_sel, (255, 255, 255),
                               cv2.MARKER_CROSS, max(8, mapa.shape[1] // 40), 1)
            self._pintar(self.canvas_pol, mapa, "_pol_id")
        procesar_ajustes(self)
        self._tick_id = self.root.after(
            espera_hasta_proximo(t0, time.perf_counter(), self.INTERVALO_MS), self._tick)

    def _pintar(self, canvas, bgr: np.ndarray, attr: str) -> None:
        """Escala el BGR al canvas y lo dibuja, igual que el visor de camara.py.

        attr nombra el atributo donde vive el id del item y la PhotoImage: hay
        que guardarla, porque si no queda referencia Tk la libera y el canvas
        se ve en blanco.
        """
        import cv2
        h, w = bgr.shape[:2]
        cw, ch = canvas.winfo_width(), canvas.winfo_height()
        if cw < 10 or ch < 10:
            return
        k = min(cw / w, ch / h)
        dw, dh = max(1, int(w * k)), max(1, int(h * k))
        disp = cv2.resize(bgr, (dw, dh), interpolation=cv2.INTER_AREA)
        foto = self.tk.PhotoImage(data=_ppm(disp[:, :, ::-1]))
        setattr(self, attr + "_foto", foto)
        ox, oy = (cw - dw) // 2, (ch - dh) // 2
        self._disp[attr] = (ox, oy, dw, dh)     # para mapear clicks a pixeles
        ident = getattr(self, attr)
        if ident is None:
            setattr(self, attr, canvas.create_image(ox, oy, anchor="nw", image=foto))
        else:
            canvas.itemconfigure(ident, image=foto)
            canvas.coords(ident, ox, oy)

    # --- exportar ---

    def exportar(self) -> None:
        from tkinter import filedialog, messagebox
        if self.params is None or len(self.medidas) < len(self.pasos):
            messagebox.showinfo("Faltan medidas",
                                f"Completa las {len(self.pasos)} medidas primero.")
            return
        carpeta = filedialog.askdirectory(title="Carpeta donde exportar")
        if not carpeta:
            return
        try:
            extra = ({"angulo_qwp_deg": self.angulos,
                      "retardo_deg": np.float64(self.retardo)}
                     if self.metodo == "dinamico" else None)
            escritos = exportar(Path(carpeta), self.medidas, self.params,
                                self.var_formato.get(), extra)
        except (ValueError, OSError) as e:
            messagebox.showerror("No se pudo exportar", str(e))
            return
        self.lbl_export.config(text=f"{len(escritos)} archivos en {Path(carpeta).name}")
        messagebox.showinfo("Exportado", "\n".join(p.name for p in escritos))

    def cerrar(self) -> None:
        self._parar_tick()
        self.detener()
        if self.cam is not None:
            guardar_ultimos(self.cam, self.realce_on)
            self.cam.close()
        self.root.destroy()


def run_gui(cfg: Config) -> int:
    import tkinter as tk

    root = tk.Tk()
    try:
        _aplicar_estilo(root)
    except tk.TclError:
        pass
    PolarimetroGUI(root, cfg)
    root.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    argv = sys.argv[1:] if argv is None else argv
    if argv:
        # Sin esto, en el .exe sin consola sys.stderr es None y --self-test se cae
        # sin decir nada en cuanto pytest escribe ahi.
        _enganchar_consola()
    p = argparse.ArgumentParser(
        description="Polarimetro de imagen para camaras Thorlabs DCx e IDS uEye.")
    p.add_argument("--demo", action="store_true", help="patron sintetico, sin hardware")
    p.add_argument("--driver", default="auto", choices=["auto", *sorted(DRIVERS)],
                   help="con que driver abrir; auto prueba los dos")
    p.add_argument("-a", "--average", type=int, default=4,
                   help="frames promediados por medida")
    p.add_argument("--self-test", action="store_true", help="corre los tests y sale")
    args = p.parse_args(argv)

    if args.self_test:
        import pytest
        ruta = (str(Path(sys._MEIPASS) / "polarimetro.py")
                if getattr(sys, "frozen", False) else __file__)
        return pytest.main(["-q", "--no-header", ruta])

    return run_gui(Config(demo=args.demo, average=args.average, driver=args.driver))


# --- tests ---------------------------------------------------------------
# Corren sin camara: `uv run polarimetro.py --self-test`

def test_serie_no_mezcla_fotos_de_distinto_tamano():
    grande, chica = np.zeros((1024, 1280)), np.zeros((512, 640))
    assert serie_compatible(grande, [])
    assert serie_compatible(grande, [np.ones((1024, 1280))])
    assert not serie_compatible(chica, [grande])


# Estados de referencia, Goldstein cap. 5. (S0, S1, S2, S3) normalizados.
ESTADOS = {
    "lineal horizontal": (1, 1, 0, 0),
    "lineal vertical": (1, -1, 0, 0),
    "lineal +45": (1, 0, 1, 0),
    "lineal -45": (1, 0, -1, 0),
    "circular derecha": (1, 0, 0, 1),
    "circular izquierda": (1, 0, 0, -1),
    "sin polarizar": (1, 0, 0, 0),
}


def test_un_offset_sin_restar_infla_s0_y_baja_el_dop():
    # Por lo que avisa aviso_de_offset: S1, S2 y S3 son diferencias y no lo notan.
    import pytest
    s = np.array([100.0, 100.0, 0.0, 0.0])          # lineal horizontal, DOP 1
    limpio = stokes_desde_intensidades(*_medir(s))
    medido = stokes_desde_intensidades(*(i + 10.0 for i in _medir(s)))
    assert medido[0] == pytest.approx(limpio[0] + 20.0)
    assert medido[1:] == pytest.approx(limpio[1:])
    assert aviso_de_offset(30, None) and "S0" in aviso_de_offset(30, None)
    assert aviso_de_offset(30, np.zeros(1)) == "" and aviso_de_offset(0, None) == ""


def _medir(stokes):
    """Simula las cuatro medidas del metodo estatico sobre un estado conocido."""
    return [intensidad_teorica(stokes, p.analizador, 0.0 if p.qwp == "fuera" else 90.0)
            for p in PASOS_ESTATICO]


def test_la_inversion_recupera_cada_estado_de_referencia():
    for nombre, s in ESTADOS.items():
        recuperado = stokes_desde_intensidades(*_medir(s))
        assert np.allclose(recuperado, s, atol=1e-12), nombre


def test_los_pasos_siguen_el_orden_de_goldstein():
    """QWP fuera en las tres primeras; analizador a 0, 45, 90 y 45."""
    assert [p.analizador for p in PASOS_ESTATICO] == [0.0, 45.0, 90.0, 45.0]
    assert [p.qwp == "fuera" for p in PASOS_ESTATICO] == [True, True, True, False]


def test_intensidad_teorica_es_la_ley_de_malus_sin_retardador():
    """Con luz lineal horizontal y sin QWP, I(theta) = cos^2(theta)/1."""
    for grados in (0, 30, 45, 60, 90):
        i = intensidad_teorica((1, 1, 0, 0), grados, 0.0)
        assert np.isclose(i, np.cos(np.deg2rad(grados)) ** 2)


def test_grado_de_polarizacion_de_los_estados_puros_y_del_despolarizado():
    assert np.isclose(parametros(ESTADOS["lineal horizontal"])["DOP"], 1.0)
    assert np.isclose(parametros(ESTADOS["circular derecha"])["DOP"], 1.0)
    assert np.isclose(parametros(ESTADOS["sin polarizar"])["DOP"], 0.0)
    mezcla = (2, 1, 0, 0)                      # mitad polarizada
    assert np.isclose(parametros(mezcla)["DOP"], 0.5)


def test_azimut_y_elipticidad_de_los_estados_conocidos():
    p = parametros(ESTADOS["lineal horizontal"])
    assert np.isclose(p["azimut"], 0.0) and np.isclose(p["elipticidad"], 0.0)
    assert np.isclose(parametros(ESTADOS["lineal +45"])["azimut"], 45.0)
    assert np.isclose(parametros(ESTADOS["lineal vertical"])["azimut"], 90.0)
    # circular: elipticidad +-45 grados, y el signo da el sentido de giro
    assert np.isclose(parametros(ESTADOS["circular derecha"])["elipticidad"], 45.0)
    assert np.isclose(parametros(ESTADOS["circular izquierda"])["elipticidad"], -45.0)


def test_dolp_y_docp_separan_la_parte_lineal_de_la_circular():
    p = parametros(ESTADOS["lineal +45"])
    assert np.isclose(p["DOLP"], 1.0) and np.isclose(p["DOCP"], 0.0)
    p = parametros(ESTADOS["circular derecha"])
    assert np.isclose(p["DOLP"], 0.0) and np.isclose(p["DOCP"], 1.0)


def test_funciona_pixel_a_pixel_sobre_imagenes():
    """La misma cuenta sobre una imagen con dos mitades de distinta polarizacion."""
    alto, ancho = 4, 6
    s = np.zeros((4, alto, ancho))
    s[0] = 1.0
    s[1, :, :3] = 1.0          # mitad izquierda: lineal horizontal
    s[2, :, 3:] = 1.0          # mitad derecha: lineal a 45
    medidas = [intensidad_teorica(s, p.analizador,
                                  0.0 if p.qwp == "fuera" else 90.0)
               for p in PASOS_ESTATICO]
    p = parametros(stokes_desde_intensidades(*medidas))
    assert p["azimut"].shape == (alto, ancho)
    assert np.allclose(p["azimut"][:, :3], 0.0)
    assert np.allclose(p["azimut"][:, 3:], 45.0)
    assert np.allclose(p["DOLP"], 1.0)


def test_pixeles_sin_luz_no_generan_nan():
    """S0 = 0 daria 0/0; tiene que salir 0 y no contaminar la imagen."""
    p = parametros(np.zeros((4, 3, 3)))
    for clave in ("DOP", "DOLP", "DOCP", "azimut", "elipticidad"):
        assert np.isfinite(p[clave]).all(), clave


def test_falso_color_es_bgr_de_8_bits():
    s = np.zeros((4, 5, 7))
    s[0] = 100.0
    s[1] = 100.0                                # lineal horizontal, DOLP 1
    img = imagen_falso_color(parametros(s))
    assert img.shape == (5, 7, 3) and img.dtype == np.uint8


def _area_orientada(pts):
    """Area con signo (formula del zapatero): el signo da el sentido de giro."""
    x, y = pts[:, 0], pts[:, 1]
    return 0.5 * float(np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]))


def test_la_elipse_de_luz_lineal_es_un_segmento_en_su_azimut():
    pts = elipse_puntos(1, 0, 0)                  # lineal horizontal
    assert np.allclose(pts[:, 1], 0.0, atol=1e-12)     # todo sobre el eje x
    assert np.isclose(np.abs(pts[:, 0]).max(), 1.0)    # semieje = sqrt(Ipol)

    pts = elipse_puntos(0, 1, 0)                  # lineal a 45 grados
    assert np.allclose(pts[:, 1], pts[:, 0], atol=1e-12)   # sobre la recta y = x


def test_la_elipse_de_luz_circular_es_una_circunferencia():
    pts = elipse_puntos(0, 0, 1)
    radios = np.hypot(pts[:, 0], pts[:, 1])
    assert np.allclose(radios, radios[0])
    assert np.isclose(radios[0], np.sqrt(0.5))    # A = B = sqrt(Ipol/2)


def test_el_signo_de_s3_invierte_el_sentido_de_giro():
    derecha = _area_orientada(elipse_puntos(0, 0, 1))
    izquierda = _area_orientada(elipse_puntos(0, 0, -1))
    assert derecha > 0 > izquierda
    assert np.isclose(derecha, -izquierda)        # espejo exacto


def test_la_elipse_general_tiene_los_semiejes_de_la_elipticidad():
    s1, s2, s3 = 0.6, 0.0, 0.8                    # chi = asin(0.8)/2
    pts = elipse_puntos(s1, s2, s3)
    radios = np.hypot(pts[:, 0], pts[:, 1])
    ip = np.sqrt(s1 ** 2 + s2 ** 2 + s3 ** 2)
    chi = 0.5 * np.arcsin(s3 / ip)
    assert np.isclose(radios.max(), np.sqrt(ip) * np.cos(chi))
    assert np.isclose(radios.min(), np.sqrt(ip) * np.sin(abs(chi)))
    # los semiejes cumplen la relacion que los define
    assert np.isclose(radios.max() ** 2 + radios.min() ** 2, ip)


def test_elipse_de_luz_sin_polarizar_no_explota():
    assert np.allclose(elipse_puntos(0, 0, 0), 0.0)


def test_clasificar_reconoce_cada_estado_de_referencia():
    esperado = {"lineal horizontal": 2, "lineal +45": 3, "lineal vertical": 4,
                "lineal -45": 5, "circular derecha": 8, "circular izquierda": 9,
                "sin polarizar": 1}
    for nombre, s in ESTADOS.items():
        # como imagen de 1 pixel, que es como lo usa la GUI
        img = np.array([[float(c)] for c in s]).reshape(4, 1, 1)
        codigo = int(clasificar(parametros(img))[0, 0])
        assert codigo == esperado[nombre], f"{nombre}: dio {codigo}"


def test_clasificar_marca_como_sin_señal_lo_que_esta_en_el_piso():
    s = np.zeros((4, 1, 4))
    s[0] = [[100.0, 100.0, 1.0, 0.0]]     # los dos ultimos, muy por debajo
    s[1] = [[100.0, 100.0, 1.0, 0.0]]     # lineal horizontal donde hay luz
    codigo = clasificar(parametros(s))[0]
    assert list(codigo[:2]) == [2, 2]     # lineal H donde llego señal
    assert list(codigo[2:]) == [0, 0]     # sin señal donde no


def test_clasificar_separa_eliptica_de_circular_por_elipticidad():
    import math
    # chi = 22.5 grados: entre los dos umbrales, tiene que dar eliptica
    chi = math.radians(22.5)
    s = np.array([1.0, math.cos(2 * chi), 0.0, math.sin(2 * chi)]).reshape(4, 1, 1)
    assert int(clasificar(parametros(s))[0, 0]) == 6          # eliptica derecha
    s[3] *= -1
    assert int(clasificar(parametros(s))[0, 0]) == 7          # eliptica izquierda


def test_imagen_estados_usa_la_paleta_y_cubre_toda_la_imagen():
    s = np.zeros((4, 3, 2))
    s[0] = 100.0
    s[1] = 100.0
    img = imagen_estados(parametros(s))
    assert img.shape == (3, 2, 3) and img.dtype == np.uint8
    assert tuple(int(v) for v in img[0, 0]) == ESTADOS_CLASES[2][1]   # lineal H


def test_exportar_escribe_fotos_stokes_y_falso_color(tmp_path):
    s = np.zeros((4, 4, 4))
    s[0] = 200.0
    s[1] = 200.0
    medidas = {p.n: np.asarray(i, dtype=np.float64) for p, i in
               zip(PASOS_ESTATICO, [intensidad_teorica(s, p.analizador,
                                                       0.0 if p.qwp == "fuera" else 90.0)
                                    for p in PASOS_ESTATICO])}
    p = parametros(stokes_desde_intensidades(*[medidas[n] for n in (1, 2, 3, 4)]))
    escritos = exportar(tmp_path, medidas, p, ".png")

    nombres = [f.name for f in escritos]
    assert sum(n.startswith("medida") and n.endswith(".png") for n in nombres) == 4
    assert any(n.endswith("_stokes.npz") for n in nombres)
    assert any(n.endswith("_falsocolor.png") for n in nombres)
    guardado = np.load([f for f in escritos if f.suffix == ".npz"][0])
    assert np.allclose(guardado["S1"], 200.0)       # float64, sin cuantizar
    assert guardado["azimut"].dtype == np.float64


# --- metodo dinamico ---

def _tomas(stokes, angulos, retardo=RETARDO_QWP):
    """Simula las tomas del dinamico con el producto de Mueller, no con la formula."""
    return [fila_dinamica(t, retardo) @ np.asarray(stokes, dtype=np.float64) for t in angulos]


def test_el_dinamico_usa_la_misma_convencion_que_el_estatico():
    # El paso 4 del estatico armado con matrices de Mueller (lamina a 0, analizador
    # a 45) da lo mismo que la (6-6): el signo de S3 es el mismo en los dos metodos.
    for nombre, s in ESTADOS.items():
        s = np.array(s, dtype=np.float64)
        m = (mueller_polarizador(45.0) @ mueller_retardador(0.0, 90.0) @ s)[0]
        assert np.isclose(m, intensidad_teorica(s, 45.0, 90.0)), nombre
        for t in (0.0, 30.0, 90.0):          # sin lamina, Malus
            assert np.isclose((mueller_polarizador(t) @ s)[0], intensidad_teorica(s, t, 0.0))


def test_la_fila_dinamica_es_la_modulacion_de_goldstein():
    rng = np.random.default_rng(7)
    for _ in range(20):
        s = rng.normal(size=4)
        a, b, c, d = s[0] + s[1] / 2, s[3], s[1] / 2, s[2] / 2
        t = np.deg2rad(rng.uniform(0, 360))
        esperado = 0.5 * (a - b * np.sin(2 * t) + c * np.cos(4 * t) + d * np.sin(4 * t))
        assert np.isclose(fila_dinamica(np.rad2deg(t)) @ s, esperado)


def test_con_pasos_iguales_da_las_sumas_de_fourier_del_libro():
    for n, recorrido in ((16, 180.0), (36, 360.0)):
        ang = angulos_dinamicos(n, 0.0, recorrido)
        s = np.array([2.0, 0.3, -0.5, 0.8])
        i = np.array(_tomas(s, ang))
        t = np.deg2rad(ang)
        a = 2 / n * i.sum()
        b = -4 / n * (i * np.sin(2 * t)).sum()
        c = 4 / n * (i * np.cos(4 * t)).sum()
        d = 4 / n * (i * np.sin(4 * t)).sum()
        fourier = np.array([a - c, 2 * c, 2 * d, b])
        ajuste, _ = stokes_dinamico(i, ang)
        assert np.allclose(ajuste, fourier) and np.allclose(ajuste, s)


def test_el_dinamico_recupera_cada_estado_de_referencia():
    series = [(n, 360.0) for n in (5, 7, 9, 16, 36)] + [(n, 180.0) for n in (5, 8, 12)]
    for n, recorrido in series:
        for inicio in (0.0, 37.5, 350.0):
            ang = angulos_dinamicos(n, inicio, recorrido)
            for nombre, s in ESTADOS.items():
                got, residuo = stokes_dinamico(_tomas(s, ang), ang)
                assert np.allclose(got, s, atol=1e-9), (nombre, n, recorrido, inicio)
                assert residuo < 1e-9
                dop = parametros(got)["DOP"]
                assert np.isclose(dop, 0.0 if nombre == "sin polarizar" else 1.0, atol=1e-9)


def test_los_angulos_vuelven_a_contar_desde_cero_al_pasar_360():
    ang = angulos_dinamicos(8, 300.0, 360.0)
    assert np.allclose(ang[:3], [300.0, 345.0, 30.0])
    assert ((0 <= ang) & (ang < 360)).all()
    assert np.allclose(angulos_dinamicos(4, 0.0, 180.0), [0.0, 45.0, 90.0, 135.0])


def test_no_todo_numero_de_tomas_separa_los_cuatro_stokes():
    def sirve(n, r):
        return problema_de_tomas(angulos_dinamicos(n, 0.0, r)) == ""
    assert not any(sirve(n, 360.0) for n in (4, 6, 8))     # sin 4theta se anula o se pliega
    assert all(sirve(n, 360.0) for n in (5, 7, 9, 10, 16))
    assert not sirve(4, 180.0) and all(sirve(n, 180.0) for n in (5, 6, 8))
    assert tomas_validas_cercanas(8, 360.0) in (7, 9)
    assert tomas_validas_cercanas(4, 180.0) == 5
    import pytest
    with pytest.raises(ValueError):
        stokes_dinamico([1.0] * 8, angulos_dinamicos(8, 0.0, 360.0))


def test_un_retardo_mal_supuesto_sesga_los_stokes_sin_dejar_residuo():
    # Con otro retardo la modulacion tiene los mismos terminos (1, sin2t, cos4t,
    # sin4t), solo con otros pesos: el ajuste cierra perfecto y los Stokes salen
    # corridos. Por eso el retardo de la lamina hay que conocerlo; el residuo no avisa.
    s = np.array([1.0, 0.2, -0.4, 0.7])
    ang = angulos_dinamicos(16, 0.0, 360.0)
    medidas = _tomas(s, ang, retardo=80.0)
    bien, res_bien = stokes_dinamico(medidas, ang, 80.0)
    mal, res_mal = stokes_dinamico(medidas, ang, 90.0)
    assert np.allclose(bien, s) and res_bien < 1e-9
    assert not np.allclose(mal, s, atol=0.02) and res_mal < 1e-9


def test_el_residuo_crece_con_un_angulo_mal_puesto():
    s = np.array([1.0, 0.3, 0.5, 0.6])
    ang = angulos_dinamicos(16, 0.0, 360.0)
    residuos = []
    for error in (0.0, 2.0, 10.0):           # la lamina quedo corrida en una toma
        puestos = ang.copy()
        puestos[5] += error
        residuos.append(float(stokes_dinamico(_tomas(s, puestos), ang)[1]))
    assert residuos[0] < 1e-12 < residuos[1] < residuos[2]


def test_el_dinamico_funciona_pixel_a_pixel_y_sin_luz_no_da_nan():
    ang = angulos_dinamicos(9, 0.0, 360.0)
    s = np.zeros((4, 2, 3))
    s[:, 0, 0] = ESTADOS["circular derecha"]
    s[:, 1, 2] = ESTADOS["lineal +45"]
    imgs = [np.tensordot(fila_dinamica(t), s, axes=1) for t in ang]
    got, residuo = stokes_dinamico(imgs, ang)
    assert got.shape == (4, 2, 3) and np.allclose(got, s, atol=1e-9)
    assert not np.isnan(residuo).any() and not np.isnan(parametros(got)["DOP"]).any()


def test_los_pasos_dinamicos_dejan_el_polarizador_fijo():
    pasos = pasos_dinamicos(angulos_dinamicos(5, 350.0, 360.0))
    assert [p.n for p in pasos] == [1, 2, 3, 4, 5]
    assert all(p.analizador == 0.0 for p in pasos)
    assert "350.0" in pasos[0].qwp and "62.0" in pasos[1].qwp


def test_exportar_guarda_los_angulos_de_la_lamina(tmp_path):
    ang = angulos_dinamicos(5, 0.0, 360.0)
    medidas = {k + 1: np.full((2, 2), v) for k, v in enumerate(_tomas((1, 1, 0, 0), ang))}
    stokes, _ = stokes_dinamico([medidas[n] for n in sorted(medidas)], ang)
    escritos = exportar(tmp_path, medidas, parametros(stokes), ".npy",
                        {"angulo_qwp_deg": ang, "retardo_deg": np.float64(90.0)})
    guardado = np.load([f for f in escritos if f.suffix == ".npz"][0])
    assert np.allclose(guardado["angulo_qwp_deg"], ang) and guardado["retardo_deg"] == 90.0
    assert sum(f.name.startswith("medida") for f in escritos) == 5


def test_todos_los_formatos_de_foto_son_validos():
    """El combo de exportacion no debe ofrecer nada que save_frame_as rechace."""
    assert {".npy", ".tiff", ".png", ".jpg"} <= set(FORMATOS)


if __name__ == "__main__":
    raise SystemExit(main())
