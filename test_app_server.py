"""Exercise the real web bridge with simulated hardware and failure injection."""
import asyncio
from contextlib import suppress
import time
import struct
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from app_server import CONTROLLER_KEY, Controller, create_app
from test_dji_remote import NEUTRAL, PRESSED
import dji_remote


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.controller = Controller(demo=True)
        self.owner = object()

    async def asyncTearDown(self):
        await self.controller.close()

    async def test_stop_invalidates_queued_drive(self):
        c = self.controller
        await c.write_lock.acquire()
        drive = asyncio.create_task(c.drive(35, self.owner))
        await asyncio.sleep(0)
        stop = asyncio.create_task(c.stop())
        await asyncio.sleep(0)
        c.write_lock.release()
        await asyncio.gather(drive, stop)
        self.assertEqual(c.train['power'], 0)
        self.assertFalse(any('Forward' in event['message'] for event in c.logs))

    async def test_heartbeat_and_remote_loss_stop_motion(self):
        c = self.controller
        for source in ('manual', 'dji'):
            c.input['source'] = source
            await c.drive(35, self.owner, source)
            c.last_heartbeat = time.monotonic() - 3 if source == 'manual' else time.monotonic()
            c.last_remote_sample = time.monotonic() - 1
            task = asyncio.create_task(c.watchdog())
            await asyncio.sleep(0.2)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self.assertEqual(c.train['power'], 0)
            self.assertIsNone(c.owner)
            self.assertEqual(c.input['source'], 'manual')

    async def test_switching_source_requires_verified_remote_and_neutral(self):
        c = self.controller
        with self.assertRaises(ValueError):
            await c.action('source', {'source': 'dji'}, self.owner)
        c.remote['status'] = 'connected'
        await c.action('source', {'source': 'dji'}, self.owner)
        self.assertTrue(c.input['neutral_required'])
        self.assertIs(c.owner, self.owner)
        await c.action('disconnect', {}, self.owner)
        self.assertEqual(c.input['source'], 'manual')
        self.assertIsNone(c.owner)

    async def test_bad_settings_are_atomic(self):
        c = self.controller
        before = dict(c.input)
        for args in ({'max_power': float('nan')}, {'deadzone': 1}, {'axis': 'nope'}, {'invert': 1}):
            with self.assertRaises(ValueError):
                await c.action('configure', args, self.owner)
            self.assertEqual(c.input, before)

    async def test_sound_catalog_is_generation_specific(self):
        c = self.controller
        self.assertEqual(c.snapshot()['sounds'], ['horn', 'starting'])
        await c.action('sound', {'sound': 'starting'}, self.owner)
        self.assertEqual(c.logs[-1]['raw'], '09 00 81 34 11 51 01 06 01')
        with self.assertRaises(ValueError):
            await c.action('sound', {'sound': 'brake'}, self.owner)
        c.train['hub_type'] = 0x20
        self.assertEqual(c.snapshot()['sounds'], ['horn', 'brake', 'depart', 'water', 'steam'])
        with self.assertRaises(ValueError):
            await c.action('sound', {'sound': 'starting'}, self.owner)

    async def test_mode_format_notifications_are_collected_per_port_and_mode(self):
        c = self.controller
        c.notification(None, bytes.fromhex('0a 00 44 34 01 80 04 01 05 00'))
        c.notification(None, bytes.fromhex('0a 00 44 34 02 80 01 00 03 00'))
        self.assertEqual(c.snapshot()['train']['mode_formats']['52']['1']['datasets'], 4)
        self.assertEqual(c.snapshot()['train']['mode_formats']['52']['2']['type'], 0)
        self.assertNotIn('mode_format', c.train)

    async def test_power_off_invalidates_queued_motion_and_stops_first(self):
        c = self.controller
        await c.write_lock.acquire()
        drive = asyncio.create_task(c.drive(35, self.owner))
        await asyncio.sleep(0)
        shutdown = asyncio.create_task(c.power_off())
        await asyncio.sleep(0)
        c.write_lock.release()
        await asyncio.gather(drive, shutdown)
        self.assertEqual(c.train['status'], 'disconnected')
        self.assertEqual(c.train['power'], 0)
        self.assertIsNone(c.owner)
        packets = [event['raw'] for event in c.logs if event['direction'] == 'TX']
        self.assertEqual(packets[-2:], ['08 00 81 32 11 51 00 00', '04 00 02 01'])
        self.assertFalse(any('Forward' in event['message'] for event in c.logs))

    async def test_idle_timer_ignores_heartbeat_and_shutdown_when_idle(self):
        c = self.controller
        await c.action('train_settings', {'idle_minutes': 1}, self.owner)
        c.last_activity = time.monotonic() - 61
        c.last_heartbeat = time.monotonic()
        task = asyncio.create_task(c.watchdog())
        await asyncio.sleep(.2)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self.assertEqual(c.train['status'], 'disconnected')
        self.assertIn('idle', c.train['shutdown_reason'])

    async def test_idle_timer_does_not_turn_off_moving_train_and_brick_resets(self):
        c = self.controller
        await c.action('train_settings', {'idle_minutes': 1}, self.owner)
        await c.drive(35, self.owner)
        c.last_activity = time.monotonic() - 61
        self.assertIsNone(c.idle_remaining())
        await c.stop()
        self.assertGreater(c.idle_remaining(), 58)
        c.notification(None, bytes.fromhex('06 00 45 33 44 01'))
        c.notification(None, bytes.fromhex('06 00 45 33 44 01'))
        self.assertEqual(c.train['purple_events'], 2)
        self.assertLess(time.monotonic() - c.last_activity, 1)
        await c.stop()
        self.assertGreater(c.idle_remaining(), 58)

    async def test_idle_settings_persist_without_accepting_invalid_values(self):
        c = self.controller
        for value in (True, 2, '5', -1, None):
            with self.assertRaises(ValueError):
                await c.action('train_settings', {'idle_minutes': value}, self.owner)
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            c.settings_path = path
            await c.action('train_settings', {'idle_minutes': 5}, self.owner)
            restored = Controller(demo=True, settings_path=path)
            self.assertEqual(restored.idle_minutes, 5)
            await restored.close()
        c.settings_path = None

    async def test_power_off_failure_is_not_reported_as_success(self):
        c = self.controller
        c.demo = False
        c.client = AsyncMock()
        c.client.is_connected = True
        c.client.write_gatt_char.side_effect = ConnectionError('write failed')
        c.characteristic = type('Characteristic', (), {'properties': ['write']})()
        with self.assertRaises(ConnectionError):
            await c.power_off()
        self.assertIsNone(c.train['shutdown_reason'])
        self.assertEqual(c.train['status'], 'disconnected')

    async def test_simulated_bricks_never_allowed_on_hardware(self):
        c = self.controller
        await c.action('demo_purple', {}, self.owner)
        self.assertEqual(c.train['purple_events'], 1)
        c.demo = False
        with self.assertRaises(ValueError):
            await c.action('demo_purple', {}, self.owner)
        c.demo = True

    async def test_cancelled_bluetooth_connect_cleans_up(self):
        c = self.controller
        c.demo = False
        c.train['status'] = 'disconnected'
        with patch('app_server.BleakScanner.discover', new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await c.connect_train()
        self.assertEqual(c.train['status'], 'disconnected')
        self.assertIsNone(c.client)

    async def test_remote_frames_neutral_then_stop_button(self):
        # The TCP parser is separately tested over loopback in test_dji_remote.
        c = self.controller
        reader = asyncio.StreamReader()
        writer = AsyncMock()
        writer.write = lambda _data: None
        writer.close = lambda: None
        with patch('app_server.asyncio.open_connection', new=AsyncMock(return_value=(reader, writer))):
            task = asyncio.create_task(c.remote_stream('127.0.0.1'))
            reader.feed_data(NEUTRAL)
            for _ in range(20):
                if c.remote['status'] == 'connected':
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(c.remote['status'], 'connected')
            await c.action('source', {'source': 'dji'}, self.owner)
            reader.feed_data(NEUTRAL)
            await asyncio.sleep(0.02)
            self.assertFalse(c.input['neutral_required'])
            # Left vertical stick at its positive endpoint, no buttons pressed.
            packet = bytearray(NEUTRAL[8:])
            offset = 11 + 2 * dji_remote.AXES['left_y'] + 1
            packet[offset:offset + 2] = struct.pack('<H', 1024 - 660)
            packet[-2:] = struct.pack('<H', dji_remote.crc(packet[:-2], 0x3692, 0x8408))
            displaced = dji_remote.MAGIC + struct.pack('<I', len(packet)) + packet
            reader.feed_data(displaced)
            await asyncio.sleep(0.02)
            self.assertEqual(c.train['power'], 35)
            # PRESSED includes the mapped stop button.
            reader.feed_data(PRESSED)
            await asyncio.sleep(0.02)
            self.assertEqual(c.train['power'], 0)
            self.assertEqual(c.input['source'], 'manual')
            reader.feed_eof()
            await task
            self.assertEqual(c.remote['status'], 'error')


class WebBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app(demo=True)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.controller = self.app[CONTROLLER_KEY]

    async def asyncTearDown(self):
        await self.client.close()

    async def command(self, ws, action, args=None, request_id=1):
        await ws.send_json({'id': request_id, 'action': action, 'args': args or {}})
        while True:
            message = await ws.receive_json(timeout=2)
            if message['type'] == 'result' and message.get('id') == request_id:
                return message

    async def test_browser_close_stops_and_clears_owner(self):
        ws = await self.client.ws_connect('/api/ws')
        self.assertEqual((await ws.receive_json())['type'], 'snapshot')
        self.assertTrue((await self.command(ws, 'drive', {'power': 35}))['ok'])
        self.assertEqual(self.controller.train['power'], 35)
        await ws.close()
        for _ in range(20):
            if not self.controller.train['power']:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.controller.train['power'], 0)
        self.assertIsNone(self.controller.owner)

    async def test_second_window_cannot_take_motion_but_can_stop(self):
        first = await self.client.ws_connect('/api/ws')
        second = await self.client.ws_connect('/api/ws')
        await first.receive_json(); await second.receive_json()
        await self.command(first, 'drive', {'power': 35})
        result = await self.command(second, 'drive', {'power': -35})
        self.assertFalse(result['ok'])
        self.assertEqual(self.controller.train['power'], 35)
        self.assertTrue((await self.command(second, 'stop'))['ok'])
        self.assertEqual(self.controller.train['power'], 0)
        await first.close(); await second.close()

    async def test_rejects_cross_origin_and_exports_packets(self):
        result = await self.client.get('/api/state', headers={'Origin': 'https://example.com'})
        self.assertEqual(result.status, 403)
        result = await self.client.get('/api/logs')
        self.assertEqual(result.status, 200)
        self.assertIn('05 00 01 06 05', await result.text())
        result = await self.client.get('/')
        self.assertEqual(result.status, 200)
        self.assertIn('DUPLO Control', await result.text())


if __name__ == '__main__':
    unittest.main()
