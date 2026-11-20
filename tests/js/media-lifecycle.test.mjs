import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

const source = readFileSync(new URL("../../src/personal_agent/channels/web/static/media.js", import.meta.url), "utf8").replace(/^export /gm, "");
function setup({ failConstructor = false, failStart = false } = {}) {
  const state = { released: 0, recorders: [], timers: new Map(), milliseconds: 0 };
  const stream = { getTracks: () => [{ stop() { state.released++; } }] };
  class Recorder {
    static isTypeSupported() { return true; }
    constructor() {
      if (failConstructor) throw new Error("recorder unavailable");
      this.state = "inactive"; this.mimeType = "audio/webm"; state.recorders.push(this);
    }
    start() { if (failStart) throw new Error("recording cannot start"); this.state = "recording"; }
    stop() { this.state = "inactive"; this.onstop(); }
  }
  const context = {
    navigator: { mediaDevices: { getUserMedia: async () => stream } }, MediaRecorder: Recorder,
    performance: { now: () => state.milliseconds }, Blob,
    setInterval(callback) { state.timers.set(1, callback); return 1; },
    clearInterval(id) { state.timers.delete(id); },
  };
  runInNewContext(source + "\nglobalThis.startRecording = record;", context);
  return { state, record: context.startRecording };
}

test("a recorder constructor failure releases the microphone stream already acquired", async () => {
  const s = setup({ failConstructor: true });
  await assert.rejects(s.record({ maxSeconds: 60 }), /recorder unavailable/);
  assert.equal(s.state.released, 1);
  assert.equal(s.state.timers.size, 0);
});

test("a recorder start failure releases the microphone without leaving a timer", async () => {
  const s = setup({ failStart: true });
  await assert.rejects(s.record({ maxSeconds: 60 }), /recording cannot start/);
  assert.equal(s.state.released, 1);
  assert.equal(s.state.timers.size, 0);
});

test("cancel discards the recording and repeated cleanup releases the microphone once", async () => {
  const s = setup(); const take = await s.record({ maxSeconds: 60 });
  take.cancel(); take.cancel();
  assert.equal(await take.done, null);
  assert.equal(s.state.released, 1);
  assert.equal(s.state.timers.size, 0);
});

test("normal completion preserves recorded data and stops at the configured duration", async () => {
  const s = setup(); const take = await s.record({ maxSeconds: 2 });
  s.state.recorders[0].ondataavailable({ data: new Blob(["recording"]) });
  s.state.milliseconds = 2100; s.state.timers.get(1)();
  const result = await take.done;
  assert.equal(result.name, "voice.webm");
  assert.equal(result.seconds, 2.1);
  assert.equal(await result.blob.text(), "recording");
  assert.equal(s.state.released, 1);
  assert.equal(s.state.timers.size, 0);
});
