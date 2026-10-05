import { useEffect, useRef, useState } from "react";
import {
  Microphone,
  Play,
  Square,
  Star,
  UploadSimple,
} from "@phosphor-icons/react";
import { freshPurpleEvent, monoWav } from "./audio-utils.mjs";
import { Panel, Switch } from "./parts.jsx";

const MAX_SECONDS = 5;

// The clip is kept in this browser's IndexedDB under a single key.
function clipStore(mode, value) {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("duplo-local-audio", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("clips");
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const database = request.result;
      const transaction = database.transaction("clips", mode);
      const store = transaction.objectStore("clips");
      const result =
        mode === "readonly" ? store.get("purple") : store.put(value, "purple");
      transaction.oncomplete = () => {
        database.close();
        resolve(result.result);
      };
      transaction.onerror = () => {
        database.close();
        reject(transaction.error);
      };
    };
  });
}

// Records or imports a short clip and plays it on this Mac when the train
// reports the purple action brick. Nothing is uploaded to the train.
export default function PurpleSound({ train, connected, demo, perform }) {
  const [clip, setClip] = useState(null);
  const [recording, setRecording] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [armed, setArmed] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const context = useRef(null);
  const recorder = useRef(null);
  const stream = useRef(null);
  const source = useRef(null);
  const timers = useRef({});
  const buffer = useRef(null);
  const previousCount = useRef(null);
  const lastPlay = useRef(0);
  const mounted = useRef(true);
  const fileInput = useRef(null);
  const canRecord = Boolean(
    navigator.mediaDevices?.getUserMedia && window.MediaRecorder,
  );

  useEffect(() => {
    mounted.current = true;
    clipStore("readonly")
      .then((saved) => mounted.current && saved && setClip(saved))
      .catch(() => {});
    return () => {
      mounted.current = false;
      clearInterval(timers.current.tick);
      clearTimeout(timers.current.limit);
      if (recorder.current) {
        recorder.current.onstop = null;
        if (recorder.current.state !== "inactive") recorder.current.stop();
      }
      stream.current?.getTracks().forEach((track) => track.stop());
      source.current?.stop();
      context.current?.close();
    };
  }, []);

  async function audioContext() {
    if (!context.current)
      context.current = new (window.AudioContext ||
        window.webkitAudioContext)();
    await context.current.resume();
    return context.current;
  }

  async function saveAudio(blob, name) {
    const ctx = await audioContext();
    const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
    if (!decoded.length) throw new Error("This clip has no audio.");
    const channels = Array.from({ length: decoded.numberOfChannels }, (_, i) =>
      decoded.getChannelData(i),
    );
    const wav = new Blob([monoWav(channels, decoded.sampleRate)], {
      type: "audio/wav",
    });
    const saved = {
      blob: wav,
      name,
      duration: Math.min(MAX_SECONDS, decoded.duration),
    };
    buffer.current = null;
    source.current?.stop();
    setArmed(false);
    setClip(saved);
    try {
      await clipStore("readwrite", saved);
      setMessage(
        decoded.duration > MAX_SECONDS
          ? "Saved the first 5 seconds."
          : "Saved.",
      );
    } catch {
      setMessage("Ready for this session (browser storage is unavailable).");
    }
    await perform("audio_event", { event: "loaded" });
  }

  function finishRecording() {
    clearInterval(timers.current.tick);
    clearTimeout(timers.current.limit);
    if (recorder.current?.state === "recording") recorder.current.stop();
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
    setRecording(false);
  }

  async function startRecording() {
    setError("");
    setMessage("");
    setPreparing(true);
    try {
      if (connected && !(await perform("stop"))) return;
      await audioContext();
      const input = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (!mounted.current) {
        input.getTracks().forEach((track) => track.stop());
        return;
      }
      stream.current = input;
      source.current?.stop();
      setArmed(false);
      const capture = new MediaRecorder(input);
      const chunks = [];
      recorder.current = capture;
      capture.ondataavailable = (event) =>
        event.data.size && chunks.push(event.data);
      capture.onerror = () => {
        finishRecording();
        setError("Recording failed. Try again or choose a file.");
      };
      capture.onstop = async () => {
        if (!mounted.current) return;
        setPreparing(true);
        try {
          await saveAudio(
            new Blob(chunks, { type: capture.mimeType }),
            "Microphone recording",
          );
        } catch {
          setError("Could not read the recording. Try again or choose a file.");
        } finally {
          if (mounted.current) setPreparing(false);
        }
      };
      capture.start();
      setRecording(true);
      setElapsed(0);
      const started = performance.now();
      timers.current.tick = setInterval(
        () =>
          setElapsed(
            Math.min(MAX_SECONDS, (performance.now() - started) / 1000),
          ),
        100,
      );
      timers.current.limit = setTimeout(finishRecording, MAX_SECONDS * 1000);
    } catch (failure) {
      stream.current?.getTracks().forEach((track) => track.stop());
      stream.current = null;
      setError(
        failure.name === "NotAllowedError"
          ? "Microphone access was denied. Allow it in the browser, or choose a file."
          : "No microphone available. Choose a file instead.",
      );
    } finally {
      setPreparing(false);
    }
  }

  async function loadBuffer() {
    const ctx = await audioContext();
    if (!buffer.current)
      buffer.current = await ctx.decodeAudioData(await clip.blob.arrayBuffer());
    return ctx;
  }

  async function playClip(trigger = false) {
    try {
      const ctx = await loadBuffer();
      source.current?.stop();
      const next = ctx.createBufferSource();
      next.buffer = buffer.current;
      next.connect(ctx.destination);
      source.current = next;
      next.onended = () => {
        if (mounted.current && source.current === next) {
          source.current = null;
          setPlaying(false);
          setMessage(trigger ? "Purple brick clip played." : "");
        }
      };
      next.start();
      setPlaying(true);
      setError("");
      setMessage(trigger ? "Purple brick detected." : "");
      await perform("audio_event", { event: trigger ? "played" : "preview" });
    } catch {
      setError(
        "Playback was blocked by the browser. Click Preview, then turn playback on again.",
      );
      setArmed(false);
    }
  }

  async function chooseFile(event) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setError("");
    if (file.size > 10 * 1024 * 1024)
      return setError("Choose an audio file smaller than 10 MB.");
    setPreparing(true);
    try {
      await saveAudio(file, file.name);
    } catch {
      setError("Could not read that file. Try WAV, MP3, M4A or WebM.");
    } finally {
      setPreparing(false);
    }
  }

  async function setPlayback(enabled) {
    try {
      if (enabled) await loadBuffer();
      setArmed(enabled);
      await perform("audio_event", { event: enabled ? "armed" : "disarmed" });
    } catch {
      setError("Audio unavailable. Preview the clip first, then try again.");
    }
  }

  // Play when the server reports a new purple-brick detection.
  useEffect(() => {
    const count = train.purple_events ?? 0;
    const fresh = freshPurpleEvent(
      previousCount.current,
      count,
      train.purple_at,
    );
    previousCount.current = count;
    if (
      fresh &&
      armed &&
      connected &&
      !recording &&
      Date.now() - lastPlay.current > 1000
    ) {
      lastPlay.current = Date.now();
      playClip(true);
    }
  }, [train.purple_events, train.purple_at, connected, armed, recording]);

  const status = recording
    ? `Recording… ${elapsed.toFixed(1)} / ${MAX_SECONDS}.0 s`
    : preparing
      ? "Preparing audio…"
      : playing
        ? "Playing on this Mac…"
        : clip
          ? `${clip.name} · ${clip.duration.toFixed(1)} s`
          : "No clip yet";
  const [chipText, chipTone] = recording
    ? ["Recording", "error"]
    : armed
      ? ["On", "ok"]
      : clip
        ? ["Clip ready", ""]
        : ["No clip", ""];

  return (
    <Panel
      title="Purple brick sound"
      subtitle="Your own sound for the purple brick"
      status={<span className={`chip ${chipTone}`}>{chipText}</span>}
    >
      <p className="hint">
        Record up to 5 seconds. It plays on this Mac when the train passes the
        purple brick. To play it from the train’s own speaker, use the{" "}
        <a
          href="https://www.lego.com/en-us/service/help-topics/article/all-about-lego-duplo-trains"
          target="_blank"
          rel="noreferrer"
        >
          LEGO DUPLO app
        </a>
        .
      </p>
      <div className="clip" role="status" aria-live="polite">
        <Star size={20} weight="fill" />
        <span>
          <strong>{status}</strong>
          {message && <span className="hint"> · {message}</span>}
        </span>
        {recording && <span className="rec-dot" aria-hidden="true" />}
      </div>
      <div className="row">
        <button
          className={`button ${recording ? "is-recording" : ""}`}
          disabled={preparing || (!canRecord && !recording)}
          title={
            canRecord ? undefined : "This browser cannot record. Choose a file."
          }
          onClick={recording ? finishRecording : startRecording}
        >
          {recording ? (
            <Square size={16} weight="fill" />
          ) : (
            <Microphone size={17} />
          )}
          {recording ? "Stop recording" : clip ? "Record again" : "Record"}
        </button>
        <button
          className="button"
          disabled={recording || preparing}
          onClick={() => fileInput.current.click()}
        >
          <UploadSimple size={17} /> Choose file
        </button>
        <input
          ref={fileInput}
          type="file"
          className="visually-hidden"
          tabIndex={-1}
          accept="audio/*"
          aria-label="Choose purple sound file"
          onChange={chooseFile}
        />
        <button
          className="button"
          disabled={!clip || recording || preparing}
          onClick={() =>
            playing ? (source.current?.stop(), setPlaying(false)) : playClip()
          }
        >
          {playing ? (
            <Square size={16} weight="fill" />
          ) : (
            <Play size={16} weight="fill" />
          )}
          {playing ? "Stop" : "Preview"}
        </button>
      </div>
      <div className="row">
        <Switch
          checked={armed}
          disabled={!clip || !connected || recording || preparing}
          onChange={setPlayback}
        >
          Play on purple brick
        </Switch>
        {demo && (
          <button
            className="text-button"
            disabled={!armed}
            onClick={() => perform("demo_purple")}
          >
            Simulate purple brick
          </button>
        )}
      </div>
      {!connected && clip && (
        <p className="hint">Connect the train to turn playback on.</p>
      )}
      {error && (
        <p className="error-text" role="alert">
          {error}
        </p>
      )}
    </Panel>
  );
}
