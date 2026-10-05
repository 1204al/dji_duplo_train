# DUPLO 10427 features — researched 2026-10-05

The requested additions stay on the selected dark, single-page controller. Purple brick sound and Power & sleep start expanded and can be collapsed. Debug remains visible by default.

## Findings and sources

- [LEGO's 10427 product and Data Act notice](https://www.lego.com/en-no/product/interactive-adventure-train-10427), section 6, confirms an optional sound clip of up to five seconds triggered by the purple brick. It does not specify an audio codec, chunk size, transfer command, or acknowledgment sequence.
- [Official LEGO Wireless Protocol 3 documentation](https://lego.github.io/lego-ble-wireless-protocol-docs/) defines Hub Actions / Switch Off Hub as `04 00 02 01`. Upstream actions `0x30` and `0x31` announce impending shutdown/disconnection. Port Information Request `0x21` and Port Mode Information Request `0x22` provide read-only capability discovery. The standard Hub Properties table does not expose an adjustable snooze timeout.
- [The original official-app Bluetooth capture in Legoino issue 90](https://github.com/corneliusmunz/legoino/issues/90#issuecomment-3456111339) reports motor port `0x32` and effects port `0x34`, with horn and color commands. The public issue's comments and the captured comment's URL were retrieved using GitHub's read-only API.
- [Pybricks' original implementation](https://github.com/pybricks/pybricks-micropython/blob/master/pybricks/iodevices/pb_type_iodevices_lwp3device.c) provides generation-specific train motor, light, horn, speed, and tag support. Purple's star tag is 324. Its train support does not provide custom-audio upload.
- [The 10427 BLE bridge's command builders](https://github.com/micschr0/duplo-train-10427-ble2mqtt/blob/main/src/protocol/commands.rs) provide another implementation of motor and effects commands. Its generic sound presets target the older speaker port; those are not treated as verified 10427 sounds here.

The official app capture reports these new-generation headlight values: Off 0, White 1, Green 7, Yellow 8, Purple 10, Red 12, and Dark blue 15. The selected UI's six swatches are preserved; their new-generation encodings were corrected. Earlier-generation values remain separate. Our packets use immediate execution and feedback flags; the existing short horn/light frames were already acknowledged by the user's train during the preceding hardware check.

## Implemented and remaining limits

| Feature | Implementation | Verification |
| --- | --- | --- |
| Forward / backward / Stop | Signed motor power on port 50 | Protocol and bridge tests; preceding live forward/Stop feedback |
| Six colors and Off | Effects port 52, mode 1, color subcommand 4 | Source capture and protocol tests; preceding blue command feedback |
| Horn | Effects port 52, mode 1, horn subcommand 7 | Protocol tests and preceding live command feedback |
| Turn off | Stop, then standard Hub Actions switch-off | Ordering, queued-motion cancellation, failure handling tests; fresh hardware check pending |
| Idle shutdown | Server-managed idle timer with a persistent setting | Activity, moving-train exclusion, heartbeat independence, expiry tests |
| Record purple sound | Browser microphone recorder with automatic five-second finish | Recording implementation present; microphone capture deliberately left for user initiation |
| Choose file and Preview | Decode, trim to five seconds, mono PCM WAV, browser storage, Web Audio preview | Seven-second synthetic WAV import, five-second output, preview, reload persistence checked in browser |
| Purple playback on Mac | Fresh tag-324 notification counter triggers an explicitly armed browser player | Repeated-event decoding, freshness tests and browser demo trigger passed; physical purple brick pass still pending |
| Upload sound to train speaker | Not implemented | Required proprietary encoding/transfer protocol not established |
| LEGO's built-in snooze setting | Not implemented | No confirmed setting command found; app timer does not override it |

No audio upload, snooze mutation, bootloader action, or guessed proprietary effect was sent. Read-only port inspection is available in Connection details to collect additional evidence. Future native audio support requires a capture of a successful official-app transfer and a verified encoder/acknowledgment sequence. A snooze setting similarly needs a confirmed app capture. The feature screen directs native speaker recording to LEGO's app.

## User data and timer behavior

The recorded/imported clip stays in IndexedDB under this local origin. Audio conversion is for browser playback, not a claim about LEGO's audio format. No microphone is opened until Record is clicked. Playback is armed by a user gesture and is not restored automatically after reload. Initial snapshots and stale sensor events do not play a clip.

The timer defaults to disabled and runs only while Bluetooth is connected to the local server. Requested motion pauses expiry; actual sensor motion and bricks refresh activity. Power-off transitions invalidate queued movement before acquiring the write lock. Browser heartbeat packets do not prevent idle shutdown. The train may still apply its own firmware sleep policy first.

## Sound-button follow-up and native upload investigation

The user expects their recording to play from the train's speaker. Native upload remains required work; browser playback is a fallback. Their existing microphone clip (3.8 seconds) was visible after the UI reload and was not replaced, exported or modified.

The Sound dropdown was replaced with direct buttons. [Check Device's original 10427/10428 controller implementation](https://checkdevice.online/static/_/bluetooth_lego-duplo-train.js?md5=a4a22a3dc0c4cc5bf449900262728cc4) defines horn as event `0x0107` and starting sound as `0x0106`, sent with `09 00 81 34 11 51 01 07 01` / `09 00 81 34 11 51 01 06 01`. Starting sound was added from this implementation. Both buttons emitted their expected packets in the isolated demo. This is not an exhaustive list of sounds in the hardware or official app. Starting sound has not yet had a live listening check.

A live read-only port inspection of the user's train (firmware 1.4.16.0000) found:

| Port | Advertised mode names |
| --- | --- |
| 50 | 0 POWER, 1 SPEED |
| 51 | 0 TAG |
| 52 | 0 VERS, 1 EVENTS, 2 DEBUG |
| 53 | 0 VLT L, 1 VLT S |
| 54 | 0 SPEED, 1 COUNT, 2 VELO |

Evidence is in `ui/qa/train-capabilities.json`. The train announced native shutdown (`04 00 02 30`) at 11:37:57 UTC, approximately five minutes after connection, while the app timer was disabled. A later scan found no advertising train. This observation does not establish the native timeout's configuration command.

Inspection now also collects standard read-only mode value formats and already-discovered GATT service metadata. These additions await another live connection. Port names alone do not reveal the audio codec, transfer framing, acknowledgments or purple-slot activation command. Public LEGO documentation, Pybricks, Legoino's app capture, and the inspected community controllers did not establish those details; no guessed audio upload was sent.

The next useful evidence is one successful five-second-or-shorter transfer from the official LEGO DUPLO Interactive Trains app (`com.lego.duplo.interactivetrains`), including setup, audio writes, replies and purple-brick playback. The user's phone platform is needed to choose the capture method. [Android's official Bluetooth debugging guide](https://source.android.com/docs/core/connect/bluetooth/verifying_debugging) describes full HCI snoop logging through Developer options and extracting logs from a bug report. A filtered LEGO-only capture should be analyzed locally; no full phone bug report needs to be uploaded externally. The browser's PCM WAV conversion is not assumed to match the train's audio format.

The user subsequently confirmed **iPhone with the LEGO app**, reported recording a sound, and said they disabled the timeout. The web server confirms its own idle timer remains disabled; this does not prove the train's native timeout setting. A fresh Mac scan still found no advertising train. The user was asked to fully close the LEGO app and connect the phone by USB. No physical iPhone appeared in Apple's device inventory at that check.

[The iPhone capture procedure](iphone-bluetooth-capture.md) now uses Apple's official logging profile plus PacketLogger or an isolated maintained capture CLI. Apple redirects the profile and instructions downloads to account sign-in. `inspect_iphone_capture.py` is an offline decoder ready for the resulting trace; six regressions cover endian variants, fragmented extended-length messages, invalid/truncated files, command/reply preservation, ambiguity, and exclusion of unrelated sessions even when an HCI handle is reused. Native audio upload still awaits a successful official-app capture. No phone logging has been enabled and no iPhone capture has been collected.
