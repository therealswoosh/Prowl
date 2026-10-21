import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from msikb import protocol, transport
from msikb.cli import main


class IoctlContractTests(unittest.TestCase):
    """The ioctl numbers are the Linux ABI; a wrong one silently does nothing."""

    def test_hidiocsfeature(self):
        self.assertEqual(transport.HIDIOCSFEATURE(524), 0xC20C4806)

    def test_hidiocgrawinfo(self):
        self.assertEqual(transport.HIDIOCGRAWINFO(), 0x80084803)

    def test_ioctl_availability_flag(self):
        self.assertEqual(transport.raw_ioctl_available(), sys.platform == "linux")

    @unittest.skipIf(sys.platform == "linux", "hidraw only enumerates on Linux")
    def test_list_hidraw_is_empty_off_linux(self):
        self.assertEqual(transport.list_hidraw(), [])

    def test_probe_node_is_none_without_fcntl(self):
        # Regression: Windows has no os.O_NONBLOCK, so probing must bail out before
        # touching platform-specific flags rather than raising AttributeError.
        with mock.patch.object(transport, "fcntl", None):
            self.assertIsNone(transport.probe_node("/dev/hidraw0"))

    @unittest.skipIf(sys.platform == "linux", "hidraw only enumerates on Linux")
    def test_hidraw_transport_refuses_off_linux(self):
        device = transport.HidrawDevice("/dev/hidraw0", 3, 0x1038, 0x113A, "fake")
        with self.assertRaises(transport.TransportError):
            transport.HidrawTransport(device)


class NullTransportTests(unittest.TestCase):
    def test_records_reports(self):
        with transport.NullTransport() as null:
            null.send_feature_report(protocol.build_region_frame("alphanum", {4: (1, 2, 3)}))
            null.send_output_report(protocol.build_commit())
        self.assertEqual(len(null.feature_reports), 1)
        self.assertEqual(len(null.feature_reports[0]), protocol.FRAME_LEN)
        self.assertEqual(len(null.output_reports[0]), protocol.COMMIT_LEN)


class SendPathTests(unittest.TestCase):
    """Exercises the real send path with a recording transport instead of hardware."""

    def run_with_recorder(self, argv):
        recorder = transport.NullTransport()
        with mock.patch("msikb.cli.open_transport", return_value=recorder):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                status = main(argv)
        return status, buffer.getvalue(), recorder

    def test_set_commit_writes_four_frames_and_one_commit(self):
        status, output, recorder = self.run_with_recorder(["set", "--color", "ff0000", "--commit"])
        self.assertEqual(status, 0, output)
        self.assertEqual(len(recorder.feature_reports), 4)
        frames = []
        for report in recorder.feature_reports:
            self.assertEqual(len(report), protocol.FRAME_LEN)
            self.assertEqual(report[0], protocol.FRAME_REPORT_ID)
            frame = protocol.parse_region_frame(report)
            self.assertTrue(frame.trailer_ok)
            frames.append(frame)
        self.assertEqual(len(recorder.output_reports), 1)
        self.assertEqual(recorder.output_reports[0], protocol.build_commit())

        # all four regions are emitted, every key of the keymap is written, and a
        # region frame never exceeds its 42 record slots. The measured keyboard has
        # 96 addressable keys (42 + 24 + 19 + 11).
        self.assertEqual([f.region for f in frames], list(protocol.REGIONS))
        self.assertEqual(sum(len(f.colors()) for f in frames), 96)
        self.assertTrue(all(len(f.colors()) <= protocol.NB_KEYS for f in frames))

    def test_key_commit_writes_only_its_region(self):
        status, output, recorder = self.run_with_recorder(
            ["key", "--key", "esc", "--color", "00ff00", "--commit"]
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(len(recorder.feature_reports), 1)
        frame = protocol.parse_region_frame(recorder.feature_reports[0])
        self.assertEqual(frame.region, "modifiers")
        self.assertEqual(frame.lit_colors(), {41: (0, 255, 0)})

    def test_verify_commit_flashes_each_key(self):
        status, output, recorder = self.run_with_recorder(
            ["verify", "--only", "esc,a", "--commit", "--dwell", "0", "--delay", "0"]
        )
        self.assertEqual(status, 0, output)
        # one full-keyboard frame per region plus one commit, per key
        self.assertEqual(len(recorder.output_reports), 2)
        self.assertGreaterEqual(len(recorder.feature_reports), 2)


class _FakeFcntl:
    """Stands in for the Linux fcntl module: records buffers, optionally rejects one."""

    def __init__(self, reject_first: bool = False):
        self.buffers = []
        self.calls = 0
        self.reject_first = reject_first

    def ioctl(self, fd, request, buffer, mutate):
        self.calls += 1
        if self.reject_first and self.calls == 1:
            raise OSError(transport.errno.EINVAL, "Invalid argument")
        self.buffers.append(bytes(buffer))
        return buffer


class HidrawAddressingTests(unittest.TestCase):
    """GG addresses report 0; hidapi-style code puts the opcode in buffer[0].

    If the kernel rejects the second form, the transport must retry once with a 0x00
    report number -- that reproduces GG's exact transfer and is the documented
    fallback for `EINVAL` from HIDIOCSFEATURE.
    """

    def test_accepted_report_number_is_used_unchanged(self):
        fake = _FakeFcntl()
        with mock.patch.object(transport, "fcntl", fake), \
                mock.patch("msikb.transport.os.open", return_value=7), \
                mock.patch.object(transport.time, "sleep"):
            device = transport.HidrawDevice("/dev/hidraw9", 3, 0x1038, 0x113A, "fake KLC")
            with transport.HidrawTransport(device, delay=0) as hid:
                hid.send_feature_report(protocol.build_region_frame("alphanum", {4: (1, 2, 3)}))
                self.assertEqual(hid.report_prefix, b"")
        self.assertEqual(len(fake.buffers[0]), protocol.FRAME_LEN)
        self.assertEqual(fake.buffers[0][0], protocol.FRAME_REPORT_ID)

    def test_einval_retries_with_report_id_zero(self):
        fake = _FakeFcntl(reject_first=True)
        frame = protocol.build_region_frame("alphanum", {4: (1, 2, 3)})
        errors = io.StringIO()
        with mock.patch.object(transport, "fcntl", fake), \
                mock.patch("msikb.transport.os.open", return_value=7), \
                mock.patch.object(transport.time, "sleep"):
            device = transport.HidrawDevice("/dev/hidraw9", 3, 0x1038, 0x113A, "fake KLC")
            with transport.HidrawTransport(device, delay=0) as hid, redirect_stderr(errors):
                hid.send_feature_report(frame)
                self.assertEqual(hid.report_prefix, b"\x00")
                self.assertIn("report id 0", hid.describe())
        self.assertIn("wants report id 0", errors.getvalue())
        self.assertEqual(fake.buffers[0], b"\x00" + frame)
        self.assertEqual(len(fake.buffers[0]), protocol.FRAME_LEN + 1)
        self.assertEqual(fake.buffers[0][1], protocol.FRAME_REPORT_ID)

    def test_a_second_rejection_is_an_error_not_a_loop(self):
        class AlwaysReject(_FakeFcntl):
            def ioctl(self, fd, request, buffer, mutate):
                raise OSError(transport.errno.EINVAL, "Invalid argument")

        with mock.patch.object(transport, "fcntl", AlwaysReject()), \
                mock.patch("msikb.transport.os.open", return_value=7), \
                mock.patch.object(transport.time, "sleep"):
            device = transport.HidrawDevice("/dev/hidraw9", 3, 0x1038, 0x113A, "fake KLC")
            with transport.HidrawTransport(device, delay=0) as hid:
                with self.assertRaises(transport.TransportError):
                    hid.send_feature_report(protocol.build_region_frame("alphanum", {4: (1, 2, 3)}))


class _FakeUsbDevice:
    def __init__(self):
        self.transfers = []

    def is_kernel_driver_active(self, interface):
        return False

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex, data, timeout):
        self.transfers.append((bmRequestType, bRequest, wValue, wIndex, bytes(data), timeout))


class LibusbAddressingTests(unittest.TestCase):
    """The libusb path must address the report numbers GG uses on this controller."""

    def make_transport(self, **kwargs):
        device = _FakeUsbDevice()
        core = mock.Mock()
        core.find.return_value = device
        usb_module = mock.Mock(core=core)
        with mock.patch.dict(sys.modules, {"usb": usb_module, "usb.core": core}):
            instance = transport.LibusbTransport(
                0x1038, 0x113A, interface=2, delay=0, **kwargs
            )
        instance.dev = device
        return instance

    def test_frames_and_commits_are_addressed_as_report_zero(self):
        instance = self.make_transport()
        frame = protocol.build_region_frame("numpad", {64: (9, 9, 9)})
        instance.send_feature_report(frame)
        instance.send_output_report(protocol.build_commit())
        (feature, commit) = instance.dev.transfers
        self.assertEqual(feature[:4], (0x21, 0x09, 0x0300, 2))
        self.assertEqual(feature[4], frame)
        self.assertEqual(commit[:4], (0x21, 0x09, 0x0200, 2))
        self.assertEqual(commit[4], protocol.build_commit())

    def test_report_id_can_be_overridden(self):
        instance = self.make_transport(report_id=0x0E)
        instance.send_feature_report(protocol.build_region_frame("numpad", {64: (9, 9, 9)}))
        self.assertEqual(instance.dev.transfers[0][2], 0x030E)


if __name__ == "__main__":
    unittest.main()
