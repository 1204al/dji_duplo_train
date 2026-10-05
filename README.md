# DUPLO Control

Drive a LEGO DUPLO smart train (10427 / 10428, also the older 10874 / 10875) from your Mac. A small Python server talks to the train over Bluetooth and serves a web page where you drive forward and backward, set the speed, change the headlight color and play sounds. Optionally you can also drive with the left stick of a DJI RC (RM330) drone remote over Wi-Fi.

Everything runs locally on this Mac. The browser does not need Bluetooth support.

## Quick start

One-time setup:

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
cd ui && npm ci && npm run build && cd ..
```

Start the app, then open <http://127.0.0.1:8100>:

```sh
.venv/bin/python app_server.py
```

Stop it with **Ctrl+C**.

No train nearby? Run the demo with a simulated train (labeled **DEMO · NO HARDWARE**) and open <http://127.0.0.1:8101>:

```sh
.venv/bin/python app_server.py --demo --port 8101
```

## How to use

1. **Connect.** Turn the train on with its green button, close the LEGO DUPLO app on your phone (the train accepts one connection at a time), then click **Connect train**.
2. **Drive.** Set **Speed**, then click **Forward**, **Backward** or **Stop**. Keys: ↑/→ forward, ↓/← backward, Space or Esc stop. Turn on **Hold to drive** to move only while a button or key is held. Leaving the window stops the train.
3. **Lights and sounds.** Pick a headlight color or press a sound button.

Optional panels below the controls:

- **DJI remote** — enter the remote's IP address (same Wi-Fi as the Mac) and click **Connect**. With **Drive with left stick** on, center the stick, then push up for forward and down for backward. Full stick equals the Speed setting. The computer controls keep working; lights and sounds stay on the computer.
- **Purple brick sound** — record or choose a clip of up to 5 seconds and turn on **Play on purple brick**. The clip plays **on the Mac**, not on the train's speaker (use the LEGO app for that).
- **Power** — turn the train off now, or automatically after 1–30 idle minutes. Press the green button to wake it.

**Debug** in the top bar shows the Bluetooth/DJI event log and connection details.

### Troubleshooting

- **Train not found:** make sure it is on and nearby, and that the LEGO app is closed. If macOS denied Bluetooth access, allow it for your terminal in **System Settings → Privacy & Security → Bluetooth**.
- **Several trains nearby:** open **Debug → Connection details** and enter the train identifier (from `train.py scan`, below).
- **DJI remote won't connect:** check its current IP in its Wi-Fi settings or your router. If macOS asks, allow Local Network access for your terminal. Some firmware disables the Wi-Fi port; see [docs/dji-remote.md](docs/dji-remote.md).

## Tests

```sh
.venv/bin/python -m unittest -v test_lego_protocol.py test_app_server.py test_dji_remote.py
cd ui && node --test tests/*.mjs
```

The tests use simulated hardware.

## Project layout

| Path | What it is |
| --- | --- |
| `app_server.py` | Local server: Bluetooth, DJI input, safety watchdogs, WebSocket API |
| `lego_protocol.py` | LEGO train Bluetooth packets |
| `dji_remote.py`, `dji_wifi.py`, `dji_usb.py` | DJI remote decoding and Wi-Fi / USB listeners |
| `train.py` | Command-line Bluetooth check (scan, name, battery) |
| `ui/` | React web page |
| `docs/` | Detailed notes and research |

## More detail

- [App behavior in detail](docs/app-behavior.md) — safety rules, how computer and stick driving share control, purple sound, idle timer.
- [DJI remote listeners](docs/dji-remote.md) — command-line Wi-Fi and USB readers, compatibility, hardware test results.
- [Train features research](docs/train-features-research.md) — sources and supported commands.

## Bluetooth check from the command line

Find a train and read its name and battery level. These commands do not move the motor.

```sh
.venv/bin/python train.py scan
.venv/bin/python train.py info
.venv/bin/python train.py info --address YOUR_IDENTIFIER   # if several trains are nearby
```

On macOS the identifier is a local Bluetooth UUID, not an IP address.

Protocol: https://lego.github.io/lego-ble-wireless-protocol-docs/ · Bluetooth client: https://bleak.readthedocs.io/en/latest/
