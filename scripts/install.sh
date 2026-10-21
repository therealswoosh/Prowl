#!/bin/sh
# Install msikb on the MSI laptop: udev rule, apply script, systemd units.
#
#   sudo sh scripts/install.sh              # install, enable at boot, light it up
#   sudo sh scripts/install.sh --no-apply   # install + enable, but write nothing
#   sudo sh scripts/install.sh --no-enable  # install without enabling the units
#   sudo sh scripts/install.sh --uninstall  # remove what was installed
#
# Everything is idempotent; re-running updates the installed copies in place.
# The udev rule only touches 1038:113a-class hardware (never all hidraw nodes),
# and the units only ever send the steady-colour frames.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PREFIX=${PREFIX:-/usr/local}
UDEV_RULE=99-msi-steelseries-rgb.rules
SERVICES="msikb-lighting.service msikb-lighting-resume.service"

usage() {
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
}

uninstall() {
    echo "-> removing systemd units"
    for unit in $SERVICES; do
        systemctl disable --now "$unit" 2>/dev/null || true
        rm -f "/etc/systemd/system/$unit"
    done
    systemctl daemon-reload || true
    echo "-> removing udev rule"
    rm -f "/etc/udev/rules.d/$UDEV_RULE"
    udevadm control --reload-rules || true
    udevadm trigger --subsystem-match=hidraw || true
    echo "-> removing $PREFIX/bin/msikb-apply"
    rm -f "$PREFIX/bin/msikb-apply"
    echo
    echo "msikb is uninstalled. The checkout at $ROOT is untouched; delete it yourself"
    echo "if you no longer want it (~/.config/msikb and ~/.cache/msikb are yours)."
    exit 0
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    --uninstall) mode=uninstall ;;
    --no-apply) mode=install; apply=0; enable=1 ;;
    --no-enable) mode=install; apply=1; enable=0 ;;
    "") mode=install; apply=1; enable=1 ;;
    *) echo "error: unknown option $1 (try --help)" >&2; exit 2 ;;
esac

if [ "$(id -u)" -ne 0 ]; then
    echo "error: run this with sudo (it installs system files)" >&2
    exit 1
fi

if [ "$mode" = uninstall ]; then
    uninstall
fi

if [ ! -f "$ROOT/msikb/protocol.py" ]; then
    echo "error: $ROOT does not look like an msikb checkout" >&2
    exit 1
fi

echo "-> udev rule (1038:113a / 1122 / 113c only)"
install -m 0644 "$ROOT/udev/$UDEV_RULE" "/etc/udev/rules.d/$UDEV_RULE"
udevadm control --reload-rules
udevadm trigger --subsystem-match=hidraw

echo "-> $PREFIX/bin/msikb-apply"
install -m 0755 "$ROOT/scripts/msikb-apply" "$PREFIX/bin/msikb-apply"

echo "-> systemd units"
for unit in $SERVICES; do
    install -m 0644 "$ROOT/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload

# The units run msikb-apply, which needs to find the checkout. Record where it is
# instead of relying on a shell profile, because systemd does not read one.
#
# They also run as root, so a per-user colour profile is not visible to them: keep
# the system-wide copy in /etc/msikb/colors.json and seed it from the invoking user.
PROFILE=/etc/msikb/colors.json
mkdir -p /etc/msikb
if [ -n "${SUDO_USER:-}" ]; then
    user_home=$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6 || true)
    user_profile="${user_home:-/root}/.config/msikb/colors.json"
    if [ -f "$user_profile" ]; then
        echo "-> colour profile: $user_profile -> $PROFILE"
        install -m 0644 "$user_profile" "$PROFILE"
    fi
fi

for unit in $SERVICES; do
    mkdir -p "/etc/systemd/system/$unit.d"
    cat >"/etc/systemd/system/$unit.d/10-root.conf" <<EOF
[Service]
Environment=MSIKB_ROOT=$ROOT
Environment=MSIKB_CONFIG=$PROFILE
EOF
done
systemctl daemon-reload

if [ "$enable" -eq 1 ]; then
    echo "-> enabling at boot and after resume"
    for unit in $SERVICES; do
        systemctl enable "$unit" >/dev/null
    done
fi

echo "-> settling udev so the rule applies to the connected controller"
udevadm settle --timeout=5 2>/dev/null || true

echo
echo "installed. current state:"
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -m msikb doctor || true

if [ "$apply" -eq 1 ]; then
    echo
    echo "-> applying the installed colour profile (steady colours only, no effects)"
    MSIKB_ROOT="$ROOT" MSIKB_CONFIG="$PROFILE" "$PREFIX/bin/msikb-apply"
fi

cat <<EOF

next:
  # colour: a flat value, or a per-key json profile at $PROFILE (what the units use)
  echo '{"default": "ff0000"}' | sudo tee $PROFILE >/dev/null
  sudo systemctl start msikb-lighting.service   # try it without rebooting

  # or apply a profile right now, as the logged-in user (udev grants access)
  MSIKB_CONFIG=$PROFILE $PREFIX/bin/msikb-apply

  # check the keymap against the real keyboard, one key at a time
  python3 -m msikb verify --commit --dwell 0.5
EOF
