# SPDX-License-Identifier: AGPL-3.0-only
# boot.py -- CDC REPL + vendor bulk + raw HID transport

import time
import machine

from usb_transport_mux import USBTransportMux

USB = machine.USBDevice
USBD = USB()

VENDOR_INTERFACE = 2
VENDOR_EP_OUT = 0x03
VENDOR_EP_IN = 0x83
HID_INTERFACE = 3
HID_EP_OUT = 0x04
HID_EP_IN = 0x84
HID_REPORT_SIZE = 64

# Logical endpoints seen only by USBChannelServer through USBTransportMux.
LOGICAL_EP_OUT = 0x01
LOGICAL_EP_IN = 0x81

_server = None
_vendor_interface_open = False

HID_REPORT_DESCRIPTOR = bytes((
    0x06, 0x00, 0xFF,       # Usage Page (Vendor Defined 0xFF00)
    0x09, 0x01,             # Usage (1)
    0xA1, 0x01,             # Collection (Application)
    0x15, 0x00,             # Logical Minimum (0)
    0x26, 0xFF, 0x00,       # Logical Maximum (255)
    0x75, 0x08,             # Report Size = 8 bits
    0x95, HID_REPORT_SIZE,  # Report Count = 64
    0x09, 0x01,
    0x81, 0x02,             # Input
    0x95, HID_REPORT_SIZE,
    0x09, 0x02,
    0x91, 0x02,             # Output
    0xC0,
))

USB_REQ_GET_DESCRIPTOR = 0x06
HID_DESC_TYPE_REPORT = 0x22
HID_REQ_GET_REPORT = 0x01
HID_REQ_GET_IDLE = 0x02
HID_REQ_GET_PROTOCOL = 0x03
HID_REQ_SET_REPORT = 0x09
HID_REQ_SET_IDLE = 0x0A
HID_REQ_SET_PROTOCOL = 0x0B

_hid_protocol = 1
_hid_idle = 0

_mux = USBTransportMux(
    USBD,
    custom_ep_out=VENDOR_EP_OUT,
    custom_ep_in=VENDOR_EP_IN,
    hid_ep_out=HID_EP_OUT,
    hid_ep_in=HID_EP_IN,
    logical_ep_out=LOGICAL_EP_OUT,
    logical_ep_in=LOGICAL_EP_IN,
)


def usb_transport():
    """Return 'custom' or 'hid' for REPL diagnostics."""
    return _mux.active_name()


def _open_interface(descriptor):
    global _vendor_interface_open
    raw = bytes(descriptor)
    if len(raw) < 3 or raw[1] != 0x04:
        return
    interface = raw[2]
    if interface not in (VENDOR_INTERFACE, HID_INTERFACE):
        return
    _mux.on_interface_open(interface)
    if interface == VENDOR_INTERFACE:
        _vendor_interface_open = True
        if _server is not None and not _server.interface_open:
            _server.on_interface_open()


def _usb_reset():
    global _vendor_interface_open
    _vendor_interface_open = False
    _mux.on_usb_reset()
    if _server is not None:
        _server.on_usb_reset()


def _transfer_complete(endpoint, result, transferred):
    _mux.on_transfer_complete(endpoint, result, transferred)


def _control_xfer(stage, request):
    global _hid_protocol, _hid_idle
    raw = bytes(request)
    if len(raw) < 8:
        return True

    bm = raw[0]
    req = raw[1]
    value = raw[2] | (raw[3] << 8)
    index = raw[4] | (raw[5] << 8)
    length = raw[6] | (raw[7] << 8)

    if index != HID_INTERFACE or stage != 1:
        return True

    if bm == 0x81 and req == USB_REQ_GET_DESCRIPTOR and (value >> 8) == HID_DESC_TYPE_REPORT:
        return HID_REPORT_DESCRIPTOR[:length]
    if bm == 0xA1 and req == HID_REQ_GET_REPORT:
        return bytes(min(length, HID_REPORT_SIZE))
    if bm == 0xA1 and req == HID_REQ_GET_IDLE:
        return bytes((_hid_idle,))
    if bm == 0xA1 and req == HID_REQ_GET_PROTOCOL:
        return bytes((_hid_protocol,))
    if bm == 0x21 and req == HID_REQ_SET_IDLE:
        _hid_idle = (value >> 8) & 0xFF
        return True
    if bm == 0x21 and req == HID_REQ_SET_PROTOCOL:
        _hid_protocol = value & 0xFF
        return True
    if bm == 0x21 and req == HID_REQ_SET_REPORT:
        return True
    return True


def _configure_usb():
    builtin = USB.BUILTIN_CDC
    USBD.active(False)
    USBD.builtin_driver = builtin

    if builtin.itf_max != VENDOR_INTERFACE or builtin.ep_max != 3:
        raise RuntimeError(
            "unexpected USB allocation: itf_max=%d ep_max=%d"
            % (builtin.itf_max, builtin.ep_max)
        )

    vendor_descriptor = bytes((
        0x09, 0x04, VENDOR_INTERFACE, 0x00, 0x02, 0xFF, 0x00, 0x00, 0x00,
        0x07, 0x05, VENDOR_EP_OUT, 0x02, 0x40, 0x00, 0x00,
        0x07, 0x05, VENDOR_EP_IN,  0x02, 0x40, 0x00, 0x00,
    ))

    hid_descriptor = bytes((
        0x09, 0x04, HID_INTERFACE, 0x00, 0x02, 0x03, 0x00, 0x00, 0x00,
        0x09, 0x21, 0x11, 0x01, 0x00, 0x01, 0x22,
        len(HID_REPORT_DESCRIPTOR) & 0xFF,
        (len(HID_REPORT_DESCRIPTOR) >> 8) & 0xFF,
        0x07, 0x05, HID_EP_OUT, 0x03, 0x40, 0x00, 0x01,
        0x07, 0x05, HID_EP_IN,  0x03, 0x40, 0x00, 0x01,
    ))

    descriptor = bytearray(builtin.desc_cfg)
    descriptor.extend(vendor_descriptor)
    descriptor.extend(hid_descriptor)
    total_length = len(descriptor)
    descriptor[2] = total_length & 0xFF
    descriptor[3] = (total_length >> 8) & 0xFF
    descriptor[4] = HID_INTERFACE + 1

    USBD.config(
        builtin.desc_dev,
        descriptor,
        {},
        _open_interface,
        _usb_reset,
        _control_xfer,
        _transfer_complete,
    )
    USBD.active(True)


_configure_usb()
time.sleep_ms(250)

import usb_channel_server

_server = usb_channel_server.USBChannelServer(
    _mux,
    interface=VENDOR_INTERFACE,
    ep_out=LOGICAL_EP_OUT,
    ep_in=LOGICAL_EP_IN,
)
_mux.set_complete_callback(_server.on_transfer_complete)
usb_channel_server.set_default_gateway(_server)

if _vendor_interface_open:
    _server.on_interface_open()
