# Capture one LEGO iPhone transfer

The user has the official LEGO app on an iPhone. The controller's native recording upload still needs the actual codec, packet framing, replies and purple-slot activation command. A recording made earlier cannot reveal the transfer; the transfer must be repeated while logging.

## Preparation

1. Use [Apple's Bluetooth logging profile for iOS/iPadOS](https://developer.apple.com/feedback-assistant/profiles-and-logs/?name=bluetooth) and its linked Instructions. Apple requires account sign-in for both downloads. Review the profile on the iPhone before installing it; this enables diagnostic Bluetooth logging and may include traffic from other accessories. Obtain the user's agreement before enabling it. Do not install a replacement profile from a third party.
2. Connect the unlocked iPhone to the Mac by USB. Complete any Trust prompt on the phone. The Mac and iPhone must take turns connecting to the train.
3. Capture with Apple's PacketLogger (in Additional Tools for Xcode), **File → New iOS Trace**, or the isolated `pymobiledevice3` CLI below. [Apple demonstrates the live iOS trace workflow](https://developer.apple.com/videos/play/wwdc2019/901/).

The optional Python environment is separate from the BLE web server:

```sh
python3 -m venv .venv-iphone-capture
.venv-iphone-capture/bin/python -m pip install -r requirements-iphone-capture.txt
```

## Short capture sequence

Begin before the LEGO app connects so the train's name and port attachments identify its session. Use a new file for every run; the upstream capture CLI overwrites an existing output path.

```sh
mkdir -p captures
.venv-iphone-capture/bin/python -m pymobiledevice3 btlogger captures/lego-transfer-01.pklg --format packetlogger
```

While capture is active:

1. Connect the train in the official LEGO app.
2. Play its sound control once.
3. Record a short test phrase and send it to the train. Test purple-brick playback from the train's speaker.
4. If investigating native snooze, change that setting separately and note the time and before/after values. Restore the user's preferred disabled state at the end.
5. Stop capture with Ctrl+C, close the LEGO app, and reconnect the web controller. Follow Apple's instructions to remove the temporary logging profile afterward.

Keep the original trace on this Mac. It can contain voice audio and traffic from other Bluetooth devices; it is not intended for external upload. Extract only the train session:

```sh
.venv/bin/python inspect_iphone_capture.py captures/lego-transfer-01.pklg --output captures/lego-transfer-01.json
```

The decoder recognizes DUPLO name/10427 port messages, reassembles ACL fragments, preserves ATT writes and notifications, and separates reused HCI connection handles. It refuses ambiguous or unidentified sessions rather than exporting other devices' contents. If multiple DUPLO sessions are identified, select the session reported by the tool using `--connection 0x000b:0` (use the actual reported value). It supports PacketLogger files, not pcapng.

This preparation is not evidence that train-speaker upload has been implemented. Once the trace exists, establish the audio encoding, transfer sizing/flow control, success response and purple activation sequence, then implement and verify upload against the physical train. The Mac's stored PCM WAV must not be sent unchanged unless the capture establishes that format.

Format sources: [Wireshark's PacketLogger reader](https://github.com/wireshark/wireshark/blob/master/wiretap/packetlogger.c), [pymobiledevice3's original capture service](https://github.com/doronz88/pymobiledevice3/blob/master/pymobiledevice3/services/bt_packet_logger.py), and [LEGO LWP3](https://lego.github.io/lego-ble-wireless-protocol-docs/).

The pinned 11.23.0 tool was installed in the isolated environment, and its `btlogger --help` interface was checked. Its single command is flattened: pass the output filename directly after `btlogger`. No phone capture was started. The complete Python suite passes 44 tests, including six offline capture-decoder tests.
