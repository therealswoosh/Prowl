import unittest

from msikb import protocol


class FrameLayoutTests(unittest.TestCase):
    def test_frame_is_524_bytes(self):
        frame = protocol.build_region_frame("alphanum", {4: (255, 0, 0)})
        self.assertEqual(len(frame), protocol.FRAME_LEN)
        self.assertEqual(protocol.FRAME_LEN, 524)

    def test_header_is_repoet_id_then_zero_then_region(self):
        frame = protocol.build_region_frame("numpad", {64: (1, 2, 3)})
        # 0x13 is the measured record count for this keyboard's fourth region (19 keys).
        self.assertEqual(list(frame[:4]), [0x0E, 0x00, 0x13, 0x00])

    def test_region_sizes_match_the_measured_keyboard(self):
        sizes = {r: len([c for c in codes if c]) for r, codes in protocol.REGION_KEYCODES.items()}
        self.assertEqual(sizes, {"alphanum": 42, "enter": 11, "modifiers": 24, "numpad": 19})
        self.assertEqual(sum(sizes.values()), 96)
        self.assertEqual({r: protocol.REGION_IDS[r] for r in sizes}, sizes)

    def test_trailer_is_constant(self):
        frame = protocol.build_region_frame("alphanum", {})
        self.assertEqual(frame[-16:], bytes([0] * 14 + [0x08, 0x39]))

    def test_record_layout(self):
        frame = protocol.build_region_frame("alphanum", {4: (0xAA, 0xBB, 0xCC)})
        record = frame[protocol.HEADER_LEN : protocol.HEADER_LEN + protocol.RECORD_LEN]
        self.assertEqual(list(record), [0xAA, 0xBB, 0xCC, 0, 0, 0, 0, 0, 0, 0x01, 0x00, 4])

    def test_records_follow_request_order(self):
        colors = {5: (1, 1, 1), 4: (2, 2, 2)}
        frame = protocol.build_region_frame("alphanum", colors)
        first = frame[protocol.HEADER_LEN + 11]
        second = frame[protocol.HEADER_LEN + protocol.RECORD_LEN + 11]
        self.assertEqual((first, second), (5, 4))
        # unused record slots stay zeroed
        third = frame[protocol.HEADER_LEN + 2 * protocol.RECORD_LEN + protocol.RECORD_LEN - 1]
        self.assertEqual(third, 0)

    def test_42_record_slots(self):
        self.assertEqual(len(protocol.REGION_KEYCODES["alphanum"]), protocol.NB_KEYS)

    def test_every_region_has_42_slots(self):
        for region, codes in protocol.REGION_KEYCODES.items():
            self.assertEqual(len(codes), protocol.NB_KEYS, region)


class CommitTests(unittest.TestCase):
    def test_commit_shape(self):
        commit = protocol.build_commit()
        self.assertEqual(len(commit), protocol.COMMIT_LEN)
        self.assertEqual(commit[0], 0x09)
        self.assertEqual(commit[1:], bytes(63))


class ChunkingTests(unittest.TestCase):
    def test_unknown_keycode_is_rejected(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.chunk_by_region({199: (1, 2, 3)})

    def test_keycode_in_wrong_region_is_rejected(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.build_region_frame("numpad", {4: (1, 2, 3)})

    def test_chunk_selection(self):
        chunks = protocol.chunk_by_region({4: (1, 2, 3), 224: (4, 5, 6)})
        self.assertEqual([c.region for c in chunks], ["alphanum", "modifiers"])

    def test_explicit_regions_emit_empty_frames(self):
        chunks = protocol.chunk_by_region({4: (1, 2, 3)}, regions=["alphanum", "numpad"])
        self.assertEqual([c.region for c in chunks], ["alphanum", "numpad"])
        self.assertEqual(chunks[1].colors, {})

    def test_bad_colour(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.build_key_record((1, 2), 4)
        with self.assertRaises(protocol.ProtocolError):
            protocol.build_key_record((1, 2, 300), 4)


class ParseTests(unittest.TestCase):
    def test_roundtrip(self):
        colors = {4: (10, 20, 30), 39: (0, 0, 0), 62: (200, 100, 50)}
        frame = protocol.parse_region_frame(protocol.build_region_frame("alphanum", colors))
        self.assertEqual(frame.region, "alphanum")
        self.assertTrue(frame.trailer_ok)
        # colors() keeps addressed keys; lit_colors() drops the black one
        self.assertEqual(frame.colors(), colors)
        self.assertEqual(frame.lit_colors(), {4: (10, 20, 30), 62: (200, 100, 50)})

    def test_parse_commit(self):
        commit = protocol.parse_packet(protocol.build_commit())
        self.assertIsInstance(commit, protocol.Commit)

    def test_parse_rejects_other_lengths(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse_region_frame(bytes(64))
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse_packet(b"\x0b" + bytes(600))

    def test_region_lookup(self):
        self.assertEqual(protocol.KEYCODE_REGION[4], "alphanum")
        self.assertEqual(protocol.KEYCODE_REGION[240], "modifiers")


class DataIntegrityTests(unittest.TestCase):
    def test_no_duplicate_keycodes_across_regions(self):
        self.assertEqual(protocol.DUPLICATE_KEYCODES, [])

    def test_region_index_covers_every_nonzero_code(self):
        expected = {
            code
            for codes in protocol.REGION_KEYCODES.values()
            for code in codes
            if code
        }
        self.assertEqual(set(protocol.KEYCODE_REGION), expected)


if __name__ == "__main__":
    unittest.main()
