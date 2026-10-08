"""Synthetic protocol tests; these do not verify a physical DJI controller."""

import contextlib
import io
import ipaddress
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

import dji_wifi as dji_remote
import dji_remote as protocol_helpers


# Checksums independently computed with the protocol reference's CRC tables.
NEUTRAL = bytes.fromhex(
    "55cc30751e000000551e048a06024d000006ae"
    "0000010000000400040004000400040004" "9de5")
PRESSED = bytes.fromhex(
    "55cc30751e000000551e048a06024d000006ae"
    "e0010e00004a05b60294066c0100046c01" "ba9f")


class DiscoveryTests(unittest.TestCase):
    def test_discovery_verifies_stick_input_and_never_writes(self):
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.recv.side_effect = [dji_remote.version_query(1), NEUTRAL[:9], NEUTRAL[9:]]
        with patch.object(protocol_helpers.socket, 'create_connection', return_value=connection):
            found = protocol_helpers.scan(ipaddress.ip_network('192.168.1.91/32'), 40007)
        self.assertEqual(found, [{'ip': '192.168.1.91', 'duml_verified': True,
                                 'controls_verified': True}])
        connection.sendall.assert_not_called()
        connection.send.assert_not_called()

    def test_open_port_and_non_control_duml_are_not_stick_input(self):
        corrupted = bytearray(NEUTRAL)
        corrupted[-1] ^= 1
        for payload, verified in ((b'not DJI', False), (bytes(corrupted), False),
                                  (dji_remote.version_query(1), True)):
            with self.subTest(payload=payload):
                connection = Mock()
                connection.__enter__ = Mock(return_value=connection)
                connection.__exit__ = Mock(return_value=False)
                connection.recv.side_effect = [payload, TimeoutError()]
                with patch.object(protocol_helpers.socket, 'create_connection', return_value=connection):
                    found = protocol_helpers.scan(ipaddress.ip_network('192.168.1.91/32'), 40007)
                self.assertEqual(found[0]['duml_verified'], verified)
                self.assertFalse(found[0]['controls_verified'])

    def test_discovery_reports_permissions_and_bounds_the_network(self):
        with patch.object(protocol_helpers.socket, 'create_connection', side_effect=PermissionError(1, 'Denied')):
            with self.assertRaisesRegex(PermissionError, 'Network access denied'):
                protocol_helpers.scan(ipaddress.ip_network('192.168.1.91/32'), 40007)
        for subnet in ('192.168.0.0/16', '8.8.8.0/24'):
            with self.assertRaises(ValueError):
                protocol_helpers.scan(ipaddress.ip_network(subnet), 40007)

    def test_network_detection_ignores_inactive_and_virtual_interfaces(self):
        interfaces = ('en1: flags=8863<UP>\n inet 10.0.0.2 netmask 0xffffff00\n status: inactive\n'
                      'utun0: flags=8863<UP>\n inet 10.1.0.2 netmask 0xffffff00\n status: active\n'
                      'en0: flags=8863<UP>\n inet 192.168.1.54 netmask 0xffffff00\n status: active\n')
        with patch.object(protocol_helpers.subprocess, 'check_output', return_value=interfaces):
            self.assertEqual(protocol_helpers.local_network(), ipaddress.ip_network('192.168.1.0/24'))


class ProtocolTests(unittest.TestCase):
    def test_session_replies_and_no_reply_loops(self):
        ack_request = bytes.fromhex("550d043306020100400001c288")
        ack_reply = bytes.fromhex("550d04330206010080000182fd")
        poll = bytes.fromhex("551004560602010000007761626323f6")
        poll_reply = bytes.fromhex("5510045602060100800077616263f87c")
        for request, expected in ((ack_request, ack_reply), (poll, poll_reply)):
            frame = dji_remote.session_reply(request)
            self.assertEqual(dji_remote.PacketParser().feed(frame), [expected])
            self.assertIsNone(dji_remote.session_reply(expected))
        self.assertIsNone(dji_remote.session_reply(NEUTRAL[8:]))

    def test_version_query_known_bytes(self):
        self.assertEqual(dji_remote.version_query(1).hex(),
                         "55cc30750d000000550d0433020601000000016ef1")

    def test_every_fragment_boundary(self):
        for split in range(1, len(NEUTRAL)):
            parser = dji_remote.PacketParser()
            self.assertEqual(parser.feed(NEUTRAL[:split]), [])
            self.assertEqual(parser.feed(NEUTRAL[split:]), [NEUTRAL[8:]])
        parser = dji_remote.PacketParser()
        packets = []
        for byte in NEUTRAL:
            packets.extend(parser.feed(bytes([byte])))
        self.assertEqual(packets, [NEUTRAL[8:]])

    def test_corruption_noise_and_combined_frames(self):
        bad_checksum = bytearray(NEUTRAL)
        bad_checksum[-1] ^= 1
        invalid_length = dji_remote.MAGIC + b"\xff\xff\xff\xff"
        parser = dji_remote.PacketParser()
        self.assertEqual(parser.feed(b"noise" + invalid_length + bad_checksum + NEUTRAL + PRESSED),
                         [NEUTRAL[8:], PRESSED[8:]])
        parser.feed(b"x" * 100000)
        self.assertLessEqual(len(parser.buffer), 3)

    def test_neutral_and_pressed_controls(self):
        neutral = dji_remote.decode_controls(NEUTRAL[8:])
        pressed = dji_remote.decode_controls(PRESSED[8:])
        self.assertEqual(set(neutral["axes"].values()), {0})
        self.assertFalse(any(neutral["buttons"].values()))
        self.assertEqual(neutral["mode"], "normal")
        self.assertEqual(pressed["axes"], {
            "left_x": 1.0, "left_y": -1.0, "right_x": -0.5,
            "right_y": 0.5, "left_wheel": 0.0, "right_wheel": 1.0})
        self.assertTrue(all(pressed["buttons"].values()))
        self.assertEqual(pressed["mode"], "cinema")

    def test_button_press_release_events(self):
        neutral = dji_remote.decode_controls(NEUTRAL[8:])
        pressed = dji_remote.decode_controls(PRESSED[8:])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            dji_remote.output_state(pressed, neutral, True, False)
            dji_remote.output_state(neutral, pressed, True, False)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(set(events[0]["button_changes"].values()), {"pressed"})
        self.assertEqual(set(events[1]["button_changes"].values()), {"released"})

    def test_listener_over_loopback(self):
        # Exercise real TCP, the CLI, JSON output and a finite listener duration.
        for filename in ("dji_remote.py", "dji_wifi.py"):
            with self.subTest(entrypoint=filename):
                self.check_listener_over_loopback(filename)

    def check_listener_over_loopback(self, filename):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            server.settimeout(5)
            port = server.getsockname()[1]
            errors = []
            received = []

            def serve():
                try:
                    with server.accept()[0] as connection:
                        connection.sendall(NEUTRAL[:9])
                        connection.sendall(NEUTRAL[9:] + PRESSED + NEUTRAL)
                        connection.settimeout(5)
                        received.append(connection.recv(1))  # Wait until stopped.
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            result = subprocess.run([
                sys.executable, str(Path(__file__).with_name(filename)), "listen",
                "--ip", "127.0.0.1", "--port", str(port),
                "--connections", "1", "--passive", "--seconds", "0.6", "--json"],
                text=True, capture_output=True, timeout=5)
            thread.join(timeout=5)
            self.assertFalse(errors)
            self.assertFalse(thread.is_alive())
            self.assertEqual(received, [b""], "Passive listeners must not send queries.")
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(len(events), 3)
            self.assertEqual(events[1]["button_changes"]["c1"], "pressed")
            self.assertEqual(events[2]["button_changes"]["c1"], "released")

    def test_overlapping_readers_and_button_hold_across_reconnect(self):
        # All links observe one physical button hold. Closing one link must
        # neither interrupt the other readers nor create another press event.
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            server.settimeout(0.05)
            port = server.getsockname()[1]
            stop = threading.Event()
            threads, accepted, errors = [], [], []
            started = []

            def stream(connection, index):
                try:
                    with connection:
                        while not stop.is_set():
                            elapsed = time.monotonic() - started[0]
                            if index == 0 and elapsed >= 0.25:
                                return
                            connection.sendall(PRESSED if elapsed < 0.8 else NEUTRAL)
                            stop.wait(0.05)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Normal listener shutdown.
                except Exception as exc:
                    errors.append(exc)

            def serve():
                while not stop.is_set():
                    try:
                        connection, _ = server.accept()
                    except TimeoutError:
                        continue
                    if not started:
                        started.append(time.monotonic())
                    index = len(accepted)
                    accepted.append(index)
                    child = threading.Thread(target=stream, args=(connection, index))
                    threads.append(child)
                    child.start()

            acceptor = threading.Thread(target=serve)
            acceptor.start()
            try:
                result = subprocess.run([
                    sys.executable, str(Path(dji_remote.__file__)), "listen",
                    "--ip", "127.0.0.1", "--port", str(port),
                    "--connections", "2", "--seconds", "1.25", "--json"],
                    text=True, capture_output=True, timeout=5)
            finally:
                stop.set()
                acceptor.join(timeout=2)
                for child in threads:
                    child.join(timeout=2)
            self.assertFalse(acceptor.is_alive())
            self.assertTrue(all(not child.is_alive() for child in threads))
            self.assertFalse(errors)
            self.assertGreaterEqual(len(accepted), 3)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            transitions = [event["button_changes"]["c1"] for event in events
                           if "c1" in event["button_changes"]]
            self.assertEqual(transitions, ["pressed", "released"])
            self.assertGreater(len(events), 15)
            self.assertNotIn("input is stale", result.stderr)


if __name__ == "__main__":
    unittest.main()
