// Local, uncompressed mono audio for browser storage and preview. Not a LEGO codec.
export function monoWav(channels, sampleRate, maxSeconds = 5) {
  if (!channels.length || !Number.isFinite(sampleRate) || sampleRate <= 0)
    throw new Error("Invalid audio data.");
  const count = Math.min(
    channels[0].length,
    Math.floor(sampleRate * maxSeconds),
  );
  const bytes = new Uint8Array(44 + count * 2);
  const view = new DataView(bytes.buffer);
  const text = (offset, value) => {
    for (let i = 0; i < value.length; i++)
      bytes[offset + i] = value.charCodeAt(i);
  };
  text(0, "RIFF");
  view.setUint32(4, bytes.length - 8, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, "data");
  view.setUint32(40, count * 2, true);
  for (let i = 0; i < count; i++) {
    const sample = Math.max(
      -1,
      Math.min(
        1,
        channels.reduce((sum, channel) => sum + (channel[i] || 0), 0) /
          channels.length,
      ),
    );
    view.setInt16(
      44 + i * 2,
      Math.round(sample * (sample < 0 ? 32768 : 32767)),
      true,
    );
  }
  return bytes;
}

export function freshPurpleEvent(
  previous,
  current,
  at,
  now = Date.now() / 1000,
) {
  return (
    previous !== null &&
    current > previous &&
    at != null &&
    now - at < 1.5 &&
    now >= at - 1
  );
}
