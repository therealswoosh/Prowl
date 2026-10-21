#!/bin/sh
# msikb first run on the MSI laptop. READ-ONLY: it never writes to the keyboard.
#
#   sh scripts/first-run.sh
#
# Run this in a native Linux session (not WSL, not a VM): the tool needs the
# internal keyboard's /dev/hidraw* node, which only the bare-metal kernel has.
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON=${MSIKB_PYTHON:-python3}
REPORT="$ROOT/msikb-diagnose.txt"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
msikb() { "$PYTHON" -m msikb "$@"; }

section() { printf '\n========== %s ==========\n' "$1"; }

section "0. prerequisites"
"$PYTHON" --version || { echo "python3 is required"; exit 1; }
printf 'kernel: '; uname -sr
if grep -qi microsoft /proc/version 2>/dev/null; then
    echo
    echo "WARNING: this looks like WSL. The internal keyboard will not be reachable"
    echo "here -- hidraw nodes for it do not exist in WSL. Boot Linux directly."
fi

section "1. system report -> $REPORT"
sh "$ROOT/scripts/diagnose.sh" >"$REPORT" 2>&1
echo "wrote $REPORT ($(wc -l <"$REPORT") lines)"

section "2. device probe"
msikb probe || true

section "3. environment verdict"
msikb doctor || true

section "4. packets we would send (nothing is written)"
msikb set --color ff0000 || true

cat <<'NEXT'

========== next steps =================================================
Only if step 3 said OK for the 1038:113a hidraw node, in this order:

  # 1. first real write: whole keyboard red, using the keymap measured from
  #    SteelSeries GG on this exact model (96 LEDs, four region frames)
  sudo python3 -m msikb set --color ff0000 --commit

  # 2. if that lit anything, install udev + systemd so it sticks and stops
  #    needing sudo (also applies the colour once, immediately)
  sudo sh scripts/install.sh

  # 3. check the keymap against the hardware, one key at a time
  python3 -m msikb verify --commit --dwell 0.5

  # 4. a key that never lights in step 3 is either unnamed on this model (the
  #    map calls those msi50, msi102, ...) or has a different keycode. Re-capture
  #    it from SteelSeries GG and rebuild the map:
  python3 -m msikb capture-help
  python3 -m msikb learn captures/*.tsv --keymap-name gp66-leopard \
      --output data/keymaps/gp66-leopard.json

Stop and read docs/findings.md if the device is missing from the probe, or if it
disappears from `lsusb` after a write (power-cycle + drain the battery resets it).
Keep msikb-diagnose.txt if you need to report a problem.
NEXT
