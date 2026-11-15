# gnome-voice-typing

Offline voice typing for the GNOME desktop. Press a shortcut, speak, and your words are typed into whatever text box has focus:
browsers, editors, terminals, chat apps, GNOME's own dialogs. A small floating bar shows that it is listening and sits next to
the text box you are typing in.

- **Fully offline.** Speech recognition runs on your computer (graphics card or processor). No cloud, no account, no telemetry.
- **Fast.** On a graphics card, a spoken phrase is typed about 0.03-0.1 s after you stop speaking (measured on a Radeon AI PRO R9700;
  yours will differ). On a processor, 0.1-0.3 s with the default CPU model.
- **Good accuracy.** The default model is NVIDIA Parakeet-TDT 0.6B, which adds punctuation and capital letters by itself.
- **Not tied to one GPU brand.** The GPU engine uses Vulkan (AMD, Intel, NVIDIA with a Vulkan driver). No CUDA or ROCm needed.

> Status: 0.1.0, first public release. Tested on Fedora 45 with GNOME Shell 51 (Wayland) and a Radeon GPU. Debian/Ubuntu and Arch
> support is implemented in the installer but has had less testing. Please report problems.

## Features

- Start/pause with a keyboard shortcut (default `Super+H`), or click the microphone button on the bar.
- Phrase-by-phrase typing: each phrase is typed when you pause speaking.
- Voice commands: "stop listening" / "pause listening", "close dictation", "delete that" / "scratch that", "new line", "new paragraph".
- Floating bar (a GNOME Shell extension): never steals keyboard focus; draggable; opens next to the focused text box (using the
  accessibility interface, AT-SPI); red close button; start/stop chimes (original sounds, can be turned off).
- Settings (gear icon): model picker with a clear Light / Medium / Heavy system-impact tag, **Run on: graphics card or processor**
  (if one fails to load, the other is used automatically), pause after silence (10 s / 30 s / 1 min / 2 min / never), microphone,
  sounds on/off.
- Many models: Parakeet v3 (GPU) and v2 (CPU), Whisper (tiny to large-v3-turbo, GPU and CPU), Moonshine. Downloaded on demand,
  checksum-verified.

## Requirements

- GNOME Shell **45-51** (the extension is tested on 51). Wayland is the target; X11 is expected to work but is untested.
- Linux **x86_64** (aarch64 should work, untested).
- A microphone, PipeWire (default on current Fedora, Ubuntu and Arch).
- About 1.7 GB of disk for the recommended GPU setup (models ~1.7 GB; Python packages ~0.3 GB; build files are temporary), less on CPU only.
- Optional: a Vulkan-capable graphics card for the fast GPU engine. Without one the installer uses the CPU engine.

The installer installs missing system packages for you (Fedora/dnf, Debian and Ubuntu/apt, Arch/pacman): `python3`, `ydotool`,
`wl-clipboard`, PipeWire and PulseAudio command-line tools, AT-SPI bindings and, for the GPU engine, `cmake`, a C++ compiler,
`glslc` and the Vulkan development files. It asks first.

## Install

```sh
tar xzf gnome-voice-typing-0.1.0.tar.gz
cd gnome-voice-typing-0.1.0
./install.sh --dry-run     # optional: shows everything it would do, changes nothing
./install.sh
```

Verify the download first if you like: `sha256sum -c gnome-voice-typing-0.1.0.tar.gz.sha256`.

**Then log out and log back in** (once). On Wayland, GNOME Shell only loads a new extension at login.

### What the installer does

1. Checks your distribution, the GNOME Shell version (it **refuses cleanly** outside 45-51 unless you pass `--force`), and looks for a Vulkan GPU.
2. Installs missing system packages with your package manager (asks first; uses `sudo`). If your distribution only has the old
   ydotool 0.1.x (Ubuntu 24.04, Debian 12), ydotool 1.0.4 is built from source instead (no sudo).
3. Copies the program to `~/.local/share/gnome-voice-typing` and links `voice-typing` into `~/.local/bin`.
4. Creates a private Python environment there and installs `numpy` and `sherpa-onnx`.
5. GPU only: downloads whisper.cpp at one pinned commit and compiles it with Vulkan (5-15 minutes, no sudo).
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
   (load the `uinput` module at boot). Decline it and typing uses GNOME's remote-control portal instead: it works, but GNOME
   asks for permission once and shows a remote-control indicator while typing. `uninstall.sh` removes both files.

> Security note: a rule that lets your user session create virtual input devices means programs running as you can inject
> keystrokes. That is also true of any program that can use the portal. If you do not want that, decline the rule.

### The `Super+H` shortcut and the minimize conflict

GNOME already binds `Super+H` to *Hide (minimize) window*. If the shortcut you choose is taken, the installer tells you which
shortcut conflicts, asks, then removes only that accelerator from that one setting and **remembers the old value**.
`uninstall.sh` puts it back. Prefer to keep minimize? Pick another shortcut: `./install.sh --binding '<Super><Alt>h'`.

## Using it

- Press the shortcut: the bar appears next to your text box and the start chime plays. Speak; each phrase is typed when you pause.
- Press it again (or say "stop listening") to pause; again to resume. Say "close dictation" or click the X to stop completely.
- Click the gear for settings. Click **Save** to apply a model change (the new model loads in the background; the old one keeps working).
- Command line: `voice-typing toggle | pause | resume | close | status | quit`, `voice-typing --models`,
  `voice-typing --model parakeet`, `voice-typing --apply '{"device":"cpu"}'`, `voice-typing --apply '{"sounds":false}'`.
  Model management: `voice-typing-models list | install ID | remove ID | verify`.
- Accented and non-English characters are typed through the clipboard (`Ctrl+V`, or `Ctrl+Shift+V` in terminals); your clipboard is
  restored afterwards. In pop-up boxes that refuse clipboard access, accents fall back to plain letters ("café" becomes "cafe").

### Files

| Where | What |
|---|---|
| `~/.local/share/gnome-voice-typing/` | program, Python environment, GPU engine, models (`models/`), install record |
| `~/.config/gnome-voice-typing/config.json` | your settings |
| `~/.local/state/gnome-voice-typing/daemon.log` | log of the background service (no audio, no typed text is stored) |
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
  `~/.local/state/gnome-voice-typing/daemon.log`.
- **The bar does not appear.** You need to log out and in once after installing. Check `gnome-extensions list | grep voice-typing`
  and `gnome-extensions info voice-typing@gnome-voice-typing.github.io`; make sure *"disable-user-extensions"* is off.
- **The bar is listening but nothing is typed.** Run `voice-typing --check`: `uinput writable: False` means the udev rule is missing (re-run
  `./install.sh`, or log out and in). Without it the portal fallback needs you to approve GNOME's remote-control prompt once.
- **The bar is not placed next to my text box.** It needs *toolkit-accessibility* (the installer turns it on) and apps started **after**
  that to be restarted once. Electron/Chromium apps (VS Code, Discord, Chrome) only expose text boxes when started with accessibility
  on (e.g. `--force-renderer-accessibility`); otherwise the bar falls back to the bottom of the window.
- **"Graphics card unavailable, using processor".** The Vulkan engine could not start; the CPU model is used and your choice is kept so
  the next start tries again. Check `vulkaninfo --summary` and `~/.local/state/gnome-voice-typing/daemon.log`.
- **The GPU build failed.** Logs are in `~/.cache/gnome-voice-typing/build/`. Install the build packages listed above and run `./install.sh --gpu` again, or use `--cpu-only`.
- **Wrong microphone.** Pick one in the gear panel (or leave it on *System default*).
- **Download interrupted.** Run `./install.sh` again; downloads resume and are re-verified.
- **Firefox:** make sure `accessibility.force_disabled` is not `1` in `about:config`.

## Privacy

Everything happens on your computer. There is no telemetry, no analytics, no account and no cloud service, and nothing is sent anywhere
while you dictate. Audio is processed in memory and never written to disk; typed text is not logged. The network is used only
by `install.sh` and `voice-typing-models`: distro package servers, PyPI, GitHub (whisper.cpp source; model archives) and Hugging Face (models),
all over HTTPS, with SHA-256 / commit-id verification. The program itself does not contact any server.

## Limitations (0.1.0)

- GNOME Shell only (it is a Shell extension); tested on Wayland. Not for KDE, Xfce, etc.
- English is the best-supported language; Parakeet v3 also handles other European languages, the `.en` Whisper models are English only.
- The bar can only find a text box in apps that expose accessibility information.
- Late layout shifts after the bar appeared (for example a banner appearing in the page) do not move the bar; drag it.
- Input into some pop-over entries can lose accented characters (see above).
- The visual style of the bar is inspired by common desktop dictation bars; it is not affiliated with any other product.

## Extra: Samsung Galaxy Book5 Pro 360 speaker fix (Fedora)

Unrelated to dictation, and **not** installed by `install.sh`: [`extras/galaxy-book5-audio/`](extras/galaxy-book5-audio/) holds an optional
installer that makes the built-in speakers of the Samsung Galaxy Book5 Pro 360 work on Fedora (MAX98390 amplifier driver plus an EasyEffects
preset). It checks the hardware first and does nothing on any other computer. Built on
[Andycodeman/samsung-galaxy-book-linux-fixes](https://github.com/Andycodeman/samsung-galaxy-book-linux-fixes) (GPL-2.0, downloaded at a pinned,
checksum-verified commit, not bundled).

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
