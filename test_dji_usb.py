"""Native USB protocol tests and a simulated request/response USB endpoint."""

import argparse
import contextlib
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import dji_usb


# Neutral replies captured from the physical RM330. The moved fixture's CRC
# was computed independently with the USB reference's CRC lookup tables.
NEUTRAL_1 = bytes.fromhex("5520047b060a010080060100000401000402000403000404000405000406da65")
NEUTRAL_3 = bytes.fromhex("5520047b060a03008006010000040100040200040300040400040500040652d3")
MOVED_2 = bytes.fromhex("5520047b060a0200800601004a0501b602029406036c01040004056c01064037")


class USBProtocolTests(unittest.TestCase):
    def test_read_only_query_known_bytes(self):
        self.assertEqual(dji_usb.read_sticks_query(1).hex(), "550d04330a060100400601247d")

    def test_fragmented_and_combined_native_packets(self):
        for split in range(1, len(NEUTRAL_1)):
            parser = dji_usb.USBPacketParser()
            self.assertEqual(parser.feed(NEUTRAL_1[:split]), [])
            self.assertEqual(parser.feed(NEUTRAL_1[split:] + MOVED_2), [NEUTRAL_1, MOVED_2])

    def test_corruption_resynchronization_and_noise_bound(self):
        damaged = bytearray(MOVED_2)
        damaged[-1] ^= 1
        parser = dji_usb.USBPacketParser()
        self.assertEqual(parser.feed(b"junk\x55\x0d\x04\x00" + damaged + NEUTRAL_1), [NEUTRAL_1])
        self.assertEqual(parser.feed(b"noise" * 10000), [])
        self.assertLess(len(parser.buffer), 4)

    def test_controls_and_unsupported_layout(self):
        neutral = dji_usb.decode_controls(NEUTRAL_1)
        moved = dji_usb.decode_controls(MOVED_2)
        self.assertEqual(set(neutral["axes"].values()), {0})
        self.assertEqual(neutral["buttons"], {})
        self.assertEqual(moved["axes"], {"left_x": 1.0, "left_y": -1.0,
                                         "right_x": -0.5, "right_y": 0.5,
                                         "left_wheel": 0.0, "right_wheel": 1.0})
        self.assertEqual(moved["buttons"], {})
        self.assertEqual(moved["mode"], "unknown")
        for offset in (4, 5, 8, 9, 10, 14):
            wrong = bytearray(NEUTRAL_1)
            wrong[offset] = 0
            self.assertIsNone(dji_usb.decode_controls(wrong), offset)
        self.assertIsNone(dji_usb.decode_controls(NEUTRAL_1[:-1]))
        failed = bytearray(NEUTRAL_1)
        failed[11] = 1  # Error status is not a button mask or valid axis sample.
        self.assertIsNone(dji_usb.decode_controls(failed))

    def test_poll_matching_live_axes_and_interface_cleanup(self):
        class USBError(Exception):
            pass

        class USBTimeoutError(USBError):
            pass

        writes = []
        outgoing = SimpleNamespace(write=lambda packet, **_: writes.append(packet))

        def read(*_, **kwargs):
            sequence = int.from_bytes(writes[-1][6:8], "little")
            if sequence == 1:
                # An old reply must not create a false movement sample.
                return MOVED_2 + NEUTRAL_1
            if sequence == 2:
                return MOVED_2
            if sequence == 3:
                return NEUTRAL_3
            raise USBTimeoutError()

        incoming = SimpleNamespace(read=read)
        util = SimpleNamespace(release_interface=Mock(), dispose_resources=Mock())
        usb = SimpleNamespace(core=SimpleNamespace(USBError=USBError, USBTimeoutError=USBTimeoutError), util=util)
        device = object()
        args = argparse.Namespace(seconds=0.12, hz=100, json=True, raw=False)
        output = io.StringIO()
        with patch.object(dji_usb, "open_remote", return_value=(device, 1, incoming, outgoing)):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                result = dji_usb.listen(args, usb, None)
        self.assertEqual(result, 0)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["button_changes"], {})
        self.assertEqual(events[0]["axes"]["left_x"], 0)
        self.assertEqual(events[1]["axes"]["left_x"], 1)
        self.assertEqual(events[2]["axes"]["left_x"], 0)
        self.assertTrue(all(event["buttons"] == {} for event in events))
        self.assertTrue(all(packet[9:11] == b"\x06\x01" for packet in writes))
        util.release_interface.assert_called_once_with(device, 1)
        util.dispose_resources.assert_called_once_with(device)


if __name__ == "__main__":
    unittest.main()
