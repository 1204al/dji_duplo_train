#!/usr/bin/env python3
"""Listen to a DJI screen controller's Wi-Fi stick/button stream.

Python standard library only. Protocol/layout reference:
https://github.com/stiad/dji-rc-linux (MIT).
The observed layout is for DJI RC RM330; other models need live verification.
The CLI uses the staggered, receive-only Wi-Fi reader in dji_wifi.py.
Protocol helpers remain here for the USB reader and web bridge.
"""

import concurrent.futures
from datetime import datetime, timezone
import errno
import ipaddress
import json
import re
import socket
import struct
import subprocess
import sys
import time


MAGIC = bytes.fromhex("55 cc 30 75")
AXES = {"left_x": 5, "left_y": 4, "right_x": 2, "right_y": 3,
        "left_wheel": 6, "right_wheel": 7}
BUTTONS = {"rth_pause": (0, 0x20), "record": (0, 0x100),
           "camera_half": (0, 0x80), "camera_full": (0, 0x40),
           "c1": (1, 0x04), "c2": (1, 0x08)}


def crc(data, initial, polynomial):
    value = initial
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (polynomial if value & 1 else 0)
    return value


def version_query(sequence):
    header = bytes.fromhex("55 0d 04")
    packet = header + bytes([crc(header, 0x77, 0x8C), 2, 6])
    packet += struct.pack("<H", sequence & 0xFFFF) + bytes.fromhex("00 00 01")
    packet += struct.pack("<H", crc(packet, 0x3692, 0x8408))
    return MAGIC + struct.pack("<I", len(packet)) + packet


class PacketParser:
    """Keep partial TCP frames and reject damaged DUML packets."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        packets = []
        while True:
            offset = self.buffer.find(MAGIC)
            if offset < 0:
                # Retain a possible split magic prefix, discarding other noise.
                self.buffer[:] = self.buffer[-3:]
                break
            del self.buffer[:offset]
            if len(self.buffer) < 8:
                break
            size = struct.unpack_from("<I", self.buffer, 4)[0]
            if not 13 <= size <= 1023:
                del self.buffer[0]
                continue
            # A valid DUML header is available before the entire frame.
            if len(self.buffer) >= 12:
                header = self.buffer[8:12]
                length = (header[1] | header[2] << 8) & 0x3FF
                if (header[0] != 0x55 or length != size
                        or header[2] >> 2 != 1
                        or crc(header[:3], 0x77, 0x8C) != header[3]):
                    del self.buffer[0]
                    continue
            if len(self.buffer) < size + 8:
                break
            packet = bytes(self.buffer[8:8 + size])
            del self.buffer[:8 + size]
            if crc(packet[:-2], 0x3692, 0x8408) == int.from_bytes(packet[-2:], "little"):
                packets.append(packet)
        return packets


def decode_controls(packet):
    if packet[9:11] != b"\x06\xae":
        return None
    payload = packet[11:-2]
    if len(payload) < 17:
        return None
    words = struct.unpack_from("<HH", payload)
    raw_axes = {name: 1024 - int.from_bytes(payload[2 * channel + 1:2 * channel + 3], "little")
                for name, channel in AXES.items()}
    return {
        "axes": {name: round(max(-1.0, min(1.0, value / 660)), 4)
                 for name, value in raw_axes.items()},
        "axis_raw": raw_axes,
        "buttons": {name: bool(words[word] & mask)
                    for name, (word, mask) in BUTTONS.items()},
        "mode": ("sport", "normal", "cinema", "unknown")[words[1] & 3],
        "payload_hex": payload.hex(),
    }


def local_network():
    output = subprocess.check_output(["ifconfig"], text=True)
    networks = set()
    for block in re.split(r"(?=^\S+: flags=)", output, flags=re.MULTILINE):
        if not block.startswith("en") or "status: active" not in block:
            continue
        for address, mask in re.findall(r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-f]+)", block):
            ip = ipaddress.IPv4Address(address)
            if not ip.is_private or ip.is_loopback or ip.is_link_local:
                continue
            bits = bin(int(mask, 16)).count("1")
            network = ipaddress.ip_network(f"{address}/{bits}", strict=False)
            if network.num_addresses > 256:
                raise ValueError("Local network is larger than /24; supply a specific --network /24 or --ip.")
            networks.add(network)
    if len(networks) != 1:
        raise ValueError("Cannot select one local Wi-Fi network; supply --network, e.g. 192.168.1.0/24.")
    return networks.pop()


def scan(network, port):
    """Check one input port on at most 254 local addresses; no DUML writes."""
    if network.num_addresses > 256 or not network.is_private:
        raise ValueError("Discovery requires a private IPv4 network of /24 or smaller.")

    def probe(address):
        try:
            with socket.create_connection((str(address), port), timeout=0.4) as connection:
                connection.settimeout(0.6)
                parser = PacketParser()
                until = time.monotonic() + 0.6
                while time.monotonic() < until:
                    data = connection.recv(4096)
                    if not data:
                        break
                    if parser.feed(data):
                        return {"ip": str(address), "duml_verified": True}
                return {"ip": str(address), "duml_verified": False}
        except TimeoutError:
            # The port accepted TCP but did not send a complete DUML packet.
            if "connection" in locals():
                return {"ip": str(address), "duml_verified": False}
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES):
                raise PermissionError("Network access denied; run from an allowed terminal and allow Local Network access.") from exc
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        return [result for result in pool.map(probe, network.hosts()) if result]


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def status(message):
    print(message, file=sys.stderr, flush=True)


def output_state(state, previous, as_json, raw):
    changes = {name: "pressed" if held else "released"
               for name, held in state["buttons"].items()
               if held != (previous or {}).get("buttons", {}).get(name, False)}
    if as_json:
        event = {"type": "controls", "timestamp": timestamp(),
                 "axes": state["axes"], "buttons": state["buttons"],
                 "mode": state["mode"], "button_changes": changes}
        if raw:
            event.update(axis_raw=state["axis_raw"], payload_hex=state["payload_hex"])
        print(json.dumps(event), flush=True)
    else:
        for name, change in changes.items():
            print(f"{timestamp()} BUTTON {name}: {change}", flush=True)
        if previous is None or state["axes"] != previous["axes"] or state["mode"] != previous["mode"]:
            axes = " ".join(f"{name}={value:+.3f}" for name, value in state["axes"].items())
            print(f"{timestamp()} {axes} mode={state['mode']}", flush=True)
        if raw and (previous is None or state["payload_hex"] != previous["payload_hex"]):
            print(f"RAW {state['payload_hex']}", flush=True)


def listen(args, ip):
    """Keep older Python callers on the same improved reader as the CLI."""
    from dji_wifi import listen as wifi_listen
    for name, default in (("connections", 5), ("keepalive", "off"),
                          ("passive", False), ("debug", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    return wifi_listen(args, ip)


def main():
    # Import after defining the shared helpers to avoid an initialization cycle.
    from dji_wifi import main as wifi_main
    return wifi_main()


if __name__ == "__main__":
    sys.exit(main())
