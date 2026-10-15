# FLSUN_Updater's Moonraker component, loaded by its [flsun_plugins] section.
#
# 1. Plugin macros. ENABLE_PLUGIN, DISABLE_PLUGIN and LIST_PLUGINS (in
#    klipper/flsun_updater_macros.cfg) call the remote methods registered
#    here, which set the plugin to yes or no in flsun_plugins.cfg. Saving
#    that file starts flsun_updater.service, which installs or removes it.
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
CHOICE_RE = re.compile(r"^([A-Za-z0-9_]+)\s*[:=]\s*(\S+)")
APPLY_SERVICE = "flsun_updater_web"
APPLY_TIMEOUT = 15 * 60


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
        names: List[str] = config.getlist("web_clients", [], separator=None)
        if not names:
            return
        # Optional components load in no fixed order; update_manager must
        # have built its updaters before they can be replaced.
        umgr = self.server.load_component(config, "update_manager", None)
        if umgr is None:
            self.server.add_warning(
                "[flsun_plugins]: update_manager is not loaded")
            return
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
