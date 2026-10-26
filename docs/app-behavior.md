# App behavior in detail

Reference notes for the web app. For a quick start, see the [README](../README.md).

## How it fits together

- `app_server.py` — local Python server (aiohttp). Talks to the train over Bluetooth (bleak), reads the DJI remote over Wi-Fi, runs the safety watchdogs and the idle timer, and serves the UI on `127.0.0.1` only.
- `ui/` — React page. It sends commands and a heartbeat over one WebSocket (`/api/ws`) and shows the state the server reports. No Bluetooth or DJI data is handled in the browser.
- `lego_protocol.py` — LEGO Wireless Protocol packets for the train. `dji_remote.py` / `dji_wifi.py` — DJI DUML decoding and the Wi-Fi reader.

## Connecting

The start card offers **Connect both**, **Train only** and **Remote only**. Each device is tried in the background up to 5 times, with a 2-second pause between tries; the train and the remote are tried at the same time. A train try is one 7-second Bluetooth scan and connection. A remote try first uses the remote's remembered IP (from this browser or the last connection) and waits up to 5 seconds for stick input, then falls back to **Search and connect**. Errors between tries go only to the debug log. The banner shows the reason after the last try fails.

The top bar shows each device's state (for example `Connecting · try 2 of 5`, the train battery, or the remote's IP). While a device is being tried, **Stop** cancels only that device, and **Stop both** appears when both are being tried. A device that has failed, dropped or been stopped shows **Reconnect** (or **Connect** if it never connected). A connected device is not disconnected by **Stop**. The DJI panel's own **Connect**, **Search and connect** and **Disconnect** cancel any automatic remote tries. The app does not reconnect by itself after a device drops.

The speed slider sets motor power, not a calibrated physical speed. Bluetooth writes and command feedback appear in the debug log; physical motion is not inferred from a successful write. "Last action brick" is the last tag the train reported.

## Connection voice

The user-selected spoken confirmations play on the Mac: **“Train connected”**, **“DJI remote connected”**, then an additional **“Train and remote connected”** when the second device connects. The train message waits until Bluetooth setup, sensor subscriptions and the initial motor Stop finish. The remote message waits for valid, fresh left-stick input.

The local server speaks through macOS's installed Samantha voice at 190 words per minute, matching the chosen preview. Messages play one at a time in the background, so they do not delay control commands, require browser autoplay permission, or repeat in multiple tabs. Queued messages for a disconnected device are dropped. A full reconnection announces again; temporary Wi-Fi input gaps do not repeat messages that already played, and the combined message waits until both devices are available. Demo is silent. Speech failures appear in the debug log without failing device connections or controls. Server shutdown cancels pending speech and stops its speech process. No system-voice recordings are included in the repository.

## Stopping and safety rules

- The train stops when the browser disconnects or its heartbeat is missing for about 1.5 seconds. The watchdog checks every 0.15 seconds; Bluetooth adds its own delay.
- Leaving the control window (blur, hidden tab, page close) stops the train. A background window cannot stop another window's active session, and a second window cannot take over an active drive session without stopping it.
- **Stop** cancels queued movement commands.
- DJI-driven motion stops when controller input is older than about 0.6 seconds. A temporary Wi-Fi gap pauses stick driving but keeps computer-driven motion and the stick preference; fresh centered input is needed before the stick can drive again. Remote disconnection or a fatal error suspends stick driving.

## Computer and DJI stick together

With **Drive with left stick** on, the computer buttons and arrow keys still work:

- A computer drive command takes priority until the stick is next centered. Centering releases that priority without cancelling the computer's motion; moving the stick afterwards takes over.
- Centering while the stick is driving sends Stop.
- After a computer Stop, the stick must be centered before it can move the train again.
- More stick travel means more power, beyond the dead zone, up to the Speed setting at full travel. With the default 8% dead zone, 54% travel requests half the maximum power.
- Only the left stick's vertical axis drives. Horizontal travel, the right stick, wheels and DJI buttons do not control the train. Lights, sounds, recordings, power and settings are computer-only.

The stick preference is on by default and turns itself on when the train, fresh DJI input and an active window are ready (it still waits for a centered sample). The RM330 mapping is inverted by default so up drives forward. The on/off and invert choices are saved in the ignored `.duplo-settings.json`.

The server receives DJI RC (RM330) Wi-Fi/DUML input on TCP port 40007 through five staggered, receive-only readers that reconnect independently. Pending input is replaced by the latest sample instead of building a backlog. Other DJI models may need another adapter; see [DJI remote listeners](dji-remote.md).

The **DJI remote** panel starts open on every page load. **Search and connect** finds the Mac's active private IPv4 subnet and checks TCP port 40007 on up to 254 addresses. It connects only when exactly one address sends checksum-valid DJI stick packets, fills in the IP address, and remembers it in this browser. Search runs in the background and can be cancelled. If no remote or several remotes are found, or the network cannot be selected automatically (including subnets larger than /24), the panel explains what to do and keeps manual IP entry available. Both devices must already be on the same Wi-Fi; the button does not change their Wi-Fi settings.

## Purple brick sound

LEGO's official app can transfer a clip of up to five seconds to the train for the purple brick. This app plays your clip **on the Mac** instead: sending audio to the train speaker is not implemented, because the public protocol and inspected implementations do not establish the encoding and transfer sequence. See [the research record](train-features-research.md).

**Record** asks for microphone access and stops after five seconds. **Choose file** accepts any browser-decodable audio and keeps the first five seconds. The clip is stored in this browser's IndexedDB; it is not uploaded anywhere. Playback on detection must be switched on again after a page reload. The demo has **Simulate purple brick** for testing without hardware.

## Power and idle timer

The idle timer runs in the local server while the train is connected. Motor commands, lights and sounds, measured motion and brick detections reset it; browser heartbeats do not. It never turns off a train with nonzero requested power. The setting is saved in `.duplo-settings.json`. It does not change LEGO's own sleep setting, which may turn the train off sooner.

Power off sends Stop, cancels queued drive commands, then sends the documented Hub Actions switch-off command. Wake the train with its green button and reconnect.

## Debug log

**Debug** starts open on every page load and can be toggled with the top-bar button. It contains the event log (filter, pause, clear, NDJSON export, event inspector) and **Connection details**: train name, Bluetooth ID, model, firmware, signal, raw speed sensor, last brick, ports, raw DJI readings, and an optional train identifier for when several trains are nearby. **Inspect train capabilities** requests advertised port modes, names and value formats and lists Bluetooth services; it does not send audio or probe unknown commands.

## Tests

The Python tests cover LEGO output bytes for both train generations, sensor decoding, controller packets, real local WebSocket/TCP transport, browser-loss stopping, control ownership, queued-command cancellation and controller input loss. They use simulated hardware; physical controls still need a real train and remote. Browser checks are recorded in [ui/design-qa.md](../ui/design-qa.md).

## App hardware check on 2026-10-05

The web controller connected to the real 10427 and read battery 100%, firmware 1.4.16.0000, sensor subscriptions and attached ports. The train acknowledged Blue headlight, horn, a 0.55-second forward command at 35% power and Stop. It finished connected with requested motor power 0. The short pulse did not yield a nonzero speed notification, so physical rotation is not inferred from command acknowledgement. Evidence is saved in `ui/qa/hardware-check.json` and `ui/qa/live-controller.png`.
