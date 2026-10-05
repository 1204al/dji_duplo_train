"""Extract one DUPLO connection from an Apple PacketLogger file, entirely offline.

No Bluetooth writes, phone access, or network requests are performed.
Format reference: Wireshark wiretap/packetlogger.c. Protocol reference: LEGO LWP3.
"""

import argparse
import json
import struct
import sys
from pathlib import Path

from lego_protocol import decode_packet


def records(stream):
    endian = None
    index = 0
    while header := stream.read(12):
        index += 1
        if len(header) != 12:
            raise ValueError(f"Truncated PacketLogger header at record {index}.")
        if endian is None:
            valid = [order for order in ('>', '<')
                     if 8 <= struct.unpack(order + 'III', header)[0] < 65536
                     and struct.unpack(order + 'III', header)[2] < 1000000]
            if len(valid) != 1:
                raise ValueError("Cannot determine PacketLogger byte order.")
            endian = valid[0]
        length, seconds, micros = struct.unpack(endian + 'III', header)
        if not 8 <= length < 65536 or micros >= 1000000:
            raise ValueError(f"Invalid PacketLogger header at record {index}.")
        body = stream.read(length - 8)
        if len(body) != length - 8:
            raise ValueError(f"Truncated PacketLogger payload at record {index}.")
        if body:
            yield seconds + micros / 1000000, body[0], body[1:]


def att_packets(stream):
    """Reassemble fixed ATT-channel L2CAP packets; isolate reused HCI handles."""
    pending, generations = {}, {}
    for timestamp, packet_type, payload in records(stream):
        if packet_type == 1 and len(payload) >= 6 and payload[:2] == b'\x05\x04':
            handle = int.from_bytes(payload[3:5], 'little') & 0x0fff
            generations[handle] = generations.get(handle, 0) + 1
            for key in list(pending):
                if key[0] == handle:
                    del pending[key]
            continue
        if packet_type not in (2, 3) or len(payload) < 4:
            continue
        flags, size = struct.unpack('<HH', payload[:4])
        if size != len(payload) - 4:
            raise ValueError("Invalid HCI ACL payload length.")
        handle, boundary = flags & 0x0fff, (flags >> 12) & 3
        key = (handle, packet_type)
        data = payload[4:]
        if boundary in (0, 2, 3):
            pending.pop(key, None)
            if len(data) < 4:
                raise ValueError("Truncated L2CAP header.")
            length, channel = struct.unpack('<HH', data[:4])
            if channel != 4:
                continue
            pending[key] = [timestamp, length, bytearray(data[4:])]
        elif boundary == 1 and key in pending:
            pending[key][2].extend(data)
        else:
            continue
        start_time, length, assembled = pending[key]
        if len(assembled) > length:
            raise ValueError("HCI fragments exceed the advertised L2CAP length.")
        if len(assembled) == length:
            del pending[key]
            connection = f'0x{handle:04x}:{generations.get(handle, 0)}'
            yield start_time, connection, 'TX' if packet_type == 2 else 'RX', bytes(assembled)
    if pending:
        raise ValueError("Capture ends with incomplete ATT fragments.")


def value_from_att(packet):
    if len(packet) >= 3 and packet[0] in (0x12, 0x52, 0x1b, 0x1d):
        return int.from_bytes(packet[1:3], 'little'), None, packet[3:]
    if len(packet) >= 5 and packet[0] in (0x16, 0x17):
        return int.from_bytes(packet[1:3], 'little'), int.from_bytes(packet[3:5], 'little'), packet[5:]
    return None, None, None


def lwp_message(value):
    if not value:
        return None
    prefix = 2 if value[0] & 0x80 else 1
    if len(value) < prefix + 2:
        return None
    length = (value[0] & 0x7f) | (value[1] << 7) if prefix == 2 else value[0]
    if length != len(value) or value[prefix] != 0:
        return None
    return value[prefix + 1], value[prefix + 2:]


def duplo_signature(value):
    message = lwp_message(value)
    if message is None:
        return False
    kind, body = message
    if kind == 1 and body[:2] == b'\x01\x06':
        return body[2:].upper().startswith(b'DUPLO')
    return (kind == 4 and len(body) >= 4 and body[1] == 1
            and (body[0], int.from_bytes(body[2:4], 'little')) in ((51, 0x5b), (52, 0x5a)))


def extract(stream, connection=None):
    """Require train-specific evidence before emitting any connection's contents."""
    start = stream.tell()
    candidates = set()
    for _, candidate, _, packet in att_packets(stream):
        _, _, value = value_from_att(packet)
        if value is not None and duplo_signature(value):
            candidates.add(candidate)
    if connection is None:
        if len(candidates) != 1:
            detail = ', '.join(sorted(candidates)) or 'none'
            raise ValueError(f"Expected one identifiable DUPLO session; found {detail}. Capture from app connection, or select --connection.")
        connection = next(iter(candidates))
    elif connection not in candidates:
        raise ValueError("Selected connection has no DUPLO name or 10427 port signature; no data exported.")
    stream.seek(start)
    origin, events, write_bytes = None, [], 0
    for timestamp, candidate, direction, packet in att_packets(stream):
        if candidate != connection or not packet:
            continue
        if origin is None:
            origin = timestamp
        attribute, offset, value = value_from_att(packet)
        event = {'elapsed_seconds': round(timestamp - origin, 6), 'direction': direction,
                 'att_opcode': f'0x{packet[0]:02x}', 'att_raw': packet.hex(' ')}
        if attribute is not None:
            event['attribute'] = f'0x{attribute:04x}'
            event['value_raw'] = value.hex(' ')
            if offset is not None:
                event['offset'] = offset
            if direction == 'TX' and packet[0] in (0x12, 0x52, 0x16):
                write_bytes += len(value)
            message = lwp_message(value)
            if message is not None:
                event['lego_message_type'] = f'0x{message[0]:02x}'
                if len(value) <= 127:
                    event['decoded'] = decode_packet(value)[0]
        events.append(event)
    return {'connection': connection, 'event_count': len(events),
            'write_value_bytes': write_bytes, 'events': events,
            'note': 'Filtered DUPLO session only. Raw payloads may contain your recorded voice. No upload protocol is inferred from byte counts.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path, help='Apple PacketLogger .pklg file')
    parser.add_argument('--connection', help='Session printed in an ambiguity error, e.g. 0x000b:0')
    parser.add_argument('--output', type=Path, help='New local JSON file; existing files are never overwritten')
    args = parser.parse_args()
    try:
        with args.capture.open('rb') as stream:
            result = extract(stream, args.connection)
        output = json.dumps(result, indent=2) + '\n'
        if args.output:
            with args.output.open('x') as destination:
                destination.write(output)
            print(f"Extracted {result['event_count']} DUPLO ATT events to {args.output}")
        else:
            print(output)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Capture inspection failed: {error}\n')


if __name__ == '__main__':
    main()
