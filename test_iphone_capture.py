"""Validate capture framing, fragmentation, and exclusion of other devices."""
import io
import struct
import unittest

from inspect_iphone_capture import att_packets, extract, records


def record(packet_type, payload, seconds=10, order='>'):
    return struct.pack(order + 'III', 9 + len(payload), seconds, 1000) + bytes([packet_type]) + payload


def acl(handle, att, packet_type=3, seconds=10):
    l2cap = struct.pack('<HH', len(att), 4) + att
    return record(packet_type, struct.pack('<HH', 0x2000 | handle, len(l2cap)) + l2cap, seconds)


def name(handle):
    return acl(handle, bytes.fromhex('1b 25 00 10 00 01 01 06') + b'DUPLO Train')


class IphoneCaptureTests(unittest.TestCase):
    def test_filters_other_connections_and_preserves_commands_and_replies(self):
        capture = acl(9, b'\x1b\x25\x00PRIVATE') + name(11)
        capture += acl(11, bytes.fromhex('52 25 00 09 00 81 34 11 51 01 07 01'), 2, 11)
        capture += acl(11, bytes.fromhex('1b 25 00 05 00 82 34 0a'), 3, 12)
        result = extract(io.BytesIO(capture))
        self.assertEqual(result['event_count'], 3)
        self.assertEqual(result['write_value_bytes'], 9)
        self.assertEqual(result['events'][1]['direction'], 'TX')
        self.assertEqual(result['events'][2]['decoded'], 'Command feedback: port 52 status 0x0a')
        self.assertNotIn('PRIVATE'.encode().hex(' '), str(result))

    def test_fragmented_extended_length_lego_message(self):
        value = bytes([0x82, 1, 0, 0x99]) + bytes(range(126))
        att = b'\x52\x25\x00' + value
        l2cap = struct.pack('<HH', len(att), 4) + att
        first = record(2, struct.pack('<HH', 0x200b, 30) + l2cap[:30], 11)
        remaining = l2cap[30:]
        second = record(2, struct.pack('<HH', 0x100b, len(remaining)) + remaining, 12)
        result = extract(io.BytesIO(name(11) + first + second))
        self.assertEqual(result['events'][1]['value_raw'], value.hex(' '))
        self.assertEqual(result['events'][1]['lego_message_type'], '0x99')
        self.assertEqual(result['events'][1]['elapsed_seconds'], 1)

    def test_reused_hci_handle_does_not_leak_next_devices_data(self):
        disconnected = record(1, bytes.fromhex('05 04 00 0b 00 13'), 11)
        result = extract(io.BytesIO(name(11) + disconnected + acl(11, b'\x1b\x25\x00PRIVATE', seconds=12)))
        self.assertEqual(result['event_count'], 1)

    def test_both_endian_variants_and_truncated_input(self):
        for order in ('>', '<'):
            self.assertEqual(list(records(io.BytesIO(record(0xfc, b'note', order=order))))[0][2], b'note')
        for data in (b'\x00', record(2, b'hello')[:-1], struct.pack('>III', 9, 10, 1000000) + b'\x02'):
            with self.assertRaises(ValueError):
                list(records(io.BytesIO(data)))

    def test_requires_duplo_evidence_and_handles_ambiguity(self):
        with self.assertRaises(ValueError):
            extract(io.BytesIO(acl(9, b'\x1b\x25\x00PRIVATE')), '0x0009:0')
        with self.assertRaises(ValueError):
            extract(io.BytesIO(name(11) + name(12)))
        self.assertEqual(extract(io.BytesIO(name(11) + name(12)), '0x000c:0')['event_count'], 1)

    def test_incomplete_or_oversized_fragments_fail_clearly(self):
        for data in (b'\x06\x00\x04\x00\x52', b'\x01\x00\x04\x00\x52\x00'):
            fragment = record(2, struct.pack('<HH', 0x200b, len(data)) + data)
            with self.assertRaises(ValueError):
                list(att_packets(io.BytesIO(fragment)))


if __name__ == '__main__':
    unittest.main()
