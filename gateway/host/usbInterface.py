#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Uniform host USB transport for the mpyctlusb gateway.

Linux uses the vendor-specific bulk interface (MI_02). Windows uses the
vendor-defined HID interface (MI_03), sends CTRL_HID_ENABLE during open(), and
then carries the exact same framed byte stream in 64-byte HID reports.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
COMMON = os.path.normpath(os.path.join(HERE, "..", "common"))
if COMMON not in sys.path:
    sys.path.insert(0, COMMON)

from channel_defs import (
    CHANNEL_CONTROL, MSG_COMMAND, MSG_STATUS, MSG_PING, MSG_PONG,
    CTRL_HID_ENABLE, CTRL_HID_DISABLE, MTU_USB,
)

VID = 0x303A
PID = 0x4001
BULK_INTERFACE = 2
BULK_EP_OUT = 0x03
BULK_EP_IN = 0x83
HID_INTERFACE = 3
HID_USAGE_PAGE = 0xFF00
HID_REPORT_SIZE = 64
HID_DATA_SIZE = 63


class ProtocolError(Exception):
    pass


class USBDisconnected(Exception):
    pass


class FrameParser:
    def __init__(self, max_payload=MTU_USB):
        self.buffer = bytearray()
        self.max_payload = max_payload

    def feed(self, data):
        self.buffer.extend(data)
        frames = []
        while len(self.buffer) >= 4:
            channel = self.buffer[0]
            msg_type = self.buffer[1]
            length = self.buffer[2] | (self.buffer[3] << 8)
            if channel == 255 or length > self.max_payload:
                self.buffer.clear()
                raise ProtocolError("invalid header channel=%d length=%d" % (channel, length))
            total = 4 + length
            if len(self.buffer) < total:
                break
            payload = bytes(self.buffer[4:total])
            del self.buffer[:total]
            frames.append((channel, msg_type, payload))
        return frames


def make_frame(channel, msg_type, payload=b""):
    payload = bytes(payload)
    if len(payload) > MTU_USB:
        raise ValueError("payload exceeds MTU_USB (%d > %d)" % (len(payload), MTU_USB))
    return bytes((channel, msg_type, len(payload) & 0xFF, len(payload) >> 8)) + payload


class USBInterface:
    """Cross-platform gateway interface.

    Public API: open(), close(), send(), read_frame(), read_until(), ping().

    MTU_USB is the USBChannel payload limit on both transports.  HID's smaller
    63-byte data area is a physical transport chunk size below that protocol
    MTU; one USBChannel frame is transparently spread across multiple reports.
    With threaded=True, received frames are instead published on .events as
    ("frame", (channel, msg_type, payload)); reader failures use ("error", text).
    """
    def __init__(self, serial=None, timeout_ms=100, threaded=False):
        self.serial = serial
        self.timeout_ms = timeout_ms
        self.threaded = threaded
        self.transport = None
        self.dev = None
        self.claimed = False
        self.parser = FrameParser()
        self.write_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.reader_thread = None
        self.events = queue.Queue(maxsize=256)
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.errors = 0
        self.reader_alive = False
        self.reader_last_error = ""

    def open(self):
        if self.dev is not None:
            return
        if sys.platform == "win32":
            self._open_hid()
        else:
            self._open_bulk()
        if self.threaded:
            self.stop_event.clear()
            self.reader_thread = threading.Thread(target=self._reader, daemon=True)
            self.reader_thread.start()

    def _open_hid(self):
        try:
            import hid
        except ImportError as exc:
            raise USBDisconnected("Windows HID transport requires 'hidapi'") from exc
        info = None
        for candidate in hid.enumerate(VID, PID):
            if candidate.get("interface_number") != HID_INTERFACE:
                continue
            if candidate.get("usage_page") != HID_USAGE_PAGE:
                continue
            if self.serial is not None and candidate.get("serial_number") != self.serial:
                continue
            info = candidate
            break
        if info is None:
            raise USBDisconnected("gateway HID interface not found")
        dev = hid.device()
        try:
            dev.open_path(info["path"])
            self.dev = dev
            self.transport = "hid"
            # Device resets to custom bulk. The inactive HID path accepts this
            # one command and switches the gateway before returning its ACK.
            self._write_stream(make_frame(CHANNEL_CONTROL, MSG_COMMAND, bytes((CTRL_HID_ENABLE,))))
            ch, typ, _ = self.read_frame(timeout_ms=2000)
            if (ch, typ) != (CHANNEL_CONTROL, MSG_STATUS):
                raise ProtocolError("unexpected HID_ENABLE response ch=%d type=%d" % (ch, typ))
        except Exception:
            try:
                dev.close()
            finally:
                self.dev = None
                self.transport = None
            raise

    def _open_bulk(self):
        try:
            import usb.core
            import usb.util
        except ImportError as exc:
            raise USBDisconnected("bulk transport requires 'pyusb'") from exc
        devices = usb.core.find(find_all=True, idVendor=VID, idProduct=PID)
        dev = None
        for candidate in devices:
            if self.serial is None:
                dev = candidate
                break
            try:
                if usb.util.get_string(candidate, candidate.iSerialNumber) == self.serial:
                    dev = candidate
                    break
            except usb.core.USBError:
                continue
        if dev is None:
            raise USBDisconnected("device not found")
        try:
            dev.set_configuration()
        except usb.core.USBError as exc:
            if getattr(exc, "errno", None) != 16:
                raise USBDisconnected(str(exc)) from exc
        if dev.is_kernel_driver_active(BULK_INTERFACE):
            raise RuntimeError("kernel driver owns interface 2")
        usb.util.claim_interface(dev, BULK_INTERFACE)
        self.dev = dev
        self.claimed = True
        self.transport = "bulk"
        # Also recover cleanly if the same powered device was previously put
        # into HID mode. Custom accepts HID_DISABLE even while inactive.
        self._write_stream(make_frame(CHANNEL_CONTROL, MSG_COMMAND, bytes((CTRL_HID_DISABLE,))))
        ch, typ, _ = self.read_frame(timeout_ms=2000)
        if (ch, typ) != (CHANNEL_CONTROL, MSG_STATUS):
            self.close()
            raise ProtocolError("unexpected HID_DISABLE response ch=%d type=%d" % (ch, typ))

    def close(self):
        self.stop_event.set()
        if self.reader_thread is not None:
            self.reader_thread.join(timeout=1.0)
            self.reader_thread = None
        dev = self.dev
        transport = self.transport
        self.dev = None
        self.transport = None
        if dev is None:
            return
        if transport == "hid":
            dev.close()
        else:
            import usb.util
            try:
                if self.claimed:
                    usb.util.release_interface(dev, BULK_INTERFACE)
            finally:
                self.claimed = False
                usb.util.dispose_resources(dev)

    def send(self, channel, msg_type, payload=b""):
        frame = make_frame(channel, msg_type, payload)
        with self.write_lock:
            self._write_stream(frame)
        self.tx_bytes += len(frame)

    def _write_stream(self, data):
        if self.dev is None:
            raise USBDisconnected("device is not open")
        data = bytes(data)
        try:
            if self.transport == "hid":
                offset = 0
                while offset < len(data):
                    chunk = data[offset:offset + HID_DATA_SIZE]
                    report = bytes((len(chunk),)) + chunk + bytes(HID_DATA_SIZE - len(chunk))
                    written = self.dev.write(b"\x00" + report)
                    if written <= 0:
                        raise USBDisconnected("HID write failed")
                    offset += len(chunk)
            else:
                written = self.dev.write(BULK_EP_OUT, data, timeout=1000)
                if written != len(data):
                    raise USBDisconnected("short write %d/%d" % (written, len(data)))
        except USBDisconnected:
            raise
        except Exception as exc:
            raise USBDisconnected(str(exc)) from exc

    def _read_stream(self, timeout_ms):
        if self.dev is None:
            raise USBDisconnected("device is not open")
        try:
            if self.transport == "hid":
                raw = bytes(self.dev.read(HID_REPORT_SIZE, timeout_ms=timeout_ms))
                if not raw:
                    return b""
                if len(raw) != HID_REPORT_SIZE:
                    raise ProtocolError("short HID report: %d" % len(raw))
                count = raw[0]
                if count > HID_DATA_SIZE:
                    raise ProtocolError("invalid HID stream count %d" % count)
                return raw[1:1 + count]
            return bytes(self.dev.read(BULK_EP_IN, 4096, timeout=timeout_ms))
        except Exception as exc:
            # Both pyusb and hidapi signal timeouts differently. Empty HID reads
            # and PyUSB's USBTimeoutError are normal polling timeouts.
            if self.transport == "bulk":
                try:
                    import usb.core
                    if isinstance(exc, usb.core.USBTimeoutError):
                        return b""
                except ImportError:
                    pass
            if isinstance(exc, ProtocolError):
                raise
            raise USBDisconnected(str(exc)) from exc

    def read_frame(self, timeout_ms=5000):
        if self.threaded and self.reader_thread is not None:
            raise RuntimeError("read_frame is unavailable while threaded reader is active")
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            frames = self.parser.feed(b"")
            if frames:
                return frames[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError("timeout waiting for response")
            data = self._read_stream(min(self.timeout_ms, max(1, int(remaining * 1000))))
            if data:
                self.rx_bytes += len(data)
                frames = self.parser.feed(data)
                if frames:
                    # read bursts can contain several frames. Put extras back as
                    # encoded bytes so the next read_frame returns them.
                    for ch, typ, payload in reversed(frames[1:]):
                        self.parser.buffer[:0] = make_frame(ch, typ, payload)
                    return frames[0]

    def read_until(self, predicate, timeout_ms=5000):
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise ProtocolError("timeout waiting for response")
            frame = self.read_frame(remaining)
            if predicate(*frame):
                return frame

    # compatibility for existing helper functions while applications migrate
    _read_until = read_until

    def ping(self, timeout_ms=5000):
        self.send(CHANNEL_CONTROL, MSG_PING)
        try:
            self.read_until(lambda ch, typ, payload: ch == CHANNEL_CONTROL and typ == MSG_PONG, timeout_ms)
            return True
        except ProtocolError:
            return False

    def _put_event(self, event):
        try:
            self.events.put_nowait(event)
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            self.events.put_nowait(event)

    def _reader(self):
        self.reader_alive = True
        try:
            while not self.stop_event.is_set():
                try:
                    data = self._read_stream(self.timeout_ms)
                    if not data:
                        continue
                    self.rx_bytes += len(data)
                    for frame in self.parser.feed(data):
                        self._put_event(("frame", frame))
                except ProtocolError as exc:
                    self.errors += 1
                    if not self.stop_event.is_set():
                        self._put_event(("error", "protocol: " + str(exc)))
                except USBDisconnected as exc:
                    self.errors += 1
                    if not self.stop_event.is_set():
                        self._put_event(("error", "usb: " + str(exc)))
                    return
        except Exception as exc:
            self.errors += 1
            self.reader_last_error = "%s: %s" % (type(exc).__name__, exc)
            if not self.stop_event.is_set():
                self._put_event(("error", "reader stopped: " + self.reader_last_error))
        finally:
            self.reader_alive = False
