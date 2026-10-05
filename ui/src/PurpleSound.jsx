import { useEffect, useRef, useState } from "react";
import {
  Microphone,
  Play,
  Square,
  Star,
  UploadSimple,
} from "@phosphor-icons/react";
import { freshPurpleEvent, monoWav } from "./audio-utils.mjs";

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

export default function PurpleSound({ train, connected, demo, perform }) {
  const [clip, setClip] = useState(null),
    [recording, setRecording] = useState(false),
    [preparing, setPreparing] = useState(false),
    [elapsed, setElapsed] = useState(0),
    [armed, setArmed] = useState(false),
    [playing, setPlaying] = useState(false),
    [message, setMessage] = useState(""),
    [error, setError] = useState("");
  const context = useRef(null),
    recorder = useRef(null),
    stream = useRef(null),
    source = useRef(null),
    timer = useRef(null),
    limit = useRef(null),
    buffer = useRef(null),
    previous = useRef(null),
    lastPlay = useRef(0),
    mounted = useRef(true),
    importInput = useRef(null);
  const capable = Boolean(
    navigator.mediaDevices?.getUserMedia && window.MediaRecorder,
  );

  useEffect(() => {
    mounted.current = true;
    clipStore("readonly")
      .then((saved) => {
        if (mounted.current && saved) setClip(saved);
      })
      .catch(() => {});
    return () => {
      mounted.current = false;
      clearInterval(timer.current);
      clearTimeout(limit.current);
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
    const saved = { blob: wav, name, duration: Math.min(5, decoded.duration) };
    buffer.current = null;
    source.current?.stop();
    setArmed(false);
    setClip(saved);
    try {
      await clipStore("readwrite", saved);
      setMessage(
        decoded.duration > 5
          ? "First 5 seconds saved in this browser."
          : "Saved in this browser.",
      );
    } catch {
      setMessage("Ready for this session. Browser storage is unavailable.");
    }
    await perform("audio_event", { event: "loaded" });
  }

  function finishRecording() {
    clearInterval(timer.current);
    clearTimeout(limit.current);
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
      const ctx = await audioContext();
      await ctx.resume();
      const input = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (!mounted.current) {
        input.getTracks().forEach((track) => track.stop());
        return;
      }
      stream.current = input;
      source.current?.stop();
      setArmed(false);
      const capture = new MediaRecorder(input),
        chunks = [];
      recorder.current = capture;
      capture.ondataavailable = (event) => {
        if (event.data.size) chunks.push(event.data);
      };
      capture.onerror = () => {
        finishRecording();
        setError("Recording failed. Try again or choose an audio file.");
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
          setError(
            "Could not read the recording. Try again or choose an audio file.",
          );
        } finally {
          if (mounted.current) setPreparing(false);
        }
      };
      capture.start();
      setRecording(true);
      setElapsed(0);
      const started = performance.now();
      timer.current = setInterval(
        () => setElapsed(Math.min(5, (performance.now() - started) / 1000)),
        100,
      );
      limit.current = setTimeout(finishRecording, 5000);
    } catch (failure) {
      stream.current?.getTracks().forEach((track) => track.stop());
      stream.current = null;
      setError(
        failure.name === "NotAllowedError"
          ? "Microphone access was denied. Allow it in browser settings or choose an audio file."
          : "Microphone unavailable. You can choose an audio file instead.",
      );
    } finally {
      setPreparing(false);
    }
  }

  async function prepareClip() {
    const ctx = await audioContext();
    if (!buffer.current)
      buffer.current = await ctx.decodeAudioData(await clip.blob.arrayBuffer());
    return ctx;
  }

  async function playClip(trigger = false) {
    try {
      const ctx = await prepareClip();
      source.current?.stop();
      const next = ctx.createBufferSource();
      next.buffer = buffer.current;
      next.connect(ctx.destination);
      source.current = next;
      next.onended = () => {
        if (mounted.current && source.current === next) {
          source.current = null;
          setPlaying(false);
          setMessage(
            trigger
              ? "Purple brick clip played on this Mac."
              : "Preview finished.",
          );
        }
      };
      next.start();
      setPlaying(true);
      setError("");
      setMessage(
        trigger
          ? "Purple brick detected · playing on this Mac."
          : "Preview playing on this Mac.",
      );
      await perform("audio_event", { event: trigger ? "played" : "preview" });
    } catch {
      setError(
        "Audio playback is blocked. Click Preview, then enable purple playback again.",
      );
      setArmed(false);
    }
  }

  useEffect(() => {
    const count = train.purple_events ?? 0;
    const fresh = freshPurpleEvent(previous.current, count, train.purple_at);
    previous.current = count;
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

  return (
    <div className="purple-content">
      <p className="feature-note">
        Record up to 5 seconds. Your clip plays on this Mac when the purple star
        brick is detected.
      </p>
      <div className="clip-status" role="status" aria-live="polite">
        <Star size={22} weight="fill" />
        <div>
          <strong>
            {recording
              ? `Recording · ${elapsed.toFixed(1)} / 5.0 s`
              : preparing
                ? "Preparing audio…"
                : playing
                  ? "Playing on this Mac"
                  : clip
                    ? `${clip.name} · ${clip.duration.toFixed(1)} s`
                    : "No clip recorded"}
          </strong>
          <span>
            {recording
              ? "Stops automatically at 5 seconds."
              : message ||
                (clip
                  ? "Saved on this Mac. Not sent to the train."
                  : "Use your microphone or choose an audio file.")}
          </span>
        </div>
        {recording && <span className="recording-dot" />}
      </div>
      <div className="clip-actions">
        <button
          className={`button ${recording ? "recording-button" : ""}`}
          disabled={preparing || (!capable && !recording)}
          onClick={recording ? finishRecording : startRecording}
        >
          {recording ? (
            <Square size={17} weight="fill" />
          ) : (
            <Microphone size={18} />
          )}
          {recording
            ? "Finish recording"
            : clip
              ? "Record again"
              : "Record sound"}
        </button>
        <button
          className="button"
          disabled={recording || preparing}
          onClick={() => importInput.current.click()}
        >
          <UploadSimple size={18} />
          Choose file
        </button>
        <input
          ref={importInput}
          type="file"
          className="visually-hidden"
          tabIndex={-1}
          accept="audio/*"
          aria-label="Choose purple sound file"
          onChange={async (event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (!file) return;
            setError("");
            if (file.size > 10 * 1024 * 1024) {
              setError("Choose an audio file smaller than 10 MB.");
              return;
            }
            setPreparing(true);
            try {
              await saveAudio(file, file.name);
            } catch {
              setError(
                "Could not read that audio file. Try WAV, MP3, M4A, or WebM.",
              );
            } finally {
              setPreparing(false);
            }
          }}
        />
        <button
          className="button"
          disabled={!clip || recording || preparing}
          onClick={() =>
            playing ? (source.current?.stop(), setPlaying(false)) : playClip()
          }
        >
          {playing ? (
            <Square size={17} weight="fill" />
          ) : (
            <Play size={17} weight="fill" />
          )}
          {playing ? "Stop preview" : "Preview"}
        </button>
      </div>
      {error && (
        <p className="audio-error" role="alert">
          {error}
        </p>
      )}
      {!capable && (
        <p className="feature-note">
          This browser cannot record a microphone. Choose an audio file instead.
        </p>
      )}
      <div className="purple-arm-row">
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={armed}
            disabled={!clip || !connected || recording || preparing}
            onChange={async (event) => {
              const enabled = event.target.checked;
              try {
                if (enabled) await prepareClip();
                setArmed(enabled);
                await perform("audio_event", {
                  event: enabled ? "armed" : "disarmed",
                });
              } catch {
                setError(
                  "Audio unavailable. Preview your clip before enabling playback.",
                );
              }
            }}
          />
          Play on Mac for purple brick
        </label>
        <span className="feature-note">
          {!connected
            ? "Connect the train to enable."
            : armed
              ? "Listening for purple · audio on Mac"
              : "Audio on Mac"}
        </span>
        {demo && (
          <button
            className="text-button"
            disabled={!armed}
            onClick={() => perform("demo_purple")}
          >
            Simulate purple
          </button>
        )}
      </div>
      <p className="native-audio-note">
        <strong>To play through the train’s speaker:</strong> record and
        transfer the clip in the{" "}
        <a
          href="https://www.lego.com/en-us/service/help-topics/article/all-about-lego-duplo-trains"
          target="_blank"
          rel="noreferrer"
        >
          LEGO DUPLO Interactive Trains app
        </a>
        . Audio upload from this controller isn’t supported yet.
      </p>
    </div>
  );
}
