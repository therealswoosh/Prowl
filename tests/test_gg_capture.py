"""The codec, checked against real SteelSeries GG traffic from this laptop.

These fixtures are not synthesised: each `*.hex` file is one SET_REPORT payload
copied out of a USBPcap capture taken while SteelSeries GG was driving the
keyboard on Windows (see `fixtures/gg/make_gg_fixtures.py`). What they pin down:

* the four region ids, and that byte 2 of a frame is the record count,
* the exact keycodes each region carries and the order they are sent in,
* that a full push addresses all 96 LEDs,
* that our encoder reproduces GG's bytes.

Keeping this test means a future edit to `REGION_IDS`/`REGION_KEYCODES` cannot
silently drift away from what the vendor software actually does on this model.
"""

import glob
import unittest
from pathlib import Path

from msikb import protocol

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gg"


def frames():
    """``{capture name: {region name: raw frame bytes}}`` from the fixtures."""
    captures = {}
    for path in sorted(glob.glob(str(FIXTURES / "*.hex"))):
        name, _, tail = Path(path).stem.rpartition("-")
        raw = bytes.fromhex(Path(path).read_text(encoding="ascii").strip())
        if raw[0] == protocol.COMMIT_REPORT_ID:
            captures.setdefault(name, {})["commit"] = raw
        else:
            captures.setdefault(name, {})[tail] = raw
    return captures


CAPTURES = frames()


class CaptureLayoutTests(unittest.TestCase):
    def test_both_full_pushes_are_present(self):
        self.assertEqual(sorted(CAPTURES), ["enable", "newcap"])
        for name, capture in CAPTURES.items():
            self.assertEqual(len([k for k in capture if k != "commit"]), 4, name)
            self.assertIn("commit", capture, name)

    def test_every_frame_is_a_524_byte_0e_report(self):
        for name, capture in CAPTURES.items():
            for key, raw in capture.items():
                if key == "commit":
                    self.assertEqual(len(raw), protocol.COMMIT_LEN, name)
                    continue
                self.assertEqual(len(raw), protocol.FRAME_LEN, name)
                self.assertEqual(raw[0], protocol.FRAME_REPORT_ID, name)

    def test_commit_is_byte_identical_to_gg(self):
        for name, capture in CAPTURES.items():
            self.assertEqual(capture["commit"], protocol.build_commit(), name)


class RegionTableTests(unittest.TestCase):
    def test_region_ids_and_counts_match_the_tables(self):
        for name, capture in CAPTURES.items():
            for region in protocol.REGIONS:
                raw = capture["%02x" % protocol.REGION_IDS[region]]
                frame = protocol.parse_region_frame(raw)
                self.assertEqual(frame.region, region, name)
                expected = [c for c in protocol.REGION_KEYCODES[region] if c]
                self.assertEqual(oct(raw[2]), oct(len(expected)), (name, region))
                self.assertEqual([r.keycode for r in frame.records if not r.is_empty],
                                 expected, (name, region))
                self.assertEqual(frame.colors(), {c: frame.colors()[c] for c in expected})

    def test_a_full_push_addresses_all_96_leds(self):
        capture = CAPTURES["newcap"]
        codecs = {}
        for region in protocol.REGIONS:
            frame = protocol.parse_region_frame(
                capture["%02x" % protocol.REGION_IDS[region]]
            )
            codecs.update(frame.colors())
        self.assertEqual(len(codecs), 96)
        self.assertEqual(set(codecs), set(protocol.KEYCODE_REGION))

    def test_gg_leaves_twelve_leds_out_of_a_select_all(self):
        """Even a "select every key" push in GG sends 12 records with no effect.

        Eight of them are the keycodes that have no X11 name at all; the other four
        are the PrintScreen/ScrollLock/Pause/Delete cluster in the 0x13 region. GG
        still emits the records (they must be present), it just leaves them unlit,
        so painting them is safe and is what `--keys all` does.
        """
        capture = CAPTURES["newcap"]
        untouched = set()
        red = 0
        for region in protocol.REGIONS:
            frame = protocol.parse_region_frame(
                capture["%02x" % protocol.REGION_IDS[region]]
            )
            for record in frame.records:
                if record.is_empty:
                    continue
                if record.effect_id == protocol.STATIC_EFFECT_ID:
                    self.assertEqual(record.rgb, (255, 0, 0))
                    red += 1
                else:
                    untouched.add(record.keycode)
        self.assertEqual(untouched, {50, 70, 71, 72, 76, 135, 136, 137, 138, 139, 144, 145})
        self.assertEqual(red, 84)

    def test_all_black_push_keeps_every_record(self):
        capture = CAPTURES["enable"]
        codecs = {}
        for region in protocol.REGIONS:
            frame = protocol.parse_region_frame(
                capture["%02x" % protocol.REGION_IDS[region]]
            )
            self.assertEqual(set(frame.lit_colors()), set())
            codecs.update(frame.colors())
        self.assertEqual(len(codecs), 96)


class EncoderMatchesGGTests(unittest.TestCase):
    def test_our_frame_reproduces_gg_for_every_key_gg_coloured(self):
        """GG's own bytes for a key it set, versus the bytes we would send for it."""
        capture = CAPTURES["newcap"]
        checked = 0
        for region in protocol.REGIONS:
            frame = protocol.parse_region_frame(
                capture["%02x" % protocol.REGION_IDS[region]]
            )
            ours = protocol.build_region_frame(region, frame.colors())
            for record in frame.records:
                if record.is_empty:
                    continue
                start = protocol.HEADER_LEN + record.index * protocol.RECORD_LEN
                mine = ours[start : start + protocol.RECORD_LEN]
                if record.effect_id == protocol.STATIC_EFFECT_ID:
                    self.assertEqual(mine, record.raw, (region, record.keycode))
                    checked += 1
                else:
                    # GG left these LEDs on "no effect, no colour": the colour and the
                    # keycode still agree, only the effect fields differ.
                    self.assertEqual(record.rgb, (0, 0, 0))
                    self.assertEqual(mine[11], record.keycode)
                    self.assertEqual(mine[:3], record.raw[:3])
        self.assertEqual(checked, 84)

    def test_only_the_trailer_differs_for_a_coloured_push(self):
        """Our tail is the reference implementation's constant; GG pads zeros."""
        capture = CAPTURES["newcap"]
        for region in protocol.REGIONS:
            frame = protocol.parse_region_frame(
                capture["%02x" % protocol.REGION_IDS[region]]
            )
            self.assertEqual(frame.trailer, bytes(protocol.TRAILER_LEN), region)
            ours = protocol.build_region_frame(region, frame.colors())
            kept = protocol.HEADER_LEN + len(frame.records) * protocol.RECORD_LEN
            self.assertEqual(ours[kept:], protocol.FRAME_TRAILER, region)
            for record in frame.records:
                if record.is_empty or record.effect_id != protocol.STATIC_EFFECT_ID:
                    continue
                start = protocol.HEADER_LEN + record.index * protocol.RECORD_LEN
                self.assertEqual(ours[start : start + protocol.RECORD_LEN], record.raw,
                                 (region, record.keycode))

    def test_black_only_frames_differ_in_the_effect_bytes(self):
        """For an all-black push GG puts 0x01 in record byte 8, we put it in byte 9.

        Both mean "steady colour, this is the colour"; black is off either way, and
        byte 9 is what the reference implementation sends for mixed frames too. Only
        those two bytes may differ -- colour and keycode are still identical.
        """
        capture = CAPTURES["enable"]
        for region in protocol.REGIONS:
            frame = protocol.parse_region_frame(
                capture["%02x" % protocol.REGION_IDS[region]]
            )
            ours = protocol.build_region_frame(region, frame.colors())
            for record in frame.records:
                if record.is_empty:
                    continue
                start = protocol.HEADER_LEN + record.index * protocol.RECORD_LEN
                mine = ours[start : start + protocol.RECORD_LEN]
                self.assertEqual(mine[:8], record.raw[:8], (region, record.keycode))
                self.assertEqual(mine[10:], record.raw[10:], (region, record.keycode))
                self.assertNotEqual(mine[8:10], record.raw[8:10], (region, record.keycode))

    def test_rebuild_is_stable(self):
        """parse -> build -> parse is idempotent, so decoded captures can be replayed."""
        for name, capture in CAPTURES.items():
            for region in protocol.REGIONS:
                frame = protocol.parse_region_frame(
                    capture["%02x" % protocol.REGION_IDS[region]]
                )
                again = protocol.parse_region_frame(
                    protocol.build_region_frame(region, frame.colors())
                )
                self.assertEqual(again.colors(), frame.colors(), (name, region))
                self.assertEqual(again.region, frame.region, (name, region))


if __name__ == "__main__":
    unittest.main()
