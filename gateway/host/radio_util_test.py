#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# radio_util_test.py
#
# Host-side test for radio file transfer protocol.
# Uses the same protocol as radio_util.py but provides simpler test commands.
#
# Usage:
#   python3 host/radio_util_test.py -e                  # ESP-NOW mode, interactive
#   python3 host/radio_util_test.py -w                  # WiFi mode, interactive
#   python3 host/radio_util_test.py -e -s <data>       # Send test data
#   python3 host/radio_util_test.py -w -r              # Receive mode

from __future__ import annotations

import argparse
import os
import sys
import time

import usb.core
import usb.util

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from channel_defs import (
    MSG_COMMAND,
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
    CTRL_GET_WIFI_CLIENTS,
)

VID = 0x303A
PID = 0x4001
INTERFACE = 2
EP_OUT = 0x03
EP_IN = 0x83

MAX_PACKET_SIZE = 4096
HEADER_SIZE = 2


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


class DeviceManager:
    def __init__(self):
        self.devices = {}
        self.next_id = 0

    def add_device(self, device_id):
        for num, did in self.devices.items():
            if did == device_id:
                return num
        num = self.next_id
        self.next_id += 1
        self.devices[num] = device_id
        return num

    def get_device_id(self, device_num):
        if isinstance(device_num, int):
            return self.devices.get(device_num)
        return device_num

    def list_devices(self):
        return list(self.devices.items())


class USBGateway:
    def __init__(self, serial=None, timeout_ms=100):
        self.serial = serial
        self.timeout_ms = timeout_ms
        self.dev = None
        self.claimed = False
        self.parser = FrameParser()
        self.write_lock = None

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
        import threading
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
        self.write_lock = threading.Lock()

    def close(self):
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
        except usb.core.USBError as exc:
            raise USBDisconnected(str(exc)) from exc

    def read(self, timeout_ms=None):
        if timeout_ms is None:
            timeout_ms = self.timeout_ms
        try:
            data = bytes(
                self.dev.read(
                    EP_IN,
                    8192,
                    timeout=timeout_ms,
                )
            )
            return self.parser.feed(data)
        except usb.core.USBTimeoutError:
            return []
        except ProtocolError as exc:
            raise ProtocolError(str(exc))


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


def get_channel_list(gateway):
    gateway.send(CHANNEL_CONTROL, MSG_CHANNEL_LIST_REQUEST)
    start = time.time()
    while (time.time() - start) < 5:
        frames = gateway.read(timeout_ms=500)
        for channel, msg_type, payload in frames:
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
        frames = gateway.read(timeout_ms=500)
        for channel, msg_type, payload in frames:
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
        print(f"Channel {target_channel} ({target_name}) already loaded")
        return True, "Channel already loaded"

    other_wireless = CHANNEL_WIFI if target_channel == CHANNEL_ESPNOW else CHANNEL_ESPNOW
    if other_wireless in loaded_channels:
        other_name = "WiFi" if other_wireless == CHANNEL_WIFI else "ESP-NOW"
        target_name_str = "WiFi" if target_channel == CHANNEL_WIFI else "ESP-NOW"
        print(f"WARNING: {other_name} is currently loaded (channel {other_wireless}).")
        print(f"To use {target_name_str}, the radio needs to be reconfigured.")
        response = input(f"Continue and reload for {target_name_str}? (y/N): ")
        if response.lower() != 'y':
            print("Aborted.")
            return False, "User cancelled"

    print(f"Loading {target_name} channel (id={target_channel})...")
    if load_channel(gateway, target_channel):
        print(f"{target_name} channel loaded successfully.")
        return True, "Channel loaded"
    else:
        print(f"ERROR: Failed to load channel {target_channel}. Is the server running on the stick?")
        return False, "Failed to load channel"


def send_test(gateway, channel, use_wifi, data_size=5000, device_id="sensor1", device_mgr=None):
    packet_id = 1
    total_size = data_size
    test_data = b"T" * data_size

    print(f"Test params: data_size={data_size}, device_id={device_id}")

    if use_wifi:
        device_bytes = device_id.encode("utf-8")
        wrapper = bytes([len(device_bytes)]) + device_bytes
        print(f"Wrapper: device={device_id} ({len(device_bytes)} bytes)")
    else:
        wrapper = bytes([peer_index])
        print(f"Wrapper: peer_index={peer_index}")

    handshake_payload = encode_handshake(packet_id, total_size)
    full_payload = wrapper + bytes([MSG_FILE_HANDSHAKE]) + handshake_payload
    print(f"Sending handshake: packet_id={packet_id}, total_size={total_size}, payload_len={len(full_payload)}")
    gateway.send(channel, MSG_COMMAND, full_payload)

    start = time.time()
    ack_received = False
    while (time.time() - start) < 5:
        frames = gateway.read(timeout_ms=200)
        for ch, msg_type, payload in frames:
            if ch == channel and msg_type == MSG_EVENT:
                if use_wifi:
                    if len(payload) > 1:
                        device_len = payload[0]
                        if len(payload) > 1 + device_len:
                            resp_payload = payload[1 + device_len:]
                        else:
                            continue
                    else:
                        continue
                else:
                    resp_payload = payload[1:] if len(payload) > 1 else b""

                if len(resp_payload) >= 5 and resp_payload[0] == MSG_FILE_ACK:
                    ack_pid = resp_payload[1]
                    ack_seg = int.from_bytes(resp_payload[2:4], "little")
                    ack_res = resp_payload[4] if len(resp_payload) > 4 else 0
                    print(f"Received ACK: pid={ack_pid}, seg={ack_seg}, result={ack_res}")
                    ack_received = True
                    break
        if ack_received:
            break
        time.sleep(0.05)

    if not ack_received:
        print("No ACK received")
        return False

    data_payload = get_header(packet_id, 0) + test_data
    full_payload = wrapper + bytes([MSG_FILE_DATA]) + data_payload
    print(f"Sending data packet: packet_id={packet_id}, segment=0, size={len(test_data)}")
    gateway.send(channel, MSG_COMMAND, full_payload)

    start = time.time()
    echo_received = False
    while (time.time() - start) < 5:
        frames = gateway.read(timeout_ms=200)
        for ch, msg_type, payload in frames:
            if ch == channel and msg_type == MSG_EVENT:
                if use_wifi:
                    if len(payload) > 1:
                        device_len = payload[0]
                        if len(payload) > 1 + device_len:
                            resp_payload = payload[1 + device_len:]
                        else:
                            continue
                    else:
                        continue
                else:
                    resp_payload = payload[1:] if len(payload) > 1 else b""

                if len(resp_payload) >= 3 and resp_payload[0] == MSG_FILE_DATA:
                    if len(resp_payload) >= HEADER_SIZE + 1:
                        recv_pid, recv_seg = parse_header(resp_payload[1:HEADER_SIZE+1])
                        recv_data = resp_payload[HEADER_SIZE+1:]
                        print(f"Received echo: pid={recv_pid}, seg={recv_seg}, size={len(recv_data)}")
                        if recv_data == test_data:
                            print("Data integrity verified!")
                            return True
                        else:
                            print("Data mismatch!")
                            return False
        time.sleep(0.05)

    print("No echo received")
    return False


def list_clients(gateway, use_wifi, device_mgr=None):
    if device_mgr is None:
        device_mgr = DeviceManager()

    if not use_wifi:
        print("Client list only available in WiFi mode")
        return

    gateway.send(CHANNEL_CONTROL, MSG_COMMAND, bytes([CTRL_GET_WIFI_CLIENTS]))

    start = time.time()
    while (time.time() - start) < 5:
        frames = gateway.read(timeout_ms=500)
        for ch, msg_type, payload in frames:
            if ch == CHANNEL_CONTROL and msg_type == MSG_STATUS:
                if len(payload) > 0:
                    count = payload[0]
                    print(f"Connected clients ({count}):")
                    pos = 1
                    for i in range(count):
                        if pos >= len(payload):
                            break
                        ip_len = payload[pos]
                        pos += 1
                        ip = payload[pos:pos + ip_len].decode() if ip_len > 0 else ""
                        pos += ip_len
                        if pos >= len(payload):
                            break
                        device_len = payload[pos]
                        pos += 1
                        device_id = payload[pos:pos + device_len].decode() if device_len > 0 else ""
                        pos += device_len
                        device_mgr.add_device(device_id)
                        print(f"  [{i}] {device_id} ({ip})")
                return
        time.sleep(0.1)

    print("No client list received")


def receive_loop(gateway, channel, use_wifi, timeout=30, device_mgr=None):
    if device_mgr is None:
        device_mgr = DeviceManager()
    print(f"Waiting for incoming data on channel {channel}...")
    print(f"Timeout: {timeout} seconds")

    start = time.time()
    while (time.time() - start) < timeout:
        frames = gateway.read(timeout_ms=500)
        for ch, msg_type, payload in frames:
            if ch == channel and msg_type == MSG_EVENT:
                if use_wifi:
                    if len(payload) > 1:
                        device_len = payload[0]
                        device_id = payload[1:1+device_len].decode("utf-8", "replace") if device_len > 0 else "unknown"
                        resp_payload = payload[1 + device_len:]
                        dev_num = device_mgr.add_device(device_id)
                        print(f"=== From device {dev_num}: {device_id} ===")
                    else:
                        continue
                else:
                    peer_idx = payload[0] if len(payload) > 0 else 0
                    resp_payload = payload[1:] if len(payload) > 1 else b""
                    print(f"From peer index: {peer_idx}")

                if len(resp_payload) >= 1:
                    msg_type_client = resp_payload[0]
                    msg_data = resp_payload[1:]

                    if msg_type_client == MSG_FILE_HANDSHAKE:
                        try:
                            packet_id, total_size = parse_handshake(msg_data)
                            print(f"  HS: pid={packet_id}, size={total_size}")
                            ack = bytes([packet_id]) + (0).to_bytes(2, "little") + bytes([1])
                            if use_wifi:
                                resp = bytes([len(device_id)]) + device_id.encode() + bytes([MSG_FILE_ACK]) + ack
                            else:
                                resp = bytes([peer_idx]) + bytes([MSG_FILE_ACK]) + ack
                            gateway.send(channel, MSG_COMMAND, resp)
                            print("  ACK sent")
                        except Exception as e:
                            print(f"  HS error: {e}")

                    elif msg_type_client == MSG_FILE_DATA:
                        try:
                            if len(msg_data) >= HEADER_SIZE:
                                packet_id, segment = parse_header(msg_data[:HEADER_SIZE])
                                data = msg_data[HEADER_SIZE:]
                                print(f"  DATA: pid={packet_id}, seg={segment}, size={len(data)}")
                                ack = bytes([packet_id]) + segment.to_bytes(2, "little") + bytes([1])
                                if use_wifi:
                                    resp = bytes([len(device_id)]) + device_id.encode() + bytes([MSG_FILE_ACK]) + ack
                                else:
                                    resp = bytes([peer_idx]) + bytes([MSG_FILE_ACK]) + ack
                                gateway.send(channel, MSG_COMMAND, resp)
                                print("  ACK sent")
                        except Exception as e:
                            print(f"  DATA error: {e}")

                    elif msg_type_client == MSG_FILE_ACK:
                        if len(msg_data) >= 4:
                            ack_pid = msg_data[0]
                            ack_seg = int.from_bytes(msg_data[1:3], "little")
                            ack_res = msg_data[3]
                            print(f"  ACK: pid={ack_pid}, seg={ack_seg}, result={ack_res}")

        time.sleep(0.1)

    print("Timeout reached")


def main():
    parser = argparse.ArgumentParser(description="Radio file transfer test")
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
        "-s", "--send",
        type=int,
        default=None,
        metavar="SIZE",
        help="Send test data of specified size"
    )
    parser.add_argument(
        "-l", "--list",
        action="store_true",
        help="List connected WiFi clients"
    )
    parser.add_argument(
        "-r", "--receive",
        action="store_true",
        help="Receive mode (wait for incoming data)"
    )
    parser.add_argument(
        "-t", "--timeout",
        type=int,
        default=30,
        help="Receive timeout in seconds (default: 30)"
    )
    parser.add_argument(
        "-d", "--device",
        type=str,
        default="sensor1",
        help="Device ID for WiFi (default: sensor1)"
    )
    args = parser.parse_args()

    if args.espnow and args.wifi:
        raise SystemExit("Error: -e (espnow) and -w (wifi) cannot be used together")

    if not args.espnow and not args.wifi:
        raise SystemExit("Error: must specify either -e (espnow) or -w (wifi)")

    gateway = USBGateway(serial=args.serial)
    device_mgr = DeviceManager()

    try:
        gateway.open()
        print("Gateway opened")

        target_channel = CHANNEL_WIFI if args.wifi else CHANNEL_ESPNOW
        target_name = "WiFi" if args.wifi else "ESP-NOW"

        success, msg = check_and_load_channel(gateway, target_channel, target_name)
        if not success:
            print(f"Failed: {msg}")
            return 1

        if args.list:
            print(f"Using channel {target_channel} ({target_name})")
            list_clients(gateway, args.wifi, device_mgr)
        elif args.receive:
            print(f"Using channel {target_channel} ({target_name})")
            receive_loop(gateway, target_channel, args.wifi, args.timeout, device_mgr)
        elif args.send is not None:
            print(f"Using channel {target_channel} ({target_name})")
            send_test(gateway, target_channel, args.wifi, args.send, args.device, device_mgr)
        else:
            print("No action specified. Use -l to list, -r to receive, or -s SIZE to send.")

        return 0

    except Exception as exc:
        print(f"Error: {exc}")
        return 1
    finally:
        gateway.close()


if __name__ == "__main__":
    sys.exit(main())
