# Changelog

## 0.1.4

- Typing works on machines without ydotool: if /dev/uinput is writable but ydotool isn't installed, the daemon now uses GNOME's typing portal instead of failing every phrase.
- rnnoise (noise suppression) is listed with the other optional packages in the installer and the first-launch setup card, and installs with the same one click.
- Optional Silero voice detection packages (onnxruntime, faster-whisper) are offered at install and in the setup card, installed into the app's Python environment (pip is bootstrapped if missing).

## 0.1.3 - 2026-11-15

Brings the changes that were only on one machine into the repository, so every install is the same version.

- **Uninstall Voice Typing** button in Settings (with a confirm). It removes the app, its models and settings, and asks once for the administrator password (graphical prompt) to remove the udev rule; if that prompt is declined everything else is still removed. `uninstall.sh --pkexec` does the same from the command line; started from the panel it runs from a temporary copy and logs to `/tmp/gnome-voice-typing-uninstall.log`.
- **Noise suppression** (RNNoise, else GTCRN through sherpa-onnx) before voice detection and recognition, plus a streaming Silero voice-activity detector and an adaptive noise floor (`src/bin/audiofx.py`). Settings: Noise suppression on/off, Background noise Auto or Manual with **Calibrate**, Sensitivity, and a live Noise level meter (the yellow mark is the tracked noise floor). New commands `voice-typing calibrate` and `voice-typing meter`.
- **Type while you talk** (`streaming`: auto / true / false; `stream_interval`): partial results are typed as you speak whenever the model keeps up, otherwise each phrase is typed at the end.
- **App-grid launcher**: "Voice Typing" in Activities, same as pressing the shortcut. Removed by the uninstaller.
- Shortcut conflicts: a shortcut already on `Super+H` is moved to `Super+Shift+H` (when free) instead of deleted, and its exact old value is restored on uninstall. Shortcuts kept by other extensions in their own settings are checked too.
- Installer: `rnnoise` added to the runtime packages on Fedora and Arch (optional; without it the GTCRN fallback or no suppression is used).

## 0.1.2 - 2026-11-15

- **First-launch prompt for missing system packages** ([#1](https://github.com/TrademarkPoIicy/gnome-voice-typing/issues/1)). If the install skipped the distro packages (for example `--no-system-deps`, or no sudo at install time), the first time the bar opens the settings panel shows which packages are missing for the graphics-card engine (cmake, glslc, Vulkan / SPIR-V headers, ...) and for typing (ydotool). It explains that the processor (CPU) engine works without them, but the graphics-card version can't be built or used until they're installed.
  - **Install** asks for your password with the desktop's normal prompt (`pkexec`), installs the packages with your package manager, builds the graphics-card engine, downloads its default model (Parakeet GPU), and selects it, so you only press Save. Progress and readable errors are shown in the panel. Nothing is ever run with sudo silently.
  - **Not now** remembers the choice (until the set of missing packages changes), and a one-line "Graphics-card setup: N packages missing · Set up" stays in Settings so it can be done later.
  - No Vulkan graphics card: the graphics-card packages are never asked for. No `pkexec`: the panel shows the exact command to paste in a terminal instead. A cancelled password prompt installs nothing and offers Try again.
- `voice-typing-models deps [--json] | deps install | deps dismiss | deps undismiss`. Package lists live in `data/sysdeps.json`; `tests/sysdeps-test.sh` checks they match `install.sh`.
- Needs one logout/login to load the updated extension.

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
