# msikb — MSI SteelSeries keyboard lighting on Linux

Goal: **light up the SteelSeries per-key RGB keyboard on an MSI GP66 Leopard under
Linux, without SteelSeries GG.** GG itself is Windows-only and its keyboard control
is not exposed to Linux; the backlight is driven by USB HID packets that we can send
ourselves.

What is in here:

* a decoded implementation of the protocol (`0x0E` colour frames + `0x09` commit),
* a `probe`/`doctor` path that explains *why* a machine does not light up instead of
  just failing to open a device,
* a `verify` sweep that flashes one key at a time so a keymap can be checked by eye,
* `decode`/`learn` tools that turn a SteelSeries GG capture (USBPcap + tshark, on
  Windows) into a keymap for this exact model,
* udev rule and systemd units so the colour comes back at boot and after resume.

Read [docs/findings.md](docs/findings.md) for the state of the public evidence on the
GP66 and [docs/protocol.md](docs/protocol.md) for the packet format with sources.

## Status — read this first

The protocol codec, capture decoder, keymap handling and CLI are implemented and
covered by 100 tests. The wire format and the keymap are **measured from SteelSeries
GG's own traffic on this laptop** (11 USBPcap captures, two full pushes kept in
`tests/fixtures/gg/`), not inherited from another model:

* four region frames — `0x2A`/`0x18`/`0x13`/`0x0B` = 42/24/19/11 records = **96 LEDs**,
  the keyboard's complete keycode set, with no gaps or duplicates,
* `modifiers` carries keycode `102`, and the numpad region is `0x13` with 19 keys
  (`64`–`82`) — the `0x24`/36-key set is a GS65 layout,
* `data/keymaps/gp66-leopard.json` is the measured keymap (86 named keys, `fn`, and the
  nine addresses GG uses that have no Linux name), and it is the default.

`tests/test_gg_capture.py` pins the codec to GG's bytes, so the tables cannot drift
back to the GS65 assumptions.

**What is still unproven:** that a *Linux* write is accepted. This was developed on a
Windows host, so the hidraw/libusb path has been exercised only through a recording
transport. `scripts/first-run.sh` (read-only) and then one `set --commit` are the tests
that matter. Details in `docs/findings.md` and `docs/handoff.md`.

## Quick start (on the laptop, in Linux)

No Python packages are needed for the core path: it talks to `/dev/hidrawN` directly.

```sh
git clone <this repo> msikb && cd msikb

# 1. What is on the bus, and is the KLC reachable?
sh scripts/first-run.sh            # probe + doctor + dry run, writes nothing
python3 -m msikb probe             # the same probe on its own

# 2. See the packets without touching the keyboard (dry run is the default)
python3 -m msikb set --color ff0000
python3 -m msikb key --key esc --color 00ff00 --print-packets

# 3. First real write
sudo python3 -m msikb set --color ff0000 --commit

# 4. udev + systemd: no more sudo, colour re-applied at boot and after resume
sudo sh scripts/install.sh

# 5. Check the keymap against the hardware: lights one key at a time
python3 -m msikb verify --commit --dwell 0.5
```

`--commit` is required for anything to be written; without it every command is a dry
run that prints the exact bytes it would send. `scripts/install.sh` installs the udev
rule, `msikb-apply` and the systemd units (`--no-apply` to skip the test write,
`--uninstall` to undo). Any MSI keycode with no Linux name appears as `msi<N>` and can
still be addressed: `--keys msi50`.

## Commands

| command | what it does |
| ------- | ------------ |
| `probe` | lists hidraw nodes with VID/PID, name, permissions; `--usb` adds raw USB enumeration |
| `doctor` | environment report: python, uid, tshark, pyusb, hidraw nodes, udev rule, verdict + next steps |
| `keys` | prints every key with its name, X11 keycode, MSI keycode and region (`--aliases` for alias expansions) |
| `set` | colours the keyboard: `--color RRGGBB [--keys SPEC]` or `--map file.json`; dry run unless `--commit` |
| `key` | one key: `--key esc --color ff0000` |
| `verify` | flashes each key alone in sequence, so the keymap can be checked visually |
| `decode` | decodes `.pcapng` captures (via tshark) or hex text files into key records |
| `learn` | turns labelled captures into a keymap JSON for this model |
| `capture-help` | prints the Windows capture recipe |

Key specs accept names (`esc`, `home`, `leftmeta`, `fn`, or `msi50` for an LED that has
no Linux name), X11 keycodes as printed by `xev` (`67`), ranges (`67-76`) and the
aliases the older tooling uses (`all`, `f_row`, `arrows`, `num_row`, `characters`).
This keyboard has no numpad, so the `numpad` alias is empty here and says so instead
of quietly doing nothing:

```sh
python3 -m msikb set --color 000000 --keys all --commit            # everything black
python3 -m msikb set --keys 'esc,f1-f4,arrows' --color ff0000 --commit
python3 -m msikb set --map examples/colors.json --commit           # per-key profile
python3 -m msikb set --keys msi50,msi102 --color ff00ff            # the unnamed LEDs
```

## Which device is targeted

Default target is `VID 0x1038 / PID 0x113A` ("SteelSeries KLC"). Override with
`--vid`/`--pid` if your machine differs (`--verify` with `lsusb`), and pick a specific
node with `--device /dev/hidrawN`. If the device has no usable hidraw interface, try
the control-transfer backend:

```sh
pip install pyusb
sudo python3 -m msikb set --color ff0000 --transport libusb --interface 1 --commit
```

Report addressing follows what GG does on this controller. The libusb backend sends
`wValue 0x0300` (frame) and `0x0200` (commit) — report 0, with the opcode in the
payload — and `--report-id` overrides that for other firmware. The hidraw backend uses
the hidapi convention (`buffer[0]` is the report number) and, if the kernel answers
`EINVAL`, retries once with report 0 before giving up; it prints a note when that
happens so the log shows which form this machine accepted.

`--clear-others` also writes black for every other key in the keymap, which makes the
result deterministic on firmware that treats a frame as a full region update.

## Making it stick across boot/suspend

The keyboard controller is not told about a colour after a power cycle or a resume, so
re-apply it. One command does the whole install — udev rule, `msikb-apply`, both
systemd units, enabled at boot and after resume — and then applies the colour once:

```sh
sudo sh scripts/install.sh

# profile: a flat colour, or a per-key JSON map the units read
#   sudo tee /etc/msikb/colors.json <<< '{"a": "00ff00", "default": "000000"}'
sudo systemctl start msikb-lighting.service    # try it without rebooting
```

The units run as root, so their profile is `/etc/msikb/colors.json` (`install.sh` seeds
it from your `~/.config/msikb/colors.json` if that exists). `install.sh` also records the
checkout path in a systemd drop-in (`MSIKB_ROOT`), because systemd does not read a shell
profile. `--no-apply`, `--no-enable` and `--uninstall` control the side effects.
Manual equivalent:

```sh
sudo install -m 0755 scripts/msikb-apply /usr/local/bin/msikb-apply
sudo install -m 0644 systemd/msikb-lighting.service /etc/systemd/system/
sudo install -m 0644 systemd/msikb-lighting-resume.service /etc/systemd/system/
sudo systemctl enable --now msikb-lighting.service msikb-lighting-resume.service
```

`msikb-apply` honours `MSIKB_KEYMAP`, `MSIKB_COLOR`, `MSIKB_CONFIG`, `MSIKB_PYTHON`,
`MSIKB_ROOT` and `MSIKB_ARGS`; see the header of [scripts/msikb-apply](scripts/msikb-apply).

## Learning the keymap from SteelSeries GG (Windows side)

Capturing GG's own packets does two useful things: it proves our byte layout against
the vendor's, and it produces an exact keycode map for this model. On Windows:

```powershell
# elevated PowerShell, Wireshark installed with the USBPcap component
.\scripts\capture-windows.ps1 -List                 # find the USBPcap interface index
.\scripts\capture-windows.ps1 -Interface 4 -Seconds 90 -Output gg-red.pcapng
```

In GG, set exactly one key to a distinctive colour (pure red), apply it, and stop the
capture. Then, back in Linux (or anywhere with tshark):

```sh
tshark -r gg-red.pcapng -Y "usb.setup.bRequest == 0x09" -T fields \
  -E separator=/t -e frame.number -e usb.device_address -e usb.setup.wValue \
  -e usb.setup.wIndex -e usb.capdata -e usb.data_fragment > red.tsv

python3 -m msikb decode red.tsv --keymap gp66-leopard   # what GG actually sent
python3 -m msikb decode baseline.tsv                       # capture with nothing changed
```

Repeat per key, naming each file after the key, then build the model keymap:

```sh
python3 -m msikb learn captures/*.tsv --name gp66-leopard \
    --keymap-name gp66-leopard --output data/keymaps/gp66-leopard.json
sudo python3 -m msikb verify --keymap data/keymaps/gp66-leopard.json --commit
```

`learn` refuses to guess: a capture that lights more than one keycode is reported as
ambiguous rather than silently mis-mapped.

## Safety

* Only the steady-colour path is implemented. The effect packet (`0x0B`) is
  deliberately absent because it once left a keyboard's lighting inoperative with no
  recovery short of hardware resets ([issue #24](https://github.com/Askannz/msi-perkeyrgb/issues/24)).
* There is one public report of `1038:113a` disappearing from `lsusb` after lighting
  experiments on a GP66 Leopard. If that happens, shut down, hold the power button
  ~15 s and drain the battery; that reset is what revives a wedged keyboard
  controller.
* Nothing is written without `--commit`, and each write is limited to the documented
  frames.

## Repository layout

```
msikb/protocol.py    packet codec: 0x0E frames, 0x09 commit, regions, records
msikb/keymaps.py     X11/evdev/MSI keycode mapping, aliases, spec expansion
msikb/transport.py   hidraw (ioctl) and libusb control-transfer backends
msikb/capture.py     tshark/hex decoding, keymap inference
msikb/cli.py         probe, doctor, keys, set, key, verify, decode, learn
data/keymaps/        keymap JSON (gp66-leopard is the default; reference-gs65 kept for other models)
udev/ systemd/ scripts/  integration and diagnostics
docs/protocol.md     wire format with sources
docs/findings.md     GP66 evidence, open questions, experiment order
docs/gg-capture-evidence.md  what SteelSeries GG's own captures prove, byte by byte
docs/handoff.md      state + run order for continuing this on the laptop
tests/               stdlib unittest suite + synthetic and GG-derived capture fixtures
```

Moving the project to the laptop: `sh scripts/make-bundle.sh` writes a 55 KB
`dist/msikb-bundle.tar.gz` (verified self-contained — unpack it, run
`sh scripts/first-run.sh`), or push this repo and clone it. If you are picking this up
in a fresh session there, start with [docs/handoff.md](docs/handoff.md).

## Tests

```sh
python3 -m unittest discover -s tests -t .   # 100 tests, no dependencies
python3 tests/fixtures/make_fixtures.py      # regenerate the synthetic captures
```

The suite covers the packet codec (`tests/test_protocol.py`), keymap/alias handling
(`tests/test_keymaps.py`), capture decoding and `learn` (`tests/test_capture.py`), the
real send path with a recording transport (`tests/test_transport.py`) and — the
important one — `tests/test_gg_capture.py`, which replays SteelSeries GG's actual
bytes from this laptop and fails if the region tables, keycodes or encoder drift.

The `tests/fixtures/learn` files are built with our own codec, so they exercise the
pipeline (hex/pcap text -> decode -> keymap) but prove nothing about the vendor;
`tests/fixtures/gg` is the vendor traffic.

## Credits and license

Protocol work by [Askannz](https://github.com/Askannz/msi-perkeyrgb) and TauAkiou
(effect documentation); the reference keymap shipped in `data/keymaps/reference-gs65.json`
is copied from that project, which is MIT licensed like this one — see [LICENSE](LICENSE).
`msikb` is unaffiliated with MSI and SteelSeries.
