# SPDX-License-Identifier: AGPL-3.0-only
# radio_test_handshake_wifi.py
#
# MicroPython WiFi client: sends handshake + data packet, waits for echo.
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


def get_header(packet_id, segment):
    return bytes([((packet_id << 4) & 0xF0) | ((segment >> 8) & 0x0F), segment & 0x0F])


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
        wlan.config(pm=wlan.PM_NONE)
    except Exception:
        pass

    print("Connecting to WiFi...")
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

    print(f"Sending handshake: pid={packet_id}, size={PACKET_SIZE}")
    payload = bytes([MSG_FILE_HANDSHAKE]) + encode_handshake(packet_id, PACKET_SIZE)
    sock.send(payload)

    ack_received = False
    start = time.time()
    while not ack_received and (time.time() - start) < 5:
        try:
            sock.setblocking(False)
            data = sock.recv(4096)
            if data:
                msg_type = data[0]
                msg_data = data[1:]
                if msg_type == MSG_FILE_ACK:
                    result = parse_ack(msg_data)
                    if result:
                        ack_pid, ack_seg, ack_res = result
                        print(f"ACK: pid={ack_pid}, seg={ack_seg}, result={ack_res}")
                        ack_received = True
        except OSError:
            pass
        time.sleep(0.05)

    if not ack_received:
        print("No ACK received")
        sock.close()
        return

    print("Sending data packet...")
    payload = bytes([MSG_FILE_DATA]) + get_header(packet_id, 0) + test_data
    sock.send(payload)

    echo_received = False
    start = time.time()
    while not echo_received and (time.time() - start) < 5:
        try:
            sock.setblocking(False)
            data = sock.recv(4096)
            if data:
                msg_type = data[0]
                msg_data = data[1:]
                if msg_type == MSG_FILE_DATA and len(msg_data) >= 2:
                    recv_pid, recv_seg = parse_header(msg_data[:2])
                    recv_data = msg_data[2:]
                    print(f"Echo: pid={recv_pid}, seg={recv_seg}, size={len(recv_data)}")
                    if recv_data == test_data:
                        print("Data integrity verified!")
                        echo_received = True
                    else:
                        print(f"Data mismatch! Expected {PACKET_SIZE}, got {len(recv_data)}")
        except OSError:
            pass
        time.sleep(0.05)

    if not echo_received:
        print("No echo received")
    sock.close()


main()
