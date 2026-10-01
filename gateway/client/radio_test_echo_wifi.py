# SPDX-License-Identifier: AGPL-3.0-only
# radio_test_echo_wifi.py
#
# MicroPython WiFi client: echo server for file transfer tests.
# Listens on port 8081, receives handshake/data and responds with ACK + echo.
# Auto-runs on import (no CLI args, suitable for mpremote).
#
# Usage:
#   mpremote run client/radio_test_echo_wifi.py
#
# Note: This connects TO the WiFi AP and opens port 8081 as a server to receive
# commands from the host. The host must send to this client.

import time
import json
import network
import socket

import private as pr

WIFI_SSID = pr.WIFI_SSID
WIFI_PASSWORD = pr.WIFI_PASSWORD
WIFI_CHANNEL = pr.WIFI_CHANNEL

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


def main():
    print("WiFi echo server client")
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

    print("Connecting to WiFi...")
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)
    while not wlan.isconnected():
        time.sleep(1)

    print("Connected! IP:", wlan.ifconfig()[0])

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(("0.0.0.0", 8081))
    server_sock.listen(1)
    server_sock.settimeout(0)
    print("Listening on port 8081...")

    clients = []

    print("Echo server running. Press Ctrl+C to exit.")

    try:
        while True:
            try:
                cl, addr = server_sock.accept()
                cl.settimeout(0)
                clients.append(cl)
                print(f"Client connected: {addr}")
            except OSError:
                pass

            for cl in clients[:]:
                try:
                    cl.setblocking(False)
                    data = cl.recv(4096)
                    if data:
                        if len(data) >= 1:
                            msg_type = data[0]
                            payload = data[1:]

                            if msg_type == MSG_FILE_HANDSHAKE:
                                try:
                                    packet_id, total_size = parse_handshake(payload)
                                    print(f"HS: pid={packet_id}, size={total_size}")
                                    ack = bytes([MSG_FILE_ACK]) + bytes([packet_id]) + (0).to_bytes(2, "little") + bytes([1])
                                    cl.send(ack)
                                    print("  ACK sent")
                                except Exception as e:
                                    print(f"HS error: {e}")

                            elif msg_type == MSG_FILE_DATA:
                                try:
                                    if len(payload) >= 2:
                                        packet_id, segment = parse_header(payload[:2])
                                        recv_data = payload[2:]
                                        print(f"DATA: pid={packet_id}, seg={segment}, size={len(recv_data)}")
                                        ack = bytes([MSG_FILE_ACK]) + bytes([packet_id]) + segment.to_bytes(2, "little") + bytes([1])
                                        cl.send(ack)
                                        print("  ACK sent")
                                        echo = bytes([MSG_FILE_DATA]) + payload
                                        cl.send(echo)
                                        print("  Echo sent")
                                except Exception as e:
                                    print(f"DATA error: {e}")

                    else:
                        cl.close()
                        clients.remove(cl)
                except OSError:
                    pass
                except Exception as e:
                    try:
                        cl.close()
                        clients.remove(cl)
                    except Exception:
                        pass

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nExiting")
        for cl in clients:
            cl.close()
        server_sock.close()


main()
