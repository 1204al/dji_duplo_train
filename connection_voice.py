"""Spoken connection confirmations on the Mac, independent of browser tabs."""

import asyncio
from collections import deque
from contextlib import suppress


class ConnectionVoice:
    messages = {
        "train": "Train connected.",
        "remote": "D J I remote connected.",
        "both": "Train and remote connected.",
    }

    def __init__(self, enabled=True, warn=None):
        self.enabled = enabled
        self.warn = warn or (lambda message: None)
        self.available = {"train": False, "remote": False}
        self.announced = {"train": False, "remote": False, "both": False}
        self.generation = {"train": 0, "remote": 0}
        self.pending = deque()
        self.task = None
        self.closed = False

    def ready(self, device):
        if not self.enabled or self.closed:
            return
        self.available[device] = True
        if not self.announced[device]:
            self.enqueue(device, (device,))
        if all(self.available.values()) and not self.announced["both"]:
            self.enqueue("both", ("train", "remote"))

    def waiting(self, device):
        # A brief Wi-Fi gap does not start another connection session.
        self.available[device] = False

    def disconnected(self, device):
        self.available[device] = False
        self.announced[device] = self.announced["both"] = False
        self.generation[device] += 1
        self.pending = deque((name, session) for name, session in self.pending
                             if device not in session)

    def enqueue(self, name, devices):
        self.announced[name] = True
        self.pending.append((name, {device: self.generation[device] for device in devices}))
        if self.task is None:
            self.task = asyncio.create_task(self.play_pending())

    async def play_pending(self):
        try:
            while self.pending:
                name, session = self.pending.popleft()
                if any(self.generation[device] != generation for device, generation in session.items()):
                    continue
                if not all(self.available[device] for device in session):
                    self.announced[name] = False
                    continue
                try:
                    await self.speak(self.messages[name])
                except (OSError, RuntimeError, TimeoutError) as error:
                    self.warn(f"Connection voice unavailable: {error}")
        finally:
            self.task = None

    async def speak(self, message):
        # Match the chosen preview: Samantha, 190 words/minute. No network,
        # browser autoplay permission, or redistributed system-voice recordings.
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/say", "-v", "Samantha", "-r", "190", message,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, error = await asyncio.wait_for(process.communicate(), timeout=10)
            if process.returncode:
                raise OSError(error.decode(errors="replace").strip() or "macOS speech failed")
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    async def close(self):
        self.closed = True
        self.pending.clear()
        if self.task is not None:
            task = self.task
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self.task = None
