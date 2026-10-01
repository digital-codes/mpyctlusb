# SPDX-License-Identifier: AGPL-3.0-only
# radio_test_handshake_espnow.py
#
# MicroPython ESP-NOW client: sends handshake + data packet, waits for echo.
# Auto-runs on import (no CLI args, suitable for mpremote).
#
# Usage:
#   mpremote run client/radio_test_handshake_espnow.py

import time
import json
import network
import espnow

import private as pr
import channel_defs

S3U_SERVER = pr.ENOW_SERVER
S3U_KEY = pr.ENOW_KEY
WIFI_CHANNEL = pr.ENOW_CHANNEL
SHARED_KEY = bytes.fromhex(S3U_KEY[:32])

MSG_FILE_HANDSHAKE = 0x30
MSG_FILE_DATA = 0x31
MSG_FILE_ACK = 0x32

PACKET_SIZE = 5000


def get_header(packet_id, segment):
    return bytes([((packet_id << 4) & 0xF0) | ((segment >> 8) & 0x0F), segment & 0x0F])


def encode_handshake(packet_id, total_size):
    return bytes([packet_id]) + total_size.to_bytes(4, "little")


def parse_ack(msg_data):
    if len(msg_data) >= 4 and msg_data[0] == MSG_FILE_ACK:
        ack_pid = msg_data[1]
        ack_seg = int.from_bytes(msg_data[2:4], "little")
        ack_res = msg_data[4] if len(msg_data) > 4 else 0
        return ack_pid, ack_seg, ack_res
    return None


def send_ack(radio, mac, packet_id, segment, result=1):
    payload = bytes([packet_id]) + segment.to_bytes(2, "little") + bytes([result])
    full_payload = SHARED_KEY[:16] + bytes([MSG_FILE_ACK]) + payload
    radio.send(mac, full_payload)


def main():
    print("ESP-NOW handshake test client")
    print("Server MAC:", S3U_SERVER)
    print("Packet size:", PACKET_SIZE)

    try:
        with open("config.json", "r") as f:
            config = json.load(f)
    except Exception:
        config = {}

    LMK = None
    if "ble" in config and "key" in config["ble"]:
        LMK = bytes.fromhex(config["ble"]["key"])

    wlan = network.WLAN(network.WLAN.IF_STA)
    try:
        wlan.disconnect()
    except Exception:
        pass
    wlan.active(True)
    while not wlan.active():
        time.sleep(1)
    try:
        wlan.config(channel=WIFI_CHANNEL)
        wlan.config(pm=wlan.PM_NONE)
    except Exception:
        pass

    radio = espnow.ESPNow()
    radio.active(True)
    radio.config(rxbuf=4096, timeout_ms=0)
    radio.set_pmk(SHARED_KEY[:16])

    SERVER_MAC = bytes.fromhex(S3U_SERVER)
    if LMK:
        radio.add_peer(SERVER_MAC, LMK, channel=WIFI_CHANNEL)
    else:
        radio.add_peer(SERVER_MAC, channel=WIFI_CHANNEL)

    packet_id = 1
    test_data = b"D" * PACKET_SIZE

    print("Sending handshake...")
    payload = bytes([MSG_FILE_HANDSHAKE]) + encode_handshake(packet_id, PACKET_SIZE)
    radio.send(SERVER_MAC, SHARED_KEY[:16] + payload)

    ack_received = False
    start = time.time()
    while not ack_received and (time.time() - start) < 5:
        sender, msg = radio.recv(0)
        if sender is None:
            continue
        if len(msg) >= 17 and msg[:16] == SHARED_KEY:
            if msg[16] == MSG_FILE_ACK:
                result = parse_ack(msg[17:])
                if result:
                    ack_pid, ack_seg, ack_res = result
                    print(f"ACK: pid={ack_pid}, seg={ack_seg}, result={ack_res}")
                    ack_received = True
        time.sleep(0.05)

    if not ack_received:
        print("No ACK received")
        return

    print("Sending data packet...")
    payload = bytes([MSG_FILE_DATA]) + get_header(packet_id, 0) + test_data
    radio.send(SERVER_MAC, SHARED_KEY[:16] + payload)

    echo_received = False
    start = time.time()
    while not echo_received and (time.time() - start) < 5:
        sender, msg = radio.recv(0)
        if sender is None:
            continue
        if len(msg) >= 17 and msg[:16] == SHARED_KEY:
            if msg[16] == MSG_FILE_DATA:
                header = msg[17:19]
                recv_data = msg[19:]
                recv_pid = (header[0] >> 4) & 0x0F
                recv_seg = ((header[0] & 0x0F) << 8) | header[1]
                print(f"Echo: pid={recv_pid}, seg={recv_seg}, size={len(recv_data)}")
                if recv_data == test_data:
                    print("Data integrity verified!")
                    echo_received = True
                else:
                    print(f"Data mismatch! Expected {PACKET_SIZE}, got {len(recv_data)}")
        time.sleep(0.05)

    if not echo_received:
        print("No echo received")


main()
