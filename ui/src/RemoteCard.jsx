import { useState } from "react";
import { Plug } from "@phosphor-icons/react";
import { Panel, Switch, readStored, writeStored } from "./parts.jsx";

const DEADZONES = [0.04, 0.08, 0.12, 0.2];

// Optional DJI RC (RM330) over Wi-Fi. Only the left stick's up/down drives;
// the server does the mapping, safety checks and hand-over with the computer.
export default function RemoteCard({
  state,
  online,
  connected,
  working,
  speed,
  perform,
}) {
  const [ip, setIp] = useState(() => readStored("duplo-remote-ip"));
  const remote = state?.controller || { status: "disconnected" };
  const input = state?.input || {};
  const stick = state?.left_stick || { available: false };
  const enabled = input.left_stick_enabled ?? true;
  const linked = ["connecting", "waiting", "connected"].includes(remote.status);
  const position = stick.available ? stick.position : 0;
  const power = stick.available ? stick.power : 0;
  const powerDirection = power > 0 ? "forward" : power < 0 ? "backward" : "";

  const [statusText, statusTone] =
    remote.status === "connected"
      ? ["Connected", "ok"]
      : linked
        ? ["Connecting…", "busy"]
        : remote.status === "error"
          ? ["Failed", "error"]
          : ["Not connected", ""];

  const ready = connected && stick.available && enabled;
  const hint = !connected
    ? "Connect the train first."
    : !stick.available
      ? "Waiting for the stick…"
      : !enabled
        ? "Stick driving is off. The buttons and keys still work."
        : input.manual_override
          ? "The buttons are driving. Center the stick to take over."
          : input.source !== "dji" || input.neutral_required
            ? "Center the stick to start."
            : input.driver === "dji"
              ? "Driving with the stick."
              : "Ready. Push the stick up to go forward.";

  const toggleLink = async () => {
    const action = linked ? "remote_disconnect" : "remote_connect";
    if (await perform(action, { ip })) writeStored("duplo-remote-ip", ip);
  };

  return (
    <Panel
      title="DJI remote"
      subtitle="Drive with the left stick of a DJI RC"
      status={<span className={`chip ${statusTone}`}>{statusText}</span>}
      defaultOpen={Boolean(ip)}
    >
      <p className="hint">
        Put a DJI RC (RM330) on the same Wi-Fi as this Mac. Left stick up is
        forward, down is backward. The other sticks and buttons do nothing.
      </p>
      <div className="row">
        <label className="field grow">
          Remote’s IP address (see its Wi-Fi settings)
          <input
            value={ip}
            onChange={(event) => setIp(event.target.value)}
            placeholder="192.168.1.123"
            inputMode="decimal"
            disabled={linked}
          />
        </label>
        <button
          className="button"
          disabled={!online || (!linked && !ip)}
          onClick={toggleLink}
        >
          <Plug size={17} />
          {linked ? "Disconnect" : "Connect"}
        </button>
      </div>
      {linked && (
        <>
          <div className="stick">
            <Switch
              checked={enabled}
              disabled={
                !online ||
                working ||
                (!enabled && !(connected && stick.available))
              }
              onChange={(next) =>
                perform("source", { source: next ? "dji" : "manual" })
              }
            >
              Drive with left stick
            </Switch>
            <div
              className="meter"
              role="meter"
              aria-label="Left stick position"
              aria-valuemin={-100}
              aria-valuemax={100}
              aria-valuenow={Math.round(position * 100)}
            >
              <span
                style={{
                  left: `${Math.min(50, 50 + position * 50)}%`,
                  width: `${Math.abs(position) * 50}%`,
                }}
              />
            </div>
            <span className="stick-power">
              {stick.available ? `${Math.abs(power)}% ${powerDirection}` : "—"}
            </span>
          </div>
          <p className="hint" aria-live="polite">
            {hint}
            {ready && ` Full stick = ${speed}% (the Speed setting).`}
          </p>
          <div className="row">
            <label className="check small">
              <input
                type="checkbox"
                checked={input.invert ?? true}
                disabled={!online}
                onChange={(event) =>
                  perform("configure", { invert: event.target.checked })
                }
              />
              Invert up/down
            </label>
            <label
              className="field inline"
              title="How far the stick must move before the train starts"
            >
              Dead zone
              <select
                value={input.deadzone ?? 0.08}
                disabled={!online}
                onChange={(event) =>
                  perform("configure", { deadzone: Number(event.target.value) })
                }
              >
                {DEADZONES.map((value) => (
                  <option key={value} value={value}>
                    {Math.round(value * 100)}%
                  </option>
                ))}
              </select>
            </label>
            <button
              className="button small"
              disabled={remote.status !== "connected"}
              title="Let go of the stick first"
              onClick={() => perform("calibrate")}
            >
              Recenter stick
            </button>
          </div>
        </>
      )}
    </Panel>
  );
}
