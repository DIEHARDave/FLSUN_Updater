#!/usr/bin/env python3
"""FLSUN_Updater for the FLSun T1 Pro.

Installs and removes the plugins listed in plugins.json to match the choices
in ~/printer_data/config/flsun_plugins.cfg, and repairs the parts of FLSun's
system image that stop Moonraker's update manager from working.

    flsun_updater.py run    Repair, then install or remove plugins. Needs
                            root. The flsun_updater service runs this at
                            boot, whenever flsun_plugins.cfg is saved, and
                            after Moonraker updates FLSUN_Updater.
    flsun_updater.py list   Show the plugins and which are installed.

Standard library only: the T1 Pro has Python 3.7 and no pip.
"""

import argparse
import configparser
import contextlib
import fcntl
import glob
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

REPO_DIR = os.path.dirname(os.path.realpath(__file__))
REGISTRY = os.path.join(REPO_DIR, "plugins.json")
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
LOG = os.path.join(DATA_DIR, "logs", "flsun_updater.log")
# Web clients that Moonraker updates by deleting and re-extracting the folder.
WEB_CLIENTS = [os.path.join(HOME, "mainsail"), os.path.join(HOME, "fluidd")]
# sudo ignores files in sudoers.d whose names contain a dot, so the temporary
# name is inert until the rename.
SUDOERS = "/etc/sudoers.d/zz-flsun-updater"
LOCK = "/run/flsun_updater.lock"
MOONRAKER = "http://127.0.0.1:7125"
# FLSun's image boots with a stale clock; a time before this isn't real.
CLOCK_VALID_AFTER = 1790000000  # 2026-09-21
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
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


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    if _log_file is not None:
        _log_file.write(line + "\n")
        _log_file.flush()


def open_log():
    global _log_file
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    if os.path.exists(LOG) and os.path.getsize(LOG) > 512 * 1024:
        os.replace(LOG, LOG + ".1")
    _log_file = open(LOG, "a")
    os.chown(LOG, USER_PW.pw_uid, USER_PW.pw_gid)


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
    res = subprocess.run(
        ["runuser", "-u", USER, "--"] + cmd, cwd=cwd, env=USER_ENV,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, universal_newlines=True)
    return res.stdout.strip() if res.returncode == 0 else None


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
    """Plugin installers restart Klipper, which would kill a print."""
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


# ------------------------------------------------------------------ plugins

def expand(path):
    if path == "~" or path.startswith("~/"):
        return HOME + path[1:]
    return path


def load_registry():
    with open(REGISTRY) as f:
        entries = json.load(f)["plugins"]
    plugins, seen = [], set()
    for entry in entries:
        name = entry.get("name", "")
        path = os.path.realpath(expand(entry.get("path", "")))
        um = entry.get("update_manager")
        if (not NAME_RE.match(name) or name in seen or name == "flsun_updater"
                or not entry.get("repo") or not path.startswith(HOME + "/")
                or not (um == "plugin" or isinstance(um, dict))
                or (um == "plugin" and not entry.get("uninstall"))):
            log("Skipping an invalid plugins.json entry: %r" % name)
            continue
        seen.add(name)
        p = dict(entry)
        p["abspath"] = path
        p.setdefault("title", name)
        p.setdefault("branch", "main")
        plugins.append(p)
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


def is_installed(p, state):
    if p["name"] in state["installed"]:
        return True
    # Installed by hand, before FLSUN_Updater: its installer added its entry.
    return p["update_manager"] == "plugin" and has_update_section(p["name"])


def read_selection():
    """Returns {name: bool}, or None if the file doesn't exist yet.
    Raises configparser.Error if the file can't be read."""
    if not os.path.exists(SELECTION):
        return None
    parser = configparser.ConfigParser()
    parser.read(SELECTION)
    choices = {}
    if parser.has_section("plugins"):
        for name in parser.options("plugins"):
            try:
                choices[name] = parser.getboolean("plugins", name)
            except ValueError:
                log("%s: '%s' is not yes or no; leaving it as it is"
                    % (os.path.basename(SELECTION), name))
    return choices


def write_selection(plugins, choices):
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
        url = re.sub(r"\.git$", "", p["repo"])
        lines += [
            "# %s: %s" % (p["title"], p.get("description", "")),
            "# " + url,
            "%s: %s" % (p["name"], "yes" if choices[p["name"]] else "no"),
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


def run_plugin_script(p, key):
    script = p.get(key)
    if not script:
        return True
    path = os.path.join(p["abspath"], script)
    if not os.path.isfile(path):
        log("  %s has no %s" % (p["title"], script))
        return False
    cmd = [path] if os.access(path, os.X_OK) else ["bash", path]
    return run_logged(cmd + list(p.get(key + "_args", [])),
                      cwd=p["abspath"], as_user=True)


def install_plugin(p, state):
    log("Installing %s" % p["title"])
    path = p["abspath"]
    if not os.path.exists(path):
        if not run_logged(["git", "clone", "--branch", p["branch"], "--",
                           p["repo"], path], cwd=HOME, as_user=True):
            log("  could not download %s" % p["repo"])
            return
    else:
        origin = user_output(["git", "remote", "get-url", "origin"], path)
        if origin is None or not same_repo(origin, p["repo"]):
            log("  %s exists but is not a copy of %s; move it out of the way"
                % (path, p["repo"]))
            return
    if not run_plugin_script(p, "install"):
        log("  %s's installer failed; it will be tried again at the next "
            "run" % p["title"])
        return
    state["installed"][p["name"]] = {"installed_at": int(time.time())}
    save_state(state)
    log("  %s installed" % p["title"])


def remove_plugin(p, state):
    log("Removing %s" % p["title"])
    if os.path.isdir(p["abspath"]) and not run_plugin_script(p, "uninstall"):
        log("  %s's uninstaller failed; leaving it in place" % p["title"])
        return
    state["installed"].pop(p["name"], None)
    save_state(state)
    if os.path.isdir(p["abspath"]) and p["abspath"] != HOME:
        shutil.rmtree(p["abspath"])
    log("  %s removed" % p["title"])


def write_update_entries(plugins, state):
    """Writes [update_manager] entries for plugins that don't add their own.
    Returns True if Moonraker needs a restart to see the change."""
    blocks, services = [], []
    for p in plugins:
        um = p["update_manager"]
        if not isinstance(um, dict) or p["name"] not in state["installed"]:
            continue
        if has_update_section(p["name"]):
            log("%s already has an update manager entry; not adding another"
                % p["title"])
            continue
        options = {
            "type": "git_repo",
            "path": p["path"],
            "origin": p["repo"],
            "primary_branch": p["branch"],
        }
        options.update(um)
        blocks.append("[update_manager %s]\n%s\n" % (p["name"], "".join(
            "%s: %s\n" % (k, v) for k, v in options.items())))
        if p["name"] in str(options.get("managed_services", "")).split():
            services.append(p["name"])
    entries = "\n".join(blocks)
    try:
        with open(GENERATED) as f:
            old_entries = re.sub(r"^#.*\n|^\n", "", f.read(), flags=re.M)
    except FileNotFoundError:
        old_entries = None
    write_if_changed(GENERATED,
                     "# Written by FLSUN_Updater on every run; don't edit. "
                     "Update\n# manager entries for plugins that don't add "
                     "their own.\n\n" + entries)
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


def sync_plugins():
    plugins = load_registry()
    state = load_state()
    try:
        selection = read_selection()
    except configparser.Error as e:
        log("Can't read %s, so no plugins were changed: %s"
            % (os.path.basename(SELECTION), e))
        return
    installed = {p["name"]: is_installed(p, state) for p in plugins}
    choices = {}
    for p in plugins:
        wanted = None if selection is None else selection.get(p["name"])
        choices[p["name"]] = installed[p["name"]] if wanted is None else wanted
    for name in sorted(set(selection or {}) - set(choices)):
        log("%s: '%s' is not in the plugin list; ignored"
            % (os.path.basename(SELECTION), name))
    write_selection(plugins, choices)

    todo = [p for p in plugins if choices[p["name"]] != installed[p["name"]]]
    if todo:
        wait_moonraker(300)
        wait_until_idle()
        with plugin_sudo():
            for p in todo:
                if choices[p["name"]]:
                    install_plugin(p, state)
                else:
                    remove_plugin(p, state)
    else:
        log("Plugins match flsun_plugins.cfg")

    restart = write_update_entries(plugins, state)
    restart = ensure_include() or restart
    if restart:
        restart_service("moonraker")


# ----------------------------------------------------------------- commands

def cmd_run(_args):
    if os.geteuid() != 0:
        sys.exit("flsun_updater.py run needs root: sudo %s run" % sys.argv[0])
    lock = open(LOCK, "w")
    fcntl.flock(lock, fcntl.LOCK_EX)  # one run at a time; others wait
    open_log()
    log("FLSUN_Updater run started")
    # Left behind if a run was killed part way.
    with contextlib.suppress(FileNotFoundError):
        os.remove(SUDOERS)
    for step in (fix_hosts, fix_certificates, check_git, fix_web_clients,
                 sync_plugins, fix_refresh_times):
        try:
            step()
        except Exception:
            log("%s failed:\n%s" % (step.__name__, traceback.format_exc()))
    log("FLSUN_Updater run finished")


def cmd_list(_args):
    plugins = load_registry()
    state = load_state()
    try:
        selection = read_selection() or {}
    except configparser.Error:
        selection = {}
    print("%-22s %-10s %-9s %s" % ("PLUGIN", "INSTALLED", "CHOSEN", "REPO"))
    for p in plugins:
        chosen = selection.get(p["name"])
        print("%-22s %-10s %-9s %s" % (
            p["name"], "yes" if is_installed(p, state) else "no",
            "-" if chosen is None else ("yes" if chosen else "no"),
            re.sub(r"\.git$", "", p["repo"])))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="repair, then install or remove plugins")
    sub.add_parser("list", help="show the plugins")
    args = parser.parse_args()
    if args.command == "run":
        cmd_run(args)
    elif args.command == "list":
        cmd_list(args)
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
