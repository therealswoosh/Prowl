"""Generate synthetic capture fixtures for the tests.

These are NOT real GG captures: they are built with our own codec so the tests can
exercise the decode/learn pipeline end to end. Real 1038:113a captures must still
come from Wireshark + USBPcap (see `msikb capture-help`).

Run from the repository root::

    python tests/fixtures/make_fixtures.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from msikb import protocol  # noqa: E402

OUT = Path(__file__).resolve().parent / "learn"

# key name -> (MSI keycode, colour), i.e. "GG set this one key to this colour"
KEYS = {
    "esc": (41, (255, 0, 0)),
    "a": (4, (0, 255, 0)),
    "kp7": (95, (0, 0, 255)),
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (msi_code, rgb) in KEYS.items():
        region = protocol.KEYCODE_REGION[msi_code]
        # A black keyboard, as GG leaves it, is the baseline the diffs are taken against.
        colors = {
            code: (0, 0, 0)
            for code, key_region in protocol.KEYCODE_REGION.items()
            if key_region == region
        }
        colors[msi_code] = rgb
        frame = protocol.build_region_frame(region, colors)
        commit = protocol.build_commit()
        lines = [
            "# synthetic fixture: one key coloured, everything else black",
            " ".join("%02x" % b for b in frame),
            " ".join("%02x" % b for b in commit),
        ]
        (OUT / ("%s.txt" % name)).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote %d fixtures to %s" % (len(KEYS), OUT))


if __name__ == "__main__":
    main()
