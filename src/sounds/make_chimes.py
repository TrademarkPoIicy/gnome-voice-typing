#!/usr/bin/env python3
"""Synthesizes ORIGINAL voice-typing chimes (no third-party audio) into this directory.
48 kHz, 16-bit, mono, peak ~ -18 dBFS. Run: python3 src/sounds/make_chimes.py [outdir]
  start.wav  soft two-note rising (E5 -> B5), ~200 ms
  stop.wav   falling counterpart (B5 -> E5), ~200 ms
  wake.wav   subtle short tick, ~45 ms
"""
import os, sys, wave
import numpy as np

SR = 48000
PEAK_DB = -18.0
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))

def tone(freq, dur, attack=0.012, release=0.06, decay=4.0):
    n = int(SR * dur)
    t = np.arange(n) / SR
    # sine + a little 2nd harmonic for warmth, slight detuned partial for softness
    y = np.sin(2*np.pi*freq*t) + 0.18*np.sin(2*np.pi*2*freq*t) + 0.05*np.sin(2*np.pi*1.003*freq*t)
    env = np.exp(-decay * t / dur)
    a = max(1, int(SR * attack)); r = max(1, int(SR * release))
    env[:a] *= 0.5 - 0.5*np.cos(np.pi*np.arange(a)/a)           # raised-cosine attack
    env[-r:] *= 0.5 + 0.5*np.cos(np.pi*np.arange(r)/r)          # raised-cosine release -> ends at 0
    return y * env

def place(parts, total):
    y = np.zeros(int(SR * total))
    for start, sig in parts:
        i = int(SR * start); seg = sig[:len(y) - i]; y[i:i+len(seg)] += seg
    return y

def save(name, y, peak_db=PEAK_DB):
    y = y / np.abs(y).max() * 10**(peak_db/20)
    pcm = np.round(y * 32767).astype('<i2')
    with wave.open(os.path.join(OUT, name), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())

E5, B5 = 659.25, 987.77
os.makedirs(OUT, exist_ok=True)
save('start.wav', place([(0.0, tone(E5, 0.11)), (0.08, tone(B5, 0.12))], 0.20))
save('stop.wav',  place([(0.0, tone(B5, 0.11)), (0.08, tone(E5, 0.12))], 0.20))
save('wake.wav',  place([(0.0, tone(1568.0, 0.045, attack=0.004, release=0.025, decay=5.0))], 0.045), peak_db=-22.0)
print('wrote', sorted(f for f in os.listdir(OUT) if f.endswith('.wav')))
