# Changelog

## 0.1.2 - 2026-11-15

- **First-launch package prompt.** The first time the bar or Settings opens, missing graphics-card build packages (cmake, glslc, the Vulkan and SPIR-V headers and loader) and ydotool are listed by name. **Install** uses `pkexec` so the desktop shows its usual password window, then builds the graphics-card engine and switches to it, with progress and errors on the prompt. **Not now** keeps the processor version and does not ask again at startup; Settings → System packages opens the same prompt. No Vulkan graphics card: the graphics-card build is not offered. No `pkexec`, or a closed password window: nothing is installed. sudo is never run from the prompt. The package names are the same lists `install.sh` already uses.

## 0.1.1 - 2026-11-14

- **One-click model installs.** Every model in the catalog is always listed in the settings panel. Not downloaded: a **Download · size** button. Downloading: progress bar and **Cancel**. Failed: a short reason and **Retry**. Installed: an **Uninstall** button with a confirm (the model in use can't be uninstalled; the panel says why). Downloads run in a separate helper process (never inside GNOME Shell) and report progress through `models/.status/<id>.json`.
- **Installed means verified.** A model counts as installed only when every file in the registry exists with the right size and a marker written after the SHA-256 check is present. Models downloaded by 0.1.0 are checked once and adopted automatically (`voice-typing-models adopt`). Downloads are staged in `models/.dl/` and moved into place only when complete, so a half-downloaded model (for example a Whisper folder holding only `config.json`) can no longer appear in `models/`.
- `voice-typing-models`: new `status [--json]`, `cancel`, `adopt`; `remove` refuses the model in use; resumes partial downloads (also ones left by 0.1.0); IPv4 addresses are tried first (some networks have a broken IPv6 route to the model hosts and hung); readable error messages.
- Needs one logout/login to load the new settings panel.

## 0.1.0 - 2026-11-14

First public release.

- Offline, phrase-by-phrase voice typing for GNOME Shell (Wayland; 45-51, tested on 51): press a shortcut (default `Super+H`), speak, and the text is typed into the focused window.
- Floating bar (GNOME Shell extension): listening / paused state, mic button toggles dictation, red-on-hover close, draggable, placed next to the focused text box, settings panel.
- Settings panel: model picker with load spinner, "Run on" graphics card or processor with automatic fallback to the other, pause-after-silence, microphone, sounds on/off, Save.
- Engines: Vulkan GPU (whisper.cpp + parakeet.cpp, built on your machine) and CPU (sherpa-onnx; optional faster-whisper). Models are downloaded on demand with pinned SHA-256 checksums.
- Voice commands: "stop listening", "close dictation", "delete that" / "scratch that", "new line", "new paragraph".
- Installer: `install.sh` (Fedora / Debian / Ubuntu / Arch, `--dry-run`, per-step consent for sudo, every changed setting saved) and `uninstall.sh` (restores them).
- No telemetry, no accounts, no network use at runtime.
