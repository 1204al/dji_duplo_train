import test from 'node:test';
import assert from 'node:assert/strict';
import { monoWav, freshPurpleEvent } from '../src/audio-utils.mjs';

test('audio is trimmed to five seconds and has a valid mono PCM WAV header', () => {
  const input = new Float32Array(8000 * 7).fill(0.5);
  const bytes = monoWav([input], 8000);
  const view = new DataView(bytes.buffer);
  assert.equal(bytes.length, 44 + 8000 * 5 * 2);
  assert.equal(new TextDecoder().decode(bytes.slice(0, 4)), 'RIFF');
  assert.equal(view.getUint16(22, true), 1);
  assert.equal(view.getUint32(24, true), 8000);
  assert.equal(view.getInt16(44, true), 16384);
});

test('stereo downmix and clipping preserve valid signed PCM samples', () => {
  const bytes = monoWav([new Float32Array([1, -1, 2]), new Float32Array([-1, -1, 2])], 8000);
  const view = new DataView(bytes.buffer);
  assert.deepEqual([44, 46, 48].map(i => view.getInt16(i, true)), [0, -32768, 32767]);
});

test('purple triggers ignore snapshots, stale events and unchanged values', () => {
  assert.equal(freshPurpleEvent(null, 1, 100, 100), false);
  assert.equal(freshPurpleEvent(1, 1, 100, 100), false);
  assert.equal(freshPurpleEvent(1, 2, 98, 100), false);
  assert.equal(freshPurpleEvent(1, 2, 100, 100), true);
  assert.equal(freshPurpleEvent(2, 1, 100, 100), false);
});
