#SYNTHPY

Sintetizador polifónico **standalone** de terminal. Sin DAW, sin plugin host,
sin navegador — un solo script Python que abre un stream de audio real y una
TUI cyberpunk (verde fósforo, monoespaciado).

Motor: 5 formas de onda (sine/saw/square/triangle/noise), ADSR completo,
filtro pasabajas de un polo con resonancia, hasta 16 voces polifónicas,
tocado en vivo desde el teclado QWERTY.

## Instalación

### Termux (Android)

```bash
pkg update
pkg install python portaudio
pip install numpy sounddevice --break-system-packages
```

Si `pip install sounddevice` falla compilando en arm64 (a veces pasa en
Termux porque no hay wheel prebuilt), instala PortAudio del sistema con
`pkg install portaudio` **antes** del pip install — sounddevice se linkea
contra esa lib. Si sigue sin poder, `pkg install python-numpy` en vez del
pip de numpy suele ser más confiable en Termux.

### Linux / macOS

```bash
pip3 install numpy sounddevice
```

(En Linux si no tienes PortAudio: `sudo apt install libportaudio2` o
equivalente de tu distro.)

## Uso

```bash
python3 perron_synth.py
```

### Controles

| Tecla | Función |
|---|---|
| `z s x d c v g b n m , .` | Notas, octava actual |
| `q 2 w 3 e r 5 t 6 y 7 u` | Notas, octava+1 |
| `-` / `=` | Octava abajo / arriba |
| `Tab` | Cicla forma de onda |
| `a` / `A` | Attack − / + |
| `f` / `F` | Decay − / + |
| `h` / `H` | Sustain − / + |
| `j` / `J` | Release − / + |
| `↑` / `↓` | Cutoff del filtro |
| `←` / `→` | Resonancia |
| `Esc` / `Ctrl+C` | Salir |

## Nota técnica sobre el "note off"

Las terminales no mandan evento de "tecla soltada" — solo el auto-repeat
del sistema operativo mientras la mantienes presionada. Este synth usa
ese repeat como proxy: si no llega otro repeat de esa tecla en ~140ms,
se dispara `note_off` automáticamente (release del ADSR). Es el mismo
truco que usan los trackers de teclado tipo keyjazz. Si tu terminal tiene
el repeat muy lento o desactivado, las notas van a sonar más cortas de
lo esperado — puedes subir `RELEASE_TIMEOUT_MS` en el código si hace falta.

## Rendimiento

El filtro por-voz corre con un loop en Python puro (no vectorizado) porque
es un IIR recursivo — en un teléfono con pocas voces activas a la vez
(tocando en vivo, normalmente 2-6 notas simultáneas) no debería glitchear.
Si notas underruns con muchas voces sostenidas al mismo tiempo, baja
`MAX_VOICES` o `BLOCK_SIZE` en el código.
