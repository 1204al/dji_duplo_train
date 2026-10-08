"""Local DUPLO controller: Bluetooth, DJI input and a same-origin web interface."""

import argparse
import asyncio
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone
import ipaddress
import json
import math
from pathlib import Path
import time
from urllib.parse import urlparse

from aiohttp import web, WSMsgType
from bleak import BleakClient, BleakScanner

import dji_remote
from lego_protocol import (PORTS, SOUNDS, INTERACTIVE_SOUNDS, decode_packet, light_packet, motor_packet,
                           request_property, sound_packet, subscribe, power_off_packet,
                           port_info_packet, mode_info_packet)
from train import CHARACTERISTIC, SERVICE, LEGO_COMPANY_ID, DUPLO_HUB_TYPES

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "ui" / "dist" / "client"


def finite_number(value, minimum, maximum, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a number.")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


class Controller:
    # Automatic connection: tries per device and the pause between them.
    connect_tries = 5
    retry_delay = 2
    remote_input_timeout = 5

    def __init__(self, demo=False, settings_path=None):
        self.demo = demo
        self.train = {"status": "disconnected", "name": None, "address": None,
                      "hub_type": 0x21, "battery": None, "rssi": None, "power": 0,
                      "light": "off", "error": None, "measured_speed": None,
                      "tag": None, "tag_name": None, "ports": {}, "firmware": None,
                      "modes": {}, "mode_formats": {}, "bluetooth_services": [],
                      "purple_events": 0, "purple_at": None,
                      "shutdown_reason": None}
        self.settings_path = Path(settings_path) if settings_path else None
        self.idle_minutes = 0
        preferences = {}
        if self.settings_path and self.settings_path.exists():
            try:
                preferences = json.loads(self.settings_path.read_text())
                if not isinstance(preferences, dict):
                    preferences = {}
                value = preferences.get("idle_minutes", 0)
                if type(value) is int and value in (0, 1, 5, 10, 15, 30):
                    self.idle_minutes = value
            except (ValueError, OSError, KeyError, TypeError):
                pass
        self.last_activity = time.monotonic()
        self.last_countdown = None
        self.remote = {"status": "disconnected", "ip": "", "axes": {}, "buttons": {},
                       "axis_raw": {}, "mode": None, "error": None}
        self.input = {"source": "manual", "axis": "left_y",
                      "invert": preferences.get("invert") if type(preferences.get("invert")) is bool else True,
                      "left_stick_enabled": (preferences.get("left_stick_enabled")
                                             if type(preferences.get("left_stick_enabled")) is bool else True),
                      "deadzone": 0.08, "center": 0, "max_power": 35,
                      "neutral_required": True, "manual_override": False,
                      "driver": None}
        self.logs = deque(maxlen=1000)
        self.next_id = 1
        self.client = None
        self.characteristic = None
        self.clients = set()
        self.owner = None
        self.last_heartbeat = 0
        self.last_remote_sample = 0
        self.remote_revision = 0
        self.remote_session = 0
        self.remote_sample = None
        self.remote_paused = False
        self.remote_ready = asyncio.Event()
        self.previous_remote_controls = None
        self.last_remote_log = 0
        self.neutral_after = time.monotonic()
        self.write_lock = asyncio.Lock()
        self.lifecycle_lock = asyncio.Lock()
        self.remote_lifecycle_lock = asyncio.Lock()
        self.computer_stop_lock = asyncio.Lock()
        self.stops_pending = 0
        self.remote_task = None
        self.remote_search_task = None
        self.connect_tasks = {"train": None, "remote": None}
        self.attempts = {"train": None, "remote": None}
        self.tasks = set()
        self.dirty = asyncio.Event()
        self.closed = False
        self.epoch = 0
        if demo:
            self.train.update(status="connected", name="DUPLO Train", battery=100,
                              rssi=-50, address="DEMO-TRAIN", measured_speed=0)
            self.log("train", "TX", "Request battery level", request_property(6))
            self.log("train", "RX", "Battery level: 100%", bytes.fromhex("06 00 01 06 06 64"))
            self.log("input", "INFO", "Keyboard & buttons active")
            self.log("input", "INFO", "DJI remote not configured")
        else:
            self.log("app", "INFO", "Ready. Connect your train to begin.")

    def snapshot(self):
        left_stick = self.left_stick_drive(self.remote)
        return {"train": dict(self.train), "controller": dict(self.remote),
                "input": dict(self.input), "demo": self.demo,
                "attempts": dict(self.attempts), "connect_tries": self.connect_tries,
                "left_stick": {**(left_stick or {"position": None, "power": None}),
                               "available": left_stick is not None and self.remote_input_fresh()},
                "sounds": list(SOUNDS) if self.train["hub_type"] == 0x20 else list(INTERACTIVE_SOUNDS),
                "service": SERVICE, "characteristic": CHARACTERISTIC,
                "settings": {"idle_minutes": self.idle_minutes,
                             "idle_remaining_seconds": self.idle_remaining()}}

    def idle_remaining(self):
        if not self.idle_minutes or self.train["status"] != "connected" or self.train["power"]:
            return None
        return max(0, math.ceil(self.idle_minutes * 60 - (time.monotonic() - self.last_activity)))

    def activity(self):
        self.last_activity = time.monotonic()

    def save_settings(self, **changes):
        if self.settings_path is None:
            return
        settings = {"idle_minutes": self.idle_minutes,
                    "left_stick_enabled": self.input["left_stick_enabled"],
                    "invert": self.input["invert"]}
        settings.update(changes)
        temporary = self.settings_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(settings) + "\n")
        temporary.replace(self.settings_path)

    def log(self, source, direction, message, raw=None, level="info"):
        event = {"id": self.next_id, "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                 "source": source, "direction": direction, "message": message,
                 "raw": raw.hex(" ") if raw else "", "level": level}
        self.next_id += 1
        self.logs.append(event)
        self.dirty.set()

    async def updates(self):
        last_log = self.next_id - 1
        while not self.closed:
            await self.dirty.wait()
            await asyncio.sleep(0.05)
            self.dirty.clear()
            payload = {"type": "update", "state": self.snapshot(),
                       "logs": [event for event in self.logs if event["id"] > last_log]}
            last_log = self.next_id - 1
            for ws in list(self.clients):
                if not ws.closed:
                    with suppress(ConnectionError, RuntimeError):
                        await ws.send_json(payload)

    def notification(self, _sender, data):
        message, changes = decode_packet(bytes(data), self.train["hub_type"])
        if "port" in changes:
            port, kind = changes.pop("port")
            if kind is None:
                self.train["ports"].pop(str(port), None)
            else:
                self.train["ports"][str(port)] = kind
        if "port_modes" in changes:
            port, info = changes.pop("port_modes")
            info["names"] = self.train["modes"].get(str(port), {}).get("names", {})
            self.train["modes"][str(port)] = info
        if "mode_name" in changes:
            port, mode, name = changes.pop("mode_name")
            info = self.train["modes"].setdefault(str(port), {"names": {}})
            info["names"][str(mode)] = name
        if "mode_format" in changes:
            port, mode, info = changes.pop("mode_format")
            self.train["mode_formats"].setdefault(str(port), {})[str(mode)] = info
        if "tag" in changes or changes.get("measured_speed", 0):
            self.activity()
        if changes.get("tag") == 324:
            # Count notifications, not value changes: the same brick may be read again.
            self.train["purple_events"] += 1
            self.train["purple_at"] = time.time()
        protocol_error = changes.pop("protocol_error", False)
        self.train.update(changes)
        self.log("train", "RX", message, data, "error" if protocol_error else "info")

    def lost_train(self, _client):
        expected = self.train["status"] == "powering_off"
        self.train.update(status="disconnected", power=0, battery=None, rssi=None,
                          measured_speed=None, tag=None, tag_name=None)
        self.owner = None
        self.epoch += 1
        self.input.update(source="manual", neutral_required=True, manual_override=False, driver=None)
        self.log("train", "INFO", "Bluetooth disconnected during power off" if expected else "Train disconnected",
                 level="info" if expected else "warning")

    async def write(self, packet, message):
        async with self.write_lock:
            if self.train["status"] != "connected":
                raise ValueError("Connect the train first.")
            if not self.demo:
                if self.client is None or not self.client.is_connected:
                    raise ValueError("The Bluetooth connection has been lost. Reconnect the train.")
                await asyncio.wait_for(self.client.write_gatt_char(
                    self.characteristic, packet,
                    response="write" in self.characteristic.properties), timeout=4)
            self.log("train", "TX", message, packet)

    async def connect_train(self, address=None):
        async with self.lifecycle_lock:
            if self.train["status"] == "connected":
                return
            self.train.update(status="scanning", error=None, battery=None, rssi=None,
                              ports={}, modes={}, mode_formats={}, bluetooth_services=[], firmware=None, measured_speed=None,
                              tag=None, tag_name=None, shutdown_reason=None)
            self.activity()
            self.log("train", "INFO", "Searching for nearby DUPLO trains…")
            client = None
            try:
                if self.demo:
                    await asyncio.sleep(0.3)
                    self.train.update(status="connected", name="DUPLO Train", battery=100,
                                      rssi=-50, address="DEMO-TRAIN")
                    self.log("train", "INFO", "Demo train connected")
                    return
                found = await BleakScanner.discover(timeout=7, return_adv=True, service_uuids=[SERVICE])
                candidates = []
                for device, advertisement in found.values():
                    data = advertisement.manufacturer_data.get(LEGO_COMPANY_ID, b"")
                    if len(data) >= 2 and data[1] in DUPLO_HUB_TYPES:
                        if not address or device.address.lower() == address.lower():
                            candidates.append((device, advertisement, data[1]))
                if not candidates:
                    raise ValueError("No train found. Switch it on nearby and close the LEGO phone app, then retry.")
                if len(candidates) > 1:
                    raise ValueError("Several trains found. Enter the train's Bluetooth identifier in Connection details.")
                device, advertisement, kind = candidates[0]
                self.train.update(status="connecting", address=device.address,
                                  name=advertisement.local_name or device.name, hub_type=kind,
                                  rssi=advertisement.rssi)
                self.log("train", "INFO", f"Connecting to {self.train['name']}…")
                client = BleakClient(device, disconnected_callback=self.lost_train, timeout=20)
                await client.connect()
                characteristic = client.services.get_characteristic(CHARACTERISTIC)
                if characteristic is None:
                    raise ValueError("The LEGO Bluetooth service was not found.")
                self.client, self.characteristic = client, characteristic
                self.train["bluetooth_services"] = [
                    {"uuid": service.uuid, "characteristics": [
                        {"uuid": char.uuid, "properties": list(char.properties),
                         "max_write_without_response": char.max_write_without_response_size}
                        for char in service.characteristics]}
                    for service in client.services]
                self.train["status"] = "connected"
                await client.start_notify(characteristic, self.notification)
                self.log("train", "INFO", "Connected to DUPLO Train")
                for prop in (1, 6, 5, 3, 8):
                    await self.write(request_property(prop), f"Request { {1:'name',6:'battery level',5:'signal',3:'firmware',8:'manufacturer'}[prop]}")
                if kind == 0x20:
                    await self.write(subscribe(1, 1), "Activate sound presets")
                ports = PORTS[kind]
                await self.write(subscribe(ports["color"], 0 if kind == 0x21 else 3), "Subscribe to action brick sensor")
                await self.write(subscribe(ports["speed"], 2 if kind == 0x21 else 0), "Subscribe to speed sensor")
                await self.write(motor_packet(kind, 0), "Stop motor on connection")
            except asyncio.CancelledError:
                if client is not None:
                    with suppress(Exception):
                        await client.disconnect()
                self.client = self.characteristic = None
                self.train.update(status="disconnected", power=0)
                raise
            except Exception as error:
                if client is not None:
                    with suppress(Exception):
                        await client.disconnect()
                self.client = self.characteristic = None
                self.train.update(status="error", error=str(error), power=0)
                self.log("train", "ERROR", str(error), level="error")
                raise
            finally:
                self.dirty.set()

    async def disconnect_train(self):
        await self.stop_connecting("train")
        async with self.lifecycle_lock:
            await self.stop("Stopped before disconnecting")
            if self.client is not None:
                with suppress(Exception):
                    await self.client.disconnect()
            self.client = self.characteristic = None
            self.train.update(status="disconnected", battery=None, rssi=None, error=None,
                              measured_speed=None, tag=None, tag_name=None)
            self.input.update(source="manual", neutral_required=True, manual_override=False, driver=None)
            self.log("train", "INFO", "Train disconnected")

    async def power_off(self, reason="Turned off from this app"):
        async with self.lifecycle_lock:
            if self.train["status"] != "connected":
                raise ValueError("Connect the train before turning it off.")
            # Invalidate queued motion before waiting for the Bluetooth write lock.
            self.epoch += 1
            self.owner = None
            self.input.update(source="manual", neutral_required=True, manual_override=False, driver=None)
            self.train.update(status="powering_off", power=0)
            self.dirty.set()
            try:
                async with self.write_lock:
                    for packet, message in ((motor_packet(self.train["hub_type"], 0), "Stop before power off"),
                                            (power_off_packet(), "Power off train")):
                        if not self.demo:
                            if self.client is None or not self.client.is_connected:
                                raise ValueError("The train disconnected before the power-off command.")
                            await asyncio.wait_for(self.client.write_gatt_char(
                                self.characteristic, packet,
                                response="write" in self.characteristic.properties), 4)
                        self.log("train", "TX", message, packet)
                self.train["shutdown_reason"] = reason
                self.log("app", "INFO", f"{reason}. Press the train's green button to wake it.")
            finally:
                if self.client:
                    with suppress(Exception):
                        await self.client.disconnect()
                self.client = self.characteristic = None
                self.train.update(status="disconnected", battery=None, rssi=None, light="off",
                                  measured_speed=None, tag=None, tag_name=None)
                self.dirty.set()

    async def inspect_train(self):
        """Ask for advertised capabilities only; never probe undocumented outputs."""
        if self.demo:
            self.log("app", "INFO", "Capability inspection requires a real Bluetooth train")
            return
        for port in sorted(int(p) for p in self.train["ports"]):
            await self.write(port_info_packet(port), f"Inspect port {port} modes")
        await asyncio.sleep(0.4)
        for port, info in list(self.train["modes"].items()):
            for mode in range(min(info.get("count", 0), 16)):
                await self.write(mode_info_packet(int(port), mode), f"Request port {port} mode {mode} name")
                await self.write(mode_info_packet(int(port), mode, 0x80), f"Request port {port} mode {mode} value format")

    def claim(self, owner):
        if self.owner is not None and self.owner is not owner:
            raise ValueError("Another window is controlling the train. Stop it before switching windows.")
        self.owner = owner
        self.last_heartbeat = time.monotonic()

    def remote_input_fresh(self):
        return (self.remote["status"] == "connected" and self.last_remote_sample > 0
                and 0 <= time.monotonic() - self.last_remote_sample < 0.6)

    def activate_default_input(self, owner):
        """An active browser can resume the enabled stick after fresh connections."""
        computer_driving = self.owner is owner and self.input["driver"] == "manual"
        if (not self.input["left_stick_enabled"] or self.closed or self.stops_pending
                or (self.input["source"] == "dji" and self.owner is not None)
                or (self.owner is not None and not computer_driving) or getattr(owner, "closed", False)
                or self.train["status"] != "connected" or (self.train["power"] and not computer_driving)
                or not self.remote_input_fresh()):
            return
        self.epoch += 1
        self.input.update(source="dji", neutral_required=True, manual_override=computer_driving,
                          driver="manual" if computer_driving else None)
        self.claim(owner)
        self.neutral_after = time.monotonic()
        self.log("input", "INFO", "DJI left stick enabled automatically · center the stick to begin")

    async def drive(self, power, owner, source="manual", remote_revision=None, received_at=None):
        finite_number(power, -100, 100, "Motor power")
        power = round(power)
        if self.stops_pending:
            return
        if source not in ("manual", "dji"):
            raise ValueError("Unknown driving control.")
        if source == "dji" and (self.input["source"] != "dji" or self.input["manual_override"]):
            return
        if source == "dji" and power and self.input["neutral_required"]:
            return
        if source == "dji" and (self.owner is None or self.owner is not owner):
            return
        if power and source == "manual":
            self.claim(owner)
        if source == "manual":
            # Computer input takes priority over every older queued stick command.
            self.epoch += 1
            self.input.update(manual_override=self.input["source"] == "dji", neutral_required=True)
            self.neutral_after = time.monotonic()
        epoch = self.epoch
        packet = motor_packet(self.train["hub_type"], power)
        # The lock serializes output. Recheck stop/source changes after waiting.
        async with self.write_lock:
            if epoch != self.epoch:
                return
            if source == "dji" and (self.input["source"] != "dji" or self.input["manual_override"]
                                    or (power and self.input["neutral_required"])
                                    or (remote_revision is not None and remote_revision != self.remote_revision)
                                    or (received_at is not None and time.monotonic() - received_at >= 0.6)):
                return
            if self.train["status"] != "connected":
                raise ValueError("Connect the train first.")
            if not self.demo:
                if self.client is None or not self.client.is_connected:
                    raise ValueError("Bluetooth disconnected. Reconnect the train.")
                await asyncio.wait_for(self.client.write_gatt_char(
                    self.characteristic, packet,
                    response="write" in self.characteristic.properties), timeout=4)
            self.train["power"] = power
            self.input["driver"] = source if power else None
            self.activity()
            self.log("train", "TX", "Stop motor" if not power else f"{'Forward' if power > 0 else 'Reverse'} · {abs(power)}% power", packet)
        if not power and source == "manual" and self.input["source"] == "manual":
            self.owner = None

    async def stop(self, reason="Stop motor"):
        self.stops_pending += 1
        try:
            return await self._stop(reason)
        finally:
            self.stops_pending -= 1

    async def _stop(self, reason):
        was_moving = bool(self.train["power"])
        self.epoch += 1
        stop_epoch = self.epoch
        self.input.update(neutral_required=True, manual_override=False, driver=None)
        self.neutral_after = time.monotonic()
        self.owner = None
        cancelled = False
        if self.train["status"] == "connected":
            stop_write = asyncio.create_task(self.write(motor_packet(self.train["hub_type"], 0), reason))
            try:
                while not stop_write.done():
                    try:
                        await asyncio.shield(stop_write)
                    except asyncio.CancelledError:
                        # Browser teardown cancels pending actions, but the
                        # physical Stop must finish before teardown returns.
                        cancelled = True
                stop_write.result()
            except Exception as error:
                self.log("train", "ERROR", f"Stop failed: {error}", level="error")
                if self.client is not None:
                    with suppress(Exception):
                        await self.client.disconnect()
        self.train["power"] = 0
        unchanged = self.epoch == stop_epoch
        # Also discard commands submitted while the Stop write was in flight.
        self.epoch += 1
        self.input.update(neutral_required=True, manual_override=False, driver=None)
        self.neutral_after = time.monotonic()
        self.owner = None
        if was_moving:
            self.activity()
        self.dirty.set()
        if cancelled:
            raise asyncio.CancelledError
        return self.epoch if unchanged else None

    async def computer_stop(self, owner, reason="Stop motor"):
        async with self.computer_stop_lock:
            await self._computer_stop(owner, reason)

    async def _computer_stop(self, owner, reason):
        dji_owner = self.owner if self.owner is not None else owner
        keep_dji = self.input["source"] == "dji" and self.remote["status"] in ("connected", "waiting")
        session = self.remote_session
        stopped_epoch = await self.stop(reason)
        self.activity()
        if stopped_epoch is None or self.epoch != stopped_epoch or self.remote_session != session:
            if self.owner is None and self.input["source"] == "dji":
                self.input["source"] = "manual"
            return
        if (keep_dji and self.input["source"] == "dji" and self.remote["status"] in ("connected", "waiting")
                and self.train["status"] == "connected" and not getattr(dji_owner, "closed", False)):
            if dji_owner is owner:
                self.claim(owner)
            else:
                self.owner = dji_owner
            self.neutral_after = time.monotonic()
        else:
            self.input["source"] = "manual"

    async def disable_remote_input(self, reason):
        """Wi-Fi loss must never cancel a command from the computer."""
        if self.input["source"] != "dji":
            return
        self.input.update(source="manual", neutral_required=True, manual_override=False)
        self.remote_revision += 1
        if self.input["driver"] != "manual":
            await self.stop(reason)
        self.log("input", "INFO", reason, level="warning")

    async def pause_remote_input(self):
        """Stop through a temporary Wi-Fi gap, then require fresh neutral input."""
        async with self.computer_stop_lock:
            if self.input["source"] != "dji" or self.remote_paused or self.remote_input_fresh():
                return
            self.remote_paused = True
            self.remote_revision += 1
            self.input["neutral_required"] = True
            self.neutral_after = time.monotonic()
            reason = "DJI input paused: Wi-Fi gap; center the left stick when input returns"
            if self.input["driver"] != "manual":
                owner, session = self.owner, self.remote_session
                stopped_epoch = await self.stop(reason)
                if (stopped_epoch is not None and self.epoch == stopped_epoch
                        and self.remote_session == session and self.input["source"] == "dji"
                        and self.train["status"] == "connected" and owner is not None
                        and not getattr(owner, "closed", False)
                        and time.monotonic() - self.last_heartbeat <= 1.5):
                    # Keep the browser session without refreshing its heartbeat.
                    self.owner = owner
                elif self.owner is None:
                    self.input["source"] = "manual"
            self.log("input", "INFO", reason, level="warning")
            self.dirty.set()

    async def watchdog(self):
        while not self.closed:
            await asyncio.sleep(0.15)
            now = time.monotonic()
            reason = None
            if self.owner is not None and now - self.last_heartbeat > 1.5:
                reason = "Stopped: browser heartbeat lost"
            elif self.input["source"] == "dji" and now - self.last_remote_sample >= 0.6:
                await self.pause_remote_input()
            if self.remote["status"] == "connected" and now - self.last_remote_sample >= 0.6:
                self.remote.update(status="waiting", error="Controller input is stale; Wi-Fi readers are reconnecting.")
                self.dirty.set()
            if reason:
                await self.stop(reason)
                self.input["source"] = "manual"
                self.log("app", "INFO", reason, level="warning")
            remaining = self.idle_remaining()
            if remaining != self.last_countdown:
                self.last_countdown = remaining
                self.dirty.set()
            if remaining == 0:
                try:
                    await self.power_off(f"Turned off after {self.idle_minutes} minute(s) idle")
                except Exception as error:
                    self.log("app", "ERROR", f"Auto power off failed: {error}", level="error")

    async def disconnect_remote(self):
        async with self.remote_lifecycle_lock:
            await self._disconnect_remote()

    async def _disconnect_remote(self):
        self.remote_session += 1
        was_linked = self.remote["status"] not in ("disconnected", "error")
        # Automatic tries call this between attempts; only outside callers cancel them.
        retry = self.connect_tasks["remote"]
        if retry and retry is not asyncio.current_task():
            self.connect_tasks["remote"] = None
            retry.cancel()
            with suppress(asyncio.CancelledError):
                await retry
            # A task cancelled before it started never runs its own cleanup.
            self.attempts["remote"] = None
        if self.remote_search_task:
            search = self.remote_search_task
            self.remote_search_task = None
            if search is not asyncio.current_task():
                search.cancel()
                with suppress(asyncio.CancelledError):
                    await search
        if self.remote_task:
            self.remote_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.remote_task
            self.remote_task = None
        await self.disable_remote_input("DJI input disabled: controller disconnected")
        self.last_remote_sample = 0
        self.remote_revision += 1
        self.remote_sample = None
        self.remote_paused = False
        self.remote_ready.clear()
        self.previous_remote_controls = None
        self.remote.update(status="disconnected", axes={}, buttons={}, axis_raw={}, error=None)
        if was_linked:
            self.log("controller", "INFO", "Controller disconnected")

    async def connect_remote(self, ip):
        if self.demo:
            raise ValueError("DJI hardware connections are disabled in Demo. Open the local controller to connect.")
        address = ipaddress.IPv4Address(ip)
        if not address.is_private or address.is_unspecified or address.is_multicast:
            raise ValueError("Enter the remote's private IPv4 address on your local network.")
        async with self.remote_lifecycle_lock:
            await self._disconnect_remote()
            self.remote.update(status="connecting", ip=str(address), error=None)
            self.log("controller", "INFO", f"Connecting to DJI input at {address}:40007…")
            self.remote_task = asyncio.create_task(self.remote_stream(str(address)))

    async def search_remote(self):
        if self.demo:
            raise ValueError("DJI hardware connections are disabled in Demo. Open the local controller to connect.")
        async with self.remote_lifecycle_lock:
            if self.remote_search_task is not None:
                return
            if self.remote["status"] in ("connecting", "waiting", "connected"):
                raise ValueError("Disconnect the current DJI remote before searching again.")
            await self._disconnect_remote()
            self.remote.update(status="scanning", error=None)
            self.log("controller", "INFO", "Searching this Mac's Wi-Fi network for a DJI remote…")
            self.remote_search_task = asyncio.create_task(self.discover_remote())

    async def find_remote(self):
        """Return the IP of the only DJI stick-input stream on this Mac's Wi-Fi."""
        try:
            network = await asyncio.to_thread(dji_remote.local_network)
        except ValueError as error:
            raise ValueError("Couldn't choose the Wi-Fi network automatically. Connect this Mac to the remote's Wi-Fi network, or enter its IP address below.") from error
        self.log("controller", "INFO", f"Searching {network} for DJI stick input on TCP 40007…")
        candidates = await asyncio.to_thread(dji_remote.scan, network, 40007)
        verified = [item for item in candidates if item.get("controls_verified")]
        if not verified:
            raise ValueError("No DJI remote found. Turn it on, connect it to the same Wi-Fi as this Mac, then try again or enter its IP address below.")
        if len(verified) > 1:
            addresses = ", ".join(item["ip"] for item in verified)
            raise ValueError(f"Several DJI remotes found ({addresses}). Enter the IP address of the one you want below.")
        ip = verified[0]["ip"]
        self.log("controller", "INFO", f"Found DJI stick input at {ip}. Connecting…")
        return ip

    async def discover_remote(self):
        try:
            await self.connect_remote(await self.find_remote())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.remote.update(status="error", error=str(error))
            self.log("controller", "ERROR", str(error), level="error")
        finally:
            if self.remote_search_task is asyncio.current_task():
                self.remote_search_task = None
            self.dirty.set()

    @staticmethod
    def connect_targets(targets):
        if (not isinstance(targets, list) or not targets
                or not all(target in ("train", "remote") for target in targets)):
            raise ValueError("Choose the train, the DJI remote or both.")
        return dict.fromkeys(targets)

    async def auto_connect(self, targets, address=None, ip=None):
        """Start background tries for each chosen device that is not connected yet."""
        targets = self.connect_targets(targets)
        if not isinstance(address, (str, type(None))) or not isinstance(ip, (str, type(None))):
            raise ValueError("Invalid connection details.")
        if "remote" in targets and self.demo:
            raise ValueError("DJI hardware connections are disabled in Demo. Open the local controller to connect.")
        if "train" in targets and self.connect_tasks["train"] is None and self.train["status"] != "connected":
            self.start_tries("train", lambda: self.connect_train(address or None))
        if "remote" in targets and self.connect_tasks["remote"] is None and self.remote["status"] != "connected":
            remembered = ip or self.remote["ip"] or None
            self.start_tries("remote", lambda: self.remote_attempt(remembered))

    def start_tries(self, device, attempt):
        self.attempts[device] = {"try": 1, "of": self.connect_tries}
        task = asyncio.create_task(self.tries(device, attempt))
        self.connect_tasks[device] = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def tries(self, device, attempt):
        name = {"train": "the train", "remote": "the DJI remote"}[device]
        try:
            for number in range(1, self.connect_tries + 1):
                self.attempts[device] = {"try": number, "of": self.connect_tries}
                self.dirty.set()
                try:
                    await attempt()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Each attempt has already logged and shown its own error.
                    if number == self.connect_tries:
                        self.log("app", "ERROR", f"Couldn't connect {name} after {number} tries.", level="error")
                    else:
                        self.log("app", "INFO", f"Try {number} of {self.connect_tries} for {name} failed. Trying again…",
                                 level="warning")
                        await asyncio.sleep(self.retry_delay)
        finally:
            self.attempts[device] = None
            if self.connect_tasks[device] is asyncio.current_task():
                self.connect_tasks[device] = None
            self.dirty.set()

    async def remote_attempt(self, ip):
        """One try: the remembered IP first, then a search of this Mac's Wi-Fi."""
        try:
            if ip:
                try:
                    await self.connect_remote(ip)
                    await self.wait_for_remote_input()
                    return
                except ValueError as error:
                    self.log("controller", "INFO", f"{error} Searching Wi-Fi instead…", level="warning")
            async with self.remote_lifecycle_lock:
                await self._disconnect_remote()
                self.remote.update(status="scanning", error=None)
            await self.connect_remote(await self.find_remote())
            await self.wait_for_remote_input()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            async with self.remote_lifecycle_lock:
                await self._disconnect_remote()
            self.remote.update(status="error", error=str(error))
            self.log("controller", "ERROR", str(error), level="error")
            raise

    async def wait_for_remote_input(self):
        """Wait until the reader started by connect_remote receives stick input."""
        deadline = time.monotonic() + self.remote_input_timeout
        while not self.remote_input_fresh():
            if self.remote["status"] == "error":
                raise ValueError(self.remote["error"] or "The DJI remote connection failed.")
            if time.monotonic() >= deadline:
                raise ValueError(f"No stick input from {self.remote['ip']}.")
            await asyncio.sleep(0.1)

    async def stop_connecting(self, device):
        """Cancel automatic tries. A device that already connected stays connected."""
        task = self.connect_tasks[device]
        if task is None:
            return
        if device == "remote":
            await self.disconnect_remote()
        else:
            self.connect_tasks["train"] = None
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self.attempts["train"] = None
            if self.train["status"] == "error":
                self.train.update(status="disconnected", error=None)
        self.log("app", "INFO", f"Stopped connecting {'the train' if device == 'train' else 'the DJI remote'}")

    def publish_remote_sample(self, controls, received_at, packet=None):
        """Overwrite the mailbox without waiting for Bluetooth output."""
        now = time.monotonic()
        if received_at < self.last_remote_sample or not 0 <= now - received_at < 0.6:
            return False
        # A missing axis must never be interpreted as a centered stick.
        axis = controls.get("axes", {}).get("left_y")
        if isinstance(axis, bool) or not isinstance(axis, (int, float)) or not math.isfinite(axis):
            return False
        first = self.previous_remote_controls is None
        self.last_remote_sample = received_at
        self.remote_paused = False
        self.remote_revision += 1
        self.remote_sample = (self.remote_revision, received_at, controls)
        self.remote.update(status="connected", axes=controls["axes"],
                           buttons=controls.get("buttons", {}), axis_raw=controls.get("axis_raw", {}),
                           mode=controls.get("mode"), error=None)
        if first:
            self.log("controller", "INFO", "DJI Wi-Fi controls received. Left stick controls forward and reverse; other actions stay on the computer.")
        elif controls != self.previous_remote_controls and now - self.last_remote_log >= 0.1:
            self.log("controller", "RX", f"Controller input · {controls.get('mode', 'unknown')} mode", packet)
            self.last_remote_log = now
        self.previous_remote_controls = controls
        self.remote_ready.set()
        self.dirty.set()
        return True

    def left_stick_drive(self, controls):
        """Map only left-stick vertical travel to signed motor power."""
        axis = controls.get("axes", {}).get("left_y")
        if isinstance(axis, bool) or not isinstance(axis, (int, float)) or not math.isfinite(axis):
            return None
        position = max(-1, min(1, axis - self.input["center"]))
        if self.input["invert"]:
            position = -position
        deadzone = self.input["deadzone"]
        magnitude = min(1, max(0, (abs(position) - deadzone) / (1 - deadzone)))
        power = round(math.copysign(magnitude * self.input["max_power"], position))
        return {"position": round(position, 4), "power": power}

    async def apply_remote_sample(self, revision, received_at, controls):
        if (revision != self.remote_revision or time.monotonic() - received_at >= 0.6
                or self.input["source"] != "dji" or self.owner is None):
            return
        left_stick = self.left_stick_drive(controls)
        if left_stick is None:
            return
        if abs(left_stick["position"]) <= self.input["deadzone"]:
            if self.input["neutral_required"] and received_at <= self.neutral_after:
                return
            self.input.update(neutral_required=False, manual_override=False)
            # Center hands control back without canceling computer movement.
            if self.train["power"] and self.input["driver"] == "dji":
                await self.drive(0, self.owner, "dji", revision, received_at)
        elif not self.input["neutral_required"] and not self.input["manual_override"]:
            desired = left_stick["power"]
            if desired != self.train["power"] or self.input["driver"] != "dji":
                await self.drive(desired, self.owner, "dji", revision, received_at)
        self.dirty.set()

    async def consume_remote_samples(self):
        while True:
            await self.remote_ready.wait()
            self.remote_ready.clear()
            if self.remote_sample is not None:
                await self.apply_remote_sample(*self.remote_sample)

    async def remote_reader(self, ip, index):
        await asyncio.sleep(index * 0.5)
        while True:
            writer, had_controls = None, False
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, 40007), 0.3)
                if self.remote["status"] == "connecting":
                    self.remote["status"] = "waiting"
                    self.dirty.set()
                parser = dji_remote.PacketParser()
                last_valid = time.monotonic()
                while True:
                    try:
                        chunk = await asyncio.wait_for(reader.read(8192), 0.05)
                    except TimeoutError:
                        chunk = None
                    received_at = time.monotonic()
                    if chunk == b"":
                        raise ConnectionError("The controller closed the connection.")
                    for packet in parser.feed(chunk) if chunk else []:
                        controls = dji_remote.decode_controls(packet)
                        if controls is not None:
                            had_controls, last_valid = True, received_at
                            self.publish_remote_sample(controls, received_at, packet)
                    if received_at - last_valid > 5:
                        raise ConnectionError("No valid controller frames for 5 seconds.")
            except asyncio.CancelledError:
                raise
            except PermissionError as error:
                raise PermissionError("Local network access denied. Allow network access for the app.") from error
            except (OSError, TimeoutError):
                # RM330 closes links frequently; every reader recovers independently.
                pass
            finally:
                if writer is not None:
                    writer.close()
                    with suppress(Exception):
                        await asyncio.wait_for(writer.wait_closed(), 0.3)
            await asyncio.sleep(0.1 if had_controls else 0.35)

    async def remote_stream(self, ip):
        workers = [asyncio.create_task(self.remote_reader(ip, index), name=f"dji-reader-{index + 1}")
                   for index in range(5)]
        workers.append(asyncio.create_task(self.consume_remote_samples(), name="dji-latest-input"))
        self.log("controller", "INFO", "Listening through five staggered, receive-only Wi-Fi connections.")
        error = None
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            raise
        except Exception as caught:
            error = caught
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            self.dirty.set()
        if error is not None:
            self.remote.update(status="error", error=str(error), axes={}, buttons={}, axis_raw={})
            await self.disable_remote_input("DJI input disabled: controller connection lost")
            self.log("controller", "ERROR", str(error), level="error")

    async def action(self, action, args, owner):
        if action == "connect":
            await self.connect_train(args.get("address") or None)
        elif action == "disconnect":
            await self.disconnect_train()
        elif action == "drive":
            await self.drive(args.get("power"), owner)
        elif action == "stop":
            if "disarm" in args and not isinstance(args["disarm"], bool):
                raise ValueError("Disarm must be true or false.")
            if args.get("disarm"):
                self.input["source"] = "manual"
            await self.computer_stop(owner)
        elif action == "suspend":
            if self.owner is owner:
                self.input["source"] = "manual"
                await self.stop("Stopped: control window inactive")
        elif action == "light":
            color = args.get("color")
            await self.write(light_packet(self.train["hub_type"], color), f"Set headlight: {color}")
            self.train["light"] = color
            self.activity()
        elif action == "sound":
            sound = args.get("sound", "horn")
            await self.write(sound_packet(self.train["hub_type"], sound), f"Play {sound}")
            self.activity()
        elif action == "power_off":
            await self.power_off()
        elif action == "train_settings":
            minutes = args.get("idle_minutes")
            if type(minutes) is not int or minutes not in (0, 1, 5, 10, 15, 30):
                raise ValueError("Choose a supported idle time.")
            self.save_settings(idle_minutes=minutes)
            self.idle_minutes = minutes
            self.activity()
            self.log("app", "INFO", "App auto power off: disabled" if not minutes else f"App auto power off: {minutes} minute(s) idle")
        elif action == "inspect":
            await self.inspect_train()
        elif action == "audio_event":
            messages = {"loaded": "Purple clip ready on this computer",
                        "armed": "Purple brick sound armed · playback on this computer",
                        "disarmed": "Purple brick sound disarmed", "preview": "Preview purple clip on this computer",
                        "played": "Purple brick detected · playing clip on this computer"}
            event = args.get("event")
            if event not in messages:
                raise ValueError("Unknown audio event.")
            self.log("app", "INFO", messages[event])
        elif action == "demo_purple":
            if not self.demo:
                raise ValueError("Brick simulation is available only in Demo.")
            self.notification(None, bytes.fromhex("06 00 45 33 44 01"))
        elif action == "source":
            source = args.get("source")
            if source not in ("manual", "dji"):
                raise ValueError("Choose Keyboard & buttons or DJI remote.")
            if self.owner is not None and self.owner is not owner:
                raise ValueError("Stop the active control window before switching input.")
            if source == "dji" and (not self.remote_input_fresh() or self.train["status"] != "connected"):
                raise ValueError("Connect the train and receive valid controller input first.")
            self.save_settings(left_stick_enabled=source == "dji")
            self.input["left_stick_enabled"] = source == "dji"
            await self.stop("Stopped before switching input")
            self.input["source"] = source
            if source == "dji":
                self.claim(owner)
                self.neutral_after = time.monotonic()
            self.log("input", "INFO", f"Driving controls: {'Computer + DJI left stick · center the stick to begin' if source == 'dji' else 'Computer only'}")
        elif action == "configure":
            config = dict(self.input)
            if "axis" in args and args["axis"] != "left_y":
                raise ValueError("Only the DJI left stick's vertical axis controls the train.")
            if "stop_button" in args or "horn_button" in args:
                raise ValueError("DJI buttons do not control train actions. Use the computer controls.")
            if "deadzone" in args:
                config["deadzone"] = finite_number(args["deadzone"], 0.02, 0.4, "Dead zone")
            if "max_power" in args:
                config["max_power"] = round(finite_number(args["max_power"], 0, 100, "Speed setting"))
            if "invert" in args:
                if not isinstance(args["invert"], bool):
                    raise ValueError("Invert must be true or false.")
                config["invert"] = args["invert"]
                self.save_settings(invert=config["invert"])
            if self.input["source"] == "dji":
                await self.computer_stop(owner, "Stopped before updating controller settings")
            config.update(source=self.input["source"], neutral_required=True,
                          left_stick_enabled=self.input["left_stick_enabled"],
                          manual_override=self.input["manual_override"], driver=self.input["driver"])
            self.neutral_after = time.monotonic()
            self.input = config
        elif action == "calibrate":
            if not self.remote_input_fresh():
                raise ValueError("Connect the controller first.")
            await self.computer_stop(owner, "Stopped for controller calibration")
            self.input.update(center=self.remote["axes"]["left_y"])
            self.log("input", "INFO", "Centered controller axis")
        elif action == "remote_connect":
            await self.connect_remote(args.get("ip", ""))
        elif action == "remote_search":
            await self.search_remote()
        elif action == "remote_disconnect":
            await self.disconnect_remote()
        elif action == "auto_connect":
            await self.auto_connect(args.get("targets"), args.get("address"), args.get("ip"))
        elif action == "stop_connecting":
            for device in self.connect_targets(args.get("targets")):
                await self.stop_connecting(device)
        else:
            raise ValueError("Unknown action.")
        self.dirty.set()

    async def close(self):
        self.closed = True
        if self.remote_task or self.remote_search_task or self.connect_tasks["remote"]:
            await self.disconnect_remote()
        await self.disconnect_train()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


CONTROLLER_KEY = web.AppKey("controller", Controller)


def create_app(demo=False, settings_path=None):
    controller = Controller(demo, settings_path)

    @web.middleware
    async def local_only(request, handler):
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise web.HTTPForbidden(text="This is a local controller.")
        return await handler(request)

    app = web.Application(middlewares=[local_only])
    app[CONTROLLER_KEY] = controller

    async def state(request):
        return web.json_response({"state": controller.snapshot(), "logs": list(controller.logs)})

    async def logs(request):
        body = "\n".join(json.dumps(event) for event in controller.logs) + "\n"
        return web.Response(text=body, content_type="application/x-ndjson",
                            headers={"Content-Disposition": 'attachment; filename="duplo-debug.ndjson"'})

    async def socket(request):
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=16384)
        await ws.prepare(request)
        controller.clients.add(ws)
        await ws.send_json({"type": "snapshot", "state": controller.snapshot(), "logs": list(controller.logs)})
        pending = set()

        async def dispatch(message):
            request_id = message.get("id")
            try:
                args = message.get("args") or {}
                if not isinstance(args, dict):
                    raise ValueError("Invalid action arguments.")
                await controller.action(message.get("action"), args, ws)
                if not ws.closed:
                    await ws.send_json({"type": "result", "id": request_id, "ok": True})
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if controller.owner is ws:
                    await controller.stop("Stopped after command failure")
                    controller.input["source"] = "manual"
                controller.log("app", "ERROR", str(error), level="error")
                if not ws.closed:
                    await ws.send_json({"type": "result", "id": request_id, "ok": False, "error": str(error)})

        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                    if not isinstance(data, dict):
                        raise ValueError("Expected an action object.")
                    if data.get("type") == "heartbeat":
                        if data.get("active") is True:
                            controller.activate_default_input(ws)
                        if controller.owner is ws and data.get("active") is not False:
                            controller.last_heartbeat = time.monotonic()
                        continue
                    if len(pending) >= 20:
                        raise ValueError("Too many pending commands.")
                    task = asyncio.create_task(dispatch(data))
                    pending.add(task)
                    controller.tasks.add(task)
                    task.add_done_callback(pending.discard)
                    task.add_done_callback(controller.tasks.discard)
                except (ValueError, TypeError) as error:
                    await ws.send_json({"type": "result", "ok": False, "error": str(error)})
        finally:
            controller.clients.discard(ws)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if controller.owner is ws:
                await controller.stop("Stopped: browser window closed")
                controller.input["source"] = "manual"
        return ws

    async def index(request):
        if not (STATIC / "index.html").exists():
            return web.Response(status=503, text="Build the interface first: cd ui && npm run build")
        return web.FileResponse(STATIC / "index.html")

    async def startup(_app):
        for coroutine in (controller.updates(), controller.watchdog()):
            task = asyncio.create_task(coroutine)
            controller.tasks.add(task)
            task.add_done_callback(controller.tasks.discard)

    async def cleanup(_app):
        await controller.close()

    async def shutdown(_app):
        await controller.stop("Stopped: local server shutting down")
        for ws in list(controller.clients):
            await ws.close(code=1001, message=b"Local controller shutting down")

    app.router.add_get("/api/state", state)
    app.router.add_get("/api/logs", logs)
    app.router.add_get("/api/ws", socket)
    app.router.add_get("/", index)
    if (STATIC / "assets").exists():
        app.router.add_static("/assets/", STATIC / "assets")
    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)
    app.on_cleanup.append(cleanup)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--demo", action="store_true", help="Simulated train, no Bluetooth access")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535.")
    web.run_app(create_app(args.demo, None if args.demo else ROOT / ".duplo-settings.json"),
                host="127.0.0.1", port=args.port, access_log=None)
