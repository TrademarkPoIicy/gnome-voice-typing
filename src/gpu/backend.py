"""GPU (Vulkan) speech backend for gnome-voice-typing (whisper.cpp / parakeet.cpp). Pure ctypes, no extra Python deps (numpy only).

    import sys; sys.path.insert(0, '<install dir>/gpu')
    import backend
    m = backend.load('small.en')          # or 'parakeet', 'large-v3-turbo', 'base.en', ... ; device='cpu' for ggml-CPU
    text = m.transcribe('/path/to.wav')   # wav path OR float32 mono 16 kHz array
    m.warm()                               # optional (load() already warms)
    m.close()

The model stays resident on the GPU until close(); call transcribe() as often as you like (not thread-safe: use one lock).
Keep ONE long-lived process/daemon. First-ever load compiles shaders (~1-3 s, cached by Mesa afterwards).
"""
import os, ctypes, wave, numpy as np

G = os.path.dirname(os.path.abspath(__file__))
BIN = G + '/lib'                                   # libggml-*.so (built by scripts/build-gpu.sh)
MD = os.path.join(os.path.dirname(G), 'models', 'ggml')
MODELS = {  # id -> (kind, file)
    'tiny.en': ('whisper', 'ggml-tiny.en.bin'),
    'base.en': ('whisper', 'ggml-base.en.bin'),
    'small.en': ('whisper', 'ggml-small.en.bin'),
        'medium.en-q5_0': ('whisper', 'ggml-medium.en-q5_0.bin'),
    'large-v3-turbo': ('whisper', 'ggml-large-v3-turbo-q5_0.bin'),
    'parakeet': ('parakeet', 'ggml-parakeet-tdt-0.6b-v3-f16.bin'),
    'parakeet-q8': ('parakeet', 'ggml-parakeet-tdt-0.6b-v3-q8_0.bin'),
}
_lib = None


def _L():
    global _lib
    if _lib is None:
        l = ctypes.CDLL(G + '/libasrshim.so')
        l.asr_init_backends.argtypes = [ctypes.c_int, ctypes.c_char_p]
        for n in ('asr_whisper_load',):
            getattr(l, n).restype = ctypes.c_void_p
            getattr(l, n).argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        l.asr_parakeet_load.restype = ctypes.c_void_p
        l.asr_parakeet_load.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        l.asr_whisper_run.restype = ctypes.c_char_p
        l.asr_whisper_run.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        l.asr_parakeet_run.restype = ctypes.c_char_p
        l.asr_parakeet_run.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        l.asr_whisper_free.argtypes = [ctypes.c_void_p]
        l.asr_parakeet_free.argtypes = [ctypes.c_void_p]
        l.asr_gpu_name.restype = ctypes.c_char_p; l.asr_gpu_name.argtypes = [ctypes.c_int]
        l.asr_init_backends(1, BIN.encode())
        _lib = l
    return _lib


def read_wav(path):
    with wave.open(path) as w:
        assert w.getframerate() == 16000, 'need 16 kHz wav'
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        if w.getnchannels() > 1: a = a.reshape(-1, w.getnchannels()).mean(1)
    return a


class Model:
    def __init__(self, name, device='gpu', gpu_index=0, threads=8, flash_attn=True, opts=None):
        kind, fn = MODELS[name]
        self.name, self.kind, self.device, self.threads = name, kind, device, threads
        self.opts = opts if opts is not None else 0
        l = _L(); use_gpu = 1 if device == 'gpu' else 0
        if use_gpu:   # refuse a silent CPU fallback (no Vulkan ICD / only llvmpipe): the caller then falls back to a proper CPU model
            g = l.asr_gpu_name(gpu_index).decode()
            if not g or 'llvmpipe' in g: raise RuntimeError('no usable Vulkan GPU device (got %r)' % g)
            self.gpu = g
        path = (MD + '/' + fn).encode()
        if not os.path.exists(path): raise FileNotFoundError(path)
        if kind == 'whisper': self.ctx = l.asr_whisper_load(path, use_gpu, gpu_index, int(flash_attn))
        else: self.ctx = l.asr_parakeet_load(path, use_gpu, gpu_index)
        if not self.ctx: raise RuntimeError('model load failed: ' + name)
        self.warm()

    def warm(self):
        self.transcribe(np.zeros(16000, dtype=np.float32))
        self.transcribe((np.random.RandomState(0).randn(48000) * 0.02).astype(np.float32))

    def transcribe(self, audio):
        if isinstance(audio, (str, os.PathLike)): audio = read_wav(str(audio))
        a = np.ascontiguousarray(audio, dtype=np.float32)
        if len(a) < 16000: a = np.concatenate([a, np.zeros(16000 - len(a), np.float32)])  # whisper.cpp needs >=1 s
        p = a.ctypes.data_as(ctypes.c_void_p); l = _L()
        r = l.asr_whisper_run(self.ctx, p, len(a), self.threads, b'en', self.opts) if self.kind == 'whisper' else l.asr_parakeet_run(self.ctx, p, len(a), self.threads)
        return r.decode('utf-8', 'replace').strip()

    def close(self):
        if self.ctx:
            (_L().asr_whisper_free if self.kind == 'whisper' else _L().asr_parakeet_free)(self.ctx); self.ctx = None


def load(name='small.en', device='gpu', **kw):
    return Model(name, device, **kw)


if __name__ == '__main__':
    import sys, time
    if len(sys.argv) == 2 and sys.argv[1] == '--probe':      # used by install.sh: print the name of the first usable Vulkan GPU, exit 0; else exit 1
        g = _L().asr_gpu_name(0).decode()
        print(g); sys.exit(0 if g and 'llvmpipe' not in g else 1)
    if len(sys.argv) < 4: sys.exit('usage: backend.py <model> <gpu|cpu> <16 kHz mono wav> | backend.py --probe')
    m = load(sys.argv[1], sys.argv[2])
    f = sys.argv[3]
    t = time.time(); print(repr(m.transcribe(f)), '%.3fs' % (time.time() - t))
