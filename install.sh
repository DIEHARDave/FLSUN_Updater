#!/bin/bash
# FLSUN_Updater installer for the FLSun T1 Pro.
# Run as the pi user from the cloned repo: ~/FLSUN_Updater/install.sh
# Safe to re-run, e.g. after a FLSun firmware update.

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${HOME}/printer_data/config"
MOONRAKER_CONF="${CONFIG_DIR}/moonraker.conf"
GENERATED="${CONFIG_DIR}/flsun_updater_plugins.conf"
ASVC="${HOME}/printer_data/moonraker.asvc"
SERVICE="flsun_updater"

msg() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run as the pi user, not root (sudo is used where needed)"
# The services point at this path.
[ "$REPO_DIR" = "${HOME}/FLSUN_Updater" ] \
    || die "clone the repo to ${HOME}/FLSUN_Updater (it is at $REPO_DIR)"
[ -x /usr/bin/python3 ] || die "/usr/bin/python3 not found"
[ -f "$MOONRAKER_CONF" ] || die "$MOONRAKER_CONF not found"

# Clock, certificates and git https support, shared with the other FLSun
# plugins; see prepare.sh.
"${REPO_DIR}/prepare.sh"

msg "Installing the boot service that sets the clock"
sudo install -m 755 "${REPO_DIR}/system/flsun-timesync" /usr/local/bin/flsun-timesync
sudo install -m 644 "${REPO_DIR}/system/flsun-timesync.service" /etc/systemd/system/flsun-timesync.service

msg "Installing the updater service"
chmod 755 "${REPO_DIR}/flsun_updater.py"
sudo install -m 644 "${REPO_DIR}/system/${SERVICE}.service" "/etc/systemd/system/${SERVICE}.service"
sudo install -m 644 "${REPO_DIR}/system/${SERVICE}.path" "/etc/systemd/system/${SERVICE}.path"
sudo systemctl daemon-reload
sudo systemctl enable flsun-timesync.service "${SERVICE}.service" "${SERVICE}.path" > /dev/null 2>&1
sudo systemctl restart "${SERVICE}.path"

msg "Adding the Moonraker update manager entries"
if [ ! -f "${CONFIG_DIR}/flsun_updater.conf" ]; then
    cp "${REPO_DIR}/config/flsun_updater.conf" "${CONFIG_DIR}/flsun_updater.conf"
fi
# Entries for listed plugins that don't add their own; rewritten on each run.
[ -f "$GENERATED" ] || : > "$GENERATED"
for conf in flsun_updater.conf flsun_updater_plugins.conf; do
    if ! grep -q "^\[include ${conf//./\\.}\]" "$MOONRAKER_CONF"; then
        [ -f "${MOONRAKER_CONF}.flsun_updater.bak" ] \
            || cp "$MOONRAKER_CONF" "${MOONRAKER_CONF}.flsun_updater.bak"
        printf '\n[include %s]\n' "$conf" >> "$MOONRAKER_CONF"
    fi
done
# Let Moonraker restart the service after an update.
if [ -f "$ASVC" ] && ! grep -qx "$SERVICE" "$ASVC"; then
    # FLSun's file has no final newline.
    [ -z "$(tail -c 1 "$ASVC")" ] || echo >> "$ASVC"
    echo "$SERVICE" >> "$ASVC"
fi

msg "Restarting Moonraker"
sudo systemctl restart moonraker

msg "Running the updater"
sudo /usr/bin/python3 "${REPO_DIR}/flsun_updater.py" run

msg "Done. In Mainsail or Fluidd, open flsun_plugins.cfg in the config files,"
msg "set the plugins you want to yes, and save."
