"""Speech-to-text backends (CPU side). All local/offline: nothing here ever downloads a model.
  ct2    : faster-whisper (CTranslate2), CPU int8, models in models/ct2/<id>/
  sherpa : sherpa-onnx (Parakeet-TDT / Moonshine), CPU,           models in models/sherpa/<dir>/
The GPU (Vulkan) backend lives in gpu/backend.py.
"""
import os, time
import numpy as np
N = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NT = max(2, min(8, os.cpu_count() or 4))

class Ct2:
    name = 'ct2-cpu'
    def __init__(self, model):
        from faster_whisper import WhisperModel
        path = N + '/models/ct2/' + model
        if not os.path.exists(path + '/model.bin'): raise FileNotFoundError(path)
        self.m = WhisperModel(path, device='cpu', compute_type='int8', cpu_threads=max(2, min(8, os.cpu_count() or 4)))
    def transcribe(self, audio):
        segs, _ = self.m.transcribe(audio, language='en', beam_size=1, vad_filter=True, condition_on_previous_text=False)
        return ' '.join(s.text.strip() for s in segs).strip()

class Sherpa:
    """sherpa-onnx on CPU: NVIDIA Parakeet-TDT 0.6B v2 (int8) or Useful Sensors Moonshine."""
    DIRS = {'parakeet': 'sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8', 'moonshine-base': 'sherpa-onnx-moonshine-base-en-int8',
            'moonshine-tiny': 'sherpa-onnx-moonshine-tiny-en-int8'}
    def __init__(self, model):
        import sherpa_onnx
        self.name = 'sherpa-%s-cpu' % model
        d = N + '/models/sherpa/' + self.DIRS[model]
        if model.startswith('moonshine'):
            self.r = sherpa_onnx.OfflineRecognizer.from_moonshine(preprocessor=d + '/preprocess.onnx', encoder=d + '/encode.int8.onnx',
                uncached_decoder=d + '/uncached_decode.int8.onnx', cached_decoder=d + '/cached_decode.int8.onnx', tokens=d + '/tokens.txt', num_threads=NT)
        else:
            self.r = sherpa_onnx.OfflineRecognizer.from_transducer(encoder=d + '/encoder.int8.onnx', decoder=d + '/decoder.int8.onnx',
                joiner=d + '/joiner.int8.onnx', tokens=d + '/tokens.txt', num_threads=NT, model_type='nemo_transducer')
    def transcribe(self, audio):
        st = self.r.create_stream(); st.accept_waveform(16000, audio); self.r.decode_stream(st); return st.result.text.strip()

def load(backend, model, strict=False):
    if backend == 'sherpa': return Sherpa(model)
    return Ct2(model)

def warm(asr):
    t = time.time(); asr.transcribe(np.zeros(16000 * 2, dtype=np.float32)); return time.time() - t
