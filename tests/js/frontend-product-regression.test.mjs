import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { FakeElement, elements } from "./fake_dom.mjs";
import { Store } from "../../src/personal_agent/channels/web/static/store.js";

// Run the real page handlers with controllable media promises. This deliberately does not
// replace the media lifecycle being tested with a copy of its implementation.
class Element extends FakeElement {
  constructor(tag) {
    super(tag); this.value = ""; this.dataset = {}; this.style = {}; this.hidden = false;
    this.events = new Map(); this.files = []; this.disabled = false; this.open = false;
  }
  append(...nodes) { super.append(...nodes); for (const child of this.children) child.parentElement = this; }
  replaceChildren(...nodes) {
    if (this.ownerDocument && elements(this).includes(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = this.ownerDocument.body;
    for (const child of this.children) child.parentElement = null;
    this.children = []; this.append(...nodes);
  }
  remove() {
    if (this.ownerDocument && this.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = this.ownerDocument.body;
    if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(item => item !== this);
    this.parentElement = null;
  }
  addEventListener(type, callback, options) {
    if (!this.events.has(type)) this.events.set(type, new Set());
    this.events.get(type).add(callback);
    options?.signal?.addEventListener("abort", () => this.removeEventListener(type, callback), { once: true });
  }
  removeEventListener(type, callback) { this.events.get(type)?.delete(callback); }
  async fire(type, event = {}) { if (!event.preventDefault) event.preventDefault = () => {}; return await Promise.all([...(this.events.get(type) || [])].map(callback => callback(event))); }
  contains(node) { return node === this || elements(this).includes(node); }
  get disabled() { return this._disabled; }
  set disabled(value) {
    this._disabled = Boolean(value);
    // A control becoming disabled cannot retain browser keyboard focus.
    if (this._disabled && this.ownerDocument?.activeElement === this) this.ownerDocument.activeElement = this.ownerDocument.body;
  }
  focus() { if (this.ownerDocument && !this.disabled) this.ownerDocument.activeElement = this; }
  showModal() { this.open = true; }
  close() { this.open = false; for (const callback of [...(this.events.get("close") || [])]) callback(); }
  get isConnected() { return this.root === true || Boolean(this.parentElement?.isConnected); }
}

const source = readFileSync(new URL("../../src/personal_agent/channels/web/static/app.js", import.meta.url), "utf8")
  .replace(/^import\b[\s\S]*?;\n/gm, "")
  .replace("import.meta.url", '"https://example.invalid/static/app.js"')
  .replace(/\nmain\(\);\s*$/, "");
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const settle = async () => { for (let index = 0; index < 8; index++) await Promise.resolve(); };

function setup({ record, duration = async () => 1, shrink = async file => file, canRecord = true, respond } = {}) {
  const root = new Element("div"), body = new Element("body"), viewport = new Element("viewport"), window = new Element("window");
  window.visualViewport = viewport; window.innerHeight = 800;
  root.root = true; body.root = true;
  const document = {
    getElementById: () => root, createElement: tag => Object.assign(new Element(tag), { ownerDocument: document }),
    createElementNS: (_ns, tag) => Object.assign(new Element(tag), { ownerDocument: document }), createTextNode: text => ({ nodeType: 3, textContent: String(text) }),
    addEventListener() {}, querySelectorAll: selector => selector === "dialog" ? elements(body).filter(item => item.tagName === "DIALOG") : [],
    body, activeElement: body, documentElement: new Element("html"), title: "Personal Agent",
  };
  root.ownerDocument = document; body.ownerDocument = document;
  const calls = [], cancelled = [], revoked = [], timers = [], scrolls = [];
  const context = {
    document, window,
    navigator: {}, location: { href: "https://example.invalid/", origin: "https://example.invalid", search: "", hash: "" },
    URL: class extends URL { static createObjectURL() { return "blob:preview"; } static revokeObjectURL(value) { revoked.push(value); } },
    URLSearchParams, FormData, AbortController, Store,
    setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; }, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    onAccessLost() {}, invalidateRequests() {}, setRequestContext() {}, scopedURL: path => path,
    record: record || (async () => ({ done: Promise.resolve(null), cancel: () => cancelled.push(true) })),
    canRecord: () => canRecord, audioDuration: duration, shrinkImage: shrink,
    api: async (path, options) => { calls.push({ path, options }); return respond ? await respond(path, options) : {}; },
    matchMedia: () => ({ matches: false }), commandHints: () => [], newClientId: () => "client-a", scrolls,
    durationLabel: () => "0:00", connectionLabel: () => "已连接", activityLabel: () => "已完成", localStorage: { getItem() { return null; } },
    // sheet.js is covered by its own tests; here a sheet only has to open and close like a dialog.
    presentSheet(dialog, { document: doc }) { doc.body.append(dialog); dialog.showModal(); }, dismissSheet(dialog) { dialog.close(); },
  };
  runInNewContext(source + `
    renderLog = () => {};
    scrollToBottom = () => { scrolls.push(ui); };
    toast = () => {};
    globalThis.page = {
      buildComposer, addFiles, startVoice, submit, teardownChat, watchViewport, openPicture, openSettings, editPending,
      failedMessage(text, files = []) {
        const entry = store.addPending({ clientId: "failed-message", text, files, at: 1 });
        entry.state = "rejected";
        return entry;
      },
      get pending() { return store.pending; },
      get current() { return ui; },
      attach() {
        session = { features: { images: true, voice: true, max_images: 2, max_image_bytes: 8000000, max_voice_bytes: 10000000, voice_seconds: 60 }, agent: "Agent" };
        const composer = buildComposer();
        ui = { composer, cards: { destroy() {} }, log: { scrollHeight: 0, scrollTop: 0, clientHeight: 0 } };
        return ui;
      }
    };
  `, context);
  const view = context.page.attach();
  return { page: context.page, view, root, body, document, viewport, window, calls, cancelled, revoked, timers, scrolls };
}

async function leaving(window) {
  const event = { prevented: false, preventDefault() { this.prevented = true; } };
  await window.fire("beforeunload", event); return event;
}

test("sign-out cancels an active recording and its late result cannot be sent by the next session", async () => {
  const result = deferred(); let cancellations = 0;
  const s = setup({ record: async () => ({ done: result.promise, cancel() { cancellations++; }, stop() {} }) });
  const recording = s.page.startVoice(); await settle();
  s.page.teardownChat(); s.page.attach();
  result.resolve({ blob: new Blob(["private recording"]), name: "voice.webm", seconds: 1 });
  await recording;
  assert.equal(cancellations, 1);
  assert.equal(s.page.pending.length, 0);
  assert.equal(s.calls.length, 0);
});

test("a microphone permission request that resolves after sign-out is cancelled without posting", async () => {
  const permission = deferred(); let cancellations = 0;
  const s = setup({ record: () => permission.promise });
  const recording = s.page.startVoice();
  s.page.teardownChat(); s.page.attach();
  permission.resolve({ done: Promise.resolve({ blob: new Blob(["private"]), name: "voice.webm", seconds: 1 }), cancel() { cancellations++; } });
  await recording;
  assert.equal(cancellations, 1);
  assert.equal(s.calls.length, 0);
});

test("repeated microphone clicks share one pending permission request", async () => {
  const permission = deferred(); let requests = 0;
  const s = setup({ record: () => { requests++; return permission.promise; } });
  const first = s.page.startVoice(), second = s.page.startVoice();
  permission.resolve({ done: Promise.resolve(null), cancel() {} });
  await Promise.all([first, second]);
  assert.equal(requests, 1);
});

test("an audio file whose metadata loads after sign-out never enters the next conversation", async () => {
  const metadata = deferred();
  const s = setup({ canRecord: false, duration: () => metadata.promise });
  const picker = elements(s.view.composer.element).find(item => item.getAttribute("accept") === "audio/*");
  picker.files = [new File(["private audio"], "private.webm", { type: "audio/webm" })];
  const picking = picker.fire("change");
  s.page.teardownChat(); s.page.attach(); metadata.resolve(1); await picking;
  assert.equal(s.page.pending.length, 0);
  assert.equal(s.calls.length, 0);
});

test("signing in again replaces drag and drop handlers instead of accumulating attachments", async () => {
  const s = setup();
  s.page.teardownChat();
  assert.equal(s.root.events.get("drop")?.size || 0, 0);
  const next = s.page.attach();
  await s.root.fire("drop", { dataTransfer: { files: [new File(["image"], "photo.png", { type: "image/png" })] } });
  await settle();
  assert.equal(s.root.events.get("drop")?.size, 1);
  assert.equal(next.composer.files.length, 1);
});

test("simultaneous image imports respect the message image limit", async () => {
  const s = setup({ shrink: async file => { await settle(); return file; } });
  const files = ["a", "b", "c", "d"].map(name => new File([name], `${name}.png`, { type: "image/png" }));
  await Promise.all([s.page.addFiles(files.slice(0, 2)), s.page.addFiles(files.slice(2))]);
  assert.equal(s.view.composer.files.length, 2);
});

test("a completed image import cannot repopulate a signed-out composer's private files", async () => {
  const resizing = deferred();
  const s = setup({ shrink: () => resizing.promise });
  const picture = new File(["private"], "photo.png", { type: "image/png" });
  const importing = s.page.addFiles([picture]);
  s.page.teardownChat(); s.page.attach(); resizing.resolve(picture); await importing;
  assert.equal(s.view.composer.files.length, 0);
  assert.equal(s.page.current.composer.files.length, 0);
});

test("sign-out clears open private image dialogs and releases viewport listeners", () => {
  const s = setup();
  s.page.openPicture("a".repeat(64));
  s.view.viewportCleanup = s.page.watchViewport();
  assert.equal(s.body.children.length, 1);
  s.page.teardownChat();
  assert.equal(s.body.children.length, 0);
  assert.equal(s.viewport.events.get("resize")?.size || 0, 0);
  assert.equal(s.viewport.events.get("scroll")?.size || 0, 0);
});

test("late status data cannot refill a settings dialog removed on sign-out", async () => {
  const status = deferred();
  const s = setup({ respond: () => status.promise });
  const opening = s.page.openSettings();
  const dialog = s.body.children[0];
  s.page.teardownChat(); s.page.attach();
  status.resolve({ text: "private old status", connection: { address: "private host" }, activity: { items: [{ title: "private old task" }] } });
  await opening;
  assert.equal(s.body.children.length, 0);
  assert.equal(dialog.textContent, "");
});

test("an authorized recording still sends its audio and measured duration exactly once", async () => {
  const result = deferred();
  const s = setup({ record: async () => ({ done: result.promise, cancel() {}, stop() {} }) });
  const recording = s.page.startVoice(); await settle();
  result.resolve({ blob: new Blob(["recording"]), name: "voice.webm", seconds: 2.5 });
  await recording;
  assert.equal(s.calls.length, 1);
  assert.equal(s.calls[0].path, "/api/messages");
  assert.equal(s.calls[0].options.form.get("voice_seconds"), "2.5");
  assert.equal(s.calls[0].options.form.get("voice").name, "voice.webm");
  assert.equal(s.view.composer.voice, null);
});

test("sending waits for image preparation and keeps the text and attachment in the same message", async () => {
  const resize = deferred();
  const s = setup({ shrink: () => resize.promise });
  const picture = new File(["image"], "photo.png", { type: "image/png" });
  s.view.composer.input.value = "请整理这张图片";
  const importing = s.page.addFiles([picture]);
  await s.page.submit();
  assert.equal(s.calls.length, 0);
  resize.resolve(picture); await importing;
  await s.page.submit();
  assert.equal(s.calls.length, 1);
  assert.equal(s.calls[0].options.form.get("text"), "请整理这张图片");
  assert.equal(s.calls[0].options.form.getAll("image").length, 1);
  assert.equal(s.view.composer.input.value, "");
});

test("navigation protects unsent chat text and attachments without warning after they are sent", async () => {
  const s = setup();
  assert.equal((await leaving(s.window)).prevented, false);
  s.view.composer.input.value = "尚未发送的想法";
  const protectedDraft = await leaving(s.window);
  assert.equal(protectedDraft.prevented, true);
  assert.equal(protectedDraft.returnValue, "");
  await s.page.submit();
  assert.equal((await leaving(s.window)).prevented, false);
  await s.page.addFiles([new File(["image"], "photo.png", { type: "image/png" })]);
  assert.equal((await leaving(s.window)).prevented, true);
  await s.page.submit();
  assert.equal((await leaving(s.window)).prevented, false);
});

test("navigation protects an image import while it is still being prepared", async () => {
  const resizing = deferred(), s = setup({ shrink: () => resizing.promise });
  const picture = new File(["image"], "photo.png", { type: "image/png" });
  const importing = s.page.addFiles([picture]);
  assert.equal((await leaving(s.window)).prevented, true);
  resizing.resolve(picture); await importing;
  assert.equal((await leaving(s.window)).prevented, true);
  await s.page.submit();
  assert.equal((await leaving(s.window)).prevented, false);
});

test("navigation protects audio metadata loading, pending microphone permission and active recording", async () => {
  const metadata = deferred(), s = setup({ canRecord: false, duration: () => metadata.promise });
  const picker = elements(s.view.composer.element).find(item => item.getAttribute("accept") === "audio/*");
  picker.files = [new File(["audio"], "voice.webm", { type: "audio/webm" })];
  const picking = picker.fire("change");
  assert.equal((await leaving(s.window)).prevented, true);
  metadata.resolve(2); await picking;
  assert.equal((await leaving(s.window)).prevented, false);

  const permission = deferred(), result = deferred();
  const recordingPage = setup({ record: () => permission.promise });
  const recording = recordingPage.page.startVoice();
  assert.equal((await leaving(recordingPage.window)).prevented, true);
  permission.resolve({ done: result.promise, cancel() {} }); await settle();
  assert.equal((await leaving(recordingPage.window)).prevented, true);
  result.resolve(null); await recording;
  assert.equal((await leaving(recordingPage.window)).prevented, false);
});

test("explicit sign-out clears draft navigation protection and re-login installs one guard", async () => {
  const s = setup(); s.view.composer.input.value = "private draft";
  assert.equal((await leaving(s.window)).prevented, true);
  s.page.teardownChat();
  assert.equal((await leaving(s.window)).prevented, false);
  assert.equal(s.window.events.get("beforeunload")?.size || 0, 0);
  s.page.attach();
  assert.equal(s.window.events.get("beforeunload")?.size, 1);
  assert.equal((await leaving(s.window)).prevented, false);
});

test("a partially failed image batch keeps every attached image visible and removable", async () => {
  const good = new File(["image"], "good.png", { type: "image/png" });
  const bad = new File(["broken"], "broken.png", { type: "image/png" });
  const s = setup({ shrink: async file => { if (file === bad) throw new Error("decode failed"); return file; } });
  await s.page.addFiles([good, bad]);
  assert.deepEqual([...s.view.composer.files], [good]);
  assert.equal(s.view.composer.chips.hidden, false);
  assert.equal(elements(s.view.composer.chips).filter(item => item.tagName === "IMG").length, 1);
  assert.equal(s.view.composer.pendingImages, 0);
  const remove = elements(s.view.composer.chips).find(item => item.getAttribute("aria-label") === "移除图片");
  await remove.fire("click");
  assert.equal(s.view.composer.files.length, 0);
  assert.equal(s.view.composer.chips.hidden, true);
});

test("editing a failed message never overwrites or merges with an occupied composer", () => {
  for (const state of ["text", "files", "pendingImages", "pendingAudio", "voiceStarting", "voice"]) {
    const s = setup(), original = new File(["original"], "original.png", { type: "image/png" });
    const failed = s.page.failedMessage("失败的消息", [original]);
    if (state === "text") s.view.composer.input.value = "正在写的新消息";
    else if (state === "files") s.view.composer.files.push(new File(["draft"], "draft.png", { type: "image/png" }));
    else s.view.composer[state] = state === "voice" ? { cancel() {} } : 1;
    const draftText = s.view.composer.input.value, draftFiles = [...s.view.composer.files];
    s.page.editPending(failed);
    assert.equal(s.view.composer.input.value, draftText, state);
    assert.deepEqual([...s.view.composer.files], draftFiles, state);
    assert.ok(s.page.pending.includes(failed), state);
    assert.deepEqual([...failed.files], [original], state);
  }
});

test("editing a failed message restores it only into an empty composer within attachment limits", () => {
  const s = setup(), files = ["a", "b"].map(name => new File([name], `${name}.png`, { type: "image/png" }));
  const failed = s.page.failedMessage("重新编辑", files);
  s.page.editPending(failed);
  assert.equal(s.view.composer.input.value, "重新编辑");
  assert.deepEqual([...s.view.composer.files], files);
  assert.equal(s.page.pending.includes(failed), false);
  assert.equal(elements(s.view.composer.chips).filter(item => item.tagName === "IMG").length, 2);

  const oversized = setup(), tooMany = [...files, new File(["c"], "c.png", { type: "image/png" })];
  const overLimit = oversized.page.failedMessage("超过数量限制", tooMany);
  oversized.page.editPending(overLimit);
  assert.equal(oversized.view.composer.input.value, "");
  assert.equal(oversized.view.composer.files.length, 0);
  assert.ok(oversized.page.pending.includes(overLimit));
});

test("a deferred composer focus cannot access a signed-out page or scroll the next session", async () => {
  for (const replace of [false, true]) {
    const s = setup();
    await s.view.composer.input.fire("focus");
    const pending = s.timers.find(timer => timer.delay === 300);
    assert.ok(pending);
    s.page.teardownChat();
    if (replace) s.page.attach();
    assert.doesNotThrow(() => pending.callback());
    assert.equal(s.scrolls.length, 0);
  }
});

test("appearance choices preserve the focused button and expose the selected state after each change", async () => {
  const s = setup();
  await s.page.openSettings();
  const dialog = s.body.children[0];
  const choices = elements(dialog).find(item => item.getAttribute("aria-label") === "外观");
  assert.equal(choices.getAttribute("role"), "group");
  const buttons = choices.children.slice();
  for (const [index, theme] of [[2, "dark"], [1, "light"], [0, "auto"]]) {
    const selected = buttons[index];
    selected.focus(); await selected.fire("click");
    assert.equal(s.document.activeElement, selected);
    assert.equal(choices.children[index], selected, "the user's focused control must remain connected");
    assert.equal(s.document.documentElement.dataset.theme, theme);
    assert.deepEqual(buttons.map(button => button.getAttribute("aria-pressed")), buttons.map(button => String(button === selected)));
  }
});

test("recording carries keyboard focus to its controls and back without stealing focus during microphone permission", async () => {
  for (const movedFocus of [false, true]) {
    const permission = deferred(), result = deferred();
    const s = setup({ record: () => permission.promise });
    s.view.composer.mic.focus();
    const recording = s.page.startVoice();
    assert.equal(s.document.activeElement, s.body, "the disabled microphone yields its focus");
    const otherControl = s.document.createElement("button"); s.body.append(otherControl);
    if (movedFocus) otherControl.focus();
    permission.resolve({ done: result.promise, cancel() { result.resolve(null); }, stop() {} });
    await settle();
    const cancel = elements(s.view.composer.recording).find(item => item.getAttribute("aria-label") === "放弃录音");
    assert.ok(cancel);
    assert.equal(s.document.activeElement, movedFocus ? otherControl : cancel);
    if (movedFocus) result.resolve(null); else await cancel.fire("click");
    await recording;
    assert.equal(s.document.activeElement, movedFocus ? otherControl : s.view.composer.mic);
    assert.equal(s.view.composer.row.hidden, false);
    assert.equal(s.view.composer.recording.hidden, true);
  }
});
