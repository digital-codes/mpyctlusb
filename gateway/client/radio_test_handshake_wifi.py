# SPDX-License-Identifier: AGPL-3.0-only
# radio_test_handshake_wifi.py
#
# MicroPython WiFi client: sends handshake + segmented data, waits for echoes.
# Auto-runs on import (no CLI args, suitable for mpremote).
#
# Usage:
#   mpremote run client/radio_test_handshake_wifi.py

import time
import json
import network
import socket

import private as pr

WIFI_SSID = pr.WIFI_SSID
WIFI_PASSWORD = pr.WIFI_PASSWORD
WIFI_CHANNEL = pr.WIFI_CHANNEL
SERVER_PORT = 8080
ID_MARKER = b"\x00"

MSG_FILE_HANDSHAKE = 0x30
MSG_FILE_DATA = 0x31
MSG_FILE_ACK = 0x32

PACKET_SIZE = 5000
MAX_SEGMENT_SIZE = 1024


def make_header(packet_id, segment):
    return bytes([((packet_id << 4) & 0xF0) | ((segment >> 8) & 0x0F), segment & 0xFF])


def parse_header(header):
    b0, b1 = header[0], header[1]
    packet_id = (b0 >> 4) & 0x0F
    segment = ((b0 & 0x0F) << 8) | b1
    return packet_id, segment


def encode_handshake(packet_id, total_size):
    return bytes([packet_id]) + total_size.to_bytes(4, "little")


def parse_ack(msg_data):
    if len(msg_data) >= 5 and msg_data[0] == MSG_FILE_ACK:
        ack_pid = msg_data[1]
        ack_seg = int.from_bytes(msg_data[2:4], "little")
        ack_res = msg_data[4]
        return ack_pid, ack_seg, ack_res
    return None


def main():
    print("WiFi handshake test client")

    try:
        with open("config.json", "r") as f:
            config = json.load(f)
    except Exception:
        config = {}

    DEVICE_ID = str(config.get("device", "unknown"))
    print("Device id:", DEVICE_ID)

    wlan = network.WLAN(network.STA_IF)
    wlan.active(False)
    time.sleep(1)
    wlan.active(True)
    while not wlan.active():
        time.sleep(1)
    try:
        wlan.config(channel=WIFI_CHANNEL)
    except Exception:
        pass
    try:
        wlan.config(pm=wlan.PM_NONE)
    except Exception:
        pass

    print("Connecting to WiFi AP...")
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)
    while not wlan.isconnected():
        time.sleep(1)
    print("Connected! IP:", wlan.ifconfig()[0])
    SERVER_IP = wlan.ifconfig()[2]

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((SERVER_IP, SERVER_PORT))
    sock.settimeout(0)
    sock.send(ID_MARKER + DEVICE_ID.encode("utf-8"))
    print("Connected and identified")

    packet_id = 1
    test_data = b"D" * PACKET_SIZE
    num_segments = (len(test_data) + MAX_SEGMENT_SIZE - 1) // MAX_SEGMENT_SIZE
    print(f"Packet: pid={packet_id}, size={PACKET_SIZE}, segments={num_segments}")

    print("Sending handshake...")
    payload = bytes([MSG_FILE_HANDSHAKE]) + encode_handshake(packet_id, PACKET_SIZE)
    sock.send(payload)

    rx_buffer = b""
    ack_received = False
    start = time.time()
    while not ack_received and (time.time() - start) < 5:
        try:
            sock.setblocking(False)
            data = sock.recv(4096)
            if data:
                rx_buffer += data
        except OSError:
            pass

        while len(rx_buffer) >= 1:
            msg_type = rx_buffer[0]
            if msg_type == MSG_FILE_ACK:
                payload = rx_buffer[1:]
                if len(payload) < 4:
                    break
                result = parse_ack(payload[:5] if len(payload) >= 5 else payload)
                if result:
                    ack_pid, ack_seg, ack_res = result
                    print(f"ACK: pid={ack_pid}, seg={ack_seg}, result={ack_res}")
                    ack_received = True
                    rx_buffer = b""
                    break
            else:
                rx_buffer = b""
                break

        time.sleep(0.05)

    if not ack_received:
        print("No ACK received")
        sock.close()
        return

    print(f"Sending {num_segments} data segments...")
    for seg in range(num_segments):
        start_byte = seg * MAX_SEGMENT_SIZE
        end_byte = min(start_byte + MAX_SEGMENT_SIZE, len(test_data))
        segment_data = test_data[start_byte:end_byte]
        payload = bytes([MSG_FILE_DATA]) + make_header(packet_id, seg) + segment_data
        sock.send(payload)
        print(f"  Sent seg={seg}, size={len(segment_data)}")

    print("Waiting for echoes...")
    echoed_segments = {}
    start = time.time()
    while len(echoed_segments) < num_segments and (time.time() - start) < 10:
        try:
            sock.setblocking(False)
            data = sock.recv(4096)
            if data:
                rx_buffer += data
        except OSError:
            pass

        while len(rx_buffer) >= 3:
            msg_type = rx_buffer[0]
            if msg_type == MSG_FILE_DATA:
                payload = rx_buffer[1:]
                if len(payload) < 2:
                    break
                recv_pid, recv_seg = parse_header(payload[:2])
                recv_data = payload[2:]
                if recv_pid == packet_id and recv_seg not in echoed_segments:
                    echoed_segments[recv_seg] = recv_data
                    print(f"  Echo: seg={recv_seg}, size={len(recv_data)}")
                rx_buffer = b""
            elif msg_type == MSG_FILE_ACK:
                payload = rx_buffer[1:]
                if len(payload) >= 4:
                    result = parse_ack(payload[:5] if len(payload) >= 5 else payload)
                    if result:
                        print(f"  ACK: seg={result[1]}, result={result[2]}")
                rx_buffer = b""
            else:
                rx_buffer = b""
                break

        time.sleep(0.05)

    print(f"Received {len(echoed_segments)}/{num_segments} echoes")
    for seg in range(num_segments):
        if seg not in echoed_segments:
            print(f"  Missing seg={seg}")
        else:
            start_byte = seg * MAX_SEGMENT_SIZE
            end_byte = min(start_byte + MAX_SEGMENT_SIZE, len(test_data))
            expected = test_data[start_byte:end_byte]
            if echoed_segments[seg] != expected:
                print(f"  Mismatch seg={seg}")

    sock.close()


main()