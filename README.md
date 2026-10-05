# DUPLO Control

A local web app for controlling your DUPLO 10427 from this Mac. The dark, single-screen interface has train controls on the left and a collapsible Debug panel on the right. Debug opens visible by default. Bluetooth and the DJI connection run in a small local Python server, so browser Bluetooth support is not required.

## Run the app

One-time setup:

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
cd ui
npm ci
npm run build
cd ..
```

Start the local controller:

```sh
.venv/bin/python app_server.py
```

Open [DUPLO Control](http://127.0.0.1:8100). Switch the train on nearby, close the LEGO phone app, then click **Connect train**. The server listens only on this Mac. Stop it with **Ctrl+C**.

- Forward, backward, speed setting, Stop, and optional hold-to-drive.
- Arrow keys drive; Space or Escape stops. Moving away from the app stops the train.
- Six headlight colors, Off, and direct Train horn / Starting sound buttons. The new Starting sound command still needs a hardware listening check. Earlier 10874/10875 trains expose their own five presets as buttons.
- Live battery, connection status, signal, raw speed sensor and last action brick.
- Debug filters, pause/resume, local clear, NDJSON export, selected packet inspection and copyable connection details.
- Inline DJI settings with drive-axis choice, inversion, dead zone, center calibration and stop/horn button mapping.
- Inline **Purple brick sound**: microphone recording, audio-file import, five-second limit, local preview, and optional playback on the Mac when the purple star brick is detected.
- Inline **Power & sleep**: turn the train off now, or send power off after 1/5/10/15/30 minutes idle. Disabled by default; the choice persists locally.

The speed slider sets motor power, not a calibrated physical speed. Action-brick reporting is the last observed tag. Bluetooth writes and command feedback appear in Debug; physical motion is not inferred from a successful write.

The controller stops on browser disconnection or a missing browser heartbeat (about 1.5 seconds). DJI driving also stops when controller input is stale (about 0.6 seconds), a mapped stop button is pressed, or the connection fails. Stop cancels queued movement commands. Selecting DJI input requires valid controller frames, then a centered stick before motion begins. A second browser window cannot take over an active drive session without stopping it.

## Purple sound and sleep

LEGO confirms that its official app can transfer a clip of up to five seconds for the purple brick. This controller currently plays your recording **through the Mac**, with a clear **Audio on Mac** label. Sending audio to the train speaker is not implemented: the public protocol and the inspected implementations do not establish the required encoding and transfer sequence. Use LEGO's app for that native feature, then close it before reconnecting here. See [the research record](docs/train-features-research.md) for sources and supported commands.

Click **Record sound** to request microphone access; recording finishes automatically after five seconds. **Choose file** accepts browser-decodable audio and keeps its first five seconds. The clip is stored in this browser's local IndexedDB, and **Preview** plays it locally. Connect the train and enable **Play on Mac for purple brick** to listen for new sensor events. Playback must be enabled again after a page reload. Clips remain on the Mac; this app does not upload them to a service or the train. The labeled demo includes **Simulate purple** for testing without hardware.

The app's idle timer runs in the local Python server while Bluetooth is connected. Motor commands, headlight/sound use, measured motion, and brick detections reset activity; browser heartbeats do not. It never shuts down a train with nonzero requested motor power. Idle settings are stored in the ignored `.duplo-settings.json` file. This does not change LEGO's own snooze setting, which may shut the train down earlier. Power off sends Stop first, cancels queued drive commands, then uses the documented Hub Actions command. Wake the train with its green button and reconnect.

**Connection details → Inspect train capabilities** requests advertised port modes, names and value formats. The same disclosure shows discovered Bluetooth services and characteristic properties. It does not send audio data or probe unknown effect commands. Responses appear in Debug and connection details.

## DJI input in the app

Open **Controller settings**, enter the controller's private IPv4 address, and choose **Connect remote**. This adapter uses the existing DJI RC (RM330) Wi-Fi/DUML listener on TCP port 40007. Verify the actual axes and buttons in the inline readings, apply the mapping, then choose **DJI remote** in Input source. Other DJI models may need another adapter. The standalone listener documentation below describes compatibility and its hardware checks.

## Demo and verification

For an explicitly simulated train, with no Bluetooth access:

```sh
.venv/bin/python app_server.py --demo --port 8101
```

Open [the demo](http://127.0.0.1:8101). It is visibly labeled **DEMO · NO HARDWARE**.

```sh
.venv/bin/python -m unittest -v test_lego_protocol.py test_app_server.py test_dji_remote.py
```

The train and web bridge checks pass. The standalone DJI Wi-Fi/USB protocol tests pass against `dji_wifi.py` and `dji_usb.py`; the Wi-Fi tests cover session replies and overlapping readers.

These tests cover LEGO output bytes for both generations, sensor decoding, controller packets, real local WebSocket/TCP transport, browser-loss stopping, control ownership, queued-command cancellation and controller input loss. They use simulated hardware; physical controls still require a train and remote test. The visual comparison and browser interaction record are in [ui/design-qa.md](ui/design-qa.md).

## Bluetooth diagnostic CLI


Find a LEGO DUPLO 10427 train from a Mac and read its name and battery level.
These commands do not activate the motor.

## Setup

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

Turn the train on, keep it nearby, and close the LEGO phone app so it can accept
another connection. Enable Bluetooth on the Mac. If macOS prompts for Bluetooth
access, allow access for the app running this script.

## Use

```sh
.venv/bin/python train.py scan
.venv/bin/python train.py info
```

If several trains are nearby, select the identifier printed by `scan`:

```sh
.venv/bin/python train.py info --address YOUR_IDENTIFIER
```

If Bluetooth access is denied, enable it for the app running the script in
**System Settings → Privacy & Security → Bluetooth**. The identifier on macOS is
a local Bluetooth UUID, not an IP address.

Protocol: https://lego.github.io/lego-ble-wireless-protocol-docs/
Bluetooth client: https://bleak.readthedocs.io/en/latest/

## DJI remote sticks and buttons over Wi-Fi

`dji_remote.py` and `dji_wifi.py` both start the improved Python 3 listener
with no extra dependencies. Existing `dji_remote.py listen --ip ...` commands
now use the same staggered readers described below. Shared protocol helpers
remain in `dji_remote.py` for the USB reader and web bridge.
It reads a DJI screen controller's TCP/DUML stream and prints stick and wheel
positions, button press/release events, and the Sport/Normal/Cinema switch.
By default, it uses five staggered receive-only TCP readers. When the controller
recycles a session, another reader can continue receiving input. Version queries
and session replies are opt-in with `--keepalive version`; `--passive` is an
alias for the receive-only default. The keepalive setting is not a guarantee that
the controller will leave a connection open.
It does not issue flight, motor, camera, pairing, or firmware commands.

The decoded layout comes from a DJI RC (RM330) community implementation.
Other models require hardware verification. RC-N controllers and DJI FPV USB
controllers need a different transport; connecting them to Wi-Fi alone does not
make this listener applicable.

Power on the screen controller and connect it to the same local Wi-Fi as the
Mac. Find its current IP in its Wi-Fi network details or the router's client
list. For a bench input check, leave the aircraft powered off.

```sh
# Discover candidates on the Mac's active local network:
python3 dji_wifi.py scan

# Explicit subnet if automatic selection is unavailable:
python3 dji_wifi.py scan --network 192.168.1.0/24

# Automatically listen when exactly one valid DJI stream is discovered:
python3 dji_wifi.py listen

# Or use the controller's actual IP (replace this example):
python3 dji_wifi.py listen --ip 192.168.1.123

# JSON for consuming from another script; bounded 30-second capture:
python3 dji_wifi.py listen --ip 192.168.1.123 --json --raw --seconds 30

# Diagnose individual connection resets (normally only input outages are logged):
python3 dji_wifi.py listen --ip 192.168.1.123 --debug

# One reader with the previous version-query behavior, for comparison:
python3 dji_wifi.py listen --ip 192.168.1.123 --connections 1 --keepalive version
```

Stop with **Ctrl+C**. Axis values are approximately -1 to +1, with 0 at center;
axis signs and button labels should be checked on the physical controller.
JSON emits decoded samples, including unchanged samples. Text prints axis
changes and button transitions. Each reader reconnects independently, with a
short retry delay. Button state is preserved across brief reconnects, so a held
button does not produce a new press each time a link restarts. Older observations
from another reader are ignored. Identical neutral frames are retained because
the observed controller uses a constant DUML sequence number.

If no control sample arrives for one second, stderr reports that input is stale;
it reports when samples resume. The final summary includes the longest period
without input and the total number of TCP reconnects. These are real samples;
the listener does not interpolate movement or invent missing button presses.
Short taps can still be missed during outages. A program that drives something
from this stream should track sample age and stop when input becomes stale.
Use `--connections 1..8` to compare reader counts on a different controller.

Discovery checks only TCP port 40007 on one private IPv4 subnet of /24 or
smaller. An open port is listed as a candidate; `duml_verified: true` means a
checksum-valid DJI packet was received. This does not identify the controller's
exact model or guarantee that its control stream is available.

If no service is found, check the controller's model, IP, power and Wi-Fi.
Some firmware reportedly disables network port 40007, according to
[DJI RC Joystick's compatibility note](https://github.com/voluminor/dji-rc-joystick).
This script does not enable ADB, change firmware or open closed services.
If macOS denies network access, allow the terminal under
**System Settings → Privacy & Security → Local Network**.

```sh
python3 -m unittest -v test_dji_remote.py
```

Tests use synthetic packets and loopback TCP servers. They verify checksums,
both CLI entry points, receive-only operation, fragmented TCP reads, corrupted
frames, control decoding, button transitions and
an overlapping stream with a button held across a dropped connection;
they do not establish physical controller compatibility.

### Hardware check on 2026-10-05

The screen controller announced `rm330` (DJI RC) at `192.168.1.91:40007`.
The listener received checksum-valid control frames from it. The user's live
capture also verified left-stick horizontal movement in both directions, a
vertical endpoint, intermediate positions and return to center. The pasted
Wi-Fi log also contains camera half/full press and release transitions.
Right-stick, wheel and remaining button mappings still need a physical Wi-Fi
input test.

The controller closed connections in both receive-only and query modes. The
previous one-reader/version-query captures had pauses of up to 15.98 seconds
over 90 seconds (181 samples), and 17.64 seconds over 120 seconds (149 samples).
With five receive-only readers and faster retries, a 45-second hardware check
received 381 samples, with a longest pause of 1.32 seconds and a 95th-percentile
interval of 0.188 seconds. It still recycled TCP sessions (561 reconnects), so
this reduces gaps rather than making the controller's TCP sessions permanent.
These are sequential bench captures, not a guarantee of future latency.
After routing the original `dji_remote.py` command to the improved reader,
another 45-second capture received 367 samples with 1.14 seconds of maximum
input silence, despite 571 TCP reconnects. Both CLI entry points passed the
loopback regression checks; all 13 standalone Wi-Fi/USB tests passed.
The controller's IP can change when the router assigns a new address.

The overlapping-link approach is also described in the
[DJI RC Joystick connection documentation](https://github.com/voluminor/dji-rc-joystick/blob/main/mod/rc/README.md).

### USB-C alternative

The [DJI RC manual](https://dl.djicdn.com/downloads/DJI_RC/UM/2023/DJI_RC_User_Manual_v1.0_en.pdf)
identifies the normal USB-C charging port as the computer connection (port 8).
The separate Host port is reserved in that manual. Use a data-capable USB cable
to connect the normal port directly to the Mac.
[DJI RC Emulator](https://github.com/deviverr/DJI-RC-Emulator) reports tested
RM330 stick input through a vendor USB bulk interface. Its app and virtual
gamepad backend are Windows-specific. `dji_usb.py` implements a separate USB
listener on this Mac, using PyUSB and a bundled libusb userspace library.
It claims only the DUML bulk interface and sends read-sticks requests. It does
not change the USB configuration or use the MTP/ADB interfaces.

```sh
uv venv .venv-dji-usb
uv pip install --python .venv-dji-usb/bin/python -r requirements-dji-usb.txt
.venv-dji-usb/bin/python dji_usb.py scan
.venv-dji-usb/bin/python dji_usb.py listen
# JSON for another program; 60-second hardware capture:
.venv-dji-usb/bin/python dji_usb.py listen --json --raw --seconds 60
```

The USB listener polls at 50 Hz by default (`--hz 1..100`), validates DUML checksums
and channel tags, and accepts only replies to the current request. It reconnects
if the device disappears or stops replying and releases the interface on Ctrl+C.
JSON uses the same control-event envelope as the Wi-Fi listener. This USB layout
has six axes; `mode` is `unknown` and `buttons` is empty (unavailable). Physical
C1/C2 presses did not change the channel-query reply on this RM330. The reply
starts with a success/error status byte, which is not treated as a button mask.
The documented detection/custom-button USB queries tested here did not return
button data on this firmware. Use the Wi-Fi listener for button/mode experiments;
the USB listener does not infer those fields or report them as released.

On 2026-10-05, the Mac detected DJI vendor/product `2ca3:1023`, with DUML bulk
interface 1 (OUT `0x02`, IN `0x83`). A 60-second capture received 2767 valid USB
responses (46.1 Hz), with no reconnects, a longest silence of 0.067 seconds and
a 95th-percentile interval of 0.026 seconds. That first capture contained only neutral
controls. A subsequent five-minute capture received 13,783 responses (45.9 Hz),
with zero reconnects and longest input silence 0.205 seconds. During that capture
the user moved both sticks: all four axes changed. Observed ranges were right X
[-0.95, +1], right Y [-1, +1], left Y [-1, +1], and left X [-1, +0.447]. The wheel
channels stayed centered, so physical wheel mapping remains unverified.
C1/C2 were pressed but this USB channel query did not report their state.

```sh
python3 -m unittest -v test_dji_remote.py test_dji_usb.py
```

USB tests cover independently checked packet fixtures, corruption and fragmented
reads, channel decoding, rejection of replies to old requests and error statuses, live-axis changes,
and interface cleanup. The USB dependencies are isolated from the web app's
`.venv` and are not needed to run these protocol tests.

Protocol reference: https://github.com/stiad/dji-rc-linux
Attribution: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

### App hardware check on 2026-10-05

The web controller connected to the real 10427 and read battery 100%, firmware 1.4.16.0000, sensor subscriptions and attached ports. The train acknowledged Blue headlight, horn, a 0.55-second forward command at 35% power and Stop. It finished connected with requested motor power 0. The short pulse did not yield a nonzero speed notification, so physical rotation is not inferred from command acknowledgement. Evidence is saved in `ui/qa/hardware-check.json` and `ui/qa/live-controller.png`.
