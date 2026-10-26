"""Connection speech uses simulated players and hardware; tests never speak."""

import asyncio
from contextlib import suppress
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from app_server import Controller
from connection_voice import ConnectionVoice
from train import LEGO_COMPANY_ID


class ConnectionVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.warn = Mock()
        self.voice = ConnectionVoice(warn=self.warn)
        self.voice.speak = AsyncMock()

    async def asyncTearDown(self):
        await self.voice.close()

    async def drain(self):
        if self.voice.task is not None:
            await asyncio.wait_for(self.voice.task, 1)

    def spoken(self):
        return [call.args[0] for call in self.voice.speak.await_args_list]

    async def test_each_device_and_both_announce_once_in_either_order(self):
        for first, second in (("train", "remote"), ("remote", "train")):
            with self.subTest(first=first):
                self.voice.disconnected("train")
                self.voice.disconnected("remote")
                self.voice.speak.reset_mock()
                self.voice.ready(first)
                await self.drain()
                self.assertEqual(self.spoken(), [self.voice.messages[first]])
                for _ in range(100):
                    self.voice.ready(second)
                    self.voice.ready(first)
                await self.drain()
                self.assertEqual(self.spoken(), [self.voice.messages[first],
                                                self.voice.messages[second],
                                                self.voice.messages["both"]])

    async def test_brief_input_gap_does_not_repeat_played_confirmations(self):
        self.voice.ready("train")
        self.voice.ready("remote")
        await self.drain()
        self.voice.waiting("remote")
        self.voice.ready("remote")
        await self.drain()
        self.assertEqual(len(self.spoken()), 3)

    async def test_both_message_waits_for_remote_input_to_return(self):
        self.voice.ready("remote")
        await self.drain()
        self.voice.waiting("remote")
        self.voice.ready("train")
        await self.drain()
        self.assertEqual(self.spoken(), [self.voice.messages["remote"], self.voice.messages["train"]])
        self.voice.ready("remote")
        await self.drain()
        self.assertEqual(self.spoken()[-1], self.voice.messages["both"])
        self.assertEqual(len(self.spoken()), 3)

    async def test_disconnect_drops_queued_remote_and_both_then_rearms(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def speak(message):
            started.set()
            await release.wait()

        self.voice.speak.side_effect = speak
        self.voice.ready("train")
        await started.wait()
        self.voice.ready("remote")
        self.voice.disconnected("remote")
        release.set()
        await self.drain()
        self.assertEqual(self.spoken(), [self.voice.messages["train"]])
        self.voice.ready("remote")
        await self.drain()
        self.assertEqual(self.spoken(), list(self.voice.messages.values()))

    async def test_gap_drops_stale_queued_messages_and_can_retry_when_fresh(self):
        self.voice.ready("train")
        self.voice.ready("remote")
        self.voice.waiting("remote")
        await self.drain()
        self.assertEqual(self.spoken(), [self.voice.messages["train"]])
        self.voice.ready("remote")
        await self.drain()
        self.assertEqual(self.spoken(), list(self.voice.messages.values()))

    async def test_speech_failure_does_not_break_the_queue(self):
        self.voice.speak.side_effect = [OSError("No speech output"), None, None]
        self.voice.ready("train")
        self.voice.ready("remote")
        await self.drain()
        self.assertEqual(self.spoken(), list(self.voice.messages.values()))
        self.warn.assert_called_once()
        self.assertIn("No speech output", self.warn.call_args.args[0])

    async def test_demo_and_closed_player_are_silent(self):
        self.voice.enabled = False
        self.voice.ready("train")
        self.voice.ready("remote")
        await self.drain()
        self.voice.enabled = True
        self.voice.ready("train")
        await self.voice.close()
        self.voice.ready("remote")
        self.voice.speak.assert_not_awaited()
        self.assertIsNone(self.voice.task)
        self.assertFalse(self.voice.pending)

    async def test_shutdown_kills_an_active_speech_process(self):
        started = asyncio.Event()

        async def communicate():
            started.set()
            await asyncio.Event().wait()

        process = Mock(returncode=None, communicate=AsyncMock(side_effect=communicate),
                       wait=AsyncMock())
        self.voice.speak = ConnectionVoice.speak.__get__(self.voice)
        with patch("connection_voice.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)) as spawn:
            self.voice.ready("train")
            await started.wait()
            await self.voice.close()
        self.assertEqual(spawn.await_args.args, ("/usr/bin/say", "-v", "Samantha", "-r", "190",
                                                "Train connected."))
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
        self.assertIsNone(self.voice.task)


class ControllerVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.controller = Controller()
        self.controller.voice.speak = AsyncMock()

    async def asyncTearDown(self):
        await self.controller.close()

    async def drain(self):
        if self.controller.voice.task is not None:
            await asyncio.wait_for(self.controller.voice.task, 1)

    async def test_train_announces_only_after_all_bluetooth_setup_succeeds(self):
        c = self.controller
        device = Mock(address="TRAIN", name="DUPLO Train")
        advertisement = Mock(manufacturer_data={LEGO_COMPANY_ID: b"\x00\x21"},
                             local_name="DUPLO Train", rssi=-40)
        services = MagicMock()
        services.__iter__.return_value = iter([])
        client = Mock(services=services, connect=AsyncMock(), start_notify=AsyncMock(),
                      disconnect=AsyncMock(), write_gatt_char=AsyncMock(), is_connected=True)

        async def setup_write(*args):
            self.assertFalse(c.voice.available["train"])
            c.voice.speak.assert_not_awaited()

        with patch("app_server.BleakScanner.discover", new=AsyncMock(return_value={"train": (device, advertisement)})), \
                patch("app_server.BleakClient", return_value=client), \
                patch.object(c, "write", new=AsyncMock(side_effect=setup_write)):
            await c.connect_train()
        await self.drain()
        c.voice.speak.assert_awaited_once_with("Train connected.")

    async def test_failed_train_setup_is_silent(self):
        c = self.controller
        with patch("app_server.BleakScanner.discover", new=AsyncMock(return_value={})):
            with self.assertRaises(ValueError):
                await c.connect_train()
        c.voice.speak.assert_not_awaited()

    async def test_only_fresh_valid_remote_input_announces_and_gaps_stay_quiet(self):
        c = self.controller
        self.assertFalse(c.publish_remote_sample({"axes": {}}, time.monotonic()))
        self.assertFalse(c.publish_remote_sample({"axes": {"left_y": 0}}, time.monotonic() - 1))
        c.voice.speak.assert_not_awaited()
        self.assertTrue(c.publish_remote_sample({"axes": {"left_y": 0}}, time.monotonic()))
        await self.drain()
        c.voice.speak.assert_awaited_once_with("D J I remote connected.")
        c.last_remote_sample = time.monotonic() - 1
        watchdog = asyncio.create_task(c.watchdog())
        try:
            await asyncio.sleep(.18)
            self.assertEqual(c.remote["status"], "waiting")
        finally:
            watchdog.cancel()
            with suppress(asyncio.CancelledError):
                await watchdog
        c.publish_remote_sample({"axes": {"left_y": 0}}, time.monotonic())
        await self.drain()
        c.voice.speak.assert_awaited_once()
        await c.disconnect_remote()
        c.publish_remote_sample({"axes": {"left_y": 0}}, time.monotonic())
        await self.drain()
        self.assertEqual(c.voice.speak.await_count, 2)

    async def test_drive_and_stop_complete_while_speech_is_still_playing(self):
        c = self.controller
        c.demo = True
        c.train["status"] = "connected"
        started = asyncio.Event()

        async def speak(message):
            started.set()
            await asyncio.Event().wait()

        c.voice.speak.side_effect = speak
        c.publish_remote_sample({"axes": {"left_y": 0}}, time.monotonic())
        await started.wait()
        owner = object()
        await asyncio.wait_for(c.action("drive", {"power": 35}, owner), .2)
        self.assertEqual(c.train["power"], 35)
        await asyncio.wait_for(c.action("stop", {}, owner), .2)
        self.assertEqual(c.train["power"], 0)
        self.assertFalse(c.voice.task.done())
