# Handoff: light the keyboard up on the MSI laptop (Linux)

Pick this up on the laptop, in a native Linux boot (not WSL, not a VM — the internal
keyboard's hidraw node only exists on bare metal).

## The goal

Light the per-key RGB keyboard of an MSI GP66 Leopard 11UG (SteelSeries KLC, USB
`1038:113a`) under Linux, without SteelSeries GG. GG is Windows-only; the backlight is
driven by HID reports that `msikb` sends itself.

## State of the work

The codec and the keymap are no longer guesses. They were checked against 11 USBPcap
captures of SteelSeries GG on the Windows side of **this same laptop**:

* four region frames `0x2A`/`0x18`/`0x13`/`0x0B` = 42/24/19/11 = **96 LEDs**, which is
  this keyboard's complete keycode set,
* key `102` (not `101`) in `modifiers`, keycodes `64`–`82` (not `64`–`99`) in the
  numpad region,
* `data/keymaps/gp66-leopard.json` — the measured map: 86 named keys, plus `fn`, plus
  nine addresses GG uses that have no X11 name (`msi50`, `msi102`, `msi135`–`msi139`,
  `msi144`, `msi145`),
* `tests/fixtures/gg/*.hex` are two of GG's full pushes, and `tests/test_gg_capture.py`
  pins the codec to them so the tables cannot drift back to the GS65 layout.

Implemented and locally tested (100 tests,
`python3 -m unittest discover -s tests -t .`), **never yet run against the physical
keyboard from Linux**:

* `msikb/protocol.py` — `0x0E` 524-byte colour frames + `0x09` 64-byte commit
* `msikb/keymaps.py` — X11/evdev ↔ MSI keycode, names, aliases, `msi<N>` for unnamed LEDs
* `msikb/transport.py` — hidraw ioctl backend (no third-party deps) and pyusb alternative
* `msikb/capture.py` — tshark/.pcapng and hex decoding, keymap inference
* `msikb/cli.py` — `probe`, `doctor`, `keys`, `set`, `key`, `verify`, `decode`, `learn`,
  `capture-help`
* integration — `udev/` rule, `systemd/` units, `scripts/first-run.sh`,
  `scripts/install.sh`, `scripts/msikb-apply`, `scripts/diagnose.sh`

## Run order on the laptop

```sh
# 0. read-only: probe, doctor, and a dry run of the exact bytes (nothing is written)
sh scripts/first-run.sh

# 1. first real write: whole keyboard red
sudo python3 -m msikb set --color ff0000 --commit

# 2. udev rule + apply script + both systemd units, enabled at boot/resume,
#    and the colour applied once right away
sudo sh scripts/install.sh

# 3. check the keymap key by key against the hardware
python3 -m msikb verify --commit --dwell 0.5
```

`scripts/install.sh` is safe to re-run (`--no-apply` to skip the write, `--no-enable` to
leave the units disabled, `--uninstall` to remove it). The udev rule matches only
`1038:113a`/`1122`/`113c`, never all hidraw nodes. The units run as root and read
`/etc/msikb/colors.json`; `install.sh` seeds that from `~/.config/msikb/colors.json` of
the user who invoked `sudo`, because a system service does not inherit their `$HOME`.

## Failure branches

* **No hidraw node for `1038:113a`** — check `lsusb`; disable BIOS Fast Boot; if it is
  still absent, try `--transport libusb --interface N` (`pip install pyusb`).
* **Node exists but not writable** — udev rule or root (step 2 fixes this permanently).
* **`HIDIOCSFEATURE` fails with `EINVAL`** — GG addresses report id 0 on this model, so
  the transport already retries once with `0x00` and prints `note: ... wants report id
  0` when that works. If both forms fail, use `--transport libusb --interface N`, which
  addresses report 0 from the start (`--report-id` overrides).
* **Write succeeds but nothing lights** — compare against the captured bytes with
  `python3 -m msikb decode tests/fixtures/gg/newcap-2a.hex`; then try the trailer as
  16 zeroes, which is what GG sends. Both are documented, neither has been hardware-tested.
* **Device disappears from `lsusb` after a write** — shut down, hold power ~15 s, drain
  the battery. That reset is what revives a wedged KLC. Do not retry by inventing packets.
* **One key never lights in `verify`** — it is either one of the nine unnamed addresses
  or has a different keycode. Capture it from GG and rebuild with `msikb learn`.

## Guardrails

* **Never send the `0x0B` effect packet.** It once left a keyboard's lighting
  inoperative with no reliable recovery
  ([msi-perkeyrgb#24](https://github.com/Askannz/msi-perkeyrgb/issues/24)). The captures
  confirm steady colours do not need it: GG's partial recolours skip `0x0B` entirely.
* Every write goes through `--commit`; everything else is a dry run that prints the
  exact bytes. Keep it that way — do not add convenience defaults that write.
* Do not widen the ioctl/libusb code into blind probing loops on a controller that can
  wedge: prefer enumerated nodes and explicit interfaces.
* udev rules stay targeted at `1038:113a`-class hardware; no `chmod 666` on all hidraw.

## Keycode cheat sheet

* **MSI keycode** — byte 11 of a key record; what the controller understands; grouped
  into region packets (`alphanum 0x2A`, `enter 0x0B`, `modifiers 0x18`, `numpad 0x13`).
* **X11 keycode** = Linux evdev code + 8 — what `xev` prints and what keymaps use.
  `esc` = X11 9 = MSI 41. WASD = X11 `25,38,39,40`.
* `python3 -m msikb keys` prints name, X11 code, MSI code and region for every key,
  including the `msi<N>` entries that have no Linux name.

## Re-capturing GG (if the keymap ever needs to change)

The captures that produced this map were made on the Windows side of the same laptop:

```powershell
.\scripts\capture-windows.ps1 -List
.\scripts\capture-windows.ps1 -Interface <n> -Seconds 90 -Output gg-red.pcapng
```

Change one key to pure red in GG, apply, stop. Then reduce and decode:

```sh
tshark -r gg-red.pcapng -Y "usb.setup.bRequest == 0x09" -T fields \
  -E separator=/t -e frame.number -e usb.device_address -e usb.setup.wValue \
  -e usb.setup.wIndex -e usb.capdata -e usb.data_fragment > red.tsv
python3 -m msikb decode red.tsv --keymap gp66-leopard
python3 -m msikb learn captures/*.tsv --keymap-name gp66-leopard --output gp66-leopard.json
```

Only the resulting `.json` needs to reach Linux.

## Reporting a blocker

`sh scripts/diagnose.sh > msikb-diagnose.txt` gathers kernel, USB descriptors, hidraw
permissions, dmesg and `msikb doctor` in one file. That plus the failing command is
usually enough to diagnose remotely. `scripts/first-run.sh` writes it automatically.
