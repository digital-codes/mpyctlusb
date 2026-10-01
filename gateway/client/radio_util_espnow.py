# SPDX-License-Identifier: AGPL-3.0-only
# radio_util_espnow.py
#
# MicroPython client for file transfer testing over ESP-NOW.
# Reuses configuration from espnow_client_example.py.
#
# Protocol:
#   - 16-bit header per packet: upper 4 bits = packet_id (0-15), lower 12 bits = segment
#   - Handshake: sender sends packet_id + total_size (4 bytes)
#   - Receiver responds with ACK (packet_id + segment + result)
#   - Sender transmits data packets with header + data
#   - Each data packet is echoed back by receiver
#
# Usage:
#   import radio_util_espnow
#   radio_util_espnow.run_test()

import time
import json
import network
import espnow
import micropython

import private as pr
import channel_defs

S3U_SERVER = pr.ENOW_SERVER
S3U_KEY = pr.ENOW_KEY
WIFI_CHANNEL = pr.ENOW_CHANNEL

SHARED_KEY = bytes.fromhex(S3U_KEY[:32])

MSG_FILE_HANDSHAKE = 0x30
MSG_FILE_DATA = 0x31
MSG_FILE_ACK = 0x32

MAX_PACKET_SIZE = 4096
HEADER_SIZE = 2


def get_header(packet_id, segment):
    return bytes([((packet_id << 4) & 0xF0) | ((segment >> 8) & 0x0F), segment & 0x0F])


def parse_header(header):
    b0, b1 = header[0], header[1]
    packet_id = (b0 >> 4) & 0x0F
    segment = ((b0 & 0x0F) << 8) | b1
    return packet_id, segment


def encode_handshake(packet_id, total_size):
    return bytes([packet_id]) + total_size.to_bytes(4, "little")


def parse_handshake(payload):
    packet_id = payload[0]
    total_size = int.from_bytes(payload[1:5], "little")
    return packet_id, total_size


def send_ack(radio, mac, packet_id, segment, result=1):
    payload = bytes([packet_id]) + segment.to_bytes(2, "little") + bytes([result])
    full_payload = SHARED_KEY[:16] + bytes([MSG_FILE_ACK]) + payload
    radio.send(mac, full_payload)


def receive_loop(radio, mac):
    print("Receive loop started")
    irq_pending = False

    def _irq(r):
        nonlocal irq_pending
        if irq_pending:
            return
        irq_pending = True
        try:
            micropython.schedule(_drain, 0)
        except RuntimeError:
            irq_pending = False

    def _drain(ignored):
        nonlocal irq_pending
        irq_pending = False
        while True:
            sender, msg = radio.irecv(0)
            if sender is None:
                return

            if len(msg) >= 16 and msg[:16] == SHARED_KEY:
                app_data = msg[16:]
                if len(app_data) < 1:
                    continue
                msg_type = app_data[0]
                payload = app_data[1:]

                if msg_type == MSG_FILE_HANDSHAKE:
                    try:
                        packet_id, total_size = parse_handshake(payload)
                        print(f"Received handshake: packet_id={packet_id}, total_size={total_size}")
                        send_ack(radio, sender, packet_id, 0, 1)
                    except Exception as e:
                        print(f"Handshake parse error: {e}")

                elif msg_type == MSG_FILE_DATA:
                    try:
                        if len(payload) >= HEADER_SIZE:
                            packet_id, segment = parse_header(payload[:HEADER_SIZE])
                            data = payload[HEADER_SIZE:]
                            print(f"Received data: packet_id={packet_id}, segment={segment}, size={len(data)}")
                            send_ack(radio, sender, packet_id, segment, 1)
                    except Exception as e:
                        print(f"Data parse error: {e}")

    radio.irq(_irq)
    return _irq


def run_test():
    print("Starting ESP-NOW file transfer test client")
    print("Server MAC:", S3U_SERVER)
    print("WiFi channel:", WIFI_CHANNEL)
    print("Shared key:", SHARED_KEY.hex())

    try:
        with open("config.json", "r") as f:
            config = json.load(f)
            print("Config:", config)
    except Exception as e:
        print("Failed to load config.json:", e)
        config = {}

    LMK = None
    if "ble" in config and "key" in config["ble"]:
        LMK = bytes.fromhex(config["ble"]["key"])
        print("Using LMK:", LMK.hex())

    wlan = network.WLAN(network.WLAN.IF_STA)
    try:
        wlan.disconnect()
    except Exception:
        pass

    wlan.active(True)
    while not wlan.active():
        print("Waiting for WLAN...")
        time.sleep(1)

    try:
        wlan.config(channel=WIFI_CHANNEL)
        wlan.config(pm=wlan.PM_NONE)
    except Exception as e:
        print("WLAN config error:", e)

    print("WLAN channel:", wlan.config("channel"))

    radio = espnow.ESPNow()
    radio.active(True)
    radio.config(rxbuf=4096, timeout_ms=0)
    radio.set_pmk(SHARED_KEY[:16])

    SERVER_MAC = bytes.fromhex(S3U_SERVER)
    if LMK:
        radio.add_peer(SERVER_MAC, LMK, channel=WIFI_CHANNEL)
    else:
        radio.add_peer(SERVER_MAC, channel=WIFI_CHANNEL)

    print("Registered peer, starting receive loop")
    receive_loop(radio, SERVER_MAC)

    print("\n--- Test 1: Send handshake from client ---")
    packet_id = 1
    total_size = 1024
    payload = bytes([MSG_FILE_HANDSHAKE]) + encode_handshake(packet_id, total_size)
    full_payload = SHARED_KEY[:16] + payload
    print(f"Sending handshake: packet_id={packet_id}, total_size={total_size}")
    result = radio.send(SERVER_MAC, full_payload)
    print(f"Send result: {result}")

    print("\n--- Test 2: Send data packet ---")
    test_data = b"B" * 512
    payload = bytes([MSG_FILE_DATA]) + get_header(packet_id, 0) + test_data
    full_payload = SHARED_KEY[:16] + payload
    print(f"Sending data: packet_id={packet_id}, segment=0, size={len(test_data)}")
    result = radio.send(SERVER_MAC, full_payload)
    print(f"Send result: {result}")

    print("\nPolling for 30 seconds...")
    for i in range(30):
        time.sleep(1)
        if i % 5 == 0:
            print(f"Still running... ({i}s)")

    print("Done")


def run_echo_mode():
    print("Starting ESP-NOW echo server mode")
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

    irq_pending = False

    def _irq(r):
        nonlocal irq_pending
        if irq_pending:
            return
        irq_pending = True
        try:
            micropython.schedule(_drain, 0)
        except RuntimeError:
            irq_pending = False

    def _drain(ignored):
        nonlocal irq_pending
        irq_pending = False
        while True:
            sender, msg = radio.irecv(0)
            if sender is None:
                return

            if len(msg) >= 16 and msg[:16] == SHARED_KEY:
                app_data = msg[16:]
                if len(app_data) < 1:
                    continue
                msg_type = app_data[0]
                payload = app_data[1:]

                if msg_type == MSG_FILE_HANDSHAKE:
                    try:
                        packet_id, total_size = parse_handshake(payload)
                        print(f"HS: pid={packet_id} size={total_size}")
                        send_ack(radio, sender, packet_id, 0, 1)
                    except Exception as e:
                        print(f"HS err: {e}")

                elif msg_type == MSG_FILE_DATA:
                    try:
                        if len(payload) >= HEADER_SIZE:
                            packet_id, segment = parse_header(payload[:HEADER_SIZE])
                            data = payload[HEADER_SIZE:]
                            print(f"DATA: pid={packet_id} seg={segment} size={len(data)}")
                            send_ack(radio, sender, packet_id, segment, 1)
                    except Exception as e:
                        print(f"DATA err: {e}")

    radio.irq(_irq)
    print("Echo server running. Press Ctrl+C to exit.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nExiting")


if __name__ == "__main__":
    run_test()
