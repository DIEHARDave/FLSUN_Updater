# FLSUN_Updater's Moonraker component, loaded by its [flsun_plugins] section.
#
# 1. Plugin macros. ENABLE_PLUGIN, DISABLE_PLUGIN and LIST_PLUGINS (in
#    klipper/flsun_updater_macros.cfg) call the remote methods registered
#    here, which set the plugin to yes or no in flsun_plugins.cfg. Saving
#    that file starts flsun_updater.service, which installs or removes it.
#    PLUGINS opens a Mainsail/Fluidd window, like Z-Mod's, to do the same
#    from buttons and to turn each plugin's updates on or off ([updates]
#    in flsun_plugins.cfg).
#
# 2. Release updates. Takes over the [update_manager <name>] type: web
# sections named in web_clients, and fixes what FLSun's older Moonraker gets
# wrong for them:
#   - it downloads a release's first asset; this picks `asset_name`, from
#     the newest release on the channel that has it,
#   - its persistent_files only match top-level names,
#   - zipfile drops the executable bits,
#   - Moonraker runs as pi, but some installs must be owned by root.
# The download happens here; the install runs as root in
# flsun_updater_web.service (flsun_updater.py web-apply), which also runs the
# plugin's hooks, e.g. restarting the screen.
#
# 3. Update all. Takes over Moonraker's full update, so Mainsail's and
#    Fluidd's "Update all components" button installs every update except
#    the OS packages, and skips repos it can't update (local changes,
#    invalid) instead of stopping there. UPDATE_ALL [CHECK=1] does the same
#    from the console and shows the progress there.
#
# Installed by FLSUN_Updater as a symlink in Moonraker's components folder.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import asyncio
import json
import logging
import pathlib
import re
import time

from .update_manager.common import Channel
try:
    # Moonraker 0.10 and newer (FLSUN_moonraker)
    from .update_manager.net_deploy import NetDeploy as WebBase
    NET_DEPLOY = True
except ImportError:
    # FLSun's 2023 Moonraker
    from .update_manager.web_deploy import WebClientDeploy as WebBase
    NET_DEPLOY = False

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
if TYPE_CHECKING:
    from ..confighelper import ConfigHelper

STAGING = pathlib.Path("~/printer_data/flsun_updater").expanduser()
SELECTION = pathlib.Path("~/printer_data/config/flsun_plugins.cfg").expanduser()
CHOICE_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*[:=]\s*(\S+)")
ON_WORDS = ("yes", "true", "on", "1")
APPLY_SERVICE = "flsun_updater_web"
APPLY_TIMEOUT = 15 * 60

# Update all: never the OS packages (the "System" row; it can still be
# updated on its own). Klipper and Moonraker go last, as in Moonraker's own
# full update. flsun_updater goes just before them: its service run can
# restart Moonraker when the update manager entries change.
SKIP_ALL = ("system",)
LAST = ("flsun_updater", "klipper", "moonraker")
# Moonraker's full update stops at the first repo it can't update; these
# are left out with a reason instead.
UPDATE_PROBLEMS = (
    ("is_valid", False, "not a valid install"),
    ("is_dirty", True, "repo has local changes"),
    ("corrupt", True, "repo is corrupt"),
    ("detached", True, "repo is on a detached HEAD"),
    ("channel_invalid", True, "configured channel is invalid"),
)
DOWNLOAD_RE = re.compile(r"^Downloading .*\[\d+%\]$")


def has_update(info: Dict[str, Any]) -> bool:
    if info.get("configured_type") == "git_repo":
        behind = info.get("commits_behind_count")
        if behind is None:
            behind = len(info.get("commits_behind") or [])
        return behind > 0
    remote = info.get("remote_version")
    if not remote or remote == "?":
        return False
    return info.get("version") != remote


def update_problem(info: Dict[str, Any]) -> Optional[str]:
    for key, bad, reason in UPDATE_PROBLEMS:
        if key in info and info[key] == bad:
            return reason
    return None


class FlsunWebDeploy(WebBase):
    def __init__(self, config: ConfigHelper, cmd_helper: Any) -> None:
        if NET_DEPLOY:
            super().__init__(config)
        else:
            super().__init__(config, cmd_helper)
        # Newer Moonraker resets self.asset_name from release_info.json, so
        # the configured name is kept separately.
        self.flsun_asset: str = config.get("asset_name")
        self.asset_name = self.flsun_asset

    async def initialize(self) -> Dict[str, Any]:
        storage = await super().initialize()
        self.asset_name = self.flsun_asset
        return storage

    # The base class's names for these changed in Moonraker 0.10.
    def _is_ok(self) -> bool:
        return getattr(self, "_is_valid", getattr(self, "_valid", False))

    async def _revalidate(self) -> None:
        if NET_DEPLOY:
            await self._validate_release_info()
            self.asset_name = self.flsun_asset
        else:
            await self._validate_client_info()

    def _log_info_block(self) -> None:
        if NET_DEPLOY:
            self._log_app_info()
        else:
            self._log_client_info()

    def _pick(self, release: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for asset in release.get("assets", []):
            if asset.get("name") == self.flsun_asset:
                return asset
        return None

    async def _github(self, resource: str) -> Optional[Any]:
        client = self.cmd_helper.get_http_client()
        resp = await client.github_api_request(
            resource, attempts=3, retry_pause_time=.5)
        if resp.status_code == 304:
            return resp.json() if resp.content else None
        if resp.has_error():
            self.log_info(f"Github Request Error - {resp.error}")
            self.last_error = str(resp.error)
            return None
        self.last_error = ""
        return resp.json()

    async def _get_remote_version(self) -> None:
        if not self._is_ok():
            self.log_info("Invalid installation, aborting remote refresh")
            return
        releases = await self._github(
            f"repos/{self.repo}/releases?per_page=30")
        if not isinstance(releases, list):
            return
        for release in releases:
            if release.get("draft"):
                continue
            if release.get("prerelease") and self.channel == Channel.STABLE:
                continue
            asset = self._pick(release)
            if asset is None:
                continue
            self.remote_version = release.get("tag_name", "?")
            self.dl_info = (asset.get("browser_download_url", "?"),
                            asset.get("content_type", "?"),
                            asset.get("size", 0))
            self._is_prerelease = release.get("prerelease", False)
            return
        self.log_info(f"No {self.channel} release has {self.flsun_asset}")

    async def rollback(self) -> bool:
        if self.rollback_version in ("?", self.version):
            return False
        release = await self._github(
            f"repos/{self.repo}/releases/tags/{self.rollback_version}")
        asset = self._pick(release) if isinstance(release, dict) else None
        if asset is None:
            raise self.server.error(
                f"Release {self.rollback_version} has no {self.flsun_asset}")
        return await self.update((asset.get("browser_download_url", "?"),
                                  asset.get("content_type", "?"),
                                  asset.get("size", 0)))

    async def update(
        self,
        rollback_info: Optional[Tuple[str, str, int]] = None,
        is_recover: bool = False,
        force_dep_update: bool = False
    ) -> bool:
        if not self._is_ok():
            raise self.server.error(
                f"{self.name}: invalid install detected, aborting update")
        if rollback_info is not None:
            # Newer Moonraker's recover reinstalls the current version.
            dl_url, content_type, size = rollback_info
            target = self.version if is_recover else self.rollback_version
            start_msg = (f"Recovering {self.name}..." if is_recover
                         else f"Rolling back {self.name} to {target}...")
        else:
            if self.remote_version == "?":
                await self._get_remote_version()
                if self.remote_version == "?":
                    raise self.server.error(
                        f"{self.repo}: no release with {self.flsun_asset}")
            dl_url, content_type, size = self.dl_info
            target = self.remote_version
            if self.version == target:
                return False
            start_msg = f"Updating {self.name} to {target}..."
        if dl_url == "?":
            raise self.server.error(f"{self.repo}: invalid download url")
        current_version = self.version
        self.cmd_helper.notify_update_response(start_msg)
        STAGING.mkdir(parents=True, exist_ok=True)
        zip_path = STAGING.joinpath(f"{self.name}.zip")
        request = STAGING.joinpath(f"{self.name}.request.json")
        result = STAGING.joinpath(f"{self.name}.result.json")
        for path in (zip_path, request, result):
            if path.exists():
                path.unlink()
        client = self.cmd_helper.get_http_client()
        try:
            await client.download_file(
                dl_url, content_type, zip_path, size,
                self.cmd_helper.on_download_progress)
            self.cmd_helper.notify_update_response(
                f"Download complete, installing to '{self.path}'")
            request.write_text(json.dumps({
                "name": self.name, "zip": str(zip_path), "version": target}))
            machine = self.server.lookup_component("machine")
            await machine.do_service_action("start", APPLY_SERVICE)
            outcome = await self._wait_for(result)
        finally:
            for path in (zip_path, request):
                if path.exists():
                    path.unlink()
        for line in outcome.get("log", []):
            self.cmd_helper.notify_update_response(line)
        if not outcome.get("ok"):
            raise self.server.error(
                f"{self.name}: {outcome.get('error', 'install failed')}")
        await self._revalidate()
        if self._is_ok() and rollback_info is None:
            self.rollback_version = current_version
            self.rollback_repo = self.repo
        msg = (f"Rollback Complete: {self.name}" if rollback_info is not None
               else f"Client Update Finished: {self.name}")
        self.cmd_helper.notify_update_response(msg, is_complete=True)
        self._log_info_block()
        self._save_state()
        return True

    async def _wait_for(self, result: pathlib.Path) -> Dict[str, Any]:
        deadline = time.time() + APPLY_TIMEOUT
        while time.time() < deadline:
            if result.exists():
                try:
                    outcome = json.loads(result.read_text())
                except ValueError:
                    pass  # still being written
                else:
                    result.unlink()
                    return outcome
            await asyncio.sleep(1.)
        return {"ok": False, "error": f"{APPLY_SERVICE} did not finish; "
                "see logs/flsun_updater.log"}


class FlsunPlugins:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.server.register_remote_method(
            "flsun_plugin_set", self._remote_set)
        self.server.register_remote_method(
            "flsun_plugin_list", self._remote_list)
        self._pending: Dict[Tuple[str, str], bool] = {}
        for method, func in (
                ("flsun_plugin_menu", self._remote_menu),
                ("flsun_plugin_menu_toggle", self._remote_menu_toggle),
                ("flsun_plugin_menu_updates", self._remote_menu_updates),
                ("flsun_plugin_menu_save", self._remote_menu_save),
                ("flsun_plugin_menu_close", self._remote_menu_close)):
            self.server.register_remote_method(method, func)
        self.server.register_remote_method(
            "flsun_update_all", self._remote_update_all)
        self.server.register_event_handler(
            "update_manager:update_response", self._on_update_response)
        self._console_run = False
        self.full_hooked = False
        # Optional components load in no fixed order; update_manager must
        # have built its updaters and endpoints before they can be replaced.
        self.umgr = self.server.load_component(config, "update_manager", None)
        if self.umgr is None:
            self.server.add_warning(
                "[flsun_plugins]: update_manager is not loaded")
            return
        self.full_hooked = self._hook_full_update()
        names: List[str] = config.getlist("web_clients", [], separator=None)
        umgr = self.umgr
        for name in names:
            section = f"update_manager {name}"
            if section not in config.sections():
                self.server.add_warning(
                    f"[flsun_plugins]: no [{section}] section")
                continue
            try:
                umgr.updaters[name] = FlsunWebDeploy(
                    config.getsection(section), umgr.cmd_helper)
            except Exception as e:
                self.server.add_warning(
                    f"[flsun_plugins]: can't manage {name}: {e}")
            else:
                logging.info(f"flsun_plugins: managing {name}")

    # -------------------------------------------------------- update all

    def _hook_full_update(self) -> bool:
        """Route Moonraker's full update (Mainsail's and Fluidd's "Update
        all" button) to _full_update, which leaves out the OS packages."""
        umgr = self.umgr
        needed = (hasattr(umgr, "cmd_request_lock")
                  and hasattr(umgr, "kconn")
                  and hasattr(umgr.cmd_helper, "set_full_complete"))
        if not needed:
            logging.info("flsun_plugins: this Moonraker's update manager is "
                         "not supported, Update all stays as it is")
            return False
        # /machine/update/upgrade and /client without a name look the
        # method up on the instance at call time.
        umgr._handle_full_update_request = self._full_update
        # /machine/update/full keeps the method it was registered with, in
        # its (frozen) APIDefinition, shared by HTTP and JSON-RPC.
        api = None
        try:
            from ..common import APIDefinition
            api = APIDefinition.get_cache().get("/machine/update/full")
        except (ImportError, AttributeError):
            app = getattr(self.server, "moonraker_app", None)
            api = getattr(app, "api_cache", {}).get("/machine/update/full")
        if api is None:
            logging.info("flsun_plugins: /machine/update/full not found")
        else:
            object.__setattr__(api, "callback", self._full_update)
        logging.info("flsun_plugins: Update all leaves out the OS packages")
        return True

    def _update_plan(self) -> Tuple[List[str], List[Tuple[str, str]]]:
        """The updaters with an update, in install order, and the ones left
        out with the reason."""
        todo: List[str] = []
        skipped: List[Tuple[str, str]] = []
        for name, updater in list(self.umgr.updaters.items()):
            if name in SKIP_ALL:
                continue
            try:
                info = updater.get_update_status()
            except Exception:
                continue
            if not info.get("configured_type") or not has_update(info):
                continue
            reason = update_problem(info)
            if reason:
                skipped.append((name, reason))
            else:
                todo.append(name)
        todo.sort(key=lambda n: LAST.index(n) if n in LAST else -1)
        return todo, skipped

    async def _full_update(self, web_request: Any) -> str:
        umgr = self.umgr
        ch = umgr.cmd_helper
        if umgr.kconn.is_printing():
            raise self.server.error("Update Refused: Klippy is printing", 503)
        if ch.is_update_busy():
            raise self.server.error("An update is already running", 503)
        async with umgr.cmd_request_lock:
            ch.set_update_info("full", id(web_request), True)
            ch.notify_update_response(
                "Preparing full software update (OS packages are left out, "
                "update System on its own)...")
            done: List[str] = []
            failed: List[str] = []
            try:
                todo, skipped = self._update_plan()
                for name, reason in skipped:
                    ch.notify_update_response(f"Skipping {name}: {reason}")
                for name in todo:
                    if umgr.kconn.is_printing():
                        ch.notify_update_response(
                            f"A print started, stopping before {name}")
                        break
                    try:
                        await self._update_one(name)
                    except Exception as e:
                        logging.exception(f"flsun_plugins: updating {name}")
                        ch.notify_update_response(
                            f"Error updating {name}: {e}")
                        failed.append(name)
                    else:
                        done.append(name)
                summary = "Full Update Complete"
                if not todo:
                    summary += ", everything was up to date"
                else:
                    summary += f", updated {len(done)}"
                    if done:
                        summary += ": " + ", ".join(done)
                if failed:
                    summary += ". Failed: " + ", ".join(failed)
                ch.set_full_complete(True)
                ch.notify_update_response(summary, is_complete=True)
            except Exception as e:
                ch.set_full_complete(True)
                ch.notify_update_response(
                    f"Full update stopped: {e}", is_complete=True)
                raise
            finally:
                ch.clear_update_info()
        return "ok"

    async def _update_one(self, name: str) -> None:
        """One updater, as Moonraker's own full update does it."""
        umgr = self.umgr
        ch = umgr.cmd_helper
        updater = umgr.updaters[name]
        if name == "klipper":
            umgr.klippy_identified_evt = asyncio.Event()
            try:
                check_restart = await updater.update()
                if ch.needs_service_restart(name):
                    await updater.restart_service()
                    check_restart = True
                if check_restart:
                    ch.notify_update_response(
                        "Waiting for Klippy to reconnect (this may take up "
                        "to 2 minutes)...")
                    try:
                        await asyncio.wait_for(
                            umgr.klippy_identified_evt.wait(), 120.)
                    except asyncio.TimeoutError:
                        ch.notify_update_response(
                            "Klippy reconnect timed out...")
                    else:
                        ch.notify_update_response("Klippy Reconnected")
            finally:
                umgr.klippy_identified_evt = None
        elif name == "moonraker":
            await updater.update()
            if ch.needs_service_restart(name):
                await updater.restart_service()
        else:
            await updater.update()

    async def _refresh_all(self) -> None:
        """Refresh every updater except the OS packages (slow, and not
        installed here anyway)."""
        umgr = self.umgr
        async with umgr.cmd_request_lock:
            for name, updater in list(umgr.updaters.items()):
                if name in SKIP_ALL:
                    continue
                try:
                    await updater.refresh()
                except Exception as e:
                    logging.info(f"flsun_plugins: refresh {name}: {e}")
        umgr.cmd_helper.notify_update_refreshed()

    def _on_update_response(self, notification: Dict[str, Any]) -> None:
        # UPDATE_ALL from the console: show the progress there too.
        if not self._console_run:
            return
        msg = str(notification.get("message", "")).strip()
        if not msg or DOWNLOAD_RE.match(msg):
            return
        for line in msg.splitlines()[:5]:
            self._respond(f"UPDATE_ALL: {line}")

    def _remote_update_all(self, check: int = 0) -> None:
        if self.umgr is None or not self.full_hooked:
            self._respond("UPDATE_ALL: this Moonraker is not supported, use "
                          "Update all in Mainsail or Fluidd")
            return
        if self._console_run or self.umgr.cmd_helper.is_update_busy():
            self._respond("UPDATE_ALL: an update is already running")
            return
        self._console_run = True
        self.server.get_event_loop().register_callback(
            self._console_update_all, bool(int(check)))

    async def _console_update_all(self, check: bool) -> None:
        try:
            if self.umgr.kconn.is_printing():
                self._respond("UPDATE_ALL: refused, the printer is printing")
                return
            self._respond("UPDATE_ALL: checking for updates...")
            await self._refresh_all()
            todo, skipped = self._update_plan()
            if check or not todo:
                # Otherwise _full_update reports them.
                for name, reason in skipped:
                    self._respond(f"UPDATE_ALL: skipping {name}: {reason}")
            if not todo:
                self._respond("UPDATE_ALL: everything is up to date (OS "
                              "packages are not checked)")
                return
            status = {n: self.umgr.updaters[n].get_update_status()
                      for n in todo}
            self._respond(f"UPDATE_ALL: {len(todo)} update(s): " + ", ".join(
                f"{n} {status[n].get('version')} -> "
                f"{status[n].get('remote_version')}" for n in todo))
            if check:
                self._respond("UPDATE_ALL: check only, run UPDATE_ALL to "
                              "install them")
                return
            if "moonraker" in todo:
                self._respond("UPDATE_ALL: Moonraker goes last and restarts "
                              "itself; the update is done when it is back")
            await self._full_update(object())
        except Exception as e:
            logging.exception("flsun_plugins: UPDATE_ALL")
            self._respond(f"UPDATE_ALL: stopped: {e}")
        finally:
            self._console_run = False

    # ------------------------------------------------------------ macros

    def _respond(self, msg: str) -> None:
        # Shows in the Mainsail and Fluidd console without Klipper's
        # [respond] section.
        self.server.send_event("server:gcode_response", f"// {msg}")

    def _read_choices(self) -> Tuple[List[str], Dict[str, int]]:
        """The file's lines, and the line index of each plugin's choice."""
        lines = SELECTION.read_text().splitlines() if SELECTION.exists() else []
        found: Dict[str, int] = {}
        in_plugins = False
        for i, line in enumerate(lines):
            if line.startswith("["):
                in_plugins = line.strip() == "[plugins]"
                continue
            m = CHOICE_RE.match(line)
            if in_plugins and m:
                found[m.group(1).lower()] = i
        return lines, found

    def _remote_set(self, name: str = "", enabled: bool = True) -> None:
        name = str(name).strip().lower()
        lines, found = self._read_choices()
        if not lines:
            self._respond("FLSUN_Updater is still setting up; try again "
                          "in a minute")
            return
        if name not in found:
            self._respond(f"No plugin named '{name}'. LIST_PLUGINS shows "
                          "the plugins.")
            return
        i = found[name]
        current = CHOICE_RE.match(lines[i]).group(2).lower()
        wanted = "yes" if enabled else "no"
        if (current in ("yes", "true", "on", "1")) == enabled:
            self._respond(f"{name} is already set to {wanted}")
            return
        lines[i] = f"{name}: {wanted}"
        # Saving the file starts flsun_updater.service (flsun_updater.path).
        SELECTION.write_text("\n".join(lines) + "\n")
        action = "Installing" if enabled else "Removing"
        self._respond(f"{action} {name}. It happens after any running "
                      "print finishes; progress is in "
                      "logs/flsun_updater.log")

    # ------------------------------------------------------- PLUGINS menu
    #
    # A Mainsail/Fluidd dialog like Z-Mod's PLUGINS: one row per plugin,
    # the plugin's button (installed or not) and its Update button (listed
    # in the update manager or not). Clicks only mark changes; Save writes
    # them to flsun_plugins.cfg in one go, which starts FLSUN_Updater.
    # The dialog is drawn with "// action:prompt_..." lines, the same as
    # Klipper's RESPOND TYPE=command sends.

    def _prompt(self, *lines: str) -> None:
        for line in lines:
            self.server.send_event("server:gcode_response",
                                   f"// action:{line}")

    def _menu_rows(self) -> List[Dict[str, Any]]:
        lines = SELECTION.read_text().splitlines() if SELECTION.exists() else []
        rows: List[Dict[str, Any]] = []
        updates: Dict[str, bool] = {}
        section = ""
        for i, line in enumerate(lines):
            if line.startswith("["):
                section = line.strip()
                continue
            m = CHOICE_RE.match(line)
            if not m:
                continue
            name, on = m.group(1).lower(), m.group(2).lower() in ON_WORDS
            if section == "[plugins]":
                # Above each choice: "# Title: description", then its URL
                title = name
                if i >= 2 and lines[i - 2].startswith("# "):
                    title = lines[i - 2][2:].split(": ", 1)[0].strip() or name
                rows.append({"name": name, "title": title, "on": on})
            elif section == "[updates]":
                updates[name] = on
        for row in rows:
            row["updates"] = updates.get(row["name"], True)
        return rows

    def _menu_show(self) -> None:
        rows = self._menu_rows()
        if not rows:
            self._respond("FLSUN_Updater is still setting up; try again "
                          "in a minute")
            return
        out = ["prompt_begin Plugins (FLSUN_Updater)",
               "prompt_text Left: install or remove. Right: list it in the "
               "update manager or not. Orange: changed; Save applies it "
               "(after any running print)."]
        for row in rows:
            name = row["name"]
            on = self._pending.get(("plugin", name), row["on"])
            upd = self._pending.get(("updates", name), row["updates"])
            color = ("warning" if ("plugin", name) in self._pending
                     else "success" if on else "secondary")
            ucolor = ("warning" if ("updates", name) in self._pending
                      else "success" if upd else "secondary")
            out += [
                "prompt_button_group_start",
                f"prompt_button {row['title']}|"
                f"_FLSUN_PLUGIN_MENU_TOGGLE NAME={name}|{color}",
                f"prompt_button Update: {'On' if upd else 'Off'}|"
                f"_FLSUN_PLUGIN_MENU_UPDATES NAME={name}|{ucolor}",
                "prompt_button_group_end",
            ]
        out += [
            "prompt_footer_button Save|_FLSUN_PLUGIN_MENU_SAVE|"
            + ("warning" if self._pending else "success"),
            "prompt_footer_button Close|_FLSUN_PLUGIN_MENU_CLOSE|secondary",
            "prompt_show",
        ]
        self._prompt(*out)

    def _remote_menu(self) -> None:
        self._pending = {}
        self._menu_show()

    def _menu_flip(self, kind: str, name: str) -> None:
        name = str(name).strip().lower()
        row = next((r for r in self._menu_rows() if r["name"] == name), None)
        if row is None:
            self._respond(f"No plugin named '{name}'")
            return
        saved = row["on"] if kind == "plugin" else row["updates"]
        current = self._pending.get((kind, name), saved)
        if not current == saved:
            # Clicked back to what is saved: no change
            self._pending.pop((kind, name), None)
        else:
            self._pending[(kind, name)] = not saved
        self._menu_show()

    def _remote_menu_toggle(self, name: str = "") -> None:
        self._menu_flip("plugin", name)

    def _remote_menu_updates(self, name: str = "") -> None:
        self._menu_flip("updates", name)

    def _remote_menu_close(self) -> None:
        self._pending = {}
        self._prompt("prompt_end")

    def _remote_menu_save(self) -> None:
        pending, self._pending = self._pending, {}
        self._prompt("prompt_end")
        if not pending:
            self._respond("Plugins: nothing changed")
            return
        lines = SELECTION.read_text().splitlines() if SELECTION.exists() else []
        if not lines:
            self._respond("FLSUN_Updater is still setting up; try again "
                          "in a minute")
            return
        found: Dict[Tuple[str, str], int] = {}
        updates_at = -1
        section = ""
        for i, line in enumerate(lines):
            if line.startswith("["):
                section = line.strip()
                if section == "[updates]":
                    updates_at = i
                continue
            m = CHOICE_RE.match(line)
            if m and section in ("[plugins]", "[updates]"):
                kind = "plugin" if section == "[plugins]" else "updates"
                found[(kind, m.group(1).lower())] = i
        added: List[str] = []
        done: List[str] = []
        for (kind, name), value in sorted(pending.items()):
            text = f"{name}: {'yes' if value else 'no'}"
            if (kind, name) in found:
                lines[found[(kind, name)]] = text
            elif kind == "updates":
                added.append(text)
            if kind == "plugin":
                done.append(f"{'installing' if value else 'removing'} {name}")
            else:
                done.append(f"updates {'on' if value else 'off'} for {name}")
        if added:
            if updates_at < 0:
                lines += ["", "[updates]"]
                updates_at = len(lines) - 1
            lines[updates_at + 1:updates_at + 1] = added
        # Saving the file starts flsun_updater.service (flsun_updater.path).
        SELECTION.write_text("\n".join(lines) + "\n")
        self._respond("Plugins: " + ", ".join(done) + ". It happens after "
                      "any running print; progress is in "
                      "logs/flsun_updater.log, and Moonraker restarts if "
                      "the update manager changes.")

    def _remote_list(self) -> None:
        lines, found = self._read_choices()
        if not found:
            self._respond("No plugins listed yet")
            return
        self._respond("Plugins (ENABLE_PLUGIN NAME=... / "
                      "DISABLE_PLUGIN NAME=...):")
        for name, i in sorted(found.items()):
            value = CHOICE_RE.match(lines[i]).group(2).lower()
            on = value in ("yes", "true", "on", "1")
            self._respond(f"  {name}: {'enabled' if on else 'disabled'}")


def load_component(config: ConfigHelper) -> FlsunPlugins:
    return FlsunPlugins(config)
