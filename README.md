# gnome-voice-typing

Offline voice typing for the GNOME desktop. Press a shortcut, speak, and your words are typed into whatever text box has focus:
browsers, editors, terminals, chat apps, GNOME's own dialogs. A small floating bar shows that it is listening and sits next to
the text box you are typing in.

- **Fully offline.** Speech recognition runs on your computer (graphics card or processor). No cloud, no account, no telemetry.
- **Fast.** On a graphics card, a spoken phrase is typed about 0.03-0.1 s after you stop speaking (measured on a Radeon AI PRO R9700;
  yours will differ). On a processor, 0.1-0.3 s with the default CPU model.
- **Good accuracy.** The default model is NVIDIA Parakeet-TDT 0.6B, which adds punctuation and capital letters by itself.
- **Not tied to one GPU brand.** The GPU engine uses Vulkan (AMD, Intel, NVIDIA with a Vulkan driver). No CUDA or ROCm needed.

> Status: early releases (see `CHANGELOG.md`). Tested on Fedora 45 with GNOME Shell 51 (Wayland) and a Radeon GPU. Debian/Ubuntu and Arch
> support is implemented in the installer but has had less testing. Please report problems.

## Features

- Start/pause with a keyboard shortcut (default `Super+H`), or click the microphone button on the bar.
- Phrase-by-phrase typing: each phrase is typed when you pause speaking.
- Voice commands: "stop listening" / "pause listening", "close dictation", "delete that" / "scratch that", "new line", "new paragraph".
- Floating bar (a GNOME Shell extension): never steals keyboard focus; draggable; opens next to the focused text box (using the
  accessibility interface, AT-SPI); red close button; start/stop chimes (original sounds, can be turned off).
- Settings (gear icon): model picker with a clear Light / Medium / Heavy system-impact tag, **Run on: graphics card or processor**
  (if one fails to load, the other is used automatically), pause after silence (10 s / 30 s / 1 min / 2 min / never), microphone,
  sounds on/off, noise suppression, background noise Auto / Manual with Calibrate, sensitivity and a live noise-level meter, and
  **Uninstall Voice Typing** (removes everything, one password prompt for the udev rule).
- Many models: Parakeet v3 (GPU) and v2 (CPU), Whisper (tiny to large-v3-turbo, GPU and CPU), Moonshine. **One-click installs:** the
  installer downloads no models; the first time you open the bar it shows **Install model** next to the mic. Every model is always listed in the settings panel, and **Download** (with its size)
  fetches, checksum-verifies and installs it, with a progress bar and Cancel. Installed models show **Uninstall** (not available for
  the model in use).

## Settings in the cog panel

Click the cog on the dictation bar. **Appearance**, **Dictation formatting**, **Custom vocabulary**, and **Keyboard shortcuts** are categories inside that panel. There is no separate Personalization window. `voice-typing preferences` opens the same cog panel.

- **Appearance:** dark, light or high-contrast bar; compact, comfortable or large text/buttons; background opacity; show/hide the model name. Setup/loading messages remain visible. Bar placement still lives in **Typing & sounds**.
- **Dictation formatting:** keep or remove model punctuation, preserve capitalization or capitalize sentence starts/use lowercase, convert spoken punctuation, and choose whether to add a space after each phrase. “New line” and “new paragraph” still work.
- **Custom vocabulary:** edit “heard phrase → preferred text” rules directly in the panel. Dictation pauses while you enter text. Use **Done editing**, then **Save**. Matches ignore case, respect word boundaries, prioritize longer matches, and preserve preferred spelling. Rules replace recognized text; they do not retrain the model.
- **Keyboard shortcuts:** record or disable start/pause and close shortcuts, then **Save shortcuts**. Conflicts are rejected without changing other apps. Routine upgrades preserve your choices.
- **Microphone & noise:** click the microphone dropdown to see all available microphones and choose one, then **Save**. A disconnected selection is labeled explicitly.

Appearance, formatting and vocabulary use the panel’s **Save** button. Changes are confirmed by the service before the button reports success. Custom formatting and vocabulary use complete phrases instead of incremental typing. Settings stay local in `~/.config/gnome-voice-typing/config.json` (or `$XDG_CONFIG_HOME`). Actionable errors remain under **Setup & uninstall** until dismissed.

## Optional packages

Open **Setup & uninstall → Refresh package list**. The app resolves optional add-ons and their supporting libraries without installing them. Each missing package gets its own **Install** button and download progress bar. A percentage is shown when the server supplies the download size, followed by an Installing stage. Installed packages disappear from this list and stay absent after reopening Settings or restarting; the package database is the source of truth.

Each click installs exactly one package. Python wheels use `--no-deps`; system packages are downloaded individually and installed locally, with dependency checks still enforced. If a system package needs another listed package first, install that supporting package and retry. No dependency checks are bypassed. The package list is cached locally and can be refreshed when needed. The refresh fetches package metadata and may inspect wheels to resolve requirements; it does not install packages.

Model downloads no longer install Python add-ons or build the GPU engine. Those actions are separate in Setup & uninstall. The main installer installs the core runtime; optional voice-detection and GPU packages are selected from the cog panel. Install all is available alongside the individual package buttons.

## Requirements

- GNOME Shell **45-51** (the extension is tested on 51). Wayland is the target; X11 is expected to work but is untested.
- Linux **x86_64** (aarch64 should work, untested).
- A microphone, PipeWire (default on current Fedora, Ubuntu and Arch).
- About 1.7 GB of disk for the recommended GPU setup (models ~1.7 GB; Python packages ~0.3 GB; build files are temporary), less on CPU only.
- Optional: a Vulkan-capable graphics card for the fast GPU engine. Without one the installer uses the CPU engine.

The installer installs missing system packages for you (Fedora/dnf, Debian and Ubuntu/apt, Arch/pacman): `python3`, `ydotool`,
`wl-clipboard`, PipeWire and PulseAudio command-line tools, AT-SPI and GTK bindings. It asks first. Optional GPU build tools and libraries are listed separately in the cog panel.

## Install

One line in a terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/TrademarkPoIicy/gnome-voice-typing/main/get.sh | bash
```

It downloads the current version from GitHub into a temporary folder and runs `./install.sh`, which asks before anything that
needs your password. Pass installer options after `bash -s --`, for example `... | bash -s -- --cpu-only` or `... | bash -s -- --dry-run`
(shows everything it would do, changes nothing). Run the same line again to upgrade.

Prefer to look first? Clone and run it yourself:

```sh
git clone https://github.com/TrademarkPoIicy/gnome-voice-typing.git
cd gnome-voice-typing
./install.sh --dry-run     # optional: shows everything it would do, changes nothing
./install.sh
```

**Then log out and log back in** (once). On Wayland, GNOME Shell only loads a new extension at login.

### What the installer does

1. Checks your distribution, the GNOME Shell version (it **refuses cleanly** outside 45-51 unless you pass `--force`), and looks for a Vulkan GPU.
2. Installs missing system packages with your package manager (asks first; uses `sudo`). If your distribution only has the old
   ydotool 0.1.x (Ubuntu 24.04, Debian 12), ydotool 1.0.4 is built from source instead (no sudo).
3. Copies the program to `~/.local/share/gnome-voice-typing` and links `voice-typing` into `~/.local/bin`.
4. Creates a private Python environment there and installs `numpy` and `sherpa-onnx`.
5. Keeps an existing GPU engine. New GPU packages and the engine build are selected individually in Settings > Setup & uninstall.
6. Downloads the speech model(s) from their official hosts and verifies the SHA-256 of every file.
7. Writes your settings to `~/.config/gnome-voice-typing/config.json` (never overwrites an existing one).
8. Only if `/dev/uinput` is not already usable, and only if you agree: installs a udev rule (explained below).
9. Installs the extension, creates the shortcut, turns on GNOME's `toolkit-accessibility`, and enables the extension. **Every GNOME
   setting it changes is saved first** in `~/.local/share/gnome-voice-typing/install-state.json` so uninstall can restore it.
10. Adds an autostart entry so the background service keeps the model warm (invisible until you press the shortcut).

Options: `--yes` (answer yes to everything, including sudo), `--gpu` / `--cpu-only`, `--model ID`, `--binding '<Super><Alt>h'`,
`--no-keybinding`, `--no-system-deps`, `--no-udev`, `--no-autostart`, `--no-start`, `--no-cpu-fallback`, `--no-models`, `--force`.
See `./install.sh --help`. Re-running the installer is safe: it upgrades the program and keeps your models and settings.

### Why `sudo`? (and what changes system-wide)

Only two things need administrator rights, and both are optional:

1. **Distro packages** (`dnf` / `apt` / `pacman install ...`): the tools listed above. You can install them yourself and use `--no-system-deps`.
2. **The `/dev/uinput` rule.** To type into other windows the program creates a virtual keyboard with `ydotool`, which needs write
   access to `/dev/uinput` (normally root-only). The installer adds `/etc/udev/rules.d/70-gnome-voice-typing-uinput.rules`
   (one line: give the logged-in desktop user access to `/dev/uinput`) and `/etc/modules-load.d/gnome-voice-typing-uinput.conf`
   (load the `uinput` module at boot). Decline it and set it up later from Settings > Setup & uninstall (one password prompt);
   typing does not work until then. `uninstall.sh` removes both files.

> Security note: a rule that lets your user session create virtual input devices means programs running as you can inject
> keystrokes. That is also true of any program that can use the portal. If you do not want that, decline the rule.

### The `Super+H` shortcut and the minimize conflict

GNOME already binds `Super+H` to *Hide (minimize) window*. If the shortcut you choose is taken, the installer tells you which
shortcut conflicts, asks, then removes only that accelerator from that one setting and **remembers the old value**.
`uninstall.sh` puts it back. Prefer to keep minimize? Pick another shortcut: `./install.sh --binding '<Super><Alt>h'`.

## Using it

- Press the shortcut: the bar appears next to your text box and the start chime plays. Speak; each phrase is typed when you pause.
- Press it again (or say "stop listening") to pause; again to resume. Say "close dictation" or click the X to stop completely.
- Click the gear for settings. More models: press **Download · size** on a model row (progress bar, **Cancel**); when it says Installed you can pick it. **Uninstall** removes a model you are not using. Click **Save** to apply a model change (the new model loads in the background; the old one keeps working).
- Command line: `voice-typing toggle | pause | resume | close | status | quit`, `voice-typing --models`,
  `voice-typing --model parakeet`, `voice-typing --apply '{"device":"cpu"}'`, `voice-typing --apply '{"sounds":false}'`.
  Model management: `voice-typing-models list | status | install ID | cancel ID | remove ID | verify | adopt`.
  Packages: **Settings > Setup & uninstall** lists individual system packages, Python add-ons, typing permission and the graphics-card engine, including supporting libraries, each with its own **Install** button. Only the selected package is installed. Installed packages stay hidden from the install list. Engine builds do not download speech models. From a terminal, `voice-typing-models deps` lists component IDs and `voice-typing-models deps install COMPONENT` installs one item—for example `python:onnxruntime` or `system:rnnoise`. There is no install-all action.
- Accented and non-English characters are typed through the clipboard (`Ctrl+V`, or `Ctrl+Shift+V` in terminals); your clipboard is
  restored afterwards. In pop-up boxes that refuse clipboard access, accents fall back to plain letters ("café" becomes "cafe").

### Files

| Where | What |
|---|---|
| `~/.local/share/gnome-voice-typing/` | program, Python environment, GPU engine, models (`models/`), install record |
| `~/.config/gnome-voice-typing/config.json` | your settings |
| `~/.local/state/gnome-voice-typing/logs/session-*.txt` | optional developer logs, including speech text when enabled |
| `~/.local/share/gnome-shell/extensions/voice-typing@gnome-voice-typing.github.io/` | the extension |
| `~/.config/autostart/gnome-voice-typing.desktop` | start at login |
| `$XDG_RUNTIME_DIR/gnome-voice-typing*` | sockets and small state files (RAM only, gone at reboot) |

## Uninstall

```sh
~/.local/share/gnome-voice-typing/uninstall.sh --dry-run   # preview
~/.local/share/gnome-voice-typing/uninstall.sh
```

It stops the service, restores every GNOME setting the installer changed (the minimize shortcut, `toolkit-accessibility`, the
custom shortcut list, the enabled-extensions list), removes the extension, autostart entry, program, models, caches and (after asking)
your settings, and removes the udev rule (asks before using sudo). Options: `--keep-models`, `--keep-config`, `--yes`. Distro packages
are left alone. Log out and in afterwards so GNOME Shell forgets the extension.

## Troubleshooting

- **Nothing happens when I press the shortcut.** Check the shortcut exists: *Settings > Keyboard > Custom Shortcuts > "Voice typing"*.
  Run `voice-typing status` (prints `{"state": "stopped"}` when the service is not running) and read
  the latest session log in Settings → Developer tools.
- **The bar does not appear.** You need to log out and in once after installing. Check `gnome-extensions list | grep voice-typing`
  and `gnome-extensions info voice-typing@gnome-voice-typing.github.io`; make sure *"disable-user-extensions"* is off.
- **The bar is listening but nothing is typed.** Run `voice-typing --check`: `uinput writable: False` means the udev rule is missing (re-run
  `./install.sh`, press Install in Settings > Setup & uninstall, or log out and in).
- **The bar is not placed next to my text box.** It needs *toolkit-accessibility* (the installer turns it on) and apps started **after**
  that to be restarted once. Electron/Chromium apps (VS Code, Discord, Chrome) only expose text boxes when started with accessibility
  on (e.g. `--force-renderer-accessibility`); otherwise the bar falls back to the bottom of the window.
- **"Graphics card unavailable, using processor".** The Vulkan engine could not start; the CPU model is used and your choice is kept so
  the next start tries again. Check `vulkaninfo --summary` and the latest session log in Settings → Developer tools.
- **The GPU build failed.** Logs are in `~/.cache/gnome-voice-typing/build/`. Install the build packages listed above and run `./install.sh --gpu` again, or use `--cpu-only`.
- **Wrong microphone.** Pick one in the gear panel (or leave it on *System default*).
- **Download interrupted.** Run `./install.sh` again; downloads resume and are re-verified.
- **Firefox:** make sure `accessibility.force_disabled` is not `1` in `about:config`.

## Privacy

Everything happens on your computer. There is no telemetry, no analytics, no account and no cloud service, and nothing is sent anywhere
while you dictate. Audio is processed in memory and never written to disk; speech and typed text are logged only when Developer tools → Enable developer logging is turned on (off by default). The network is used only
by `install.sh` and `voice-typing-models`: distro package servers, PyPI, GitHub (whisper.cpp source; model archives) and Hugging Face (models),
all over HTTPS, with SHA-256 / commit-id verification. The program itself does not contact any server.

## Limitations

- GNOME Shell only (it is a Shell extension); tested on Wayland. Not for KDE, Xfce, etc.
- English is the best-supported language; Parakeet v3 also handles other European languages, the `.en` Whisper models are English only.
- The bar can only find a text box in apps that expose accessibility information.
- Late layout shifts after the bar appeared (for example a banner appearing in the page) do not move the bar; drag it.
- Input into some pop-over entries can lose accented characters (see above).
- The visual style of the bar is inspired by common desktop dictation bars; it is not affiliated with any other product.

## Attribution

gnome-voice-typing is MIT licensed (see `LICENSE`), copyright (c) 2026 Jesse Fisher. It uses, downloads or builds:

- **NVIDIA Parakeet-TDT 0.6B v3 / v2** - CC BY 4.0, © NVIDIA. <https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3>. *Attribution required if you redistribute it.*
- **OpenAI Whisper** (MIT), **Distil-Whisper** (MIT), **Moonshine** by Useful Sensors (MIT).
- **whisper.cpp / ggml / parakeet.cpp** (MIT), **sherpa-onnx** (Apache-2.0), **faster-whisper / CTranslate2** (MIT), **NumPy** (BSD-3).
- **ydotool** (AGPL-3.0) and **wl-clipboard** (GPL-3.0) are used as separate programs from your distribution.

Full details and links: [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md). The start/stop chimes are original (`src/sounds/make_chimes.py`).

## Development

- `scripts/make-tarball.sh` builds `dist/gnome-voice-typing-<version>.tar.gz` and its `.sha256`.
- `tests/` has the isolated install tests (a private session bus, runtime dir and config backend, never your real settings).
  `GVT_DRY_RUN=1` makes the daemon log what it would type instead of typing (developer option).
- Layout: `src/bin` (daemon, CLI, model manager), `src/extension` (GNOME Shell extension), `src/gpu` (Vulkan backend + C shim),
  `src/data` (model catalog and checksummed registry), `src/lib` (GNOME settings helper), `src/system` (udev rule).

### Appearance and individual packages (0.1.12)

The cog’s Appearance page offers separate bubble and Settings themes: Dark, Light,
High contrast, Ocean, Forest, Plum, Midnight and Warm. Tiny reduces the bubble to
half the Compact width, retaining microphone, Settings and Close controls. Type a
background opacity from 0 to 100 percent. Following clicked text fields defaults
to off; an explicitly saved preference is preserved.

In Setup & uninstall, install **pip** first if offered, then **Refresh package
list** to discover individual packages and their supporting libraries. Downloads
can run concurrently with independent progress bars. Writes to the Python
environment or system package database are serialized. System packages may still
require their listed dependencies to be installed first. Individual buttons install only the selected package. Installed packages disappear based on installed package metadata.
The graphics engine shows approximate build-stage percentages, rather than a
byte-download percentage. Settings stays open during background state changes.

### Finish setup later (0.1.14)

**Settings → Setup & uninstall → Install all** installs missing optional system
packages and their dependencies, prepares pip, installs the voice-detection
packages, sets up typing permission, and builds the graphics engine when available.
The initial GitHub installer stays lightweight. Individual package buttons remain
available for people who prefer to choose each package. Speech models are still
chosen separately. Failed batch installs show the error and can be retried;
already installed packages are skipped. System installation may ask for a password.

### Developer session logs (0.1.15)

Open **Settings → Developer tools**, enable developer logging, and click **Save**.
Reproduce the problem, then use **Copy latest log**, **Open latest log**, or
**Open log folder**. Each session is one timestamped, readable `.txt` file under
`~/.local/state/gnome-voice-typing/logs/` (or `$XDG_STATE_HOME/gnome-voice-typing/logs/`).
Attach that file when reporting a problem; nothing is uploaded automatically.

Logging starts a new file when enabled, on daemon startup, and when dictation opens
from closed. Pause/resume stays in the same file. Setup helpers and the bubble
append to the current session. The header records software/environment details;
events include partial/final speech, formatting, typing attempts and corrections,
commands, settings changes, sampled microphone/voice-detection diagnostics,
package operations, GPU build output, errors and Python exception tracebacks.
This is application diagnostics, not a raw audio recording or a system-wide recorder.

Logging is **off by default**. Turning it off and saving stops new diagnostic entries;
existing files remain available until you delete them. Files are owner-readable only.
Review personal dictation and settings before sharing. Long sessions can create large
files. Native crashes and events before logging was enabled may need separate system
logs. The explicit `GVT_DRY_RUN` development mode retains its separate legacy log.

### Microphone recovery (0.1.16)

Saving a different microphone now reconnects both dictation and the Settings meter
to the selected input. Old room-noise measurements are cleared when switching
microphones. Pause/resume closes old recorder streams before starting new ones.
Stalled audio streams time out and show a microphone error; five seconds of digital
silence shows “No microphone signal” and automatically clears when input returns.
Calibration rejects an input that sends only zero samples. Developer logs include
raw and processed microphone levels to help distinguish silence from suppression.
