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

ID_MARKER = b"\x00"


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
    sock.send(ID_MARKER + device_id.encode("utf-8"))


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

    print("\n--- Test 1: Send handshake ---")
    packet_id = 1
    total_size = 1024
    payload = bytes([MSG_FILE_HANDSHAKE]) + encode_handshake(packet_id, total_size)
    print(f"Sending handshake: packet_id={packet_id}, total_size={total_size}")
    sock.send(payload)
    print("Sent")

    time.sleep(0.5)

    print("\n--- Test 2: Send data packet ---")
    test_data = b"C" * 512
    payload = bytes([MSG_FILE_DATA]) + get_header(packet_id, 0) + test_data
    print(f"Sending data: packet_id={packet_id}, segment=0, size={len(test_data)}")
    sock.send(payload)
    print("Sent")

    print("\n--- Receiving responses ---")
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
                        if len(payload) >= 3:
                            ack_pid = payload[0]
                            ack_seg = int.from_bytes(payload[1:3], "little")
                            ack_res = payload[3] if len(payload) > 3 else 0
                            print(f"  ACK: pid={ack_pid}, seg={ack_seg}, result={ack_res}")
        except OSError:
            pass
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(0.2)

    print("\nDone")
    sock.close()


def run_echo_server():
    print("Starting WiFi echo server mode")

    try:
        with open("config.json", "r") as f:
            config = json.load(f)
    except Exception:
        config = {}

    DEVICE_ID = str(config.get("device", "unknown"))

    wlan = network.WLAN(network.STA_IF)
    wlan.active(False)
    time.sleep(1)
    wlan.active(True)

    while not wlan.active():
        print("Activating WiFi...")
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
        print("Connecting...")
        time.sleep(1)

    print("Connected! IP:", wlan.ifconfig()[0])
    SERVER_IP = wlan.ifconfig()[2]
    print("Server:", SERVER_IP)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 8081))
    sock.listen(1)
    sock.settimeout(0)
    print("Listening on port 8081 for echo server...")

    clients = []

    try:
        while True:
            try:
                cl, addr = sock.accept()
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
                        msg_type = data[0] if len(data) > 0 else 0
                        payload = data[1:] if len(data) > 1 else b""

                        if msg_type == MSG_FILE_HANDSHAKE:
                            try:
                                packet_id, total_size = parse_handshake(payload)
                                print(f"HS: pid={packet_id} size={total_size}")
                                ack = bytes([MSG_FILE_ACK]) + bytes([packet_id]) + (0).to_bytes(2, "little") + bytes([1])
                                cl.send(ack)
                            except Exception as e:
                                print(f"HS err: {e}")

                        elif msg_type == MSG_FILE_DATA:
                            try:
                                if len(payload) >= HEADER_SIZE:
                                    packet_id, segment = parse_header(payload[:HEADER_SIZE])
                                    recv_data = payload[HEADER_SIZE:]
                                    print(f"DATA: pid={packet_id} seg={segment} size={len(recv_data)}")
                                    ack = bytes([MSG_FILE_ACK]) + bytes([packet_id]) + segment.to_bytes(2, "little") + bytes([1])
                                    cl.send(ack)
                            except Exception as e:
                                print(f"DATA err: {e}")

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
        sock.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "echo":
        run_echo_server()
    else:
        run_test()
