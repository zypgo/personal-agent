import assert from "node:assert/strict";
import { test } from "node:test";
import { FakeElement, elements, find, toHtml } from "./fake_dom.mjs";
import { createProductShell } from "../../src/personal_agent/channels/web/static/product-shell.js";

class Element extends FakeElement {
  constructor(tag, doc) {
    super(tag); this.ownerDocument = doc; this.dataset = {}; this.hidden = false; this.disabled = false;
    this.style = { setProperty(name, value) { this[name] = value; }, removeProperty(name) { delete this[name]; } };
    const change = (name, present) => { const names = new Set(this.className.split(/\s+/).filter(Boolean)); if (present) names.add(name); else names.delete(name); this.className = [...names].join(" "); };
    this.classList = { add: name => change(name, true), remove: name => change(name, false), toggle: (name, value) => change(name, value) };
  }
  append(...kids) { for (const kid of kids) { if (kid?.parentNode) kid.remove(); } super.append(...kids); for (const child of this.children) child.parentNode = this; }
  before(kid) { const parent = this.parentNode; const index = parent.children.indexOf(this); kid.parentNode = parent; parent.children.splice(index, 0, kid); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this); this.parentNode = null; }
  removeAttribute(name) { this.attrs.delete(name); }
  contains(other) { return this === other || elements(this).includes(other); }
  focus() { this.ownerDocument.activeElement = this; }
  closest(selector) { for (let item = this; item; item = item.parentNode) if (selector === "[hidden]" && item.hidden) return item; return null; }
  querySelectorAll() { return elements(this).filter(item => ["BUTTON", "SELECT", "INPUT"].includes(item.tagName) || item.tagName === "A" && item.hasAttribute("href") || item.hasAttribute("tabindex")); }
}
function setup({ narrow = false, stored, unavailable = false, navigate = true } = {}) {
  const data = new Map(stored ? [["pa-navigation", stored]] : []), listeners = new Map(), mediaListeners = new Map(), calls = [];
  const media = { matches: narrow, addEventListener: (type, callback) => mediaListeners.set(type, callback), removeEventListener: type => mediaListeners.delete(type) };
  const document = { activeElement: null, addEventListener: (type, callback) => listeners.set(type, callback), removeEventListener: type => listeners.delete(type) };
  document.createElement = tag => new Element(tag, document);
  document.createElementNS = (_, tag) => document.createElement(tag);
  document.documentElement = document.createElement("html"); document.body = document.createElement("body");
  document.defaultView = { matchMedia: () => media, localStorage: { getItem: key => { if (unavailable) throw Error("blocked"); return data.get(key); }, setItem: (key, value) => { if (unavailable) throw Error("blocked"); data.set(key, value); } } };
  const content = document.createElement("main"), select = document.createElement("select");
  document.body.append(content);
  let space = "个人 project";
  const shell = createProductShell({ document, content, current: "chat", spaceControl: select, resolve: path => { const url = new URL(path, "https://example.invalid"); url.searchParams.set("space", space); return url.pathname + url.search; }, onNavigate: navigate ? key => calls.push(key) : undefined });
  content.append(shell.toggle);
  const drawer = find(shell.element, "aside")[0], overlay = elements(shell.element).find(item => item.className === "product-drawer-overlay"), nav = find(shell.element, "nav")[0];
  return { document, content, shell, drawer, overlay, nav, data, calls, listeners, mediaListeners, resize: value => { media.matches = value; mediaListeners.get("change")?.(); }, setSpace: value => { space = value; } };
}
function click(element, extra = {}) { const event = { defaultPrevented: false, button: 0, preventDefault() { this.defaultPrevented = true; }, ...extra }; element.listeners.get("click")(event); return event; }
function key(s, value, extra = {}) { const event = { key: value, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...extra }; s.listeners.get("keydown")?.(event); return event; }

test("chat is the first destination and all existing tools retain their scoped deep links", () => {
  const s = setup();
  const links = find(s.nav, "a");
  assert.deepEqual(links.map(item => item.textContent), ["个人助理", "任务", "资料", "助手与连接", "项目与成员", "设备与账号"]);
  assert.equal(links[0].getAttribute("aria-current"), "page");
  for (const [index, tab] of ["", "tasks", "knowledge", "agents", "spaces", "devices"].entries()) {
    const url = new URL(links[index].getAttribute("href"), "https://example.invalid");
    assert.equal(url.pathname, index ? "/workspace" : "/");
    assert.equal(url.searchParams.get("tab"), tab || null);
    assert.equal(url.searchParams.get("space"), "个人 project");
  }
  s.setSpace("new-space"); s.shell.setContext({ name: "新用户", space: "新空间", role: "成员" });
  assert.ok(find(s.nav, "a").every(link => link.getAttribute("href").includes("space=new-space")));
});

test("desktop collapse persists only its layout choice and removes hidden navigation from focus", () => {
  const s = setup(); assert.equal(s.shell.element.dataset.drawerOpen, "true");
  click(s.shell.toggle);
  assert.equal(s.drawer.inert, true); assert.equal(s.drawer.getAttribute("aria-hidden"), "true");
  assert.equal(s.shell.toggle.getAttribute("aria-expanded"), "false");
  assert.equal(s.data.get("pa-navigation"), "collapsed");
  s.shell.setContext({ name: "<img src=x>", space: "私密名称", role: "负责人" });
  assert.equal(s.data.size, 1); assert.match(toHtml(s.drawer), /&lt;img src=x&gt;/);
  const restored = setup({ stored: s.data.get("pa-navigation") }); assert.equal(restored.drawer.inert, true);
});

test("mobile starts closed, traps focus while open, and Escape restores the menu button", () => {
  const s = setup({ narrow: true }); assert.equal(s.drawer.inert, true); assert.equal(s.content.inert, false);
  click(s.shell.toggle);
  assert.equal(s.drawer.getAttribute("aria-modal"), "true"); assert.equal(s.content.inert, true); assert.equal(s.overlay.hidden, false);
  const focusable = s.drawer.querySelectorAll(), first = focusable[0], last = focusable.at(-1);
  last.focus(); assert.equal(key(s, "Tab").defaultPrevented, true); assert.equal(s.document.activeElement, first);
  first.focus(); key(s, "Tab", { shiftKey: true }); assert.equal(s.document.activeElement, last);
  assert.equal(key(s, "Escape").defaultPrevented, true);
  assert.equal(s.document.activeElement, s.shell.toggle); assert.equal(s.content.inert, false); assert.equal(s.overlay.hidden, true);
  assert.equal(s.data.size, 0, "a mobile overlay must not overwrite the desktop preference");
});

test("overlay dismiss and a viewport change never strand focus inside a closed drawer", () => {
  const s = setup({ narrow: true }); click(s.shell.toggle); click(s.overlay);
  assert.equal(s.document.activeElement, s.shell.toggle);
  s.resize(false); assert.equal(s.drawer.inert, false);
  find(s.nav, "a")[1].focus(); s.resize(true);
  assert.equal(s.drawer.inert, true); assert.equal(s.document.activeElement, s.shell.toggle);
  assert.equal(s.content.inert, false);
});

test("tool navigation closes the mobile overlay and keeps modified clicks and chat links native", () => {
  const s = setup({ narrow: true }), links = find(s.nav, "a");
  click(s.shell.toggle); assert.equal(click(links[2]).defaultPrevented, true);
  assert.deepEqual(s.calls, ["knowledge"]); assert.equal(s.content.inert, false);
  assert.equal(click(links[0]).defaultPrevented, false);
  assert.equal(click(links[1], { ctrlKey: true }).defaultPrevented, false);
  assert.deepEqual(s.calls, ["knowledge"]);
  s.shell.setActive("knowledge"); assert.equal(links[2].getAttribute("aria-current"), "page"); assert.equal(links[0].getAttribute("aria-current"), null);
});

test("unavailable storage leaves navigation usable and destroy removes global listeners", () => {
  const s = setup({ unavailable: true, narrow: true });
  click(s.shell.toggle); assert.equal(s.content.inert, true);
  s.shell.destroy(); assert.equal(s.content.inert, false); assert.equal(s.listeners.size, 0); assert.equal(s.mediaListeners.size, 0);
  assert.ok(!s.document.body.className.includes("product-menu-open"));
});

test("on a wide screen the drawer takes focus when it opens and the bar's button gets it back when it closes", () => {
  const s = setup({ stored: "collapsed" }); assert.equal(s.shell.element.dataset.drawerOpen, "false");
  click(s.shell.toggle);
  assert.equal(s.shell.element.dataset.drawerOpen, "true");
  const close = elements(s.drawer).find(item => item.className === "product-drawer-close");
  assert.equal(s.document.activeElement, close, "the bar's own button is hidden while the drawer is open, so focus must move with it");
  click(close);
  assert.equal(s.shell.element.dataset.drawerOpen, "false"); assert.equal(s.document.activeElement, s.shell.toggle);
});

const touch = extra => ({ pointerType: "touch", isPrimary: true, pointerId: 1, clientX: 0, clientY: 0, timeStamp: 0, target: null, ...extra });
// A finger: down at the first point, moving through the rest, up at the last. Points are [x, y, ms].
function swipe(s, points, extra = {}) {
  const at = ([clientX, clientY, timeStamp]) => touch({ clientX, clientY, timeStamp, ...extra });
  s.listeners.get("pointerdown")(at(points[0]));
  for (const point of points.slice(1)) s.listeners.get("pointermove")(at(point));
  s.listeners.get("pointerup")(at(points.at(-1)));
}
const isOpen = s => s.shell.element.dataset.drawerOpen;

test("a finger pulls the drawer open or pushes it shut by how far it went, or how fast", () => {
  const s = setup({ narrow: true });
  swipe(s, [[100, 300, 0], [150, 302, 300], [200, 303, 600]]);
  assert.equal(isOpen(s), "false", "a short, slow drag springs back");
  swipe(s, [[100, 300, 0], [200, 302, 300], [300, 303, 600]]);
  assert.equal(isOpen(s), "true", "past halfway it stays open");
  assert.equal(s.shell.element.dataset.dragging, undefined);
  assert.equal(s.document.activeElement, null, "a finger needs no focus ring");
  swipe(s, [[250, 300, 0], [240, 301, 300], [230, 302, 600]]);
  assert.equal(isOpen(s), "true", "a short drag back leaves it open");
  swipe(s, [[250, 300, 0], [220, 301, 20], [190, 302, 40]]);
  assert.equal(isOpen(s), "false", "a quick flick toward the edge closes it however short");
  swipe(s, [[100, 300, 0], [130, 301, 20], [160, 302, 40]]);
  assert.equal(isOpen(s), "true", "and a quick flick the other way opens it");
});

test("a drag that belongs to something else never moves the drawer", () => {
  const s = setup({ narrow: true });
  swipe(s, [[10, 300, 0], [200, 302, 100], [300, 303, 200]]);
  assert.equal(isOpen(s), "false", "the screen edge is kept for the browser's own back gesture");
  const field = { closest: selector => (selector.includes("textarea") ? {} : null) };
  swipe(s, [[100, 300, 0], [200, 302, 100], [300, 303, 200]], { target: field });
  assert.equal(isOpen(s), "false", "fields, code blocks and tables scroll sideways themselves");
  swipe(s, [[100, 300, 0], [110, 340, 50], [120, 380, 100]]);
  assert.equal(isOpen(s), "false", "a mostly vertical drag is the page scrolling");
  swipe(s, [[100, 300, 0], [200, 302, 100], [300, 303, 200]], { pointerType: "mouse" });
  swipe(s, [[100, 300, 0], [200, 302, 100], [300, 303, 200]], { isPrimary: false });
  assert.equal(isOpen(s), "false", "only the first finger counts");
  const desktop = setup();
  swipe(desktop, [[100, 300, 0], [200, 302, 100], [10, 303, 200]]);
  assert.equal(isOpen(desktop), "true", "the sidebar on a wide screen is not a gesture");
});

test("an interrupted drag leaves the drawer as it was", () => {
  const s = setup({ narrow: true });
  s.listeners.get("pointerdown")(touch({ clientX: 100, clientY: 300, timeStamp: 0 }));
  s.listeners.get("pointermove")(touch({ clientX: 220, clientY: 302, timeStamp: 100 }));
  assert.equal(s.shell.element.dataset.dragging, "true"); assert.equal(s.overlay.hidden, false);
  s.listeners.get("pointercancel")(touch({ clientX: 220, clientY: 302, timeStamp: 120 }));
  assert.equal(isOpen(s), "false"); assert.equal(s.shell.element.dataset.dragging, undefined);
  s.shell.destroy(); assert.equal(s.listeners.size, 0);
});

test("changing the viewport during a drag discards its partial position and accepts a new gesture", () => {
  const s = setup({ narrow: true });
  s.listeners.get("pointerdown")(touch({ clientX: 100, clientY: 300, timeStamp: 0 }));
  s.listeners.get("pointermove")(touch({ clientX: 220, clientY: 302, timeStamp: 100 }));
  assert.equal(s.shell.element.dataset.dragging, "true");
  assert.equal(s.shell.element.style["--drawer-p"], "0.375");
  s.resize(false);
  assert.equal(s.shell.element.dataset.dragging, undefined);
  assert.equal(s.shell.element.style["--drawer-p"], undefined);
  assert.equal(isOpen(s), "true");
  s.resize(true);
  assert.equal(isOpen(s), "false");
  swipe(s, [[100, 300, 200], [200, 302, 500], [300, 303, 800]], { pointerId: 2 });
  assert.equal(isOpen(s), "true", "a gesture interrupted by rotation cannot keep the next finger locked out");
});
