# FLSUN_Updater

Installs, updates and removes plugins for the **FLSun T1 Pro** from Mainsail, Fluidd or a macro, and fixes the parts of FLSun's system image that stop updates from working.

## What it fixes

FLSun's system image can't reach GitHub as shipped, and its update manager can't update Mainsail. FLSUN_Updater fixes this at install and again at every boot:

- **Clock.** The printer starts with a stale date on every boot and never syncs, so every HTTPS connection fails. A boot service sets the clock.
- **Certificates.** The `/etc/ssl/certs` links are missing, so curl rejects every certificate. They're rebuilt.
- **git.** git's https support was deleted. It's reinstalled.
- **Mainsail updates.** FLSun ships `~/mainsail` owned by root, but Moonraker runs as `pi`, so updating Mainsail fails with `Permission denied`. The folder is given to `pi`.
- **Stuck update checks.** If the update manager last checked while the clock was wrong, its certificate checks failed and its next automatic check can be years away. Those checks are run again.
- **Release updates (`type: web`).** FLSun's older Moonraker downloads the wrong file from a release, loses files listed in `persistent_files` that sit in subfolders, drops the executable bits, and can't update folders owned by root. FLSUN_Updater's Moonraker component handles these entries instead.
- **`sudo: unable to resolve host`.** The printer's hostname is added to `/etc/hosts`.
- **Failed services.** Three of FLSun's services fail at every boot and show in `systemctl --failed`: `adbd` (FLSun deleted its script), `monitor` (FLSun's `/etc/monitor` has a syntax error, so it never runs) and `systemd-modules-load` (`/etc/modules` lists Raspberry Pi drivers this board doesn't have). The first two are disabled and the driver lines are commented out, with the original kept as `/etc/modules.flsun`. Each is only changed while it's broken, so if a FLSun firmware update fixes one, it's left alone.

## Plugins

The plugin list is [extra_plugins.moonraker.conf](extra_plugins.moonraker.conf): Moonraker `[update_manager]` entries. Most are `git_repo` pointers to a plugin's repo, and `type: web` entries install another project's GitHub releases directly.

| Plugin | What it does |
| --- | --- |
| `flsun_timelapse` | [FLSUN_Timelapse](https://github.com/DIEHARDave/FLSUN_Timelapse): timelapse videos in Mainsail with no need for FLSun phone app to retrive videos. |
| `flsun_remote_screen` | [FLSUN_RemoteScreen](https://github.com/DIEHARDave/FLSUN_RemoteScreen): view and control the touchscreen from a browser at `http://<printer-ip>/screen/` |
| `flsun_notify` | [FLSUN_notify](https://github.com/DIEHARDave/FLSUN_notify): print notifications with a camera photo to Telegram, Discord, Matrix and [many more services](https://github.com/caronc/apprise/wiki) |
| `flsun_macros` | [FLSUN_Macros](https://github.com/DIEHARDave/FLSUN_Macros): `PRINT_START` and `PRINT_END` (heat soak and Z offset per material, a bed mesh per bed temperature, a prime line), pause at layer (`SET_PAUSE_AT_LAYER`), and macros for filament, PID tuning and the chamber light. The Orca profile below needs it. |
| `flsun_theme` | [FLSUN_theme](https://github.com/DIEHARDave/FLSUN_theme): FLSun's theme (logo and backgrounds) for Mainsail, and as the FLSUN theme in Fluidd; your previous theme folders are backed up and put back when you disable it |
| `fluidd` | [Fluidd](https://github.com/fluidd-core/fluidd) at `http://<printer-ip>:81`, beside Mainsail on port 80, from Fluidd's own releases. Both show the same cameras, and FLSUN_RemoteScreen's `/screen/` works in both. |
| `flsun_moonraker` | [FLSUN_moonraker](https://github.com/DIEHARDave/FLSUN_moonraker): current upstream Moonraker in place of FLSun's 2023 version, updated from the update manager afterwards. It installs Python 3.13 beside the system's 3.7 and puts back the compiler FLSun removed. The install takes about 10 minutes, and Moonraker is down for the last minute. |
| `flsun_klipper` | [FLSUN_klipper](https://github.com/DIEHARDave/FLSUN_klipper): current upstream Klipper in place of FLSun's 2022 fork, without flashing the mainboard. FLSun's Klipper stays installed beside it, and one command switches between them. Needs `flsun_moonraker` (see below). |
| `helixscreen` | [HelixScreen](https://github.com/prestonbrown/helixscreen) in place of FLSun's stock screen (beta). Until HelixScreen's own releases include the T1 Pro, its T1 builds come from [DIEHARDave/helixscreen](https://github.com/DIEHARDave/helixscreen/releases), starting with v1.1.0-beta.8-T1. |

### Plugins that depend on each other

| Plugin | Needs | Order |
| --- | --- | --- |
| `flsun_klipper` | `flsun_moonraker` | **Install `flsun_moonraker` first**, or turn both on at the same time: plugins turned on together are installed in the order of the table above, so Moonraker comes first. Turned on alone, `flsun_klipper` stops with a message and changes nothing. Turning `flsun_moonraker` off later doesn't break `flsun_klipper`, because Python 3.13 and the compiler stay. |

The other plugins don't depend on each other and can be turned on in any order:

- `flsun_timelapse`, `flsun_remote_screen` and `flsun_notify` make the same system fixes, so whichever comes first does them. Timelapse and notify use the printer's camera, and the Screen webcam that `flsun_remote_screen` adds is listed after it.
- `flsun_theme` gives Fluidd an FLSUN theme preset when `fluidd` is installed, before or after the theme, and puts it back after each Fluidd update.
- `flsun_macros` works with FLSun's Klipper and with `flsun_klipper`, which uses an upstream version of its pause-at-layer macros automatically.
- `flsun_remote_screen` follows whichever screen is running: FLSun's or `helixscreen`.

### OrcaSlicer profile

An OrcaSlicer printer profile for the T1 Pro set up for these plugins: **https://cloud.orcaslicer.com/b/4a1e3c1b305c**

Its start and end G-code call `PRINT_START` and `PRINT_END` from `flsun_macros`, so turn that plugin on before printing with it. It also reports layers (`SET_PRINT_STATS_INFO`), which pause at layer and FLSUN_klipper's power loss recovery use, and takes timelapse frames for `flsun_timelapse`.

To ask for another plugin, [open a plugin request](https://github.com/DIEHARDave/FLSUN_Updater/issues/new?template=plugin_request.yml). Plugin authors: see [docs/adding-plugins.md](docs/adding-plugins.md).

## Install

SSH into the printer as `pi` (the default password is `1`), then:

```sh
sudo ntpdate -u -b time.google.com
curl -fsSL --cacert /etc/ssl/certs/ca-certificates.crt https://raw.githubusercontent.com/DIEHARDave/FLSUN_Updater/main/prepare.sh | bash
cd ~
git clone https://github.com/DIEHARDave/FLSUN_Updater.git
~/FLSUN_Updater/install.sh
```

The first two commands set the clock and repair certificates and git so that `git clone` works. Don't use `timedatectl set-ntp true`: on this image it stops the time service rather than starting it.

The installer restarts Moonraker, and Klipper too unless a print is running (Klipper has to restart to load the plugin macros). Run it again after a FLSun firmware update.

## Use

From the console in Mainsail or Fluidd:

```
LIST_PLUGINS
ENABLE_PLUGIN NAME=flsun_timelapse
DISABLE_PLUGIN NAME=flsun_timelapse
```

The macros set the plugin to `yes` or `no` in **flsun_plugins.cfg**, which you can also edit directly in the config files:

```ini
[plugins]
flsun_timelapse: yes
flsun_remote_screen: no
```

When the file changes, the printer installs or removes plugins within a few seconds. If a print is running, it waits until the print finishes, because installers restart Klipper or the screen. Progress is logged in **logs/flsun_updater.log**. Plugins that were installed before FLSUN_Updater are detected and start as `yes`. Each `type: web` plugin also gets a section, such as `[helixscreen]`, with its release `channel` (`stable` or `beta`).

Installed plugins, FLSUN_Updater itself and Mainsail show up in the update manager (**Machine** page in Mainsail, **Settings → Software Updates** in Fluidd), where you update them as usual. When FLSUN_Updater updates and the list has new plugins, they're added to `flsun_plugins.cfg` set to `no`.

**You don't need to install the System updates.** With `flsun_moonraker`, the update manager also lists about 170 **System** packages: Debian 10's last security and bug-fix updates. The printer and the plugins work the same without them, so leaving them is fine. They're also safe to install while the printer is idle; FLSUN_moonraker holds back FLSun's own camera libraries, so three of them may stay listed. To hide the list, add `enable_system_updates: False` under `[update_manager]` in `moonraker.conf`. See [FLSUN_moonraker's notes](https://github.com/DIEHARDave/FLSUN_moonraker#system-packages).

<img src="docs/update-manager.png" alt="Mainsail's Update Manager on a T1 Pro listing flsun_notify, flsun_remote_screen, flsun_timelapse, flsun_updater, Mainsail and the system packages, all up to date" width="600">

From SSH, `~/FLSUN_Updater/flsun_updater.py list` shows the plugins, and `sudo ~/FLSUN_Updater/flsun_updater.py run` applies `flsun_plugins.cfg` and runs the repairs.

## Recovery from a USB stick

If a plugin leaves the printer unreachable, for example with no Wi-Fi, a USB stick can turn it off without a network:

1. Download [recovery/flsun_recovery.cfg](recovery/flsun_recovery.cfg) and copy it to the top folder of a FAT32 USB stick, keeping its name.
2. Set the plugins to turn off to `no`. It turns off HelixScreen as it comes, which brings FLSun's stock screen back, along with its Wi-Fi setup. `all: no` turns off every plugin.

   ```ini
   [plugins]
   helixscreen: no
   ```

3. Plug the stick into the printer and turn it on, or plug it in while the printer is on.

The plugins are removed as if you had set them to `no` in `flsun_plugins.cfg`, after any running print. The file is then renamed `flsun_recovery.done.cfg`, so it isn't applied again while the stick stays in, and `flsun_recovery.log` on the stick shows what happened. A recovery file only turns plugins off; turn them back on as usual once the printer is reachable.

### Restoring a Klipper backup

The same file can put back a backup of Klipper, for example FLSun's stock Klipper after an upgrade that went wrong. Copy the backup into a folder named `flsun_restore` on the stick, and add a `[restore]` section naming it:

```ini
[restore]
klipper: klipper_stock
```

```
flsun_recovery.cfg
flsun_restore/klipper_stock.tar.gz
flsun_restore/klipper_stock.tar.gz.sha256
```

The backup is a `tar` archive made from `/`, with its SHA-256 beside it. After any running print, the printer checks the backup against its `.sha256`, then stops Klipper. It renames the files that the backup replaces to `<name>.pre_restore-<date>` (it doesn't delete them), unpacks the backup and restarts Klipper. Only Klipper's own files can be restored: Klipper and its Python environment, the printer config, `klipper.service` and the few files FLSun's Klipper keeps in `/home/pi`. The printer refuses a backup that holds anything else.

If [FLSUN_klipper](https://github.com/DIEHARDave/FLSUN_klipper) runs upstream Klipper and the backup includes FLSun's Klipper (`/home/pi/klipper`), the klipper service is switched back to FLSun's Klipper too; FLSUN_klipper's switch is kept beside it as `*.pre_restore-<date>`.

This restores the printer's computer only. If the printer's mainboard firmware was also changed, it has to be put back too, or Klipper can't connect to the mainboard.

This works on printers whose FLSUN_Updater already has it, so update FLSUN_Updater before you need it.

## How it works

- `flsun_updater.service` runs `flsun_updater.py run` as root at boot, after the clock is set. It also runs when `flsun_plugins.cfg` changes (`flsun_updater.path`) and when Moonraker updates FLSUN_Updater.
- **`git_repo` plugins** are cloned into `path` as `pi`. If the repo has an `install.sh`, it runs; `uninstall.sh` runs on removal. FLSun's image asks `pi` for a password for `sudo`, so the updater allows passwordless `sudo` for `pi` only while those scripts run, and removes the rule afterwards.
- **`type: web` plugins** are unpacked from the release zip named by `asset_name`. Updates from the update manager are downloaded by FLSUN_Updater's Moonraker component (`moonraker/flsun_plugins.py`) and installed as root by `flsun_updater_web.service`. A plugin can have a hook in `hooks/` for anything else; HelixScreen's switches the screen's boot script and restarts the screen.
- Installed plugins' entries are copied into `flsun_updater_plugins.conf`, which `moonraker.conf` includes. An entry that the plugin's own installer already added isn't copied, because Moonraker won't start with a duplicate.
- FLSun's udev rules mount USB sticks under `~/printer_data/gcodes` (`/etc/um`). A drop-in for those mount units starts `flsun_updater_usb.service` once a stick is mounted, at boot or later. It runs the same `flsun_updater.py run`, which first looks for `flsun_recovery.cfg` on each mounted stick.
- The macros (`klipper/flsun_updater_macros.cfg`, included from `printer.cfg`) call Moonraker remote methods in the same component. Replies appear in the console, without needing Klipper's `[respond]`.

Plugin installers run with full `sudo` rights, so every plugin is reviewed before it's listed. Updates to a listed plugin reach printers without another review, so only list repos whose maintainers you trust.

## Uninstall

```sh
~/FLSUN_Updater/uninstall.sh
rm -rf ~/FLSUN_Updater
```

Plugins stay installed. Disable them first if you want them removed. The uninstaller restarts Klipper, so don't run it during a print. The boot service that sets the clock stays, because the other plugins use it.

## License

GPL-3.0. See [LICENSE](LICENSE).
