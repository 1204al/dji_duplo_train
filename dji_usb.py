#!/usr/bin/env python3
"""Read DJI RC RM330 sticks and wheels through its USB-C data port.

Requires pyusb and libusb-package. Only the read-sticks DUML command is sent;
the MTP/ADB interfaces and simulator/flight/firmware settings are not used.
USB packet layout reference: https://github.com/deviverr/DJI-RC-Emulator (MIT).
"""

import argparse
import math
import struct
import sys
import time

from dji_remote import crc, output_state, status


VENDOR = 0x2CA3
PRODUCT = 0x1023
CHANNELS = ("right_x", "right_y", "left_y", "left_x", "left_wheel", "right_wheel")


def read_sticks_query(sequence):
    header = bytes.fromhex("55 0d 04")
    packet = header + bytes([crc(header, 0x77, 0x8C), 0x0A, 6])
    packet += struct.pack("<H", sequence & 0xFFFF) + bytes.fromhex("40 06 01")
    return packet + struct.pack("<H", crc(packet, 0x3692, 0x8408))


class USBPacketParser:
    """Parse native DUML, including split reads, noise and bad checksums."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        packets = []
        while self.buffer:
            offset = self.buffer.find(b"\x55")
            if offset < 0:
                self.buffer.clear()
                break
            del self.buffer[:offset]
            if len(self.buffer) < 4:
                break
            size = int.from_bytes(self.buffer[1:3], "little") & 0x3FF
            if (not 13 <= size <= 1023 or self.buffer[2] >> 2 != 1
                    or crc(self.buffer[:3], 0x77, 0x8C) != self.buffer[3]):
                del self.buffer[0]
                continue
            if len(self.buffer) < size:
                break
            packet = bytes(self.buffer[:size])
            if crc(packet[:-2], 0x3692, 0x8408) != int.from_bytes(packet[-2:], "little"):
                del self.buffer[0]
                continue
            del self.buffer[:size]
            packets.append(packet)
        return packets


def decode_controls(packet):
    # Channel-parameter response: success status, six LE16 values with tags 1..6.
    if (len(packet) != 32 or packet[4:6] != b"\x06\x0a"
            or not packet[8] & 0x80 or packet[9:11] != b"\x06\x01"):
        return None
    payload = packet[11:-2]
    if payload[0] != 0 or tuple(payload[3::3]) != tuple(range(1, 7)):
        return None
    raw = {name: 1024 - int.from_bytes(payload[1 + 3 * i:3 + 3 * i], "little")
           for i, name in enumerate(CHANNELS)}
    return {
        "axes": {name: round(max(-1, min(1, value / 660)), 4) for name, value in raw.items()},
        "axis_raw": raw,
        # Physical C1/C2 presses did not change this payload. Empty means
        # unavailable, rather than falsely declaring every button released.
        "buttons": {},
        "mode": "unknown",  # This USB response has no flight-mode switch field.
        "payload_hex": payload.hex(),
    }


def load_usb():
    try:
        import usb.core
        import usb.util
        import libusb_package
    except ImportError as exc:
        raise RuntimeError("USB dependencies missing; install requirements-dji-usb.txt "
                           "in the Python environment used to run this script.") from exc
    backend = libusb_package.get_libusb1_backend()
    if backend is None:
        raise RuntimeError("Cannot load the bundled libusb library on this computer.")
    return usb, backend


def open_remote(usb, backend):
    devices = list(usb.core.find(find_all=True, idVendor=VENDOR, idProduct=PRODUCT, backend=backend))
    if not devices:
        raise ConnectionError("DJI RC RM330 not found; connect its normal USB-C port with a data cable.")
    if len(devices) != 1:
        raise RuntimeError("More than one DJI RC RM330 is connected; leave one connected for this listener.")
    device = devices[0]
    try:
        # Read the current configuration without changing the Android USB mode.
        config = device.get_active_configuration()
        interfaces = [item for item in config
                      if (item.bInterfaceClass, item.bInterfaceSubClass, item.bInterfaceProtocol)
                      == (0xFF, 0x43, 1)]
        if len(interfaces) != 1:
            raise RuntimeError("The controller does not expose one DUML USB data interface.")
        interface = interfaces[0]
        endpoints = [ep for ep in interface if ep.bmAttributes & 3 == 2]
        incoming = next(ep for ep in endpoints if ep.bEndpointAddress & 0x80)
        outgoing = next(ep for ep in endpoints if not ep.bEndpointAddress & 0x80)
        usb.util.claim_interface(device, interface.bInterfaceNumber)
        return device, interface.bInterfaceNumber, incoming, outgoing
    except BaseException:
        usb.util.dispose_resources(device)
        raise


def listen(args, usb, backend):
    start = time.monotonic()
    deadline = start + args.seconds if args.seconds else float("inf")
    previous, count, reconnects = None, 0, 0
    last_frame, max_silence = start, 0.0
    sequence, stale = 0, False
    last_error = None
    try:
        while time.monotonic() < deadline:
            device = None
            try:
                device, interface, incoming, outgoing = open_remote(usb, backend)
                parser = USBPacketParser()
                status(f"USB connected to DJI RC RM330; polling sticks/wheels at {args.hz:g} Hz.")
                last_error = None
                connection_started = time.monotonic()
                while time.monotonic() < deadline:
                    tick = time.monotonic()
                    sequence = (sequence + 1) & 0xFFFF
                    outgoing.write(read_sticks_query(sequence), timeout=200)
                    until = min(tick + 0.2, deadline)
                    matched = False
                    while time.monotonic() < until and not matched:
                        timeout = max(1, math.ceil((until - time.monotonic()) * 1000))
                        try:
                            data = bytes(incoming.read(4096, timeout=timeout))
                        except usb.core.USBTimeoutError:
                            break
                        for packet in parser.feed(data):
                            # Discard replies to earlier polls, including data
                            # remaining in the endpoint from an earlier reader.
                            if int.from_bytes(packet[6:8], "little") != sequence:
                                continue
                            state = decode_controls(packet)
                            if state is None:
                                continue
                            now = time.monotonic()
                            max_silence = max(max_silence, now - last_frame)
                            if stale:
                                status(f"USB input resumed after {now - last_frame:.2f}s.")
                            output_state(state, previous, args.json, args.raw)
                            previous, last_frame, stale = state, now, False
                            count += 1
                            matched = True
                            break
                    if not stale and time.monotonic() - last_frame >= 1:
                        status("No USB input for 1 second; input is stale.")
                        stale = True
                    if time.monotonic() - max(connection_started, last_frame) >= 2:
                        raise ConnectionError("No valid USB stick responses for 2 seconds.")
                    delay = min(tick + 1 / args.hz, deadline) - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
            except (usb.core.USBError, ConnectionError) as exc:
                if getattr(exc, "errno", None) in (1, 13):
                    raise PermissionError("USB access denied; close other apps using the DJI USB data interface.") from exc
                reconnects += 1
                message = str(exc)
                if message != last_error:
                    status(f"{message} Reconnecting over USB...")
                    last_error = message
            finally:
                if device is not None:
                    try:
                        usb.util.release_interface(device, interface)
                    except usb.core.USBError:
                        pass  # A disconnected device may already be gone.
                    usb.util.dispose_resources(device)
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    finally:
        elapsed = time.monotonic() - start
        max_silence = max(max_silence, time.monotonic() - last_frame)
        status(f"Stopped; {count} USB control frames in {elapsed:.2f}s "
               f"({count / max(elapsed, 0.001):.1f} Hz); "
               f"longest input silence {max_silence:.3f}s; {reconnects} reconnects.")
    return 0 if count else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("scan", "listen"))
    parser.add_argument("--seconds", type=float, default=0, help="Duration; 0 means until Ctrl+C")
    parser.add_argument("--hz", type=float, default=50, help="Polling frequency, 1..100 (default: 50)")
    parser.add_argument("--json", action="store_true", help="One JSON control event per line")
    parser.add_argument("--raw", action="store_true", help="Include raw axes and the channel payload")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds < 0 or not 1 <= args.hz <= 100:
        parser.error("Seconds must be finite and nonnegative; hz must be 1..100.")
    try:
        usb, backend = load_usb()
        if args.command == "scan":
            device, interface, incoming, outgoing = open_remote(usb, backend)
            try:
                print(f"DJI RC RM330: USB {VENDOR:04x}:{PRODUCT:04x}, DUML interface {interface}, "
                      f"IN 0x{incoming.bEndpointAddress:02x}, OUT 0x{outgoing.bEndpointAddress:02x}")
            finally:
                usb.util.release_interface(device, interface)
                usb.util.dispose_resources(device)
            return 0
        return listen(args, usb, backend)
    except KeyboardInterrupt:
        status("USB listener stopped.")
        return 0
    except (RuntimeError, OSError, usb.core.USBError if "usb" in locals() else OSError) as exc:
        status(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
