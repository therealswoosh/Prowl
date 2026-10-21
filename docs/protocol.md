# The MSI SteelSeries keyboard lighting protocol

This is the wire format SteelSeries Engine / SteelSeries GG uses to drive the
per-key RGB backlight on MSI laptops with a "SteelSeries KLC" controller. It was
first reverse engineered with Wireshark by Askannz and extended by TauAkiou; this
document restates it for the subset `msikb` implements, with the exact bytes.

**Measured, not only inherited:** the tables below were re-checked against 11
USBPcap captures of SteelSeries GG running on the GP66 Leopard 11UG this toolkit
targets (two full pushes are kept in `tests/fixtures/gg/`). Where GG and the older
GS65-derived documentation disagree, this document follows GG and says so.

Sources:

* <https://github.com/Askannz/msi-perkeyrgb> — working steady-colour implementation
  (`msi_perkeyrgb/msiprotocol.py`, `protocol_data/keycodes.py`, `protocol_data/msi_keymaps.py`)
* `documentation/0b_packet_information/msi-kb-effectdoc` in that repository —
  TauAkiou's write-up of the effect (`0x0B`) packet
* <https://github.com/Askannz/msi-perkeyrgb/issues/55> — GP66 Vector 12UGS success report
* <https://github.com/Askannz/msi-perkeyrgb/issues/24> — the effect-packet lockup that got
  effects support disabled

## Transport

The keyboard appears as a USB HID device. On the models we care about it is
`1038:113a` ("SteelSeries KLC"), bound to the `usbhid` driver, so Linux exposes a
`/dev/hidrawN` node and the kernel's `HIDIOCSFEATURE` ioctl can send reports without
any third-party library. Keypresses travel over a separate PS/2 path, so RGB traffic
never interferes with typing.

Two report ids are used:

| report | direction | length | purpose |
| ------ | --------- | ------ | ------- |
| `0x0E` | feature (control transfer, `bmRequestType 0x21`, `bRequest 0x09`, `wValue 0x030E`) | 524 bytes | colours for up to 42 keys |
| `0x09` | output (control transfer, `wValue 0x0209`) | 64 bytes | commit/apply the pending frame |

After each report the controller wants a short pause; the reference implementation
sleeps 10 ms (`msikb` uses the same default, `--delay`).

The report buffer includes the report id as its first byte, matching `hidapi` and
`hidraw` conventions:

* `hid_send_feature_report()` / `HIDIOCSFEATURE(524)` with `data[0] == 0x0E`
* `hid_write()` / `write(fd, ...)` with `data[0] == 0x09`

That is the convention `msi-perkeyrgb` uses and it works on a GP66 Vector 12UGS.
GG itself addresses **report 0** on this model (`wValue 0x0300` / `0x0200`, no report
IDs in the HID descriptor) and puts the opcode in the payload instead. The payload
bytes are identical either way. If a write is rejected with `EINVAL` from
`HIDIOCSFEATURE`, prefixing the buffer with a `0x00` report id reproduces GG's transfer
exactly; `msikb` does that retry automatically and prints a note when it does. The
libusb backend addresses report 0 from the start (`--report-id` overrides it).

## The `0x0E` colour frame

```
offset  size  contents
0x000   1     report id 0x0E
0x001   1     0x00
0x002   1     record count for this packet (also the region id, see below)
0x003   1     0x00
0x004   504   42 key records, 12 bytes each
0x1FC   16    trailer (GG pads zeros; msikb sends the reference `00*14 08 39`)
```

Total: 4 + 504 + 16 = **524** bytes (`0x20C`).

### Region ids

The controller is told about a key's colour in a frame tagged with the region the
key belongs to. A key outside its region is (at best) ignored, which is why the
keymap carries a region for every keycode.

| region | byte 2 | MSI keycodes carried |
| ------ | ------ | -------------------- |
| `alphanum` | `0x2A` (42) | 4–39, 58–63 |
| `enter` | `0x0B` (11) | 40, 49, 50, 100, 135–139, 144, 145 |
| `modifiers` | `0x18` (24) | 41–48, 51–57, **102**, 224–230, 240 |
| `numpad` | `0x13` (19) | 64–82 |

42 + 24 + 19 + 11 = **96**, the number of addressable LEDs on a GP66 Leopard 11UG.

The byte doubles as the record count, which is why it differs between models: the
GS65-derived table that circulated first used `0x24` (36 keys, keycodes 64–99) for
the fourth region. That is a GS65 layout. On this keyboard `83`–`99` are never
addressed and the fourth region holds 19 keys. If you port this codec to another
MSI model, re-measure these four numbers before trusting the keymap.

Unused slots in a region are written as all-zero records. The region names are
MSI's own grouping and are not a physical description: for example `space` (MSI
keycode 44) travels in the `modifiers` frame, and the numpad region also carries
`scrolllock` and `pause`.

### Key record (12 bytes)

```
[ R G B ] [ 0 0 0 0 0 0 ] [ 0x01 ] [ mode ] [ keycode ]
   0 1 2      3 4 5 6 7 8       9        10        11
```

* bytes 0–2 — the colour, one byte per channel, no gamma/brightness byte. A colour
  of `00 00 00` means "this key is black".
* bytes 3–8 — zero in static mode. TauAkiou's document labels this run as reactive
  colour + reactive duration. `msikb` writes zeroes, exactly like the reference
  implementation. GG actually puts `2c 01` in bytes 6–7 here (a 300 ms reactive
  duration left over from its default profile) but the colour is static either way;
  a working implementation sends zeroes, so there is no reason to copy that.
* byte 9 — `0x01` in every steady-colour capture **with a colour**. In TauAkiou's
  layout this is the effect id, and byte 10 is the mode field (`0` effect-no-refresh,
  `1` static inline, `2` effect-refresh, `8` reactive).
* byte 10 — `0x00` for steady colours.
* byte 11 — MSI keycode, the number that decides which physical key lights up.

One GG quirk worth knowing when comparing bytes: in an all-black push GG moves the
`0x01` to byte 8 and leaves byte 9 zero for records it is not lighting. The colour
and the keycode are byte-identical; only the effect fields move. `msikb` keeps
`0x01` in byte 9 for black records too, which is still "steady colour, this colour".

Because records are keycode-addressed and padded slots address keycode 0, a frame
can carry a subset of a region. Whether unlisted keys are left untouched or forced
black is model/firmware dependent as far as the public captures show — `msikb set`
therefore always writes every key of every region explicitly (a deterministic full
frame), while `msikb key` sends just the one record.

### Trailer

The last 16 bytes are constant in every preset file of the reference project
(`00 * 14`, `08`, `39`) and are not a checksum — they never change with the colours.
GG on the GP66 Leopard 11UG pads the same 16 bytes with zeroes instead, and the
records it declares end well before them, so the firmware evidently reads the
record count rather than the tail. `msikb` keeps sending the reference constant
because that is the value known to work on a GP66 Vector 12UGS, and flags a mismatch
when decoding foreign captures. If a first hardware run ever shows colours being
ignored, swapping the tail for 16 zeroes is the cheapest thing to try.

## The `0x09` commit

```
[ 0x09 ] [ 0x00 * 63 ]
```

Sent once after the colour frames. Without it the colours do not appear: the
controller buffers the frame. GG sends one commit per visual update.

## The `0x0B` effect packet — deliberately not implemented

`0x0B` carries an effect: up to 16 transitions of 8 bytes, a starting colour stored
nibble-swapped, wave origin/wavelength, transition count and total duration. It can
express every built-in GG effect. `msikb` does not implement it, and neither does
current `msi-perkeyrgb`: sending an effect with no transitions left a keyboard's
lighting inoperative, and an EC reset did not reliably recover it
([issue #24](https://github.com/Askannz/msi-perkeyrgb/issues/24)). The full format is
documented in `msi-kb-effectdoc` if you want to read it — but not to run it on a
machine you need.

## Keycodes

Two numbering spaces appear in the wild, and mixing them up is the usual reason a
script lights the wrong keys:

* **MSI keycodes** — the byte in a record, and the values in the region table above.
  These are what the controller understands.
* **X11 keycodes** — what `xev`, `msi-perkeyrgb` config files and this toolkit's
  keymaps use. X11 keycode = Linux evdev keycode + 8, so `esc` (evdev 1) is X11 9
  and MSI 41 in the reference map, and WASD is X11 `25,38,39,40`.

`msikb keys` prints both numbers for every key, which makes it easy to translate a
forum post in either convention.

## Windows capture recipe

To re-derive any of this on your own machine (and to learn a model-specific
keymap), capture GG with USBPcap and reduce the trace with `tshark`:

```sh
tshark -r gg-red.pcapng -Y "usb.setup.bRequest == 0x09" -T fields \
  -E separator=/t -e frame.number -e usb.device_address -e usb.setup.wValue \
  -e usb.setup.wIndex -e usb.capdata -e usb.data_fragment > red.tsv
python -m msikb decode red.tsv --keymap gp66-leopard
```

A `0x0E` frame shows up as 524 bytes of payload, the commit as 64 bytes.
`usb.setup.wValue` high byte is the report type (`0x03` feature, `0x02` output). In
GG's own traffic the low byte is `0x00` — no report id — so the `wValue` is `0x0300`
for a frame and `0x0200` for the commit; the report id the codec talks about is the
first payload byte. Wireshark traces from tools built on hidapi show `0x030e` /
`0x0209` instead because hidapi folds the report id into `wValue`; the payload after
that is the same. Filtering on `usb.setup.bRequest == 0x09` (HID SET_REPORT) catches
both.
