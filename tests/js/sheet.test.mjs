import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { dismissSheet, presentSheet } from "../../src/personal_agent/channels/web/static/sheet.js";

// A dialog with just enough of the platform: open state, listeners, a style bag and the ways out.
class Dialog {
  constructor() {
    this.open = false; this.dataset = {}; this.className = "sheet"; this.offsetHeight = 400; this.closes = 0; this.shown = 0;
    this.listeners = new Map();
    const style = { removeProperty: name => { delete style[name]; }, setProperty: (name, value) => { style[name] = value; } };
    this.style = style;
  }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  removeEventListener(type, callback) { if (this.listeners.get(type) === callback) this.listeners.delete(type); }
  showModal() { this.open = true; this.shown++; }
  close() { if (!this.open) return; this.open = false; this.closes++; }
  fire(type, event = {}) { return this.listeners.get(type)?.({ preventDefault() { event.prevented = true; }, ...event }); }
}
class Handle {
  constructor() { this.listeners = new Map(); this.captures = []; }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  setPointerCapture(id) { this.captures.push(id); }
  fire(type, event) { this.listeners.get(type)?.({ type, ...event }); }
}

const saved = { matchMedia: globalThis.matchMedia, setTimeout: globalThis.setTimeout };
afterEach(() => { globalThis.matchMedia = saved.matchMedia; globalThis.setTimeout = saved.setTimeout; });
function environment({ reduced = false, wide = false } = {}) {
  const timers = [];
  globalThis.matchMedia = query => ({ matches: query.includes("reduce") ? reduced : wide });
  globalThis.setTimeout = (callback, delay) => { timers.push({ callback, delay }); return timers.length; };
  return timers;
}
const opened = () => { const dialog = new Dialog(); dialog.open = true; return dialog; };

test("with reduced motion a sheet closes at once", () => {
  const timers = environment({ reduced: true }), dialog = opened();
  dismissSheet(dialog);
  assert.equal(dialog.open, false); assert.equal(dialog.className, "sheet"); assert.equal(timers.length, 0);
});

test("a sheet leaves with its own animation: the children's animations do not count and a missed event cannot trap it", () => {
  const timers = environment(), dialog = opened();
  dismissSheet(dialog);
  assert.equal(dialog.className, "sheet closing"); assert.equal(dialog.dataset.closing, "true"); assert.equal(dialog.open, true);
  dialog.fire("animationend", { target: {} });
  assert.equal(dialog.open, true, "an animation inside the sheet bubbles up and must be ignored");
  dismissSheet(dialog); assert.equal(timers.length, 1, "asking twice does not queue a second exit");
  dialog.fire("animationend", { target: dialog });
  assert.equal(dialog.open, false); assert.equal(dialog.className, "sheet"); assert.equal(dialog.dataset.closing, undefined);
  assert.equal(dialog.listeners.has("animationend"), false, "the exit listener goes with the exit");
  timers[0].callback(); assert.equal(dialog.closes, 1, "the fallback timer finding it already closed does nothing");

  const stuck = opened();
  dismissSheet(stuck);
  assert.equal(timers.at(-1).delay, 540, "a lost animationend still closes the sheet shortly after the animation should have ended");
  timers.at(-1).callback();
  assert.equal(stuck.open, false); assert.equal(stuck.className, "sheet");
});

test("dismissing something that is not open does nothing", () => {
  const timers = environment(), dialog = new Dialog();
  dismissSheet(dialog);
  assert.equal(dialog.className, "sheet"); assert.equal(timers.length, 0); assert.equal(dialog.closes, 0);
});

test("a presented sheet is modal, and Escape and the backdrop leave the same animated way as the close button", () => {
  environment();
  const dialog = new Dialog(), body = { children: [], append(node) { this.children.push(node); } };
  presentSheet(dialog, { document: { body } });
  assert.deepEqual(body.children, [dialog]); assert.equal(dialog.open, true);
  const escape = {}; dialog.fire("cancel", escape);
  assert.equal(escape.prevented, true, "the browser's own abrupt close is replaced");
  assert.equal(dialog.dataset.closing, "true");
  dialog.fire("animationend", { target: dialog }); assert.equal(dialog.open, false);

  const again = new Dialog(); presentSheet(again, { document: { body } });
  again.fire("click", { target: {} });
  assert.equal(again.dataset.closing, undefined, "a tap inside the sheet stays inside it");
  again.fire("click", { target: again });
  assert.equal(again.dataset.closing, "true", "a tap on the backdrop dismisses");
});

function presentWithHandle(options) {
  const timers = environment(options), dialog = new Dialog(), handle = new Handle();
  presentSheet(dialog, { document: { body: { append() {} } }, handles: [handle] });
  const touch = (type, clientY, timeStamp, extra = {}) => handle.fire(type, { pointerType: "touch", pointerId: 7, clientY, timeStamp, ...extra });
  return { dialog, handle, timers, touch };
}

test("pulling a sheet down by its handle follows the finger and lets go of it by distance or by flick", () => {
  const far = presentWithHandle();
  far.touch("pointerdown", 100, 0); far.touch("pointermove", 180, 400);
  assert.equal(far.dialog.style.transform, "translateY(80px)"); assert.equal(far.dialog.style.transition, "none");
  assert.equal(far.dialog.style["--sheet-alpha"], "0.800", "the backdrop thins as the sheet leaves");
  far.touch("pointermove", 260, 800); far.touch("pointerup", 260, 800);
  assert.equal(far.dialog.dataset.closing, "true", "past the distance it goes");

  const flick = presentWithHandle();
  flick.touch("pointerdown", 100, 0); flick.touch("pointermove", 120, 10); flick.touch("pointermove", 150, 30); flick.touch("pointerup", 150, 30);
  assert.equal(flick.dialog.dataset.closing, "true", "a short but quick flick goes too");

  const near = presentWithHandle();
  near.touch("pointerdown", 100, 0); near.touch("pointermove", 150, 600); near.touch("pointerup", 150, 600);
  assert.equal(near.dialog.dataset.closing, undefined, "a short, slow pull settles back");
  assert.equal(near.dialog.style.transform, ""); assert.match(near.dialog.style.transition, /^transform 360ms cubic-bezier/);
  assert.equal(near.dialog.style["--sheet-alpha"], undefined);
  near.timers.at(-1).callback(); assert.equal(near.dialog.style.transition, "", "the settle transition is cleared afterwards");

  const cancelled = presentWithHandle();
  cancelled.touch("pointerdown", 100, 0); cancelled.touch("pointermove", 300, 100); cancelled.touch("pointercancel", 300, 100);
  assert.equal(cancelled.dialog.dataset.closing, undefined, "an interrupted pull never dismisses");
});

test("pulling up resists like a rubber band, and mice, wide screens and other fingers do not pull", () => {
  const up = presentWithHandle();
  up.touch("pointerdown", 100, 0); up.touch("pointermove", 0, 100);
  assert.equal(up.dialog.style.transform, "translateY(-15px)");

  const mouse = presentWithHandle();
  mouse.touch("pointerdown", 100, 0, { pointerType: "mouse" }); mouse.touch("pointermove", 300, 100, { pointerType: "mouse" });
  assert.equal(mouse.dialog.style.transform, undefined);

  const wide = presentWithHandle({ wide: true });
  wide.touch("pointerdown", 100, 0); wide.touch("pointermove", 300, 100);
  assert.equal(wide.dialog.style.transform, undefined, "on a wide screen a sheet is a dialog, not something to pull");

  const other = presentWithHandle();
  other.touch("pointerdown", 100, 0); other.touch("pointermove", 300, 100, { pointerId: 8 }); other.touch("pointerup", 300, 100, { pointerId: 8 });
  assert.equal(other.dialog.style.transform, undefined); assert.equal(other.dialog.dataset.closing, undefined);
});

test("touching a title-bar control preserves its click target instead of capturing it for a sheet drag", () => {
  const s = presentWithHandle();
  const control = { closest: () => control };
  // Capturing at the header retargets the subsequent pointerup and click to that header,
  // so the close button inside it would never receive its activation on a touch screen.
  s.touch("pointerdown", 100, 0, { target: control });
  s.touch("pointermove", 260, 100, { target: control });
  s.touch("pointerup", 260, 100, { target: control });
  assert.deepEqual(s.handle.captures, []);
  assert.equal(s.dialog.style.transform, undefined);
  assert.equal(s.dialog.dataset.closing, undefined);
});

test("a second finger cannot replace the active sheet gesture", () => {
  const s = presentWithHandle();
  s.touch("pointerdown", 100, 0, { isPrimary: true });
  s.touch("pointerdown", 200, 10, { pointerId: 8, isPrimary: false });
  s.touch("pointermove", 160, 400, { isPrimary: true });
  assert.equal(s.dialog.style.transform, "translateY(60px)");
  assert.deepEqual(s.handle.captures, [7]);
});
