#!/usr/bin/env python3
"""Windows hardware smoke test for the shared USBInterface."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "common"))

from channel_defs import CHANNEL_CONTROL, MSG_PING, MSG_PONG
from usbInterface import USBInterface

usb = USBInterface(timeout_ms=100)
try:
    print("Opening gateway (Windows selects HID and sends HID_ENABLE)...")
    usb.open()
    print("  transport:", usb.transport)
    assert usb.transport == "hid"

    print("PING over shared USBInterface...")
    usb.send(CHANNEL_CONTROL, MSG_PING)
    ch, typ, payload = usb.read_frame(timeout_ms=2000)
    assert (ch, typ, payload) == (CHANNEL_CONTROL, MSG_PONG, b"AS3U\x01"), (ch, typ, payload)
    print("  OK: PONG over HID")
    print("PASS: shared USBInterface Windows HID transport")
finally:
    usb.close()
