# Third-party software and models

gnome-voice-typing itself is MIT-licensed (see `LICENSE`). It installs, builds or downloads the third-party
components below. **Nothing in this list is bundled in the release tarball**: models and code are fetched by
`install.sh` / `voice-typing-models` directly from the upstream projects, from pinned URLs, and verified with
SHA-256 (or, for source code, a pinned git commit id). The sums are in `src/data/models-registry.json`.

## Speech models (downloaded on demand)

| Model id | What it is | License | Attribution / notes |
|---|---|---|---|
| `parakeet-gpu` | NVIDIA Parakeet-TDT 0.6B **v3**, GGML f16 (`ggml-org/parakeet-GGUF`) | **CC-BY-4.0** | Credit required: "Parakeet-TDT 0.6B v3 © NVIDIA, CC BY 4.0, https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3". The Hugging Face page for the GGML conversion is tagged MIT, but it is a derivative of the NVIDIA model, so the CC-BY-4.0 attribution requirement is carried along. |
| `parakeet` | NVIDIA Parakeet-TDT 0.6B **v2**, int8 ONNX (`k2-fsa/sherpa-onnx` release `asr-models`) | **CC-BY-4.0** | Credit required: "Parakeet-TDT 0.6B v2 © NVIDIA, CC BY 4.0, https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2". |
| `small.en-gpu`, `medium.en-q5-gpu`, `large-v3-turbo-gpu` | OpenAI Whisper small.en / medium.en (q5_0) / large-v3-turbo (q5_0), GGML (`ggerganov/whisper.cpp`) | MIT | © OpenAI, https://github.com/openai/whisper. (Some Hugging Face mirror cards of the Whisper weights list Apache-2.0; both are permissive.) |
| `moonshine-base`, `moonshine-tiny` | Moonshine English models, int8 ONNX (`k2-fsa/sherpa-onnx`) | MIT | © Useful Sensors, https://github.com/usefulsensors/moonshine (only these English MIT-licensed models are used; other Moonshine releases may carry different terms). |
| `tiny.en`, `base.en`, `small.en`, `medium.en` | OpenAI Whisper, CTranslate2 conversions (`Systran/faster-whisper-*`) | MIT | © OpenAI; conversion by Systran. |
| `distil-small.en`, `distil-large-v3` | Distil-Whisper, CTranslate2 conversions (`Systran/faster-distil-whisper-*`) | MIT | © Hugging Face, https://github.com/huggingface/distil-whisper |

If you redistribute a build that **includes** a Parakeet model, or show the model's output in a product, keep the CC-BY-4.0
credit above. Using the tool for your own dictation needs nothing.

## Code that is built or installed at install time

| Component | How it is obtained | License |
|---|---|---|
| whisper.cpp / ggml / parakeet.cpp (`ggml-org/whisper.cpp`, commit `5922450fbce1ed9d1648babd0d5ac4520055b436`) | cloned by commit id and compiled locally with Vulkan (GPU engine only) | MIT |
| sherpa-onnx (CPU engine) | `pip` into a private venv | Apache-2.0 |
| NumPy | `pip` into the venv | BSD-3-Clause |
| faster-whisper, CTranslate2 (only if you pick a `ct2` Whisper model) | `pip` into the venv, on demand | MIT |
| ydotool / ydotoold | distro package (separate program, started as a child process; **not** bundled or linked). Where the distro only has the old 0.1.x series (Ubuntu 24.04, Debian 12), v1.0.4 is built from source (commit `57ba7d0a...`) on your machine by `scripts/build-ydotool.sh` | AGPL-3.0 |
| wl-clipboard (`wl-copy`, `wl-paste`) | distro package (separate program) | GPL-3.0 |
| PipeWire tools (`pw-record`, `pw-play`), PulseAudio utils (`pactl`) | distro packages | MIT / LGPL |

## Sounds

The three chimes in `src/sounds/` are original, synthesized by `src/sounds/make_chimes.py` (plain sine tones with envelopes).
They contain no third-party audio and are covered by this project's MIT license.

## Test data

`tests/data/sample.wav` is a short excerpt of the 1961 US presidential inaugural address (a US government work, public domain),
as used in the whisper.cpp sample set. It is only used by the automated tests.

## Trademarks

"Windows" and "Microsoft" are trademarks of Microsoft Corporation, "NVIDIA" and "Parakeet" of NVIDIA Corporation, "AMD" and
"Radeon" of Advanced Micro Devices, "GNOME" of the GNOME Foundation. They are used here only to describe compatibility
or to credit sources. This project is not affiliated with or endorsed by any of them.
