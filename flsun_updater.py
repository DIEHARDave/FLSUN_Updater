#!/usr/bin/env python3
"""FLSUN_Updater for the FLSun T1 Pro.

Installs and removes the plugins listed in extra_plugins.moonraker.conf to
match the choices in ~/printer_data/config/flsun_plugins.cfg, adds their
update manager entries, and repairs the parts of FLSun's system image that
stop Moonraker's update manager from working.

    flsun_updater.py run        Repair, then install or remove plugins.
                                Needs root. The flsun_updater service runs
                                this at boot, whenever flsun_plugins.cfg is
                                saved, and after Moonraker updates
                                FLSUN_Updater; flsun_updater_usb runs it
                                when a USB stick is mounted, to apply a
                                flsun_recovery.cfg on it.
    flsun_updater.py web-apply  Install the releases Moonraker downloaded
                                for type: web plugins. Needs root; run by
                                the flsun_updater_web service.
    flsun_updater.py list       Show the plugins and which are installed.

Standard library only: the T1 Pro has Python 3.7 and no pip.
"""

import argparse
import configparser
import contextlib
import fcntl
import glob
import importlib.util
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPO_DIR = os.path.dirname(os.path.realpath(__file__))
REGISTRY = os.path.join(REPO_DIR, "extra_plugins.moonraker.conf")
HOOKS_DIR = os.path.join(REPO_DIR, "hooks")
USER = "pi"
USER_PW = pwd.getpwnam(USER)
HOME = USER_PW.pw_dir
DATA_DIR = os.path.join(HOME, "printer_data")
CONFIG_DIR = os.path.join(DATA_DIR, "config")
MOONRAKER_CONF = os.path.join(CONFIG_DIR, "moonraker.conf")
SELECTION = os.path.join(CONFIG_DIR, "flsun_plugins.cfg")
GENERATED = os.path.join(CONFIG_DIR, "flsun_updater_plugins.conf")
ASVC = os.path.join(DATA_DIR, "moonraker.asvc")
STATE = os.path.join(DATA_DIR, "flsun_updater_state.json")
STAGING = os.path.join(DATA_DIR, "flsun_updater")
LOG = os.path.join(DATA_DIR, "logs", "flsun_updater.log")
COMPONENT_SRC = os.path.join(REPO_DIR, "moonraker", "flsun_plugins.py")
COMPONENT_LINK = os.path.join(HOME, "moonraker", "moonraker", "components",
                              "flsun_plugins.py")
MACROS_SRC = os.path.join(REPO_DIR, "klipper", "flsun_updater_macros.cfg")
MACROS_LINK = os.path.join(CONFIG_DIR, "flsun_updater_macros.cfg")
# Web clients that Moonraker updates by deleting and re-extracting the folder.
WEB_CLIENTS = [os.path.join(HOME, "mainsail"), os.path.join(HOME, "fluidd")]
# sudo ignores files in sudoers.d whose names contain a dot, so the temporary
# name is inert until the rename.
SUDOERS = "/etc/sudoers.d/zz-flsun-updater"
# Recovery without a network: a USB stick with this file turns plugins off.
# FLSun's udev rules mount sticks under ~/printer_data/gcodes (/etc/um);
# HelixScreen mounts them read-only under /mnt/usb.
RECOVERY_FILE = "flsun_recovery.cfg"
RECOVERY_DONE = "flsun_recovery.done.cfg"
RECOVERY_LOG = "flsun_recovery.log"
# Installed from system/: the file's name there, and where it goes.
UNITS = [
    ("flsun_updater.service", "/etc/systemd/system/flsun_updater.service"),
    ("flsun_updater.path", "/etc/systemd/system/flsun_updater.path"),
    ("flsun_updater_web.service",
     "/etc/systemd/system/flsun_updater_web.service"),
    ("flsun_updater_usb.service",
     "/etc/systemd/system/flsun_updater_usb.service"),
    # FLSun's mount units: usb-mount@ for a partition, u-mount@ for a stick
    # without a partition table.
    ("usb-mount-dropin.conf",
     "/etc/systemd/system/usb-mount@.service.d/flsun_updater.conf"),
    ("usb-mount-dropin.conf",
     "/etc/systemd/system/u-mount@.service.d/flsun_updater.conf"),
]
LOCK = "/run/flsun_updater.lock"
WEB_LOCK = "/run/flsun_updater_web.lock"
MOONRAKER = "http://127.0.0.1:7125"
# FLSun's image boots with a stale clock; a time before this isn't real.
CLOCK_VALID_AFTER = 1790000000  # 2026-09-21
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
USER_ENV = {
    "HOME": HOME,
    "USER": USER,
    "LOGNAME": USER,
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "TERM": "dumb",
}

_log_file = None
_collect = None  # web-apply returns its log lines to Moonraker


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    if _log_file is not None:
        _log_file.write(line + "\n")
        _log_file.flush()
    if _collect is not None:
        _collect.append(msg)


def open_log():
    global _log_file
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    if os.path.exists(LOG) and os.path.getsize(LOG) > 512 * 1024:
        os.replace(LOG, LOG + ".1")
    _log_file = open(LOG, "a")
    os.chown(LOG, USER_PW.pw_uid, USER_PW.pw_gid)


def require_root(command):
    if os.geteuid() != 0:
        sys.exit("flsun_updater.py %s needs root: sudo %s %s"
                 % (command, sys.argv[0], command))


def give_to_user(path):
    os.chown(path, USER_PW.pw_uid, USER_PW.pw_gid)


def write_if_changed(path, text):
    """Writes text to path as the pi user's file; returns True if it changed."""
    try:
        with open(path) as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass
    with open(path, "w") as f:
        f.write(text)
    give_to_user(path)
    return True


def run(cmd):
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True)
    if res.returncode != 0:
        raise RuntimeError("%s failed: %s" % (" ".join(cmd), res.stdout.strip()))


def run_logged(cmd, cwd=None, as_user=False):
    """Runs cmd, copying its output to the log; returns True on success."""
    if as_user:
        cmd = ["runuser", "-u", USER, "--"] + cmd
    env = USER_ENV if as_user else None
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, errors="replace")
    except OSError as e:
        log("  could not run %s: %s" % (cmd[0], e))
        return False
    for line in proc.stdout:
        line = ANSI_RE.sub("", line.rstrip())
        if line:
            log("  | " + line)
    return proc.wait() == 0


def user_output(cmd, cwd):
    # As pi, so that git trusts pi's checkouts.
    if os.geteuid() != USER_PW.pw_uid:
        cmd = ["runuser", "-u", USER, "--"] + cmd
    res = subprocess.run(
        cmd, cwd=cwd, env=USER_ENV,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, universal_newlines=True)
    return res.stdout.strip() if res.returncode == 0 else None


def take_lock(path):
    lock = open(path, "w")
    fcntl.flock(lock, fcntl.LOCK_EX)  # one at a time; others wait
    return lock


# ---------------------------------------------------------------- Moonraker

def moonraker(method, path, timeout=10):
    req = urllib.request.Request(MOONRAKER + path, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())["result"]


def wait_moonraker(limit):
    deadline = time.time() + limit
    while True:
        try:
            moonraker("GET", "/server/info")
            return True
        except (OSError, ValueError, KeyError):
            if time.time() > deadline:
                return False
            time.sleep(5)


def print_state():
    try:
        res = moonraker("GET", "/printer/objects/query?print_stats=state")
        return res["status"]["print_stats"]["state"]
    except (OSError, ValueError, KeyError):
        return None


def wait_until_idle():
    """Plugin installers restart Klipper or the screen."""
    waiting = False
    while print_state() in ("printing", "paused"):
        if not waiting:
            log("A print is running; waiting for it to finish")
            waiting = True
        time.sleep(60)


def restart_service(name):
    log("Restarting " + name)
    subprocess.call(["systemctl", "restart", name])


# ------------------------------------------------------------ system repairs

def fix_hosts():
    """sudo prints 'unable to resolve host' on every call without this."""
    hostname = socket.gethostname()
    with open("/etc/hosts") as f:
        text = f.read()
    for line in text.splitlines():
        fields = line.split("#", 1)[0].split()
        if hostname in fields[1:]:
            return
    log("Adding %s to /etc/hosts" % hostname)
    with open("/etc/hosts", "a") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write("127.0.1.1\t%s\n" % hostname)


def fix_certificates():
    if glob.glob("/etc/ssl/certs/*.0"):
        return
    log("Rebuilding the certificate links")
    run_logged(["update-ca-certificates", "--fresh"])


def check_git():
    if not os.access("/usr/lib/git-core/git-remote-https", os.X_OK):
        log("WARNING: git has no https support; run ~/FLSUN_Updater/install.sh "
            "again to restore it")


def fix_web_clients():
    """FLSun ships ~/mainsail owned by root, but Moonraker runs as pi and
    updates a web client by deleting and re-extracting its folder."""
    uid, gid = USER_PW.pw_uid, USER_PW.pw_gid
    for client in WEB_CLIENTS:
        if not os.path.isdir(client) or os.path.islink(client):
            continue
        paths = [client]
        for root, dirs, files in os.walk(client):
            paths.extend(os.path.join(root, n) for n in dirs + files)
        wrong = [p for p in paths if os.lstat(p).st_uid != uid]
        if not wrong:
            continue
        log("Giving %s to %s so the update manager can update it"
            % (client, USER))
        for p in wrong:
            os.lchown(p, uid, gid)


def fix_refresh_times():
    """Repairs updaters that last checked for updates while the clock was
    wrong. A check made with the clock in the future leaves a refresh time
    that the next automatic check waits for, for years, and certificates
    looked expired then, so it failed anyway."""
    if time.time() < CLOCK_VALID_AFTER:
        log("The clock looks wrong; not checking the update manager")
        return
    if not wait_moonraker(300):
        log("Moonraker is not answering; not checking the update manager")
        return
    try:
        stored = moonraker("GET",
                           "/server/database/item?namespace=update_manager")
        stored = stored.get("value") or {}
        status = moonraker("GET", "/machine/update/status")["version_info"]
    except (OSError, ValueError, KeyError) as e:
        log("Could not read the update manager's state: %s" % e)
        return
    now = time.time()
    names = []
    for name, info in status.items():
        saved = stored.get(name)
        future = (isinstance(saved, dict)
                  and saved.get("last_refresh_time", 0) > now + 3600)
        cert_error = "certificate" in str(info.get("last_error", "")).lower()
        if future or cert_error:
            names.append(name)
    for name in names:
        log("Checking %s for updates again (last check failed or had the "
            "wrong time)" % name)
        path = "/machine/update/refresh?name=" + urllib.parse.quote(name)
        for attempt in range(10):
            try:
                moonraker("POST", path, timeout=180)
                break
            except urllib.error.HTTPError as e:
                if e.code != 503:  # 503: busy or its own first check
                    log("  refresh failed: HTTP %d" % e.code)
                    break
            except (OSError, ValueError) as e:
                log("  refresh failed: %s" % e)
                break
            time.sleep(30)


def ensure_units():
    """Installs FLSUN_Updater's systemd units from the repo, so that an
    update from the update manager also brings new or changed units."""
    changed = False
    for unit, dst in UNITS:
        src = os.path.join(REPO_DIR, "system", unit)
        with open(src) as f:
            text = f.read()
        try:
            with open(dst) as f:
                if f.read() == text:
                    continue
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o644)
        log("Installed " + dst)
        changed = True
    if changed:
        subprocess.call(["systemctl", "daemon-reload"])


def ensure_component():
    """Links the Moonraker component behind the plugin macros and type: web
    updates. Returns True if Moonraker needs a restart to load it."""
    if not os.path.isdir(os.path.dirname(COMPONENT_LINK)):
        log("WARNING: Moonraker's components folder not found; the plugin "
            "macros and type: web updates won't work")
        return False
    if os.path.realpath(COMPONENT_LINK) == COMPONENT_SRC:
        return False
    tmp = COMPONENT_LINK + ".tmp"
    with contextlib.suppress(FileNotFoundError):
        os.remove(tmp)
    os.symlink(COMPONENT_SRC, tmp)
    os.lchown(tmp, USER_PW.pw_uid, USER_PW.pw_gid)
    os.replace(tmp, COMPONENT_LINK)
    log("Linked the Moonraker component")
    return True


# ------------------------------------------------------------------ plugins

def expand(path):
    if path == "~" or path.startswith("~/"):
        return HOME + path[1:]
    return path


def plugin_url(p):
    if p["kind"] == "web":
        return "https://github.com/%s (releases)" % p["repo"]
    return re.sub(r"\.git$", "", p["repo"])


def load_registry():
    """Reads extra_plugins.moonraker.conf: Moonraker [update_manager]
    sections, plus flsun_* options that only FLSUN_Updater reads."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    with open(REGISTRY) as f:
        parser.read_file(f)
    plugins = []
    for section in parser.sections():
        m = re.match(r"update_manager\s+(\S+)$", section)
        o = dict(parser.items(section))
        name = m.group(1) if m else ""
        path = os.path.realpath(expand(o.get("path", "")))
        hook = o.get("flsun_hook")
        kind = {"git_repo": "git", "web": "web"}.get(o.get("type"))
        if kind == "git":
            repo, ok = o.get("origin", ""), bool(o.get("origin"))
        else:
            repo = o.get("repo", "")
            ok = (bool(GITHUB_REPO_RE.match(repo))
                  and bool(o.get("asset_name"))
                  and o.get("channel", "stable") in ("stable", "beta"))
        if (not m or not NAME_RE.match(name) or name == "flsun_updater"
                or kind is None or not ok or not path.startswith(HOME + "/")
                or o.get("flsun_owner", USER) not in (USER, "root")
                or (hook and not os.path.isfile(
                    os.path.join(HOOKS_DIR, hook + ".py")))):
            log("Skipping an invalid entry in %s: [%s]"
                % (os.path.basename(REGISTRY), section))
            continue
        plugins.append({
            "kind": kind,
            "name": name,
            "title": o.get("flsun_title", name),
            "description": o.get("flsun_description", ""),
            "repo": repo,
            "path": o["path"],
            "abspath": path,
            "branch": o.get("primary_branch", "master"),
            "channel": o.get("channel", "stable"),
            "asset_name": o.get("asset_name"),
            "persistent_files": [
                pf.strip().strip("/") for pf in
                o.get("persistent_files", "").splitlines() if pf.strip()],
            "install": o.get("flsun_install", "install.sh"),
            "uninstall": o.get("flsun_uninstall", "uninstall.sh"),
            "owner": o.get("flsun_owner", USER),
            "hook": hook,
            # What Moonraker gets: the section without the flsun_ options.
            "moonraker": [(k, v) for k, v in parser.items(section)
                          if not k.startswith("flsun_")],
        })
    return plugins


def load_state():
    try:
        with open(STATE) as f:
            state = json.load(f)
    except (FileNotFoundError, ValueError):
        state = {}
    state.setdefault("installed", {})
    return state


def save_state(state):
    write_if_changed(STATE, json.dumps(state, indent=2, sort_keys=True) + "\n")


def has_update_section(name):
    """True if a config file other than ours declares [update_manager name].
    Moonraker refuses to start when a section is declared twice."""
    pattern = re.compile(
        r"^\[update_manager\s+(client\s+)?%s\]" % re.escape(name), re.M)
    for path in glob.glob(os.path.join(CONFIG_DIR, "*.conf")):
        if path == GENERATED:
            continue
        try:
            with open(path, errors="replace") as f:
                if pattern.search(f.read()):
                    return True
        except OSError:
            pass
    return False


def release_info(root):
    try:
        with open(os.path.join(root, "release_info.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def is_installed(p, state):
    if p["name"] in state["installed"]:
        return True
    if p["kind"] == "web":
        # Installed by hand from the same project's release.
        info = release_info(p["abspath"]) or {}
        found = "%s/%s" % (info.get("project_owner"), info.get("project_name"))
        return found.lower() == p["repo"].lower()
    # Cloned by hand, before FLSUN_Updater.
    if not os.path.isdir(os.path.join(p["abspath"], ".git")):
        return False
    origin = user_output(["git", "remote", "get-url", "origin"], p["abspath"])
    return origin is not None and same_repo(origin, p["repo"])


def read_selection():
    """Returns ({name: bool}, {name: channel}), or (None, {}) if the file
    doesn't exist yet. Raises configparser.Error if it can't be read."""
    if not os.path.exists(SELECTION):
        return None, {}
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(SELECTION)
    choices, channels = {}, {}
    if parser.has_section("plugins"):
        for name in parser.options("plugins"):
            try:
                choices[name] = parser.getboolean("plugins", name)
            except ValueError:
                log("%s: '%s' is not yes or no; leaving it as it is"
                    % (os.path.basename(SELECTION), name))
    for section in parser.sections():
        value = parser.get(section, "channel", fallback="").strip().lower()
        if value in ("stable", "beta"):
            channels[section] = value
        elif value:
            log("%s: [%s] channel must be stable or beta"
                % (os.path.basename(SELECTION), section))
    return choices, channels


def write_selection(plugins, choices, channels):
    lines = [
        "# FLSUN_Updater: choose which plugins are installed on this printer.",
        "# Set a plugin to yes to install it or no to remove it, then save.",
        "# The printer makes the change right away, or after the current print",
        "# finishes. Progress is logged in logs/flsun_updater.log.",
        "#",
        "# FLSUN_Updater adds new plugins here as they are published. It keeps",
        "# your choices but not comments you add.",
        "",
        "[plugins]",
    ]
    for p in plugins:
        lines += [
            "# %s: %s" % (p["title"], p.get("description", "")),
            "# " + plugin_url(p),
            "%s: %s" % (p["name"], "yes" if choices[p["name"]] else "no"),
            "",
        ]
    for p in plugins:
        if p["kind"] == "web":
            lines += [
                "[%s]" % p["name"],
                "# Release channel for %s: stable or beta." % p["title"],
                "channel: %s" % channels[p["name"]],
                "",
            ]
    write_if_changed(SELECTION, "\n".join(lines))


@contextlib.contextmanager
def plugin_sudo():
    """Plugin installers run as pi and call sudo, and FLSun's image asks pi
    for a password. Allow it without one only while the installers run."""
    tmp = SUDOERS + ".tmp"
    with open(tmp, "w") as f:
        f.write("# FLSUN_Updater: removed when the plugin installers finish\n"
                "%s ALL=(ALL) NOPASSWD: ALL\n" % USER)
    os.chmod(tmp, 0o440)
    if subprocess.call(["visudo", "-cqf", tmp]) != 0:
        os.remove(tmp)
        raise RuntimeError("visudo rejected the temporary sudo rule")
    os.replace(tmp, SUDOERS)
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(SUDOERS)


def same_repo(a, b):
    def norm(url):
        return re.sub(r"(\.git)?/*$", "", url.strip()).lower()
    return norm(a) == norm(b)


# ---------------------------------------------------- plugins from our repos

def run_plugin_script(p, key):
    """Runs the repo's install or uninstall script, if it has one. Plugins
    that only need to be cloned have none, as in Zmod."""
    script = p.get(key)
    if not script or script == "none":
        return True
    path = os.path.join(p["abspath"], script)
    if not os.path.isfile(path):
        return True
    log("  running %s" % script)
    cmd = [path] if os.access(path, os.X_OK) else ["bash", path]
    with plugin_sudo():
        return run_logged(cmd, cwd=p["abspath"], as_user=True)


def install_git_plugin(p, state):
    path = p["abspath"]
    if not os.path.exists(path):
        if not run_logged(["git", "clone", "--branch", p["branch"], "--",
                           p["repo"], path], cwd=HOME, as_user=True):
            log("  could not download %s" % p["repo"])
            return False
    else:
        origin = user_output(["git", "remote", "get-url", "origin"], path)
        if origin is None or not same_repo(origin, p["repo"]):
            log("  %s exists but is not a copy of %s; move it out of the way"
                % (path, p["repo"]))
            return False
    return run_plugin_script(p, "install")


def remove_git_plugin(p):
    if os.path.isdir(p["abspath"]):
        if not run_plugin_script(p, "uninstall"):
            return False
        shutil.rmtree(p["abspath"])
    return True


# ------------------------------------------- plugins from others' releases

def load_hook(p):
    if not p.get("hook"):
        return None
    spec = importlib.util.spec_from_file_location(
        "flsun_hook_" + p["hook"], os.path.join(HOOKS_DIR, p["hook"] + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def call_hook(p, func, *args):
    module = load_hook(p)
    if module is not None and hasattr(module, func):
        getattr(module, func)(p, *(args + (log,)))


def find_release(p, channel):
    """Returns (tag, download url) of the newest release on the channel
    that has the plugin's asset, or None."""
    req = urllib.request.Request(
        "https://api.github.com/repos/%s/releases?per_page=30" % p["repo"],
        headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        releases = json.loads(resp.read().decode())
    for rel in releases:
        if rel.get("draft") or (rel.get("prerelease") and channel == "stable"):
            continue
        for asset in rel.get("assets", []):
            if asset.get("name") == p["asset_name"]:
                return rel["tag_name"], asset["browser_download_url"]
    return None


def ensure_staging():
    os.makedirs(STAGING, exist_ok=True)
    give_to_user(STAGING)


def copy_item(src, dst):
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)


def remove_item(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.remove(path)


def extract_zip(zip_path, dest):
    """Like zipfile's extractall, but keeps the files' permissions."""
    real_dest = os.path.realpath(dest)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            target = os.path.realpath(os.path.join(dest, info.filename))
            if not target.startswith(real_dest + os.sep):
                raise RuntimeError("unsafe path in the release: "
                                   + info.filename)
            zf.extract(info, dest)
            mode = (info.external_attr >> 16) & 0o777
            if mode and not info.is_dir():
                os.chmod(target, mode)


def apply_release(p, zip_path):
    """Installs a release zip into the plugin's folder, keeping its
    persistent files and the previous version in <path>.old."""
    root = p["abspath"]
    new, old = root + ".new", root + ".old"
    remove_item(new)
    os.makedirs(new)
    try:
        extract_zip(zip_path, new)
        info = release_info(new)
        if info is None:
            raise RuntimeError("the release has no release_info.json")
        log("Installing %s %s" % (p["title"], info.get("version", "?")))
        if os.path.isdir(root):
            for pf in p["persistent_files"]:
                src = os.path.join(root, pf)
                if os.path.lexists(src):
                    dst = os.path.join(new, pf)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    remove_item(dst)
                    copy_item(src, dst)
            call_hook(p, "post_extract", root, new)
        if p["owner"] == "root":
            run(["chown", "-R", "root:root", new])
            run(["chmod", "-R", "go-w", new])
        else:
            run(["chown", "-R", "%s:%s" % (USER, USER), new])
    except Exception:
        remove_item(new)
        raise
    remove_item(old)
    if os.path.exists(root):
        os.rename(root, old)
    os.rename(new, root)
    call_hook(p, "after_install")


def install_web_plugin(p, state, channel):
    if is_installed(p, state):
        # Adopt an install made by hand from the same project's release.
        log("  found %s %s in %s" % (
            p["title"], (release_info(p["abspath"]) or {}).get("version", "?"),
            p["abspath"]))
        call_hook(p, "at_boot")
        return True
    if os.path.exists(p["abspath"]):
        log("  %s exists but is not %s; move it out of the way"
            % (p["abspath"], p["title"]))
        return False
    try:
        found = find_release(p, channel)
    except (OSError, ValueError) as e:
        log("  could not check %s's releases: %s" % (p["repo"], e))
        return False
    if found is None:
        log("  %s has no %s release with %s yet"
            % (p["repo"], channel, p["asset_name"]))
        return False
    tag, url = found
    ensure_staging()
    zip_path = os.path.join(STAGING, p["name"] + ".zip")
    log("  downloading %s %s" % (p["title"], tag))
    try:
        run(["curl", "-fsSL", "--retry", "3", "-o", zip_path, url])
        apply_release(p, zip_path)
    except RuntimeError as e:
        log("  %s" % e)
        return False
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(zip_path)
    return True


def remove_web_plugin(p):
    call_hook(p, "before_remove")
    for path in (p["abspath"], p["abspath"] + ".new", p["abspath"] + ".old"):
        remove_item(path)
    return True


# ---------------------------------------------------------------- the sync

def install_plugin(p, state, channel):
    log("Installing %s" % p["title"])
    if p["kind"] == "web":
        ok = install_web_plugin(p, state, channel)
    else:
        ok = install_git_plugin(p, state)
    if not ok:
        log("  %s was not installed; it will be tried again at the next "
            "run" % p["title"])
        return
    state["installed"][p["name"]] = {"installed_at": int(time.time())}
    save_state(state)
    log("  %s installed" % p["title"])


def remove_plugin(p, state):
    log("Removing %s" % p["title"])
    ok = remove_web_plugin(p) if p["kind"] == "web" else remove_git_plugin(p)
    if not ok:
        log("  %s's uninstaller failed; leaving it in place" % p["title"])
        return
    state["installed"].pop(p["name"], None)
    save_state(state)
    log("  %s removed" % p["title"])


def render_section(name, options):
    lines = ["[update_manager %s]" % name]
    for key, value in options:
        if "\n" in value.strip():
            lines.append("%s:" % key)
            lines += ["    " + v.strip() for v in value.strip().splitlines()]
        else:
            lines.append("%s: %s" % (key, value.strip()))
    return "\n".join(lines) + "\n"


def write_update_entries(plugins, state, channels):
    """Copies the entries of installed plugins into
    flsun_updater_plugins.conf. Returns True if Moonraker needs a restart to
    see the change."""
    blocks, services, web = [], [], []
    for p in plugins:
        if not is_installed(p, state):
            continue
        if has_update_section(p["name"]):
            # e.g. our FLSUN_ plugins' installers add their own entry.
            continue
        options = list(p["moonraker"])
        if p["kind"] == "web":
            options = [(k, channels[p["name"]] if k == "channel" else v)
                       for k, v in options]
            if not any(k == "channel" for k, _ in options):
                options.insert(1, ("channel", channels[p["name"]]))
            web.append(p["name"])
        else:
            opts = dict(options)
            svcs = opts.get("managed_services", "")
            is_service = opts.get("is_system_service", "true").lower()
            if p["name"] in svcs.split() or (
                    not svcs and is_service in ("true", "yes", "1")):
                services.append(p["name"])
        blocks.append(render_section(p["name"], options))
    # The component behind the plugin macros, which also makes type: web
    # entries work on FLSun's Moonraker.
    blocks.append("[flsun_plugins]\n%s" % (
        "web_clients: %s\n" % " ".join(web) if web else ""))
    entries = "\n".join(blocks)
    try:
        with open(GENERATED) as f:
            old_entries = re.sub(r"^#.*\n|^\n", "", f.read(), flags=re.M)
    except FileNotFoundError:
        old_entries = None
    write_if_changed(GENERATED,
                     "# Written by FLSUN_Updater on every run; don't edit. "
                     "Update\n# manager entries for the plugins it installs."
                     "\n\n" + entries)
    changed = old_entries != re.sub(r"^\n", "", entries, flags=re.M)
    if services and os.path.exists(ASVC):
        with open(ASVC) as f:
            current = f.read()
        missing = [s for s in services if s not in current.split()]
        if missing:
            with open(ASVC, "a") as f:
                if current and not current.endswith("\n"):
                    f.write("\n")
                f.write("".join(s + "\n" for s in missing))
            changed = True
    return changed


def ensure_include():
    """Returns True if the include had to be added."""
    include = "[include %s]" % os.path.basename(GENERATED)
    with open(MOONRAKER_CONF) as f:
        text = f.read()
    if any(line.strip() == include for line in text.splitlines()):
        return False
    shutil.copy2(MOONRAKER_CONF, MOONRAKER_CONF + ".flsun_updater.bak")
    with open(MOONRAKER_CONF, "a") as f:
        f.write("%s\n%s\n" % ("" if text.endswith("\n") else "\n", include))
    return True


def ensure_macros():
    """Links the plugin macros into Klipper's config and includes them from
    printer.cfg. Returns True if Klipper needs a restart to load them."""
    printer_cfg = os.path.join(CONFIG_DIR, "printer.cfg")
    changed = False
    if os.path.realpath(MACROS_LINK) != MACROS_SRC:
        tmp = MACROS_LINK + ".tmp"
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp)
        os.symlink(MACROS_SRC, tmp)
        os.lchown(tmp, USER_PW.pw_uid, USER_PW.pw_gid)
        os.replace(tmp, MACROS_LINK)
        changed = True
    include = "[include %s]" % os.path.basename(MACROS_LINK)
    with open(printer_cfg) as f:
        text = f.read()
    if not any(line.strip() == include for line in text.splitlines()):
        shutil.copy2(printer_cfg, printer_cfg + ".flsun_updater.bak")
        # Above SAVE_CONFIG's block, which must stay last.
        marker = "#*# <---------------------- SAVE_CONFIG ---------------------->"
        if marker in text:
            head, tail = text.split(marker, 1)
            text = "%s%s\n\n%s%s" % (head.rstrip("\n") + "\n\n", include,
                                     marker, tail)
        else:
            text = "%s%s\n" % (text if text.endswith("\n") else text + "\n",
                               include)
        with open(printer_cfg, "w") as f:
            f.write(text)
        give_to_user(printer_cfg)
        log("Added the plugin macros to printer.cfg")
        changed = True
    return changed


def sync_plugins():
    plugins = load_registry()
    state = load_state()
    try:
        selection, chosen_channels = read_selection()
    except configparser.Error as e:
        log("Can't read %s, so no plugins were changed: %s"
            % (os.path.basename(SELECTION), e))
        return
    installed = {p["name"]: is_installed(p, state) for p in plugins}
    choices, channels = {}, {}
    for p in plugins:
        wanted = None if selection is None else selection.get(p["name"])
        choices[p["name"]] = installed[p["name"]] if wanted is None else wanted
        if p["kind"] == "web":
            channels[p["name"]] = chosen_channels.get(p["name"], p["channel"])
    for name in sorted(set(selection or {}) - set(choices)):
        log("%s: '%s' is not in the plugin list; ignored"
            % (os.path.basename(SELECTION), name))
    apply_recovery(plugins, choices)
    write_selection(plugins, choices, channels)

    todo = [p for p in plugins if choices[p["name"]] != installed[p["name"]]]
    if todo:
        wait_moonraker(300)
        wait_until_idle()
        for p in todo:
            if choices[p["name"]]:
                install_plugin(p, state, channels.get(p["name"]))
            else:
                remove_plugin(p, state)
    else:
        log("Plugins match flsun_plugins.cfg")
    # A FLSun firmware update can undo a hook's changes, e.g. the screen's
    # boot script.
    for p in plugins:
        if (p["kind"] == "web" and p not in todo
                and is_installed(p, state)):
            call_hook(p, "at_boot")

    restart = write_update_entries(plugins, state, channels)
    restart = ensure_component() or restart
    restart = ensure_include() or restart
    if restart:
        restart_service("moonraker")
    if ensure_macros():
        if print_state() in ("printing", "paused"):
            log("The plugin macros load at Klipper's next restart")
        else:
            restart_service("klipper")


# ------------------------------------------------------- USB stick recovery

_recovery = []  # flsun_recovery.cfg files found by this run
_recovery_devs = {}  # each file's USB device
_recovery_applied = set()  # the ones sync_plugins read


def usb_mounts():
    """(device, mount point, read-only) for each mounted USB stick."""
    try:
        with open("/proc/mounts") as f:
            lines = [line.split() for line in f]
    except OSError:
        return []
    return [(dev, point.replace("\\040", " "), "ro" in opts.split(","))
            for dev, point, _fs, opts, *_ in lines
            if re.match(r"/dev/sd[a-z]", dev)]


def find_recovery_files():
    """flsun_recovery.cfg at the top of each mounted USB stick. The name is
    matched without case, since FAT sticks written elsewhere may change it."""
    found = []
    for dev, point, _ro in usb_mounts():
        try:
            names = os.listdir(point)
        except OSError:
            continue
        for n in names:
            if n.lower() == RECOVERY_FILE:
                found.append(os.path.join(point, n))
                _recovery_devs[found[-1]] = dev
    return found


def apply_recovery(plugins, choices):
    """Turns off the plugins that a USB stick's flsun_recovery.cfg sets to
    no. It only turns plugins off: installing needs a network, which the
    printer may not have."""
    known = {p["name"] for p in plugins}
    for path in _recovery:
        log("Recovery file found: " + path)
        parser = configparser.ConfigParser(interpolation=None)
        try:
            with open(path, errors="replace") as f:
                text = f.read()
            # The [plugins] line is easy to leave out when writing it by hand.
            if not re.search(r"^\s*\[", text, re.M):
                text = "[plugins]\n" + text
            parser.read_string(text, path)
        except (OSError, configparser.Error) as e:
            # Left in place, so the stick's log shows this again until
            # the file is fixed.
            log("  Can't read it, so nothing was changed: %s" % e)
            continue
        _recovery_applied.add(path)
        if not parser.has_section("plugins"):
            log("  It has no [plugins] section, so nothing was changed")
            continue
        for name in parser.options("plugins"):
            try:
                enabled = parser.getboolean("plugins", name)
            except ValueError:
                log("  '%s' is not yes or no; ignored" % name)
                continue
            if enabled:
                log("  '%s: yes' ignored; a recovery file only turns "
                    "plugins off" % name)
                continue
            names = sorted(known) if name == "all" else [name]
            if name != "all" and name not in known:
                log("  '%s' is not in the plugin list; ignored" % name)
                continue
            for n in names:
                if choices[n]:
                    log("  Turning off %s" % n)
                    choices[n] = False


def _mount(args):
    res = subprocess.run(["mount"] + args, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, universal_newlines=True)
    if res.returncode != 0:
        raise OSError(res.stdout.strip() or "mount failed")


@contextlib.contextmanager
def writable_stick(dev):
    """Yields a read-write mount point for the stick. FLSun's /etc/um mounts
    sticks read-write, but HelixScreen mounts them read-only and can get
    there first, and removing HelixScreen can unmount the stick."""
    mount = next(((p, ro) for d, p, ro in usb_mounts() if d == dev), None)
    if mount and not mount[1]:
        yield mount[0]
    elif mount:
        _mount(["-o", "remount,rw", mount[0]])
        try:
            yield mount[0]
        finally:
            os.sync()
            with contextlib.suppress(OSError):
                _mount(["-o", "remount,ro", mount[0]])
    else:
        point = tempfile.mkdtemp(prefix="flsun_recovery.", dir="/run")
        try:
            _mount([dev, point])
            try:
                yield point
            finally:
                os.sync()
                subprocess.call(["umount", point])
        finally:
            with contextlib.suppress(OSError):
                os.rmdir(point)


def finish_recovery():
    """Renames each recovery file that was applied, so that it isn't applied
    again at every boot while the stick stays in, and writes this run's log
    beside it. A file the run never got to stays for the next run."""
    for path in _recovery:
        name = os.path.basename(path)
        try:
            with writable_stick(_recovery_devs[path]) as point:
                if path in _recovery_applied:
                    os.replace(os.path.join(point, name),
                               os.path.join(point, RECOVERY_DONE))
                with open(os.path.join(point, RECOVERY_LOG), "w") as f:
                    f.write("\n".join(_collect or []) + "\n")
        except OSError as e:
            log("Couldn't update the USB stick %s: %s; remove the stick, or "
                "the file is applied again at the next boot"
                % (_recovery_devs[path], e))
    os.sync()


# ----------------------------------------------------------------- commands

def cmd_run(_args):
    global _collect, _recovery
    require_root("run")
    _lock = take_lock(LOCK)
    open_log()
    _recovery = find_recovery_files()
    if _recovery:
        _collect = []  # written to the USB stick at the end
    log("FLSUN_Updater run started")
    # Left behind if a run was killed part way.
    with contextlib.suppress(FileNotFoundError):
        os.remove(SUDOERS)
    for step in (fix_hosts, fix_certificates, check_git, ensure_units,
                 fix_web_clients, sync_plugins, fix_refresh_times):
        try:
            step()
        except Exception:
            log("%s failed:\n%s" % (step.__name__, traceback.format_exc()))
    log("FLSUN_Updater run finished")
    if _recovery:
        finish_recovery()


def cmd_web_apply(_args):
    """Installs releases that the Moonraker component downloaded. Each
    request names a plugin; its path and options come from
    extra_plugins.moonraker.conf, never from the request."""
    global _collect
    require_root("web-apply")
    _lock = take_lock(WEB_LOCK)
    open_log()
    plugins = {p["name"]: p for p in load_registry() if p["kind"] == "web"}
    staging = os.path.realpath(STAGING)
    for request in sorted(glob.glob(os.path.join(STAGING, "*.request.json"))):
        _collect = []
        outcome = {"ok": False}
        try:
            with open(request) as f:
                data = json.load(f)
            os.remove(request)
            p = plugins.get(data.get("name"))
            zip_path = os.path.realpath(data.get("zip", ""))
            if p is None:
                raise RuntimeError("%r is not in extra_plugins.moonraker.conf"
                                   % data.get("name"))
            if os.path.dirname(zip_path) != staging:
                raise RuntimeError("the download is not in " + STAGING)
            log("Update requested: %s %s" % (p["title"], data.get("version")))
            apply_release(p, zip_path)
            outcome["ok"] = True
        except Exception as e:
            log("Update failed: %s" % e)
            outcome["error"] = str(e)
        outcome["log"] = _collect
        _collect = None
        name = re.sub(r"\.request\.json$", "", os.path.basename(request))
        result = os.path.join(STAGING, name + ".result.json")
        with open(result + ".tmp", "w") as f:
            json.dump(outcome, f)
        give_to_user(result + ".tmp")
        os.replace(result + ".tmp", result)


def cmd_list(_args):
    plugins = load_registry()
    state = load_state()
    try:
        selection, channels = read_selection()
    except configparser.Error:
        selection, channels = {}, {}
    selection = selection or {}
    print("%-22s %-10s %-7s %-7s %s"
          % ("PLUGIN", "INSTALLED", "CHOSEN", "CHANNEL", "SOURCE"))
    for p in plugins:
        chosen = selection.get(p["name"])
        print("%-22s %-10s %-7s %-7s %s" % (
            p["name"], "yes" if is_installed(p, state) else "no",
            "-" if chosen is None else ("yes" if chosen else "no"),
            channels.get(p["name"], p.get("channel", "-")),
            plugin_url(p)))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="repair, then install or remove plugins")
    sub.add_parser("web-apply", help="install downloaded remote releases")
    sub.add_parser("list", help="show the plugins")
    args = parser.parse_args()
    commands = {"run": cmd_run, "web-apply": cmd_web_apply, "list": cmd_list}
    if args.command not in commands:
        parser.print_help()
        sys.exit(2)
    commands[args.command](args)


if __name__ == "__main__":
    main()
