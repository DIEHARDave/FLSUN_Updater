#!/bin/sh
# Installed by FLSUN_Updater (hooks/helixscreen.py) as
# /home/pi/qt/out_linux/startboot.sh.
# HelixScreen replaces FLSun's stock screen; /etc/rc.local runs this script
# when /home/pi/qt/qt_start contains 1. FLSun's own script is kept as
# startboot.sh.flsun. To go back to the stock screen, set
# helixscreen to no in flsun_plugins.cfg (or DISABLE_PLUGIN NAME=helixscreen).

HELIX_DIR=/home/pi/helixscreen

if [ -x "$HELIX_DIR/bin/helix-launcher.sh" ]; then
    # rc.local sources /etc/profile first, which carries the stock screen's
    # tslib LD_PRELOAD and Qt library paths. Keep them out of HelixScreen.
    unset LD_PRELOAD LD_LIBRARY_PATH QT_QPA_PLATFORM QT_QPA_GENERIC_PLUGINS QT_QPA_FB_TSLIB
    cd "$HELIX_DIR" || exit 1
    exec "$HELIX_DIR/bin/helix-launcher.sh" >/tmp/helixscreen-launcher.log 2>&1 </dev/null
fi

# HelixScreen missing: start the stock screen instead of leaving it blank.
exec /home/pi/qt/out_linux/startboot.sh.flsun
