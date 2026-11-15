# Extra: Samsung Galaxy Book5 Pro 360 speaker fix (Fedora)

> **This extra now lives in its own repository, which is the maintained version:
> [TrademarkPoIicy/galaxy-book-fedora-audio](https://github.com/TrademarkPoIicy/galaxy-book-fedora-audio)** - a one-command, tag-pinned installer
> (`curl -fsSL https://raw.githubusercontent.com/TrademarkPoIicy/galaxy-book-fedora-audio/v1.0.0/install.sh | bash`), release tarball with checksum, and an
> uninstall one-liner. Use that one. The copy in this folder is kept for reference and will not be updated.

This is an **optional extra** and has nothing to do with dictation. It is here because the same person runs both on
the same laptop. It is **not** installed by the main `install.sh`, and on any computer that is not this laptop it
refuses to do anything.

## What it fixes

On a Samsung Galaxy Book5 Pro 360 (model 960QHA) running Linux, the built-in speakers are silent or very quiet
because the four MAX98390 speaker amplifiers are not set up by the stock kernel. The installer:

1. installs a DKMS kernel driver for the amplifiers (`max98390-hda`, rebuilt automatically for each new kernel),
2. adds the two small systemd services that bring up the amplifiers at boot and remove the workaround again once
   your kernel supports the laptop natively,
3. installs [EasyEffects](https://github.com/wwmm/easyeffects) with the `GalaxyBook-Speakers-Warm` equaliser preset
   (needs `calf` and `lsp-plugins`) and starts it hidden at login, so the speakers sound good rather than just loud.

## Tested on

| | |
|---|---|
| Laptop | Samsung Galaxy Book5 Pro 360, model 960QHA (NP960QHA-KG1US), Intel Lunar Lake |
| Audio | Intel SOF, Realtek ALC298 codec (subsystem `144d:c1da`), four MAX98390 amplifiers on I2C bus 2 (`0x38`, `0x39`, `0x3c`, `0x3d`) |
| OS | Fedora 45, kernels 7.2.11 (running) and 7.3.3 (module also built), Secure Boot **off** |
| EasyEffects | 8.2.8 |

Not tested: Secure Boot **on** (the MOK enrollment path), other Galaxy Book models, and a from-scratch run of
`install.sh` itself (the laptop was set up by running the upstream steps by hand; this script wraps the same steps).
See "What is not verified" below.

## Install

```sh
cd extras/galaxy-book5-audio
./install.sh --dry-run    # check the hardware and show the steps; changes nothing
./install.sh              # asks once, then uses sudo where needed
```

Run it as your normal user, not with `sudo`. Reboot afterwards.

What `install.sh` does, in order:

1. **Hardware check.** The laptop must have MAX98390 amplifiers (reported by ACPI or the I2C bus) **and** be either
   a Samsung `960Q*` model (DMI product name / SKU / board name) or have the Realtek codec with subsystem id
   `144d:c1da`. On anything else, for example a desktop, it prints why and exits with status 0 without changing a
   thing. `./install.sh --check` runs only this step.
2. Requires Fedora (`dnf`). Other distributions: use the upstream project directly.
3. `sudo dnf install` of `dkms`, `kernel-devel-$(uname -r)`, `kernel-devel-matched`, `i2c-tools`, `gcc`, `make`,
   `alsa-sof-firmware`, `easyeffects`, `calf`, `lsp-plugins-lv2`.
4. Downloads the upstream project **at one pinned commit** and refuses to continue unless the `speaker-fix/` folder
   matches the SHA-256 recorded in `common.sh`. Nothing from upstream is copied into this repository.
5. Runs upstream's own `speaker-fix/install.sh` with `sudo`: DKMS build and install of `max98390-hda/1.0`,
   `/etc/modules-load.d/max98390-hda.conf`, `max98390-hda-i2c-setup.service` (creates the other three amplifier
   devices at boot), `max98390-hda-check-upstream.service`, and the helper scripts in `/usr/local/sbin`. If the same
   module is already installed for the running kernel with the same source, this step is skipped.
6. Installs the EasyEffects presets with upstream's `install-presets.sh` (never overwrites a preset you edited),
   writes `~/.config/autostart/easyeffects-service.desktop` (`easyeffects --service-mode --hide-window`, so it runs
   hidden), starts EasyEffects if it is not running, and loads `GalaxyBook-Speakers-Warm`.

It is safe to run again. If an autostart file with different content already exists it is left alone.

### Secure Boot

If Secure Boot is on, the DKMS module must be signed with a key your firmware trusts. Upstream's installer creates
a local signing key and runs `mokutil --import`: you choose a one-time password, **reboot, and enter that password in
the blue "MOK Manager" screen**. Until you do, the kernel refuses the speaker modules and the speakers stay silent.
(Or turn Secure Boot off in the firmware setup.) `install.sh` tells you before it starts.

## Uninstall

```sh
./uninstall.sh --dry-run
./uninstall.sh
```

Stops and removes the two services, the module (DKMS), `/etc/modules-load.d/max98390-hda.conf`, the helper scripts and
`/usr/src/max98390-hda-1.0`, then removes the five EasyEffects presets **only if you have not edited them** and the autostart file
if it is the one this installer wrote. Packages stay installed (the command to remove them is printed). Reboot afterwards.

## Headphones and other outputs

The Warm preset is meant for the built-in speakers. If it is applied to headphones or HDMI they will sound wrong. In
EasyEffects open *Presets > Autoload* and add one rule per output device: the built-in speaker gets
`GalaxyBook-Speakers-Warm`, headphones get `GalaxyBook-Headphones`. Other presets that were installed: `-Warm-Loud`,
`-Clear`, `-Clear-Loud`.

## Testing the hardware check

`common.sh` reads `/sys/class/dmi/id`, `/sys/bus/acpi/devices`, `/sys/bus/i2c/devices` and `/proc/asound/card*/codec#0`. For testing
against saved values you can point it somewhere else with `GBA_DMI_DIR`, `GBA_ACPI_DIR`, `GBA_I2C_DIR` and
`GBA_CODEC_GLOB`, then run `./install.sh --check`. Normal use never sets these.

## What is not verified

- A full end-to-end run of `install.sh` on a clean Fedora 45 Galaxy Book (not done; only the checks, the checksum verification and
  `--dry-run` were exercised).
- Secure Boot enabled.
- That `easyeffects -l GalaxyBook-Speakers-Warm` (used to load the preset right away) behaves the same on every EasyEffects
  version. If it does not, the installer says so and you pick the preset in the EasyEffects window.

## Credit and licenses

All the real work here is by **Andycodeman and contributors**:
**[Andycodeman/samsung-galaxy-book-linux-fixes](https://github.com/Andycodeman/samsung-galaxy-book-linux-fixes)**
(`speaker-fix/`), pinned to commit
[`a6b13c5dd6b5ca5bccca3e8c417c77e3a3a0d138`](https://github.com/Andycodeman/samsung-galaxy-book-linux-fixes/tree/a6b13c5dd6b5ca5bccca3e8c417c77e3a3a0d138/speaker-fix).
That project is licensed under the **GNU General Public License v2.0**
([LICENSE](https://github.com/Andycodeman/samsung-galaxy-book-linux-fixes/blob/a6b13c5dd6b5ca5bccca3e8c417c77e3a3a0d138/LICENSE)); the kernel
module sources carry `SPDX-License-Identifier: GPL-2.0` and `MODULE_LICENSE("GPL")`. The EasyEffects presets there derive from
[JackHack96/EasyEffects-Presets](https://github.com/JackHack96/EasyEffects-Presets) (see `LICENSE-JackHack96` in upstream's
`easyeffects-presets/` folder).

Because of that license, **no upstream code is vendored in this repository**. `install.sh` downloads it from upstream at install
time and verifies it; you receive it under upstream's license. Only the glue in this folder (`install.sh`, `uninstall.sh`,
`common.sh`, this README) is part of gnome-voice-typing and is MIT-licensed like the rest of the repository.
