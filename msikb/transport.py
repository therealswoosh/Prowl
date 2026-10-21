"""Device transports for the MSI SteelSeries RGB controller.

Two backends:

* ``HidrawTransport`` -- Linux hidraw + ioctls, no third-party packages. This is
  the path that worked for other people on USB ``1038:113a``.
* ``LibusbTransport`` -- optional ``pyusb`` control transfers, for machines where
  the controller does not expose a usable hidraw node.

Writing to the controller is never done implicitly: the CLI requires ``--commit``.
"""

from __future__ import annotations

import errno
import os
import struct
import sys
import time

try:  # Linux only; the capture/decode half of the toolkit must import everywhere
    import fcntl
    import termios  # noqa: F401  (part of the ioctl contract on Linux)
except ImportError:  # pragma: no cover - exercised on Windows/macOS runners
    fcntl = None  # type: ignore[assignment]
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

DEFAULT_DELAY = 0.01  # seconds between reports; the controller drops packets if rushed

_IOC_NRBITS, _IOC_TYPEBITS, _IOC_SIZEBITS = 8, 8, 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_WRITE, _IOC_READ = 1, 2


def raw_ioctl_available() -> bool:
    return fcntl is not None


def _IOC(direction: int, type_char: str, nr: int, size: int) -> int:
    return (
        (direction << _IOC_DIRSHIFT)
        | (size << _IOC_SIZESHIFT)
        | (ord(type_char) << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
    )


def HIDIOCGRAWINFO(size: int = 8) -> int:
    return _IOC(_IOC_READ, "H", 0x03, size)


def HIDIOCGRAWNAME(length: int) -> int:
    return _IOC(_IOC_READ, "H", 0x04, length)


def HIDIOCSFEATURE(length: int) -> int:
    return _IOC(_IOC_READ | _IOC_WRITE, "H", 0x06, length)


def HIDIOCGFEATURE(length: int) -> int:
    return _IOC(_IOC_READ | _IOC_WRITE, "H", 0x07, length)


class TransportError(RuntimeError):
    pass


@dataclass
class HidrawDevice:
    path: str
    bustype: int
    vid: int
    pid: int
    name: str

    @property
    def usb_id(self) -> str:
        return "%04x:%04x" % (self.vid, self.pid)

    def writable(self) -> bool:
        return os.access(self.path, os.R_OK | os.W_OK)

    def __str__(self) -> str:
        perm = "rw" if self.writable() else "ro/denied"
        return "%s  %s  %s  (%s)" % (self.path, self.usb_id, self.name or "?", perm)


def _read_c_string(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")


def probe_node(path: str) -> Optional[HidrawDevice]:
    """Open a hidraw node and read its ids. Returns None off Linux, or on any error."""
    if fcntl is None:
        # Windows has neither hidraw nodes nor fcntl; callers get a clean None
        # instead of an AttributeError from a platform-specific os flag.
        return None
    nonblock = getattr(os, "O_NONBLOCK", 0)
    fd = None
    try:
        fd = os.open(path, os.O_RDWR | nonblock)
    except OSError:
        try:
            fd = os.open(path, os.O_RDONLY | nonblock)
        except OSError:
            fd = None
    if fd is None:
        return None
    try:
        try:
            info = fcntl.ioctl(fd, HIDIOCGRAWINFO(), bytes(8))
            bustype, vid, pid = struct.unpack("<Ihh", info)
        except OSError:
            bustype, vid, pid = 0, 0, 0
        try:
            name = _read_c_string(fcntl.ioctl(fd, HIDIOCGRAWNAME(128), bytes(128)))
        except OSError:
            name = ""
        return HidrawDevice(path=path, bustype=bustype, vid=vid & 0xFFFF, pid=pid & 0xFFFF, name=name)
    finally:
        os.close(fd)


def list_hidraw() -> List[HidrawDevice]:
    """Enumerate hidraw nodes. Returns an empty list off Linux."""
    if sys.platform != "linux" or fcntl is None:
        return []
    devices: List[HidrawDevice] = []
    for node in sorted(Path("/dev").glob("hidraw*")):
        device = probe_node(str(node))
        if device is not None:
            devices.append(device)
    devices.sort(key=lambda d: d.path)
    return devices


def sysfs_vendor_product(path: str) -> Optional[str]:
    """Best-effort sysfs lookup of ``vvvv:pppp`` for a hidraw node (no open needed)."""
    name = Path(path).name
    uevent = Path("/sys/class/hidraw") / name / "device" / "uevent"
    try:
        for line in uevent.read_text().splitlines():
            if line.startswith("HID_ID="):
                parts = line.split("=", 1)[1].split(":")
                if len(parts) == 3:
                    return "%s:%s" % (parts[1][-4:].lower(), parts[2][-4:].lower())
    except OSError:
        return None
    return None


def find_hidraw(vid: int, pid: int, fallback_to_vid_only: bool = False) -> List[HidrawDevice]:
    matches = [d for d in list_hidraw() if d.vid == vid and d.pid == pid]
    if not matches and fallback_to_vid_only:
        matches = [d for d in list_hidraw() if d.vid == vid]
    return matches


class HidrawTransport:
    def __init__(self, device: HidrawDevice, delay: float = DEFAULT_DELAY):
        if fcntl is None:
            raise TransportError(
                "hidraw ioctls are only available on Linux (this is %s); "
                "use --transport dry-run here, or run the tool on the target OS"
                % sys.platform
            )
        self.device = device
        self.delay = delay
        self.fd = os.open(device.path, os.O_RDWR)
        self._last: Optional[bytes] = None
        # hidraw treats buffer[0] as the HID report number. GG's own traffic on this
        # model addresses report 0 and puts the opcode in the payload instead, so if
        # the kernel/firmware rejects the opcode-as-report-number form we retry once
        # with a 0x00 report number, which reproduces GG's transfer exactly.
        self.report_prefix = b""

    def describe(self) -> str:
        mode = "" if not self.report_prefix else ", report id 0"
        return "hidraw %s (%s %s%s)" % (
            self.device.path, self.device.usb_id, self.device.name, mode
        )

    @staticmethod
    def _rejected_address(exc: OSError) -> bool:
        return exc.errno in (errno.EINVAL, errno.ENOTTY, errno.EPIPE, errno.ENOSYS)

    def _feature(self, data: bytes) -> None:
        buffer = bytearray(self.report_prefix + data)
        fcntl.ioctl(self.fd, HIDIOCSFEATURE(len(buffer)), buffer, True)

    def send_feature_report(self, data: bytes) -> None:
        try:
            self._feature(data)
        except OSError as exc:
            if self.report_prefix or not self._rejected_address(exc):
                raise TransportError(
                    "%s rejected a %d-byte feature report: %s%s"
                    % (self.device.path, len(data), exc, self._hint())
                ) from exc
            self.report_prefix = b"\x00"
            try:
                self._feature(data)
            except OSError as retry_exc:
                raise TransportError(
                    "%s rejected a %d-byte feature report both as report 0x%02x and as "
                    "report 0: %s"
                    % (self.device.path, len(data), data[0] if data else 0, retry_exc)
                ) from retry_exc
            print(
                "note: %s wants report id 0 (vendor convention); retried and accepted"
                % self.device.path,
                file=sys.stderr,
            )
        self._last = bytes(data)
        time.sleep(self.delay)

    @staticmethod
    def _hint() -> str:
        return (
            "\n  - EINVAL here usually means the report number is not the one the "
            "device uses; re-run with `--transport libusb --interface N` to address "
            "the device directly"
        )

    def send_output_report(self, data: bytes) -> None:
        # hidraw write() takes the report id as the first byte, matching hidapi.
        try:
            written = os.write(self.fd, self.report_prefix + data)
        except OSError as exc:
            if self.report_prefix or not self._rejected_address(exc):
                raise TransportError(
                    "%s rejected the %d-byte commit report: %s"
                    % (self.device.path, len(data), exc)
                ) from exc
            self.report_prefix = b"\x00"
            written = os.write(self.fd, self.report_prefix + data)
        expected = len(self.report_prefix) + len(data)
        if written != expected:
            raise TransportError(
                "%s accepted %d of %d output bytes" % (self.device.path, written, expected)
            )
        self._last = bytes(data)
        time.sleep(self.delay)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass

    def __enter__(self) -> "HidrawTransport":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class LibusbTransport:
    """Control-transfer backend for interfaces with no usable hidraw node."""

    SET_REPORT = 0x09
    HID_FEATURE, HID_OUTPUT = 0x03, 0x02
    HID_CLASS, VENDOR_CLASS = 0x03, 0xFF

    def __init__(self, vid: int, pid: int, interface: Optional[int] = None,
                 delay: float = DEFAULT_DELAY, report_id: int = 0):
        try:
            import usb.core  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on host packages
            raise TransportError(
                "pyusb is not installed (pip install pyusb), or use the hidraw backend"
            ) from exc
        self._usb = usb.core
        self.vid = vid
        self.pid = pid
        self.delay = delay
        # GG addresses report 0 on this controller (wValue 0x0300 / 0x0200); the
        # report descriptor declares no report ids at all. Override with --report-id
        # if some other firmware expects the opcode in wValue.
        self.report_id = report_id & 0xFF
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is None:
            raise TransportError("no USB device %04x:%04x on the bus" % (vid, pid))
        self.dev = dev
        self.interface = interface if interface is not None else self._pick_interface(dev)

    def _pick_interface(self, dev) -> int:
        candidates: List[int] = []
        try:
            config = dev.get_active_configuration()
        except Exception:
            dev.set_configuration()
            config = dev.get_active_configuration()
        for iface in config:
            if iface.bInterfaceClass in (self.HID_CLASS, self.VENDOR_CLASS):
                candidates.append(iface.bInterfaceNumber)
        if not candidates:
            raise TransportError("device exposes no HID/vendor interface to talk to")
        return candidates[0]

    def describe(self) -> str:
        return "libusb %04x:%04x interface %d (report id 0x%02x)" % (
            self.vid, self.pid, self.interface, self.report_id
        )

    def _detach(self, interface: int) -> None:
        try:
            if self.dev.is_kernel_driver_active(interface):
                self.dev.detach_kernel_driver(interface)
        except Exception:
            pass

    def _set_report(self, report_type: int, data: bytes) -> None:
        self._detach(self.interface)
        self.dev.ctrl_transfer(
            0x21,
            self.SET_REPORT,
            (report_type << 8) | self.report_id,
            self.interface,
            data,
            timeout=1000,
        )
        time.sleep(self.delay)

    def send_feature_report(self, data: bytes) -> None:
        self._set_report(self.HID_FEATURE, data)

    def send_output_report(self, data: bytes) -> None:
        self._set_report(self.HID_OUTPUT, data)

    def close(self) -> None:
        pass

    def __enter__(self) -> "LibusbTransport":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class NullTransport:
    """Records packets instead of sending them; used by tests and ``--dry-run``."""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.feature_reports: List[bytes] = []
        self.output_reports: List[bytes] = []

    def describe(self) -> str:
        return "dry-run (nothing is written to hardware)"

    def send_feature_report(self, data: bytes) -> None:
        self.feature_reports.append(bytes(data))
        time.sleep(self.delay)

    def send_output_report(self, data: bytes) -> None:
        self.output_reports.append(bytes(data))
        time.sleep(self.delay)

    def close(self) -> None:
        pass

    def __enter__(self) -> "NullTransport":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
