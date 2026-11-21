import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { FakeElement, FakeText, elements, toHtml } from "./fake_dom.mjs";
import { Store } from "../../src/personal_agent/channels/web/static/store.js";

// The chat page when the conversation is shared: what a person is told when their access changes,
// what a visitor can do, and how what other people said is shown. The real page script runs here,
// with its network and its browser replaced by things a test can answer for.

class Element extends FakeElement {
  constructor(tag) {
    super(tag); this.value = ""; this.dataset = {}; this.style = {}; this.hidden = false;
    this.events = new Map(); this.files = []; this.disabled = false; this.open = false;
  }
  append(...nodes) { super.append(...nodes); for (const child of this.children) child.parentElement = this; }
  replaceChildren(...nodes) { for (const child of this.children) child.parentElement = null; this.children = []; this.append(...nodes); }
  remove() {
    if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(item => item !== this);
    this.parentElement = null;
  }
  addEventListener(type, callback) {
    if (!this.events.has(type)) this.events.set(type, new Set());
    this.events.get(type).add(callback);
  }
  removeEventListener(type, callback) { this.events.get(type)?.delete(callback); }
  async fire(type, event = {}) { return await Promise.all([...(this.events.get(type) || [])].map(callback => callback({ preventDefault() {}, ...event }))); }
  contains(node) { return node === this || elements(this).includes(node); }
  querySelectorAll(selector) { return selector === "button" ? elements(this).filter(item => item.tagName === "BUTTON") : []; }
  focus() { if (this.ownerDocument) this.ownerDocument.activeElement = this; }
  get isConnected() { return this.root === true || Boolean(this.parentElement?.isConnected); }
}

const source = readFileSync(new URL("../../src/personal_agent/channels/web/static/app.js", import.meta.url), "utf8")
  .replace(/^import\b[\s\S]*?;\n/gm, "")
  .replace("import.meta.url", '"https://example.invalid/static/app.js"')
  .replace(/\nmain\(\);\s*$/, "");

const settle = async () => { for (let index = 0; index < 12; index++) await Promise.resolve(); };
const refused = status => Object.assign(new Error(`HTTP ${status}`), { status });
const PROJECT = { id: "space-p", name: "产品研究", kind: "project" };
const PERSONAL = { id: "space-me", name: "Personal", kind: "personal" };
const account = (role, spaces = [PERSONAL, PROJECT], current = PROJECT.id) =>
  ({ principal: { id: "me", display_name: "小明", is_admin: false }, role, space_id: current, spaces });
const members = [
  { principal_id: "me", display_name: "小明", role: "owner" },
  { principal_id: "bob", display_name: "小红", role: "member" },
];
const said = (id, who, text = "你好", extra = {}) => ({ id, direction: "in", channel: "web", kind: "message", text, at: 1700000000 + id, meta: who ? { principal_id: who } : {}, ...extra });
const reply = (id, text = "好的") => ({ id, direction: "out", channel: "web", kind: "message", text, at: 1700000000 + id, meta: {} });

function chat({ search = "", respond = async () => ({}), whoami = async () => account("member") } = {}) {
  const root = new Element("div"), body = new Element("body"), window = new Element("window");
  window.visualViewport = new Element("viewport"); window.innerHeight = 800;
  root.root = true; body.root = true;
  const document = {
    getElementById: () => root, createElement: tag => Object.assign(new Element(tag), { ownerDocument: document }),
    createElementNS: (_ns, tag) => Object.assign(new Element(tag), { ownerDocument: document }), createTextNode: text => new FakeText(text),
    addEventListener() {}, querySelectorAll: () => [], body, activeElement: body, documentElement: new Element("html"), title: "栖序", hidden: false,
  };
  root.ownerDocument = document; body.ownerDocument = document;
  const calls = [], asked = [], toasts = [], assigned = [], replaced = [], streams = [], contexts = [];
  const location = { href: `https://example.invalid/${search}`, origin: "https://example.invalid", pathname: "/", search, hash: "", assign: path => { assigned.push(path); } };
  const history = { replaceState: (_state, _title, path) => { replaced.push(path); const at = path.indexOf("?"); location.search = at < 0 ? "" : path.slice(at); } };
  const context = {
    document, window, location, history, navigator: {}, URL, URLSearchParams, FormData, AbortController, Store,
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    onAccessLost() {}, invalidateRequests() {}, setRequestContext() {}, scopedURL: path => path,
    whoAmI: async (...args) => { asked.push(args); return await whoami(...args); },
    api: async (path, options) => { calls.push({ path, options }); return await respond(path, options); },
    createAgentMark: () => new Element("span"), renderMarkdown: (_doc, text) => Object.assign(new Element("p"), { textContent: text }),
    clockTime: () => "12:00", commandHints: () => [], canRecord: () => false, newClientId: () => "client-a", parsePairing: () => "",
    pageTitle: (agent, unread = 0) => (unread ? `(${unread}) ` : "") + agent, matchMedia: () => ({ matches: false }), localStorage: { getItem() { return null; } },
    record: async () => null, audioDuration: async () => 1, shrinkImage: async file => file,
    toasts, contexts, streams,
  };
  runInNewContext(source + `
    toast = (message, options = {}) => toasts.push({ message, error: Boolean(options.error) });
    renderLog = () => {};
    scrollToBottom = () => {};
    globalThis.page = {
      lostAccess, recheckAccess, showLeft, start, welcomeJoin, applyRole, loadAssistantSpaces, loadPeople, turnNode, turnSignature, onTurn, deliver, submit, decide, addFiles,
      get ui() { return ui; }, get live() { return live; }, get pending() { return store.pending; }, get unread() { return unread; },
      attach() {
        session = { agent: "产品研究", principal_id: "me", features: { images: true, voice: true, max_images: 2, max_image_bytes: 8000000, max_voice_bytes: 10000000, voice_seconds: 60 } };
        store.principalId = "me";
        ui = {
          composer: buildComposer(), cards: { destroy() {}, refresh: async () => {} }, log: { scrollHeight: 0, scrollTop: 0, clientHeight: 0 },
          spaceSelect: document.createElement("select"), toasts: document.createElement("div"),
          shell: { setContext: value => contexts.push(value), destroy() {} },
        };
        live = { state: "closed", start() { this.state = "open"; streams.push("start"); }, stop() { this.state = "closed"; } };
        return ui;
      },
    };
  `, context);
  return { page: context.page, root, body, document, calls, asked, toasts, assigned, replaced, streams, contexts, location };
}

// The page builds its objects where it runs; to compare them with plain ones, look at their JSON.
const plain = value => JSON.parse(JSON.stringify(value));

// ---- who is told what when access changes -----------------------------------------------------------

test("a request refused as 401 means this device is signed out, and says so without asking anyone", async () => {
  const c = chat(); c.page.attach();
  c.page.lostAccess(401); await settle();
  assert.match(c.root.textContent, /当前设备已退出/);
  assert.equal(c.asked.length, 0);
  assert.equal(c.page.ui, null);
});

test("a request refused as 403 in a project the person was taken out of says so, and is no sign-out", async () => {
  const c = chat({ search: "?space=space-p", whoami: async () => account("owner", [PERSONAL], PERSONAL.id) });
  c.page.attach();
  c.page.lostAccess(403); await settle();
  assert.match(c.root.textContent, /你已不在「产品研究」里了/);
  assert.doesNotMatch(c.root.textContent, /当前设备已退出/);
  assert.equal(c.page.ui, null, "the project's conversation is closed");
  await elements(c.root).find(item => item.tagName === "BUTTON").fire("click");
  assert.deepEqual(c.assigned, ["/"], "their own conversation is one button away");
});

test("a 403 from a browser that really is signed out still says the device signed out", async () => {
  const c = chat({ search: "?space=space-p", whoami: async () => 401 }); c.page.attach();
  c.page.lostAccess(403); await settle();
  assert.match(c.root.textContent, /当前设备已退出/);
});

test("a 403 for someone who is still in the project only re-reads what they may do, and reopens the stream", async () => {
  const c = chat({ search: "?space=space-p", respond: async path => path === "/api/v3/me" ? account("viewer") : path === "/api/v3/members" ? { members } : {} });
  const view = c.page.attach();
  c.page.applyRole("member"); // what they were allowed when the page opened
  c.page.lostAccess(403); await settle();
  assert.equal(c.page.ui, view, "still in the conversation");
  assert.equal(view.composer.readOnly, true, "and now only to read");
  assert.deepEqual(plain(c.toasts), [{ message: "你在这个项目里的权限变了：现在是访客，能看，不能发言。", error: false }]);
  assert.deepEqual(plain(c.contexts.at(-1)), { name: "小明", space: "产品研究", role: "访客" });
  assert.deepEqual(c.streams, ["start"], "the stream the server closed is opened again");
});

test("a refusal that arrives while the stream is open does not reopen it", async () => {
  const c = chat({ search: "?space=space-p", respond: async path => path === "/api/v3/me" ? account("member") : {} });
  c.page.attach(); c.page.live.state = "open";
  c.page.lostAccess(403); await settle();
  assert.deepEqual(c.streams, []);
});

test("not reaching the server while checking is not taken for a sign-out", async () => {
  const c = chat({ search: "?space=space-p", whoami: async () => 0 });
  const view = c.page.attach();
  c.page.lostAccess(403); await settle();
  assert.equal(c.page.ui, view, "nothing was learned, so nothing is closed");
  assert.deepEqual(plain(c.toasts), [{ message: "暂时连不上，稍后再试。", error: true }]);
  assert.deepEqual(c.streams, ["start"], "the stream is tried again, which asks the same question later");
});

test("several refused requests at once make one check", async () => {
  const c = chat({ search: "?space=space-p" }); c.page.attach();
  c.page.lostAccess(403); c.page.lostAccess(403); c.page.lostAccess(403); await settle();
  assert.equal(c.asked.length, 1);
});

test("opening a project address that is not theirs says so instead of asking them to pair again", async () => {
  const c = chat({ search: "?space=space-p", respond: async path => { if (path === "/api/session") throw refused(403); return {}; } });
  await c.page.start();
  assert.match(c.root.textContent, /你不在这个项目里/);
  assert.doesNotMatch(c.root.textContent, /连接你的个人助理/);
});

test("an address with no one signed in still asks to pair", async () => {
  const c = chat({ search: "?space=space-p", respond: async path => { if (path === "/api/session") throw refused(401); return {}; } });
  await c.page.start();
  assert.match(c.root.textContent, /连接你的个人助理/);
});

// ---- what a visitor can do ----------------------------------------------------------------------------

test("a visitor can read the conversation, but the box to write in is shut and says why", () => {
  const c = chat(), { composer } = c.page.attach();
  c.page.applyRole("viewer");
  assert.equal(composer.readOnly, true);
  assert.equal(composer.input.disabled, true);
  assert.equal(composer.input.getAttribute("placeholder"), "你是访客：能看对话，不能发言");
  assert.equal(composer.send.disabled, true);
  assert.match(composer.row.className, /\breadonly\b/);
  assert.deepEqual(c.toasts, [], "the first reading says nothing: they were told when they joined");
});

test("nothing a visitor does can send a message, queue one, or add a picture", async () => {
  const c = chat(), { composer } = c.page.attach();
  c.page.applyRole("viewer");
  composer.input.value = "我想说一句";
  await c.page.submit();
  assert.equal(c.page.pending.length, 0);
  assert.equal(c.calls.some(call => call.path === "/api/messages"), false);
  await c.page.addFiles([{ name: "a.png", type: "image/png", size: 10 }]);
  assert.equal(composer.files.length, 0);
});

test("becoming a member again opens the box, and says what changed", () => {
  const c = chat(), { composer } = c.page.attach();
  c.page.applyRole("viewer"); c.page.applyRole("member");
  assert.equal(composer.readOnly, false);
  assert.equal(composer.input.disabled, false);
  assert.equal(composer.input.getAttribute("placeholder"), "说说你想做的事…");
  assert.doesNotMatch(composer.row.className, /readonly/);
  assert.deepEqual(plain(c.toasts), [{ message: "你在这个项目里的权限变了：现在是成员。", error: false }]);
  composer.input.value = "能说话了"; composer.refresh();
  assert.equal(composer.send.disabled, false);
});

test("the space picker calls a personal space 个人空间, and the header says what the person is here", async () => {
  const c = chat({ respond: async path => path === "/api/v3/me" ? account("member") : path === "/api/v3/members" ? { members } : {} });
  const view = c.page.attach(); await c.page.loadAssistantSpaces();
  assert.deepEqual(view.spaceSelect.children.map(option => option.textContent), ["个人空间", "产品研究"]);
  assert.deepEqual(plain(c.contexts.at(-1)), { name: "小明", space: "产品研究", role: "成员" });
  const alone = chat({ respond: async path => path === "/api/v3/me" ? account("owner", [PERSONAL], PERSONAL.id) : {} });
  alone.page.attach(); await alone.page.loadAssistantSpaces();
  assert.deepEqual(plain(alone.contexts.at(-1)), { name: "小明", space: "个人空间", role: "仅自己可见" });
});

test("a refused message is kept, with the reason in plain words, and the access is looked at", async () => {
  const c = chat({
    respond: async path => { if (path === "/api/messages") throw refused(403); return path === "/api/v3/me" ? account("viewer") : {}; },
    whoami: async () => account("viewer"),
  });
  const { composer } = c.page.attach();
  composer.input.value = "你好";
  await c.page.submit(); await settle();
  assert.equal(c.page.pending.length, 1, "the words are not thrown away");
  assert.equal(c.page.pending[0].state, "failed");
  assert.equal(c.page.pending[0].error, "你现在不能在这个项目里发言，消息没有发出。");
  assert.equal(c.asked.length, 1);
  assert.doesNotMatch(c.root.textContent, /当前设备已退出/);
  assert.equal(composer.readOnly, true, "and the box is now shut, which is what is true");
});

test("a decision the person may not make is refused in words, and is no sign-out", async () => {
  const c = chat({ respond: async path => { if (path.startsWith("/api/approvals/")) throw refused(403); return {}; }, whoami: async () => account("member") });
  c.page.attach();
  const card = new Element("div"), button = new Element("button"); card.append(button);
  await c.page.decide({ id: "ap1" }, "approve", card); await settle();
  assert.deepEqual(plain(c.toasts), [{ message: "你在这个项目里不能处理这条请求。", error: true }]);
  assert.equal(button.disabled, false, "they can try something else");
  assert.equal(c.asked.length, 1);
  assert.notEqual(c.page.ui, null);
});

test("a decision refused as 401 does sign the device out", async () => {
  const c = chat({ respond: async path => { if (path.startsWith("/api/approvals/")) throw refused(401); return {}; } });
  c.page.attach();
  await c.page.decide({ id: "ap1" }, "approve", new Element("div")); await settle();
  assert.match(c.root.textContent, /当前设备已退出/);
});

// ---- the welcome -----------------------------------------------------------------------------------------

test("arriving from an invitation says where you are once, and tidies the address", () => {
  const c = chat({ search: "?space=space-p&joined=member" }); c.page.attach();
  c.page.welcomeJoin();
  assert.deepEqual(plain(c.toasts), [{ message: "已加入「产品研究」。这里说的话，项目里的人都看得到。", error: false }]);
  assert.deepEqual(c.replaced, ["/?space=space-p"], "the project stays in the address, the welcome does not");
  c.page.welcomeJoin();
  assert.equal(c.toasts.length, 1, "only the once");
});

test("a visitor's welcome says what they may and may not do", () => {
  const c = chat({ search: "?joined=viewer&space=space-p" }); c.page.attach();
  c.page.welcomeJoin();
  assert.equal(c.toasts[0].message, "已加入「产品研究」。你是访客：能看对话，不能发言。");
  assert.deepEqual(c.replaced, ["/?space=space-p"]);
});

test("without an invitation there is no welcome and the address is left alone", () => {
  const c = chat({ search: "?space=space-p" }); c.page.attach();
  c.page.welcomeJoin();
  assert.deepEqual(c.toasts, []); assert.deepEqual(c.replaced, []);
});

// ---- who said what -----------------------------------------------------------------------------------------

test("what other people in the project said is on their side with their name, and what I said is on mine", async () => {
  const c = chat({ respond: async path => path === "/api/v3/members" ? { members } : {} });
  c.page.attach(); await c.page.loadPeople();
  const theirs = c.page.turnNode(said(1, "bob", "大家好"), null);
  assert.match(theirs.className, /\bother\b/);
  assert.doesNotMatch(theirs.className, /\bmine\b/);
  assert.match(toHtml(theirs), /小红/);
  assert.match(toHtml(theirs), /大家好/);
  const mine = c.page.turnNode(said(2, "me", "收到"), said(1, "bob"));
  assert.match(mine.className, /\bmine\b/);
  assert.doesNotMatch(toHtml(mine), /小明/, "nobody is shown their own name");
  const answer = c.page.turnNode(reply(3), said(2, "me"));
  assert.match(answer.className, /\bagent\b/);
  assert.match(toHtml(answer), /产品研究/, "the assistant is named by the project it works in");
});

test("the instance owner, who has not given themselves a name, is 负责人 to everyone else and not Owner", async () => {
  const c = chat({ respond: async path => path === "/api/v3/members" ? { members: [{ principal_id: "owner", display_name: "Owner", role: "owner" }, members[0]] } : {} });
  c.page.attach(); await c.page.loadPeople();
  const html = toHtml(c.page.turnNode(said(1, "owner", "我来安排"), null));
  assert.match(html, /负责人/);
  assert.doesNotMatch(html, /Owner/);
});

test("a person is named when they start talking, and again after someone else did, not on every line", async () => {
  const c = chat({ respond: async path => path === "/api/v3/members" ? { members } : {} });
  c.page.attach(); await c.page.loadPeople();
  const first = said(1, "bob", "第一句"), second = said(2, "bob", "第二句"), mine = said(3, "me", "我说"), again = said(4, "bob", "我又说");
  assert.match(toHtml(c.page.turnNode(first, null)), /小红/);
  assert.doesNotMatch(toHtml(c.page.turnNode(second, first)), /小红/);
  assert.match(toHtml(c.page.turnNode(again, mine)), /小红/);
});

test("a message with nobody recorded is the person looking at it, as it always was", () => {
  const c = chat(); c.page.attach();
  assert.match(c.page.turnNode(said(1, ""), null).className, /\bmine\b/);
  assert.match(c.page.turnNode({ ...said(2, "me"), channel: "telegram" }, null).className, /\bmine\b/);
});

test("someone who is not on the list is called 项目成员 until the list catches up, and the log is drawn again when it does", async () => {
  let listing = [members[0]];
  const c = chat({ respond: async path => path === "/api/v3/members" ? { members: listing } : {} });
  c.page.attach(); await c.page.loadPeople();
  const turn = said(1, "bob", "我是新来的");
  assert.match(toHtml(c.page.turnNode(turn, null)), /项目成员/);
  const before = c.page.turnSignature(turn, null), mineBefore = c.page.turnSignature(said(2, "me"), turn);
  listing = members; await c.page.loadPeople();
  assert.match(toHtml(c.page.turnNode(turn, null)), /小红/);
  assert.notEqual(c.page.turnSignature(turn, null), before, "a line that names someone is redrawn when the names change");
  assert.equal(c.page.turnSignature(said(2, "me"), turn), mineBefore, "a line that names no one is left alone");
});

test("a message from someone who joined after the page opened fetches the member list once", async () => {
  let listed = 0;
  const c = chat({ respond: async path => { if (path === "/api/v3/members") { listed += 1; return { members }; } return {}; } });
  c.page.attach();
  c.page.onTurn(said(1, "bob")); c.page.onTurn(said(2, "bob")); await settle();
  assert.equal(listed, 1);
});

test("while the page is in the background, what others say counts as unread and what I said does not", () => {
  const c = chat(); c.page.attach(); c.document.hidden = true;
  c.page.onTurn(said(1, "bob")); assert.equal(c.page.unread, 1);
  c.page.onTurn(said(2, "me")); assert.equal(c.page.unread, 1);
  c.page.onTurn(reply(3)); assert.equal(c.page.unread, 2);
  assert.equal(c.document.title, "(2) 产品研究");
});
