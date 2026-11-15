"""Audio front end for the dictation daemon: noise suppression (RNNoise via ctypes, else GTCRN via sherpa-onnx), a streaming Silero voice-activity
detector (the ONNX file that ships inside faster-whisper) and an adaptive noise-floor tracker. All local, no downloads, numpy + onnxruntime only."""
import os, ctypes, numpy as np
N = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHUNK = 1600                                     # 100 ms at 16 kHz

class RnnoiseDenoiser:
    """RNNoise (system librnnoise.so.0, 48 kHz, 10 ms frames) behind a streaming 16 kHz <-> 48 kHz polyphase resampler. ~0.7 % of one core, ~21 ms delay."""
    name = 'rnnoise'
    def __init__(self):
        l = ctypes.CDLL('librnnoise.so.0')
        l.rnnoise_create.restype = ctypes.c_void_p; l.rnnoise_create.argtypes = [ctypes.c_void_p]
        l.rnnoise_process_frame.restype = ctypes.c_float; l.rnnoise_process_frame.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        l.rnnoise_destroy.argtypes = [ctypes.c_void_p]
        self.l = l; self.st = l.rnnoise_create(None)
        if not self.st: raise RuntimeError('rnnoise_create failed')
        n = 63; k = np.arange(n) - (n - 1) / 2; fc = 7000 / 48000
        self.h = (2 * fc * np.sinc(2 * fc * k) * np.hamming(n)).astype(np.float32)
        self.up_tail = np.zeros(n - 1, np.float32); self.dn_tail = np.zeros(n - 1, np.float32); self.inbuf = np.zeros(0, np.float32)
    def run(self, x):                            # x: float32, int16 scale, 16 kHz -> same length (RNNoise keeps count)
        u = np.zeros(len(x) * 3, np.float32); u[::3] = x * 3
        z = np.concatenate([self.up_tail, u]); self.up_tail = z[-(len(self.h) - 1):]
        self.inbuf = np.concatenate([self.inbuf, np.convolve(z, self.h, 'valid')]); out = []
        while len(self.inbuf) >= 480:
            f = np.ascontiguousarray(self.inbuf[:480]); self.inbuf = self.inbuf[480:]; o = np.zeros(480, np.float32)
            self.l.rnnoise_process_frame(self.st, o.ctypes.data, f.ctypes.data); out.append(o)
        if not out: return np.zeros(0, np.float32)
        z = np.concatenate([self.dn_tail] + out); self.dn_tail = z[-(len(self.h) - 1):]
        return np.convolve(z, self.h, 'valid')[::3]
    def close(self):
        if self.st: self.l.rnnoise_destroy(self.st); self.st = None

class GtcrnDenoiser:
    """GTCRN speech enhancement through sherpa-onnx (models/denoise/gtcrn_simple.onnx, 0.5 MB, native 16 kHz). ~3 % of one core, ~16 ms delay."""
    name = 'gtcrn'
    def __init__(self):
        import sherpa_onnx as so
        path = N + '/models/denoise/gtcrn_simple.onnx'
        if not os.path.exists(path): raise FileNotFoundError(path)
        c = so.OnlineSpeechDenoiserConfig(); c.model.gtcrn.model = path; c.model.num_threads = 1
        self.d = so.OnlineSpeechDenoiser(c)
    def run(self, x):
        r = self.d.run((x / 32768.0).astype(np.float32).tolist(), 16000); return np.array(r.samples, np.float32) * 32768.0
    def close(self): self.d = None

class Denoiser:
    """pcm16 bytes in -> pcm16 bytes out, ALWAYS the same length (the engine's start-up delay is filled with silence), so callers keep their 100 ms timing."""
    def __init__(self, engine='auto'):
        self.eng = None; self.err = ''
        for cls in ((RnnoiseDenoiser, GtcrnDenoiser) if engine in ('auto', 'rnnoise') else (GtcrnDenoiser, RnnoiseDenoiser)):
            try: self.eng = cls(); break
            except Exception as e: self.err += '%s: %r; ' % (cls.name, e)
        self.buf = np.zeros(256 if self.eng and self.eng.name == 'gtcrn' else 0, np.float32)
        self.name = self.eng.name if self.eng else 'off'
    def process(self, pcm):
        if self.eng is None: return pcm
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        self.buf = np.concatenate([self.buf, self.eng.run(x)])
        if len(self.buf) >= len(x): y, self.buf = self.buf[:len(x)], self.buf[len(x):]
        else: y = np.concatenate([self.buf, np.zeros(len(x) - len(self.buf), np.float32)]); self.buf = np.zeros(0, np.float32)
        return np.clip(y, -32768, 32767).astype(np.int16).tobytes()
    def close(self):
        if self.eng: self.eng.close(); self.eng = None

class Vad:
    """Streaming Silero VAD. prob(pcm16 bytes of 100 ms) -> highest speech probability over its 512-sample windows, or None if unavailable."""
    def __init__(self):
        self.ok = False; self.err = ''
        try:
            import onnxruntime, faster_whisper
            o = onnxruntime.SessionOptions(); o.inter_op_num_threads = 1; o.intra_op_num_threads = 1; o.log_severity_level = 4
            self.s = onnxruntime.InferenceSession(os.path.join(os.path.dirname(faster_whisper.__file__), 'assets', 'silero_vad_v6.onnx'),
                                                  providers=['CPUExecutionProvider'], sess_options=o)
            self.reset(); self.ok = True
        except Exception as e: self.err = repr(e)
    def reset(self):
        self.h = np.zeros((1, 1, 128), np.float32); self.c = np.zeros((1, 1, 128), np.float32)
        self.ctx = np.zeros(64, np.float32); self.carry = np.zeros(0, np.float32)
    def prob(self, pcm):
        if not self.ok: return None
        x = np.concatenate([self.carry, np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0])
        n = len(x) // 512
        if n == 0: self.carry = x; return 0.0
        w = x[:n * 512].reshape(n, 512); self.carry = x[n * 512:]
        ctx = np.concatenate([self.ctx[None, :], w[:-1, -64:]], 0); self.ctx = w[-1, -64:].copy()
        p, self.h, self.c = self.s.run(None, {'input': np.concatenate([ctx, w], 1).astype(np.float32), 'h': self.h, 'c': self.c})
        return float(np.max(p))

class Floor:
    """Adaptive noise floor of the (denoised) signal: falls quickly to quieter levels, rises slowly, and never rises while someone is talking."""
    def __init__(self, init=None): self.v = init
    def update(self, rms, talking):
        if self.v is None: self.v = rms
        elif rms < self.v: self.v = 0.7 * self.v + 0.3 * rms
        elif not talking: self.v += (rms - self.v) * 0.02
        return self.v
