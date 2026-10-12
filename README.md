# FLSUN_Updater

Installs, updates and removes plugins for the **FLSun T1 Pro** from Mainsail or Fluidd, and fixes the parts of FLSun's system image that stop updates from working.

## What it fixes

FLSun's system image can't reach GitHub as shipped, and its update manager can't update Mainsail. FLSUN_Updater fixes this at install and again at every boot:

- **Clock.** The printer starts with a stale date on every boot and never syncs, so every HTTPS connection fails. A boot service sets the clock.
- **Certificates.** The `/etc/ssl/certs` links are missing, so curl rejects every certificate. They're rebuilt.
- **git.** git's https support was deleted. It's reinstalled.
- **Mainsail updates.** FLSun ships `~/mainsail` owned by root, but Moonraker runs as `pi`, so updating Mainsail fails with `Permission denied`. The folder is given to `pi`.
- **Stuck update checks.** If the update manager last checked while the clock was wrong, its certificate checks failed and its next automatic check can be years away. Those checks are run again.
- **`sudo: unable to resolve host`.** The printer's hostname is added to `/etc/hosts`.

## Plugins

| Plugin | What it does |
| --- | --- |
| [FLSUN_Timelapse](https://github.com/DIEHARDave/FLSUN_Timelapse) | Timelapse videos in Mainsail and Fluidd, based on moonraker-timelapse |
| [FLSUN_RemoteScreen](https://github.com/DIEHARDave/FLSUN_RemoteScreen) | View and control the touchscreen from a browser |

The list lives in [plugins.json](plugins.json). To ask for another plugin, [open a plugin request](https://github.com/DIEHARDave/FLSUN_Updater/issues/new?template=plugin_request.yml). Plugin authors: see [docs/adding-plugins.md](docs/adding-plugins.md).

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

The installer restarts Moonraker but not Klipper, so it's safe to run during a print. Run it again after a FLSun firmware update.

## Use

In Mainsail or Fluidd, open **flsun_plugins.cfg** in the config files. Each plugin has a line:

```ini
flsun_timelapse: yes
flsun_remote_screen: no
```

Set a plugin to `yes` to install it or `no` to remove it, then save. The printer makes the change within a few seconds. If a print is running, it waits until the print finishes, because plugin installers restart Klipper. Progress is logged in **logs/flsun_updater.log**.

Installed plugins, FLSUN_Updater itself and Mainsail show up in the update manager (**Machine** page in Mainsail, **Settings → Software Updates** in Fluidd), where you update them as usual. When FLSUN_Updater updates and the list has new plugins, they're added to `flsun_plugins.cfg` set to `no`.

Plugins that were installed before FLSUN_Updater are detected and start as `yes`.

From SSH, `~/FLSUN_Updater/flsun_updater.py list` shows the plugins, and `sudo ~/FLSUN_Updater/flsun_updater.py run` applies `flsun_plugins.cfg` and runs the repairs.

## How it works

- `flsun_updater.service` runs `flsun_updater.py run` as root at boot, after the clock is set. It also runs when `flsun_plugins.cfg` is saved (`flsun_updater.path`) and when Moonraker updates FLSUN_Updater.
- Plugins are cloned into `~` as `pi`, and their own `install.sh` and `uninstall.sh` scripts do the work. FLSun's image asks `pi` for a password for `sudo`, so the updater allows passwordless `sudo` for `pi` only while those scripts run, and removes the rule afterwards.
- Most plugins add their own update manager entry. For those that don't, FLSUN_Updater writes one into `flsun_updater_plugins.conf`. It never adds an entry that another config file already declares, because Moonraker won't start with a duplicate.

## Uninstall

```sh
~/FLSUN_Updater/uninstall.sh
rm -rf ~/FLSUN_Updater
```

Plugins stay installed. Set them to `no` first if you want them removed. The boot service that sets the clock stays as well, because the other plugins use it.

## License

GPL-3.0. See [LICENSE](LICENSE).
