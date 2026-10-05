import { useCallback, useEffect, useRef, useState } from "react";

// WebSocket link to the local Python server (app_server.py).
// The server owns Bluetooth and the DJI listener; this hook only sends
// commands, receives state, and sends a heartbeat so the server can stop
// the train when this window goes away.
export function useBridge() {
  const [state, setState] = useState(null);
  const [logs, setLogs] = useState([]);
  const [online, setOnline] = useState(false);
  const [error, setError] = useState("");
  const socket = useRef(null);
  const requests = useRef(new Map());
  const counter = useRef(0);

  useEffect(() => {
    let active = true;
    let retry;

    const rejectPending = (message) => {
      for (const pending of requests.current.values()) {
        clearTimeout(pending.timer);
        pending.reject(new Error(message));
      }
      requests.current.clear();
    };

    const heartbeat = () => {
      if (socket.current?.readyState === WebSocket.OPEN)
        socket.current.send(
          JSON.stringify({
            type: "heartbeat",
            active: !document.hidden && document.hasFocus(),
          }),
        );
    };

    const connect = () => {
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${protocol}://${location.host}/api/ws`);
      socket.current = ws;
      ws.onopen = () => {
        if (active) setOnline(true);
        heartbeat();
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
            if (data.ok) pending.resolve();
            else pending.reject(new Error(data.error || "Command failed."));
          }
          if (!data.ok) setError(data.error || "Command failed.");
        }
      };
      ws.onclose = () => {
        if (!active) return;
        setOnline(false);
        rejectPending("Connection to the local app was lost.");
        retry = setTimeout(connect, 1500);
      };
      ws.onerror = () => ws.close();
    };

    connect();
    const heartbeatTimer = setInterval(heartbeat, 400);
    window.addEventListener("focus", heartbeat);
    document.addEventListener("visibilitychange", heartbeat);
    return () => {
      active = false;
      clearInterval(heartbeatTimer);
      window.removeEventListener("focus", heartbeat);
      document.removeEventListener("visibilitychange", heartbeat);
      clearTimeout(retry);
      socket.current?.close();
      rejectPending("App closed.");
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
          reject(new Error("Command timed out. Check the debug log."));
        },
        action === "connect" ? 35000 : 10000,
      );
      requests.current.set(id, { resolve, reject, timer });
      socket.current.send(JSON.stringify({ id, action, args }));
    });
  }, []);

  return { state, logs, setLogs, online, error, setError, send };
}
