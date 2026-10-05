import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowClockwise,
  ArrowLeft,
  ArrowRight,
  BatteryEmpty,
  BatteryFull,
  BatteryHigh,
  BatteryLow,
  BatteryMedium,
  Bluetooth,
  Play,
  Power,
  Square,
  Terminal,
  WarningCircle,
} from "@phosphor-icons/react";
import { useBridge } from "./bridge.js";
import DebugPanel from "./DebugPanel.jsx";
import PurpleSound from "./PurpleSound.jsx";
import RemoteCard from "./RemoteCard.jsx";
import { Panel, Switch, readStored, writeStored } from "./parts.jsx";

const COLORS = {
  white: "#eceef2",
  yellow: "#f6e200",
  red: "#f02828",
  green: "#18e67b",
  blue: "#176af4",
  purple: "#a83be4",
};
const SOUND_NAMES = {
  horn: "Horn",
  starting: "Start sound",
  brake: "Brake",
  depart: "Departure",
  water: "Water refill",
  steam: "Steam",
};
const IDLE_MINUTES = [1, 5, 10, 15, 30];
// Commands that change who is driving. A pending speed change is dropped if one of these happens.
const DRIVE_ACTIONS = [
  "drive",
  "stop",
  "suspend",
  "source",
  "disconnect",
  "power_off",
];
const capitalize = (text) => text[0].toUpperCase() + text.slice(1);
const minutesSeconds = (seconds) =>
  `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;

function Battery({ level }) {
  const Icon =
    level > 80
      ? BatteryFull
      : level > 55
        ? BatteryHigh
        : level > 30
          ? BatteryMedium
          : level > 10
            ? BatteryLow
            : BatteryEmpty;
  return (
    <span
      className={`battery ${level <= 20 ? "low" : ""}`}
      title="Train battery"
    >
      <Icon size={20} /> {level}%
    </span>
  );
}

export function App() {
  const { state, logs, setLogs, online, error, setError, send } = useBridge();
  const [debugOpen, setDebugOpen] = useState(
    () => readStored("duplo-debug-open") === "1",
  );
  const [speed, setSpeed] = useState(35);
  const [hold, setHold] = useState(false);
  const [busy, setBusy] = useState(false);
  const [address, setAddress] = useState("");
  const [dismissedError, setDismissedError] = useState("");
  const heldPointer = useRef(false);
  const latest = useRef({});
  const speedTimer = useRef(null);
  const driveRevision = useRef(0);

  const train = state?.train || {
    status: "disconnected",
    power: 0,
    light: "off",
  };
  const input = state?.input || {};
  const connected = online && train.status === "connected";
  const working =
    busy || ["scanning", "connecting", "powering_off"].includes(train.status);
  const moving = connected && train.power !== 0;
  const idleMinutes = state?.settings?.idle_minutes ?? 0;
  const idleLeft = state?.settings?.idle_remaining_seconds;

  const perform = useCallback(
    async (action, args) => {
      if (DRIVE_ACTIONS.includes(action)) driveRevision.current += 1;
      try {
        setError("");
        await send(action, args);
        return true;
      } catch (failure) {
        setError(failure.message);
        return false;
      }
    },
    [send, setError],
  );
  const stop = useCallback(() => {
    heldPointer.current = false;
    clearTimeout(speedTimer.current);
    return perform("stop");
  }, [perform]);
  const drive = (sign) => perform("drive", { power: sign * speed });
  latest.current = { connected, train, input, speed, hold, perform, stop };

  useEffect(
    () => setSpeed(state?.input?.max_power ?? 35),
    [state?.input?.max_power],
  );

  // Safety: leaving the window stops the train. Keyboard: arrows drive, Space/Escape stop.
  useEffect(() => {
    const leave = () => {
      heldPointer.current = false;
      clearTimeout(speedTimer.current);
      if (latest.current.connected) latest.current.perform("suspend");
    };
    const hide = () => document.hidden && leave();
    const keyDown = (event) => {
      const current = latest.current;
      if (
        (event.code === "Space" || event.code === "Escape") &&
        current.connected
      ) {
        event.preventDefault();
        current.stop();
        return;
      }
      if (
        event.target instanceof Element &&
        event.target.closest("input,select,textarea")
      )
        return;
      const sign = ["ArrowUp", "ArrowRight"].includes(event.code)
        ? 1
        : ["ArrowDown", "ArrowLeft"].includes(event.code)
          ? -1
          : 0;
      if (sign && current.connected && !event.repeat) {
        event.preventDefault();
        current.perform("drive", { power: sign * current.speed });
      }
    };
    const keyUp = (event) => {
      const current = latest.current;
      if (current.hold && current.connected && event.code.startsWith("Arrow"))
        current.stop();
    };
    const release = () => heldPointer.current && latest.current.stop();
    window.addEventListener("blur", leave);
    window.addEventListener("pagehide", leave);
    document.addEventListener("visibilitychange", hide);
    window.addEventListener("keydown", keyDown);
    window.addEventListener("keyup", keyUp);
    window.addEventListener("pointerup", release);
    window.addEventListener("pointercancel", release);
    return () => {
      clearTimeout(speedTimer.current);
      window.removeEventListener("blur", leave);
      window.removeEventListener("pagehide", leave);
      document.removeEventListener("visibilitychange", hide);
      window.removeEventListener("keydown", keyDown);
      window.removeEventListener("keyup", keyUp);
      window.removeEventListener("pointerup", release);
      window.removeEventListener("pointercancel", release);
    };
  }, []);

  // Drive buttons: a click latches motion, or with "Hold to drive" it moves only while pressed.
  const driveButton = (sign) => ({
    disabled: !connected || speed === 0,
    onPointerDown: (event) => {
      if (hold && event.button === 0) {
        event.currentTarget.setPointerCapture(event.pointerId);
        heldPointer.current = true;
        drive(sign);
      }
    },
    onPointerUp: () => heldPointer.current && stop(),
    onPointerCancel: () => heldPointer.current && stop(),
    onClick: (event) => (!hold || event.detail === 0) && drive(sign),
  });

  // Save the new speed shortly after the slider stops; keep computer-driven motion at the new speed.
  const changeSpeed = (value) => {
    setSpeed(value);
    clearTimeout(speedTimer.current);
    speedTimer.current = setTimeout(async () => {
      const current = latest.current;
      const computerPower =
        current.input.driver === "manual" ? current.train.power : 0;
      const revision = driveRevision.current;
      const saved = await perform("configure", { max_power: value });
      if (
        saved &&
        computerPower &&
        latest.current.connected &&
        revision === driveRevision.current
      )
        await perform("drive", { power: Math.sign(computerPower) * value });
    }, 140);
  };

  const toggleConnection = async () => {
    setBusy(true);
    await perform(connected ? "disconnect" : "connect", { address });
    setBusy(false);
  };

  const turnOff = async () => {
    setBusy(true);
    try {
      await perform("power_off");
    } finally {
      setBusy(false);
    }
  };

  const toggleDebug = () => {
    writeStored("duplo-debug-open", debugOpen ? "0" : "1");
    setDebugOpen(!debugOpen);
  };

  const [statusText, statusTone] = !online
    ? ["App server offline", "error"]
    : train.status === "powering_off"
      ? ["Turning off…", "busy"]
      : train.status === "scanning"
        ? ["Searching…", "busy"]
        : working
          ? [connected ? "Disconnecting…" : "Connecting…", "busy"]
          : connected
            ? ["Connected", "ok"]
            : train.status === "error"
              ? ["Connection failed", "error"]
              : ["Not connected", ""];

  const connectLabel = working
    ? train.status === "powering_off"
      ? "Turning off…"
      : connected
        ? "Disconnecting…"
        : "Connecting…"
    : connected
      ? "Disconnect"
      : train.status === "error"
        ? "Try again"
        : "Connect train";

  const driveState = !connected
    ? "Not connected"
    : moving
      ? `${train.power > 0 ? "Forward" : "Backward"} · ${Math.abs(train.power)}%`
      : "Stopped";

  const currentError = error || train.error || state?.controller?.error;
  const shownError = currentError !== dismissedError ? currentError : "";

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <h1>
            DUPLO Control
            {state?.demo && (
              <span className="demo-badge">DEMO · NO HARDWARE</span>
            )}
          </h1>
          <p>Drive your LEGO DUPLO train from this Mac</p>
        </div>
        <div className="topbar-actions">
          <span className={`status ${statusTone}`} aria-live="polite">
            <span className="dot" aria-hidden="true" />
            {statusText}
            {connected && train.battery != null && (
              <Battery level={train.battery} />
            )}
          </span>
          {(connected || working) && (
            <button
              className="button"
              onClick={toggleConnection}
              disabled={!online || working}
            >
              {working && <ArrowClockwise className="spin" size={16} />}
              {connectLabel}
            </button>
          )}
          <button
            className="button"
            aria-pressed={debugOpen}
            aria-label="Debug log"
            title="Debug log"
            onClick={toggleDebug}
          >
            <Terminal size={17} />
            <span className="wide-only">Debug</span>
          </button>
        </div>
      </header>

      <div className={`layout ${debugOpen ? "with-debug" : ""}`}>
        <main className="main">
          {shownError && (
            <div className="banner" role="alert">
              <WarningCircle size={20} />
              <span>{shownError}</span>
              <button
                className="text-button"
                onClick={() => {
                  setDismissedError(currentError);
                  setError("");
                }}
              >
                Dismiss
              </button>
            </div>
          )}

          {!connected && !working && (
            <section className="card intro" aria-labelledby="intro-title">
              {online ? (
                <>
                  <h2 id="intro-title">Connect your train</h2>
                  <ol>
                    <li>Turn the train on (green button) and keep it near.</li>
                    <li>Close the LEGO DUPLO app on your phone.</li>
                    <li>Click Connect train.</li>
                  </ol>
                  {train.shutdown_reason && (
                    <p className="hint">
                      {train.shutdown_reason}. Press its green button to wake
                      it.
                    </p>
                  )}
                  <button
                    className="button primary large"
                    onClick={toggleConnection}
                  >
                    <Bluetooth size={18} /> {connectLabel}
                  </button>
                  <p className="hint">
                    For safety, the train stops when you leave this window.
                  </p>
                </>
              ) : (
                <>
                  <h2 id="intro-title">Can’t reach the app server</h2>
                  <p className="hint">
                    Trying again… If you stopped the server, start it with{" "}
                    <code>.venv/bin/python app_server.py</code>.
                  </p>
                </>
              )}
            </section>
          )}

          <section className="card drive" aria-labelledby="drive-title">
            <h2 id="drive-title" className="label">
              Drive
            </h2>
            <p
              className={`drive-state ${moving ? "is-moving" : ""} ${connected ? "" : "is-off"}`}
              aria-live="polite"
            >
              {driveState}
              {moving && input.driver === "dji" && <small>by DJI stick</small>}
            </p>
            <div className="speed">
              <label htmlFor="speed" className="label">
                Speed
              </label>
              <input
                id="speed"
                type="range"
                min="0"
                max="100"
                value={speed}
                style={{ "--fill": `${speed}%` }}
                disabled={!online || working}
                aria-valuetext={`${speed}% motor power`}
                onChange={(event) => changeSpeed(Number(event.target.value))}
              />
              <output htmlFor="speed">{speed}%</output>
            </div>
            <div className="drive-buttons">
              <button
                className={`drive-button ${moving && train.power < 0 ? "is-active" : ""}`}
                {...driveButton(-1)}
              >
                <ArrowLeft size={26} weight="bold" /> Backward
              </button>
              <button
                className="drive-button stop"
                disabled={!connected}
                onClick={stop}
              >
                <Square size={20} weight="fill" /> Stop
              </button>
              <button
                className={`drive-button forward ${moving && train.power > 0 ? "is-active" : ""}`}
                {...driveButton(1)}
              >
                Forward <ArrowRight size={26} weight="bold" />
              </button>
            </div>
            <div className="drive-foot">
              <Switch
                checked={hold}
                disabled={!connected}
                title="Move only while a button or arrow key is held down"
                onChange={(next) => {
                  stop();
                  setHold(next);
                }}
              >
                Hold to drive
              </Switch>
              <span className="hint">
                Keys: <kbd>↑</kbd> <kbd>↓</kbd> drive · <kbd>Space</kbd> stop
              </span>
            </div>
          </section>

          <section className="card effects" aria-label="Lights and sounds">
            <div className="effect-group">
              <h2 className="label">Headlight</h2>
              <div
                className="swatches"
                role="group"
                aria-label="Headlight color"
              >
                <button
                  className="swatch-off"
                  aria-pressed={connected && train.light === "off"}
                  disabled={!connected}
                  onClick={() => perform("light", { color: "off" })}
                >
                  Off
                </button>
                {Object.entries(COLORS).map(([name, color]) => (
                  <button
                    key={name}
                    className="swatch"
                    style={{ "--swatch": color }}
                    title={capitalize(name)}
                    aria-label={`${capitalize(name)} headlight`}
                    aria-pressed={connected && train.light === name}
                    disabled={!connected}
                    onClick={() => perform("light", { color: name })}
                  >
                    <span />
                  </button>
                ))}
              </div>
            </div>
            <div className="effect-group">
              <h2 className="label">Sounds</h2>
              <div className="row" role="group" aria-label="Train sounds">
                {(state?.sounds || ["horn"]).map((sound) => (
                  <button
                    key={sound}
                    className="button"
                    disabled={!connected}
                    onClick={() => perform("sound", { sound })}
                  >
                    <Play size={15} weight="fill" />{" "}
                    {SOUND_NAMES[sound] || sound}
                  </button>
                ))}
              </div>
            </div>
          </section>

          <RemoteCard
            state={state}
            online={online}
            connected={connected}
            working={working}
            speed={speed}
            perform={perform}
          />

          <PurpleSound
            train={train}
            connected={connected}
            demo={state?.demo}
            perform={perform}
          />

          <Panel
            title="Power"
            subtitle="Turn off now or when idle"
            status={
              <span className="chip">
                {idleLeft != null
                  ? `Off in ${minutesSeconds(idleLeft)}`
                  : idleMinutes
                    ? `Auto off · ${idleMinutes} min`
                    : "Auto off · never"}
              </span>
            }
          >
            <div className="row">
              <label className="field">
                Turn off after
                <select
                  value={idleMinutes}
                  disabled={!online || working}
                  onChange={(event) =>
                    perform("train_settings", {
                      idle_minutes: Number(event.target.value),
                    })
                  }
                >
                  <option value="0">Never</option>
                  {IDLE_MINUTES.map((minutes) => (
                    <option key={minutes} value={minutes}>
                      {minutes} {minutes === 1 ? "minute" : "minutes"} idle
                    </option>
                  ))}
                </select>
              </label>
              <button
                className="button"
                disabled={!connected || working}
                onClick={turnOff}
              >
                <Power size={17} /> Turn off now
              </button>
            </div>
            <p className="hint">
              Runs only while connected. The train may still go to sleep sooner
              by itself. To wake it, press its green button.
            </p>
          </Panel>
        </main>

        {debugOpen && (
          <DebugPanel
            state={state}
            logs={logs}
            setLogs={setLogs}
            connected={connected}
            address={address}
            setAddress={setAddress}
            perform={perform}
            onClose={toggleDebug}
          />
        )}
      </div>
    </div>
  );
}
