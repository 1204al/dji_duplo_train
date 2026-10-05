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
        if self.settings_path and self.settings_path.exists():
            try:
                value = json.loads(self.settings_path.read_text())["idle_minutes"]
                if type(value) is int and value in (0, 1, 5, 10, 15, 30):
                    self.idle_minutes = value
            except (ValueError, OSError, KeyError, TypeError):
                pass
        self.last_activity = time.monotonic()
        self.last_countdown = None
        self.remote = {"status": "disconnected", "ip": "", "axes": {}, "buttons": {},
                       "axis_raw": {}, "mode": None, "error": None}
        self.input = {"source": "manual", "axis": "left_y", "invert": False,
                      "deadzone": 0.08, "center": 0, "max_power": 35,
                      "stop_button": "rth_pause", "horn_button": "c1",
                      "neutral_required": True}
        self.logs = deque(maxlen=1000)
        self.next_id = 1
        self.client = None
        self.characteristic = None
        self.clients = set()
        self.owner = None
        self.last_heartbeat = 0
        self.last_remote_sample = 0
        self.write_lock = asyncio.Lock()
        self.lifecycle_lock = asyncio.Lock()
        self.remote_task = None
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
        return {"train": dict(self.train), "controller": dict(self.remote),
                "input": dict(self.input), "demo": self.demo,
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
        self.input.update(source="manual", neutral_required=True)
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
        async with self.lifecycle_lock:
            await self.stop("Stopped before disconnecting")
            if self.client is not None:
                with suppress(Exception):
                    await self.client.disconnect()
            self.client = self.characteristic = None
            self.train.update(status="disconnected", battery=None, rssi=None, error=None,
                              measured_speed=None, tag=None, tag_name=None)
            self.input.update(source="manual", neutral_required=True)
            self.log("train", "INFO", "Train disconnected")

    async def power_off(self, reason="Turned off from this app"):
        async with self.lifecycle_lock:
            if self.train["status"] != "connected":
                raise ValueError("Connect the train before turning it off.")
            # Invalidate queued motion before waiting for the Bluetooth write lock.
            self.epoch += 1
            self.owner = None
            self.input.update(source="manual", neutral_required=True)
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

    async def drive(self, power, owner, source="manual"):
        finite_number(power, -100, 100, "Motor power")
        power = round(power)
        if source != self.input["source"]:
            raise ValueError("Change the input source before driving from this control.")
        if power:
            self.claim(owner)
        epoch = self.epoch
        packet = motor_packet(self.train["hub_type"], power)
        # The lock serializes output. Recheck stop/source changes after waiting.
        async with self.write_lock:
            if epoch != self.epoch:
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
            self.activity()
            self.log("train", "TX", "Stop motor" if not power else f"{'Forward' if power > 0 else 'Reverse'} · {abs(power)}% power", packet)
        if not power and source == "manual":
            self.owner = None

    async def stop(self, reason="Stop motor"):
        was_moving = bool(self.train["power"])
        self.epoch += 1
        self.input["neutral_required"] = True
        if self.train["status"] == "connected":
            try:
                await self.write(motor_packet(self.train["hub_type"], 0), reason)
            except Exception as error:
                self.log("train", "ERROR", f"Stop failed: {error}", level="error")
                if self.client is not None:
                    with suppress(Exception):
                        await self.client.disconnect()
        self.train["power"] = 0
        if was_moving:
            self.activity()
        self.owner = None
        self.dirty.set()

    async def watchdog(self):
        while not self.closed:
            await asyncio.sleep(0.15)
            now = time.monotonic()
            reason = None
            if self.owner is not None and now - self.last_heartbeat > 1.5:
                reason = "Stopped: browser heartbeat lost"
            elif self.train["power"] and self.input["source"] == "dji" and now - self.last_remote_sample > 0.6:
                reason = "Stopped: controller input lost"
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
        if self.remote_task:
            self.remote_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.remote_task
            self.remote_task = None
        if self.input["source"] == "dji":
            await self.stop("Stopped: controller disconnected")
            self.input["source"] = "manual"
        self.remote.update(status="disconnected", axes={}, buttons={}, axis_raw={}, error=None)
        self.log("controller", "INFO", "Controller disconnected")

    async def connect_remote(self, ip):
        if self.demo:
            raise ValueError("DJI hardware connections are disabled in Demo. Open the local controller to connect.")
        address = ipaddress.IPv4Address(ip)
        if not address.is_private or address.is_unspecified or address.is_multicast:
            raise ValueError("Enter the remote's private IPv4 address on your local network.")
        await self.disconnect_remote()
        self.remote.update(status="connecting", ip=str(address), error=None)
        self.log("controller", "INFO", f"Connecting to DJI input at {address}:40007…")
        self.remote_task = asyncio.create_task(self.remote_stream(str(address)))

    async def remote_stream(self, ip):
        writer = None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, 40007), 4)
            self.remote["status"] = "waiting"
            self.log("controller", "INFO", "TCP connected. Waiting for valid DJI stick/button frames.")
            parser, previous, sequence = dji_remote.PacketParser(), None, 1
            next_query, last_valid, last_log = 0, time.monotonic(), 0
            while True:
                now = time.monotonic()
                if now >= next_query:
                    packet = dji_remote.version_query(sequence)
                    writer.write(packet)
                    await writer.drain()
                    sequence = (sequence + 1) & 0xFFFF
                    next_query = now + 0.5
                try:
                    chunk = await asyncio.wait_for(reader.read(8192), 0.2)
                except TimeoutError:
                    chunk = None
                if chunk == b"":
                    raise ConnectionError("The controller closed the connection.")
                for packet in parser.feed(chunk) if chunk else []:
                    controls = dji_remote.decode_controls(packet)
                    if controls is None:
                        continue
                    last_valid = self.last_remote_sample = time.monotonic()
                    self.remote.update(status="connected", axes=controls["axes"],
                                       buttons=controls["buttons"], axis_raw=controls["axis_raw"],
                                       mode=controls["mode"], error=None)
                    if previous is None:
                        self.input["neutral_required"] = True
                        self.log("controller", "INFO", "DJI controls received. Verify axes and buttons before choosing DJI input.")
                    elif controls != previous and now - last_log >= 0.1:
                        self.log("controller", "RX", f"Controller input · {controls['mode']} mode", packet)
                        last_log = now
                    if self.input["source"] == "dji" and self.owner is not None:
                        axis = controls["axes"].get(self.input["axis"], 0) - self.input["center"]
                        if self.input["invert"]:
                            axis = -axis
                        deadzone = self.input["deadzone"]
                        stop_pressed = controls["buttons"].get(self.input["stop_button"], False)
                        if stop_pressed:
                            await self.stop("Stopped by controller button")
                            self.input["source"] = "manual"
                        elif abs(axis) <= deadzone:
                            self.input["neutral_required"] = False
                            if self.train["power"]:
                                await self.drive(0, self.owner, "dji")
                        elif not self.input["neutral_required"]:
                            magnitude = min(1, max(0, (abs(axis) - deadzone) / (1 - deadzone)))
                            desired = round(math.copysign(magnitude * self.input["max_power"], axis))
                            if desired != self.train["power"]:
                                await self.drive(desired, self.owner, "dji")
                        horn_button = self.input["horn_button"]
                        if previous and controls["buttons"].get(horn_button) and not previous["buttons"].get(horn_button):
                            await self.write(sound_packet(self.train["hub_type"], "horn"), "Play train horn")
                    previous = controls
                    self.dirty.set()
                if time.monotonic() - last_valid > 5:
                    raise ConnectionError("No valid controller frames. Check the model, IP address and Wi-Fi input service.")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.remote.update(status="error", error=str(error), axes={}, buttons={}, axis_raw={})
            if self.input["source"] == "dji":
                await self.stop("Stopped: controller connection lost")
                self.input["source"] = "manual"
            self.log("controller", "ERROR", str(error), level="error")
        finally:
            if writer:
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
            self.dirty.set()

    async def action(self, action, args, owner):
        if action == "connect":
            await self.connect_train(args.get("address") or None)
        elif action == "disconnect":
            await self.disconnect_train()
        elif action == "drive":
            await self.drive(args.get("power"), owner)
        elif action == "stop":
            await self.stop()
            self.activity()
            if self.input["source"] == "dji":
                self.input["source"] = "manual"
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
            if self.settings_path:
                temporary = self.settings_path.with_suffix(".tmp")
                temporary.write_text(json.dumps({"idle_minutes": minutes}) + "\n")
                temporary.replace(self.settings_path)
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
            if source == "dji" and (self.remote["status"] != "connected" or self.train["status"] != "connected"):
                raise ValueError("Connect the train and receive valid controller input first.")
            await self.stop("Stopped before switching input")
            self.input["source"] = source
            if source == "dji":
                self.claim(owner)
            self.log("input", "INFO", f"Input source: {'DJI remote · center the stick to begin' if source == 'dji' else 'Keyboard & buttons'}")
        elif action == "configure":
            config = dict(self.input)
            for key in ("axis", "stop_button", "horn_button"):
                allowed = dji_remote.AXES if key == "axis" else dji_remote.BUTTONS
                if key in args:
                    if args[key] not in allowed:
                        raise ValueError(f"Unknown {key}.")
                    config[key] = args[key]
            if "deadzone" in args:
                config["deadzone"] = finite_number(args["deadzone"], 0.02, 0.4, "Dead zone")
            if "max_power" in args:
                config["max_power"] = round(finite_number(args["max_power"], 0, 100, "Speed setting"))
            if "invert" in args:
                if not isinstance(args["invert"], bool):
                    raise ValueError("Invert must be true or false.")
                config["invert"] = args["invert"]
            if self.input["source"] == "dji":
                await self.stop("Stopped before updating controller settings")
                self.input["source"] = "manual"
            config.update(source=self.input["source"], neutral_required=True)
            self.input = config
        elif action == "calibrate":
            if self.remote["status"] != "connected":
                raise ValueError("Connect the controller first.")
            await self.stop("Stopped for controller calibration")
            self.input.update(source="manual", center=self.remote["axes"].get(self.input["axis"], 0))
            self.log("input", "INFO", "Centered controller axis")
        elif action == "remote_connect":
            await self.connect_remote(args.get("ip", ""))
        elif action == "remote_disconnect":
            await self.disconnect_remote()
        else:
            raise ValueError("Unknown action.")
        self.dirty.set()

    async def close(self):
        self.closed = True
        if self.remote_task:
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
                        if controller.owner is ws:
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
