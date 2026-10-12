#!/bin/bash
# Repairs the parts of FLSun's T1 Pro system image that stop git and
# Moonraker's update manager from reaching GitHub:
#   - the clock starts in the past and never syncs (HTTPS then fails),
#   - the /etc/ssl/certs hash links are missing (curl rejects every cert),
#   - /usr/lib/git-core was deleted (git has no https helper).
# Safe to re-run; install.sh runs it every time, then installs the boot
# service that keeps the clock right. Run as pi; uses sudo.
# Shared, unchanged, by FLSUN_Timelapse and FLSUN_RemoteScreen: whichever
# plugin is installed first applies these fixes.

set -e

GIT_DEB_URL="https://archive.debian.org/debian-security/pool/updates/main/g/git/git_2.20.1-2+deb10u9_armhf.deb"
CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"

msg() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run as the pi user, not root (sudo is used where needed)"

msg "Setting the clock"
# -u uses an unprivileged port, so this works while ntpd is running.
sudo ntpdate -u -b time.google.com || sudo ntpdate -u -b pool.ntp.org \
    || die "could not set the clock; check the printer's network connection"

if ! ls /etc/ssl/certs/*.0 > /dev/null 2>&1; then
    msg "Rebuilding the certificate links"
    sudo update-ca-certificates --fresh > /dev/null
fi

if [ ! -x /usr/lib/git-core/git-remote-https ]; then
    msg "Restoring git's https support"
    tmp=$(mktemp -d)
    curl -fsSL --cacert "$CA_BUNDLE" -o "${tmp}/git.deb" "$GIT_DEB_URL" \
        || die "could not download $GIT_DEB_URL"
    sudo dpkg -i "${tmp}/git.deb" 2>&1 | grep -v 'files list file for package' || true
    rm -rf "$tmp"
    [ -x /usr/lib/git-core/git-remote-https ] || die "git https support is still missing"
fi

msg "System ready"
