"""Decode captured SteelSeries GG traffic and rebuild keymaps from it.

Two input paths:

* ``.pcap``/``.pcapng`` -- shell out to ``tshark`` (ships with Wireshark) and pull
  the HID SET_REPORT control transfers out of the trace.
* plain text -- hex dumps, one packet per line (``0e 00 2a ...``), including
  lines labelled ``<keyname>: 0e 00 ...``, which is what ``learn`` wants.

The parser is deliberately tolerant: unknown reports are reported, not fatal.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from . import protocol
from .keymaps import keymap_from_msi_mapping

SET_REPORT = 0x09
DEFAULT_FILTER = "usb.setup.bRequest == 0x09"
TSHARK_FIELDS = (
    "frame.number",
    "usb.device_address",
    "usb.setup.wValue",
    "usb.setup.wIndex",
    "usb.capdata",
    "usb.data_fragment",
)

HEX_LINE = re.compile(r"^(?:(?P<label>[A-Za-z0-9_\-\.]+)\s*[:=]\s*)?(?P<hex>(?:[0-9a-fA-F]{2}[\s:]?)+)\s*$")
HEX_BYTE = re.compile(r"[0-9a-fA-F]{2}")


class CaptureError(RuntimeError):
    pass


@dataclass
class CapturePacket:
    source: str
    frame: Optional[int]
    device_address: Optional[int]
    wvalue: Optional[int]
    windex: Optional[int]
    payload: bytes
    label: Optional[str] = None

    @property
    def report_id(self) -> Optional[int]:
        return self.payload[0] if self.payload else None

    @property
    def report_type(self) -> Optional[int]:
        if self.wvalue is None:
            return None
        return (self.wvalue >> 8) & 0xFF

    @property
    def report_id_from_setup(self) -> Optional[int]:
        return None if self.wvalue is None else self.wvalue & 0xFF

    def describe(self) -> str:
        parts = ["%s" % (self.source,)]
        if self.frame is not None:
            parts.append("frame %s" % self.frame)
        if self.device_address is not None:
            parts.append("dev %s" % self.device_address)
        if self.wvalue is not None:
            parts.append(
                "wValue 0x%04x (type %s, report 0x%02x)"
                % (self.wvalue, self.report_type, self.report_id_from_setup)
            )
        if self.windex is not None:
            parts.append("wIndex %s" % self.windex)
        parts.append("%d bytes" % len(self.payload))
        if self.label:
            parts.insert(0, "[%s]" % self.label)
        return "  ".join(parts)


def _to_int(text: str) -> Optional[int]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return int(text, 0)
    except ValueError:
        try:
            return int(text, 16)
        except ValueError:
            return None


def _hex_to_bytes(text: str) -> bytes:
    cleaned = re.sub(r"[^0-9a-fA-F]", "", text or "")
    if len(cleaned) % 2:
        cleaned = cleaned[:-1]
    return bytes.fromhex(cleaned)


def parse_tsv(text: str, source: str = "<tsv>") -> List[CapturePacket]:
    """Parse ``tshark -T fields`` output ordered as in :data:`TSHARK_FIELDS`."""
    packets: List[CapturePacket] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        fields += [""] * (len(TSHARK_FIELDS) - len(fields))
        frame, address, wvalue, windex, capdata, fragment = fields[:6]
        payload = _hex_to_bytes(capdata) or _hex_to_bytes(fragment)
        if not payload:
            continue
        packets.append(
            CapturePacket(
                source=source,
                frame=_to_int(frame),
                device_address=_to_int(address),
                wvalue=_to_int(wvalue),
                windex=_to_int(windex),
                payload=payload,
            )
        )
    return packets


def parse_hex_text(text: str, source: str = "<text>") -> List[CapturePacket]:
    packets: List[CapturePacket] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = HEX_LINE.match(stripped)
        if not match:
            raise CaptureError(
                "%s line %d: cannot parse as hex bytes: %r" % (source, lineno, line[:80])
            )
        payload = _hex_to_bytes(match.group("hex"))
        if not payload:
            continue
        packets.append(
            CapturePacket(
                source=source,
                frame=None,
                device_address=None,
                wvalue=None,
                windex=None,
                payload=payload,
                label=match.group("label"),
            )
        )
    return packets


def tshark_path() -> Optional[str]:
    return shutil.which("tshark")


def extract_pcap(path: Path, display_filter: str = DEFAULT_FILTER,
                 tshark: Optional[str] = None) -> List[CapturePacket]:
    """Run tshark over a capture file and return the SET_REPORT payloads."""
    binary = tshark or tshark_path()
    if not binary:
        raise CaptureError(
            "tshark was not found on PATH. Install Wireshark/tshark, or export the "
            "fields manually and use `msikb decode --hex`."
        )
    args = [
        binary, "-r", str(path),
        "-Y", display_filter,
        "-T", "fields",
        "-E", "separator=/t",
    ]
    for field in TSHARK_FIELDS:
        args += ["-e", field]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CaptureError(
            "tshark failed on %s (exit %d): %s"
            % (path, proc.returncode, (proc.stderr or proc.stdout).strip()[:500])
        )
    return parse_tsv(proc.stdout, source=str(path))


def packets_from_file(path: Path, display_filter: str = DEFAULT_FILTER) -> List[CapturePacket]:
    suffix = path.suffix.lower()
    if suffix in (".pcap", ".pcapng", ".cap"):
        return extract_pcap(path, display_filter=display_filter)
    return parse_hex_text(path.read_text(encoding="utf-8", errors="replace"), source=str(path))


@dataclass
class DecodedPacket:
    packet: CapturePacket
    kind: str                     # "frame" | "commit" | "unknown"
    frame: Optional[protocol.Frame] = None
    error: Optional[str] = None


def decode_packet(packet: CapturePacket) -> DecodedPacket:
    try:
        parsed = protocol.parse_packet(packet.payload)
    except protocol.ProtocolError as exc:
        return DecodedPacket(packet=packet, kind="unknown", error=str(exc))
    if isinstance(parsed, protocol.Frame):
        return DecodedPacket(packet=packet, kind="frame", frame=parsed)
    return DecodedPacket(packet=packet, kind="commit")


def decode_packets(packets: Iterable[CapturePacket]) -> List[DecodedPacket]:
    return [decode_packet(packet) for packet in packets]


def lit_colors(decoded: Iterable[DecodedPacket]) -> Dict[int, Tuple[int, int, int]]:
    """Merge every lit (non-black) key record into ``{msi keycode: rgb}``."""
    colors: Dict[int, Tuple[int, int, int]] = {}
    for item in decoded:
        if item.frame is None:
            continue
        colors.update(item.frame.lit_colors())
    return colors


def infer_keymap_from_captures(
    captures: Dict[str, List[CapturePacket]],
    color_filter: Optional[Tuple[int, int, int]] = None,
) -> Tuple[Dict[str, int], List[str]]:
    """Turn ``{key name: packets}`` into ``{key name: MSI keycode}``.

    Each named capture should be a single GG change of one key to a distinctive
    colour. A capture that lights more than one keycode is reported as ambiguous
    rather than guessed at. Returns ``(mapping, problems)``.
    """
    mapping: Dict[str, int] = {}
    problems: List[str] = []
    for name in sorted(captures):
        packets = captures[name]
        decoded = decode_packets(packets)
        frames = [item.frame for item in decoded if item.frame is not None]
        if not frames:
            problems.append("%s: no 0x0E key frame found (%d packets decoded)"
                            % (name, len(packets)))
            continue
        colors = lit_colors(decoded)
        if color_filter is not None:
            colors = {code: rgb for code, rgb in colors.items() if rgb == color_filter}
        if not colors:
            problems.append("%s: no lit key record matched" % name)
            continue
        if len(colors) > 1:
            problems.append(
                "%s: ambiguous, %d keycodes lit (%s) -- capture one key at a time, "
                "or pass --color"
                % (name, len(colors), ", ".join(str(c) for c in sorted(colors)))
            )
            continue
        msi_code = next(iter(colors))
        if msi_code in mapping.values():
            previous = next(k for k, v in mapping.items() if v == msi_code)
            problems.append(
                "%s: MSI keycode %d is already mapped to %r -- duplicate capture?"
                % (name, msi_code, previous)
            )
            continue
        mapping[name] = msi_code
    return mapping, problems


def write_keymap(mapping: Dict[str, int], output: Path, name: str,
                 source_note: str = "") -> None:
    keymap = keymap_from_msi_mapping(
        name=name,
        name_to_msi=mapping,
        source=source_note or "captured from SteelSeries GG with msikb learn",
        notes="Generated by msikb learn; verify with `msikb keys --keymap %s`." % name,
    )
    output.write_text(json.dumps(keymap.to_json(), indent=2) + "\n", encoding="utf-8")
