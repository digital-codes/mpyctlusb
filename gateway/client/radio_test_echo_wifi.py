# SPDX-License-Identifier: AGPL-3.0-only
# radio_test_echo_wifi.py
#
# MicroPython WiFi client: connects to server, identifies, then echoes data.
# Auto-runs on import (no CLI args, suitable for mpremote).
#
# Usage:
#   mpremote run client/radio_test_echo_wifi.py
#
# Protocol:
#   - Client connects to server's WiFi AP on port 8080
#   - Sends identification: ID_MARKER (0x00) + device_id
#   - Waits for incoming handshake/data OR can send its own
#   - On receive: sends ACK, then echoes the data back (with corrected segment)

import time
import json
import network
import socket

import private as pr

WIFI_SSID = pr.WIFI_SSID
WIFI_PASSWORD = pr.WIFI_PASSWORD
WIFI_CHANNEL = pr.WIFI_CHANNEL
SERVER_PORT = 8080

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


def make_header(packet_id, segment):
    return bytes([((packet_id << 4) & 0xF0) | ((segment >> 8) & 0x0F), segment & 0xFF])


def main():
    print("WiFi echo client")
    print("SSID:", WIFI_SSID)
    print("Channel:", WIFI_CHANNEL)

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
    print("Server IP:", SERVER_IP)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((SERVER_IP, SERVER_PORT))
    sock.settimeout(0)
    device_id_int = int(DEVICE_ID)
    sock.send(device_id_int.to_bytes(2, "little"))
    print(f"Identified as '{DEVICE_ID}'. Echo mode running. Press Ctrl+C to exit.")

    rx_buffer = b""
    try:
        while True:
            try:
                sock.setblocking(False)
                data = sock.recv(4096)
                if not data:
                    print("Connection closed by server")
                    break
                rx_buffer += data
            except OSError:
                pass

            while len(rx_buffer) >= 1:
                msg_type = rx_buffer[0]

                if msg_type == MSG_FILE_HANDSHAKE:
                    payload = rx_buffer[1:]
                    if len(payload) < 5:
                        break
                    packet_id, total_size = parse_handshake(payload[:5])
                    print(f"HS: pid={packet_id}, size={total_size}")
                    ack = bytes([MSG_FILE_ACK]) + bytes([packet_id]) + (0).to_bytes(2, "little") + bytes([1])
                    sock.send(ack)
                    print("  ACK sent")
                    rx_buffer = b""

                elif msg_type == MSG_FILE_DATA:
                    payload = rx_buffer[1:]
                    if len(payload) < 2:
                        break
                    packet_id, segment = parse_header(payload[:2])
                    recv_data = payload[2:]
                    print(f"DATA: pid={packet_id}, seg={segment}, size={len(recv_data)}")
                    ack = bytes([MSG_FILE_ACK]) + bytes([packet_id]) + segment.to_bytes(2, "little") + bytes([1])
                    sock.send(ack)
                    echo = bytes([MSG_FILE_DATA]) + make_header(packet_id, segment) + recv_data
                    sock.send(echo)
                    print(f"  ACK + echo sent (seg={segment}, size={len(recv_data)})")
                    rx_buffer = b""

                else:
                    print(f"Unknown msg_type: {msg_type}")
                    rx_buffer = b""

            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\nExiting")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        try:
            sock.close()
        except Exception:
            pass


main()