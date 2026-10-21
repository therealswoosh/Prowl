#!/bin/sh
# Collect everything needed to diagnose MSI SteelSeries keyboard lighting on Linux.
#
#   sh scripts/diagnose.sh > msikb-diagnose.txt 2>&1
#
# Safe: reads descriptors and permissions, never writes to the keyboard.
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON=${MSIKB_PYTHON:-python3}

section() { printf '\n===== %s =====\n' "$1"; }

section "system"
uname -a
[ -r /etc/os-release ] && cat /etc/os-release

section "usb devices matching 1038 (SteelSeries)"
lsusb | grep -i -e steelseries -e 1038 || echo "(none found -- is the KLC enumerated?)"

section "usb descriptors for 1038 (needs root for full output)"
lsusb -v -d 1038: 2>/dev/null | head -120 || echo "(could not read descriptors; run as root for this section)"

section "hidraw nodes"
for node in /dev/hidraw*; do
    [ -e "$node" ] || continue
    printf '%s: ' "$node"
    ls -l "$node" | awk '{print $1, $3, $4}'
    name=$(basename "$node")
    if [ -r "/sys/class/hidraw/$name/device/uevent" ]; then
        grep -e HID_ID -e HID_NAME "/sys/class/hidraw/$name/device/uevent" | tr '\n' ' '
        echo
    fi
    desc="/sys/class/hidraw/$name/device/report_descriptor"
    [ -r "$desc" ] && printf '  report_descriptor: %s bytes\n' "$(wc -c < "$desc")"
done

section "kernel modules"
lsmod | grep -e hid -e usbhid | head -20

section "recent kernel messages"
dmesg 2>/dev/null | grep -i -e hidraw -e steelseries -e "1038" | tail -30 || echo "(dmesg needs root)"

section "msikb doctor"
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -m msikb doctor 2>&1

section "done"
echo "Paste this whole file when reporting a problem."
