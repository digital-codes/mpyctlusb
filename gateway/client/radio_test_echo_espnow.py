# SPDX-License-Identifier: AGPL-3.0-only
# radio_test_echo_espnow.py
#
# MicroPython ESP-NOW client: echo server for file transfer tests.
# Receives handshake/data and responds with ACK + echo.
# Auto-runs on import (no CLI args, suitable for mpremote).
#
# Usage:
#   mpremote run client/radio_test_echo_espnow.py

import time
import json
import network
import espnow

import private as pr

S3U_SERVER = pr.ENOW_SERVER
S3U_KEY = pr.ENOW_KEY
WIFI_CHANNEL = pr.ENOW_CHANNEL
SHARED_KEY = bytes.fromhex(S3U_KEY[:32])

MSG_FILE_HANDSHAKE = 0x30
MSG_FILE_DATA = 0x31
MSG_FILE_ACK = 0x32


def parse_handshake(payload):
    packet_id = payload[0]
    total_size = int.from_bytes(payload[1:5], "little")
    return packet_id, total_size


def parse_header(header):
    b0, b1 = header[0], header[1]
    packet_id = (b0 >> 4) & 0x0F
    segment = ((b0 & 0x0F) << 8) | b1
    return packet_id, segment


def send_ack(radio, mac, packet_id, segment, result=1):
    payload = bytes([packet_id]) + segment.to_bytes(2, "little") + bytes([result])
    full_payload = SHARED_KEY[:16] + bytes([MSG_FILE_ACK]) + payload
    radio.send(mac, full_payload)


def main():
    print("ESP-NOW echo server")
    print("Server MAC:", S3U_SERVER)
    print("WiFi channel:", WIFI_CHANNEL)

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

    print("Echo server running. Press Ctrl+C to exit.")

    try:
        while True:
            sender, msg = radio.recv(0)
            if sender is None:
                time.sleep(0.1)
                continue

            if len(msg) >= 16 and msg[:16] == SHARED_KEY[:16]:
                app_data = msg[16:]
                if len(app_data) < 1:
                    continue
                msg_type = app_data[0]
                payload = app_data[1:]

                if msg_type == MSG_FILE_HANDSHAKE:
                    try:
                        packet_id, total_size = parse_handshake(payload)
                        print(f"HS: pid={packet_id}, size={total_size}")
                        send_ack(radio, sender, packet_id, 0, 1)
                    except Exception as e:
                        print(f"HS error: {e}")

                elif msg_type == MSG_FILE_DATA:
                    try:
                        if len(payload) >= 2:
                            packet_id, segment = parse_header(payload[:2])
                            recv_data = payload[2:]
                            print(f"DATA: pid={packet_id}, seg={segment}, size={len(recv_data)}")
                            send_ack(radio, sender, packet_id, segment, 1)
                            echo_payload = bytes([MSG_FILE_DATA]) + payload
                            full_echo = SHARED_KEY[:16] + echo_payload
                            radio.send(sender, full_echo)
                            print("  Echo sent")
                    except Exception as e:
                        print(f"DATA error: {e}")

                else:
                    try:
                        text_msg = payload.decode("utf-8")
                        print(f"MSG: {text_msg}")
                    except:
                        print(f"RAW: {payload.hex()}")

    except KeyboardInterrupt:
        print("Exiting")
    except Exception as e:
        print(f"Error: {e}")


main()
