#!/bin/sh
# Installed by FLSUN_Updater (hooks/helixscreen.py) as
# /home/pi/qt/out_linux/helixscreen_wifi.sh. Runs as root.
#
# FLSun's stock screen manages Wi-Fi through its own wpa_supplicant, which
# /etc/sw starts from /etc/rc.local after killing every other one.
# HelixScreen manages Wi-Fi through NetworkManager, which needs the system
# wpa_supplicant (with D-Bus) and otherwise reports wlan0 as unavailable.
#
#   helixscreen_wifi.sh nm      switch wlan0 to NetworkManager, for HelixScreen
#   helixscreen_wifi.sh flsun   switch wlan0 back to FLSun's /etc/sw

case "$1" in
nm)
    # At boot, /etc/sw runs after the screen starts: wait for rc.local to
    # finish so it doesn't undo the switch.
    i=0
    while [ "$(systemctl is-active rc-local 2>/dev/null)" = activating ] \
        && [ $i -lt 300 ]; do
        sleep 2
        i=$((i + 2))
    done
    # /etc/sw's dhclient, and the bare one FLSun's stock screen starts.
    pkill -f '^dhclient wlan0'
    pkill -f '^/sbin/dhclient$'
    pkill -f '^wpa_supplicant -i wlan0'
    if ! systemctl is-active --quiet wpa_supplicant; then
        systemctl restart wpa_supplicant
    fi
    ;;
flsun)
    systemctl stop wpa_supplicant
    # /etc/sw waits for a DHCP lease, so don't wait for it.
    if [ -x /etc/sw ]; then
        setsid /etc/sw >/dev/null 2>&1 </dev/null &
    fi
    ;;
*)
    echo "usage: $0 nm|flsun" >&2
    exit 2
    ;;
esac
