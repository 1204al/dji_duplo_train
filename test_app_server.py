"""Exercise the real web bridge with simulated hardware and failure injection."""
import asyncio
import ipaddress
from contextlib import suppress
import time
import struct
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer
from app_server import CONTROLLER_KEY, Controller, create_app
from test_dji_remote import NEUTRAL
import dji_remote


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.controller = Controller(demo=True)
        # Transport tests use positive raw values for forward; the physical
        # RM330 default direction is exercised separately below.
        self.controller.input['invert'] = False
        self.owner = object()

    async def asyncTearDown(self):
        await self.controller.close()

    def controls(self, left_y=0, **axes):
        controls = dji_remote.decode_controls(NEUTRAL[8:])
        controls['axes'].update(left_y=left_y, **axes)
        return controls

    def frame(self, left_y):
        packet = bytearray(NEUTRAL[8:])
        offset = 11 + 2 * dji_remote.AXES['left_y'] + 1
        packet[offset:offset + 2] = struct.pack('<H', 1024 - round(left_y * 660))
        packet[-2:] = struct.pack('<H', dji_remote.crc(packet[:-2], 0x3692, 0x8408))
        return dji_remote.MAGIC + struct.pack('<I', len(packet)) + packet

    async def sample(self, left_y=0, **axes):
        c = self.controller
        self.assertTrue(c.publish_remote_sample(self.controls(left_y, **axes), time.monotonic()))
        await c.apply_remote_sample(*c.remote_sample)

    async def enable_dji(self):
        await self.sample()
        await self.controller.action('source', {'source': 'dji'}, self.owner)
        await self.sample()

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
            if source == 'dji':
                await self.enable_dji()
            await c.drive(35, self.owner, source)
            c.last_heartbeat = time.monotonic() - 3 if source == 'manual' else time.monotonic()
            c.last_remote_sample = time.monotonic() - 1
            task = asyncio.create_task(c.watchdog())
            await asyncio.sleep(0.2)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self.assertEqual(c.train['power'], 0)
            if source == 'manual':
                self.assertIsNone(c.owner)
                self.assertEqual(c.input['source'], 'manual')
            else:
                self.assertIs(c.owner, self.owner)
                self.assertEqual(c.input['source'], 'dji')
                self.assertTrue(c.input['neutral_required'])

    async def test_switching_source_requires_verified_remote_and_neutral(self):
        c = self.controller
        with self.assertRaises(ValueError):
            await c.action('source', {'source': 'dji'}, self.owner)
        c.remote['status'] = 'connected'
        with self.assertRaises(ValueError):
            await c.action('source', {'source': 'dji'}, self.owner)
        await self.sample()
        await c.action('source', {'source': 'dji'}, self.owner)
        self.assertTrue(c.input['neutral_required'])
        self.assertIs(c.owner, self.owner)
        await c.action('disconnect', {}, self.owner)
        self.assertEqual(c.input['source'], 'manual')
        self.assertIsNone(c.owner)

    async def test_bad_settings_are_atomic(self):
        c = self.controller
        before = dict(c.input)
        for args in ({'max_power': float('nan')}, {'deadzone': 1}, {'axis': 'nope'},
                     {'axis': 'right_y'}, {'stop_button': 'rth_pause'}, {'horn_button': 'c1'}, {'invert': 1}):
            with self.assertRaises(ValueError):
                await c.action('configure', args, self.owner)
            self.assertEqual(c.input, before)

    async def test_left_stick_travel_sets_proportional_power_in_both_directions(self):
        c = self.controller
        await c.action('configure', {'max_power': 100}, self.owner)
        await self.enable_dji()
        for position, power in ((.04, 0), (.31, 25), (.54, 50), (.77, 75),
                                (1, 100), (0, 0), (-.54, -50), (-1, -100), (0, 0)):
            await self.sample(position, left_x=1, right_y=-1)
            self.assertEqual(c.train['power'], power)
            self.assertEqual(c.snapshot()['left_stick']['power'], power)

    async def test_left_stick_readout_uses_calibration_inversion_and_freshness(self):
        c = self.controller
        self.assertEqual(c.snapshot()['left_stick'],
                         {'position': None, 'power': None, 'available': False})
        await self.sample(.1)
        await c.action('calibrate', {}, self.owner)
        await c.action('configure', {'max_power': 50, 'invert': True}, self.owner)
        await self.sample(.64, left_x=-1, right_y=1)
        self.assertEqual(c.snapshot()['left_stick'],
                         {'position': -.54, 'power': -25, 'available': True})
        c.last_remote_sample = time.monotonic() - 1
        self.assertFalse(c.snapshot()['left_stick']['available'])

    async def test_default_left_stick_is_enabled_with_corrected_rm330_direction(self):
        c = Controller(demo=True)
        try:
            self.assertTrue(c.input['left_stick_enabled'])
            self.assertTrue(c.input['invert'])
            c.publish_remote_sample(self.controls(-1), time.monotonic())
            c.activate_default_input(self.owner)
            self.assertEqual(c.input['source'], 'dji')
            self.assertTrue(c.input['neutral_required'])
            for raw_position, power in ((-1, 0), (0, 0), (-.54, 18),
                                        (-1, 35), (1, -35), (0, 0)):
                c.publish_remote_sample(self.controls(raw_position), time.monotonic())
                await c.apply_remote_sample(*c.remote_sample)
                self.assertEqual(c.train['power'], power)
        finally:
            await c.close()

    async def test_default_activation_waits_for_train_and_fresh_remote_input(self):
        c = self.controller
        c.activate_default_input(self.owner)
        self.assertIsNone(c.owner)
        await self.sample()
        c.train['status'] = 'disconnected'
        c.activate_default_input(self.owner)
        self.assertIsNone(c.owner)
        c.train['status'] = 'connected'
        c.last_remote_sample = time.monotonic() - 1
        c.activate_default_input(self.owner)
        self.assertIsNone(c.owner)
        await self.sample()
        c.activate_default_input(self.owner)
        self.assertIs(c.owner, self.owner)
        self.assertTrue(c.input['neutral_required'])

    async def test_default_activation_preserves_computer_motion_until_center_handoff(self):
        c = self.controller
        await c.action('drive', {'power': 25}, self.owner)
        await self.sample()
        c.activate_default_input(self.owner)
        self.assertEqual(c.input['source'], 'dji')
        self.assertTrue(c.input['manual_override'])
        self.assertEqual(c.train['power'], 25)
        await self.sample(1)
        self.assertEqual(c.train['power'], 25)
        await self.sample()
        self.assertEqual(c.train['power'], 25)
        self.assertFalse(c.input['manual_override'])
        c.activate_default_input(self.owner)
        self.assertFalse(c.input['manual_override'])
        self.assertFalse(c.input['neutral_required'])
        await self.sample(1)
        self.assertEqual(c.train['power'], 35)

    async def test_stick_preferences_persist_alongside_idle_settings(self):
        controllers = []
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            path.write_text('{"idle_minutes": 10}')
            try:
                c = Controller(demo=True, settings_path=path)
                controllers.append(c)
                self.assertTrue(c.input['left_stick_enabled'])
                self.assertTrue(c.input['invert'])
                self.assertEqual(c.idle_minutes, 10)
                await c.action('source', {'source': 'manual'}, self.owner)
                await c.action('configure', {'invert': False}, self.owner)
                await c.action('train_settings', {'idle_minutes': 5}, self.owner)
                restored = Controller(demo=True, settings_path=path)
                controllers.append(restored)
                self.assertFalse(restored.input['left_stick_enabled'])
                self.assertFalse(restored.input['invert'])
                self.assertEqual(restored.idle_minutes, 5)
                restored.publish_remote_sample(self.controls(), time.monotonic())
                restored.activate_default_input(self.owner)
                self.assertIsNone(restored.owner)
                await restored.action('source', {'source': 'dji'}, self.owner)
                restarted = Controller(demo=True, settings_path=path)
                controllers.append(restarted)
                self.assertTrue(restarted.input['left_stick_enabled'])
                self.assertFalse(restarted.input['invert'])
                self.assertEqual(restarted.idle_minutes, 5)
            finally:
                for controller in controllers:
                    await controller.close()

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

    async def test_computer_takeover_and_center_handoff(self):
        c = self.controller
        await self.enable_dji()
        await self.sample(1)
        self.assertEqual(c.train['power'], 35)
        self.assertEqual(c.input['driver'], 'dji')
        await c.action('drive', {'power': -25}, self.owner)
        self.assertTrue(c.input['manual_override'])
        self.assertEqual(c.input['source'], 'dji')
        self.assertEqual(c.input['driver'], 'manual')
        await self.sample(1)
        self.assertEqual(c.train['power'], -25)
        await self.sample()
        await self.sample()
        self.assertEqual(c.train['power'], -25)
        self.assertFalse(c.input['manual_override'])
        self.assertFalse(c.input['neutral_required'])
        await self.sample(-1)
        self.assertEqual(c.train['power'], -35)
        self.assertEqual(c.input['driver'], 'dji')
        await self.sample()
        self.assertEqual(c.train['power'], 0)

    async def test_computer_stop_keeps_dji_but_requires_new_center(self):
        c = self.controller
        await self.enable_dji()
        await self.sample(1)
        await c.action('stop', {}, self.owner)
        self.assertEqual(c.input['source'], 'dji')
        self.assertIs(c.owner, self.owner)
        self.assertTrue(c.input['neutral_required'])
        self.assertFalse(c.input['manual_override'])
        self.assertIsNone(c.input['driver'])
        await self.sample(1)
        self.assertEqual(c.train['power'], 0)
        await self.sample()
        await self.sample(-1)
        self.assertEqual(c.train['power'], -35)

    async def test_window_exit_stop_disarms_dji_until_explicit_enable(self):
        c = self.controller
        await self.enable_dji()
        await self.sample(1)
        await c.action('stop', {'disarm': True}, self.owner)
        await self.sample()
        await self.sample(-1)
        self.assertEqual(c.input['source'], 'manual')
        self.assertEqual(c.train['power'], 0)
        self.assertIsNone(c.owner)

    async def test_background_window_cannot_suspend_active_stick_and_focus_requires_center(self):
        c = self.controller
        await self.enable_dji()
        await self.sample(1)
        await c.action('suspend', {}, object())
        self.assertEqual(c.train['power'], 35)
        self.assertIs(c.owner, self.owner)
        await c.action('suspend', {}, self.owner)
        self.assertEqual(c.train['power'], 0)
        self.assertIsNone(c.owner)
        self.assertTrue(c.input['left_stick_enabled'])
        await self.sample(1)
        self.assertEqual(c.train['power'], 0)
        c.activate_default_input(self.owner)
        await self.sample(1)
        self.assertEqual(c.train['power'], 0)
        await self.sample()
        await self.sample(1)
        self.assertEqual(c.train['power'], 35)

    async def test_buffered_center_cannot_rearm_after_stop_or_takeover(self):
        c = self.controller
        await self.enable_dji()
        c.publish_remote_sample(self.controls(), time.monotonic())
        buffered = c.remote_sample
        await c.action('stop', {}, self.owner)
        await c.apply_remote_sample(*buffered)
        self.assertTrue(c.input['neutral_required'])
        await self.sample(1)
        self.assertEqual(c.train['power'], 0)
        c.publish_remote_sample(self.controls(), time.monotonic())
        buffered = c.remote_sample
        await c.action('drive', {'power': 20}, self.owner)
        await c.apply_remote_sample(*buffered)
        self.assertTrue(c.input['manual_override'])
        self.assertTrue(c.input['neutral_required'])
        self.assertEqual(c.train['power'], 20)

    async def test_other_stick_axes_and_all_buttons_have_no_train_actions(self):
        c = self.controller
        await self.enable_dji()
        await self.sample(1)
        before = [event for event in c.logs if event['source'] == 'train' and event['direction'] == 'TX']
        controls = self.controls(1, left_x=-1, right_y=-1, right_x=1, left_dial=1, right_dial=-1)
        controls['buttons'] = {button: True for button in dji_remote.BUTTONS}
        c.publish_remote_sample(controls, time.monotonic())
        await c.apply_remote_sample(*c.remote_sample)
        after = [event for event in c.logs if event['source'] == 'train' and event['direction'] == 'TX']
        self.assertEqual(after, before)
        self.assertEqual(c.train['power'], 35)
        self.assertEqual(c.train['light'], 'off')
        self.assertEqual(c.input['source'], 'dji')
        self.assertNotIn('stop_button', c.input)
        self.assertNotIn('horn_button', c.input)

    async def test_controller_settings_keep_enabled_mode_and_require_center(self):
        c = self.controller
        await self.enable_dji()
        await self.sample(1)
        await c.action('configure', {'axis': 'left_y', 'max_power': 20, 'invert': True}, self.owner)
        self.assertEqual(c.input['source'], 'dji')
        self.assertIs(c.owner, self.owner)
        self.assertTrue(c.input['neutral_required'])
        await self.sample()
        await self.sample(1)
        self.assertEqual(c.train['power'], -20)
        await self.sample()
        await c.action('calibrate', {}, self.owner)
        self.assertEqual(c.input['source'], 'dji')
        self.assertIs(c.owner, self.owner)
        self.assertTrue(c.input['neutral_required'])

    async def test_wifi_loss_preserves_computer_motion_and_browser_watchdog(self):
        c = self.controller
        await self.enable_dji()
        await c.action('drive', {'power': 25}, self.owner)
        c.last_remote_sample = time.monotonic() - 1
        task = asyncio.create_task(c.watchdog())
        try:
            await asyncio.sleep(.2)
            self.assertEqual(c.input['source'], 'dji')
            self.assertTrue(c.input['neutral_required'])
            self.assertEqual(c.train['power'], 25)
            self.assertEqual(c.input['driver'], 'manual')
            self.assertIs(c.owner, self.owner)
            c.last_heartbeat = time.monotonic() - 3
            await asyncio.sleep(.2)
            self.assertEqual(c.train['power'], 0)
            self.assertIsNone(c.owner)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_wifi_gap_recovers_only_after_a_new_center_sample(self):
        c = self.controller
        await self.enable_dji()
        await self.sample(1)
        c.publish_remote_sample(self.controls(), time.monotonic())
        buffered_center = c.remote_sample
        c.last_remote_sample = time.monotonic() - 1
        heartbeat = c.last_heartbeat
        await c.pause_remote_input()
        self.assertEqual(c.train['power'], 0)
        self.assertEqual(c.input['source'], 'dji')
        self.assertIs(c.owner, self.owner)
        self.assertEqual(c.last_heartbeat, heartbeat)
        stopped_logs = len(c.logs)
        await c.pause_remote_input()
        self.assertEqual(len(c.logs), stopped_logs)
        await c.apply_remote_sample(*buffered_center)
        self.assertTrue(c.input['neutral_required'])
        await self.sample(-1)
        self.assertEqual(c.train['power'], 0)
        await self.sample()
        await self.sample(-.54)
        self.assertEqual(c.train['power'], -18)
        await self.sample()
        self.assertEqual(c.train['power'], 0)

    async def test_stop_during_a_wifi_gap_keeps_mode_but_requires_center(self):
        c = self.controller
        await self.enable_dji()
        await self.sample(1)
        c.last_remote_sample = time.monotonic() - 1
        c.remote['status'] = 'waiting'
        await c.pause_remote_input()
        await c.action('stop', {}, self.owner)
        self.assertEqual(c.input['source'], 'dji')
        self.assertIs(c.owner, self.owner)
        await self.sample(1)
        self.assertEqual(c.train['power'], 0)
        await self.sample()
        await self.sample(1)
        self.assertEqual(c.train['power'], 35)

    async def test_wifi_gap_cannot_rearm_a_closed_browser(self):
        c = self.controller
        owner = Mock(closed=False)
        await self.sample()
        await c.action('source', {'source': 'dji'}, owner)
        await self.sample()
        await self.sample(1)
        owner.closed = True
        c.last_remote_sample = time.monotonic() - 1
        await c.pause_remote_input()
        self.assertEqual(c.input['source'], 'manual')
        self.assertIsNone(c.owner)
        await self.sample()
        await self.sample(1)
        self.assertEqual(c.train['power'], 0)

    async def test_dji_samples_never_refresh_browser_heartbeat(self):
        c = self.controller
        await self.enable_dji()
        c.last_heartbeat = time.monotonic() - 3
        heartbeat = c.last_heartbeat
        await self.sample(1)
        await self.sample(-1)
        self.assertEqual(c.last_heartbeat, heartbeat)
        task = asyncio.create_task(c.watchdog())
        try:
            await asyncio.sleep(.2)
            self.assertEqual(c.train['power'], 0)
            self.assertEqual(c.input['source'], 'manual')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_second_window_stop_keeps_original_dji_owner(self):
        c = self.controller
        await self.enable_dji()
        heartbeat = c.last_heartbeat
        await c.action('stop', {}, object())
        self.assertIs(c.owner, self.owner)
        self.assertEqual(c.last_heartbeat, heartbeat)

    async def test_overlapping_computer_stops_keep_original_dji_owner(self):
        c = self.controller
        await self.enable_dji()
        await c.write_lock.acquire()
        first = asyncio.create_task(c.action('stop', {}, self.owner))
        second = asyncio.create_task(c.action('stop', {}, object()))
        await asyncio.sleep(0)
        c.write_lock.release()
        await asyncio.gather(first, second)
        self.assertEqual(c.input['source'], 'dji')
        self.assertIs(c.owner, self.owner)
        self.assertTrue(c.input['neutral_required'])
        self.assertEqual(c.train['power'], 0)

    async def test_stop_cannot_restore_closed_owner_or_changed_session(self):
        c = self.controller
        for change in ('closed', 'session'):
            self.owner = type('Owner', (), {'closed': False})()
            await self.enable_dji()
            await c.write_lock.acquire()
            stopped = asyncio.create_task(c.action('stop', {}, object()))
            await asyncio.sleep(0)
            if change == 'closed':
                self.owner.closed = True
            else:
                c.remote_session += 1
                c.input['source'] = 'manual'
            c.write_lock.release()
            await stopped
            self.assertIsNone(c.owner)
            self.assertEqual(c.input['source'], 'manual')

    async def test_stop_discards_motion_submitted_while_stop_is_waiting(self):
        c = self.controller
        await c.write_lock.acquire()
        stopped = asyncio.create_task(c.action('stop', {}, self.owner))
        await asyncio.sleep(0)
        driving = asyncio.create_task(c.action('drive', {'power': 35}, self.owner))
        await asyncio.sleep(0)
        c.write_lock.release()
        await asyncio.gather(stopped, driving)
        self.assertEqual(c.train['power'], 0)
        self.assertIsNone(c.owner)
        self.assertFalse(any('Forward' in event['message'] for event in c.logs))

    async def test_cancelled_stop_finishes_its_motor_command(self):
        c = self.controller
        await c.drive(35, self.owner)
        await c.write_lock.acquire()
        stopped = asyncio.create_task(c.action('stop', {}, self.owner))
        await asyncio.sleep(0)
        stopped.cancel()
        await asyncio.sleep(0)
        self.assertFalse(stopped.done())
        c.write_lock.release()
        with self.assertRaises(asyncio.CancelledError):
            await stopped
        self.assertEqual(c.train['power'], 0)
        self.assertIsNone(c.owner)
        self.assertIsNone(c.input['driver'])
        self.assertEqual(c.logs[-1]['raw'], '08 00 81 32 11 51 00 00')

    async def test_latest_sample_discards_queued_motion_and_old_frames(self):
        c = self.controller
        await self.enable_dji()
        await c.write_lock.acquire()
        consumer = asyncio.create_task(c.consume_remote_samples())
        try:
            c.publish_remote_sample(self.controls(1), time.monotonic())
            await asyncio.sleep(0)
            latest_time = time.monotonic()
            c.publish_remote_sample(self.controls(-1), latest_time)
            self.assertFalse(c.publish_remote_sample(self.controls(1), latest_time - .01))
            self.assertFalse(c.publish_remote_sample(self.controls(1), time.monotonic() - 1))
            c.write_lock.release()
            for _ in range(20):
                if c.train['power'] == -35:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(c.train['power'], -35)
            self.assertFalse(any('Forward' in event['message'] for event in c.logs))
        finally:
            if c.write_lock.locked():
                c.write_lock.release()
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    async def test_queued_stick_sample_expires_while_waiting_for_bluetooth(self):
        c = self.controller
        await self.enable_dji()
        c.publish_remote_sample(self.controls(1), time.monotonic())
        revision, received_at, controls = c.remote_sample
        await c.write_lock.acquire()
        task = asyncio.create_task(c.apply_remote_sample(revision, received_at, controls))
        await asyncio.sleep(0)
        with patch('app_server.time') as clock:
            clock.monotonic.return_value = received_at + .7
            c.write_lock.release()
            await task
        self.assertEqual(c.train['power'], 0)
        self.assertFalse(any('Forward' in event['message'] for event in c.logs))

    async def test_computer_takeover_cancels_queued_stick_motion(self):
        c = self.controller
        await self.enable_dji()
        await c.write_lock.acquire()
        c.publish_remote_sample(self.controls(1), time.monotonic())
        stick = asyncio.create_task(c.apply_remote_sample(*c.remote_sample))
        await asyncio.sleep(0)
        computer = asyncio.create_task(c.action('drive', {'power': -20}, self.owner))
        await asyncio.sleep(0)
        c.write_lock.release()
        await asyncio.gather(stick, computer)
        self.assertEqual(c.train['power'], -20)
        self.assertTrue(c.input['manual_override'])
        self.assertFalse(any('Forward' in event['message'] for event in c.logs))

    async def test_receive_only_reader_reconnects_and_closes_all_writers(self):
        c = self.controller
        readers, writers = [], []

        async def connect(_ip, _port):
            reader, writer = asyncio.StreamReader(), AsyncMock()
            writer.write = Mock()
            writer.close = Mock()
            readers.append(reader)
            writers.append(writer)
            reader.feed_data(NEUTRAL)
            if len(readers) == 1:
                reader.feed_eof()
            return reader, writer

        with patch('app_server.asyncio.open_connection', new=connect):
            stream = asyncio.create_task(c.remote_stream('127.0.0.1'))
            try:
                for _ in range(40):
                    if len(readers) >= 2:
                        break
                    await asyncio.sleep(.01)
                self.assertGreaterEqual(len(readers), 2)
                self.assertEqual(c.remote['status'], 'connected')
                self.assertEqual(sum(task.get_name().startswith('dji-reader-') for task in asyncio.all_tasks()), 5)
            finally:
                stream.cancel()
                await asyncio.gather(stream, return_exceptions=True)
        self.assertTrue(all(writer.close.called for writer in writers))
        self.assertTrue(all(not writer.write.called for writer in writers))
        self.assertFalse(any(task.get_name().startswith('dji-reader-') for task in asyncio.all_tasks()))

    async def test_wifi_acquisition_continues_during_slow_bluetooth_write(self):
        c = self.controller
        await self.enable_dji()
        reader, writer = asyncio.StreamReader(), AsyncMock()
        writer.close = Mock()
        write_started, release_write = asyncio.Event(), asyncio.Event()

        async def bluetooth_write(*_args, **_kwargs):
            write_started.set()
            await release_write.wait()

        c.demo = False
        c.client = AsyncMock()
        c.client.is_connected = True
        c.client.write_gatt_char.side_effect = bluetooth_write
        c.characteristic = type('Characteristic', (), {'properties': ['write']})()
        reader.feed_data(self.frame(1))
        with patch('app_server.asyncio.open_connection', new=AsyncMock(return_value=(reader, writer))):
            stream = asyncio.create_task(c.remote_stream('127.0.0.1'))
            try:
                await asyncio.wait_for(write_started.wait(), .5)
                before = c.last_remote_sample
                reader.feed_data(self.frame(-1))
                for _ in range(20):
                    if c.last_remote_sample > before:
                        break
                    await asyncio.sleep(.01)
                self.assertGreater(c.last_remote_sample, before)
                self.assertEqual(c.remote['axes']['left_y'], -1)
                self.assertEqual(c.train['power'], 0)
                release_write.set()
                for _ in range(20):
                    if c.train['power'] == -35:
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(c.train['power'], -35)
            finally:
                release_write.set()
                stream.cancel()
                await asyncio.gather(stream, return_exceptions=True)
                c.demo = True
                c.client = c.characteristic = None

    async def test_fatal_transport_error_cleans_readers_before_disabling_input(self):
        c = self.controller
        await self.enable_dji()
        await c.action('drive', {'power': 25}, self.owner)
        with patch('app_server.asyncio.open_connection', new=AsyncMock(side_effect=PermissionError('denied'))):
            await c.remote_stream('127.0.0.1')
        self.assertEqual(c.remote['status'], 'error')
        self.assertEqual(c.input['source'], 'manual')
        self.assertEqual(c.train['power'], 25)
        self.assertIs(c.owner, self.owner)
        self.assertFalse(any(task.get_name().startswith('dji-reader-') for task in asyncio.all_tasks()))

    async def test_concurrent_remote_connect_does_not_orphan_streams(self):
        c = self.controller
        c.demo = False
        started, finished = [], []

        async def stream(ip):
            started.append(ip)
            try:
                await asyncio.Event().wait()
            finally:
                finished.append(ip)

        try:
            with patch.object(c, 'remote_stream', new=stream):
                await asyncio.gather(c.connect_remote('192.168.1.2'), c.connect_remote('192.168.1.3'))
                await asyncio.sleep(0)
                await c.disconnect_remote()
            self.assertEqual(set(started), set(finished))
            self.assertIsNone(c.remote_task)
            self.assertEqual(c.remote['status'], 'disconnected')
        finally:
            c.demo = True

    async def test_search_connects_only_a_unique_verified_stick_stream(self):
        c = self.controller
        c.demo = False
        candidates = [{'ip': '192.168.1.12', 'duml_verified': True, 'controls_verified': False},
                      {'ip': '192.168.1.91', 'duml_verified': True, 'controls_verified': True}]
        with patch.object(dji_remote, 'local_network', return_value=ipaddress.ip_network('192.168.1.0/24')), \
                patch.object(dji_remote, 'scan', return_value=candidates) as scan, \
                patch.object(c, 'remote_stream', new_callable=AsyncMock) as stream:
            await c.action('remote_search', {}, self.owner)
            self.assertEqual(c.remote['status'], 'scanning')
            await c.remote_search_task
            await asyncio.sleep(0)
            scan.assert_called_once_with(ipaddress.ip_network('192.168.1.0/24'), 40007)
            stream.assert_awaited_once_with('192.168.1.91')
            self.assertEqual(c.snapshot()['controller']['ip'], '192.168.1.91')
            self.assertEqual(c.remote['status'], 'connecting')
            self.assertTrue(c.input['neutral_required'])
            self.assertIsNone(c.remote_search_task)
            await c.disconnect_remote()

    async def test_search_failure_explains_no_remote_and_multiple_remotes(self):
        c = self.controller
        c.demo = False
        for candidates, expected in (([], 'No DJI remote found'),
                                     ([{'ip': '192.168.1.12', 'duml_verified': True}], 'No DJI remote found'),
                                     ([{'ip': '192.168.1.91', 'controls_verified': True},
                                       {'ip': '192.168.1.92', 'controls_verified': True}], 'Several DJI remotes found')):
            with self.subTest(candidates=candidates):
                with patch.object(dji_remote, 'local_network', return_value=ipaddress.ip_network('192.168.1.0/24')), \
                        patch.object(dji_remote, 'scan', return_value=candidates), \
                        patch.object(c, 'remote_stream', new_callable=AsyncMock) as stream:
                    await c.search_remote()
                    await c.remote_search_task
                    self.assertEqual(c.remote['status'], 'error')
                    self.assertIn(expected, c.remote['error'])
                    stream.assert_not_called()
                    self.assertIsNone(c.remote_task)

    async def test_search_network_errors_are_visible_and_demo_never_scans(self):
        c = self.controller
        with patch.object(dji_remote, 'scan') as scan:
            with self.assertRaisesRegex(ValueError, 'disabled in Demo'):
                await c.search_remote()
            scan.assert_not_called()
        c.demo = False
        with patch.object(dji_remote, 'local_network', side_effect=ValueError('No subnet')):
            await c.search_remote()
            await c.remote_search_task
            self.assertIn("Couldn't choose the Wi-Fi network", c.remote['error'])
        with patch.object(dji_remote, 'local_network', return_value=ipaddress.ip_network('192.168.1.0/24')), \
                patch.object(dji_remote, 'scan', side_effect=PermissionError('Network access denied')):
            await c.search_remote()
            await c.remote_search_task
            self.assertEqual(c.remote['status'], 'error')
            self.assertIn('Network access denied', c.remote['error'])

    async def test_cancelled_search_cannot_connect_later_or_duplicate(self):
        c = self.controller
        c.demo = False
        started = asyncio.Event()
        gate = asyncio.Event()

        async def background_call(function, *args):
            if function is dji_remote.local_network:
                return ipaddress.ip_network('192.168.1.0/24')
            started.set()
            await gate.wait()
            return [{'ip': '192.168.1.91', 'controls_verified': True}]

        with patch('app_server.asyncio.to_thread', new=background_call), \
                patch.object(c, 'remote_stream', new_callable=AsyncMock) as stream:
            await c.search_remote()
            search = c.remote_search_task
            await started.wait()
            await c.search_remote()
            self.assertIs(c.remote_search_task, search)
            await c.disconnect_remote()
            gate.set()
            await asyncio.sleep(0)
            self.assertTrue(search.cancelled())
            self.assertIsNone(c.remote_search_task)
            self.assertEqual(c.remote['status'], 'disconnected')
            stream.assert_not_called()

    async def test_manual_connection_cancels_search_and_close_cleans_it_up(self):
        c = self.controller
        c.demo = False

        async def background_call(function, *args):
            await asyncio.Event().wait()

        with patch('app_server.asyncio.to_thread', new=background_call), \
                patch.object(c, 'remote_stream', new_callable=AsyncMock):
            await c.search_remote()
            search = c.remote_search_task
            await asyncio.sleep(0)
            await c.connect_remote('192.168.1.88')
            self.assertTrue(search.cancelled())
            self.assertEqual(c.remote['ip'], '192.168.1.88')
            await c.disconnect_remote()
            await c.search_remote()
            search = c.remote_search_task
            await asyncio.sleep(0)
            await c.close()
            self.assertTrue(search.cancelled())
            self.assertIsNone(c.remote_search_task)

    async def test_auto_connect_retries_the_train_and_reports_each_try(self):
        c = self.controller
        c.demo, c.retry_delay = False, 0
        c.train['status'] = 'disconnected'
        seen = []

        async def connect(address):
            seen.append((address, dict(c.attempts['train'])))
            if len(seen) < 3:
                raise ValueError('No train found.')
            c.train['status'] = 'connected'

        with patch.object(c, 'connect_train', side_effect=connect):
            await c.action('auto_connect', {'targets': ['train'], 'address': 'AA:BB'}, self.owner)
            self.assertEqual(c.snapshot()['attempts']['train'], {'try': 1, 'of': 5})
            await c.connect_tasks['train']
        self.assertEqual(seen, [('AA:BB', {'try': n, 'of': 5}) for n in (1, 2, 3)])
        self.assertEqual(c.train['status'], 'connected')
        self.assertEqual(c.snapshot()['attempts'], {'train': None, 'remote': None})
        self.assertIsNone(c.connect_tasks['train'])
        self.assertTrue(any('Try 2 of 5 for the train failed' in event['message'] for event in c.logs))
        c.train['status'] = 'disconnected'

    async def test_auto_connect_gives_up_after_the_last_try(self):
        c = self.controller
        c.demo, c.retry_delay = False, 0
        c.train['status'] = 'disconnected'
        with patch.object(c, 'connect_train', new=AsyncMock(side_effect=ValueError('No train found.'))) as connect:
            await c.auto_connect(['train'])
            await c.connect_tasks['train']
        self.assertEqual(connect.await_count, 5)
        self.assertIsNone(c.attempts['train'])
        self.assertEqual(c.logs[-1]['message'], "Couldn't connect the train after 5 tries.")

    async def test_auto_connect_skips_connected_devices_and_validates_targets(self):
        c = self.controller
        await c.auto_connect(['train'])
        self.assertIsNone(c.connect_tasks['train'])
        for targets in (None, [], ['train', 'lamp'], 'train'):
            with self.subTest(targets=targets), self.assertRaisesRegex(ValueError, 'Choose the train'):
                await c.action('auto_connect', {'targets': targets}, self.owner)
        with self.assertRaisesRegex(ValueError, 'disabled in Demo'):
            await c.auto_connect(['train', 'remote'])
        self.assertIsNone(c.connect_tasks['remote'])

    async def test_one_device_can_stop_trying_while_the_other_continues(self):
        c = self.controller
        c.demo = False
        c.train['status'] = 'disconnected'
        hold = asyncio.Event()

        async def connect(address):
            c.train['status'] = 'scanning'
            await hold.wait()

        async def background_call(function, *args):
            await hold.wait()

        with patch.object(c, 'connect_train', side_effect=connect), \
                patch('app_server.asyncio.to_thread', new=background_call):
            await c.action('auto_connect', {'targets': ['train', 'remote']}, self.owner)
            train, remote = c.connect_tasks['train'], c.connect_tasks['remote']
            await asyncio.sleep(0)
            self.assertEqual(c.remote['status'], 'scanning')
            await c.action('stop_connecting', {'targets': ['train']}, self.owner)
            self.assertTrue(train.cancelled())
            self.assertIsNone(c.attempts['train'])
            self.assertFalse(remote.done())
            self.assertEqual(c.attempts['remote'], {'try': 1, 'of': 5})
            await c.action('stop_connecting', {'targets': ['train', 'remote']}, self.owner)
            self.assertTrue(remote.cancelled())
            self.assertEqual(c.remote['status'], 'disconnected')
            self.assertEqual(c.snapshot()['attempts'], {'train': None, 'remote': None})

    async def test_remote_tries_remembered_ip_then_searches_wifi(self):
        c = self.controller
        c.demo, c.remote_input_timeout = False, 0.2
        streams = []

        async def stream(ip):
            streams.append(ip)
            if ip == '192.168.1.91':
                c.publish_remote_sample(self.controls(), time.monotonic())
            await asyncio.Event().wait()

        with patch.object(dji_remote, 'local_network', return_value=ipaddress.ip_network('192.168.1.0/24')), \
                patch.object(dji_remote, 'scan', return_value=[{'ip': '192.168.1.91', 'controls_verified': True}]), \
                patch.object(c, 'remote_stream', side_effect=stream):
            await c.auto_connect(['remote'], ip='192.168.1.50')
            await c.connect_tasks['remote']
            self.assertEqual(streams, ['192.168.1.50', '192.168.1.91'])
            self.assertEqual(c.remote['status'], 'connected')
            self.assertEqual(c.remote['ip'], '192.168.1.91')
            self.assertTrue(any('No stick input from 192.168.1.50' in event['message'] for event in c.logs))
            await c.disconnect_remote()

    async def test_failed_remote_try_stops_readers_and_manual_disconnect_cancels_tries(self):
        c = self.controller
        c.demo, c.retry_delay, c.remote_input_timeout = False, 0, 0.05

        with patch.object(dji_remote, 'local_network', return_value=ipaddress.ip_network('192.168.1.0/24')), \
                patch.object(dji_remote, 'scan', return_value=[]), \
                patch.object(c, 'remote_stream', new=AsyncMock()):
            await c.auto_connect(['remote'], ip='192.168.1.50')
            await c.connect_tasks['remote']
        self.assertEqual(c.remote['status'], 'error')
        self.assertIn('No DJI remote found', c.remote['error'])
        self.assertIsNone(c.remote_task)
        c.retry_delay = 60
        with patch.object(dji_remote, 'local_network', side_effect=ValueError('No subnet')), \
                patch.object(c, 'remote_stream', new=AsyncMock()):
            # Stopping before the first try starts, then while waiting to retry.
            for wait_for_retry in (False, True):
                first_log = c.next_id
                await c.auto_connect(['remote'])
                retry = c.connect_tasks['remote']
                while wait_for_retry and not any('Try 1 of 5 for the DJI remote' in event['message']
                                                 for event in c.logs if event['id'] >= first_log):
                    await asyncio.sleep(0.01)
                self.assertFalse(retry.done())
                await c.action('remote_disconnect', {}, self.owner)
                self.assertTrue(retry.cancelled())
                self.assertIsNone(c.attempts['remote'])
                self.assertIsNone(c.connect_tasks['remote'])
                self.assertEqual(c.remote['status'], 'disconnected')


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

    async def test_search_keeps_websocket_and_computer_controls_responsive(self):
        c = self.controller
        c.demo = False
        c.client = Mock(is_connected=True, write_gatt_char=AsyncMock(), disconnect=AsyncMock())
        c.characteristic = Mock(properties=['write'])
        started = asyncio.Event()

        async def background_call(function, *args):
            started.set()
            await asyncio.Event().wait()

        ws = await self.client.ws_connect('/api/ws')
        await ws.receive_json()
        with patch('app_server.asyncio.to_thread', new=background_call), \
                patch.object(c, 'write', new_callable=AsyncMock):
            self.assertTrue((await self.command(ws, 'remote_search'))['ok'])
            await started.wait()
            search = c.remote_search_task
            self.assertEqual(c.remote['status'], 'scanning')
            self.assertTrue((await self.command(ws, 'drive', {'power': 35}, request_id=2))['ok'])
            previous_heartbeat = c.last_heartbeat
            await ws.send_json({'type': 'heartbeat', 'active': True})
            self.assertTrue((await self.command(ws, 'audio_event', {'event': 'loaded'}, request_id=3))['ok'])
            self.assertGreater(c.last_heartbeat, previous_heartbeat)
            self.assertTrue((await self.command(ws, 'stop', request_id=4))['ok'])
            self.assertEqual(c.train['power'], 0)
            self.assertTrue((await self.command(ws, 'remote_disconnect', request_id=5))['ok'])
            self.assertTrue(search.cancelled())
        await ws.close()
        c.demo = True

    async def test_only_an_active_browser_automatically_enables_the_default_stick(self):
        ws = await self.client.ws_connect('/api/ws')
        snapshot = await ws.receive_json()
        self.assertTrue(snapshot['state']['input']['left_stick_enabled'])
        self.controller.publish_remote_sample(dji_remote.decode_controls(NEUTRAL[8:]), time.monotonic())
        await ws.send_json({'type': 'heartbeat', 'active': False})
        await self.command(ws, 'audio_event', {'event': 'loaded'})
        self.assertEqual(self.controller.input['source'], 'manual')
        self.assertIsNone(self.controller.owner)
        await ws.send_json({'type': 'heartbeat', 'active': True})
        await self.command(ws, 'audio_event', {'event': 'loaded'})
        self.assertEqual(self.controller.input['source'], 'dji')
        self.assertIsNotNone(self.controller.owner)
        self.assertTrue(self.controller.input['neutral_required'])
        await self.command(ws, 'source', {'source': 'manual'})
        await ws.send_json({'type': 'heartbeat', 'active': True})
        await self.command(ws, 'audio_event', {'event': 'loaded'})
        self.assertFalse(self.controller.input['left_stick_enabled'])
        self.assertEqual(self.controller.input['source'], 'manual')
        self.assertIsNone(self.controller.owner)
        await ws.close()

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
