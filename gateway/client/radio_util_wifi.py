# SPDX-License-Identifier: AGPL-3.0-only
# radio_util_wifi.py
#
# MicroPython client for file transfer testing over WiFi TCP.
# Reuses configuration from wifi_client_example.py.
#
# Protocol:
#   - 16-bit header per packet: upper 4 bits = packet_id (0-15), lower 12 bits = segment
#   - Handshake: sender sends packet_id + total_size (4 bytes)
#   - Receiver responds with ACK (packet_id + segment + result)
#   - Sender transmits data packets with header + data
#   - Each data packet is echoed back by receiver
#
# Usage:
#   import radio_util_wifi
#   radio_util_wifi.run_test()

import time
import json
import network
import socket
import struct

import private as pr

WIFI_SSID = pr.WIFI_SSID
WIFI_PASSWORD = pr.WIFI_PASSWORD
WIFI_CHANNEL = pr.WIFI_CHANNEL
SERVER_PORT = 8080
SHARED_KEY = bytes.fromhex(pr.WIFI_KEY[:32])

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


def connect_to_server():
    wlan = network.WLAN(network.STA_IF)
    while not wlan.isconnected():
        print("Connecting to WiFi...")
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        for _ in range(10):
            if wlan.isconnected():
                break
            time.sleep(1)

    print("Connected! IP:", wlan.ifconfig()[0])
    SERVER_IP = wlan.ifconfig()[2]
    print("Server:", SERVER_IP)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((SERVER_IP, SERVER_PORT))
    sock.settimeout(0)
    return sock


def send_identification(sock, device_id):
    device_id_int = int(device_id)
    sock.send(device_id_int.to_bytes(2, "little"))

    
def run_test():
    print("Starting WiFi file transfer test client")
    print("SSID:", WIFI_SSID)
    print("Server port:", SERVER_PORT)

    try:
        with open("config.json", "r") as f:
            config = json.load(f)
            print("Config:", config)
    except Exception as e:
        print("Failed to load config.json:", e)
        config = {}

    DEVICE_ID = str(config.get("device", ""))
    if not DEVICE_ID:
        print("WARNING: no 'device' field in config.json, using 'unknown'")
        DEVICE_ID = "unknown"
    print("Device id:", DEVICE_ID)

    print("\nConnecting to server...")
    sock = connect_to_server()
    send_identification(sock, DEVICE_ID)
    print("Connected and identified")

    FRAGMENT_SIZE = 1000
    print("\n--- Test 1: Send handshake ---")
    packet_id = 1
    total_size = 3011
    payload = bytes([MSG_FILE_HANDSHAKE]) + encode_handshake(packet_id, total_size)
    print(f"Sending handshake: packet_id={packet_id}, total_size={total_size}")
    sock.send(payload)
    print("Sent")

    time.sleep(0.5)

    bytes_to_send = total_size
    seg = 0
    while bytes_to_send > 0:
        test_data = b"C" * min(bytes_to_send, FRAGMENT_SIZE)
        payload = bytes([MSG_FILE_DATA]) + get_header(packet_id, seg) + test_data
        print(f"\n--- Sending segment {seg}: size={len(test_data)} ---")
        sock.send(payload)

        bytes_to_send -= len(test_data)
        seg += 1

        for _ in range(10):
            try:
                sock.setblocking(False)
                data = sock.recv(4096)
                if data:
                    print(f"Received: {len(data)} bytes")
                    if len(data) >= 1:
                        msg_type = data[0]
                        payload = data[1:]
                        if msg_type == MSG_FILE_ACK:
                            if len(payload) >= 4:
                                ack_pid = payload[0]
                                ack_seg = int.from_bytes(payload[1:3], "little")
                                ack_res = payload[3]
                                print(f"  ACK: pid={ack_pid}, seg={ack_seg}, result={ack_res}")
                                break
            except OSError:
                pass
            except Exception as e:
                print(f"Error: {e}")
            time.sleep(0.2)

    print("\nDone")
    sock.close()


def run_echo_server():
    print("Deprecated: use radio_test_echo_wifi.py instead")
    print("WiFi echo client connects to server AP and echoes incoming data")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "echo":
        run_echo_server()
    else:
        run_test()
