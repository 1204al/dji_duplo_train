# DUPLO Control design QA

final result: passed

## 2026-10-05 simplified layout

At the user's request the UI was simplified for first-time use. One centered column: a "Connect your train" step card (shown only while disconnected), Drive, Headlight & Sounds, then collapsible DJI remote / Purple brick sound / Power panels with a one-line subtitle and status chip. Debug is hidden by default behind the top-bar button. The Driving controls dropdown and the separate Enable/Disable left-stick button were merged into one **Drive with left stick** switch; invert and dead zone apply immediately. Long explanatory text moved to `../docs/app-behavior.md`. The backend was not changed.

Browser checks in the labeled demo (1440px and 375px): connect/disconnect, Forward button, ArrowUp drive and Space stop, Blue headlight, Debug open/close, idle timer countdown (5 min), audio-file import with 7 s trimmed to 5 s, purple playback switch and Simulate purple brick. No horizontal overflow at 375px and no console errors. Python and JavaScript test suites pass.

Polish pass: shorter copy throughout, one muted label style, neutral direction buttons with the moving direction lit blue, battery icon by level (amber at 20% or less), state chips for Purple brick and Power (with idle countdown), DJI stick controls hidden until the remote connects, icon-only Debug button and one-line header at 375px. Checked in the demo and with a simulated connected DJI remote (switch, live meter, power reading, center hint).

## 2026-10-05 default enable and corrected direction

The user confirmed that forward/backward were reversed. The RM330 mapping now defaults to inversion, so negative raw vertical input (physical up) requests forward power, and physical down requests reverse power. The left-stick preference defaults to enabled and is shown consistently by Driving controls and the Disable button. An active browser automatically activates the preference once train and fresh remote input are ready, requiring a new centered sample before movement. Explicit disable and inversion choices survive server restarts, alongside existing idle settings.

Window exit suspends that window's session; focus/reconnection can resume the enabled preference after centering. A background window cannot suspend another window's active session. Computer-origin motion remains active while default stick mode joins the same browser, with a fresh center required before stick takeover.

The complete Python suite passes **74 tests**, including default direction, connection readiness, saved preferences, focus suspension/resume, active versus inactive WebSocket heartbeats and computer takeover. The production UI build and all seven JavaScript tests pass. The earlier direction uncertainty below is resolved by the user's correction; subsequent evidence is recorded in `qa/default-left-stick-state.json` and `qa/default-left-stick-controls.png`.

Both open app tabs were refreshed. The user's tab shows Computer + DJI selected, Disable left stick pressed, inversion checked in settings and the previous 12% maximum. No browser warnings/errors were reported. The real train is connected and stopped. The remote did not respond at `192.168.1.91:40007`, and a bounded receive-only scan of the active private /24 found no DJI service. The enabled preference is retained while Wi-Fi reconnects; automatic activation is covered by the real local WebSocket test with simulated input.

## 2026-10-05 direct left-stick control and proportional power

Train controls now includes **Enable left stick / Disable left stick**, a live vertical-position meter, signed direction and motor-power readings. The readings and drive commands share the same backend mapping: only left-stick vertical travel is used, with zero power inside the dead zone and proportional power beyond it. Full travel reaches the computer's speed setting. The existing 12% maximum and 3.8-second microphone recording were preserved. Status/help text is grouped with the stick control; computer Forward, Backward and Stop remain available on the same page.

All **68 Python tests** pass. New checks cover proportional power in both directions, calibration/inversion/freshness readouts, recovery after a temporary Wi-Fi gap, Stop during the gap and refusal to rearm a closed browser. The production UI build and all seven JavaScript tests pass. No browser warnings or errors were reported, and the inspected 1066px view has no horizontal overflow.

The real train and RM330 both connected. While the user moved the left stick, the bridge delivered forward/reverse commands at varying powers and received motor feedback and nonzero raw speed notifications. Evidence is in `qa/left-stick-hardware-feedback.json`. A real input gap exposed unwanted switching to Computer only; the final backend instead stops stick-driven motion, retains the enabled mode and requires a fresh centered sample before resuming. Explicit disconnection, fatal errors and browser loss still disable it. Computer-origin motion remains under the browser watchdog during a Wi-Fi gap.

Live input reached both ends and returned to center. Those readings do not independently label physical up versus down; the existing inversion setting remains available for direction correction. The capture is `qa/left-stick-direction-check.json`. The final live state and inspected screenshot are `qa/left-stick-live.json` and `qa/left-stick-live.png`.

## 2026-10-05 Wi-Fi left-stick driving and computer controls

Driving controls now offers **Computer only** and **Computer + DJI left stick**. Computer Forward/Backward buttons and arrow keys remain available in both modes. Only the left stick's vertical axis commands the train; other stick axes, wheels and buttons are readings only. Remote stop/horn mapping fields were removed. Lights, sounds, purple audio, power and settings remain computer controls. Inversion, dead zone and left-stick center calibration remain inline.

The backend uses five staggered receive-only Wi-Fi readers with independent reconnects, separate from a latest-sample Bluetooth consumer. Computer commands override the stick until a later center sample; center hands control back without stopping latched computer movement. Visible Stop requires a new center before DJI can resume. Window blur/hide/exit additionally disables DJI driving. Wi-Fi loss stops DJI-driven movement while leaving computer-driven movement under the browser heartbeat watchdog.

All **63 Python tests** pass, including fresh-neutral boundaries, latest-sample replacement, slow Bluetooth acquisition, reconnection cleanup, computer/stick takeover, computer-only effects, browser heartbeat loss, cross-window ownership, overlapping Stops and canceled Stop writes. The production UI build and all seven existing JavaScript tests pass.

Browser checks used a visibly labeled demo and temporary synthetic neutral DJI frames: computer Forward/Backward, Stop, horn and Blue headlight all worked with combined driving enabled. Settings expose the fixed left stick and no remote action mappings. No browser warnings or errors were observed. The inspected screenshot is `qa/wifi-dual-controls.png`.

The updated real local controller received valid neutral DJI Wi-Fi input from RM330 at `192.168.1.91`, with computer-only driving selected and the previous 12% setting preserved. The live Bluetooth scan did not find an advertising train, so combined physical movement remains unverified. No forward/reverse commands were sent to real hardware during this update. Connection evidence is in `qa/wifi-drive-connections.json`.

No actionable P0/P1/P2 visual differences remain for the implemented controls. The earlier live train command/feedback checks passed. New shutdown and physical purple-trigger checks are pending because the train was not advertising at the final scan. Native sound upload and native snooze configuration remain explicitly unsupported; see the feature update below. Physical DJI button/stick operation is a separate verification gap.

## 2026-10-05 sound buttons and recording follow-up

The Sound dropdown and separate Play action are replaced by direct **Train horn** and **Starting sound** buttons for 10427/10428. Earlier trains receive their own five sound buttons. Starting sound follows event 0x0106 in Check Device's original implementation; its hardware listening check is still pending. The screen now states that the existing clip is saved on the Mac and has not been sent to the train; the checkbox is labeled **Play on Mac for purple brick**. Native upload remains required work.

The direct buttons were exercised through the real WebSocket bridge in the isolated demo: Debug showed the expected horn and starting packets. Desktop (1488px) and narrow (390px) views were inspected; at 390px, scroll width equals viewport width and both buttons fit. No console warnings or errors were reported. Captures: `qa/sound-buttons-desktop.png`, `qa/sound-buttons-mobile.png`, `qa/sound-buttons-live.png`. The last capture uses the real origin and retains the user's **Microphone recording · 3.8 s**, with the 12% setting preserved.

All **38 Python tests** pass, including 25 train/protocol/bridge tests and the independently maintained DJI tests. Production build passes. Two new bridge regressions cover generation-specific sound availability and format metadata collection. No user microphone recording was initiated or replaced.

Live read-only port inspection returned port 52's VERS / EVENTS / DEBUG mode names (`qa/train-capabilities.json`). The train then announced native shutdown while the app timer was disabled. A reconnect scan found no advertising train. Therefore the new Starting sound and extended GATT/value-format inspection have not been validated on hardware. The native audio transfer format, chunks, acknowledgments and activation command still need evidence from one official-app transfer. The user's phone platform and a wake-up for the train were requested.

## 2026-10-05 feature update

The user requested forward/backward, colors, sound, shutdown timing, and recording for purple. The original layout remains the reference for the header, drive/effects controls and Debug. The user-requested extension adds two inline sections below effects: **Purple brick sound** and **Power & sleep**. They start expanded, are collapsible, and introduce no tabs. Reverse is labeled Backward. The new sections reuse the existing borders, typography, controls, and restrained purple accent.

Final evidence: `qa/features-desktop.png` (1488px desktop, expanded sections, labeled demo), `qa/features-mobile.png` (390px, expanded sections), and `qa/features-live.png` (real local app, no clip recorded, no Bluetooth train found). Screenshots were inspected. Debug remains visible and sticky on desktop. Narrow screens retain the same page and have no horizontal document overflow. A narrow-screen effects grid correction prevents the Sound row's intrinsic width from extending beyond its content column. No new actionable P0/P1/P2 visual issue remains.

Browser checks passed: seven-second synthetic audio import trims to five seconds; Preview starts local Web Audio playback; explicitly enabling Play on purple brick and Simulate purple starts local playback and logs the tag and audio event; reloading restores the clip and resets playback arming; idle selection shows a countdown; Turn off train emits Stop then Hub Actions switch-off and shows physical wake instructions; both sections collapse; Hide/Show Debug preserves one page. The browser console reported no warnings/errors. Microphone capture itself was left for user initiation and was not exercised by the agent.

The finalized checks pass: 23 train/bridge tests, 3 audio tests, and 4 Sites packaging tests; the production build succeeds. New regressions cover shutdown ordering, queued-drive invalidation, failure handling, idle expiry, heartbeat independence, moving-train exclusion, persisted settings, repeated purple events, stale/snapshot event suppression, five-second truncation, and WAV conversion. Existing DJI work was preserved.

**Functional limits:** the five-second clip plays on the Mac. The screen explicitly directs train-speaker recording to LEGO's official app; a native audio upload protocol has not been established. The app idle timer does not mutate LEGO's built-in snooze policy, which may turn the train off earlier. The final real Bluetooth scans found no advertising train, so this update does not claim live validation of power off, corrected additional color values, or a physical purple-brick pass. Earlier blue/horn/forward/Stop command feedback remains the recorded hardware evidence. Sources and implementation boundaries are in `../docs/train-features-research.md`.

## Evidence and comparison state

- Source visual truth: `/Users/andriiliubivyi/Documents/ChatGPT/dji_duplo_train/ui/reference/selected-design.png` (selected dark option 2).
- Implementation: `http://127.0.0.1:8101/`, the explicitly labeled demo. The real controller is `http://127.0.0.1:8100/`.
- Final screenshot: `/Users/andriiliubivyi/Documents/ChatGPT/dji_duplo_train/ui/qa/desktop-final.png`.
- Source: 1487 × 1058 pixels. Implementation: 1488 × 1056 pixels, CSS viewport 1488 × 1056, devicePixelRatio 1. The source was resampled by less than 0.2% to the implementation dimensions for side-by-side evidence.
- Matched state: dark desktop, stopped train, speed setting 35%, connected, battery 100%, signal −50 dBm, headlight Off, Train horn, manual input, unconfigured DJI, Debug visible, disclosures closed, four initial log events.
- The design's “DESIGN PREVIEW” text becomes “DEMO · NO HARDWARE” in the demo and “LOCAL CONTROLLER” in the real app. Connection/battery data are actual backend state outside demo. Runtime timestamps intentionally differ.
- Full-view comparison: `qa/comparison-final.jpg` combines the source and rendered implementation in one image.
- Focused comparisons: `qa/focus-controls-final.jpg` and `qa/focus-debug-final.jpg` combine both artifacts at readable scale.
- Additional browser captures: `qa/mobile.png` at 390 × 844 CSS viewport (390 × 1725 full page), `qa/mobile-settings.png` (390 × 2206 full page), `qa/tablet.png` at 1100 × 800 CSS viewport (1100 × 1108 full page), and `qa/connection-error.png` at 1488 × 1056.

## Comparison history

1. First capture: `qa/desktop-before.png`, combined in `qa/comparison-before.jpg`.
   - [P2, fixed] Header/input rows were too tall, Train controls heading too small and control/debug proportions drifted. Set desktop rows to 80px each, grid ratio to 1.718:1, heading to 43px/700, Debug to 36px/700, and reduce excess space above the speed control.
   - [P2, fixed] Effects layout gave too much room to the color controls and lacked the sound divider. Restore 1.23:1 proportions, the divider, selected color order and six color tokens.
   - [P2, fixed] Native slider styling was too thin and the thumb too small. Use an 8px track with a 24px thumb and a filled portion following the actual value.
   - [P2, fixed] Debug typography was undersized and Connection details sat too low. Increase log text/hex and place the disclosure at the selected design's desktop vertical position.
2. Revised capture: `qa/desktop-after.png`, combined in `qa/comparison-after.jpg`; readable comparisons in `qa/focus-controls.jpg` and `qa/focus-debug.jpg`.
   - [P2, fixed] Debug INFO rows remained too tall. Reduce ordinary rows to 74px, packet rows to 94px, line height to 1.35 and message/hex gap to 6px.
3. Final capture: `qa/desktop-final.png`, full and focused comparisons listed above. No remaining P0/P1/P2 findings. A keyboard-stop focus fix changes interaction behavior only and preserves this visual state.

## Required fidelity surfaces

- **Fonts and typography:** locally bundled Inter, 400/500/600/700 weights. Desktop title/status hierarchy, body text, labels and readable monospace hex match the reference's intent. No font network dependency. Native select arrows and library icon glyphs have minor, acceptable differences.
- **Spacing and layout rhythm:** header, input row, left/right regions, drive buttons, dividers, effect groups and Debug footer align with the selected visual. Debug Hide expands controls to full width. Narrow screens stack the same sections with no navigation tabs or horizontal overflow. Expanded settings/details use normal page/panel scrolling.
- **Colors and tokens:** charcoal backgrounds, subdued borders, cool foregrounds, red Stop, blue Forward, green connection/RX and blue TX. The generated reference has slight background/shading texture; flat tokens are an intentional UI implementation choice.
- **Image and asset fidelity:** the reference has no photos or bespoke illustrations. Icons come from Phosphor React, with actual interactive HTML controls. No screenshot used as the interface. White/yellow/red/green/blue/purple swatches preserve the reference ordering.
- **Copy and content:** one adult control screen, Debug visible by default, inline controller settings/details. Added operational text explains keyboard controls and connection requirements. Direct horn and starting buttons are now exposed on 10427; the starting command is awaiting a hardware listening check. Other presets and native clip upload are not established for this generation.

## Browser interaction checks

Passed in the in-app browser with the real local web bridge and simulated train:

- Forward/reverse generate the correct signed motor packets and update the status.
- Stop and Space-to-stop return status to Stopped. Space also emits Stop while the speed slider has keyboard focus.
- Hold-to-drive pointer press/release produces drive followed by Stop.
- Speed slider reaches 100%; headlight Blue and Play horn emit the 10427 effect packets.
- Debug Pause freezes displayed events while new events continue collecting; Resume catches up.
- Errors-only filter shows the empty state; All events restores events.
- Packet selection reveals decoded text and raw bytes. Clear removes this window's events.
- Hide/Show Debug preserves one screen. Controller settings and Connection details expand inline.
- Export was clicked without console errors. The NDJSON server export is verified by the web tests. The in-app browser's automated download event did not return a file path, so saved-file verification remains a browser-specific test gap.
- At 390px and 1100px, document scroll width equals viewport width. Settings remain on the same page and all controls are reachable.
- Real Bluetooth Connect shows Searching, then a readable retry error when no train is advertising. Motor/effect controls stay disabled. Dismiss clears the alert.
- Browser console warning/error logs checked: none reported during the interaction pass.

## Functional checks and limits

The latest full Python suite passes all 38 tests, including the train/web bridge and the independently maintained DJI listener. Existing DJI implementations were preserved. This supersedes the earlier intermediate run with stale standalone DJI tests.

`npm run build` passed; all four existing Sites packaging tests passed. The app's hardware bridge runs locally; static Sites packaging does not provide Bluetooth access.

After the user switched it on, the real 10427 connected successfully. Battery 100%, firmware 1.4.16.0000, attached ports 50–54, and raw speed sensor notifications were received. Blue headlight, horn, a 35% forward motor pulse lasting 0.55 seconds, and Stop all received hardware command feedback. The train finished connected with requested power 0. Evidence: `qa/hardware-check.json` and `qa/live-controller.png`. The motor pulse did not produce a nonzero speed notification, so this proves live command delivery/feedback rather than observed physical rotation. Existing DJI listener documentation records RM330 frames; this app's physical remote drive mapping still needs a hands-on check. No other DJI model compatibility is assumed.

## Follow-up polish

- [P3] Phosphor switch/battery/signal glyphs differ slightly from the generated mock.
- [P3] Long axis option text can truncate slightly in the mobile settings select; the selected option remains available in its native menu.

## Implementation checklist

- [x] Single dark screen faithful to option 2.
- [x] Debug visible by default and hideable.
- [x] Local BLE connection and generation-specific controls.
- [x] Existing DJI listener integrated with explicit input selection.
- [x] Connection-loss stopping and meaningful transport tests.
- [x] Full-view and focused visual comparisons inspected after fixes.
- [x] Desktop/narrow layouts, empty/error states and console reviewed.
- [x] Live train connection, effect commands, brief motor command and Stop feedback.
- [ ] Physical remote mapping check. The current standalone DJI tests pass.

## DJI discovery check — 2026-10-07

The DJI panel now has **Search and connect**, a searching state with Cancel, inline failure feedback, and manual IP entry. The live app selected `192.168.1.0/24`, completed a search with no verified remote found, and successfully cancelled a second search. The real train reconnected with battery 100% and requested power 0.

A temporary server on a separate localhost port supplied simulated verified remote input. Browser checks confirmed automatic connection, IP fill (`192.168.1.91`), and persistence after disconnect and reload. This verifies the UI/WebSocket path; live remote discovery success still needs a powered-on remote advertising its stick stream. The temporary server and tab were closed. The main browser reported no warning/error logs. Screenshot: `qa/search-and-connect.jpg`.

Validation: all 84 Python tests and seven UI tests passed; `npm run build` and `git diff --check` passed. Discovery tests cover valid/invalid packets, unrelated open services, no or multiple candidates, network errors, cancellation, duplicate searches, manual takeover, shutdown cleanup, and WebSocket/heartbeat/Stop responsiveness during search.

## Automatic connection check — 2026-10-08

The start card now has **Connect both**, **Train only** and **Remote only**, and the top bar shows a Train and a DJI remote status pill. A temporary localhost server simulated no train nearby and a remote found on the third Wi-Fi search. Browser checks confirmed:

- both devices showing `Connecting · try N of 5` with their own **Stop** plus **Stop both**;
- stopping only the train while the remote kept trying and then connected (`192.168.1.91`);
- the train failing all five tries, then showing **Failed · Reconnect** with the reason in the banner;
- **Reconnect** and **Stop** working from the top bar;
- the 375 px layout;
- the Demo state (train connected with battery and **Disconnect**, remote "Off in Demo").

The temporary server was stopped and the browser console had no errors. Physical retries with the real train and remote are not yet checked.

Validation: all 90 Python tests and four UI tests passed; `npm run build` and `git diff --check` passed.
