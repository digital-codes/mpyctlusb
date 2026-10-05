# SPDX-License-Identifier: AGPL-3.0-only
"""USB bulk/HID transport multiplexer for usb_channel_server.

The channel server sees one logical endpoint pair.  This module keeps the
existing vendor bulk interface alive and adds a raw HID transport.  Bulk is
the default after boot/reset.  The inactive transport only accepts its switch
command:

    HID:    CTRL_HID_ENABLE  -> switch to HID
    custom: CTRL_HID_DISABLE -> switch to custom bulk

HID reports use one byte of transport framing: byte 0 is the number of valid
stream bytes (0..63), bytes 1..63 carry the framed channel byte stream.
"""

from channel_defs import (
    CHANNEL_CONTROL,
    MSG_COMMAND,
    CTRL_HID_ENABLE,
    CTRL_HID_DISABLE,
)

TRANSPORT_CUSTOM = 0
TRANSPORT_HID = 1
HID_REPORT_SIZE = 64
HID_DATA_SIZE = HID_REPORT_SIZE - 1


class USBTransportMux:
    """USBD-compatible proxy used by USBChannelServer."""

    def __init__(
        self,
        usbd,
        custom_ep_out=0x03,
        custom_ep_in=0x83,
        hid_ep_out=0x04,
        hid_ep_in=0x84,
        logical_ep_out=0x01,
        logical_ep_in=0x81,
        rx_size=4096,
    ):
        self.usbd = usbd
        self.custom_ep_out = custom_ep_out
        self.custom_ep_in = custom_ep_in
        self.hid_ep_out = hid_ep_out
        self.hid_ep_in = hid_ep_in
        self.logical_ep_out = logical_ep_out
        self.logical_ep_in = logical_ep_in

        self.mode = TRANSPORT_CUSTOM
        self.complete = None
        self.custom_open = False
        self.hid_open = False

        self.logical_rx = None
        self.custom_rx = bytearray(rx_size)
        self.hid_rx = bytearray(HID_REPORT_SIZE)
        self.custom_rx_armed = False
        self.hid_rx_armed = False

        self.logical_tx = None
        self.logical_tx_length = 0
        self.logical_tx_offset = 0
        self.physical_tx_ep = None
        self.physical_tx_length = 0
        self.hid_tx_report = bytearray(HID_REPORT_SIZE)

    def active_name(self):
        return "hid" if self.mode == TRANSPORT_HID else "custom"

    def set_complete_callback(self, callback):
        self.complete = callback

    def on_interface_open(self, interface):
        if interface == 2:
            self.custom_open = True
        elif interface == 3:
            self.hid_open = True
        self._arm_physical_outs()

    def on_usb_reset(self):
        self.mode = TRANSPORT_CUSTOM
        self.custom_open = False
        self.hid_open = False
        self.logical_rx = None
        self.custom_rx_armed = False
        self.hid_rx_armed = False
        self.logical_tx = None
        self.logical_tx_length = 0
        self.logical_tx_offset = 0
        self.physical_tx_ep = None
        self.physical_tx_length = 0

    def submit_xfer(self, endpoint, buf):
        """Subset of machine.USBDevice.submit_xfer used by the server."""
        if endpoint == self.logical_ep_out:
            self.logical_rx = buf
            self._arm_physical_outs()
            return True

        if endpoint == self.logical_ep_in:
            if self.logical_tx is not None:
                return False
            self.logical_tx = buf
            self.logical_tx_length = len(buf)
            self.logical_tx_offset = 0
            return self._submit_next_in()

        raise ValueError("unknown logical endpoint 0x%02x" % endpoint)

    @staticmethod
    def _success(result):
        return result is True or result == 0

    def _arm_physical_outs(self):
        # Keep both OUT endpoints armed.  The inactive endpoint is needed for
        # the command which switches back to it.
        if self.custom_open and not self.custom_rx_armed:
            try:
                self.custom_rx_armed = bool(
                    self.usbd.submit_xfer(self.custom_ep_out, self.custom_rx)
                )
            except Exception:
                self.custom_rx_armed = False

        if self.hid_open and not self.hid_rx_armed:
            try:
                self.hid_rx_armed = bool(
                    self.usbd.submit_xfer(self.hid_ep_out, self.hid_rx)
                )
            except Exception:
                self.hid_rx_armed = False

    def _is_switch_frame(self, data, command):
        return (
            len(data) == 5
            and data[0] == CHANNEL_CONTROL
            and data[1] == MSG_COMMAND
            and data[2] == 1
            and data[3] == 0
            and data[4] == command
        )

    def _deliver_rx(self, data):
        if self.logical_rx is None or self.complete is None:
            return
        n = min(len(data), len(self.logical_rx))
        self.logical_rx[:n] = data[:n]
        self.logical_rx = None
        self.complete(self.logical_ep_out, 0, n)

    def _handle_custom_out(self, result, transferred):
        self.custom_rx_armed = False
        if self._success(result):
            data = bytes(self.custom_rx[:transferred])
            if self.mode == TRANSPORT_CUSTOM:
                self._deliver_rx(data)
            elif self._is_switch_frame(data, CTRL_HID_DISABLE):
                self.mode = TRANSPORT_CUSTOM
                self._deliver_rx(data)
        self._arm_physical_outs()

    def _handle_hid_out(self, result, transferred):
        self.hid_rx_armed = False
        if self._success(result) and transferred:
            count = self.hid_rx[0]
            if count <= HID_DATA_SIZE and count + 1 <= transferred:
                data = bytes(self.hid_rx[1:1 + count])
                if self.mode == TRANSPORT_HID:
                    self._deliver_rx(data)
                elif self._is_switch_frame(data, CTRL_HID_ENABLE):
                    self.mode = TRANSPORT_HID
                    self._deliver_rx(data)
        self._arm_physical_outs()

    def _submit_next_in(self):
        if self.logical_tx is None:
            return False

        remaining = self.logical_tx_length - self.logical_tx_offset
        if remaining <= 0:
            self._finish_logical_in(0)
            return True

        if self.mode == TRANSPORT_CUSTOM:
            ep = self.custom_ep_in
            chunk = self.logical_tx[self.logical_tx_offset:self.logical_tx_length]
            physical_length = len(chunk)
        else:
            ep = self.hid_ep_in
            count = min(remaining, HID_DATA_SIZE)
            self.hid_tx_report[0] = count
            self.hid_tx_report[1:1 + count] = self.logical_tx[
                self.logical_tx_offset:self.logical_tx_offset + count
            ]
            if count < HID_DATA_SIZE:
                self.hid_tx_report[1 + count:] = bytes(HID_DATA_SIZE - count)
            chunk = self.hid_tx_report
            physical_length = count

        self.physical_tx_ep = ep
        self.physical_tx_length = physical_length
        try:
            queued = bool(self.usbd.submit_xfer(ep, chunk))
        except Exception:
            self._finish_logical_in(1)
            return False

        # A synchronous callback may already have completed the logical TX.
        if not queued and self.logical_tx is not None:
            self._finish_logical_in(1)
        return queued

    def _finish_logical_in(self, result):
        length = self.logical_tx_offset if self._success(result) else 0
        self.logical_tx = None
        self.logical_tx_length = 0
        self.logical_tx_offset = 0
        self.physical_tx_ep = None
        self.physical_tx_length = 0
        if self.complete is not None:
            self.complete(self.logical_ep_in, result, length)

    def _handle_physical_in(self, endpoint, result, transferred):
        if self.logical_tx is None or endpoint != self.physical_tx_ep:
            return
        if not self._success(result):
            self._finish_logical_in(result)
            return

        # For HID, transferred is 64 physical bytes but only count bytes are
        # channel-stream data.  For bulk they are identical.
        self.logical_tx_offset += self.physical_tx_length
        self.physical_tx_ep = None
        self.physical_tx_length = 0

        if self.logical_tx_offset >= self.logical_tx_length:
            self._finish_logical_in(0)
        else:
            self._submit_next_in()

    def on_transfer_complete(self, endpoint, result, transferred):
        if endpoint == self.custom_ep_out:
            self._handle_custom_out(result, transferred)
        elif endpoint == self.hid_ep_out:
            self._handle_hid_out(result, transferred)
        elif endpoint in (self.custom_ep_in, self.hid_ep_in):
            self._handle_physical_in(endpoint, result, transferred)
