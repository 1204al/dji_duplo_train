#!/usr/bin/env python3
"""Receive DJI RC RM330 Wi-Fi controls through staggered TCP readers.

Receive-only by default. Optional version queries/session replies are enabled
with --keepalive version. Shared framing/layout helpers are in dji_remote.py.
"""

import argparse
import errno
import ipaddress
import math
import queue
import socket
import struct
import subprocess
import sys
import threading
import time

from dji_remote import (MAGIC, PacketParser, crc, decode_controls, local_network,
                        output_state, scan, status, version_query)


def session_reply(packet):
    if packet[8] & 0x80:
        return None
    ack_requested = (packet[8] >> 5) & 3
    poll = packet[9:11] in (b"\x00\x77", b"\x18\x40")
    if not ack_requested and not poll:
        return None
    payload = b"" if ack_requested else packet[11:-2]
    header = b"\x55" + struct.pack("<H", (13 + len(payload)) | 0x400)
    reply = header + bytes([crc(header, 0x77, 0x8C), packet[5], packet[4]])
    reply += packet[6:8] + bytes([0x80]) + packet[9:11] + payload
    reply += struct.pack("<H", crc(reply, 0x3692, 0x8408))
    return MAGIC + struct.pack("<I", len(reply)) + reply


def listen(args, ip):
    start = time.monotonic()
    deadline = start + args.seconds if args.seconds else float("inf")
    events = queue.Queue(maxsize=256)
    stop = threading.Event()
    reconnects = [0] * args.connections
    active = args.keepalive == "version" and not args.passive

    def publish(event):
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                events.put(event, timeout=0.05)
                return
            except queue.Full:
                pass

    def reader(index):
        if stop.wait(min(index * 0.5, max(0, deadline - time.monotonic()))):
            return
        while not stop.is_set() and time.monotonic() < deadline:
            had_controls = False
            try:
                with socket.create_connection((ip, args.port), timeout=0.3) as connection:
                    connection.settimeout(0.05)
                    parser = PacketParser()
                    next_query, sequence = 0, 1
                    last_controls = time.monotonic()
                    if args.debug:
                        publish(("debug", f"Link {index + 1}: TCP connected."))
                    while not stop.is_set() and time.monotonic() < deadline:
                        now = time.monotonic()
                        if active and now >= next_query:
                            connection.sendall(version_query(sequence))
                            sequence = (sequence + 1) & 0xFFFF
                            next_query = now + 0.5
                        try:
                            data = connection.recv(8192)
                        except TimeoutError:
                            data = None
                        received_at = time.monotonic()
                        if data == b"":
                            raise ConnectionError("Controller closed the connection.")
                        for packet in parser.feed(data) if data else []:
                            if active:
                                reply = session_reply(packet)
                                if reply is not None:
                                    connection.sendall(reply)
                            state = decode_controls(packet)
                            if state is not None:
                                publish(("controls", received_at, state))
                                had_controls, last_controls = True, received_at
                        if received_at - last_controls > 5:
                            raise ConnectionError("No valid controller frames for 5 seconds.")
            except OSError as exc:
                if exc.errno in (errno.EPERM, errno.EACCES):
                    publish(("error", PermissionError(
                        "Network access denied; allow Local Network access for the terminal.")))
                    return
                reconnects[index] += 1
                if args.debug:
                    publish(("debug", f"Link {index + 1}: {exc} Reconnecting..."))
            stop.wait(min(0.1 if had_controls else 0.35,
                          max(0, deadline - time.monotonic())))

    workers = [threading.Thread(target=reader, args=(index,), name=f"dji-reader-{index + 1}")
               for index in range(args.connections)]
    count, previous = 0, None
    last_frame, max_silence, stale = start, 0.0, False
    status(f"Listening to {ip}:{args.port} using {args.connections} staggered "
           f"TCP links ({'version queries' if active else 'receive only'}).")
    try:
        for worker in workers:
            worker.start()
        while time.monotonic() < deadline:
            try:
                event = events.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                event = None
            if event:
                kind, *values = event
                if kind == "error":
                    raise values[0]
                if kind == "debug":
                    status(values[0])
                if kind == "controls":
                    received_at, state = values
                    # Constant DUML sequence numbers cannot order these samples.
                    if received_at < last_frame:
                        continue
                    max_silence = max(max_silence, received_at - last_frame)
                    if not count:
                        status("Valid stick/button frames received. Move sticks or press buttons.")
                    elif stale:
                        status(f"Controller input resumed after {received_at - last_frame:.2f}s.")
                    output_state(state, previous, args.json, args.raw)
                    previous = state  # Preserve held buttons across link resets.
                    count += 1
                    last_frame, stale = received_at, False
            if not stale and time.monotonic() - last_frame >= 1:
                status("No controller input for 1 second; input is stale. Readers keep reconnecting.")
                stale = True
    finally:
        max_silence = max(max_silence, time.monotonic() - last_frame)
        stop.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join()
        status(f"Stopped; received {count} controller frames; longest input silence "
               f"{max_silence:.2f}s; {sum(reconnects)} TCP reconnects.")
    return 0 if count else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("scan", "listen"))
    parser.add_argument("--ip", type=ipaddress.IPv4Address)
    parser.add_argument("--network", type=lambda value: ipaddress.IPv4Network(value, strict=False))
    parser.add_argument("--port", type=int, default=40007)
    parser.add_argument("--seconds", type=float, default=0, help="Duration; 0 means until Ctrl+C")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--raw", action="store_true")
    parser.add_argument("--connections", type=int, default=5, help="TCP readers, 1..8 (default: 5)")
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--keepalive", choices=("off", "version"), default="off")
    transport.add_argument("--passive", action="store_true", help="Alias for --keepalive off")
    parser.add_argument("--debug", action="store_true", help="Log individual link connections and resets")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not math.isfinite(args.seconds) or args.seconds < 0:
        parser.error("Port must be 1..65535 and seconds must be finite and nonnegative.")
    if not 1 <= args.connections <= 8:
        parser.error("Connections must be 1..8.")
    try:
        if args.command == "scan" or args.ip is None:
            import json
            network = args.network or local_network()
            status(f"Checking {network} for the DJI input service on TCP {args.port}...")
            candidates = scan(network, args.port)
            if args.command == "scan":
                print(json.dumps({"network": str(network), "port": args.port, "candidates": candidates}))
                return 0 if candidates else 2
            verified = [item for item in candidates if item["duml_verified"]]
            if len(verified) != 1:
                status("No unique verified DJI stream found; supply the controller's exact --ip.")
                return 2
            ip = verified[0]["ip"]
        else:
            ip = str(args.ip)
        status("Using DJI RC RM330 labels. Ctrl+C stops listening.")
        return listen(args, ip)
    except KeyboardInterrupt:
        status("Listener stopped.")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        status(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
