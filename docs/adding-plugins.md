# Adding a plugin

Any public GitHub repo that runs on the FLSun T1 Pro can be listed. To ask for one, [open a plugin request](https://github.com/DIEHARDave/FLSUN_Updater/issues/new?template=plugin_request.yml), or open a pull request that adds a section to [extra_plugins.moonraker.conf](../extra_plugins.moonraker.conf).

Installer scripts run with full `sudo` rights, so each plugin is reviewed before it's listed. Updates to a listed plugin reach printers through Moonraker's update manager without another review, so only list repos whose maintainers you trust.

## What the printer gives a plugin

- Debian 10 (buster) on a Cortex-A7 (armhf), 715 MB of RAM.
- Python 3.7 without pip. Moonraker runs in `~/moonraker-env` (Python 3.7, tornado 6.2).
- FLSun's own Klipper (1.0.1.5) and an older Moonraker (API 1.3.0). Moonraker is not a git checkout; its components live in `~/moonraker/moonraker/components`.
- Unless the user turned on FLSUN_moonraker (current upstream Moonraker on Python 3.13, which also puts the compiler back) or FLSUN_klipper (upstream Klipper, which runs `printer_upstream.cfg`; `[include]` lines added to `printer.cfg` are copied over at each start). A plugin should work with both.
- Mainsail 2.12 or newer at `~/mainsail`, served by nginx from `/etc/nginx/sites-available/mainsail`.
- Config files in `~/printer_data/config`, logs in `~/printer_data/logs`.
- FLSUN_Updater has already fixed the clock, certificates and git.

## The entry

Each plugin is a Moonraker `[update_manager <name>]` section, the same format as Zmod's `extra_plugins.moonraker.conf`. `<name>` is what users type in `ENABLE_PLUGIN NAME=<name>`: lowercase letters, digits and `_`.

### A git repo

```ini
[update_manager quickswap]
type: git_repo
channel: stable
path: ~/quickswap
origin: https://github.com/someone/quickswap.git
primary_branch: master
is_system_service: False
flsun_title: QuickSwap
flsun_description: One line about what it does
```

FLSUN_Updater clones `origin` into `path` (it must be under `~`). If the repo has an `install.sh`, it runs as `pi` from the repo's folder, with `sudo` available without a password, and `uninstall.sh` runs on removal. FLSUN_Updater deletes the folder afterwards. A plugin that only needs cloning, such as a macro file the user includes, needs neither script.

Scripts must be safe to run again and exit with an error if they fail. If a script restarts Klipper, it should refuse to run during a print. FLSUN_Updater waits for prints to finish, but people also run installers by hand.

All the Moonraker options are copied into the printer's config, so the entry is also the plugin's update manager entry. If the plugin's `install.sh` adds its own `[update_manager <name>]` section, use the same `<name>`; FLSUN_Updater then doesn't copy its entry, because Moonraker won't start with a duplicate.

On FLSun's Moonraker, `managed_services` accepts only `klipper`, `moonraker` and the plugin's own name. To have a plugin's own service restarted after an update, give the systemd service the plugin's name. For entries it copies, FLSUN_Updater adds that name to `moonraker.asvc`. Set `is_system_service: False` if the plugin has no service.

### Another project's releases

```ini
[update_manager helixscreen]
type: web
channel: beta
repo: prestonbrown/helixscreen
path: ~/helixscreen
asset_name: helixscreen-flsun-t1.zip
persistent_files:
    config/settings.json
    config/helixscreen.env
flsun_owner: root
flsun_hook: helixscreen
```

`asset_name` is the release file to install. It must be a zip of the folder's contents with a `release_info.json` at its root (`{"project_name", "project_owner", "version"}`), the same as Mainsail's and Fluidd's releases. Releases without that asset are skipped. Users can switch `channel` between `stable` and `beta` in `flsun_plugins.cfg`.

An existing install whose `release_info.json` names the same repo is adopted rather than downloaded again.

### FLSUN_Updater's options

Options starting with `flsun_` are read by FLSUN_Updater and never reach Moonraker.

| Option | Meaning |
| --- | --- |
| `flsun_title` | Name shown in `flsun_plugins.cfg`. Defaults to `<name>`. |
| `flsun_description` | One line shown in `flsun_plugins.cfg`. |
| `flsun_install`, `flsun_uninstall` | `git_repo`: other script names, or `none`. Default `install.sh` and `uninstall.sh`, run when present. |
| `flsun_owner` | `web`: `pi` (default) or `root`, the owner of the unpacked files. |
| `flsun_hook` | `web`: a module in [hooks/](../hooks) that does the rest of the setup. It can define `post_extract(spec, old_root, new_root, log)`, called with the new release unpacked beside the old one; `after_install(spec, log)`, called after an install, update or rollback; `at_boot(spec, log)`, called on every run while installed; and `before_remove(spec, log)`. They run as root. |

## Before you list it

On a T1 Pro with FLSUN_Updater installed:

1. Add the section to `~/FLSUN_Updater/extra_plugins.moonraker.conf`.
2. Run `sudo ~/FLSUN_Updater/flsun_updater.py run`. The plugin appears in `LIST_PLUGINS`, disabled.
3. Run `ENABLE_PLUGIN NAME=<name>`. Check `logs/flsun_updater.log`, and check that the plugin works and shows up in the update manager.
4. Run `DISABLE_PLUGIN NAME=<name>`. Check that it was removed cleanly and that Moonraker still starts.
5. Undo the local edit with `git -C ~/FLSUN_Updater checkout extra_plugins.moonraker.conf`, so that the update manager doesn't report the repo as modified.
