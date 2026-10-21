"""Command line interface: probe, set, keys, decode, learn, doctor."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import __version__, protocol
from .capture import (
    CaptureError,
    decode_packets,
    infer_keymap_from_captures,
    packets_from_file,
    parse_hex_text,
    tshark_path,
    write_keymap,
)
from .keymaps import Keymap, KeymapError, default_keymap
from .transport import (
    DEFAULT_DELAY,
    HidrawDevice,
    HidrawTransport,
    LibusbTransport,
    NullTransport,
    TransportError,
    find_hidraw,
    list_hidraw,
    probe_node,
    sysfs_vendor_product,
)

DEFAULT_VID = 0x1038
DEFAULT_PID = 0x113A
KNOWN_KLC_PIDS = (0x113A, 0x1122)
CACHE_PATH = Path.home() / ".cache" / "msikb" / "device"

CAPTURE_RECIPE = """\
Capture SteelSeries GG traffic on Windows (ground truth for this machine)
------------------------------------------------------------------------
1. Install Wireshark with the USBPcap component and reboot. Windows 11 is not
   listed as supported by USBPcap; if it will not install, use Bus Hound.
2. Find which USBPcap interface carries the keyboard: take a 10 s capture while
   changing a colour in GG, then look for the device with vendor id 1038.
3. Take a fresh capture, then in GG change ONE key to a distinctive colour
   (e.g. pure red) and apply it. Save as pcapng.
4. Reduce it to the HID SET_REPORT payloads:

     tshark -r gg-red.pcapng -Y "usb.setup.bRequest == 0x09" -T fields \\
       -E separator=/t -e frame.number -e usb.device_address \\
       -e usb.setup.wValue -e usb.setup.wIndex -e usb.capdata \\
       -e usb.data_fragment > red.tsv

   A HID feature report shows up as wValue 0x0300|report_id (GG uses 0x030e,
   524 bytes) and the frame commit as 0x0200|0x09 (64 bytes).
5. Decode and compare:

     msikb decode red.tsv                 # what GG actually sent
     msikb decode gg-baseline.tsv         # capture with no key changed

6. Repeat step 3-5 for every key you care about, naming each capture after the
   key (esc.pcapng, a.pcapng, f1.pcapng, ...), then:

     msikb learn captures/*.tsv --name gp66-leopard --output gp66-leopard.json

For a sanity check of the whole pipeline without hardware, run:
     msikb learn tests/fixtures/learn --name demo --output /tmp/demo.json
"""


# --------------------------------------------------------------------- helpers
def parse_hex_color(text: str) -> Tuple[int, int, int]:
    cleaned = text.strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", cleaned):
        raise KeymapError("invalid colour %r (expected six hex digits, e.g. ff0000)" % text)
    return (int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16))


def parse_usb_id(text: str) -> int:
    try:
        return int(text, 16)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a hex USB id, e.g. 1038 or 113a")


def load_color_map(path: Path, keymap: Keymap) -> Dict[int, Tuple[int, int, int]]:
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise KeymapError("%s must contain a JSON object of {key: RRGGBB}" % path)
    colors: Dict[int, Tuple[int, int, int]] = {}
    for key_spec, color in raw.items():
        if key_spec in ("default", "notes") or key_spec.startswith("_"):
            continue
        rgb = parse_hex_color(str(color))
        for code in keymap.codes_for_spec(str(key_spec)):
            colors[code] = rgb
    if "default" in raw:
        rgb = parse_hex_color(str(raw["default"]))
        for code in keymap.codes_for_spec("all"):
            colors.setdefault(code, rgb)
    return colors


def describe_colors(colors: Dict[int, Tuple[int, int, int]], keymap: Optional[Keymap],
                    limit: int = 12) -> List[str]:
    lines: List[str] = []
    for code in sorted(colors):
        rgb = colors[code]
        name = keymap.name_for_msi(code) if keymap else "?"
        region = protocol.KEYCODE_REGION.get(code, "unmapped")
        lines.append(
            "  %-14s msi=%-3d x11=%-4s %02x%02x%02x  region=%s"
            % (
                name,
                code,
                (keymap.x11_for_msi(code) if keymap else None) or "-",
                rgb[0],
                rgb[1],
                rgb[2],
                region,
            )
        )
    if len(lines) > limit:
        lines = lines[:limit] + ["  ... and %d more" % (len(colors) - limit)]
    return lines


def describe_trailer(frame: protocol.Frame) -> str:
    """Name the 16-byte tail: ours, the all-zero tail GG sends, or something new."""
    if frame.trailer_ok:
        return "ok"
    if frame.trailer == bytes(protocol.TRAILER_LEN):
        return "zeros (GG pads this)"
    return "MISMATCH"


def hexdump(data: bytes, width: int = 32, max_bytes: int = 96) -> str:
    chunk = data[:max_bytes]
    lines = []
    for offset in range(0, len(chunk), width):
        row = chunk[offset : offset + width]
        lines.append("%04x  %s" % (offset, " ".join("%02x" % b for b in row)))
    if len(data) > max_bytes:
        lines.append("...  (%d bytes total)" % len(data))
    return "\n".join(lines)


def open_hidraw_transport(args, candidates: Sequence[HidrawDevice], delay: float):
    errors: List[str] = []
    for device in candidates:
        if not device.writable():
            errors.append(
                "%s: not writable by uid %s (install udev/99-msi-steelseries-rgb.rules)"
                % (device.path, getattr(os, "geteuid", lambda: "?")())
            )
            continue
        try:
            return HidrawTransport(device, delay=delay)
        except OSError as exc:
            errors.append("%s: %s" % (device.path, exc))
    raise TransportError(
        "could not open a hidraw node." + ("\n  " + "\n  ".join(errors) if errors else "")
    )


def transport_candidates(args) -> List[HidrawDevice]:
    if args.device:
        probed = probe_node(args.device)
        if probed is not None:
            return [probed]
        return [HidrawDevice(path=args.device, bustype=0, vid=args.vid, pid=args.pid, name="")]
    candidates: List[HidrawDevice] = []
    if not getattr(args, "no_cache", False) and CACHE_PATH.is_file():
        cached = probe_node(CACHE_PATH.read_text(encoding="utf-8").strip())
        if cached is not None:
            candidates.append(cached)
    for device in find_hidraw(args.vid, args.pid):
        if device not in candidates:
            candidates.append(device)
    return candidates


def open_transport(args, delay: float):
    if args.dry_run or args.transport == "dry-run":
        return NullTransport()
    if args.transport == "libusb":
        return LibusbTransport(
            args.vid, args.pid, args.interface, delay=delay,
            report_id=getattr(args, "report_id", 0),
        )
    candidates = transport_candidates(args)
    if not candidates:
        raise TransportError(
            "no hidraw device found for %04x:%04x.\n"
            "  - run `msikb probe` to see every HID device\n"
            "  - check `lsusb` for the SteelSeries KLC (BIOS Fast Boot can hide it)\n"
            "  - try `--transport libusb` if the interface has no hidraw node"
            % (args.vid, args.pid)
        )
    transport = open_hidraw_transport(args, candidates, delay)
    if not args.dry_run:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(transport.device.path, encoding="utf-8")
    return transport


def send_frames(colors: Dict[int, Tuple[int, int, int]], args, keymap: Optional[Keymap] = None) -> int:
    regions = args.region or None
    packets = protocol.build_frame_packets(colors, regions)
    commit = protocol.build_commit()

    print("Preparing %d region frame(s) + 1 commit for %d key(s)" % (len(packets), len(colors)))
    for region, packet in packets:
        count = len(protocol.chunk_by_region(colors, [region])[0].colors)
        print("  %-9s %d bytes, %d key record(s)" % (region, len(packet), count))
    print("  commit    %d bytes" % len(commit))

    if args.show_packets:
        for region, packet in packets:
            print("\n--- %s (%d bytes) ---" % (region, len(packet)))
            print(hexdump(packet))
        print("\n--- commit (%d bytes) ---" % len(commit))
        print(hexdump(commit))

    if args.dry_run:
        print("\nDry run: nothing was written. Re-run with --commit to talk to the keyboard.")
        return 0

    delay = args.delay
    with open_transport(args, delay) as transport:
        print("\nUsing %s" % transport.describe())
        for region, packet in packets:
            transport.send_feature_report(packet)
            print("  sent %s frame" % region)
        transport.send_output_report(commit)
        print("  sent commit -- lighting updated")
    return 0


# -------------------------------------------------------------------- commands
def cmd_probe(args) -> int:
    print("msikb %s -- device probe" % __version__)
    print("platform: %s %s" % (platform.system(), platform.release()))
    if platform.system() != "Linux":
        print(
            "\nNot Linux: hidraw probing is only meaningful on the target OS.\n"
            "On this host you can still decode captures (msikb decode) and build keymaps."
        )
    devices = list_hidraw()
    if not devices:
        print("\nNo /dev/hidraw* nodes found.")
    else:
        print("\n%d hidraw node(s):" % len(devices))
        for device in devices:
            marks = []
            if device.vid == DEFAULT_VID:
                marks.append("SteelSeries")
            if (device.vid, device.pid) in [(DEFAULT_VID, p) for p in KNOWN_KLC_PIDS]:
                marks.append("KNOWN KLC id")
            print("  %s  %s" % (device, " ".join(marks)))
    matches = find_hidraw(args.vid, args.pid)
    print("\nTarget %04x:%04x -> %d match(es)" % (args.vid, args.pid, len(matches)))
    for device in matches:
        print("  %s" % device)
        print("    sysfs id: %s" % (sysfs_vendor_product(device.path) or "?"))
        descriptor = Path("/sys/class/hidraw") / Path(device.path).name / "device" / "report_descriptor"
        if descriptor.is_file():
            print("    report descriptor: %s (%d bytes)" % (descriptor, descriptor.stat().st_size))
    if args.usb:
        try:
            import usb.core  # type: ignore

            print("\nUSB devices with vendor 0x%04x:" % DEFAULT_VID)
            for dev in usb.core.find(find_all=True, idVendor=DEFAULT_VID):
                print("  %04x:%04x" % (dev.idVendor, dev.idProduct))
        except ImportError:
            print("\n(pyusb not installed; skipping raw USB enumeration)")
    if not matches:
        print(
            "\nNext steps:\n"
            "  - `lsusb | grep -i steel` to confirm the KLC is enumerated at all\n"
            "  - disable BIOS Fast Boot if the device is missing entirely\n"
            "  - run `msikb doctor` for a full environment report"
        )
    return 0


def cmd_keys(args) -> int:
    keymap = Keymap.by_name(args.keymap)
    entries = keymap.entries()
    named = [e for e in entries if e.x11 >= 0]
    print(
        "keymap: %s  (%d LEDs: %d named, %d unnamed, fn)"
        % (keymap.name, len(entries) + 1, len(named), len(entries) - len(named))
    )
    if keymap.notes:
        print("notes:  %s" % keymap.notes)
    print("%-14s %-5s %-5s %s" % ("name", "x11", "msi", "region"))
    for entry in sorted(entries + [keymap.entry("fn")], key=lambda e: (e.region or "~", e.x11 < 0, e.x11)):
        x11 = str(entry.x11) if entry.x11 >= 0 else "-"
        print("%-14s %-5s %-5d %s" % (entry.name, x11, entry.msi, entry.region or "UNMAPPED"))
    unmapped = keymap.unmapped_msi_codes()
    if unmapped:
        print("\nWarning: %d MSI keycode(s) in this map belong to no known region: %s"
              % (len(unmapped), ", ".join(str(c) for c in unmapped)))
    if args.aliases:
        from .keymaps import ALIASES

        print("\naliases:")
        for alias, names in ALIASES.items():
            try:
                codes = keymap.codes_for_spec(alias)
            except KeymapError:
                print("  %-11s --    none of these keys exist on this model" % alias)
                continue
            print("  %-11s %2d keys  (x11 keycodes: %s)"
                  % (alias, len(codes),
                     ",".join(str(keymap.x11_for_msi(c) if (keymap.x11_for_msi(c) or -1) >= 0 else "-") for c in codes[:6]) + ("..." if len(codes) > 6 else "")))
    return 0


def cmd_set(args) -> int:
    keymap = Keymap.by_name(args.keymap)
    colors: Dict[int, Tuple[int, int, int]] = {}

    map_path = getattr(args, "map", None)
    if map_path:
        colors.update(load_color_map(Path(map_path), keymap))
    if args.color:
        rgb = parse_hex_color(args.color)
        spec = args.keys or "all"
        for code in keymap.codes_for_spec(spec):
            colors[code] = rgb
    if not colors:
        print("Nothing to do: pass --color (optionally with --keys) or --map.", file=sys.stderr)
        return 2

    if args.clear_others:
        for code in keymap.codes_for_spec("all"):
            colors.setdefault(code, (0, 0, 0))

    print("keymap %s -> %d key(s)" % (keymap.name, len(colors)))
    for line in describe_colors(colors, keymap):
        print(line)
    print()
    return send_frames(colors, args, keymap)


def cmd_verify(args) -> int:
    """Light one key at a time so the keymap can be checked against the real keyboard."""
    keymap = Keymap.by_name(args.keymap)
    entries = sorted(keymap.entries(), key=lambda e: (e.region or "~", e.x11))
    if args.only:
        wanted = {n.strip().lower() for n in args.only.split(",")}
        entries = [e for e in entries if e.name in wanted]
    if args.start:
        names = [e.name for e in entries]
        if args.start not in names:
            print("unknown key %r" % args.start, file=sys.stderr)
            return 2
        entries = entries[names.index(args.start) :]
    if not entries:
        print("nothing to verify", file=sys.stderr)
        return 2

    rgb = parse_hex_color(args.color)
    all_codes = keymap.codes_for_spec("all")
    print("%d key(s) to flash, %.2fs each" % (len(entries), args.dwell))
    if args.dry_run:
        print("Dry run: nothing was written. Re-run with --commit to flash the keys.")
        return 0

    with open_transport(args, args.delay) as transport:
        print("Using %s" % transport.describe())
        for index, entry in enumerate(entries, start=1):
            colors = {code: (0, 0, 0) for code in all_codes}
            colors[entry.msi] = rgb
            for _region, packet in protocol.build_frame_packets(colors, None):
                transport.send_feature_report(packet)
            transport.send_output_report(protocol.build_commit())
            print("  [%3d/%3d] %-14s msi=%-3d x11=%-4s region=%s"
                  % (index, len(entries), entry.name, entry.msi, entry.x11, entry.region))
            time.sleep(args.dwell)
    print("\nSweep finished. Any key that never lit is either not addressable on this "
          "model or has a different keycode -- capture it with `msikb learn`.")
    return 0


def cmd_learn(args) -> int:
    if not args.captures:
        print("Pass one capture per key (file name becomes the key name).", file=sys.stderr)
        return 2
    captures: Dict[str, list] = {}
    for raw in args.captures:
        path = Path(raw)
        name = args.name if (args.name and len(args.captures) == 1) else path.stem
        packets = packets_from_file(path, display_filter=args.filter)
        captures.setdefault(name, []).extend(packets)
        print("read %-28s %3d packet(s)" % (path.name, len(packets)))

    color_filter = parse_hex_color(args.color) if args.color else None
    mapping, problems = infer_keymap_from_captures(captures, color_filter=color_filter)

    print("\ninferred %d key(s):" % len(mapping))
    for name in sorted(mapping, key=lambda n: mapping[n]):
        code = mapping[name]
        region = protocol.KEYCODE_REGION.get(code, "UNMAPPED")
        print("  %-14s msi=%-3d region=%s" % (name, code, region))

    if problems:
        print("\nproblems:")
        for problem in problems:
            print("  - %s" % problem)

    if args.output:
        write_keymap(mapping, Path(args.output), name=args.keymap_name)
        print("\nwrote %s (use with --keymap %s)" % (args.output, args.output))
    else:
        print("\n(dry run: pass --output FILE to write a keymap json)")
    return 0 if mapping else 1


def cmd_decode(args) -> int:
    keymap: Optional[Keymap] = None
    if args.keymap:
        keymap = Keymap.by_name(args.keymap)

    packets = []
    if args.hex:
        packets.extend(parse_hex_text(Path(args.hex).read_text(encoding="utf-8", errors="replace"), source=args.hex))
    for raw in args.captures:
        packets.extend(packets_from_file(Path(raw), display_filter=args.filter))
    if not packets:
        print("Nothing decoded. Pass capture files or --hex FILE.", file=sys.stderr)
        return 2

    summary: Dict[str, int] = {}
    for item in decode_packets(packets):
        if item.error:
            summary["unknown"] = summary.get("unknown", 0) + 1
            if args.verbose:
                print("%s  -> unparsed: %s" % (item.packet.describe(), item.error))
            continue
        summary[item.kind] = summary.get(item.kind, 0) + 1
        if item.kind == "commit":
            print("%s  -> commit/apply" % item.packet.describe())
            continue
        frame = item.frame
        assert frame is not None
        print("%s  -> region %s (id 0x%02x), trailer %s"
              % (item.packet.describe(), frame.region or "?", frame.region_id,
                 describe_trailer(frame)))
        for record in frame.records:
            if record.is_empty and not args.all_records:
                continue
            name = keymap.name_for_msi(record.keycode) if (keymap and record.keycode) else "?"
            x11 = keymap.x11_for_msi(record.keycode) if keymap else None
            print(
                "     %2d: keycode=%-3d %-14s x11=%-4s rgb=%s effect=0x%02x mode=0x%02x"
                % (record.index, record.keycode, name, x11 if x11 else "-", record.hex_rgb,
                   record.effect_id, record.mode)
            )
        unknown = frame.unknown_keycodes()
        if unknown:
            print("     note: %d keycode(s) not in any known region: %s"
                  % (len(unknown), ", ".join(str(c) for c in unknown)))
    print("\nsummary: %s" % ", ".join("%s=%d" % (k, v) for k, v in sorted(summary.items())))
    return 0


def cmd_doctor(args) -> int:
    print("msikb %s doctor" % __version__)
    print("python      : %s (%s)" % (platform.python_version(), sys.executable))
    print("platform    : %s %s %s" % (platform.system(), platform.release(), platform.machine()))
    print("tshark      : %s" % (tshark_path() or "not found (needed to decode .pcapng)"))
    uid = getattr(os, "geteuid", lambda: None)()
    print("euid        : %s%s" % (uid, " (root)" if uid == 0 else ""))
    try:
        import usb.core  # type: ignore  # noqa: F401

        print("pyusb       : available")
    except ImportError:
        print("pyusb       : not installed (only needed for --transport libusb)")

    devices = list_hidraw()
    print("hidraw      : %d node(s)" % len(devices))
    for device in devices:
        print("   %s" % device)

    target = find_hidraw(args.vid, args.pid)
    print("target      : %04x:%04x -> %d match(es)" % (args.vid, args.pid, len(target)))

    rule = Path("/etc/udev/rules.d/99-msi-steelseries-rgb.rules")
    print("udev rule   : %s" % (rule if rule.is_file() else "not installed"))

    problems: List[str] = []
    if platform.system() != "Linux":
        problems.append("not running on Linux; nothing here can reach the keyboard")
    if not devices:
        problems.append("no hidraw nodes at all -- check the kernel and USB enumeration")
    if not target:
        problems.append(
            "no hidraw node for %04x:%04x. Check `lsusb`; disable BIOS Fast Boot; if the "
            "device vanished after a lighting experiment, power-cycle the laptop "
            "(and drain the battery) to reset the keyboard controller." % (args.vid, args.pid)
        )
    if target and not any(d.writable() for d in target):
        problems.append("target node exists but is not writable: install the udev rule or run as root")
    if not protocol.DUPLICATE_KEYCODES:
        pass
    else:
        problems.append("keymap data has duplicate MSI keycodes: %s" % protocol.DUPLICATE_KEYCODES)

    print("\nverdict: %s" % ("OK" if not problems else "needs attention"))
    for problem in problems:
        print("  - %s" % problem)
    return 0 if not problems else 1


def cmd_capture_help(args) -> int:
    print(CAPTURE_RECIPE)
    return 0


# ----------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="msikb",
        description="Reverse-engineered control for MSI SteelSeries per-key RGB keyboards "
                    "(GP66/GE66 family, USB 1038:113a).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=CAPTURE_RECIPE,
    )
    parser.add_argument("--version", action="version", version="msikb %s" % __version__)
    parser.add_argument("--vid", type=parse_usb_id, default=DEFAULT_VID,
                        help="USB vendor id (default 1038)")
    parser.add_argument("--pid", type=parse_usb_id, default=DEFAULT_PID,
                        help="USB product id (default 113a)")
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="list HID/USB devices and find the KLC")
    probe.add_argument("--usb", action="store_true", help="also enumerate USB devices via pyusb")
    probe.set_defaults(func=cmd_probe)

    keys = sub.add_parser("keys", help="list the keys in a keymap")
    keys.add_argument("--keymap", default="gp66-leopard")
    keys.add_argument("--aliases", action="store_true", help="show alias expansions")
    keys.set_defaults(func=cmd_keys)

    def add_send_options(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--keymap", default="gp66-leopard",
                        help="keymap name or path (default gp66-leopard)")
        sp.add_argument("--region", action="append", choices=list(protocol.REGIONS),
                        help="restrict to a region packet (default: all needed)")
        sp.add_argument("--device", help="explicit /dev/hidrawN to use")
        sp.add_argument("--transport", choices=("hidraw", "libusb", "dry-run"), default="hidraw")
        sp.add_argument("--interface", type=int, default=None,
                        help="USB interface for --transport libusb")
        sp.add_argument("--report-id", type=lambda v: int(v, 0), default=0,
                        help="HID report number for --transport libusb (default 0, which is "
                             "what SteelSeries GG uses on this controller)")
        sp.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help="seconds between reports (default %.3f)" % DEFAULT_DELAY)
        sp.add_argument("--no-cache", action="store_true", help="ignore the cached hidraw path")
        sp.add_argument("--commit", dest="commit", action="store_true",
                        help="actually write to the keyboard (default is dry run)")
        sp.add_argument("--dry-run", dest="dry_run", action="store_true", default=False,
                        help=argparse.SUPPRESS)
        sp.add_argument("--print-packets", dest="show_packets", action="store_true",
                        help="hexdump the packets that will be sent")

    set_parser = sub.add_parser("set", help="set colours (dry run unless --commit)")
    set_parser.add_argument("--color", help="HTML colour applied to --keys (default: all keys)")
    set_parser.add_argument("--keys", help="key spec: names, comma list, ranges or aliases")
    set_parser.add_argument("--map", help="JSON file of {key: RRGGBB} (optional 'default')")
    set_parser.add_argument("--clear-others", action="store_true",
                            help="also send black for every other key in the keymap")
    add_send_options(set_parser)
    set_parser.set_defaults(func=cmd_set)

    key_parser = sub.add_parser("key", help="set one key (dry run unless --commit)")
    key_parser.add_argument("--key", required=True, dest="keys",
                            help="key name, X11 keycode, range or alias")
    key_parser.add_argument("--color", required=True, help="HTML colour, e.g. ff0000")
    key_parser.add_argument("--clear-others", action="store_true")
    add_send_options(key_parser)
    key_parser.set_defaults(func=cmd_set)

    decode = sub.add_parser("decode", help="decode captured 0x0E/0x09 packets")
    decode.add_argument("captures", nargs="*", help=".pcapng captures and/or hex text files")
    decode.add_argument("--hex", help="a text file of hex packets, one per line")
    decode.add_argument("--keymap", help="resolve keycodes to key names")
    decode.add_argument("--filter", default="usb.setup.bRequest == 0x09",
                        help="tshark display filter for .pcapng input")
    decode.add_argument("--all-records", action="store_true",
                        help="print empty records too")
    decode.add_argument("-v", "--verbose", action="store_true")
    decode.set_defaults(func=cmd_decode)

    verify = sub.add_parser("verify", help="flash keys one at a time to check a keymap")
    add_send_options(verify)
    verify.add_argument("--color", default="ff0000", help="colour used for each test (default ff0000)")
    verify.add_argument("--dwell", type=float, default=0.6, help="seconds to hold each key")
    verify.add_argument("--only", help="comma-separated subset of key names")
    verify.add_argument("--start", help="resume the sweep at this key name")
    verify.set_defaults(func=cmd_verify)

    learn = sub.add_parser("learn", help="build a keymap from labelled captures")
    learn.add_argument("captures", nargs="*", help="one capture per key; file name = key name")
    learn.add_argument("--name", help="key name when a single capture is given")
    learn.add_argument("--keymap-name", default="captured", help="name to store in the JSON")
    learn.add_argument("--color", help="only accept records with this RRGGBB colour")
    learn.add_argument("--filter", default="usb.setup.bRequest == 0x09")
    learn.add_argument("--output", help="write the keymap JSON here")
    learn.set_defaults(func=cmd_learn)

    doctor = sub.add_parser("doctor", help="environment and permission diagnosis")
    doctor.set_defaults(func=cmd_doctor)

    capture = sub.add_parser("capture-help", help="how to capture GG traffic on Windows")
    capture.set_defaults(func=cmd_capture_help)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "commit", False):
        args.dry_run = False
    else:
        args.dry_run = True
    try:
        return args.func(args)
    except (TransportError, KeymapError, CaptureError, protocol.ProtocolError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
