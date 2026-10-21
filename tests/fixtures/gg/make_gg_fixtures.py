"""Write the real SteelSeries GG frames (from the USBPcap session) as test fixtures.

Source: analysis/pcap/klc-decoded.json, produced by analysis/pcap/klc_pcap.py from
captures taken with SteelSeries GG on the GP66 Leopard 11UG under Windows.

Each fixture is one line of hex: the exact 524-byte SET_REPORT payload the vendor
software sent, report id byte included.

    newcap.pcap  first full push: all 96 keys pure red, four region frames
    enable.pcap  first full push: all 96 keys black, four region frames

Run from the repository root:

    python3 tests/fixtures/gg/make_gg_fixtures.py /path/to/klc-decoded.json
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WANTED = {"newcap.pcap": "newcap", "enable.pcap": "enable"}


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    decoded = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    written = 0
    for capture in decoded:
        prefix = WANTED.get(capture.get("file", ""))
        if prefix is None:
            continue
        seen = set()
        for command in capture.get("commands", []):
            if command["cmd"] != 0x0E:
                continue
            raw = bytes.fromhex(command["raw"])
            region = raw[2]  # the count byte also names the region on this firmware
            if region in seen:
                continue
            seen.add(region)
            path = HERE / ("%s-%02x.hex" % (prefix, region))
            path.write_text(raw.hex() + "\n", encoding="ascii")
            print("wrote %s (%d bytes)" % (path.name, len(raw)))
            written += 1
        commits = [c for c in capture.get("commands", []) if c["cmd"] == 0x09]
        if commits:
            raw = bytes.fromhex(commits[0]["raw"])
            path = HERE / ("%s-commit.hex" % prefix)
            path.write_text(raw.hex() + "\n", encoding="ascii")
            print("wrote %s (%d bytes)" % (path.name, len(raw)))
            written += 1
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
