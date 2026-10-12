#!/bin/bash
# Removes FLSUN_Updater. Plugins it installed stay installed; remove them
# first with DISABLE_PLUGIN NAME=<plugin> if you don't want them.
# Restarts Klipper, so don't run it during a print.

set -e

CONFIG_DIR="${HOME}/printer_data/config"
MOONRAKER_CONF="${CONFIG_DIR}/moonraker.conf"
ASVC="${HOME}/printer_data/moonraker.asvc"
SERVICE="flsun_updater"

msg() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*"; }

[ "$(id -u)" -ne 0 ] || { echo "run as the pi user, not root" >&2; exit 1; }

msg "Removing the updater service"
sudo systemctl disable --now "${SERVICE}.path" "${SERVICE}.service" > /dev/null 2>&1 || true
sudo rm -f "/etc/systemd/system/${SERVICE}.path" "/etc/systemd/system/${SERVICE}.service"
sudo rm -f /etc/systemd/system/flsun_updater_web.service
sudo rm -f /etc/sudoers.d/zz-flsun-updater
sudo systemctl daemon-reload

if grep -q '^\[update_manager' "${CONFIG_DIR}/flsun_updater_plugins.conf" 2>/dev/null; then
    warn "plugins listed in flsun_updater_plugins.conf stay installed but no longer appear in the update manager"
fi

msg "Removing the plugin macros"
sed -i '/^\[include flsun_updater_macros\.cfg\]/d' "${CONFIG_DIR}/printer.cfg"
rm -f "${CONFIG_DIR}/flsun_updater_macros.cfg"

msg "Removing the Moonraker component"
rm -f "${HOME}/moonraker/moonraker/components/flsun_plugins.py"
rm -rf "${HOME}/printer_data/flsun_updater"

msg "Removing the Moonraker settings"
sed -i -e '/^\[include flsun_updater\.conf\]/d' \
    -e '/^\[include flsun_updater_plugins\.conf\]/d' "$MOONRAKER_CONF"
rm -f "${CONFIG_DIR}/flsun_updater.conf" "${CONFIG_DIR}/flsun_updater_plugins.conf" \
    "${CONFIG_DIR}/flsun_plugins.cfg" "${HOME}/printer_data/flsun_updater_state.json"
if [ -f "$ASVC" ]; then
    sed -i "/^${SERVICE}\$/d" "$ASVC"
fi

# The flsun-timesync boot service is left in place on purpose: a correct
# clock helps everything else on the printer too, and the other FLSun
# plugins use the same service.

msg "Restarting Moonraker and Klipper"
sudo systemctl restart moonraker
sudo systemctl restart klipper

msg "Done. Delete ~/FLSUN_Updater to finish."
