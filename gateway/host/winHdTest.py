#!/usr/bin/env python3
"""Windows HID smoke test for the gateway transport multiplexer.

The device defaults to the custom bulk transport.  HID_ENABLE is the only
protocol frame accepted on HID while bulk is active.  After its status ACK,
normal framed traffic uses HID.

HID transport reports are 64 bytes: byte 0 is the valid stream-byte count
(0..63), followed by up to 63 bytes of the existing gateway byte stream.
"""

import os
import sys
import time
import hid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common"))
from channel_defs import (
    CHANNEL_CONTROL,
    MSG_COMMAND,
    MSG_PING,
    MSG_PONG,
    MSG_STATUS,
    CTRL_HID_ENABLE,
)

VID = 0x303A
PID = 0x4001
HID_INTERFACE = 3
HID_USAGE_PAGE = 0xFF00
REPORT_SIZE = 64
DATA_SIZE = 63


def make_frame(channel, msg_type, payload=b""):
    return bytes((
        channel,
        msg_type,
        len(payload) & 0xFF,
        (len(payload) >> 8) & 0xFF,
    )) + bytes(payload)


def find_device():
    devices = hid.enumerate(VID, PID)
    for info in devices:
        if (
            info.get("interface_number") == HID_INTERFACE
            and info.get("usage_page") == HID_USAGE_PAGE
        ):
            return info
    raise RuntimeError("gateway HID interface not found")


class HIDStream:
    def __init__(self, dev):
        self.dev = dev
        self.pending = bytearray()

    def write(self, data):
        data = bytes(data)
        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + DATA_SIZE]
            report = bytes((len(chunk),)) + chunk + bytes(DATA_SIZE - len(chunk))
            # hidapi write buffer starts with report ID 0.  The actual USB HID
            # report which reaches EP 0x04 is the following 64 bytes.
            written = self.dev.write(b"\x00" + report)
            if written <= 0:
                raise RuntimeError("HID write failed")
            offset += len(chunk)

    def _read_report(self, timeout_ms):
        raw = bytes(self.dev.read(REPORT_SIZE, timeout_ms=timeout_ms))
        if len(raw) != REPORT_SIZE:
            raise RuntimeError("HID read timeout/short report: %d" % len(raw))
        count = raw[0]
        if count > DATA_SIZE:
            raise RuntimeError("invalid HID stream count %d" % count)
        self.pending.extend(raw[1:1 + count])

    def read_frame(self, timeout_ms=2000):
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            if len(self.pending) >= 4:
                length = self.pending[2] | (self.pending[3] << 8)
                total = 4 + length
                if len(self.pending) >= total:
                    raw = bytes(self.pending[:total])
                    del self.pending[:total]
                    return raw[0], raw[1], raw[4:]
            remaining = int(max(1, (deadline - time.monotonic()) * 1000))
            if time.monotonic() >= deadline:
                raise RuntimeError("timeout waiting for framed HID response")
            self._read_report(remaining)


info = find_device()
print("HID:", info.get("path"))

dev = hid.device()
dev.open_path(info["path"])
stream = HIDStream(dev)

try:
    print("HID_ENABLE...")
    stream.write(make_frame(
        CHANNEL_CONTROL,
        MSG_COMMAND,
        bytes((CTRL_HID_ENABLE,)),
    ))
    ch, typ, payload = stream.read_frame()
    assert (ch, typ) == (CHANNEL_CONTROL, MSG_STATUS), (ch, typ, payload)
    print("  OK: status ACK received over HID")

    print("PING over HID...")
    stream.write(make_frame(CHANNEL_CONTROL, MSG_PING))
    ch, typ, payload = stream.read_frame()
    assert (ch, typ, payload) == (
        CHANNEL_CONTROL,
        MSG_PONG,
        b"AS3U\x01",
    ), (ch, typ, payload)
    print("  OK: PONG over HID")

    print("PASS: custom default -> HID_ENABLE -> framed HID traffic")
finally:
    dev.close()
