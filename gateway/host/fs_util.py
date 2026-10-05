#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Filesystem operations tool for the AtomS3U USB sensor gateway.

Host counterpart of the MicroPython filesystem handlers. Talks to the device's
uniform host USB interface using bulk on Linux and HID on Windows, with
the 4-byte framing defined by usb_channel_server.py.

Commands:
  ls [path]     - List directory contents (like mpremote ls)
  cat <path>    - Read file contents (like mpremote cat)
  put <src> [dst] - Write local file to device (like mpremote cp)
  rm <path>     - Delete file from device
  exists <path> - Check if file exists on device

Requires pyusb on Linux or hidapi on Windows. Optional --serial selects among several attached boards.
"""

from __future__ import annotations

import argparse
import os
import sys
import time



try:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _BASE_DIR = os.getcwd()

_COMMON_DIR = os.path.normpath(os.path.join(_BASE_DIR, "..", "common"))
if _COMMON_DIR not in sys.path:
    sys.path.insert(0, _COMMON_DIR)

from channel_defs import (
    MSG_COMMAND,
    MSG_RESPONSE,
    MSG_ERROR,
    MSG_PING,
    MSG_PONG,
    MSG_FS_LIST,
    MSG_FS_READ,
    MSG_FS_WRITE,
    MSG_FS_DELETE,
    MSG_FS_EXISTS,
    MSG_FS_RESPONSE,
    CHANNEL_CONTROL,
    CTRL_FS_LIST,
    CTRL_FS_READ,
    CTRL_FS_WRITE,
    CTRL_FS_DELETE,
    CTRL_FS_EXISTS,
    CTRL_RESET,
    MTU_USB
)

from usbInterface import USBInterface, ProtocolError, USBDisconnected


def read_fs_response(gateway, timeout_ms=5000):
    """Wait for a filesystem response while preserving transport independence."""
    def predicate(channel, msg_type, payload):
        if channel != CHANNEL_CONTROL:
            return False
        if msg_type == MSG_FS_RESPONSE:
            return True
        if msg_type == MSG_ERROR:
            related = payload[0] if len(payload) > 0 else -1
            code = payload[1] if len(payload) > 1 else -1
            text = payload[2:].decode("utf-8", "replace")
            raise ProtocolError("device error ch=%d code=%d: %s" % (related, code, text))
        return False
    return gateway.read_until(predicate, timeout_ms)[2]

def fs_list(gateway, path="/"):
    """List directory contents on the device."""
    payload = bytes((CTRL_FS_LIST,)) + path.encode("utf-8")
    gateway.send(CHANNEL_CONTROL, MSG_COMMAND, payload)
    response = read_fs_response(gateway)

    if not response:
        return []

    entries = []
    offset = 0
    while offset < len(response):
        if offset + 2 > len(response):
            break
        name_len = response[offset]
        offset += 1
        if offset + name_len + 2 > len(response):
            break
        name = response[offset:offset + name_len].decode("utf-8")
        offset += name_len
        is_dir = response[offset] != 0
        offset += 1
        if offset + 4 > len(response):
            break
        size = int.from_bytes(response[offset:offset + 4], "little")
        offset += 4
        entries.append({"name": name, "is_dir": is_dir, "size": size})

    return entries


def _u32(value):
    return bytes((value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF, (value >> 24) & 0xFF))


def fs_read(gateway, path, max_size=None):
    """Read file contents from the device, handling large files in chunks."""
    path_bytes = path.encode("utf-8")
    offset = 0
    result = bytearray()

    while True:
        size = MTU_USB
        if max_size is not None:
            remaining = max_size - offset
            if remaining <= 0:
                break
            size = min(size, remaining)

        extra = _u32(offset) + _u32(size)
        payload = bytes((CTRL_FS_READ,)) + path_bytes + b"\0" + extra
        gateway.send(CHANNEL_CONTROL, MSG_COMMAND, payload)
        data = read_fs_response(gateway)

        if not data:
            break
        result.extend(data)

        if len(data) < size:
            break
        offset += len(data)

    return bytes(result)


def fs_write(gateway, path, data):
    """Write data to a file on the device, chunking if necessary."""
    path_bytes = path.encode("utf-8")
    total_written = 0
    offset = 0

    # The USB MTU applies to the complete control-frame payload, not only
    # to the file bytes.  Account for command + path terminator + offset.
    write_overhead = 1 + len(path_bytes) + 1 + 4
    chunk_size = MTU_USB - write_overhead
    if chunk_size <= 0:
        raise ValueError("path too long for USB MTU")

    while offset < len(data):
        chunk = data[offset:offset + chunk_size]
        offset_bytes = _u32(offset)
        payload = bytes((CTRL_FS_WRITE,)) + path_bytes + b"\0" + offset_bytes + chunk
        gateway.send(CHANNEL_CONTROL, MSG_COMMAND, payload)
        response = read_fs_response(gateway)
        if len(response) >= 4:
            written = int.from_bytes(response[:4], "little")
            total_written += written
        offset += len(chunk)

    return total_written


def fs_delete(gateway, path):
    """Delete a file from the device."""
    payload = bytes((CTRL_FS_DELETE,)) + path.encode("utf-8")
    gateway.send(CHANNEL_CONTROL, MSG_COMMAND, payload)
    response = read_fs_response(gateway)
    return len(response) > 0 and response[0] == 1


def fs_exists(gateway, path):
    """Check if a file exists on the device."""
    payload = bytes((CTRL_FS_EXISTS,)) + path.encode("utf-8")
    gateway.send(CHANNEL_CONTROL, MSG_COMMAND, payload)
    response = read_fs_response(gateway)
    return len(response) > 0 and response[0] == 1


def cmd_ls(args):
    """Execute ls command."""
    gateway = USBInterface(serial=args.device_serial)
    try:
        gateway.open()
        if not gateway.ping():
            print("Error: device not responding to ping")
            return 1

        entries = fs_list(gateway, args.path)
        for entry in entries:
            if entry["is_dir"]:
                print("%s/" % entry["name"])
            else:
                print("%s  %d" % (entry["name"], entry["size"]))
        return 0
    except Exception as e:
        print("Error: %s" % e)
        return 1
    finally:
        gateway.close()


def cmd_cat(args):
    """Execute cat command."""
    gateway = USBInterface(serial=args.device_serial)
    try:
        gateway.open()
        if not gateway.ping():
            print("Error: device not responding to ping")
            return 1

        data = fs_read(gateway, args.path)

        if args.output:
            with open(args.output, "wb") as f:
                f.write(data)
            print("Saved %d bytes to %s" % (len(data), args.output))
            return 0

        is_binary = b"\x00" in data[:512] or any(b > 127 for b in data[:512])
        if is_binary:
            print("Binary file detected (%d bytes). Use -o to save to local file." % len(data))
            return 1

        sys.stdout.buffer.write(data)
        return 0
    except Exception as e:
        print("Error: %s" % e)
        return 1
    finally:
        gateway.close()


def cmd_put(args):
    """Execute put command (write local file to device)."""
    dest = args.dest if args.dest else os.path.basename(args.src)

    gateway = USBInterface(serial=args.device_serial)
    try:
        gateway.open()
        if not gateway.ping():
            print("Error: device not responding to ping")
            return 1

        with open(args.src, "rb") as f:
            data = f.read()

        size = fs_write(gateway, dest, data)
        print("Wrote %d bytes to %s" % (size, dest))
        return 0
    except Exception as e:
        print("Error: %s" % e)
        return 1
    finally:
        gateway.close()


def cmd_rm(args):
    """Execute rm command (delete file from device)."""
    gateway = USBInterface(serial=args.device_serial)
    try:
        gateway.open()
        if not gateway.ping():
            print("Error: device not responding to ping")
            return 1

        if fs_delete(gateway, args.path):
            print("Deleted %s" % args.path)
            return 0
        else:
            print("Failed to delete %s" % args.path)
            return 1
    except Exception as e:
        print("Error: %s" % e)
        return 1
    finally:
        gateway.close()


def cmd_exists(args):
    """Execute exists command."""
    gateway = USBInterface(serial=args.device_serial)
    try:
        gateway.open()
        if not gateway.ping():
            print("Error: device not responding to ping")
            return 1

        if fs_exists(gateway, args.path):
            print("%s exists" % args.path)
            return 0
        else:
            print("%s does not exist" % args.path)
            return 1
    except Exception as e:
        print("Error: %s" % e)
        return 1
    finally:
        gateway.close()


def cmd_reset(args):
    """Reset the device via machine.reset()."""
    gateway = USBInterface(serial=args.device_serial)
    try:
        gateway.open()
        payload = bytes((CTRL_RESET,))
        gateway.send(CHANNEL_CONTROL, MSG_COMMAND, payload)
        print("Reset command sent")
        return 0
    except Exception as e:
        print("Reset issued (device may be disconnecting): %s" % e)
        return 0
    finally:
        gateway.close()


def main():
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Filesystem operations for AtomS3U device",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s ls /                - List root directory
  %(prog)s ls /flash           - List flash filesystem
  %(prog)s cat /main.py        - Read file to stdout
  %(prog)s cat /main.py -o local.py - Read file to local file
  %(prog)s put local.py /main.py - Write file to device
  %(prog)s rm /test.py         - Delete file from device
  %(prog)s exists /boot.py     - Check if file exists
  %(prog)s reset               - Reset the device
        """,
    )
    parser.add_argument(
        "--device-serial",
        help="Device serial number from USB descriptor (if multiple devices)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    ls_parser = subparsers.add_parser("ls", help="List directory")
    ls_parser.add_argument("path", nargs="?", default="/", help="Directory path")

    cat_parser = subparsers.add_parser("cat", help="Read file")
    cat_parser.add_argument("path", help="File path")
    cat_parser.add_argument("-o", "--output", help="Save to local file instead of stdout")

    put_parser = subparsers.add_parser("put", help="Write file")
    put_parser.add_argument("src", help="Local source file")
    put_parser.add_argument("dest", nargs="?", help="Remote destination path")

    rm_parser = subparsers.add_parser("rm", help="Delete file")
    rm_parser.add_argument("path", help="File path")

    exists_parser = subparsers.add_parser("exists", help="Check file exists")
    exists_parser.add_argument("path", help="File path")

    subparsers.add_parser("reset", help="Reset the device")

    args = parser.parse_args()

    if args.command == "ls":
        return cmd_ls(args)
    elif args.command == "cat":
        return cmd_cat(args)
    elif args.command == "put":
        return cmd_put(args)
    elif args.command == "rm":
        return cmd_rm(args)
    elif args.command == "exists":
        return cmd_exists(args)
    elif args.command == "reset":
        return cmd_reset(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
