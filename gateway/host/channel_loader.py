#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Dynamic channel loader for the AtomS3U USB sensor gateway.

Host tool that demonstrates CTRL_LOAD_CHANNEL. Sends a command to the device
asking it to import and instantiate a sensor module at runtime.

Usage:
  python3 host/channel_loader.py rgb --pin 35
  python3 host/channel_loader.py button --pin 41
  python3 host/channel_loader.py espnow
  python3 host/channel_loader.py wifi

Or specify the channel id directly:
  python3 host/channel_loader.py load --channel-id 2 --pin 35
"""

from __future__ import annotations

import argparse
import os
import sys
import time


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from channel_defs import (
    MSG_COMMAND,
    MSG_ERROR,
    MSG_STATUS,
    MSG_CHANNEL_LIST_RESPONSE,
    MSG_CHANNEL_LIST_REQUEST,
    CHANNEL_CONTROL,
    CHANNEL_BUTTON,
    CHANNEL_RGB,
    CHANNEL_ESPNOW,
    CTRL_LOAD_CHANNEL,
    CHANNEL_WIFI,
)

from usbInterface import USBInterface, ProtocolError, USBDisconnected

def load_channel(gateway, channel_id, config=b""):
    """Send CTRL_LOAD_CHANNEL to the device."""
    payload = bytes((CTRL_LOAD_CHANNEL, channel_id)) + config
    gateway.send(CHANNEL_CONTROL, MSG_COMMAND, payload)

    def predicate(channel, msg_type, payload):
        if channel != CHANNEL_CONTROL:
            return False
        if msg_type == MSG_STATUS:
            return True
        if msg_type == MSG_ERROR:
            related = payload[0] if len(payload) > 0 else -1
            code = payload[1] if len(payload) > 1 else -1
            text = payload[2:].decode("utf-8", "replace")
            raise ProtocolError(
                "device error ch=%d code=%d: %s"
                % (related, code, text)
            )
        return False

    return gateway.read_until(predicate)


def request_channel_list(gateway):
    """Ask the device for its current channel list."""
    gateway.send(CHANNEL_CONTROL, MSG_CHANNEL_LIST_REQUEST)

    def predicate(channel, msg_type, payload):
        return channel == CHANNEL_CONTROL and msg_type == MSG_CHANNEL_LIST_RESPONSE

    return gateway.read_until(predicate)


def parse_channel_list(payload):
    """Decode channel list payload into a list of (id, name)."""
    if not payload:
        return []
    count = payload[0]
    entries = []
    offset = 1
    for _ in range(count):
        if offset + 6 > len(payload):
            break
        channel_id = payload[offset]
        name_length = payload[offset + 5]
        offset += 6
        if offset + name_length > len(payload):
            break
        name = payload[offset:offset + name_length].decode("utf-8")
        offset += name_length
        entries.append((channel_id, name))
    return entries


def cmd_load(args):
    """Generic load command."""
    gateway = USBInterface(serial=args.device_serial)
    try:
        gateway.open()

        config = b""
        if hasattr(args, "pin") and args.pin is not None:
            if args.name:
                config = bytes((args.pin & 0xFF,)) + bytes((len(args.name),)) + args.name.encode("utf-8")
            else:
                config = bytes((args.pin & 0xFF,))

        print("Loading channel id=%d config=%s..." % (args.channel_id, config.hex()))
        load_channel(gateway, args.channel_id, config)
        print("Load command accepted")

        print("Requesting channel list...")
        _, _, payload = request_channel_list(gateway)
        channels = parse_channel_list(payload)
        print("Current channels:")
        for cid, cname in sorted(channels):
            print("  %d: %s" % (cid, cname))
        return 0
    except Exception as e:
        print("Error: %s" % e)
        return 1
    finally:
        gateway.close()


def cmd_rgb(args):
    """Load RGB channel with convenient defaults."""
    args.channel_id = CHANNEL_RGB
    return cmd_load(args)


def cmd_button(args):
    """Load button channel with convenient defaults."""
    args.channel_id = CHANNEL_BUTTON
    return cmd_load(args)


def cmd_espnow(args):
    """Load ESP-NOW channel (no config required)."""
    args.channel_id = CHANNEL_ESPNOW
    args.pin = None
    return cmd_load(args)

def cmd_wifi(args):
    """Load Wi-Fi channel (no config required)."""
    args.channel_id = CHANNEL_WIFI
    args.pin = None
    return cmd_load(args)



def main():
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Dynamic channel loader for AtomS3U device",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s rgb --pin 35                  - Load RGB sensor on GPIO35
  %(prog)s button --pin 41               - Load button sensor on GPIO41
  %(prog)s espnow                        - Load ESP-NOW radio
  %(prog)s wifi                          - Load Wi-Fi radio
  %(prog)s load --channel-id 2 --pin 35  - Load by channel id
        """,
    )
    parser.add_argument(
        "--device-serial",
        help="Device serial number from USB descriptor",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    rgb_parser = subparsers.add_parser("rgb", help="Load RGB LED sensor")
    rgb_parser.add_argument("--pin", type=int, required=True, help="GPIO pin number")
    rgb_parser.add_argument("--name", help="Channel name")

    btn_parser = subparsers.add_parser("button", help="Load button sensor")
    btn_parser.add_argument("--pin", type=int, required=True, help="GPIO pin number")
    btn_parser.add_argument("--name", help="Channel name")

    esp_parser = subparsers.add_parser("espnow", help="Load ESP-NOW radio")

    wifi_parser = subparsers.add_parser("wifi", help="Load Wi-Fi radio")

    load_parser = subparsers.add_parser("load", help="Load by channel id")
    load_parser.add_argument("--channel-id", type=int, required=True)
    load_parser.add_argument("--pin", type=int)
    load_parser.add_argument("--name", help="Channel name")

    args = parser.parse_args()

    if args.command == "rgb":
        return cmd_rgb(args)
    elif args.command == "button":
        return cmd_button(args)
    elif args.command == "espnow":
        return cmd_espnow(args)
    elif args.command == "wifi":
        return cmd_wifi(args)
    elif args.command == "load":
        return cmd_load(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
