# What the GG captures prove about `msikb`

These 11 USBPcap files are SteelSeries GG traffic from the Windows side of the same
laptop (`1038:113a` SteelSeries KLC). That closes the two open questions in
`docs/handoff.md` that a capture can settle — the wire format and the keymap — without
a hardware session, and it turns up four concrete differences between GG's bytes and
`msikb/protocol.py`.

Everything below is measured from the captures. Nothing was sent to the hardware.

> **Status:** all of this has since been applied in `analysis/msikb` — the region
> table, the model keymap (`data/keymaps/gp66-leopard.json`, now the default), the
> `msi<N>` names for the nine unnamed LEDs, the GG-derived regression fixtures and the
> report-0 fallback. Sections 6 and 7 are kept as the record of what changed and what
> still needs the hardware.

## 1. The device is present and enumerated normally

| | |
|---|---|
| 1038:113a SteelSeries KLC | 12–290 packets per capture; HID interface with the 524-byte vendor feature report |
| 1532:0083 Razer mouse | 93–99 % of every capture (1 kHz polling noise) |
| 5986:211c Chicory webcam | enumeration only |
| 8087:0032 Intel Bluetooth | HCI bulk traffic |

So `/dev/hidraw*` should exist for this machine — the GP66 Leopard 11UG "device not
found" reports in `docs/findings.md` are not reproduced here. The vendor collection
is usage page `0xFFC0`, one 524-byte feature report and one 64-byte output report,
no report IDs.

## 2. Region packets: byte 2 is the record count, and this keyboard's numpad region has 19 keys

GG sent exactly four region frames per full update. Measured keycode order:

| capture byte 2 | keycodes, in wire order | `msikb` today |
|---|---|---|
| `0x2A` (42) | `4-39, 58-63` | matches exactly |
| `0x18` (24) | `41-48, 51-57, 102, 224-230, 240` | **has `101` instead of `102`** |
| `0x13` (19) | `64-82` | **has `0x24` (36) and `64-99`** |
| `0x0B` (11) | `40, 49, 50, 100, 135-139, 144-145` | matches exactly |

42 + 24 + 19 + 11 = **96**, the whole keyboard. Byte 2 is the number of records in
that packet, not a fixed model-independent region id — which is why the fourth region
is `0x13` here and `0x24` in the GS65-derived table. With `REGION_IDS["numpad"] = 0x13`
and `REGION_KEYCODES["numpad"] = [64..82]`, `msikb`'s region tables contain exactly the
96 keycodes GG addresses, with no duplicates (verified).

## 3. Keymap: the reference map is 17 keys too big and 10 keys short

GG's full-region frames include a black record for every key it is not lighting, so the
union of addressed keycodes is the complete keyboard, not just the lit keys.

| | count |
|---|---|
| MSI keycodes GG addresses | **96** |
| entries in `reference-gs65.json` | 103 |
| kept (exist here) | **86** |
| dropped — never addressed by GG | **17**: `83-99` (the GS65 numpad keys) |
| addressed by GG but unnamed in the map | **9**: `50, 102, 135-139, 144, 145` |

`fn` is `240`, which the map already carries as `fn_code`.

Consequences for the current code: `set --keys all` writes 113 keycodes, 17 of which
do not exist on this keyboard, and it never touches the 9 keys above (so `--clear-others`
cannot clear them). [gp66-leopard.json](../data/keymaps/gp66-leopard.json) is the pruned map — 86 named
entries plus `fn` — and `unmapped_msi_keycodes` records the 9 that need naming on the
hardware (`msikb verify` flashes one key at a time, which is how to name them).

Loaded through `msikb.keymaps`, that keymap gives 87 keycodes from `all`, all
within GG's 96, and its region assignment agrees with GG's packet for every key.

## 4. Four byte-level differences from GG

| | GG (captured) | `msikb` |
|---|---|---|
| frame tail | all zeros after the declared records | `00*14 08 39` |
| key record bytes 6-7 (steady colour) | `2c 01` | `00 00` |
| address of the feature report | report id **0**, `wValue 0x0300`, 524 bytes | report id **0x0E**, `wValue 0x030E` |
| commit address | report id **0**, `wValue 0x0200`, 64 bytes | report id **0x09**, `wValue 0x0209` |

How much these matter:

* **Tail and bytes 6-7 are probably benign.** The tail sits past the last declared
  record, and `msi-perkeyrgb` sends the same `08 39` on working machines. Bytes 6-7
  are also zero in `msi-perkeyrgb`, which demonstrably works. Record bytes
  `[9]=0x01 [10]=0x00 [11]=keycode` match GG exactly.
* **Report addressing is the one to watch.** The descriptor has no report IDs, so GG
  addresses report 0 and the opcode is simply the first payload byte. `msikb` takes
  `data[0]` as the HID report number, which is the same convention `msi-perkeyrgb`
  uses via hidapi (and works for people on 12UGS). The payload bytes are identical
  either way — but if a write is rejected with `EINVAL` from `HIDIOCSFEATURE`, report
  id 0 (`b"\x00" + frame`) is what the vendor actually does, so try that first.
* The commit payload GG sends is all zeros, sometimes with `1`/`2` in the last byte;
  `msikb`'s all-zero commit is within what GG sends.

## 5. Steady colours really do not need the 0x0B effect packet

This is the reassuring one for the project's safety rule:

* Full profile pushes (`enable`, `newcap`, `color1`, `capture`) send `0x0B` palette
  tables (18 of them in `enable`) interleaved with the colour frames.
* The minimal recolours — `delete`, `power`, `deletepower` — are
  `0x0E` ×1-2 → `0x0D` → `0x09`, **no `0x0B` at all**, and the keyboard changed colour.

So `0x0E` + `0x09` is a complete vendor-observed path for steady colour, and declining
to implement `0x0B` does not prevent setting colours. (`0x0D` is an opcode `msikb` does
not send; GG used it before the commit in every partial update. Since full frames commit
with `0x09` alone, it is not obviously required.)

## 6. Suggested `protocol.py` change

```diff
-    "numpad": 0x24,
+    "numpad": 0x13,
@@ modifiers
-        101, 224, 225, 226, 227, 228, 229, 230, 240,
+        102, 224, 225, 226, 227, 228, 229, 230, 240,
@@ numpad
-        80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95,
-        96, 97, 98, 99, 0, 0, 0, 0, 0, 0,
+        80, 81, 82,
+        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
```

Verified in a scratch copy: the tables then hold 96 keycodes, `KEYCODE_REGION` has no
duplicates, and the model keymap's region assignment has **0** mismatches against GG.
With the corrected tables plus `gp66-leopard.json`, a dry-run `set --color ff0000`
emits four frames whose count bytes are exactly GG's — `0x2a`, `0x0b`, `0x18`, `0x13` —
addressing 87 keycodes (the 86 named keys plus `fn`).

**Seven tests encoded the GS65 assumptions and failed with this change** — expected,
not a regression. They have since been updated to the measured tables (the list is kept
because it documents which assumptions were load-bearing):

* `test_protocol.test_header_is_repoet_id_then_zero_then_region` — expects `0x24`
* `test_keymaps.test_known_keys` — `kp7`/`kpasterisk`/`kpenter` (MSI 95/85/88) no
  longer have a region; those keys are not on this keyboard
* `test_keymaps.test_every_msi_code_maps_to_a_region` — `reference-gs65` still lists
  83-99, which now have no region
* `test_transport.test_set_commit_writes_four_frames_and_one_commit` — expects 104
  keycodes and four frames
* `test_transport.test_verify_commit_flashes_each_key` — `--only esc,a` reports "2
  key(s) to flash" then fails to build a frame
* `test_capture.test_decode_frame_and_commit` — fixture calls `frame_for(95, ...)`,
  and 95 has no region any more (`KeyError: 95`)
* `test_capture.test_set_is_dry_run_by_default` — the CLI test uses the default
  (reference-gs65) keymap, so `set` stops with *"no region is known for keycode(s)
  83, 84, ... -- wrong keymap for this model?"*; the CLI guard is right, the default
  keymap is what needs to change

The design question the tests expose: region sizes are model-specific (36 on a GS65,
19 here), so a single global `REGION_IDS` cannot be right for both. Either the model
keymap carries its own region table, or the default keymap and the region table have to
move together. `gp66-leopard.json` is written to be the latter: with the corrected
tables, `set` on it emits exactly GG's packet sizes and 87 keys, while the unnamed nine
stay untouched because there is no name to address them by.

## 7. Still needs the hardware

* Whether any write is accepted at all — nothing here proves the Linux path works.
* Names for `50, 102, 135-139, 144, 145` (`msikb verify`).
* Whether the 17 phantom GS65 keycodes were ever harmless padding or a real mismatch.
* Brightness: GG never sent a brightness opcode in any capture, consistent with
  `docs/findings.md` (Fn+F8 is firmware-only).

## Reproducing

The decoder and the comparison scripts live in `analysis/pcap/` of the reverse-engineering
working tree (not shipped here); the fixtures they produced are in
`tests/fixtures/gg/` and `tests/test_gg_capture.py` replays them.

```sh
python analysis/pcap/klc_pcap.py /path/*.pcap                  # decode every command
python analysis/pcap/compare_with_msikb.py <msikb> /path/*.pcap    # GG vs codec + keymap
python analysis/pcap/region_order.py <msikb> /path/*.pcap      # region order check
python analysis/pcap/make_model_keymap.py <msikb> /path/*.pcap --out gp66-leopard.json
```
