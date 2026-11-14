# Changelog

## 0.1.0 - 2026-11-14

First public release.

- Offline, phrase-by-phrase voice typing for GNOME Shell (Wayland; 45-51, tested on 51): press a shortcut (default `Super+H`), speak, and the text is typed into the focused window.
- Floating bar (GNOME Shell extension): listening / paused state, mic button toggles dictation, red-on-hover close, draggable, placed next to the focused text box, settings panel.
- Settings panel: model picker with load spinner, "Run on" graphics card or processor with automatic fallback to the other, pause-after-silence, microphone, sounds on/off, Save.
- Engines: Vulkan GPU (whisper.cpp + parakeet.cpp, built on your machine) and CPU (sherpa-onnx; optional faster-whisper). Models are downloaded on demand with pinned SHA-256 checksums.
- Voice commands: "stop listening", "close dictation", "delete that" / "scratch that", "new line", "new paragraph".
- Installer: `install.sh` (Fedora / Debian / Ubuntu / Arch, `--dry-run`, per-step consent for sudo, every changed setting saved) and `uninstall.sh` (restores them).
- No telemetry, no accounts, no network use at runtime.
