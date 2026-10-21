"""Byte-level codec for the MSI SteelSeries per-key RGB keyboard protocol.

Everything here was reconstructed from traffic SteelSeries Engine / SteelSeries GG
sends to MSI laptops on Windows, as documented by:

  * Askannz/msi-perkeyrgb  -- working "steady colour" implementation (msiprotocol.py)
  * TauAkiou                -- "MSI-Steelseries RGB Keyboard Effects Protocol v0.2"

The controller is driven with two HID reports:

  * report id 0x0E -- 524-byte *feature* report carrying up to 42 key records per
    region. One feature report is sent per region that is being changed.
  * report id 0x09 -- 64-byte *output* report that commits/applies the frame.

Only the steady-colour path is implemented. Effect packets (0x0B) are deliberately
not implemented: msi-perkeyrgb disabled them after they bricked a keyboard
controller (Askannz/msi-perkeyrgb issue #24).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

NB_KEYS = 42
RECORD_LEN = 12
HEADER_LEN = 4
TRAILER_LEN = 16
FRAME_LEN = HEADER_LEN + NB_KEYS * RECORD_LEN + TRAILER_LEN  # 524
COMMIT_LEN = 64

FRAME_REPORT_ID = 0x0E
COMMIT_REPORT_ID = 0x09

# Last 16 bytes of every 0x0E frame. Constant in all captures we have seen; the
# two trailing bytes look like a magic/checksum but no variation has ever been
# observed, so they are sent literally.
FRAME_TRAILER = bytes([0x00] * 14 + [0x08, 0x39])

# Bytes 9 and 10 of a key record in the steady-colour path.
STATIC_EFFECT_ID = 0x01
STATIC_MODE = 0x00

# Byte 2 of a 0x0E frame is the number of key records in that frame, which is the
# number of keys the region has *on this model*. Measured from SteelSeries GG on a
# GP66 Leopard 11UG: 0x2A/0x18/0x13/0x0B = 42/24/19/11 records = 96 keys. The
# GS65-derived table had 0x24 (36) for the fourth region, which the 113A firmware
# here does not use.
REGION_IDS: Dict[str, int] = {
    "alphanum": 0x2A,
    "enter": 0x0B,
    "modifiers": 0x18,
    "numpad": 0x13,
}
REGIONS: Tuple[str, ...] = ("alphanum", "enter", "modifiers", "numpad")

# MSI lighting keycodes, grouped by the region packet they must be sent in.
# Zeroes are padding slots: they are part of the 42-record layout but address no key.
REGION_KEYCODES: Dict[str, List[int]] = {
    "alphanum": [
        4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19,
        20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35,
        36, 37, 38, 39, 58, 59, 60, 61, 62, 63,
    ],
    "enter": [
        40, 49, 50, 100, 135, 136, 137, 138, 139, 144, 145,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    ],
    "modifiers": [
        41, 42, 43, 44, 45, 46, 47, 48, 51, 52, 53, 54, 55, 56, 57,
        102, 224, 225, 226, 227, 228, 229, 230, 240,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    ],
    "numpad": [
        64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79,
        80, 81, 82,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    ],
}

def _build_keycode_region_index() -> Tuple[Dict[int, str], List[int]]:
    index: Dict[int, str] = {}
    duplicates: List[int] = []
    for region in REGIONS:
        for code in REGION_KEYCODES[region]:
            if code == 0:
                continue
            if code in index:
                duplicates.append(code)
                continue
            index[code] = region
    return index, duplicates


KEYCODE_REGION, DUPLICATE_KEYCODES = _build_keycode_region_index()


class ProtocolError(ValueError):
    """Raised when a packet does not look like a frame this codec understands."""


@dataclass(frozen=True)
class KeyRecord:
    """One 12-byte key record inside a 0x0E frame."""

    index: int
    keycode: int
    rgb: Tuple[int, int, int]
    effect_id: int
    mode: int
    raw: bytes

    @property
    def is_empty(self) -> bool:
        """True when the slot addresses no key (the zero padding record)."""
        return self.keycode == 0

    @property
    def is_lit(self) -> bool:
        return self.keycode != 0 and self.rgb != (0, 0, 0)

    @property
    def region(self) -> Optional[str]:
        return KEYCODE_REGION.get(self.keycode)

    @property
    def hex_rgb(self) -> str:
        return "%02x%02x%02x" % self.rgb


@dataclass
class Frame:
    """A decoded 0x0E feature report."""

    report_id: int
    region_id: int
    region: Optional[str]
    records: List[KeyRecord]
    trailer: bytes
    raw: bytes

    @property
    def trailer_ok(self) -> bool:
        return self.trailer == FRAME_TRAILER

    def colors(self) -> Dict[int, Tuple[int, int, int]]:
        """Every addressed key, keyed by MSI keycode (black keys included)."""
        return {r.keycode: r.rgb for r in self.records if not r.is_empty}

    def lit_colors(self) -> Dict[int, Tuple[int, int, int]]:
        """Addressed keys whose colour is not black."""
        return {r.keycode: r.rgb for r in self.records if r.is_lit}

    def unknown_keycodes(self) -> List[int]:
        return [r.keycode for r in self.records if not r.is_empty and r.region is None]


@dataclass
class Commit:
    """A decoded 0x09 output report."""

    report_id: int
    body: bytes = field(default=b"")
    raw: bytes = b""


@dataclass(frozen=True)
class RegionChunk:
    region: str
    colors: Mapping[int, Tuple[int, int, int]]


def normalize_rgb(rgb: Sequence[int]) -> Tuple[int, int, int]:
    if len(rgb) != 3:
        raise ProtocolError("colour must be three components, got %r" % (rgb,))
    out = []
    for component in rgb:
        value = int(component)
        if not 0 <= value <= 255:
            raise ProtocolError("colour component out of range: %r" % (component,))
        out.append(value)
    return (out[0], out[1], out[2])


def build_key_record(
    rgb: Sequence[int],
    keycode: int,
    effect_id: int = STATIC_EFFECT_ID,
    mode: int = STATIC_MODE,
) -> bytes:
    """Build one 12-byte key record.

    Layout (steady colour, as sent by SteelSeries GG)::

        [R G B] [0 0 0 0 0 0] [effect_id] [mode] [keycode]

    TauAkiou's write-up labels the zero run as the reactive colour + reactive
    duration fields, which is why it is six bytes even though only three are a
    colour. The reference implementation sends them as zeroes and the controller
    accepts the record, so this codec does the same.
    """
    r, g, b = normalize_rgb(rgb)
    if not 0 <= keycode <= 0xFF:
        raise ProtocolError("keycode out of range: %r" % (keycode,))
    record = bytes([r, g, b, 0, 0, 0, 0, 0, 0, effect_id & 0xFF, mode & 0xFF, keycode & 0xFF])
    assert len(record) == RECORD_LEN
    return record


def build_region_frame(region: str, colors: Mapping[int, Sequence[int]]) -> bytes:
    """Build a complete 524-byte 0x0E frame for one region."""
    if region not in REGION_IDS:
        raise ProtocolError("unknown region %r (expected one of %s)" % (region, ", ".join(REGIONS)))
    for keycode in colors:
        if KEYCODE_REGION.get(int(keycode)) != region:
            raise ProtocolError(
                "keycode %s does not belong to region %r" % (keycode, region)
            )
    if len(colors) > NB_KEYS:
        raise ProtocolError("region %s cannot address %d keys" % (region, len(colors)))

    payload = bytearray()
    payload += bytes([FRAME_REPORT_ID, 0x00, REGION_IDS[region], 0x00])
    for keycode, rgb in colors.items():
        payload += build_key_record(rgb, int(keycode))
    payload += bytes(RECORD_LEN * (NB_KEYS - len(colors)))
    payload += FRAME_TRAILER
    if len(payload) != FRAME_LEN:
        raise AssertionError("frame length %d != %d" % (len(payload), FRAME_LEN))
    return bytes(payload)


def build_commit() -> bytes:
    """Build the 64-byte 0x09 output report that applies the pending frame."""
    return bytes([COMMIT_REPORT_ID]) + bytes(COMMIT_LEN - 1)


def chunk_by_region(
    colors: Mapping[int, Sequence[int]],
    regions: Optional[Sequence[str]] = None,
) -> List[RegionChunk]:
    """Group MSI keycodes into the region packets they must travel in.

    With ``regions`` given, every requested region is emitted even when empty
    (an all-zero region packet clears those keys, which is what ``set`` wants).
    """
    if regions is None:
        selected = [r for r in REGIONS if any(KEYCODE_REGION.get(int(k)) == r for k in colors)]
    else:
        selected = list(regions)

    unknown = sorted(int(k) for k in colors if int(k) not in KEYCODE_REGION)
    if unknown:
        raise ProtocolError(
            "no region is known for keycode(s) %s -- wrong keymap for this model?"
            % ", ".join(str(k) for k in unknown)
        )

    chunks: List[RegionChunk] = []
    for region in selected:
        if region not in REGION_IDS:
            raise ProtocolError("unknown region %r" % (region,))
        region_colors = {
            int(k): normalize_rgb(v)
            for k, v in colors.items()
            if KEYCODE_REGION.get(int(k)) == region
        }
        chunks.append(RegionChunk(region=region, colors=region_colors))
    return chunks


def build_frame_packets(
    colors: Mapping[int, Sequence[int]],
    regions: Optional[Sequence[str]] = None,
) -> List[Tuple[str, bytes]]:
    """Return ``[(region, frame_bytes), ...]`` ready to be sent in order."""
    return [(chunk.region, build_region_frame(chunk.region, chunk.colors)) for chunk in chunk_by_region(colors, regions)]


def parse_key_record(raw: bytes, index: int = 0) -> KeyRecord:
    if len(raw) != RECORD_LEN:
        raise ProtocolError("key record must be %d bytes, got %d" % (RECORD_LEN, len(raw)))
    return KeyRecord(
        index=index,
        keycode=raw[11],
        rgb=(raw[0], raw[1], raw[2]),
        effect_id=raw[9],
        mode=raw[10],
        raw=bytes(raw),
    )


def parse_region_frame(data: bytes) -> Frame:
    """Decode a 524-byte 0x0E frame."""
    if len(data) != FRAME_LEN:
        raise ProtocolError("expected a %d-byte frame, got %d bytes" % (FRAME_LEN, len(data)))
    if data[0] != FRAME_REPORT_ID:
        raise ProtocolError(
            "expected report id 0x%02x, got 0x%02x (this is not a 0x0E key frame)"
            % (FRAME_REPORT_ID, data[0])
        )
    region_id = data[2]
    region = next((name for name, value in REGION_IDS.items() if value == region_id), None)
    records = [
        parse_key_record(data[HEADER_LEN + i * RECORD_LEN : HEADER_LEN + (i + 1) * RECORD_LEN], i)
        for i in range(NB_KEYS)
    ]
    trailer = bytes(data[HEADER_LEN + NB_KEYS * RECORD_LEN :])
    return Frame(
        report_id=data[0],
        region_id=region_id,
        region=region,
        records=records,
        trailer=trailer,
        raw=bytes(data),
    )


def parse_commit(data: bytes) -> Commit:
    if len(data) != COMMIT_LEN:
        raise ProtocolError("expected a %d-byte commit report, got %d bytes" % (COMMIT_LEN, len(data)))
    if data[0] != COMMIT_REPORT_ID:
        raise ProtocolError("expected report id 0x09, got 0x%02x" % data[0])
    return Commit(report_id=data[0], body=bytes(data[1:]), raw=bytes(data))


def parse_packet(data: bytes):
    """Decode either frame type, dispatching on report id."""
    if not data:
        raise ProtocolError("empty packet")
    if data[0] == FRAME_REPORT_ID:
        return parse_region_frame(data)
    if data[0] == COMMIT_REPORT_ID:
        return parse_commit(data)
    raise ProtocolError("unknown report id 0x%02x" % data[0])
