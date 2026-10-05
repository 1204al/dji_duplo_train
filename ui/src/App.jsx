import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  ArrowClockwise,
  BatteryFull,
  Bluetooth,
  CaretRight,
  CaretDown,
  CellSignalHigh,
  Check,
  Circle,
  Copy,
  DownloadSimple,
  GameController,
  Info,
  Pause,
  Play,
  Power,
  Plug,
  Square,
  ToggleLeft,
  ToggleRight,
  Trash,
  WarningCircle,
} from "@phosphor-icons/react";
import PurpleSound from "./PurpleSound.jsx";

const COLORS = [
  ["white", "#eceef2"],
  ["yellow", "#f6e200"],
  ["red", "#f02828"],
  ["green", "#18e67b"],
  ["blue", "#176af4"],
  ["purple", "#a83be4"],
];
const SOUND_NAMES = {
  horn: "Train horn",
  starting: "Starting sound",
  brake: "Brake",
  depart: "Station departure",
  water: "Water refill",
  steam: "Steam",
};
const AXIS_NAMES = {
  left_y: "Left stick · vertical",
  left_x: "Left stick · horizontal",
  right_y: "Right stick · vertical",
  right_x: "Right stick · horizontal",
  left_wheel: "Left wheel",
  right_wheel: "Right wheel",
};
const BUTTON_NAMES = {
  rth_pause: "Pause / RTH",
  c1: "C1",
  c2: "C2",
  record: "Record",
  camera_half: "Shutter · half",
  camera_full: "Shutter · full",
};

function useBridge() {
  const [state, setState] = useState(null),
    [logs, setLogs] = useState([]),
    [online, setOnline] = useState(false),
    [error, setError] = useState("");
  const socket = useRef(null),
    requests = useRef(new Map()),
    counter = useRef(0);
  useEffect(() => {
    let active = true,
      retry;
    function connect() {
      const ws = new WebSocket(
        `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws`,
      );
      socket.current = ws;
      ws.onopen = () => {
        if (active) setOnline(true);
      };
      ws.onmessage = (event) => {
        if (!active) return;
        const data = JSON.parse(event.data);
        if (data.state) setState(data.state);
        if (data.type === "snapshot") setLogs(data.logs || []);
        else if (data.logs?.length)
          setLogs((previous) => [...previous, ...data.logs].slice(-1000));
        if (data.type === "result") {
          const pending = requests.current.get(data.id);
          if (pending) {
            clearTimeout(pending.timer);
            requests.current.delete(data.id);
            data.ok
              ? pending.resolve()
              : pending.reject(new Error(data.error || "Command failed."));
          }
          if (!data.ok) setError(data.error || "Command failed.");
        }
      };
      ws.onclose = () => {
        if (!active) return;
        setOnline(false);
        for (const pending of requests.current.values()) {
          clearTimeout(pending.timer);
          pending.reject(new Error("Connection to the local app was lost."));
        }
        requests.current.clear();
        retry = setTimeout(connect, 1500);
      };
      ws.onerror = () => ws.close();
    }
    connect();
    const heartbeat = setInterval(() => {
      if (socket.current?.readyState === WebSocket.OPEN)
        socket.current.send(JSON.stringify({ type: "heartbeat" }));
    }, 400);
    return () => {
      active = false;
      clearInterval(heartbeat);
      clearTimeout(retry);
      socket.current?.close();
      for (const pending of requests.current.values()) {
        clearTimeout(pending.timer);
        pending.reject(new Error("App closed."));
      }
      requests.current.clear();
    };
  }, []);
  const send = useCallback((action, args = {}) => {
    if (socket.current?.readyState !== WebSocket.OPEN)
      return Promise.reject(
        new Error("The local server is reconnecting. Please wait."),
      );
    const id = ++counter.current;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => {
          requests.current.delete(id);
          reject(new Error("Command timed out. Check Debug for details."));
        },
        action === "connect" ? 35000 : 10000,
      );
      requests.current.set(id, { resolve, reject, timer });
      socket.current.send(JSON.stringify({ id, action, args }));
    });
  }, []);
  return { state, logs, setLogs, online, error, setError, send };
}

function CopyButton({ value, label = "Copy" }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="copy-button"
      title={`Copy ${label.toLowerCase()}`}
      aria-label={`Copy ${label.toLowerCase()}`}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(String(value));
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch {
          setCopied(false);
        }
      }}
    >
      {copied ? <Check size={16} /> : <Copy size={16} />}
    </button>
  );
}

export function App() {
  const { state, logs, setLogs, online, error, setError, send } = useBridge();
  const [debugVisible, setDebugVisible] = useState(true),
    [settingsOpen, setSettingsOpen] = useState(false),
    [detailsOpen, setDetailsOpen] = useState(false),
    [dismissedError, setDismissedError] = useState("");
  const [speed, setSpeed] = useState(35),
    [hold, setHold] = useState(false),
    [filter, setFilter] = useState("all");
  const [pausedLogs, setPausedLogs] = useState(null),
    [selectedLog, setSelectedLog] = useState(null),
    [busy, setBusy] = useState(false);
  const [ip, setIp] = useState(
      () => localStorage.getItem("duplo-remote-ip") || "",
    ),
    [address, setAddress] = useState("");
  const [mapping, setMapping] = useState({
    axis: "left_y",
    invert: false,
    deadzone: 0.08,
    stop_button: "rth_pause",
    horn_button: "c1",
  });
  const logViewport = useRef(null),
    heldPointer = useRef(false),
    latest = useRef({}),
    speedTimer = useRef(null);
  const train = state?.train || {
      status: "disconnected",
      power: 0,
      light: "off",
    },
    remote = state?.controller || {
      status: "disconnected",
      axes: {},
      buttons: {},
    };
  const source = state?.input?.source || "manual",
    connected = online && train.status === "connected",
    manual = connected && source === "manual";
  const working =
      busy || ["scanning", "connecting", "powering_off"].includes(train.status),
    moving = train.power !== 0;
  const direction = !online
    ? "Offline"
    : train.status === "powering_off"
      ? "Turning off…"
      : !connected
        ? "Not connected"
        : moving
          ? train.power > 0
            ? "Forward"
            : "Backward"
          : "Stopped";
  const perform = useCallback(
    async (action, args) => {
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
    return perform("stop");
  }, [perform]);
  latest.current = { connected, manual, train, speed, hold, perform, stop };
  useEffect(() => {
    setSpeed(state?.input?.max_power ?? 35);
  }, [state?.input?.max_power]);
  useEffect(() => {
    if (state?.input) {
      const { axis, invert, deadzone, stop_button, horn_button } = state.input;
      setMapping({ axis, invert, deadzone, stop_button, horn_button });
    }
  }, [
    state?.input?.axis,
    state?.input?.invert,
    state?.input?.deadzone,
    state?.input?.stop_button,
    state?.input?.horn_button,
  ]);
  useEffect(() => {
    const exit = () => {
      heldPointer.current = false;
      if (latest.current.connected) latest.current.stop();
    };
    const hide = () => {
      if (document.hidden) exit();
    };
    const down = (event) => {
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
        event.target.closest('input,select,textarea,[contenteditable="true"]')
      )
        return;
      const sign = ["ArrowUp", "ArrowRight"].includes(event.code)
        ? 1
        : ["ArrowDown", "ArrowLeft"].includes(event.code)
          ? -1
          : 0;
      if (sign && current.manual && !event.repeat) {
        event.preventDefault();
        current.perform("drive", { power: sign * current.speed });
      }
    };
    const up = (event) => {
      if (
        latest.current.hold &&
        event.code.startsWith("Arrow") &&
        latest.current.manual
      )
        latest.current.stop();
    };
    const release = () => {
      if (heldPointer.current) latest.current.stop();
    };
    window.addEventListener("blur", exit);
    document.addEventListener("visibilitychange", hide);
    window.addEventListener("pagehide", exit);
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    window.addEventListener("pointerup", release);
    window.addEventListener("pointercancel", release);
    return () => {
      window.removeEventListener("blur", exit);
      document.removeEventListener("visibilitychange", hide);
      window.removeEventListener("pagehide", exit);
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
      window.removeEventListener("pointerup", release);
      window.removeEventListener("pointercancel", release);
    };
  }, []);
  useEffect(() => () => clearTimeout(speedTimer.current), []);
  const visibleLogs = useMemo(
    () =>
      (pausedLogs || logs).filter(
        (event) =>
          filter === "all" ||
          (filter === "errors"
            ? event.level === "error"
            : event.source === filter),
      ),
    [logs, filter, pausedLogs],
  );
  useEffect(() => {
    if (!pausedLogs && !selectedLog && logViewport.current)
      logViewport.current.scrollTop = logViewport.current.scrollHeight;
  }, [visibleLogs.length, pausedLogs, selectedLog]);
  const driveProps = (sign) => ({
    disabled: !manual || speed === 0,
    onPointerDown: (event) => {
      if (hold && event.button === 0) {
        event.currentTarget.setPointerCapture(event.pointerId);
        heldPointer.current = true;
        perform("drive", { power: sign * speed });
      }
    },
    onPointerUp: () => {
      if (heldPointer.current) stop();
    },
    onPointerCancel: () => {
      if (heldPointer.current) stop();
    },
    onClick: (event) => {
      if (!hold || event.detail === 0)
        perform("drive", { power: sign * speed });
    },
  });
  const changeSpeed = (value) => {
    setSpeed(value);
    clearTimeout(speedTimer.current);
    speedTimer.current = setTimeout(async () => {
      await perform("configure", { max_power: value });
      if (latest.current.manual && latest.current.train.power)
        await perform("drive", {
          power: Math.sign(latest.current.train.power) * value,
        });
    }, 140);
  };
  const exportLogs = () => {
    const data =
      visibleLogs.map((event) => JSON.stringify(event)).join("\n") + "\n";
    const url = URL.createObjectURL(
      new Blob([data], { type: "application/x-ndjson" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = "duplo-debug.ndjson";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const connect = async () => {
    setBusy(true);
    await perform(connected ? "disconnect" : "connect", { address });
    setBusy(false);
  };
  const statusLabel = !online
    ? "Reconnecting to app"
    : train.status === "powering_off"
      ? "Turning off…"
      : working
        ? train.status === "scanning"
          ? "Searching…"
          : connected
            ? "Disconnecting…"
            : "Connecting…"
        : connected
          ? "Connected"
          : train.status === "error"
            ? "Connection failed"
            : "Not connected";
  const currentError = error || train.error || remote.error;
  const displayedError = currentError !== dismissedError ? currentError : "";

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <h1>DUPLO Control</h1>
          <span className="app-label">
            {state?.demo ? "DEMO · NO HARDWARE" : "LOCAL CONTROLLER"}
          </span>
        </div>
        <div className="connection-bar" aria-live="polite">
          <span
            className={`connection-status ${connected ? "is-connected" : working ? "is-working" : ""}`}
          >
            <Circle size={13} weight="fill" />
            {statusLabel}
          </span>
          <span className="header-battery">
            <BatteryFull size={26} />
            <span>
              Battery{" "}
              <strong>
                {train.battery == null ? "—" : `${train.battery}%`}
              </strong>
            </span>
          </span>
          <button
            className={`button connection-button ${!connected ? "primary-quiet" : ""}`}
            onClick={connect}
            disabled={!online || working}
          >
            {working ? (
              <ArrowClockwise className="spin" size={17} />
            ) : connected ? null : (
              <Bluetooth size={17} />
            )}{" "}
            {working
              ? train.status === "powering_off"
                ? "Turning off"
                : connected
                  ? "Disconnecting"
                  : "Connecting"
              : connected
                ? "Disconnect"
                : train.status === "error"
                  ? "Retry connection"
                  : "Connect train"}
          </button>
        </div>
      </header>
      <div className="input-bar">
        <div className="input-select">
          <label htmlFor="input-source">Input source</label>
          <select
            id="input-source"
            value={source}
            disabled={!online || working}
            onChange={(event) =>
              perform("source", { source: event.target.value })
            }
          >
            <option value="manual">Keyboard & buttons</option>
            <option
              value="dji"
              disabled={remote.status !== "connected" || !connected}
            >
              DJI remote
            </option>
          </select>
        </div>
        <span className="remote-summary">
          DJI remote:{" "}
          {remote.status === "connected"
            ? "connected"
            : ["connecting", "waiting"].includes(remote.status)
              ? "connecting…"
              : remote.status === "error"
                ? "connection failed"
                : "not configured"}
        </span>
        <button
          className="disclosure controller-disclosure"
          aria-expanded={settingsOpen}
          aria-controls="controller-settings"
          onClick={() => setSettingsOpen(!settingsOpen)}
        >
          {settingsOpen ? <CaretDown size={20} /> : <CaretRight size={20} />}
          Controller settings
        </button>
        {!debugVisible && (
          <button
            className="button show-debug"
            onClick={() => setDebugVisible(true)}
          >
            Show debug
          </button>
        )}
      </div>
      {displayedError && (
        <div className="error-banner" role="alert">
          <WarningCircle size={20} />
          <span>{displayedError}</span>
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
      {settingsOpen && (
        <section
          className="controller-settings"
          id="controller-settings"
          aria-label="Controller settings"
        >
          <div className="settings-heading">
            <GameController size={21} />
            <h2>DJI controller</h2>
            <span className="muted">Wi-Fi input · DJI RC (RM330) layout</span>
          </div>
          <p className="settings-note">
            Connect a compatible screen controller to the same Wi-Fi. Verify its
            inputs here before using it to drive.
          </p>
          <div className="settings-grid">
            <label>
              Controller IP
              <input
                value={ip}
                onChange={(event) => setIp(event.target.value)}
                placeholder="192.168.1.123"
                inputMode="decimal"
              />
            </label>
            <button
              className="button"
              disabled={
                !online ||
                !ip ||
                ["connecting", "waiting"].includes(remote.status)
              }
              onClick={async () => {
                if (
                  await perform(
                    remote.status === "connected"
                      ? "remote_disconnect"
                      : "remote_connect",
                    { ip },
                  )
                )
                  localStorage.setItem("duplo-remote-ip", ip);
              }}
            >
              <Plug size={17} />
              {remote.status === "connected"
                ? "Disconnect remote"
                : "Connect remote"}
            </button>
            <label>
              Drive axis
              <select
                value={mapping.axis}
                onChange={(event) =>
                  setMapping({ ...mapping, axis: event.target.value })
                }
              >
                {Object.entries(AXIS_NAMES).map(([key, name]) => (
                  <option key={key} value={key}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Dead zone
              <select
                value={mapping.deadzone}
                onChange={(event) =>
                  setMapping({
                    ...mapping,
                    deadzone: Number(event.target.value),
                  })
                }
              >
                {[0.04, 0.08, 0.12, 0.2].map((value) => (
                  <option key={value} value={value}>
                    {Math.round(value * 100)}%
                  </option>
                ))}
              </select>
            </label>
            <label>
              Stop button
              <select
                value={mapping.stop_button}
                onChange={(event) =>
                  setMapping({ ...mapping, stop_button: event.target.value })
                }
              >
                {Object.entries(BUTTON_NAMES).map(([key, name]) => (
                  <option key={key} value={key}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Horn button
              <select
                value={mapping.horn_button}
                onChange={(event) =>
                  setMapping({ ...mapping, horn_button: event.target.value })
                }
              >
                {Object.entries(BUTTON_NAMES).map(([key, name]) => (
                  <option key={key} value={key}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={mapping.invert}
                onChange={(event) =>
                  setMapping({ ...mapping, invert: event.target.checked })
                }
              />
              Invert axis
            </label>
            <div className="settings-actions">
              <button
                className="button"
                disabled={!online}
                onClick={() => perform("configure", mapping)}
              >
                Apply mapping
              </button>
              <button
                className="button"
                disabled={remote.status !== "connected"}
                title="Release the selected stick first"
                onClick={() => perform("calibrate")}
              >
                Center axis
              </button>
            </div>
          </div>
          {remote.status === "connected" && (
            <div className="axis-readings">
              {Object.entries(remote.axes).map(([axis, value]) => (
                <span key={axis}>
                  <span>{AXIS_NAMES[axis]}</span>
                  <strong>{value.toFixed(3)}</strong>
                </span>
              ))}
              <span>
                <span>Buttons</span>
                <strong>
                  {Object.entries(remote.buttons)
                    .filter(([, value]) => value)
                    .map(([key]) => BUTTON_NAMES[key])
                    .join(", ") || "None pressed"}
                </strong>
              </span>
            </div>
          )}
        </section>
      )}
      <main className={`workspace ${!debugVisible ? "debug-hidden" : ""}`}>
        <section className="train-controls" aria-labelledby="controls-title">
          <h2 id="controls-title">Train controls</h2>
          <div className="motion-status">
            <span className="eyebrow">Status</span>
            <div className={moving ? "is-moving" : ""}>{direction}</div>
            {!connected && (
              <p>
                {train.shutdown_reason
                  ? `${train.shutdown_reason}. Press its green button to wake it, then connect.`
                  : "Switch the train on nearby, then connect."}
              </p>
            )}
            {source === "dji" && (
              <p>
                {state?.input.neutral_required
                  ? "Center the controller stick to begin."
                  : "DJI remote is controlling the train."}
              </p>
            )}
          </div>
          <div className="drive-section">
            <div className="speed-caption">
              <label htmlFor="speed">Speed setting</label>
              <strong>{speed}%</strong>
              {source === "dji" && <span className="muted">maximum power</span>}
            </div>
            <div className="speed-slider">
              <span>0</span>
              <input
                id="speed"
                style={{ "--range-progress": `${speed}%` }}
                type="range"
                min="0"
                max="100"
                step="1"
                value={speed}
                disabled={!online || working}
                aria-valuetext={`${speed}% motor power`}
                onChange={(event) => changeSpeed(Number(event.target.value))}
              />
              <span>100</span>
            </div>
            <div className="drive-buttons">
              <button
                className={`drive-button reverse ${train.power < 0 ? "active-drive" : ""}`}
                {...driveProps(-1)}
              >
                <ArrowLeft size={29} weight="bold" />
                Backward
              </button>
              <button
                className="drive-button stop"
                disabled={!connected}
                onClick={stop}
              >
                <Square size={22} weight="fill" />
                Stop
              </button>
              <button
                className={`drive-button forward ${train.power > 0 ? "active-drive" : ""}`}
                {...driveProps(1)}
              >
                <ArrowRight size={29} weight="bold" />
                Forward
              </button>
            </div>
            <div className="drive-options">
              <button
                className="hold-switch"
                role="switch"
                aria-checked={hold}
                disabled={!manual}
                onClick={() => {
                  stop();
                  setHold(!hold);
                }}
              >
                {hold ? (
                  <ToggleRight size={42} weight="fill" />
                ) : (
                  <ToggleLeft size={42} weight="fill" />
                )}
                <span>Hold to drive</span>
              </button>
              <span className="keyboard-hint">
                <kbd>Space</kbd>
                <span>to stop</span>
              </span>
            </div>
          </div>
          <div className="effects-section">
            <div className="headlight">
              <h3>Headlight</h3>
              <div
                className="color-options"
                role="group"
                aria-label="Headlight color"
              >
                <button
                  className={`color-off ${train.light === "off" ? "selected" : ""}`}
                  aria-pressed={train.light === "off"}
                  disabled={!connected}
                  onClick={() => perform("light", { color: "off" })}
                >
                  Off
                </button>
                {COLORS.map(([name, color]) => (
                  <button
                    className={`color-choice ${train.light === name ? "selected" : ""}`}
                    key={name}
                    aria-label={`${name[0].toUpperCase() + name.slice(1)} headlight`}
                    aria-pressed={train.light === name}
                    disabled={!connected}
                    onClick={() => perform("light", { color: name })}
                  >
                    <Circle color={color} size={28} weight="fill" />
                    <span>{name[0].toUpperCase() + name.slice(1)}</span>
                  </button>
                ))}
              </div>
            </div>
            <div className="sound">
              <h3>Sounds</h3>
              <div
                className="sound-buttons"
                role="group"
                aria-label="Train sounds"
              >
                {(state?.sounds || ["horn"]).map((sound) => (
                  <button
                    key={sound}
                    className="button sound-button"
                    disabled={!connected}
                    onClick={() => perform("sound", { sound })}
                  >
                    <Play size={18} weight="fill" />
                    {SOUND_NAMES[sound]}
                  </button>
                ))}
              </div>
              {train.hub_type !== 0x20 && (
                <p className="sound-note">
                  Other built-in sounds aren’t available in this controller yet.
                </p>
              )}
            </div>
          </div>
          <details className="train-feature" open>
            <summary>
              <span>Purple brick sound</span>
              <span className="feature-badge">Audio on Mac</span>
              <CaretDown size={18} />
            </summary>
            <PurpleSound
              train={train}
              connected={connected}
              demo={state?.demo}
              perform={perform}
            />
          </details>
          <details className="train-feature" open>
            <summary>
              <span>Power & sleep</span>
              <CaretDown size={18} />
            </summary>
            <div className="power-settings">
              <div className="power-setting-row">
                <label htmlFor="idle-time">
                  Turn off after idle
                  <select
                    id="idle-time"
                    aria-label="Turn off after idle"
                    value={state?.settings?.idle_minutes ?? 0}
                    disabled={!online || working}
                    onChange={(event) =>
                      perform("train_settings", {
                        idle_minutes: Number(event.target.value),
                      })
                    }
                  >
                    <option value="0">Disabled</option>
                    {[1, 5, 10, 15, 30].map((minutes) => (
                      <option key={minutes} value={minutes}>
                        {minutes} {minutes === 1 ? "minute" : "minutes"}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  className="button power-off-button"
                  disabled={!connected || working}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      await perform("power_off");
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  <Power size={18} />
                  Turn off train
                </button>
              </div>
              <p className="feature-note">
                {state?.settings?.idle_remaining_seconds != null
                  ? `Turns off in ${Math.floor(state.settings.idle_remaining_seconds / 60)}:${String(state.settings.idle_remaining_seconds % 60).padStart(2, "0")} if it stays idle. `
                  : ""}
                This app’s timer runs while the train is connected to the local
                server. Driving or a detected action brick resets it.
              </p>
              <p className="feature-note">
                LEGO’s built-in snooze setting isn’t available here yet and may
                turn it off sooner. After power off, press its green button to
                wake it.
              </p>
            </div>
          </details>
          <div className="controls-footnote">
            <Info size={15} />
            <span>
              {connected
                ? "Arrow keys to drive. Leaving this window stops the train."
                : "Close the LEGO phone app before connecting."}
            </span>
          </div>
        </section>
        {debugVisible && (
          <aside className="debug-panel" aria-labelledby="debug-title">
            <div className="debug-heading">
              <h2 id="debug-title">Debug</h2>
              <button
                className="button hide-debug"
                onClick={() => setDebugVisible(false)}
              >
                Hide
              </button>
            </div>
            <div className="debug-filter">
              <label htmlFor="log-filter">Show</label>
              <select
                id="log-filter"
                value={filter}
                onChange={(event) => {
                  setFilter(event.target.value);
                  setSelectedLog(null);
                }}
              >
                <option value="all">All events</option>
                <option value="train">Train messages</option>
                <option value="controller">DJI controller</option>
                <option value="input">Input changes</option>
                <option value="errors">Errors only</option>
              </select>
              <span className="signal-reading">
                Signal{" "}
                <strong>
                  {train.rssi == null ? "—" : `${train.rssi} dBm`}
                </strong>
                <CellSignalHigh size={20} />
              </span>
            </div>
            <div className="debug-tools">
              <button
                className={`button ${pausedLogs ? "is-selected" : ""}`}
                onClick={() => setPausedLogs(pausedLogs ? null : [...logs])}
              >
                {pausedLogs ? <Play size={18} /> : <Pause size={18} />}{" "}
                {pausedLogs ? "Resume" : "Pause"}
              </button>
              <button
                className="button"
                title="Clear events in this window"
                onClick={() => {
                  setLogs([]);
                  if (pausedLogs) setPausedLogs([]);
                  setSelectedLog(null);
                }}
              >
                <Trash size={18} />
                Clear
              </button>
              <button
                className="button"
                disabled={!visibleLogs.length}
                onClick={exportLogs}
              >
                <DownloadSimple size={18} />
                Export
              </button>
            </div>
            <div
              className="debug-logs"
              ref={logViewport}
              aria-label="Debug event log"
            >
              {!visibleLogs.length && (
                <div className="empty-log">
                  <Info size={22} />
                  <p>
                    {filter === "errors" ? "No errors" : "No matching events"}
                  </p>
                  <span>
                    {filter === "all"
                      ? "Connection and command messages will appear here."
                      : "Choose All events to see the full log."}
                  </span>
                </div>
              )}
              {visibleLogs.map((event) => (
                <button
                  key={event.id}
                  className={`log-entry ${event.level === "error" ? "log-error" : ""} ${selectedLog?.id === event.id ? "log-selected" : ""}`}
                  onClick={() =>
                    setSelectedLog(selectedLog?.id === event.id ? null : event)
                  }
                >
                  <span
                    className={`log-badge badge-${event.direction.toLowerCase()}`}
                  >
                    {event.direction}
                  </span>
                  <span className="log-content">
                    <span className="log-message">
                      {event.direction === "TX"
                        ? "Sent — "
                        : event.direction === "RX"
                          ? "Received — "
                          : ""}
                      {event.message}
                    </span>
                    {event.raw && <code>{event.raw}</code>}
                  </span>
                  <time>
                    {new Date(event.time).toLocaleTimeString([], {
                      hour12: false,
                    })}
                  </time>
                </button>
              ))}
            </div>
            {selectedLog && (
              <div className="message-inspector">
                <div>
                  <strong>Selected event</strong>
                  <CopyButton
                    value={JSON.stringify(selectedLog, null, 2)}
                    label="event"
                  />
                </div>
                <span>
                  {selectedLog.source} ·{" "}
                  {new Date(selectedLog.time).toLocaleString()}
                </span>
                <p>{selectedLog.message}</p>
                {selectedLog.raw && <code>{selectedLog.raw}</code>}
              </div>
            )}
            <div className="connection-details">
              <button
                className="disclosure"
                aria-expanded={detailsOpen}
                aria-controls="connection-details"
                onClick={() => setDetailsOpen(!detailsOpen)}
              >
                {detailsOpen ? (
                  <CaretDown size={20} />
                ) : (
                  <CaretRight size={20} />
                )}
                Connection details
              </button>
              {detailsOpen && (
                <div id="connection-details" className="details-content">
                  <dl>
                    <dt>Train</dt>
                    <dd>{train.name || "Not connected"}</dd>
                    <dt>Bluetooth ID</dt>
                    <dd className="copy-value">
                      <code>{train.address || "—"}</code>
                      {train.address && (
                        <CopyButton
                          value={train.address}
                          label="Bluetooth identifier"
                        />
                      )}
                    </dd>
                    <dt>Hub type</dt>
                    <dd>
                      {train.address
                        ? `0x${train.hub_type.toString(16)} · ${train.hub_type === 33 ? "10427 / 10428" : "10874 / 10875"}`
                        : "—"}
                    </dd>
                    <dt>Firmware</dt>
                    <dd>{train.firmware || "—"}</dd>
                    <dt>LEGO service</dt>
                    <dd className="copy-value">
                      <code>{state?.service || "—"}</code>
                      {state?.service && (
                        <CopyButton
                          value={state.service}
                          label="service UUID"
                        />
                      )}
                    </dd>
                    <dt>Speed sensor</dt>
                    <dd>
                      {train.measured_speed == null
                        ? "Waiting for a reading"
                        : `${train.measured_speed} (raw)`}
                    </dd>
                    <dt>Last action brick</dt>
                    <dd>
                      {train.tag_name
                        ? `${train.tag_name} · ${train.tag}`
                        : "Not seen yet"}
                    </dd>
                    <dt>Attached ports</dt>
                    <dd>{Object.keys(train.ports || {}).join(", ") || "—"}</dd>
                  </dl>
                  <button
                    className="button"
                    disabled={!connected || state?.demo}
                    onClick={() => perform("inspect")}
                  >
                    Inspect train capabilities
                  </button>
                  {Object.entries(train.modes || {}).map(([port, info]) => (
                    <p className="feature-note" key={port}>
                      Port {port}:{" "}
                      {Object.entries(info.names || {})
                        .map(([mode, name]) => {
                          const format = train.mode_formats?.[port]?.[mode];
                          const type = ["8-bit", "16-bit", "32-bit", "float"][
                            format?.type
                          ];
                          return `${mode} ${name}${format ? ` (${format.datasets} × ${type || "unknown"})` : ""}`;
                        })
                        .join(" · ") || `${info.count ?? "?"} modes`}
                    </p>
                  ))}
                  {train.bluetooth_services?.length > 0 && (
                    <details className="bluetooth-services">
                      <summary>Bluetooth services</summary>
                      {train.bluetooth_services.map((service) => (
                        <div key={service.uuid}>
                          <code>{service.uuid}</code>
                          {service.characteristics.map((characteristic) => (
                            <p
                              className="feature-note"
                              key={characteristic.uuid}
                            >
                              <code>{characteristic.uuid}</code>
                              <br />
                              {characteristic.properties.join(", ")} · write
                              size {characteristic.max_write_without_response}{" "}
                              bytes
                            </p>
                          ))}
                        </div>
                      ))}
                    </details>
                  )}
                  <label className="address-field">
                    Optional train identifier
                    <input
                      value={address}
                      onChange={(event) => setAddress(event.target.value)}
                      placeholder="Only needed if several trains are nearby"
                    />
                  </label>
                </div>
              )}
            </div>
          </aside>
        )}
      </main>
    </div>
  );
}
