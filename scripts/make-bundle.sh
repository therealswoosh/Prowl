#!/bin/sh
# Package this checkout for the trip to the MSI laptop.
#
#   sh scripts/make-bundle.sh            # -> dist/msikb-bundle.tar.gz
#
# On the laptop:
#   mkdir -p ~/msikb && tar -xzf msikb-bundle.tar.gz -C ~/msikb
#   cd ~/msikb && sh scripts/first-run.sh
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT_DIR="$ROOT/dist"
BUNDLE="$OUT_DIR/msikb-bundle.tar.gz"

mkdir -p "$OUT_DIR"
rm -f "$BUNDLE"

# Only source, data and docs: no __pycache__, no previous bundles, no local reports.
tar -czf "$BUNDLE" \
    --exclude='./dist' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='./msikb-diagnose.txt' \
    -C "$ROOT" \
    README.md LICENSE pyproject.toml .gitignore \
    msikb data docs examples scripts tests udev systemd

echo "wrote $BUNDLE ($(wc -c <"$BUNDLE") bytes)"
tar -tzf "$BUNDLE" | wc -l | xargs printf '%s files\n'
