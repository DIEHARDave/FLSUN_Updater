"""FLSUN_Updater hook for HelixScreen on the FLSun T1 Pro.

HelixScreen replaces FLSun's stock screen app, which /etc/rc.local starts
through /home/pi/qt/out_linux/startboot.sh. The hook swaps that script,
keeps the user's config/ across updates the way HelixScreen's own installer
does, and restarts the screen. It also hands wlan0 to NetworkManager, which
HelixScreen's Wi-Fi settings use, and back to FLSun's /etc/sw on removal.
"""

import os
import re
import shutil
import signal
import subprocess
import time

HOOKS_DIR = os.path.dirname(os.path.realpath(__file__))
BOOT_SCRIPT = os.path.join(HOOKS_DIR, "helixscreen_startboot.sh")
WIFI_SCRIPT_SRC = os.path.join(HOOKS_DIR, "helixscreen_wifi.sh")
QT_DIR = "/home/pi/qt/out_linux"
STARTBOOT = os.path.join(QT_DIR, "startboot.sh")
WIFI_SCRIPT = os.path.join(QT_DIR, "helixscreen_wifi.sh")
STARTBOOT_FLSUN = STARTBOOT + ".flsun"
STATE_DIR = "/home/pi/helixscreen-state"
CONFIG_BACKUP = "/home/pi/helixscreen-config.bak"
SCREEN_UNIT = "flsun-screen"

# From HelixScreen's installer (scripts/lib/installer/release.sh): anything
# else in the old config/ that the new release doesn't ship is carried over.
NEVER_KEEP = {
    "crash.txt", "crash_report.txt", ".crash_restart_count",
    ".helix-fresh-install", "printer_database.json", "printing_tips.json",
    "default_layout.json", "helix_macros.cfg", "presets",
    "print_start_profiles", "sounds", "platform",
}


def _copy(src, dst):
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)


def post_extract(spec, old_root, new_root, log):
    """Called with the new release unpacked beside the old install, after
    persistent_files were copied over."""
    old_cfg = os.path.join(old_root, "config")
    new_cfg = os.path.join(new_root, "config")
    if not os.path.isdir(old_cfg) or not os.path.isdir(new_cfg):
        return
    for name in os.listdir(old_cfg):
        if name in NEVER_KEEP or re.match(r"crash_.*\.txt$", name):
            continue
        src, dst = os.path.join(old_cfg, name), os.path.join(new_cfg, name)
        if not os.path.lexists(dst):
            _copy(src, dst)
        elif os.path.isdir(src) and os.path.isdir(dst):
            for sub in os.listdir(src):
                if name == "themes" and sub == "defaults":
                    continue
                if not os.path.lexists(os.path.join(dst, sub)):
                    _copy(os.path.join(src, sub), os.path.join(dst, sub))


# ------------------------------------------------------------------ screen

def _screen_pids(install_dir):
    helix_bin = install_dir + "/bin/"
    pids = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                args = [a.decode(errors="replace")
                        for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        if not args:
            continue
        prog = args[1] if (os.path.basename(args[0]) == "sh"
                           and len(args) > 1) else args[0]
        if prog.startswith(helix_bin) or prog == os.path.join(QT_DIR, "TUI"):
            pids.append(int(pid))
    return pids


def _stop_screen(install_dir):
    for cmd in ("stop", "reset-failed"):
        subprocess.call(["systemctl", cmd, SCREEN_UNIT + ".service"],
                        stderr=subprocess.DEVNULL)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = _screen_pids(install_dir)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        time.sleep(3)


def _start_screen(log):
    # Starts the screen as rc.local does at boot, after sourcing /etc/profile:
    # the stock TUI needs its Qt library paths. RemainAfterExit keeps the
    # stock TUI, which its start script backgrounds, from being killed.
    log("Starting the screen")
    subprocess.call([
        "systemd-run", "--unit=" + SCREEN_UNIT, "-p", "RemainAfterExit=yes",
        "--description=Touchscreen (restarted by FLSUN_Updater)",
        "/bin/sh", "-c", '. /etc/profile; exec "$0"', STARTBOOT],
        stdout=subprocess.DEVNULL)


def _restart_screen(spec, log):
    _stop_screen(spec["abspath"])
    _start_screen(log)


def _is_helix_script(path):
    try:
        with open(path) as f:
            return "helix-launcher" in f.read()
    except OSError:
        return False


def _write_script(src, dst):
    """Returns True if dst changed."""
    with open(src) as f:
        script = f.read()
    try:
        with open(dst) as f:
            if f.read() == script:
                return False
    except FileNotFoundError:
        pass
    tmp = dst + ".tmp"
    with open(tmp, "w") as f:
        f.write(script)
    os.chmod(tmp, 0o755)
    os.replace(tmp, dst)
    return True


def _install_boot_script(log):
    """Returns True if the boot scripts changed. FLSun firmware updates can
    put the stock script back."""
    if not os.path.isdir(QT_DIR):
        log("WARNING: %s not found; HelixScreen won't start at boot" % QT_DIR)
        return False
    # The boot script runs the Wi-Fi script, so install that first.
    changed = _write_script(WIFI_SCRIPT_SRC, WIFI_SCRIPT)
    if os.path.exists(STARTBOOT) and not _is_helix_script(STARTBOOT):
        log("Keeping FLSun's screen start script as " + STARTBOOT_FLSUN)
        shutil.copy2(STARTBOOT, STARTBOOT_FLSUN)
    if _write_script(BOOT_SCRIPT, STARTBOOT):
        log("Setting the screen to start HelixScreen at boot")
        changed = True
    return changed


# ------------------------------------------------------------ entry points

def after_install(spec, log):
    """After a new install, an update or a rollback."""
    _install_boot_script(log)
    _restart_screen(spec, log)


def at_boot(spec, log):
    """On every FLSUN_Updater run while installed."""
    if _install_boot_script(log):
        _restart_screen(spec, log)


def before_remove(spec, log):
    if os.path.exists(STARTBOOT_FLSUN):
        log("Setting the screen to start FLSun's stock screen at boot")
        shutil.copy2(STARTBOOT_FLSUN, STARTBOOT)
    else:
        log("WARNING: %s is missing; the screen will stay blank"
            % STARTBOOT_FLSUN)
    _stop_screen(spec["abspath"])
    cfg = os.path.join(spec["abspath"], "config")
    if os.path.isdir(cfg):
        shutil.rmtree(CONFIG_BACKUP, ignore_errors=True)
        shutil.copytree(cfg, CONFIG_BACKUP, symlinks=True)
        log("HelixScreen's settings are kept in " + CONFIG_BACKUP)
    shutil.rmtree(STATE_DIR, ignore_errors=True)
    if os.path.exists(WIFI_SCRIPT):
        log("Handing Wi-Fi back to FLSun's screen")
        subprocess.call([WIFI_SCRIPT, "flsun"])
        os.remove(WIFI_SCRIPT)
    if not _is_helix_script(STARTBOOT):
        _start_screen(log)
