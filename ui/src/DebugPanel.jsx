import { useEffect, useMemo, useRef, useState } from "react";
import {
  CaretDown,
  DownloadSimple,
  Pause,
  Play,
  Trash,
  X,
} from "@phosphor-icons/react";
import { CopyButton } from "./parts.jsx";

const FILTERS = {
  all: "All events",
  train: "Train",
  controller: "DJI remote",
  input: "Input",
  errors: "Errors only",
};
const AXIS_NAMES = {
  left_y: "Left stick ↕",
  left_x: "Left stick ↔",
  right_y: "Right stick ↕",
  right_x: "Right stick ↔",
  left_wheel: "Left wheel",
  right_wheel: "Right wheel",
};
const VALUE_TYPES = ["8-bit", "16-bit", "32-bit", "float"];

const time = (iso) => new Date(iso).toLocaleTimeString([], { hour12: false });

export default function DebugPanel({
  state,
  logs,
  setLogs,
  connected,
  address,
  setAddress,
  perform,
  onClose,
}) {
  const [filter, setFilter] = useState("all");
  const [paused, setPaused] = useState(null);
  const [selected, setSelected] = useState(null);
  const viewport = useRef(null);
  const train = state?.train || {};
  const remote = state?.controller || {};

  const visible = useMemo(
    () =>
      (paused || logs).filter(
        (event) =>
          filter === "all" ||
          (filter === "errors"
            ? event.level === "error"
            : event.source === filter),
      ),
    [logs, paused, filter],
  );

  useEffect(() => {
    if (!paused && !selected && viewport.current)
      viewport.current.scrollTop = viewport.current.scrollHeight;
  }, [visible.length, paused, selected]);

  const exportLogs = () => {
    const data =
      visible.map((event) => JSON.stringify(event)).join("\n") + "\n";
    const url = URL.createObjectURL(
      new Blob([data], { type: "application/x-ndjson" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = "duplo-debug.ndjson";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const details = [
    ["Train", train.name || "Not connected"],
    ["Bluetooth ID", train.address, true],
    [
      "Model",
      train.address
        ? train.hub_type === 0x21
          ? "10427 / 10428"
          : "10874 / 10875"
        : null,
    ],
    ["Firmware", train.firmware],
    ["Signal", train.rssi == null ? null : `${train.rssi} dBm`],
    [
      "Speed sensor",
      train.measured_speed == null ? null : `${train.measured_speed} (raw)`,
    ],
    [
      "Last action brick",
      train.tag_name ? `${train.tag_name} · ${train.tag}` : null,
    ],
    ["Ports", Object.keys(train.ports || {}).join(", ")],
    ["LEGO service", state?.service, true],
  ];

  return (
    <aside className="card debug" aria-labelledby="debug-title">
      <div className="debug-head">
        <h2 id="debug-title">Debug log</h2>
        <button
          className="icon-button"
          aria-label="Close debug log"
          onClick={onClose}
        >
          <X size={18} />
        </button>
      </div>
      <div className="debug-tools">
        <select
          aria-label="Filter events"
          value={filter}
          onChange={(event) => {
            setFilter(event.target.value);
            setSelected(null);
          }}
        >
          {Object.entries(FILTERS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <button
          className="button small"
          aria-pressed={Boolean(paused)}
          onClick={() => setPaused(paused ? null : [...logs])}
        >
          {paused ? <Play size={16} /> : <Pause size={16} />}
          {paused ? "Resume" : "Pause"}
        </button>
        <button
          className="button small"
          onClick={() => {
            setLogs([]);
            if (paused) setPaused([]);
            setSelected(null);
          }}
        >
          <Trash size={16} /> Clear
        </button>
        <button
          className="button small"
          disabled={!visible.length}
          onClick={exportLogs}
        >
          <DownloadSimple size={16} /> Export
        </button>
      </div>

      <div className="log" ref={viewport} aria-label="Debug event log">
        {!visible.length && <p className="hint log-empty">No events yet.</p>}
        {visible.map((event) => (
          <button
            key={event.id}
            className={`log-row ${event.level === "error" ? "is-error" : ""} ${selected?.id === event.id ? "is-selected" : ""}`}
            onClick={() =>
              setSelected(selected?.id === event.id ? null : event)
            }
          >
            <span className={`tag tag-${event.direction.toLowerCase()}`}>
              {event.direction}
            </span>
            <span className="log-text">
              {event.message}
              {event.raw && <code>{event.raw}</code>}
            </span>
            <time>{time(event.time)}</time>
          </button>
        ))}
      </div>

      {selected && (
        <div className="inspector">
          <div className="inspector-head">
            <strong>Selected event</strong>
            <CopyButton
              value={JSON.stringify(selected, null, 2)}
              label="event"
            />
          </div>
          <pre>{JSON.stringify(selected, null, 2)}</pre>
        </div>
      )}

      <details className="details">
        <summary>
          Connection details <CaretDown size={16} className="caret" />
        </summary>
        <dl>
          {details.map(([label, value, copy]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>
                {value || "—"}
                {copy && value && <CopyButton value={value} label={label} />}
              </dd>
            </div>
          ))}
          {remote.status === "connected" &&
            Object.entries(remote.axes || {}).map(([axis, value]) => (
              <div key={axis}>
                <dt>{AXIS_NAMES[axis] || axis}</dt>
                <dd>{value.toFixed(3)}</dd>
              </div>
            ))}
          {remote.status === "connected" && (
            <div>
              <dt>DJI buttons</dt>
              <dd>
                {Object.entries(remote.buttons || {})
                  .filter(([, pressed]) => pressed)
                  .map(([name]) => name)
                  .join(", ") || "None pressed"}
              </dd>
            </div>
          )}
        </dl>
        <button
          className="button small"
          disabled={!connected || state?.demo}
          onClick={() => perform("inspect")}
        >
          Inspect train capabilities
        </button>
        {Object.entries(train.modes || {}).map(([port, info]) => (
          <p className="hint" key={port}>
            Port {port}:{" "}
            {Object.entries(info.names || {})
              .map(([mode, name]) => {
                const format = train.mode_formats?.[port]?.[mode];
                return `${mode} ${name}${format ? ` (${format.datasets} × ${VALUE_TYPES[format.type] || "unknown"})` : ""}`;
              })
              .join(" · ") || `${info.count ?? "?"} modes`}
          </p>
        ))}
        {train.bluetooth_services?.map((service) => (
          <p className="hint" key={service.uuid}>
            <code>{service.uuid}</code>
            {service.characteristics.map((characteristic) => (
              <span key={characteristic.uuid}>
                <br />
                <code>{characteristic.uuid}</code> ·{" "}
                {characteristic.properties.join(", ")} · write size{" "}
                {characteristic.max_write_without_response} bytes
              </span>
            ))}
          </p>
        ))}
        <label className="field">
          Train identifier (only if several trains are nearby)
          <input
            value={address}
            onChange={(event) => setAddress(event.target.value)}
            placeholder="Leave empty to use the first train found"
          />
        </label>
      </details>
    </aside>
  );
}
