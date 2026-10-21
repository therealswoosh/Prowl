import unittest

from msikb import protocol
from msikb.keymaps import (
    EVDEV_NAMES,
    Keymap,
    KeymapError,
    default_keymap,
    keymap_from_msi_mapping,
)


class ReferenceKeymapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keymap = Keymap.by_name("reference-gs65")

    def test_gs65_only_numpad_keys_have_no_region_here(self):
        # GS65 keycodes 83-99 are not on the captured GP66 keyboard, so the measured
        # region tables (42/24/19/11 = 96 keys) do not carry them.
        self.assertEqual(self.keymap.unmapped_msi_codes(), list(range(83, 100)))

    def test_known_keys(self):
        expected = {
            "esc": (9, 41, "modifiers"),
            "a": (38, 4, "alphanum"),
            "space": (65, 44, "modifiers"),
            "leftctrl": (37, 224, "modifiers"),
            "leftshift": (50, 225, "modifiers"),
            "leftmeta": (133, 227, "modifiers"),
            "f1": (67, 58, "alphanum"),
            # kp7/kpasterisk/kpenter are GS65 keycodes (95/85/88) that this keyboard
            # never receives, so they keep their MSI code but have no region.
            "kp7": (79, 95, None),
            "kpasterisk": (63, 85, None),
            "kpenter": (104, 88, None),
            "up": (111, 82, "numpad"),
            "fn": (-1, 240, "modifiers"),
        }
        for name, (x11, msi, region) in expected.items():
            entry = self.keymap.entry(name)
            self.assertEqual((entry.x11, entry.msi, entry.region), (x11, msi, region), name)

    def test_number_row_sits_on_msi_30_to_39(self):
        self.assertEqual([self.keymap.entry(str(d)).msi for d in range(1, 10)], list(range(30, 39)))
        self.assertEqual(self.keymap.entry("0").msi, 39)

    def test_every_name_appears_once(self):
        names = [e.name for e in self.keymap.entries()]
        self.assertEqual(len(names), len(set(names)))

    def test_names_cover_the_evdev_table(self):
        for evdev, name in EVDEV_NAMES.items():
            x11 = evdev + 8
            if x11 in self.keymap.x11_to_msi:
                self.assertLessEqual(len(name), 20)


class ModelKeymapTests(unittest.TestCase):
    """The keymap measured from SteelSeries GG captures on this laptop."""

    @classmethod
    def setUpClass(cls):
        cls.keymap = Keymap.by_name("gp66-leopard")

    def test_every_named_key_has_a_region(self):
        self.assertEqual(self.keymap.unmapped_msi_codes(), [])

    def test_matches_the_measured_96_keycodes(self):
        measured = set()
        for codes in protocol.REGION_KEYCODES.values():
            measured.update(c for c in codes if c)
        self.assertEqual(len(measured), 96)
        known = {e.msi for e in self.keymap.entries()} | {self.keymap.fn_code}
        self.assertEqual(known, measured)

    def test_nine_leds_have_no_x11_name_but_are_still_addressable(self):
        # GG colours these on this keyboard; no Linux keycode describes them, so they
        # are reachable as msi<code> and must be included in a whole-keyboard paint.
        unnamed = {50, 102, 135, 136, 137, 138, 139, 144, 145}
        self.assertEqual(set(self.keymap.unnamed_msi), unnamed)
        for code in sorted(unnamed):
            entry = self.keymap.entry("msi%d" % code)
            self.assertEqual((entry.x11, entry.msi), (-1, code))
            self.assertIsNotNone(entry.region, code)
        self.assertEqual(len(self.keymap.codes_for_spec("all")), 96)

    def test_is_the_default_keymap(self):
        self.assertEqual(default_keymap().name, "gp66-leopard")


class SpecExpansionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keymap = Keymap.by_name("reference-gs65")

    def test_alias_num_row(self):
        self.assertEqual(
            sorted(self.keymap.codes_for_spec("num_row")),
            sorted(list(range(30, 40)) + [45, 46]),
        )

    def test_alias_f_row(self):
        codes = self.keymap.codes_for_spec("f_row")
        self.assertEqual(len(codes), 12)
        self.assertTrue(all(protocol.KEYCODE_REGION[c] for c in codes))

    def test_alias_arrows(self):
        self.assertEqual(len(self.keymap.codes_for_spec("arrows")), 4)

    def test_alias_all_is_broad(self):
        self.assertGreater(len(self.keymap.codes_for_spec("all")), 90)

    def test_range_expansion(self):
        self.assertEqual(len(self.keymap.codes_for_spec("67-76")), 10)

    def test_range_over_missing_code_is_rejected(self):
        with self.assertRaises(KeymapError):
            self.keymap.codes_for_spec("96-98")

    def test_numeric_x11_keycode(self):
        self.assertEqual(self.keymap.code_for("67"), self.keymap.entry("f1").msi)

    def test_names_beat_numeric_ambiguity(self):
        # "1" is the key named 1, not X11 keycode 1 (which is not even valid)
        self.assertEqual(self.keymap.code_for("1"), self.keymap.entry("1").msi)

    def test_unknown_name(self):
        with self.assertRaises(KeymapError):
            self.keymap.code_for("nonsense")

    def test_comma_spec_mix(self):
        self.assertEqual(len(self.keymap.codes_for_spec("esc,a,67-68")), 4)

    def test_alias_with_no_keys_on_the_model_is_an_error(self):
        # gp66-leopard has no numpad cluster: the aliases must not silently expand
        # to nothing, or `set --keys numpad` looks like it worked.
        gp66 = Keymap.by_name("gp66-leopard")
        with self.assertRaises(KeymapError):
            gp66.codes_for_spec("numpad")
        self.assertEqual(len(gp66.codes_for_spec("arrows")), 4)


class ConstructedKeymapTests(unittest.TestCase):
    def test_build_from_captured_names(self):
        keymap = keymap_from_msi_mapping("captured", {"esc": 41, "a": 4, "fn": 240})
        self.assertEqual(keymap.entry("esc").msi, 41)
        self.assertEqual(keymap.entry("a").x11, 38)

    def test_build_rejects_unknown_names(self):
        with self.assertRaises(KeymapError):
            keymap_from_msi_mapping("captured", {"keycode999": 41})

    def test_json_roundtrip(self):
        keymap = keymap_from_msi_mapping("captured", {"esc": 41, "a": 4})
        payload = keymap.to_json()
        self.assertIn("x11_to_msi", payload)
        self.assertEqual(payload["x11_to_msi"]["9"], 41)


if __name__ == "__main__":
    unittest.main()
