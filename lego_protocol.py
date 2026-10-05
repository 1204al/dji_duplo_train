"""Small LWP3 codec for both generations of DUPLO train.

The 10427/10428 (hub 0x21) use different ports and effect commands.
References: LEGO LWP3 docs and Pybricks' DuploTrain implementation.
"""

import struct

COLORS = {"off": (0, 0), "white": (10, 1), "red": (9, 12),
          "green": (6, 7), "blue": (3, 15), "purple": (2, 10), "yellow": (7, 8)}
SOUNDS = {"horn": 9, "brake": 3, "depart": 5, "water": 7, "steam": 10}
INTERACTIVE_SOUNDS = {"horn": 7, "starting": 6}
TAGS = {1: "White", 21: "Red", 24: "Yellow", 28: "Green", 119: "Leaf",
        321: "Blue", 324: "Purple · star", 353: "Home"}
PORTS = {0x20: {"motor": 0, "light": 17, "sound": 1, "color": 18, "speed": 19},
         0x21: {"motor": 50, "effects": 52, "color": 51, "speed": 54}}
PROPERTY_NAMES = {1: "Name", 3: "Firmware", 4: "Hardware", 5: "Signal",
                  6: "Battery level", 8: "Manufacturer", 11: "Hub type"}


def motor_packet(hub_type, power):
    if isinstance(power, bool) or not isinstance(power, int) or not -100 <= power <= 100:
        raise ValueError("Motor power must be an integer between -100 and 100.")
    return bytes([8, 0, 0x81, PORTS[hub_type]["motor"], 0x11, 0x51, 0, power & 0xFF])


def light_packet(hub_type, color):
    if color not in COLORS:
        raise ValueError("Choose a supported headlight color.")
    old, new = COLORS[color]
    if hub_type == 0x21:
        return bytes([10, 0, 0x81, 52, 0x11, 0x51, 1, 4, 1, new])
    return bytes([8, 0, 0x81, 17, 0x11, 0x51, 0, old])


def sound_packet(hub_type, sound):
    if hub_type == 0x21:
        if sound not in INTERACTIVE_SOUNDS:
            raise ValueError("This sound is not supported by this train.")
        # Check Device's 10427/10428 implementation uses events 0x0106/0x0107.
        return bytes([9, 0, 0x81, 52, 0x11, 0x51, 1, INTERACTIVE_SOUNDS[sound], 1])
    if sound not in SOUNDS:
        raise ValueError("This sound is not supported by this train.")
    return bytes([8, 0, 0x81, 1, 0x11, 0x51, 1, SOUNDS[sound]])


def request_property(property_id):
    return bytes([5, 0, 1, property_id, 5])


def power_off_packet():
    """Standard LWP3 Hub Actions / Switch Off Hub, not a firmware command."""
    return bytes([4, 0, 2, 1])


def port_info_packet(port):
    return bytes([5, 0, 0x21, port, 1])


def mode_info_packet(port, mode, info_type=0):
    return bytes([6, 0, 0x22, port, mode, info_type])  # Read-only.


def subscribe(port, mode):
    return bytes([10, 0, 0x41, port, mode, 1, 0, 0, 0, 1])


def decode_packet(data, hub_type=0x21):
    """Return a readable log message and state updates; retain unknown packets."""
    if len(data) < 3 or data[0] != len(data):
        return "Malformed LEGO message", {}
    kind = data[2]
    if kind == 1 and len(data) >= 5:
        prop, operation, payload = data[3], data[4], data[5:]
        name = PROPERTY_NAMES.get(prop, f"Property {prop}")
        if operation == 5:
            return f"Request {name.lower()}", {}
        if operation != 6:
            return f"{name}: operation {operation}", {}
        if prop in (1, 8):
            value = payload.decode("utf-8", errors="replace").strip()
            return f"{name}: {value}", {"name" if prop == 1 else "manufacturer": value}
        if prop == 6 and payload:
            return f"Battery level: {payload[0]}%", {"battery": payload[0]}
        if prop == 5 and payload:
            value = struct.unpack("b", payload[:1])[0]
            return f"Signal: {value} dBm", {"rssi": value}
        if prop == 3 and len(payload) == 4:
            value = f"{payload[3] >> 4}.{payload[3] & 15}.{payload[2]:02x}.{payload[1]:02x}{payload[0]:02x}"
            return f"Firmware: {value}", {"firmware": value}
        return f"{name}: {payload.hex(' ')}", {}
    if kind == 4 and len(data) >= 5:
        port, event = data[3], data[4]
        if event == 0:
            return f"Port {port} detached", {"port": (port, None)}
        if len(data) < 7:
            return "Malformed port attachment", {}
        device_type = int.from_bytes(data[5:7], "little")
        return f"Port {port} attached · device 0x{device_type:04x}", {"port": (port, device_type)}
    if kind == 5 and len(data) >= 5:
        return f"Train error 0x{data[4]:02x} for message 0x{data[3]:02x}", {"protocol_error": True}
    if kind == 0x45 and len(data) >= 5:
        port, payload = data[3], data[4:]
        ports = PORTS[hub_type]
        if port == ports["speed"] and len(payload) == 2:
            value = int.from_bytes(payload, "little", signed=True)
            return f"Speed sensor: {value}", {"measured_speed": value}
        if port == ports["color"] and hub_type == 0x21 and len(payload) == 2:
            tag = int.from_bytes(payload, "little")
            return f"Action brick: {TAGS.get(tag, 'Unknown')} ({tag})", {"tag": tag, "tag_name": TAGS.get(tag, "Unknown")}
        if port == ports["color"] and len(payload) == 6:
            return f"Color RGB: {struct.unpack('<HHH', payload)}", {"color_rgb": list(struct.unpack('<HHH', payload))}
        return f"Port {port} value: {payload.hex(' ')}", {}
    if kind == 0x82:
        values = [f"port {data[i]} status 0x{data[i + 1]:02x}" for i in range(3, len(data) - 1, 2)]
        return "Command feedback: " + ", ".join(values), {}
    if kind == 0x47 and len(data) >= 5:
        return f"Sensor subscription · port {data[3]}, mode {data[4]}", {}
    if kind == 0x43 and len(data) >= 11 and data[4] == 1:
        port, capabilities, count = data[3], data[5], data[6]
        inputs, outputs = struct.unpack('<HH', data[7:11])
        info = {"count": count, "inputs": inputs, "outputs": outputs,
                "capabilities": capabilities, "names": {}}
        return f"Port {port}: {count} modes · inputs 0x{inputs:04x}, outputs 0x{outputs:04x}", {"port_modes": (port, info)}
    if kind == 0x44 and len(data) >= 6 and data[5] == 0:
        name = data[6:].rstrip(b'\x00').decode('utf-8', errors='replace')
        return f"Port {data[3]} · mode {data[4]}: {name}", {"mode_name": (data[3], data[4], name)}
    if kind == 0x44 and len(data) >= 10 and data[5] == 0x80:
        count, value_type, figures, decimals = data[6:10]
        info = {"datasets": count, "type": value_type, "figures": figures, "decimals": decimals}
        type_name = {0: "8-bit", 1: "16-bit", 2: "32-bit", 3: "float"}.get(value_type, f"type {value_type}")
        return f"Port {data[3]} · mode {data[4]} format: {count} × {type_name}", {"mode_format": (data[3], data[4], info)}
    if kind == 2 and len(data) >= 4:
        names = {1: "Power off requested", 2: "Disconnect requested",
                 0x30: "Train is powering off", 0x31: "Train is disconnecting"}
        return names.get(data[3], f"Hub action: 0x{data[3]:02x}"), {"hub_action": data[3]}
    return f"LEGO message 0x{kind:02x}", {}
