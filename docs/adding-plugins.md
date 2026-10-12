# Adding a plugin

Any public GitHub repo that runs on the FLSun T1 Pro can be listed. To ask for one, [open a plugin request](https://github.com/DIEHARDave/FLSUN_Updater/issues/new?template=plugin_request.yml), or open a pull request that adds an entry to [plugins.json](../plugins.json).

The installer scripts run with full `sudo` rights, so each plugin is reviewed before it's listed. Updates to a listed plugin reach printers through Moonraker's update manager without another review, so only list repos whose maintainers you trust.

## What the printer gives a plugin

- Debian 10 (buster) on a Cortex-A7 (armhf), 715 MB of RAM.
- Python 3.7 without pip. Moonraker runs in `~/moonraker-env` (Python 3.7, tornado 6.2).
- FLSun's own Klipper (1.0.1.5) and an older Moonraker (API 1.3.0). Moonraker is not a git checkout; its components live in `~/moonraker/moonraker/components`.
- Mainsail 2.12 at `~/mainsail`, served by nginx from `/etc/nginx/sites-available/mainsail`.
- Config files in `~/printer_data/config`, logs in `~/printer_data/logs`.
- FLSUN_Updater has already fixed the clock, certificates and git.

## The plugin's repo

The repo needs:

- **`install.sh`.** It runs as `pi` from the plugin's folder, with `sudo` available without a password. It must be safe to run again, and should exit with an error if it fails. If it restarts Klipper, it should refuse to run during a print. FLSUN_Updater waits for prints to finish, but people also run installers by hand.
- **`uninstall.sh`.** It undoes `install.sh`, also as `pi`. FLSUN_Updater deletes the plugin's folder afterwards, so don't leave anything there that the user needs.
- **An update manager entry, from either side.** Either `install.sh` adds a `[update_manager <name>]` section to Moonraker's config, using the same `<name>` as the plugins.json entry, and `uninstall.sh` removes it. Or the plugins.json entry gives the options, and FLSUN_Updater writes the section. Not both: Moonraker won't start when a section is declared twice.

The repo can include its own copies of `prepare.sh` and `system/flsun-timesync*` so that it also installs on its own. Keep them byte-identical to FLSUN_Updater's.

## The plugins.json entry

```json
{
    "name": "flsun_example",
    "title": "FLSUN_Example",
    "description": "One line about what it does",
    "repo": "https://github.com/someone/FLSUN_Example.git",
    "branch": "main",
    "path": "~/FLSUN_Example",
    "install": "install.sh",
    "uninstall": "uninstall.sh",
    "update_manager": "plugin"
}
```

| Field | Meaning |
| --- | --- |
| `name` | Lowercase letters, digits and `_`. The key in `flsun_plugins.cfg` and the update manager name. |
| `title` | Name shown in `flsun_plugins.cfg`. |
| `description` | One line shown in `flsun_plugins.cfg`. |
| `repo` | git URL to clone. |
| `branch` | Branch to clone. Defaults to `main`. |
| `path` | Where to clone it. Must be under `~`. |
| `install`, `uninstall` | Scripts in the repo, relative to its root. `uninstall` is required when `update_manager` is `"plugin"`. |
| `install_args`, `uninstall_args` | Optional lists of extra arguments for the scripts. |
| `update_manager` | `"plugin"` if `install.sh` adds the update manager entry. Otherwise an object of options for FLSUN_Updater to write, for example `{"managed_services": "klipper"}`. `type: git_repo`, `path`, `origin` and `primary_branch` are filled in, and the object can override them. |

On FLSun's Moonraker, `managed_services` accepts only `klipper`, `moonraker`, and the plugin's own `name`. To have a plugin's own service restarted after an update, name the systemd service exactly like the plugin. FLSUN_Updater adds that name to `moonraker.asvc` for entries it writes.

## Before you list it

On a T1 Pro with FLSUN_Updater installed:

1. Add the entry to `~/FLSUN_Updater/plugins.json`.
2. Run `sudo ~/FLSUN_Updater/flsun_updater.py run`. The new plugin appears in `flsun_plugins.cfg` set to `no`.
3. Set it to `yes` and save. Check `logs/flsun_updater.log`, and check that the plugin works and shows up in the update manager.
4. Set it to `no` and save. Check that it was removed cleanly and that Moonraker still starts.
5. Undo the local edit with `git -C ~/FLSUN_Updater checkout plugins.json`, so that the update manager doesn't report the repo as modified.
