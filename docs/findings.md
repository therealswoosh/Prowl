# What is actually known about the MSI GP66 Leopard 11UG

Two kinds of evidence live here:

1. **Measured on this laptop** (2026-10-21) — USBPcap captures of SteelSeries GG on
   the Windows side of the same machine, decoded and compared against this codec.
   These are facts, not guesses.
2. **Public reports** — what other people found, kept because it explains the failure
   modes this toolkit's `probe`/`doctor` exist to diagnose, and because the one
   "device vanished" report is a real risk if we experiment carelessly.

## The target device

```
ID 1038:113a SteelSeries ApS SteelSeries KLC   <- keyboard lighting controller
ID 1038:113c SteelSeries ApS SteelSeries ALC   <- audio side
```

## Measured: what GG actually sends on this machine

From 11 `.pcap` captures taken on the Windows side of this exact laptop
(full analysis in [gg-capture-evidence.md](gg-capture-evidence.md)):

| question | answer |
| -------- | ------ |
| Does the device enumerate? | **Yes.** `1038:113a` appears in every capture, alongside the Razer mouse, webcam and Intel Bluetooth. One HID collection, usage page `0xFFC0`, a 524-byte feature report and a 64-byte output report, **no report IDs**. |
| Which packets drive colour? | `0x0E` (524 B) region frames, then `0x09` (64 B) commit. A full push is four frames: `0x2A` (42 records), `0x18` (24), `0x13` (19), `0x0B` (11) = **96 LEDs**. |
| Does a steady colour need `0x0B`? | **No.** Partial recolours are `0x0E` → `0x0D` → `0x09` with no `0x0B`. `0x0B` only appears in full profile pushes. |
| Which keycodes does the keyboard have? | **96**, and the union of the four region frames is exactly that set — no gaps, no duplicates. |
| Is the GS65-derived keymap right? | **No.** 17 of its 103 entries (`83`–`99`) are never addressed here, and 9 keycodes GG does address (`50, 102, 135-139, 144, 145`) have no X11 name in it. `data/keymaps/gp66-leopard.json` is the pruned, measured map. |
| Is the numpad region `0x24`/36 keys? | **No.** On this keyboard it is `0x13`/19 keys (`64`–`82`). `0x24` is a GS65 layout. |
| `modifiers` keycode 101 or 102? | **102.** One key, same position. |
| Brightness over HID? | Never sent by GG in any capture — consistent with Fn+F8 being firmware-only (`msi-ec` handles the three brightness levels). |
| Does GG use report IDs? | **No.** It addresses report 0 (`wValue 0x0300`/`0x0200`) and puts the opcode in the payload. The libusb backend now addresses report 0 to match; the hidraw path keeps the hidapi convention and retries with report 0 if the kernel answers `EINVAL`. Payload bytes are identical either way. |

This settles, without a hardware run, the old open questions about the wire format and
the keymap. What it cannot settle is whether *Linux* accepts the write — that still
needs the hardware.

## Public evidence (background)

| claim | source | confidence |
| ----- | ------ | ---------- |
| GP66 Vector **12UGS** (12th gen) drives its per-key lighting with `msi-perkeyrgb --id 1038:113a --model GS65` on Linux | [issue #55](https://github.com/Askannz/msi-perkeyrgb/issues/55) | high — a user reports the exact working command |
| The power-button LED keeps its factory animation, no keymap controlled it | same issue | high |
| GP66 Leopard (11th gen) is **not** on any tested-model list | [README](https://github.com/Askannz/msi-perkeyrgb#compatibility) | high |
| A GP66 Leopard 11UG report fails with "No USB device with ID 1038:113a found" even with `--id` | [issue #60](https://github.com/Askannz/msi-perkeyrgb/issues/60) | high (report exists) — **not reproduced here**: the device enumerates normally. Most likely a tool-side discovery/permission quirk, which is what `probe`/`doctor` replace with facts. |
| OpenRGB 0.9 lists an "MSI 3-Zone Laptop Keyboard" at `1770:FF00`, a different device from the per-key KLC | [OpenRGB devices](https://openrgb.org/devices_0.9.html?search=MSI) | high |
| OpenRGB does not detect `1038:113a` on a GP66 11UG; disabling BIOS Fast Boot fixed enumeration but not detection | [OpenRGB #4827](https://gitlab.com/CalcProgrammer1/OpenRGB/-/issues/4827) | high (report exists) |
| One GP66 Leopard user reports `1038:113a` **disappearing from `lsusb`** after running `msi-perkeyrgb` | [issue #55 comment](https://github.com/Askannz/msi-perkeyrgb/issues/55) | high (report exists) — this is the scary one |
| Mainline Linux has MSI EC support (fan/power/battery, plus `0-3` keyboard brightness), but no per-key RGB | [msi-ec](https://github.com/BeardOverflow/msi-ec), [Phoronix](https://www.phoronix.com/news/Linux-6.4-MSI-EC-Driver) | high |
| Effects (`0x0B`) once bricked a keyboard's lighting, unrecoverable by EC reset ⇒ effects support removed | [issue #24](https://github.com/Askannz/msi-perkeyrgb/issues/24) | high |

## Reading the public evidence

1. **The protocol is almost certainly right for `1038:113a`.** A 12th-gen GP66 with
   the same USB id accepted the `0x0E`/`0x09` steady-colour frames. We have now also
   verified the byte layout against this machine's own GG traffic.
2. **"Device not found" is a tool problem, not a hardware problem.** The captures show
   the device enumerating fine on this laptop, so a failure to find it points at the
   HID interface chosen, a stale `lsusb` string match, or permissions. `msikb probe`
   enumerates every hidraw node with VID/PID and name, and `--transport libusb
   --interface N` can target a specific interface.
3. **BIOS Fast Boot can hide the device entirely.** Reported for a GP66 11UG; worth
   checking first, because "no device" is a worse failure mode than "wrong interface".
4. **The controller can be left dead.** The one report of `1038:113a` vanishing from
   `lsusb` is unexplained. A full power cycle (shutdown, hold power ~15 s, drain the
   battery) is the recovery that usually revives a wedged KLC. Prefer the documented
   steady-colour path over inventing packets.

## What only the hardware can answer

* Does `/dev/hidraw*` exist for `1038:113a` on this Linux install, and how many nodes?
* Does the controller accept the 524-byte `0x0E` feature report through
  `HIDIOCSFEATURE`, or does it need report id 0 (`b"\x00" + frame`)?
* Is an udev rule enough, or does this model need root?
* Names for the nine unnamed keycodes `50, 102, 135-139, 144, 145`
  (`msikb verify` flashes one key at a time).
* Does lighting survive reboot/suspend without the systemd units?

## Suggested order of experiments

1. `sh scripts/first-run.sh` — read-only: probe, doctor, dry run of the exact packets.
2. `sudo python3 -m msikb set --color ff0000 --commit` — first real write; watch that
   the device stays on the bus.
3. `sudo sh scripts/install.sh` — udev + systemd, then drops the need for `sudo`.
4. `python3 -m msikb verify --commit --dwell 0.5` — confirms the keymap key by key.
5. Re-capture with `msikb learn` only for keys step 4 got wrong.
