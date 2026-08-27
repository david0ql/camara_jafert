# camara-jafert

Visor en vivo para cámaras Thorlabs DCx con lectura RGB de precisión decimal,
pensado para fotometría de polarización. Desarrollado y probado contra una
**DCU224C** (1280x1024, sensor color, píxel de 4.65 um).

Todo está en `polarcam.py`. Las dependencias van declaradas en el propio script
(PEP 723), así que `uv` arma el entorno solo: no hay que crear venv ni instalar
nada a mano.

## Uso

Sin argumentos abre un **menu interactivo** con navegacion por flechas:

```bash
uv run polarcam.py
```

```
  Que queres hacer?
> Visor en vivo         medir con el mouse
  Serie de medidas      un punto por angulo
  Capturar dark frame   con la tapa puesta
  Ajustar parametros    binning, exposicion, ROI
  Diagnostico USB       tasa de error por pixel clock
  Salir
```

Con argumentos se salta el menu, para scripts:

```bash
uv run polarcam.py --viewer -b 4 -g 0 -a 8 -R 5
uv run polarcam.py --demo            # sin camara conectada
uv run polarcam.py --self-test       # tests
uv run polarcam.py --benchmark       # tasa de error del enlace USB
```

## Binning por hardware

El CCD suma la carga de varias filas **antes del ADC**, asi que la mejora es
analogica y ocurre antes del ruido de lectura. Medido sobre una DCU224C a 20 ms:

| Modo | Alto | SNR relativo |
|------|-----:|-------------:|
| sin binning | 1024 | 1.00x |
| 2x vertical | 512 | 1.62x |
| 3x vertical | 341 | 2.22x |
| 4x vertical | 256 | 2.34x |

Cuesta resolucion vertical, que para medir un punto no importa.

**Advertencia:** el sensor es Bayer y las filas alternan filtros de color, asi
que al binear se mezclan. Con binning los canales R/G/B dejan de ser colores
limpios. Para fuente monocromatica da igual; para polarimetria resuelta en
color hay que usar binning 1x.

## Dark frame

El sensor tiene un piso de oscuridad distinto en cada canal (del orden de
25/19/13 en la DCU224C). Ese offset es sistematico: no se va promediando y
aplasta el contraste cerca de la extincion. El menu tiene la opcion de
capturarlo con la tapa puesta; queda en `dark.npy` y se resta automaticamente.
En el visor se activa y desactiva con la tecla `d`.

## De dónde sale la precisión sub-nivel

La primera y más eficaz ganancia **la aporta la cámara**, no el software: el
binning suma la carga de varias filas **dentro del CCD, antes del ADC**. Es una
operación analógica que ocurre antes del ruido de lectura, y por eso rinde más
que cualquier procesamiento posterior. Medido sobre esta DCU224C: **2.34× de
relación señal-ruido** con binning 4x. Eso es hardware puro.

Sobre esa base, el programa suma señal en dos etapas más:

| Etapa | Dónde ocurre | Ganancia |
|-------|--------------|----------|
| Binning | en el CCD, antes del ADC | 2.34× SNR (medido) |
| Promedio espacial del ROI | (2r+1)² píxeles | ÷√n en el error |
| Promedio temporal | varios frames (`-a`) | ÷√N en el error |

Conviene saber qué entrega el sensor para no atribuirle de más: cada fotosito
sale del ADC como un **entero de 0 a 255**. No hay mayor profundidad
disponible — los modos de 10, 12 y 16 bits los rechaza con
`IS_INVALID_COLOR_FORMAT`, verificado sobre la cámara. Ningún ADC entrega
fracciones; los decimales aparecen al sumar señal, que es el procedimiento
estándar en fotometría cuantitativa.

Lo que importa para un ajuste es la **incertidumbre**, y el CSV la guarda: el
error estándar de la media por canal. Con binning 4x, ROI de 11×11 y 8 frames
baja al orden de **0.02 niveles**, es decir precisión equivalente a unos 12
bits partiendo de un sensor de 8. Se paga en tiempo, no en resolución.

## Respuesta lineal

Al abrir la cámara se apagan gamma (queda en 1.0), gamma de hardware,
auto-ganancia, auto-exposición, balance de blancos automático, corrección de
color, gain boost y compensación de negro. Cualquiera de esas etapas rompe la
proporcionalidad entre señal e intensidad incidente y deforma una curva de
Malus. Por el mismo motivo conviene subir exposición antes que ganancia.

El sensor tiene un piso de oscuridad de unos 19 niveles, distinto por canal
(R 25, G 19, B 13 con la tapa puesta). Para medidas absolutas hay que capturar
un dark frame y restarlo.

## Plataformas

La captura necesita el SDK **DCx Camera Support** de Thorlabs, que existe para
Windows y Linux/amd64 únicamente. **No hay driver para macOS**, ni de Thorlabs
ni de IDS, que es el fabricante original de estas cámaras.

| | Captura | Demo, tests y análisis |
|---|---|---|
| Windows + SDK | sí | sí |
| macOS | no | sí |
| Linux amd64 | requiere adaptar el cargador a `libueye_api.so` | sí |

El módulo declara los tipos de Windows a mano en lugar de importar
`ctypes.wintypes`, así que se importa en cualquier sistema. Fuera de Windows,
`UC480Camera` falla con un mensaje explícito y el resto del programa funciona
igual: `--demo`, `--self-test` y cualquier análisis sobre CSV ya guardados.

Portar el driver a macOS no es recompilar: el protocolo USB de uEye es
propietario y sin documentar, y el driver le sube firmware a la cámara al
conectarla (el PID cambia de `1000` a `2240` en ese momento). Sería un
proyecto de ingeniería inversa, no una adaptación.

Aparte del SDK, lo único que hace falta es [uv](https://docs.astral.sh/uv/).

## Nota sobre el enlace USB

La DCU224C mueve 3.93 MB por frame en color sobre USB 2.0. En el equipo donde
se desarrolló esto, `is_FreezeVideo` devuelve `IS_TRANSFER_ERROR` de forma
intermitente en buena parte de las capturas, y la tasa de error escala con los
bytes por frame en lugar de con la velocidad, lo que apunta al cable o al
conector más que al ancho de banda. El visor lo absorbe con reintentos y
descartando frames, y muestra la tasa de pérdida en pantalla.

`--benchmark` mide esa tasa para cada pixel clock; sirve para comparar cables
con números en vez de intuición. Un enlace sano debería dar ~0% en todas las
filas.
