"""Guardrails for the files that touch the system, not the codec.

These do not need Linux: they only read the shipped `udev/`, `systemd/` and
`scripts/` files and check the properties the docs promise. They exist because a
regression here is worse than a wrong colour -- an over-broad udev rule hands every
logged-in user write access to every HID device, and a service that has lost its
`--commit`-equivalent path silently does nothing at boot.
"""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UDEV_RULES = sorted((ROOT / "udev").glob("*.rules"))
SYSTEMD_UNITS = sorted((ROOT / "systemd").glob("*.service"))


class UdevRuleTests(unittest.TestCase):
    def test_a_rule_is_shipped(self):
        self.assertEqual([p.name for p in UDEV_RULES], ["99-msi-steelseries-rgb.rules"])

    def test_rules_only_match_the_klc_vendor_and_product_ids(self):
        for path in UDEV_RULES:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                self.assertRegex(
                    line, r'ATTRS?\{idVendor\}=="1038"', "%s:%d" % (path.name, number)
                )
                self.assertRegex(
                    line,
                    r'ATTRS?\{idProduct\}=="11(3a|22|3c)"',
                    "%s:%d" % (path.name, number),
                )

    def test_rules_never_widen_every_hidraw_node(self):
        for path in UDEV_RULES:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn('KERNEL=="hidraw*", MODE="0666"', text)
            self.assertNotIn("0666", text, path.name)


class SystemdUnitTests(unittest.TestCase):
    def test_units_call_the_installed_apply_script(self):
        services = {p.name for p in SYSTEMD_UNITS}
        self.assertEqual(services, {"msikb-lighting.service", "msikb-lighting-resume.service"})
        for path in SYSTEMD_UNITS:
            text = path.read_text(encoding="utf-8")
            self.assertIn("ExecStart=/usr/local/bin/msikb-apply", text, path.name)
            self.assertIn("Type=oneshot", text, path.name)
            self.assertIn("[Install]", text, path.name)

    def test_the_resume_unit_hooks_the_suspend_targets(self):
        text = (ROOT / "systemd" / "msikb-lighting-resume.service").read_text(encoding="utf-8")
        self.assertIn("After=suspend.target", text)
        self.assertIn("WantedBy=suspend.target", text)


class ScriptTests(unittest.TestCase):
    SHIPPED = (
        "scripts/diagnose.sh",
        "scripts/first-run.sh",
        "scripts/install.sh",
        "scripts/make-bundle.sh",
        "scripts/msikb-apply",
    )

    def test_every_documented_script_exists(self):
        for relative in self.SHIPPED:
            path = ROOT / relative
            self.assertTrue(path.is_file(), relative)
            self.assertTrue(path.read_text(encoding="utf-8").startswith("#!/bin/sh"), relative)

    @unittest.skipIf(os.name == "nt", "Windows does not carry POSIX execute bits")
    def test_scripts_are_executable(self):
        for relative in self.SHIPPED:
            self.assertTrue((ROOT / relative).stat().st_mode & 0o111, relative)

    @unittest.skipUnless(shutil.which("sh"), "needs a POSIX shell")
    def test_scripts_parse(self):
        for relative in self.SHIPPED:
            result = subprocess.run(
                ["sh", "-n", str(ROOT / relative)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, "%s: %s" % (relative, result.stderr.decode()))

    def test_apply_script_uses_the_measured_keymap_and_commits(self):
        text = (ROOT / "scripts" / "msikb-apply").read_text(encoding="utf-8")
        self.assertIn("MSIKB_KEYMAP:-gp66-leopard", text)
        self.assertIn("--commit", text)
        # The safety rule: steady colours only, never the 0x0B effect packet.
        self.assertNotRegex(text, r"--effects?\b|0x0B")

    def test_installer_installs_the_rule_the_units_and_the_apply_script(self):
        text = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("99-msi-steelseries-rgb.rules", text)
        self.assertIn("msikb-lighting.service", text)
        self.assertIn("msikb-lighting-resume.service", text)
        self.assertIn("install -m 0755", text)
        self.assertNotIn("chmod 666", text)


class KeymapCoverageTests(unittest.TestCase):
    """The default keymap must address every LED the region tables describe."""

    def test_default_keymap_covers_the_whole_region_table(self):
        from msikb import protocol
        from msikb.keymaps import default_keymap

        keymap = default_keymap()
        self.assertEqual(keymap.name, "gp66-leopard")
        addressed = set(keymap.codes_for_spec("all"))
        self.assertEqual(addressed, set(protocol.KEYCODE_REGION))
        self.assertEqual(len(addressed), 96)

    def test_no_unmapped_keycode_survives_in_the_default_keymap(self):
        from msikb.keymaps import default_keymap

        self.assertEqual(default_keymap().unmapped_msi_codes(), [])


if __name__ == "__main__":
    unittest.main()
