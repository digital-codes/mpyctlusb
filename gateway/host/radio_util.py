#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# radio_util.py
#
# File transfer utility using ESP-NOW or WiFi radio.
#
# Usage:
#   python3 host/radio_util.py -e
#   python3 host/radio_util.py -w
#
# Options:
#   -e, --espnow   Use ESP-NOW radio (USB channel 3)
#   -w, --wifi     Use WiFi server (USB channel 4)
#
# Protocol:
#   - 16-bit header per packet: upper 4 bits = packet_id (0-15), lower 12 bits = segment
#   - Handshake: sender sends MSG_FILE_HANDSHAKE with (packet_id, total_size)
#   - Receiver responds with MSG_FILE_ACK to confirm buffer allocation
#   - Sender then transmits data packets with MSG_FILE_DATA
#   - Each data packet is echoed back by receiver (for verification)
#   - Packet size: ~4KB (4096 bytes payload + 2 bytes header = 4098 bytes)
#
# Channel management:
#   - Checks if required radio channel is loaded
#   - If opposite radio is loaded, prompts for confirmation to reset and reload

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time

import usb.core
import usb.util

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from channel_defs import (
    MSG_COMMAND,
    MSG_RESPONSE,
    MSG_EVENT,
    MSG_CHANNEL_LIST_REQUEST,
    MSG_CHANNEL_LIST_RESPONSE,
    MSG_ERROR,
    MSG_STATUS,
    MSG_FILE_HANDSHAKE,
    MSG_FILE_DATA,
    MSG_FILE_ACK,
    CHANNEL_CONTROL,
    CHANNEL_ESPNOW,
    CHANNEL_WIFI,
    CTRL_LOAD_CHANNEL,
    CTRL_RESET,
)

VID = 0x303A
PID = 0x4001
INTERFACE = 2
EP_OUT = 0x03
EP_IN = 0x83

MAX_PACKET_SIZE = 4096
HEADER_SIZE = 2
MAX_PACKET_ID = 15
MAX_SEGMENT = 4095


class ProtocolError(Exception):
    pass


class USBDisconnected(Exception):
    pass


class FrameParser:
    def __init__(self, max_payload=8192):
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
                raise ProtocolError(
                    "invalid header channel=%d length=%d"
                    % (channel, length)
                )

            total = 4 + length
            if len(self.buffer) < total:
                break

            payload = bytes(self.buffer[4:total])
            del self.buffer[:total]
            frames.append((channel, msg_type, payload))

        return frames


class USBGateway:
    def __init__(self, serial=None, timeout_ms=100):
        self.serial = serial
        self.timeout_ms = timeout_ms
        self.dev = None
        self.claimed = False
        self.stop_event = threading.Event()
        self.reader_thread = None
        self.write_lock = threading.Lock()
        self.events = queue.Queue(maxsize=256)
        self.parser = FrameParser()
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.errors = 0

    def _find(self):
        devices = usb.core.find(
            find_all=True,
            idVendor=VID,
            idProduct=PID,
        )
        for dev in devices:
            if self.serial is None:
                return dev
            try:
                if usb.util.get_string(dev, dev.iSerialNumber) == self.serial:
                    return dev
            except usb.core.USBError:
                continue
        return None

    def open(self):
        dev = self._find()
        if dev is None:
            raise USBDisconnected("device not found")

        try:
            dev.set_configuration()
        except usb.core.USBError as exc:
            if getattr(exc, "errno", None) != 16:
                raise

        if dev.is_kernel_driver_active(INTERFACE):
            raise RuntimeError("kernel driver owns interface 2")

        usb.util.claim_interface(dev, INTERFACE)
        self.dev = dev
        self.claimed = True
        self.stop_event.clear()
        self.reader_thread = threading.Thread(
            target=self._reader,
            daemon=True,
        )
        self.reader_thread.start()

    def close(self):
        self.stop_event.set()

        if self.reader_thread is not None:
            self.reader_thread.join(timeout=1.0)
            self.reader_thread = None

        dev = self.dev
        self.dev = None

        if dev is not None:
            try:
                if self.claimed:
                    usb.util.release_interface(dev, INTERFACE)
            finally:
                self.claimed = False
                usb.util.dispose_resources(dev)

    def send(self, channel, msg_type, payload=b""):
        if self.dev is None:
            raise USBDisconnected("device is not open")

        frame = (
            bytes((
                channel,
                msg_type,
                len(payload) & 0xFF,
                (len(payload) >> 8) & 0xFF,
            ))
            + payload
        )

        try:
            with self.write_lock:
                written = self.dev.write(
                    EP_OUT,
                    frame,
                    timeout=1000,
                )
            if written != len(frame):
                raise USBDisconnected(
                    "short write %d/%d" % (written, len(frame))
                )
            self.tx_bytes += written
        except usb.core.USBError as exc:
            self.errors += 1
            raise USBDisconnected(str(exc)) from exc

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
        try:
            while not self.stop_event.is_set():
                try:
                    data = bytes(
                        self.dev.read(
                            EP_IN,
                            8192,
                            timeout=self.timeout_ms,
                        )
                    )
                    self.rx_bytes += len(data)
                    for frame in self.parser.feed(data):
                        self._put_event(("frame", frame))
                except usb.core.USBTimeoutError:
                    continue
                except ProtocolError as exc:
                    self.errors += 1
                    if not self.stop_event.is_set():
                        self._put_event(("error", "protocol: " + str(exc)))
                    continue
                except usb.core.USBError as exc:
                    self.errors += 1
                    if not self.stop_event.is_set():
                        self._put_event(("error", "usb: " + str(exc)))
                    return
        except Exception as exc:
            self.errors += 1
            if not self.stop_event.is_set():
                self._put_event(("error", "reader stopped: %s: %s" % (type(exc).__name__, exc)))

    def drain_events(self, timeout=0.1):
        events = []
        while True:
            try:
                event = self.events.get(timeout=timeout)
                events.append(event)
            except queue.Empty:
                break
        return events


def get_header(packet_id, segment):
    return bytes([
        ((packet_id << 4) & 0xF0) | ((segment >> 8) & 0x0F),
        segment & 0xFF
    ])


def parse_header(header):
    b0, b1 = header[0], header[1]
    packet_id = (b0 >> 4) & 0x0F
    segment = ((b0 & 0x0F) << 8) | b1
    return packet_id, segment


def encode_handshake(packet_id, total_size):
    return bytes([packet_id]) + total_size.to_bytes(4, "little")


def parse_handshake(payload):
    if len(payload) < 5:
        raise ProtocolError("handshake payload too short")
    packet_id = payload[0]
    total_size = int.from_bytes(payload[1:5], "little")
    return packet_id, total_size


class RadioTransfer:
    def __init__(self, gateway, use_wifi=False, use_espnow=False):
        self.gateway = gateway
        self.use_wifi = use_wifi
        self.use_espnow = use_espnow
        self.wireless_channel = CHANNEL_WIFI if use_wifi else CHANNEL_ESPNOW

    def send_handshake(self, packet_id, total_size):
        payload = encode_handshake(packet_id, total_size)
        self.gateway.send(self.wireless_channel, MSG_FILE_HANDSHAKE, payload)

    def send_data_packet(self, packet_id, segment, data):
        header = get_header(packet_id, segment)
        payload = header + data
        self.gateway.send(self.wireless_channel, MSG_FILE_DATA, payload)

    def wait_for_ack(self, timeout_ms=5000):
        start = time.time()
        while (time.time() - start) * 1000 < timeout_ms:
            events = self.gateway.drain_events(timeout=0.1)
            for kind, data in events:
                if kind == "frame":
                    channel, msg_type, payload = data
                    if channel == self.wireless_channel and msg_type == MSG_FILE_ACK:
                        if len(payload) >= 5:
                            ack_packet_id = payload[0]
                            ack_segment = int.from_bytes(payload[1:3], "little")
                            ack_result = payload[3]
                            return ack_packet_id, ack_segment, ack_result
            time.sleep(0.01)
        raise ProtocolError("timeout waiting for ACK")

    def wait_for_handshake(self, timeout_ms=5000):
        start = time.time()
        while (time.time() - start) * 1000 < timeout_ms:
            events = self.gateway.drain_events(timeout=0.1)
            for kind, data in events:
                if kind == "frame":
                    channel, msg_type, payload = data
                    if channel == self.wireless_channel and msg_type == MSG_FILE_HANDSHAKE:
                        return parse_handshake(payload)
            time.sleep(0.01)
        raise ProtocolError("timeout waiting for handshake")

    def wait_for_data(self, timeout_ms=5000):
        start = time.time()
        while (time.time() - start) * 1000 < timeout_ms:
            events = self.gateway.drain_events(timeout=0.1)
            for kind, data in events:
                if kind == "frame":
                    channel, msg_type, payload = data
                    if channel == self.wireless_channel and msg_type == MSG_FILE_DATA:
                        if len(payload) >= HEADER_SIZE:
                            packet_id, segment = parse_header(payload[:HEADER_SIZE])
                            data = payload[HEADER_SIZE:]
                            return packet_id, segment, data
            time.sleep(0.01)
        raise ProtocolError("timeout waiting for data")

    def send_ack(self, packet_id, segment, result=1):
        payload = bytes([packet_id]) + segment.to_bytes(2, "little") + bytes([result])
        self.gateway.send(self.wireless_channel, MSG_FILE_ACK, payload)


def get_channel_list(gateway):
    gateway.send(CHANNEL_CONTROL, MSG_CHANNEL_LIST_REQUEST)
    start = time.time()
    while (time.time() - start) < 5:
        events = gateway.drain_events(timeout=0.1)
        for kind, data in events:
            if kind == "frame":
                channel, msg_type, payload = data
                if channel == CHANNEL_CONTROL and msg_type == MSG_CHANNEL_LIST_RESPONSE:
                    return payload
    return b""


def parse_channels(payload):
    if not payload:
        return []
    count = payload[0]
    entries = []
    offset = 1
    for _ in range(count):
        if offset + 6 > len(payload):
            break
        channel_id = payload[offset]
        kind = payload[offset + 1]
        direction = payload[offset + 2]
        max_packet = payload[offset + 3] | (payload[offset + 4] << 8)
        name_length = payload[offset + 5]
        offset += 6
        if offset + name_length > len(payload):
            break
        name = payload[offset:offset + name_length].decode("utf-8", "replace")
        offset += name_length
        entries.append((channel_id, name))
    return entries


def load_channel(gateway, channel_id, config=b""):
    payload = bytes((CTRL_LOAD_CHANNEL, channel_id)) + config
    gateway.send(CHANNEL_CONTROL, MSG_COMMAND, payload)

    start = time.time()
    while (time.time() - start) < 5:
        events = gateway.drain_events(timeout=0.1)
        for kind, data in events:
            if kind == "frame":
                channel, msg_type, payload = data
                if channel == CHANNEL_CONTROL:
                    if msg_type == MSG_STATUS:
                        return True
                    elif msg_type == MSG_ERROR:
                        return False
    return False


def check_and_load_channel(gateway, target_channel, target_name):
    payload = get_channel_list(gateway)
    channels = parse_channels(payload)
    loaded_channels = {ch_id: name for ch_id, name in channels}

    if target_channel in loaded_channels:
        return True, "Channel already loaded"

    other_wireless = CHANNEL_WIFI if target_channel == CHANNEL_ESPNOW else CHANNEL_ESPNOW
    if other_wireless in loaded_channels:
        other_name = "WiFi" if other_wireless == CHANNEL_WIFI else "ESP-NOW"
        target_name_str = "WiFi" if target_channel == CHANNEL_WIFI else "ESP-NOW"
        print(f"WARNING: {other_name} is currently loaded.")
        print(f"To use {target_name_str}, the radio needs to be reconfigured.")
        response = input(f"Continue and reload for {target_name_str}? (y/N): ")
        if response.lower() != 'y':
            print("Aborted.")
            return False, "User cancelled"

    print(f"Loading {target_name} channel...")
    if load_channel(gateway, target_channel):
        print(f"{target_name} channel loaded successfully.")
        return True, "Channel loaded"
    else:
        return False, "Failed to load channel"


def run_host_test(gateway, use_wifi=False, use_espnow=False):
    transfer = RadioTransfer(gateway, use_wifi, use_espnow)

    test_data = b"A" * MAX_PACKET_SIZE
    packet_id = 1
    total_size = len(test_data)

    print(f"Sending handshake: packet_id={packet_id}, total_size={total_size}")
    transfer.send_handshake(packet_id, total_size)

    try:
        ack_packet_id, ack_segment, result = transfer.wait_for_ack(timeout_ms=5000)
        print(f"Received ACK: packet_id={ack_packet_id}, segment={ack_segment}, result={result}")

        print(f"Sending data packet: packet_id={packet_id}, segment=0, size={len(test_data)}")
        transfer.send_data_packet(packet_id, 0, test_data)

        recv_packet_id, recv_segment, recv_data = transfer.wait_for_data(timeout_ms=5000)
        print(f"Received echo: packet_id={recv_packet_id}, segment={recv_segment}, size={len(recv_data)}")

        if recv_data == test_data:
            print("Data integrity verified!")
        else:
            print(f"Data mismatch! Expected {len(test_data)} bytes, got {len(recv_data)}")

    except ProtocolError as e:
        print(f"Protocol error: {e}")


def poll_for_clients(gateway, use_wifi=False, use_espnow=False):
    transfer = RadioTransfer(gateway, use_wifi, use_espnow)

    print("Polling for incoming data...")
    print("Press Ctrl+C to exit")

    try:
        while True:
            events = gateway.drain_events(timeout=0.5)
            for kind, data in events:
                if kind == "frame":
                    channel, msg_type, payload = data
                    if channel == transfer.wireless_channel:
                        if msg_type == MSG_FILE_HANDSHAKE:
                            packet_id, total_size = parse_handshake(payload)
                            print(f"Received handshake: packet_id={packet_id}, total_size={total_size}")
                            transfer.send_ack(packet_id, 0, 1)
                            print("Sent ACK")

                        elif msg_type == MSG_FILE_DATA:
                            if len(payload) >= HEADER_SIZE:
                                packet_id, segment = parse_header(payload[:HEADER_SIZE])
                                data = payload[HEADER_SIZE:]
                                print(f"Received data: packet_id={packet_id}, segment={segment}, size={len(data)}")
                                transfer.send_ack(packet_id, segment, 1)

                        elif msg_type == MSG_FILE_ACK:
                            if len(payload) >= 5:
                                ack_packet_id = payload[0]
                                ack_segment = int.from_bytes(payload[1:3], "little")
                                ack_result = payload[3]
                                print(f"Received ACK: packet_id={ack_packet_id}, segment={ack_segment}, result={ack_result}")

                elif kind == "error":
                    print(f"Error: {data}")

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nExiting...")


def main():
    parser = argparse.ArgumentParser(description="Radio file transfer utility")
    parser.add_argument(
        "--serial",
        help="Device serial number from USB descriptor",
    )
    parser.add_argument(
        "-e", "--espnow",
        action="store_true",
        help="Use ESP-NOW radio (USB channel 3)"
    )
    parser.add_argument(
        "-w", "--wifi",
        action="store_true",
        help="Use WiFi server (USB channel 4)"
    )
    parser.add_argument(
        "-t", "--test",
        action="store_true",
        help="Run host test mode (send test packet)"
    )
    parser.add_argument(
        "-p", "--poll",
        action="store_true",
        help="Poll for incoming client data"
    )
    args = parser.parse_args()

    if args.espnow and args.wifi:
        raise SystemExit("Error: -e (espnow) and -w (wifi) cannot be used together")

    if not args.espnow and not args.wifi:
        raise SystemExit("Error: must specify either -e (espnow) or -w (wifi)")

    gateway = USBGateway(serial=args.serial)

    try:
        gateway.open()
        print("Gateway opened")

        target_channel = CHANNEL_WIFI if args.wifi else CHANNEL_ESPNOW
        target_name = "WiFi" if args.wifi else "ESP-NOW"

        success, msg = check_and_load_channel(gateway, target_channel, target_name)
        if not success:
            print(f"Failed: {msg}")
            return 1

        if args.test:
            run_host_test(gateway, use_wifi=args.wifi, use_espnow=args.espnow)
        elif args.poll:
            poll_for_clients(gateway, use_wifi=args.wifi, use_espnow=args.espnow)
        else:
            print("No action specified. Use -t for test or -p for polling.")

        return 0

    except Exception as exc:
        print(f"Error: {exc}")
        return 1
    finally:
        gateway.close()


if __name__ == "__main__":
    sys.exit(main())
