#!/usr/bin/env python3
"""
PERRÓN SYNTH — sintetizador polifónico standalone, terminal-only.
Sin DAW, sin navegador. Corre en cualquier lado con Python3 + numpy + sounddevice
(Linux, macOS, Termux/Android).

Controles:
  Notas (fila baja, octava N):   z s x d c v g b n m , .
  Notas (fila alta, octava N+1): q 2 w 3 e r 5 t 6 y 7 u
  -  / =        octava abajo / arriba
  Tab           cicla forma de onda (sine, saw, square, triangle, noise)
  a / A         attack  -  / +
  f / F         decay   -  / +
  h / H         sustain -  / +
  j / J         release -  / +
  Flecha Arriba/Abajo    cutoff del filtro +/-
  Flecha Izq/Der         resonancia -/+
  Ctrl+C o Esc           salir

Nota técnica: las terminales no mandan "key up", solo "key repeat" mientras
la tecla está presionada. Este synth usa el repeat del OS como proxy de nota
sostenida: si no llega un repeat de esa tecla en RELEASE_TIMEOUT_MS, se manda
note_off automático. Es el mismo truco que usan los trackers de teclado.
"""

import curses
import numpy as np
import sounddevice as sd
import threading
import time
import sys

SAMPLE_RATE = 44100
BLOCK_SIZE = 256
MAX_VOICES = 16
RELEASE_TIMEOUT_MS = 140

WAVEFORMS = ['sine', 'saw', 'square', 'triangle', 'noise']

KEY_MAP_LOW = {
    'z': 0, 's': 1, 'x': 2, 'd': 3, 'c': 4, 'v': 5,
    'g': 6, 'b': 7, 'n': 8, 'm': 9, ',': 10, '.': 11,
}
KEY_MAP_HIGH = {
    'q': 12, '2': 13, 'w': 14, '3': 15, 'e': 16, 'r': 17,
    '5': 18, 't': 19, '6': 20, 'y': 21, '7': 22, 'u': 23,
}
NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def osc(waveform, phase):
    """phase: numpy array of unwrapped phase in cycles (float)."""
    frac = phase - np.floor(phase)
    if waveform == 'sine':
        return np.sin(2 * np.pi * frac)
    if waveform == 'saw':
        return 2.0 * frac - 1.0
    if waveform == 'square':
        return np.where(frac < 0.5, 1.0, -1.0)
    if waveform == 'triangle':
        return 2.0 * np.abs(2.0 * frac - 1.0) - 1.0
    if waveform == 'noise':
        return np.random.uniform(-1.0, 1.0, size=frac.shape)
    return np.zeros_like(frac)


class Voice:
    __slots__ = ('note', 'freq', 'phase', 'stage', 'level', 'age', 'z1')

    def __init__(self):
        self.note = None
        self.freq = 0.0
        self.phase = 0.0
        self.stage = 'idle'   # idle, attack, decay, sustain, release
        self.level = 0.0
        self.age = 0
        self.z1 = 0.0


class Synth:
    def __init__(self):
        self.voices = [Voice() for _ in range(MAX_VOICES)]
        self.waveform = 'saw'
        self.octave = 4
        self.attack = 0.01
        self.decay = 0.18
        self.sustain = 0.7
        self.release = 0.25
        self.cutoff = 0.55      # normalized 0..1
        self.resonance = 0.15   # normalized 0..1
        self.master_gain = 0.30
        self.peak = 0.0
        self.lock = threading.Lock()

    def note_freq(self, note):
        # note 0 == C in self.octave; MIDI-style mapping around A4=440
        midi = 12 * (self.octave + 1) + note
        return 440.0 * (2.0 ** ((midi - 69) / 12.0))

    def note_on(self, note_id, octave_used):
        midi = 12 * (octave_used + 1) + (note_id % 12)
        freq = 440.0 * (2.0 ** ((midi - 69) / 12.0))
        key = (note_id, octave_used)
        with self.lock:
            v = None
            for cand in self.voices:
                if getattr(cand, 'note', None) == key:
                    v = cand
                    break
            if v is None:
                idle = [c for c in self.voices if c.stage == 'idle']
                v = idle[0] if idle else max(self.voices, key=lambda c: c.age)
            v.note = key
            v.freq = freq
            v.stage = 'attack'
            v.age = 0

    def note_off(self, note_id, octave_used):
        key = (note_id, octave_used)
        with self.lock:
            for v in self.voices:
                if v.note == key and v.stage not in ('idle', 'release'):
                    v.stage = 'release'

    def render(self, frames):
        out = np.zeros(frames, dtype=np.float32)
        dt = 1.0 / SAMPLE_RATE
        with self.lock:
            cutoff_hz = 60.0 + (self.cutoff ** 2) * 9000.0
            rc = 1.0 / (2 * np.pi * cutoff_hz)
            alpha = dt / (rc + dt)
            res_amt = self.resonance * 3.2
            att = max(self.attack, 0.001)
            dec = max(self.decay, 0.001)
            rel = max(self.release, 0.001)

            for v in self.voices:
                if v.stage == 'idle':
                    continue
                v.age += frames
                phase_inc = v.freq * dt
                idx = np.arange(frames)
                phases = v.phase + phase_inc * idx
                raw = osc(self.waveform, phases)
                v.phase = (v.phase + phase_inc * frames) % 1.0

                # envelope: block-linear approximation (cheap, stable on ARM)
                level = v.level
                if v.stage == 'attack':
                    target = 1.0
                    step = (target - level) if att <= (frames * dt) else \
                        (frames * dt) / att
                    env_end = min(1.0, level + step) if att > (frames*dt) else 1.0
                    env = np.linspace(level, env_end, frames, dtype=np.float32)
                    level = env_end
                    if level >= 0.999:
                        v.stage = 'decay'
                elif v.stage == 'decay':
                    total_drop = 1.0 - self.sustain
                    step = (frames * dt / dec) * total_drop
                    env_end = max(self.sustain, level - step)
                    env = np.linspace(level, env_end, frames, dtype=np.float32)
                    level = env_end
                    if level <= self.sustain + 1e-4:
                        v.stage = 'sustain'
                elif v.stage == 'sustain':
                    level = self.sustain
                    env = np.full(frames, level, dtype=np.float32)
                elif v.stage == 'release':
                    step = (frames * dt / rel) * max(self.sustain, 0.001)
                    env_end = max(0.0, level - step)
                    env = np.linspace(level, env_end, frames, dtype=np.float32)
                    level = env_end
                    if level <= 1e-4:
                        v.stage = 'idle'
                        v.note = None
                        level = 0.0
                else:
                    env = np.zeros(frames, dtype=np.float32)

                v.level = level
                sig = raw * env

                # one-pole low-pass with light resonance feedback
                filtered = np.empty(frames, dtype=np.float32)
                z1 = v.z1
                for i in range(frames):
                    fb = sig[i] - res_amt * z1
                    z1 += alpha * (fb - z1)
                    filtered[i] = z1
                v.z1 = z1

                out += filtered

        out *= self.master_gain
        np.clip(out, -1.0, 1.0, out=out)
        self.peak = float(np.max(np.abs(out))) if frames else 0.0
        return out


def make_callback(synth):
    def callback(outdata, frames, time_info, status):
        buf = synth.render(frames)
        outdata[:, 0] = buf
    return callback


# ---------------------------------------------------------------- UI ----

def draw_ui(stdscr, synth, held_display):
    stdscr.erase()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    green = curses.color_pair(1)

    stdscr.addstr(0, 2, "██▓▒░ PERRÓN SYNTH ░▒▓██", green | curses.A_BOLD)
    stdscr.addstr(1, 2, "sintetizador standalone — sin DAW, sin navegador", green)

    row = 3
    stdscr.addstr(row, 2, f"Onda: {synth.waveform:<8}  Octava: {synth.octave}", green)
    row += 1
    stdscr.addstr(row, 2,
                  f"ADSR   A:{synth.attack:0.3f}  D:{synth.decay:0.3f}  "
                  f"S:{synth.sustain:0.2f}  R:{synth.release:0.3f}", green)
    row += 1
    stdscr.addstr(row, 2,
                  f"Filtro cutoff:{synth.cutoff:0.2f}  resonancia:{synth.resonance:0.2f}",
                  green)
    row += 2

    peak = synth.peak
    bar_len = int(clamp(peak, 0.0, 1.0) * 30)
    stdscr.addstr(row, 2, "OUT [" + "█" * bar_len + " " * (30 - bar_len) + "]", green)
    row += 2

    stdscr.addstr(row, 2, "Fila alta  (oct+1): q 2 w 3 e r 5 t 6 y 7 u", green)
    row += 1
    stdscr.addstr(row, 2, "Fila baja  (oct  ): z s x d c v g b n m , .", green)
    row += 2

    active = sorted(held_display)
    stdscr.addstr(row, 2, "Sonando: " + (" ".join(active) if active else "-"), green | curses.A_BOLD)
    row += 2

    stdscr.addstr(row, 2, "Tab=onda  -/= octava  a/A dec/A  f/F dec  h/H sus  j/J rel", green)
    row += 1
    stdscr.addstr(row, 2, "↑↓ cutoff  ←→ resonancia  Ctrl+C sale", green)

    stdscr.refresh()


def note_display_name(note_id, octave_used):
    return f"{NOTE_NAMES[note_id % 12]}{octave_used}"


def main(stdscr):
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    stdscr.nodelay(True)
    stdscr.timeout(10)

    synth = Synth()
    stream = sd.OutputStream(
        samplerate=SAMPLE_RATE,
        blocksize=BLOCK_SIZE,
        channels=1,
        dtype='float32',
        callback=make_callback(synth),
    )
    stream.start()

    last_seen = {}   # (note_id, octave_used) -> last_press_time
    lock = threading.Lock()
    stop_flag = threading.Event()

    def watchdog():
        while not stop_flag.is_set():
            now = time.time()
            with lock:
                dead = [k for k, t in last_seen.items()
                        if (now - t) * 1000.0 > RELEASE_TIMEOUT_MS]
                for k in dead:
                    del last_seen[k]
            for (note_id, oct_used) in dead:
                synth.note_off(note_id, oct_used)
            time.sleep(0.02)

    wd = threading.Thread(target=watchdog, daemon=True)
    wd.start()

    try:
        while True:
            try:
                ch = stdscr.getch()
            except curses.error:
                ch = -1

            if ch != -1:
                if ch == 27:  # Esc
                    break
                if 0 < ch < 256:
                    c = chr(ch)
                else:
                    c = None

                if c and c.lower() in KEY_MAP_LOW:
                    note_id = KEY_MAP_LOW[c.lower()] % 12
                    oct_used = synth.octave
                    key = (note_id, oct_used)
                    with lock:
                        last_seen[key] = time.time()
                    synth.note_on(note_id, oct_used)
                elif c and c.lower() in KEY_MAP_HIGH:
                    note_id = KEY_MAP_HIGH[c.lower()] % 12
                    oct_used = synth.octave + 1
                    key = (note_id, oct_used)
                    with lock:
                        last_seen[key] = time.time()
                    synth.note_on(note_id, oct_used)
                elif ch == 9:  # Tab
                    i = WAVEFORMS.index(synth.waveform)
                    synth.waveform = WAVEFORMS[(i + 1) % len(WAVEFORMS)]
                elif c == '-':
                    synth.octave = clamp(synth.octave - 1, 0, 7)
                elif c == '=':
                    synth.octave = clamp(synth.octave + 1, 0, 7)
                elif c == 'a':
                    synth.attack = clamp(synth.attack - 0.005, 0.001, 2.0)
                elif c == 'A':
                    synth.attack = clamp(synth.attack + 0.005, 0.001, 2.0)
                elif c == 'f':
                    synth.decay = clamp(synth.decay - 0.01, 0.001, 3.0)
                elif c == 'F':
                    synth.decay = clamp(synth.decay + 0.01, 0.001, 3.0)
                elif c == 'h':
                    synth.sustain = clamp(synth.sustain - 0.02, 0.0, 1.0)
                elif c == 'H':
                    synth.sustain = clamp(synth.sustain + 0.02, 0.0, 1.0)
                elif c == 'j':
                    synth.release = clamp(synth.release - 0.01, 0.001, 4.0)
                elif c == 'J':
                    synth.release = clamp(synth.release + 0.01, 0.001, 4.0)
                elif ch == curses.KEY_UP:
                    synth.cutoff = clamp(synth.cutoff + 0.02, 0.0, 1.0)
                elif ch == curses.KEY_DOWN:
                    synth.cutoff = clamp(synth.cutoff - 0.02, 0.0, 1.0)
                elif ch == curses.KEY_RIGHT:
                    synth.resonance = clamp(synth.resonance + 0.02, 0.0, 1.0)
                elif ch == curses.KEY_LEFT:
                    synth.resonance = clamp(synth.resonance - 0.02, 0.0, 1.0)

            with lock:
                held_display = [note_display_name(n, o) for (n, o) in last_seen.keys()]
            draw_ui(stdscr, synth, held_display)

    finally:
        stop_flag.set()
        stream.stop()
        stream.close()


if __name__ == '__main__':
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        sys.exit(0)
