#!/bin/sh
# Install or update chrony-solar.  Run from the repository root as root:
#
#   sudo deploy/install.sh [SITE_DIR]
#
# SITE_DIR (default: ./site) holds this site's solar.toml and chrony.conf,
# made from config.example.toml and deploy/chrony-solar.conf.
# Safe to re-run: it updates code, config and units in place.
set -eu

SITE=${1:-site}
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo $0)" >&2; exit 1; }
[ -d solar_chrony ] && [ -d deploy ] || { echo "run from the repository root" >&2; exit 1; }
for f in solar.toml chrony.conf; do
    [ -f "$SITE/$f" ] || { echo "missing $SITE/$f" >&2; exit 1; }
done

echo "== user"
getent passwd chrony-solar >/dev/null ||
    useradd --system --no-create-home --shell /usr/sbin/nologin --groups _chrony chrony-solar

echo "== code -> /opt/chrony-solar"
install -d /opt/chrony-solar
rm -rf /opt/chrony-solar/solar_chrony
cp -r solar_chrony /opt/chrony-solar/
find /opt/chrony-solar -name __pycache__ -prune -exec rm -rf {} +

echo "== config -> /etc/chrony-solar"
install -d /etc/chrony-solar
install -m 0644 "$SITE/chrony.conf" /etc/chrony-solar/chrony.conf
install -m 0644 "$SITE/solar.toml" /etc/chrony-solar/solar.toml

echo "== apparmor"
LOCAL=/etc/apparmor.d/local/usr.sbin.chronyd
if [ -e /etc/apparmor.d/usr.sbin.chronyd ]; then
    touch "$LOCAL"
    grep -q chrony-solar "$LOCAL" || cat deploy/apparmor-local-usr.sbin.chronyd >> "$LOCAL"
    if command -v apparmor_parser >/dev/null && [ -d /sys/kernel/security/apparmor ]; then
        apparmor_parser -r /etc/apparmor.d/usr.sbin.chronyd
    fi
fi

echo "== systemd units"
for u in chronyd-solar.service solar-noon-feed.service solar-noon.service solar-noon.timer \
         solar-noon-learn.service solar-noon-learn.timer; do
    install -m 0644 "deploy/$u" /etc/systemd/system/
done
systemctl daemon-reload

echo "== start the solar chronyd"
systemctl enable chronyd-solar.service
systemctl restart chronyd-solar.service

echo "== start the feed (serves the latest published offset as refclock SUN)"
systemctl enable solar-noon-feed.service
systemctl restart solar-noon-feed.service

echo "== learn the shading model (a minute or so)"
systemctl start solar-noon-learn.service
journalctl -u solar-noon-learn.service -n 5 --no-pager -o cat

echo "== enable timers"
systemctl enable --now solar-noon.timer solar-noon-learn.timer

echo "== status"
systemctl --no-pager --lines=0 status chronyd-solar.service solar-noon-feed.service || true
ss -lunp | grep -E ':123\b' || true
chronyc -h ::1 -p 11323 -m tracking sources || true
systemctl list-timers --no-pager 'solar-noon*'
