# AtomS3U USB Sensor Gateway

Current status: **working baseline** with ESP-NOW and WiFi support.

## Overview

The AtomS3U USB Sensor Gateway provides two wireless communication options:

1. **ESP-NOW** - Low-power peer-to-peer communication
2. **WiFi AP** - Access point mode with TCP sockets

Each mode uses its own USB channel: ESP-NOW on channel 3 (`CHANNEL_ESPNOW`)
and WiFi on channel 4 (`CHANNEL_WIFI`). They share the same message
protocol but cannot be active concurrently on the same device.

---

## Software Structure

```
common/                     Shared constants between host and stick
    channel_defs.py        Message types, channel kinds, directions

stick/                     AtomS3U MicroPython, installed on the board
    boot.py                USB enumeration only
    usb_channel_server.py  USB transport, framing, channel management
    button_sensor.py       channel 1, GPIO41 input
    rgb_sensor.py          channel 2, GPIO35 NeoPixel output
    espnow_server.py       channel 3, ESP-NOW radio (bi-directional)
    wifi_server.py         channel 4, WiFi AP server (bi-directional)
    sensor_test_espnow.py  creates test sensors with ESP-NOW
    sensor_test_wifi.py    creates test sensors with WiFi
    config.json            shared key, device id (WiFi), own MAC
    private.py             Wi-Fi secrets (see Configuration)

host/                      Linux host applications
    sensor_tui.py          curses UI (pyusb)
    wifi_client.py         standalone WiFi client (optional)
    wifi/                  WiFi client files
        wifi_client.py     Linux WiFi TCP client

client/                    MicroPython clients for second ESP32
    espnow_client_example.py
    wifi_client_example.py

tests/                     Host-side smoke test
    usb_channel_smoketest.py
```

---

## Quick Start

### ESP-NOW Mode

1. Copy `stick/` contents to device (see Device Installation)
2. Start the sensor on the device:
   ```
   import sensor_test_espnow
   sensor_test_espnow.run()
   ```
3. Run the TUI:
   ```
   python3 host/sensor_tui.py -e
   ```

### WiFi Mode

1. Copy `stick/` contents to device
2. Start the sensor on the device:
   ```
   import sensor_test_wifi
   sensor_test_wifi.run()
   ```
3. Run the TUI:
   ```
   python3 host/sensor_tui.py -w
   ```

---

## USB Transport

- Composite USB device
  - Interface 0/1: MicroPython CDC REPL
  - Interface 2: Vendor-specific bulk interface
- 4-byte framing:
  - channel (u8)
  - message type (u8)
  - payload length (u16 little-endian)
- Full duplex operation, binary payloads
- Channel discovery, Ping/Pong, dynamic registration, debug logging

---

## Sensors

- **GPIO41 button** - Push button input with debouncing
- **GPIO35 NeoPixel** - RGB LED output

The Linux TUI successfully receives button events and controls the RGB LED.

---

## ESP-NOW

### How It Works

- Wi-Fi STA is activated before ESP-NOW (required on ESP32)
- Radio sensor registers USB channel 3
- On-air message: 16-byte shared key header + application data
- USB event payload: 6-byte source MAC + 1-byte RSSI + application data

### Configuration

The ESP-NOW server reads `/config.json`:

```json
{
  "id":  "<device id>",
  "ble":  {"key": "<32 hex chars = 16-byte shared key>"},
  "wlan": {"addr": "<own MAC hex>"}
}
```

All ESP-NOW code uses `private.py`:

```python
ENOW_SERVER = <hex mac>      # Server MAC (client only)
ENOW_KEY = <hex key>        # Shared key (first 16 bytes = PMK)
ENOW_CHANNEL = <channel>     # Wi-Fi channel
```

**Note:** `private.py` is device-specific and must be created per deployment.

### Running the Client

Copy to a second ESP32 and run:
- `client/espnow_client_example.py`
- `stick/private.py`
- `config.json`

The client sends `sensor message <n>` every 5 seconds.

---

## WiFi AP Server

### How It Works

- Creates Access Point: SSID "MPY", password from `private.py`, WiFi RF channel 3
- TCP server on port 8080
- Gateway IP: 192.168.4.1 (always .1 of AP subnet)
- Security: WPA2 (no shared key needed)
- Clients identify themselves with a device id from `config.json` on connect

### Configuration

WiFi uses the same `config.json` as ESP-NOW, with an additional `device` field on each client:

```json
{
  "id":  "<device id>",
  "ble":  {"key": "<32 hex chars = 16-byte shared key>"},
  "wlan": {"addr": "<own MAC hex>"}
}
```

`device id` is a 16 bit unsigned int as a stable client identifier. The
stick wifi_server maps it to the client's current IP address whenever a
TCP connection is established.

Additional `private.py` settings:

```python
WIFI_SSID = "MPY"
WIFI_PASSWORD = "xxx"          # WPA2 auth
WIFI_CHANNEL = 3
WIFI_PORT = 8080
```

### Running the Server

On the AtomS3U device:

```python
import sensor_test_wifi
sensor_test_wifi.run()              # Start with debug=False
sensor_test_wifi.run(debug=True)    # Start with debug output
```

### Running the Client (MicroPython)

Copy to a second ESP32 and run:
- `client/wifi_client_example.py`
- `stick/private.py`
- `config.json` (must include a `device` field)

Client automatically uses gateway IP from WiFi interface config.
On every new TCP connection the client sends an identification frame
(`0x00` + `device` string) as the first packet.

### Running the Client (Linux)

```bash
python3 host/wifi_client.py -i wlan0              # Auto-detect gateway from interface
python3 host/wifi_client.py -i wlan0 -c 3         # Send 3 messages
python3 host/wifi_client.py -i wlan0 -m "hello"    # Single message
```

The Linux client also reads the `device` field from `config.json` and
sends it as the identification frame on connect.

### Client Identification

WiFi clients are identified by their `device` id (not IP, which can
change when DHCP reassigns addresses). On every new connection:

- Client sends an identification frame: 16 bit uint `device_id` (device_id_int.to_bytes(2, "little"))
- Server records `device_id <-> IP` in `self.device_by_ip` and
  `self.ip_by_device` mappings and removes them when the client closes
- The stick then forwards messages on this connection as USB events whose
  payload is `device_id (2 bytes) + message`
- The host `CTRL_GET_WIFI_CLIENTS` response lists each connected client
  with its current IP and device id; the TUI uses the device id for
  display and selection
- Outbound messages from the host carry the same header so the server
  can resolve the target IP from `device_id`

Clients that do not send a valid identification frame as the first
packet are dropped (`rejected` counter increments).

### TUI Usage (WiFi Mode)

- Incoming messages display the client device id (from `config.json`)
- Press `m` to enter message mode
- Up/Down arrows cycle through seen device ids
- Press Enter to send, Esc to cancel

The client derives gateway IP (.1 of local subnet) from the specified interface.

---

## Peer Management

Peer management is used only for ESP-NOW mode:

- Peers defined in `peers.json`: `[{"device": "<id>", "mac": "<hex>", "lmk": "<hex>"}]`
- Host loads this file and sends `MSG_PEER_ADD` / `MSG_PEER_DEL` to device
- Only authorized MAC addresses can communicate via ESP-NOW
- The `device` field gives each peer a stable id, useful in ESP-NOW mode
  to address messages without depending on MAC ordering

WiFi mode does not use peer management - any client with the WPA2 password can connect. Clients in WiFi mode are identified by the `device` field in their own `config.json` instead.

---

## Linux TUI

Run with: `python3 host/sensor_tui.py [options]`

Options:
- `--serial <serial>` - Select specific device
- `-e, --espnow` - Use ESP-NOW mode
- `-w, --wifi` - Use WiFi mode

Keys:
- `p` - Ping
- `c` - Read channel list
- `r/g/b/w/y/0` - RGB LED
- `d` - Toggle device debug
- `s` - Request gateway status
- `x` - Clear debug log / save to file
- `m` - Send message to peer
- `q` - Quit

---

## Dynamic Channel Loading

Sensors can be loaded dynamically at runtime via the `CTRL_LOAD_CHANNEL` control command. The device looks up the channel ID in a registry, imports the corresponding module and instantiates the sensor.

### Host Tool: channel_loader.py

Run with: `python3 host/channel_loader.py <command> [options]`

Commands:
- `rgb --pin <n>` - Load RGB LED sensor (channel 2)
- `button --pin <n>` - Load button sensor (channel 1)
- `espnow` - Load ESP-NOW radio (channel 3)
- `load --channel-id <n>` - Load any registered channel by ID

Options:
- `--device-serial <serial>` - Select specific device
- `--name <name>` - Channel name (optional)

Examples:
```bash
python3 host/channel_loader.py rgb --pin 35
python3 host/channel_loader.py button --pin 41 --name my-button
python3 host/channel_loader.py espnow
python3 host/channel_loader.py load --channel-id 2 --pin 35
```

### Protocol

Request: `CTRL_LOAD_CHANNEL + channel_id(1) + config_bytes`

Config format depends on channel type:
- RGB (id=2): `pin(1) + name_len(1) + name`
- Button (id=1): `pin(1) + name_len(1) + name`
- ESP-NOW (id=3): empty

Response: `MSG_STATUS` on success, `MSG_ERROR` on failure.

### Adding New Channel Types

To register a new channel type for dynamic loading, edit `stick/usb_channel_server.py`:

1. Add a config parser method (e.g., `_parse_my_sensor_config`)
2. Add an entry to `_channel_registry` mapping channel_id to:
   - `module`: Python module name to import
   - `class`: Class name to instantiate
   - `config_parser`: Method to parse config bytes
3. Register the parser in `_register_channel_parsers()`

Example:
```python
def _parse_my_sensor_config(self, config):
    return {"channel_id": 4, "param": config[0]}

_channel_registry[4] = {
    "module": "my_sensor",
    "class": "MySensor",
    "config_parser": None,
}

def _register_channel_parsers(self):
    self._channel_registry[CHANNEL_RGB]["config_parser"] = self._parse_rgb_config
    self._channel_registry[CHANNEL_BUTTON]["config_parser"] = self._parse_button_config
    self._channel_registry[4]["config_parser"] = self._parse_my_sensor_config
```

The corresponding sensor module (`my_sensor.py`) must exist on the device's filesystem.

---

## Device Installation

Copy to the board:

**Required:**
- `boot.py`
- `usb_channel_server.py`
- `channel_defs.py` (from common/)

**Sensor modules (required for static loading):**
- `button_sensor.py` (channel 1)
- `rgb_sensor.py` (channel 2)

These can also be loaded dynamically via `CTRL_LOAD_CHANNEL` (see Dynamic Channel Loading).

**Choose one wireless mode:**
- `espnow_server.py` + `sensor_test_espnow.py` (ESP-NOW mode)
- `wifi_server.py` + `sensor_test_wifi.py` (WiFi mode)

**Add (device-specific):**
- `config.json`
- `private.py`

Power-cycle after installation.

---

## REPL

```python
import usb_channel_server
gateway = usb_channel_server.get_gateway()
gateway.stats()
```

Start sensors:
```python
import sensor_test_espnow  # or sensor_test_wifi
sensor_test_espnow.run()
```

Stop:
```python
sensor_test_espnow.stop()
```

---

## Smoke Test

```bash
python3 tests/usb_channel_smoketest.py
```

Tests framing, ping, channel list, status, RGB round trip, button events, wireless receive path, peer management, and WiFi client list sync.

---

## Debugging

```python
gateway.set_debug(True)   # Enable
gateway.set_debug(False)  # Disable
gateway.dump_debug()      # Print log
gateway.clear_debug()     # Clear
```

---

## Important Implementation Detail

On ESP32-S3 MicroPython (v1.27.0), `USBDevice.submit_xfer()` may complete **synchronously**.

The USB channel server marks an IN transfer as busy **before** calling `submit_xfer()` to avoid recursive submission.

---

## Configuration Files

### config.json (per device)
```json
{
  "device": "sensor1",
  "id": "device-001",
  "ble": {"key": "00112233445566778899aabbccddeeff"},
  "wlan": {"addr": "aabbccddeeff"}
}
```

The `device` field is used by WiFi clients as their identification on
the first packet of every TCP connection. ESP-NOW mode ignores it.

### private.py (per device)
```python
# ESP-NOW
ENOW_SERVER = "aabbccddeeff"
ENOW_KEY = "00112233445566778899aabbccddeeff"
ENOW_CHANNEL = 3

# WiFi
WIFI_SSID = "MPY"
WIFI_PASSWORD = "xxx"
WIFI_CHANNEL = 3
WIFI_PORT = 8080
WIFI_KEY = "00112233445566778899aabbccddeeff"
```

### peers.json (optional, for multiple clients)
```json
[
  {"device": "1", "mac": "aabbccddeeff0011", "lmk": "00112233445566778899aabbccddeeff"}
]
```

---

## Filesystem Operations

The gateway supports filesystem operations on the device, similar to `mpremote` commands. These are implemented via the control channel using `MSG_COMMAND` with `CTRL_FS_*` commands. Large files are transferred in 960-byte chunks.

### Host Tool: fs_util.py

Run with: `python3 host/fs_util.py <command> [options]`

Commands:
- `ls [path]` - List directory contents (like `mpremote ls`)
- `cat <path> [-o <file>]` - Read file contents (like `mpremote cat`)
- `put <src> [dst]` - Write local file to device (like `mpremote cp`)
- `rm <path>` - Delete file from device
- `exists <path>` - Check if file exists on device
- `reset` - Reset the device via `machine.reset()`

Options:
- `--device-serial <serial>` - Select specific device by USB serial number

Examples:
```bash
python3 host/fs_util.py ls /                # List root directory
python3 host/fs_util.py ls /flash           # List flash filesystem
python3 host/fs_util.py cat /main.py        # Read text file to stdout
python3 host/fs_util.py cat /image.bin -o local.bin  # Save binary file locally
python3 host/fs_util.py put local.py /main.py  # Write file to device
python3 host/fs_util.py put local.bin /data.bin  # Write binary file (images, etc)
python3 host/fs_util.py rm /test.py         # Delete file from device
python3 host/fs_util.py exists /boot.py     # Check if file exists
python3 host/fs_util.py reset               # Reset the device
```

### Protocol

All filesystem commands use `MSG_COMMAND` on channel 0 (control) with the following format:

```
Payload: CTRL_FS_* (1 byte) + command-specific data
Response: MSG_FS_RESPONSE with operation result
Error: MSG_ERROR on failure
```

#### CTRL_FS_LIST (0x10)
- Request: `CTRL_FS_LIST + path string (utf-8)`
- Response: List of entries, each: `name_len(1) + name + is_dir(1) + size(4)`

#### CTRL_FS_READ (0x11)
- Request: `CTRL_FS_READ + path + null + offset(4) + size(4)`
- Response: Raw file contents (up to max_payload bytes)
- Large files are read in 960-byte chunks by the host

#### CTRL_FS_WRITE (0x12)
- Request: `CTRL_FS_WRITE + path + null + offset(4) + data`
- Response: `bytes_written (u32)`
- Large files are written in 960-byte chunks, appended sequentially

#### CTRL_FS_DELETE (0x13)
- Request: `CTRL_FS_DELETE + path string (utf-8)`
- Response: `1` on success, `0` on failure

#### CTRL_FS_EXISTS (0x14)
- Request: `CTRL_FS_EXISTS + path string (utf-8)`
- Response: `1` if exists, `0` if not

#### CTRL_RESET (0x21)
- Request: `CTRL_RESET`
- Response: `MSG_STATUS` immediately before reset
- Effect: Calls `machine.reset()` on the device

### Binary File Support

- `put` automatically handles binary files (images, etc.)
- `cat` detects binary files and refuses to output to terminal (use `-o` to save)
- Device uses binary file modes (`rb`, `wb`, `ab`)

### Device Implementation

The filesystem handlers are implemented in `stick/usb_channel_server.py` in the `_handle_fs_*` methods. They use MicroPython's `os` module for filesystem operations:
- `os.listdir()` - List directory
- `os.stat()` - Get file info
- `open(path, "rb")` - Read file
- `open(path, "wb")` - Write file (truncates)
- `open(path, "ab")` - Append to file
- `os.remove()` - Delete file
- `os.path.exists()` - Check existence

---

## Radio File Transfer Utility

The `radio_util.py` tool provides file transfer capabilities over ESP-NOW or WiFi radio links.

### Overview

- Supports both ESP-NOW (`-e`) and WiFi (`-w`) modes
- Checks if required radio channel is loaded, loads it if not
- Prompts for confirmation when switching between WiFi and ESP-NOW (they are mutually exclusive)
- Implements a robust transfer protocol with handshake, per-segment ACK, and echo verification
- Hard fails (exit code 1) if the target device does not ACK handshake (3 sec timeout)

### Transfer Protocol

The protocol uses a 16-bit header per packet:
- Upper 4 bits: packet ID (0-15)
- Lower 12 bits: segment number (0-4095), sequential 0, 1, 2, ...

**Handshake Phase:**
1. Source sends `MSG_FILE_HANDSHAKE` with: `packet_id(1) + total_size(4)`
2. Target responds with `MSG_FILE_ACK` to confirm buffer allocation
3. If no ACK within 3 seconds: transfer aborts

**Data Transfer Phase:**
1. Source sends `MSG_FILE_DATA` with: `header(2) + segment_data(~900 bytes)`
2. Segment size is limited to 900 bytes (fits wifi_server's 1024 max_packet)
3. Target responds with `MSG_FILE_ACK` per segment
4. Target echoes back the data for verification
5. Source waits for both ACK and echo per segment

### Host Tool: radio_util.py

```bash
# Send test data (5KB) to device 30 over WiFi
python3 host/radio_util.py -w -t -d 30

# Poll for incoming client data
python3 host/radio_util.py -w -p

# List connected WiFi clients
python3 host/radio_util.py -w -l
```

Options:
- `-e, --espnow` - Use ESP-NOW radio (USB channel 3)
- `-w, --wifi` - Use WiFi server (USB channel 4)
- `-t, --test` - Run host test mode (send test packet)
- `-p, --poll` - Poll for incoming client data
- `-l, --list` - List connected WiFi clients (see Known Issue)
- `-d, --device ID` - Target device ID for WiFi (e.g. "30")

### Host Tool: radio_util_test.py

A simpler test utility with similar functionality but explicit per-action flags.

```bash
# Send 3KB to device 30 over WiFi
python3 host/radio_util_test.py -w -s 3000 -d 30

# Receive mode (wait for incoming data, 60 sec timeout)
python3 host/radio_util_test.py -w -r -t 60

# Send 1KB to device over ESP-NOW
python3 host/radio_util_test.py -e -s 1000
```

Options:
- `-e, --espnow` - Use ESP-NOW radio
- `-w, --wifi` - Use WiFi server
- `-s, --send SIZE` - Send test data of specified size
- `-r, --receive` - Receive mode (wait for incoming data)
- `-l, --list` - List connected WiFi clients
- `-d, --device ID` - Target device ID
- `-t, --timeout SEC` - Receive timeout (default: 30)

### MicroPython Clients

Both clients connect to the server's WiFi AP (port 8080) or use ESP-NOW. They reuse the existing `private.py` and `config.json`.

**client/radio_test_echo_wifi.py** - WiFi echo client:
- Connects to server AP on port 8080
- Sends identification frame (0x00 + device_id)
- Waits for incoming handshake/data
- Sends ACK + echoes data back
```bash
mpremote run client/radio_test_echo_wifi.py
```

**client/radio_test_echo_espnow.py** - ESP-NOW echo client:
- Receives handshake/data via ESP-NOW
- Sends ACK + echoes data back
```bash
mpremote run client/radio_test_echo_espnow.py
```

**client/radio_util_wifi.py** - WiFi test client (imports and runs):
```python
import radio_util_wifi
radio_util_wifi.run_test()  # Sends handshake + segments, waits for ACKs/echoes
```

**client/radio_util_espnow.py** - ESP-NOW test client:
```python
import radio_util_espnow
radio_util_espnow.run_test()      # Test sequence
radio_util_espnow.run_echo_mode() # Echo server
```

### Protocol Constants

The following message types are defined in `common/channel_defs.py`:
- `MSG_FILE_HANDSHAKE = 0x30` - Transfer initialization
- `MSG_FILE_DATA = 0x31` - Data segment transfer
- `MSG_FILE_ACK = 0x32` - Acknowledgment

### Known Issue: None (Fixed)

The `CTRL_GET_WIFI_CLIENTS` bug where the host always received "Connected clients (0)" despite active clients has been fixed in `stick/wifi_server.py` and `stick/espnow_server.py` by introducing `_WiFiHandlerWrapper` and `_ESPNowHandlerWrapper` classes. These wrappers are callable (handler protocol) and also expose `get_client_list()`, allowing the USB channel server to access the WiFiServer's client list properly.

---

## Next Steps

- Add sensor base class
- Add I²C sensor channels
- Add SPI sensor channels
- Add channel hot-plug notifications
