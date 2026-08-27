# camara-jafert

Visor en vivo para cámaras Thorlabs DCx con lectura RGB de precisión decimal,
pensado para fotometría de polarización. Desarrollado y probado contra una
**DCU224C** (1280x1024, sensor color, píxel de 4.65 um).

Todo está en `polarcam.py`. Las dependencias van declaradas en el propio script
(PEP 723), así que `uv` arma el entorno solo:

```bash
uv run polarcam.py
```

## Uso

```bash
uv run polarcam.py                  # visor en vivo
uv run polarcam.py --demo           # patrón sintético, sin cámara conectada
uv run polarcam.py --self-test      # tests
uv run polarcam.py --benchmark      # tasa de error del enlace USB
```

Para tomar datos en serio, ganancia en cero y promediando:

```bash
uv run polarcam.py -g 0 -a 8 -R 5
```

### Controles

| Tecla | Acción |
|-------|--------|
| mouse | mide donde apunta |
| click | fija el punto de medición, o lo suelta |
| `+` `-` | exposición ×1.25 / ÷1.25 |
| `[` `]` | achica o agranda el ROI |
| `a` | promediado temporal 1 → 2 → 4 → 8 → 16 |
| `s` | guarda la muestra en el CSV |
| `r` | vuelve a los valores iniciales |
| `q` / `Esc` | salir |

El recuadro del ROI se pone rojo cuando hay píxeles saturados. Un píxel
clipeado arruina el ajuste, así que conviene bajar exposición antes de medir.

## De dónde salen los decimales

Un píxel es un entero de 0 a 255 y no tiene decimales que dar. Lo que se
reporta es el promedio sobre un parche de (2r+1)² píxeles, opcionalmente
promediado también sobre varios frames (`-a`). Eso resuelve modulación por
debajo de 1 LSB, que es la escala relevante cerca de la extinción, y la
desviación estándar que acompaña a cada canal dice cuánto ruido tiene la medida.

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
