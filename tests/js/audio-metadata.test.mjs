import assert from "node:assert/strict";
import { test } from "node:test";
import { audioDuration } from "../../src/personal_agent/channels/web/static/audio-metadata.js";

function browser({ throwOnLoad = false, throwOnCreateUrl = false } = {}) {
  const instances = [];
  const created = [];
  const revoked = [];
  const timers = new Map();
  class Audio extends EventTarget {
    duration = NaN;
    src = "";
    loads = 0;
    plays = 0;
    constructor() {
      super();
      instances.push(this);
    }
    load() {
      this.loads += 1;
      if (throwOnLoad) throw new Error("decoder unavailable");
    }
    play() {
      this.plays += 1;
      throw new Error("metadata must never start playback");
    }
    removeAttribute(name) {
      if (name === "src") this.src = "";
    }
    emit(name) { this.dispatchEvent(new Event(name)); }
  }
  const options = {
    Audio,
    URL: {
      createObjectURL(file) {
        if (throwOnCreateUrl) throw new Error("object URLs unavailable");
        created.push(file);
        return `blob:audio-${created.length}`;
      },
      revokeObjectURL(url) { revoked.push(url); },
    },
    setTimeout(callback, delay) {
      const id = timers.size;
      timers.set(id, { callback, delay });
      return id; // zero is a valid timer handle, and must also be cleared
    },
    clearTimeout(id) { timers.delete(id); },
  };
  return { options, instances, created, revoked, timers };
}

const file = new Blob([new Uint8Array(30 * 44100 * 2 * 2)], { type: "audio/wav" });

test("a high-bitrate file uses its duration, without playback or microphone access", async () => {
  const env = browser();
  const result = audioDuration(file, env.options);
  const audio = env.instances[0];
  assert.equal(audio.preload, "metadata");
  assert.equal(audio.autoplay, false);
  assert.equal(audio.src, "blob:audio-1");
  assert.equal(audio.loads, 1);
  assert.deepEqual(env.created, [file]);
  audio.duration = 30;
  audio.emit("loadedmetadata");
  assert.equal(await result, 30);
  assert.equal(audio.plays, 0);
  assert.equal(audio.src, "");
  assert.equal(env.timers.size, 0);
  assert.deepEqual(env.revoked, ["blob:audio-1"]);
  // A late event or retained timer callback cannot settle/revoke a second time.
  audio.emit("error");
  audio.emit("loadedmetadata");
  assert.deepEqual(env.revoked, ["blob:audio-1"]);
});

for (const duration of [NaN, Infinity, -Infinity, 0, -2, "30"]) {
  test(`invalid duration ${String(duration)} falls back at the timeout and releases the file`, async () => {
    const env = browser();
    const result = audioDuration(file, { ...env.options, timeoutMs: 250 });
    const audio = env.instances[0];
    audio.duration = duration;
    audio.emit("loadedmetadata");
    assert.equal(env.revoked.length, 0);
    const { callback, delay } = env.timers.get(0);
    assert.equal(delay, 250);
    callback();
    assert.equal(await result, undefined);
    assert.equal(audio.src, "");
    assert.equal(audio.plays, 0);
    assert.equal(env.timers.size, 0);
    assert.deepEqual(env.revoked, ["blob:audio-1"]);
    audio.duration = 20;
    audio.emit("durationchange");
    callback();
    assert.deepEqual(env.revoked, ["blob:audio-1"]);
  });
}

test("a later durationchange can supply finite metadata before the deadline", async () => {
  const env = browser();
  const result = audioDuration(file, env.options);
  const audio = env.instances[0];
  audio.duration = Infinity;
  audio.emit("loadedmetadata");
  audio.duration = 12.5;
  audio.emit("durationchange");
  assert.equal(await result, 12.5);
  assert.equal(env.timers.size, 0);
  assert.deepEqual(env.revoked, ["blob:audio-1"]);
});

for (const event of ["error", "abort"]) {
  test(`${event} releases the decoder, object URL and timer`, async () => {
    const env = browser();
    const result = audioDuration(file, env.options);
    env.instances[0].emit(event);
    assert.equal(await result, undefined);
    assert.equal(env.instances[0].src, "");
    assert.equal(env.timers.size, 0);
    assert.deepEqual(env.revoked, ["blob:audio-1"]);
  });
}

test("decoder setup/cleanup exceptions still revoke an allocated URL", async () => {
  const env = browser({ throwOnLoad: true });
  assert.equal(await audioDuration(file, env.options), undefined);
  assert.equal(env.timers.size, 0);
  assert.deepEqual(env.revoked, ["blob:audio-1"]);
});

test("missing browser APIs and object URL failures resolve to the server fallback", async () => {
  const env = browser();
  assert.equal(await audioDuration(file, { ...env.options, Audio: null }), undefined);
  assert.equal(env.created.length, 0);
  const unsupported = browser({ throwOnCreateUrl: true });
  assert.equal(await audioDuration(file, unsupported.options), undefined);
  assert.equal(unsupported.timers.size, 0);
  assert.deepEqual(unsupported.revoked, []);
});

test("the default constructor is the browser's global Audio", async (t) => {
  const env = browser();
  t.mock.method(globalThis, "setTimeout", env.options.setTimeout);
  t.mock.method(globalThis, "clearTimeout", env.options.clearTimeout);
  t.mock.method(globalThis.URL, "createObjectURL", env.options.URL.createObjectURL);
  t.mock.method(globalThis.URL, "revokeObjectURL", env.options.URL.revokeObjectURL);
  const previous = Object.getOwnPropertyDescriptor(globalThis, "Audio");
  Object.defineProperty(globalThis, "Audio", { configurable: true, value: env.options.Audio });
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, "Audio", previous);
    else delete globalThis.Audio;
  });
  const result = audioDuration(file);
  assert.equal(env.timers.get(0).delay, 3000);
  env.instances[0].duration = 5;
  env.instances[0].emit("loadedmetadata");
  assert.equal(await result, 5);
  assert.deepEqual(env.revoked, ["blob:audio-1"]);
});
