"""FLSUN_Updater hook for Fluidd on the FLSun T1 Pro.

Mainsail has port 80, so Fluidd gets its own nginx site on port 81, written
from fluidd_nginx.conf. Moonraker serves both, and both read the same
cameras from Moonraker.
"""

import os
import shutil
import subprocess

HOOKS_DIR = os.path.dirname(os.path.realpath(__file__))
TEMPLATE = os.path.join(HOOKS_DIR, "fluidd_nginx.conf")
PORT = 81
SITE = "/etc/nginx/sites-available/fluidd"
ENABLED = "/etc/nginx/sites-enabled/fluidd"
MARKER = "Written by FLSUN_Updater's fluidd hook"


def _render(spec):
    with open(TEMPLATE) as f:
        text = f.read()
    return text.replace("@PORT@", str(PORT)).replace("@ROOT@", spec["abspath"])


def _nginx(log, *args):
    res = subprocess.run(["nginx"] + list(args), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, universal_newlines=True)
    if res.returncode != 0:
        for line in res.stdout.strip().splitlines():
            log("  | " + line)
    return res.returncode == 0


def _reload(log):
    if subprocess.call(["systemctl", "reload", "nginx"]) != 0:
        log("WARNING: nginx didn't reload; Fluidd's site applies at the next "
            "boot")


def _ensure_site(spec, log):
    """Writes and enables the site; reloads nginx if anything changed."""
    if not os.path.isdir(os.path.dirname(SITE)):
        log("WARNING: nginx's sites folder not found; Fluidd isn't served")
        return
    text = _render(spec)
    try:
        with open(SITE) as f:
            current = f.read()
    except FileNotFoundError:
        current = None
    changed = False
    if current != text:
        if current is not None and MARKER not in current:
            log("Keeping the existing Fluidd site as %s.bak" % SITE)
            shutil.copy2(SITE, SITE + ".bak")
        with open(SITE, "w") as f:
            f.write(text)
        changed = True
    if os.path.realpath(ENABLED) != SITE:
        if os.path.lexists(ENABLED):
            os.remove(ENABLED)
        os.symlink(SITE, ENABLED)
        changed = True
    if not changed:
        return
    if not _nginx(log, "-t"):
        # A broken site would take Mainsail down with it at the next reload.
        os.remove(ENABLED)
        log("WARNING: nginx rejected Fluidd's site, so it's turned off")
        return
    log("Serving Fluidd on port %d" % PORT)
    _reload(log)


# ------------------------------------------------------------ entry points

def after_install(spec, log):
    """After a new install, an update or a rollback."""
    _ensure_site(spec, log)


def at_boot(spec, log):
    """On every FLSUN_Updater run while installed."""
    _ensure_site(spec, log)


def before_remove(spec, log):
    removed = False
    if os.path.lexists(ENABLED) and os.path.realpath(ENABLED) == SITE:
        os.remove(ENABLED)
        removed = True
    try:
        with open(SITE) as f:
            ours = MARKER in f.read()
    except FileNotFoundError:
        ours = False
    if ours:
        os.remove(SITE)
    if removed:
        log("Fluidd's site on port %d removed" % PORT)
        _reload(log)
