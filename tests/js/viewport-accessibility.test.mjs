import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

// Exercise the page's real visual-viewport handler. Browser zoom also reduces
// visualViewport.height; it must not be mistaken for the software keyboard.
const source = readFileSync(new URL("../../src/personal_agent/channels/web/static/app.js", import.meta.url), "utf8");
const start = source.indexOf("function watchViewport() {");
const end = source.indexOf("\nfunction setBadge(", start);
assert.ok(start >= 0 && end > start);
const viewportHandler = source.slice(start, end);

function setup() {
  const handlers = new Map();
  const viewport = {
    height: 800, offsetTop: 0, scale: 1,
    addEventListener(type, listener) { handlers.set(type, listener); },
    removeEventListener(type, listener) { if (handlers.get(type) === listener) handlers.delete(type); },
  };
  let scrolls = 0;
  const context = {
    window: { innerHeight: 800, visualViewport: viewport },
    root: { style: {} }, ui: {}, pinned: true,
    scrollToBottom() { scrolls++; },
  };
  runInNewContext(`${viewportHandler}\nglobalThis.cleanup = watchViewport();`, context);
  return {
    context, viewport, handlers,
    resize(values) { Object.assign(viewport, values); handlers.get("resize")?.(); },
    get scrolls() { return scrolls; },
  };
}

test("software keyboard sizing remains active at normal zoom and teardown restores the page", () => {
  const s = setup();
  s.resize({ height: 420, offsetTop: 18 });
  assert.equal(s.context.root.style.height, "420px");
  assert.equal(s.context.root.style.transform, "translateY(18px)");
  assert.equal(s.scrolls, 1);
  s.context.cleanup();
  assert.equal(s.handlers.size, 0);
  assert.equal(s.context.root.style.height, "");
  assert.equal(s.context.root.style.transform, "");
});

test("pinch zoom never shrinks the layout or forces the conversation back to the latest message", () => {
  const s = setup();
  s.resize({ scale: 2, height: 400, offsetTop: 150 });
  assert.equal(s.context.root.style.height, "");
  assert.equal(s.context.root.style.transform, "");
  assert.equal(s.scrolls, 0);
  s.resize({ scale: 1, height: 420, offsetTop: 18 });
  assert.equal(s.context.root.style.height, "420px");
  assert.equal(s.scrolls, 1);
  s.resize({ scale: 1.5, height: 280, offsetTop: 90 });
  assert.equal(s.context.root.style.height, "");
  assert.equal(s.context.root.style.transform, "");
  assert.equal(s.scrolls, 1);
});
