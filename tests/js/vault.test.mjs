import assert from "node:assert/strict";
import { test } from "node:test";
import { FakeElement, FakeText, elements, find, toHtml } from "./fake_dom.mjs";
import { AREAS, bodyOf, careLabel, createVaultUI, issueCount, kindLabel, notePath, statusLine, trailOf, whatLabel, whoLabel, withLinksShown } from "../../src/personal_agent/channels/web/static/vault.js";

class Element extends FakeElement {
  constructor(tag) {
    super(tag);
    this.dataset = {};
    this.value = "";
    this.disabled = false;
    this.hidden = false;
    this.open = false;
    this.classList = {
      add: (value) => (this.className = `${this.className} ${value}`.trim()),
      toggle: (value, on) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        if (on) names.add(value); else names.delete(value);
        this.className = [...names].join(" ");
      },
    };
  }
  append(...kids) { super.append(...kids); for (const kid of this.children) kid.parentElement = this; }
  replaceChildren(...kids) { for (const kid of this.children) kid.parentElement = null; this.children = []; this.append(...kids); }
  removeAttribute(name) { this.attrs.delete(name); }
  focus() { this.focused = true; }
  contains(value) { return this === value || elements(this).includes(value); }
  querySelector(selector) { return find(this, selector)[0] || null; }
  showModal() { this.open = true; }
  close() { this.open = false; this.listeners.get("close")?.(); }
  get elements() {
    const controls = elements(this).filter((el) => ["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(el.tagName));
    controls.namedItem = (name) => controls.find((el) => el.name === name) || null;
    return controls;
  }
}

const STATUS = { enabled: true, exists: true, root: "/Users/me/vault", notes: 4, areas: { wiki: 2, raw: 1, threads: 1 }, other: 0, skipped: 0, backlog: 1, issues: { uncompiled: 1, broken_link: 1 } };
const LISTING = {
  folder: "", query: "", folders: [{ name: "wiki", count: 2 }, { name: "raw", count: 1 }],
  notes: [{ path: "inbox.md", title: "收件", summary: "随手记的", type: "", status: "", updated: 100, outside: false }],
};
const note = (extra = {}) => ({
  path: "wiki/张三.md", title: "张三", text: "---\ntype: person\n---\n# 张三\n负责预算，见 [[预算|预算表]] 和 [[还没写]]。", rev: "r1", type: "person", status: "",
  origin: "owner", outside: false, generated: false, tags: ["同事"], aliases: [], outgoing: [{ path: "wiki/预算.md", title: "预算" }], missing: ["还没写"],
  backlinks: [{ path: "wiki/会议.md", title: "会议" }], updated: 1700000000, size: 80, obsidian: "obsidian://open?path=%2FUsers%2Fme%2Fvault%2Fwiki%2F%E5%BC%A0%E4%B8%89.md", ...extra,
});

function setup({ role = "owner", routes = {}, confirm = true, status = STATUS, listing = LISTING } = {}) {
  const ids = new Map();
  const get = (id) => {
    if (!ids.has(id)) ids.set(id, new Element("div"));
    return ids.get(id);
  };
  const doc = {
    getElementById: get,
    createElement: (tag) => new Element(tag),
    createTextNode: (text) => new FakeText(text),
    createDocumentFragment: () => new Element("#fragment"),
    activeElement: null,
  };
  doc.createDocumentFragment = () => { const el = new Element("#fragment"); el.nodeType = 11; return el; };
  get("vault-care").hidden = true;
  const context = { epoch: 0, space: "space-a", me: { principal: { id: "alice" }, role } };
  const calls = [], notices = [], copied = [], confirms = [];
  const api = async (path, options = {}) => {
    calls.push({ path, method: options.method || "GET", body: options.body });
    const handler = routes[`${options.method || "GET"} ${path.split("?")[0]}`];
    if (handler) return await handler(path, options);
    if (path === "/api/v3/vault") return status;
    if (path.startsWith("/api/v3/vault/notes")) return listing;
    if (path.startsWith("/api/v3/vault/note?")) return { note: note() };
    if (path === "/api/v3/vault/lint") return { issues: [{ kind: "uncompiled", path: "raw/agents/Grok/r.md", detail: "还没有整理进 wiki/" }, { kind: "broken_link", path: "wiki/张三.md", detail: "[[还没写]] 找不到对应的笔记" }], counts: {} };
    throw new Error(`unexpected ${path}`);
  };
  const ui = createVaultUI({
    api, getContext: () => context, document: doc,
    notice: (text, error) => notices.push([text, Boolean(error)]),
    describe: (error) => `失败：${error.message}`,
    confirm: (message) => { confirms.push(message); return confirm; },
    copy: async (text) => { copied.push(text); },
  });
  return { ui, get, doc, context, calls, notices, copied, confirms, api };
}

const settled = async () => { for (let i = 0; i < 6; i++) await new Promise(setImmediate); };
const click = async (el) => { el.listeners.get("click")(); await settled(); };
const buttons = (root, text) => find(root, "button").filter((el) => el.textContent === text);
const text = (root) => root.textContent;

// ---- the small things ---------------------------------------------------------------------------
test("a note's properties are not part of what is read", () => {
  assert.equal(bodyOf("---\ntype: person\ntags: [a]\n---\n# 张三\n正文"), "# 张三\n正文");
  assert.equal(bodyOf("没有属性\n---\n这条线不是属性"), "没有属性\n---\n这条线不是属性");
  assert.equal(bodyOf("---\nunclosed: yes\n正文"), "---\nunclosed: yes\n正文");
  assert.equal(bodyOf(""), "");
});

test("links between notes read as names, with the alias when there is one", () => {
  assert.equal(withLinksShown("见 [[张三]] 和 [[预算|预算表]]，还有 [[计划#第二节]] 与 ![[图]]。"), "见 「张三」 和 「预算表」，还有 「计划」 与 「图」。");
  assert.equal(withLinksShown("没有链接 [x](y)"), "没有链接 [x](y)");
  assert.equal(withLinksShown("没合上的 [[a 后面有 [[b]]"), "没合上的 [[a 后面有 「b」");
});

test("a note full of unclosed links is still read at once", () => {
  for (const text of ["[[a".repeat(40000), `[[a#${"#".repeat(60000)}`, `[[a|${"b".repeat(60000)}`, "![[a".repeat(30000)]) {
    const started = performance.now();
    assert.equal(withLinksShown(text), text);
    assert.ok(performance.now() - started < 1000);
  }
});

test("a new note's name becomes a path inside the knowledge base", () => {
  assert.equal(notePath("张三"), "wiki/张三.md");
  assert.equal(notePath("  张三.md "), "wiki/张三.md");
  assert.equal(notePath("wiki/项目/预算"), "wiki/项目/预算.md");
  assert.equal(notePath("/daily/今天"), "daily/今天.md");
  assert.equal(notePath("a\\b"), "a/b.md");
  assert.throws(() => notePath("  "), /名字/);
});

test("the folders on the way to a note get readable names", () => {
  assert.deepEqual(trailOf(""), []);
  assert.deepEqual(trailOf("raw/agents/Grok"), [
    { name: "外部资料", folder: "raw" }, { name: "agents", folder: "raw/agents" }, { name: "Grok", folder: "raw/agents/Grok" },
  ]);
  assert.equal(AREAS.wiki, "笔记");
});

test("the status line says what is there and what is waiting, in plain words", () => {
  assert.equal(statusLine({ enabled: false }), "知识库没有打开。");
  assert.match(statusLine({ enabled: true, notes: 0 }), /还是空的/);
  assert.equal(statusLine({ enabled: true, notes: 12, backlog: 0 }), "12 篇笔记");
  assert.equal(statusLine({ enabled: true, notes: 12, backlog: 3 }), "12 篇笔记 · 3 篇外部来的资料等着整理");
  assert.equal(issueCount({ issues: { orphan: 2, broken_link: 1, skipped: 1 } }), 3, "a note-count warning is not something to tidy");
  assert.equal(issueCount({}), 0);
});

test("labels never show internal names", () => {
  assert.equal(careLabel("uncompiled"), "外部来的，还没整理进笔记");
  assert.equal(careLabel("something_new"), "something_new");
  assert.equal(whoLabel("agent"), "助手"); assert.equal(whoLabel("nobody"), "系统");
  assert.equal(whatLabel("section"), "改了一节"); assert.equal(whatLabel("other"), "改了");
  assert.equal(kindLabel({ type: "thread", path: "threads/x.md" }), "任务线索");
  assert.equal(kindLabel({ type: "", path: "raw/agents/G/x.md" }), "外部资料");
  assert.equal(kindLabel({ type: "", path: "wiki/x.md" }), "");
});

// ---- the page --------------------------------------------------------------------------------------
test("opening the page shows the count, the folders and the notes", async () => {
  const { ui, get, calls } = setup();
  await ui.load();
  assert.equal(text(get("vault-status")), "4 篇笔记 · 1 篇外部来的资料等着整理");
  assert.deepEqual(calls.map((call) => call.path), ["/api/v3/vault", "/api/v3/vault/notes?"]);
  const rows = get("knowledge-list").children;
  assert.deepEqual(rows.map((row) => text(row)), ["笔记2", "外部资料1", "收件随手记的"]);
  assert.equal(get("knowledge-new").disabled, false);
  assert.equal(get("vault-care").hidden, false);
  assert.equal(text(get("vault-care-summary")), "整理建议（2）");
  assert.match(text(get("vault-howto-body")), /Obsidian 仓库/);
  assert.equal(find(get("vault-howto-body"), "code")[0].textContent, "/Users/me/vault");
});

test("a project's page says its notes live on the server and shows no folder", async () => {
  const { ui, get } = setup({ status: { ...STATUS, root: null } });
  await ui.load();
  assert.match(text(get("vault-howto-body")), /放在服务器上/);
  assert.equal(find(get("vault-howto-body"), "code").length, 0);
});

test("the path can be copied", async () => {
  const { ui, get, copied } = setup();
  await ui.load();
  await click(buttons(get("vault-howto-body"), "复制路径")[0]);
  assert.deepEqual(copied, ["/Users/me/vault"]);
  assert.equal(buttons(get("vault-howto-body"), "已复制").length, 1);
});

test("a knowledge base that is switched off says how to switch it on and offers nothing to write", async () => {
  const { ui, get, calls } = setup({ status: { enabled: false } });
  await ui.load();
  assert.equal(text(get("vault-status")), "知识库没有打开。");
  assert.match(text(get("knowledge-list")), /enabled/);
  assert.equal(get("knowledge-new").disabled, true);
  assert.deepEqual(calls.map((call) => call.path), ["/api/v3/vault"]);
});

test("an empty folder and an empty search each say so", async () => {
  const { ui, get } = setup({ listing: { folder: "", query: "", folders: [], notes: [] } });
  await ui.load();
  assert.match(text(get("knowledge-list")), /还没有笔记/);
  await ui.search("不存在");
  assert.match(text(get("knowledge-list")), /没有找到/);
});

test("going into a folder and back keeps the way home in view", async () => {
  const { ui, get, calls } = setup();
  await ui.load();
  await click(get("knowledge-list").children[1]);
  assert.equal(calls.at(-1).path, "/api/v3/vault/notes?folder=raw");
  const trail = get("vault-trail");
  assert.equal(buttons(trail, "全部").length, 1);
  assert.equal(find(trail, "span").some((el) => el.textContent === "外部资料" && el.className === "vault-here"), true);
  await click(buttons(trail, "全部")[0]);
  assert.equal(calls.at(-1).path, "/api/v3/vault/notes?");
});

test("searching looks everywhere, a #tag looks by tag, and clearing goes back", async () => {
  const { ui, get, calls } = setup();
  await ui.load();
  await ui.search("  预算 ");
  assert.equal(calls.at(-1).path, "/api/v3/vault/notes?q=%E9%A2%84%E7%AE%97");
  assert.equal(find(get("vault-trail"), "span")[0].textContent, "搜索「预算」");
  await ui.search("#同事");
  assert.equal(calls.at(-1).path, "/api/v3/vault/notes?tag=%E5%90%8C%E4%BA%8B");
  get("knowledge-search").value = "#同事";
  await click(buttons(get("vault-trail"), "清除")[0]);
  assert.equal(calls.at(-1).path, "/api/v3/vault/notes?");
  assert.equal(get("knowledge-search").value, "");
});

test("the search box searches when submitted", async () => {
  const { ui, get, calls } = setup();
  await ui.load();
  get("knowledge-search").value = "张三";
  get("knowledge-search-form").listeners.get("submit")({ preventDefault() {} });
  await settled();
  assert.equal(calls.at(-1).path, "/api/v3/vault/notes?q=%E5%BC%A0%E4%B8%89");
});

test("a list that has not changed is not redrawn", async () => {
  const { ui, get } = setup();
  await ui.load();
  const first = get("knowledge-list").children[0];
  await ui.load();
  assert.equal(get("knowledge-list").children[0], first, "the same rows, so a poll cannot move the page under the reader");
});

test("results that arrive after the space changed are dropped", async () => {
  let release;
  const slow = new Promise((resolve) => { release = resolve; });
  const { ui, get, context } = setup({ routes: { "GET /api/v3/vault": () => slow } });
  const pending = ui.load();
  context.space = "space-b";
  ui.clear();
  release(STATUS);
  await pending;
  assert.equal(text(get("vault-status")), "");
  assert.equal(get("knowledge-list").children.length, 0);
});

// ---- reading a note --------------------------------------------------------------------------------
test("a note opens with its title, where it is, what it is and what it links to", async () => {
  const { ui, get } = setup();
  await ui.load();
  await click(get("knowledge-list").children[2]);
  const pane = get("vault-note");
  assert.equal(pane.hidden, false);
  assert.equal(get("knowledge-empty-editor").hidden, true);
  assert.equal(text(find(pane, "h2")[0]), "张三");
  assert.equal(text(find(pane, "p")[0]), "wiki/张三.md");
  const html = toHtml(pane);
  assert.match(html, /负责预算，见 「预算表」 和 「还没写」。/);
  assert.doesNotMatch(html, /type: person/, "the properties block is not read out");
  assert.match(html, /#同事/);
  assert.equal(buttons(pane, "预算").length, 1);
  assert.equal(buttons(pane, "会议").length, 1);
  assert.equal(buttons(pane, "还没写").length, 1);
  const link = find(pane, "a")[0];
  assert.equal(link.getAttribute("href").startsWith("obsidian://open?path="), true);
  assert.equal(text(link), "在 Obsidian 打开");
});

test("what a note says is shown as text, never as page elements", async () => {
  const hostile = note({ title: "<script>alert(1)</script>", text: "# <img src=x onerror=alert(1)>\n<b>粗</b> [点我](javascript:alert(1))" });
  const { ui, get } = setup({ routes: { "GET /api/v3/vault/note": () => ({ note: hostile }) } });
  await ui.open("wiki/x.md");
  const html = toHtml(get("vault-note"));
  assert.doesNotMatch(html, /<script|<img|<b>|href="javascript/);
  assert.match(html, /&lt;script&gt;/);
});

test("following a link opens that note, and a missing one starts writing it", async () => {
  const { ui, get, calls } = setup();
  await ui.open("wiki/张三.md");
  await click(buttons(get("vault-note"), "预算")[0]);
  assert.equal(calls.at(-1).path, "/api/v3/vault/note?path=wiki%2F%E9%A2%84%E7%AE%97.md");
  await ui.open("wiki/张三.md");
  await click(buttons(get("vault-note"), "还没写")[0]);
  assert.equal(text(find(get("vault-note"), "h2")[0]), "新笔记");
  assert.equal(find(get("vault-note"), "input")[0].value, "还没写");
});

test("the note chosen in the list is marked as the one being read", async () => {
  const { ui, get } = setup({ routes: { "GET /api/v3/vault/note": () => ({ note: note({ path: "inbox.md", title: "收件" }) }) } });
  await ui.load();
  await click(get("knowledge-list").children[2]);
  const row = get("knowledge-list").children[2];
  assert.equal(row.className.includes("selected"), true);
  assert.equal(row.getAttribute("aria-current"), "true");
  await click(buttons(get("vault-note"), "关闭")[0]);
  assert.equal(get("vault-note").hidden, true);
  assert.equal(get("knowledge-empty-editor").hidden, false);
  assert.equal(row.className.includes("selected"), false);
});

test("a note the product writes by itself cannot be edited, and a viewer cannot edit any", async () => {
  const generated = setup({ routes: { "GET /api/v3/vault/note": () => ({ note: note({ generated: true }) }) } });
  await generated.ui.open("wiki/index.md");
  assert.equal(buttons(generated.get("vault-note"), "编辑").length, 0);
  assert.match(toHtml(generated.get("vault-note")), /自动生成/);
  const viewer = setup({ role: "viewer" });
  await viewer.ui.load();
  await viewer.ui.open("wiki/张三.md");
  assert.equal(buttons(viewer.get("vault-note"), "编辑").length, 0);
  assert.equal(viewer.get("knowledge-new").disabled, true);
  viewer.ui.startNew();
  assert.equal(find(viewer.get("vault-note"), "form").length, 0, "nothing to write into");
});

test("something an agent wrote is marked as coming from outside", async () => {
  const { ui, get } = setup({ listing: { folder: "raw", query: "", folders: [], notes: [{ path: "raw/agents/Grok/r.md", title: "调研", summary: "数据", type: "", outside: true }] } });
  await ui.load();
  const row = get("knowledge-list").children[0];
  assert.deepEqual(find(row, "span").filter((el) => el.className === "badge").map(text), ["外部资料", "外部来的"]);
});

test("the open note catches up with what changed on disk, and says when it is gone", async () => {
  let rev = "r1";
  let gone = false;
  const { ui, get, notices } = setup({ routes: { "GET /api/v3/vault/note": () => {
    if (gone) { const error = new Error("没有找到"); error.status = 404; throw error; }
    return { note: note({ rev, text: rev === "r1" ? "# 张三\n旧" : "# 张三\n新" }) };
  } } });
  await ui.open("wiki/张三.md");
  assert.match(toHtml(get("vault-note")), /旧/);
  rev = "r2";
  await ui.load();
  assert.match(toHtml(get("vault-note")), /新/);
  gone = true;
  await ui.load();
  assert.equal(get("vault-note").hidden, true);
  assert.deepEqual(notices.at(-1), ["这篇笔记已经不在了。", false]);
});

test("catching up does not steal the cursor or scroll the page", async () => {
  let rev = "r1";
  const { ui, get } = setup({ routes: { "GET /api/v3/vault/note": () => ({ note: note({ rev }) }) } });
  await ui.open("wiki/张三.md");
  const heading = find(get("vault-note"), "h2")[0];
  assert.equal(heading.focused, true, "opening it moves the reader to the title");
  rev = "r2";
  await ui.load();
  assert.equal(find(get("vault-note"), "h2")[0].focused, undefined, "a quiet refresh leaves focus alone");
});

// ---- writing ---------------------------------------------------------------------------------------
/** A server that keeps what was saved, so that reading again gives the saved note back. */
function keeping(saved, start = note()) {
  let current = start;
  return {
    "GET /api/v3/vault/note": () => ({ note: current }),
    "POST /api/v3/vault/note": () => { current = saved; return { note: saved, notice: "" }; },
  };
}

async function startEditing(options) {
  const world = setup(options);
  await world.ui.load();
  await world.ui.open("wiki/张三.md");
  await click(buttons(world.get("vault-note"), "编辑")[0]);
  const form = find(world.get("vault-note"), "form")[0];
  return { ...world, form, area: form.elements.namedItem("text") };
}

test("editing shows the whole file and saves it against the version that was read", async () => {
  const saved = note({ rev: "r2", text: "# 张三\n改了" });
  const { ui, get, form, area, calls, notices } = await startEditing({ routes: keeping(saved) });
  assert.match(area.value, /^---\ntype: person/);
  area.value = "# 张三\n改了";
  form.listeners.get("input")();
  assert.equal(form.dataset.dirty, "1");
  assert.equal(ui.hasDraft(), true);
  form.listeners.get("submit")({ preventDefault() {} });
  await settled();
  const sent = calls.find((call) => call.method === "POST");
  assert.deepEqual(sent.body, { path: "wiki/张三.md", text: "# 张三\n改了", mode: "replace", rev: "r1" });
  assert.deepEqual(notices.at(-1), ["保存好了。", false]);
  assert.match(toHtml(get("vault-note")), /改了/);
  assert.equal(find(get("vault-note"), "form").length, 0, "back to reading");
  assert.equal(ui.hasDraft(), false);
});

test("while it is being saved nothing else can take the place of the note", async () => {
  let release;
  const slow = new Promise((resolve) => { release = resolve; });
  const { ui, form, area, notices, confirms, get } = await startEditing({ routes: { "POST /api/v3/vault/note": () => slow } });
  area.value = "新的";
  form.listeners.get("input")();
  form.listeners.get("submit")({ preventDefault() {} });
  await settled();
  assert.equal(form.dataset.busy, "1");
  assert.equal(area.disabled, true);
  form.listeners.get("submit")({ preventDefault() {} });
  await ui.open("wiki/别的.md");
  buttons(get("vault-note"), "关闭").length && await click(buttons(get("vault-note"), "关闭")[0]);
  assert.equal(confirms.length, 0, "no question is asked while it is saving");
  assert.match(notices.at(-1)[0], /正在保存/);
  assert.equal(find(get("vault-note"), "form")[0], form, "the form is still there");
  release({ note: note({ rev: "r2", text: "新的" }), notice: "" });
  await settled();
  assert.equal(ui.hasDraft(), false);
});

test("what was typed is kept when leaving is declined, and dropped when it is allowed", async () => {
  const declined = await startEditing({ confirm: false });
  declined.area.value = "写到一半";
  declined.form.listeners.get("input")();
  await declined.ui.open("wiki/别的.md");
  assert.equal(declined.confirms.length, 1);
  assert.match(declined.confirms[0], /还没保存/);
  assert.equal(declined.area.value, "写到一半");
  assert.equal(find(declined.get("vault-note"), "form")[0], declined.form);
  const allowed = await startEditing({ confirm: true });
  allowed.area.value = "写到一半";
  allowed.form.listeners.get("input")();
  await allowed.ui.open("wiki/别的.md");
  assert.equal(find(allowed.get("vault-note"), "form").length, 0);
});

test("giving up an unchanged edit asks nothing and goes back to reading", async () => {
  const { form, get, confirms } = await startEditing();
  await click(buttons(form, "放弃")[0]);
  assert.equal(confirms.length, 0);
  assert.equal(text(find(get("vault-note"), "h2")[0]), "张三");
  assert.equal(find(get("vault-note"), "form").length, 0);
});

test("a note changed elsewhere in the meantime is not overwritten, and what was typed stays", async () => {
  const conflict = Object.assign(new Error("changed"), { status: 409 });
  const { form, area, ui, get, notices, calls } = await startEditing({ routes: { "POST /api/v3/vault/note": () => { throw conflict; } } });
  area.value = "我写的";
  form.listeners.get("input")();
  form.listeners.get("submit")({ preventDefault() {} });
  await settled();
  assert.equal(area.value, "我写的");
  assert.equal(ui.hasDraft(), true);
  assert.match(notices.at(-1)[0], /别处被改过/);
  assert.equal(notices.at(-1)[1], true);
  const fresh = buttons(form, "载入最新")[0];
  assert.equal(fresh.hidden, false);
  assert.equal(find(form, "p")[0].hidden, false);
  assert.equal(area.disabled, false, "the form is usable again");
  calls.length = 0;
  await click(fresh);
  assert.equal(calls[0].path.startsWith("/api/v3/vault/note?"), true);
  assert.equal(find(get("vault-note"), "form").length, 0);
});

test("loading the latest asks first, because it drops what was typed", async () => {
  const conflict = Object.assign(new Error("changed"), { status: 409 });
  const { form, area, confirms, calls } = await startEditing({ confirm: false, routes: { "POST /api/v3/vault/note": () => { throw conflict; } } });
  area.value = "我写的";
  form.listeners.get("input")();
  form.listeners.get("submit")({ preventDefault() {} });
  await settled();
  calls.length = 0;
  await click(buttons(form, "载入最新")[0]);
  assert.equal(confirms.length, 1);
  assert.equal(calls.length, 0);
  assert.equal(area.value, "我写的");
});

test("any other failure is reported and leaves the text where it was", async () => {
  const { form, area, notices } = await startEditing({ routes: { "POST /api/v3/vault/note": () => { throw new Error("太长了"); } } });
  area.value = "内容";
  form.listeners.get("input")();
  form.listeners.get("submit")({ preventDefault() {} });
  await settled();
  assert.deepEqual(notices.at(-1), ["失败：太长了", true]);
  assert.equal(area.value, "内容");
  assert.equal(form.dataset.busy, undefined);
  assert.equal(area.disabled, false);
});

test("a new note is made from a name and some text, and the page shows it afterwards", async () => {
  const made = note({ path: "wiki/李四.md", title: "李四", text: "# 李四\n新同事", rev: "n1" });
  const { ui, get, calls, notices } = setup({ routes: keeping(made, made) });
  await ui.load();
  await click(get("knowledge-new"));
  const form = find(get("vault-note"), "form")[0];
  assert.equal(text(find(get("vault-note"), "h2")[0]), "新笔记");
  form.elements.namedItem("name").value = "李四";
  form.elements.namedItem("text").value = "# 李四\n新同事";
  form.listeners.get("input")();
  form.listeners.get("submit")({ preventDefault() {} });
  await settled();
  const sent = calls.find((call) => call.method === "POST");
  assert.deepEqual(sent.body, { path: "wiki/李四.md", text: "# 李四\n新同事", mode: "create" });
  assert.deepEqual(notices.at(-1), ["保存好了。", false]);
  assert.equal(text(find(get("vault-note"), "h2")[0]), "李四");
  assert.equal(calls.filter((call) => call.path === "/api/v3/vault/notes?").length, 2, "the list is read again");
});

test("a new note without a name never leaves the page", async () => {
  const { ui, get, calls, notices } = setup();
  await ui.load();
  await click(get("knowledge-new"));
  const form = find(get("vault-note"), "form")[0];
  form.elements.namedItem("name").value = "  ";
  form.elements.namedItem("text").value = "没有名字";
  form.listeners.get("submit")({ preventDefault() {} });
  await settled();
  assert.equal(calls.some((call) => call.method === "POST"), false);
  assert.equal(notices.at(-1)[1], true);
  assert.match(notices.at(-1)[0], /名字/);
  assert.equal(form.dataset.busy, undefined);
});

test("a name that is taken is reported as it is", async () => {
  const { ui, get, notices } = setup({ routes: { "POST /api/v3/vault/note": () => { throw new Error("wiki/张三.md 已经有了。"); } } });
  await ui.load();
  await click(get("knowledge-new"));
  const form = find(get("vault-note"), "form")[0];
  form.elements.namedItem("name").value = "张三";
  form.elements.namedItem("text").value = "x";
  form.listeners.get("submit")({ preventDefault() {} });
  await settled();
  assert.deepEqual(notices.at(-1), ["失败：wiki/张三.md 已经有了。", true]);
});

// ---- looking back ----------------------------------------------------------------------------------
const versions = [
  { id: 9, action: "replace", actor: "agent", created_at: 1700000100, restorable: true, text: "# 张三\n旧版", truncated: false },
  { id: 7, action: "create", actor: "user", created_at: 1700000000, restorable: false, text: "", truncated: false },
];

test("the history lists every change, who made it, and can put one back", async () => {
  const { ui, get, calls, notices } = setup({ routes: { "GET /api/v3/vault/history": () => ({ path: "wiki/张三.md", versions }), "POST /api/v3/vault/restore": () => ({ path: "wiki/张三.md" }) } });
  await ui.open("wiki/张三.md");
  await click(buttons(get("vault-note"), "历史")[0]);
  const dialog = get("knowledge-history-dialog");
  assert.equal(dialog.open, true);
  assert.equal(text(get("knowledge-history-title")), "「张三」的历史");
  const rows = find(get("knowledge-history-list"), "details");
  assert.equal(rows.length, 2);
  assert.match(text(find(rows[0], "summary")[0]), /助手改写/);
  assert.match(text(find(rows[1], "summary")[0]), /你新建/);
  assert.match(text(find(rows[1], "pre")[0]), /新建的，之前没有内容/);
  assert.equal(buttons(rows[1], "恢复到改之前").length, 0, "a new note has no before to go back to");
  calls.length = 0;
  await click(buttons(rows[0], "恢复到改之前")[0]);
  assert.deepEqual(calls[0], { path: "/api/v3/vault/restore", method: "POST", body: { version_id: 9 } });
  assert.equal(dialog.open, false);
  assert.deepEqual(notices.at(-1), ["已经恢复。", false]);
  assert.equal(calls.some((call) => call.path.startsWith("/api/v3/vault/note?")), true, "the note is read again");
});

test("a viewer can read the history but not put anything back", async () => {
  const { ui, get } = setup({ role: "viewer", routes: { "GET /api/v3/vault/history": () => ({ path: "wiki/张三.md", versions }) } });
  await ui.open("wiki/张三.md");
  await click(buttons(get("vault-note"), "历史")[0]);
  assert.equal(buttons(get("knowledge-history-list"), "恢复到改之前").length, 0);
  assert.equal(find(get("knowledge-history-list"), "details").length, 2);
});

test("a note with no history yet says so", async () => {
  const { ui, get } = setup({ routes: { "GET /api/v3/vault/history": () => ({ path: "wiki/张三.md", versions: [] }) } });
  await ui.open("wiki/张三.md");
  await click(buttons(get("vault-note"), "历史")[0]);
  assert.match(text(get("knowledge-history-list")), /还没有改动记录/);
});

test("closing the history empties it, and so does a change of space", async () => {
  const { ui, get } = setup({ routes: { "GET /api/v3/vault/history": () => ({ path: "p", versions }) } });
  await ui.open("wiki/张三.md");
  await click(buttons(get("vault-note"), "历史")[0]);
  get("knowledge-history-close").listeners.get("click")();
  assert.equal(get("knowledge-history-dialog").open, false);
  assert.equal(get("knowledge-history-list").children.length, 0);
  await click(buttons(get("vault-note"), "历史")[0]);
  ui.clear();
  assert.equal(get("knowledge-history-dialog").open, false);
  assert.equal(get("knowledge-history-list").children.length, 0);
});

// ---- tidying ---------------------------------------------------------------------------------------
test("the tidying list is read only when it is opened, and each line goes to its note", async () => {
  const { ui, get, calls } = setup();
  await ui.load();
  assert.equal(calls.some((call) => call.path === "/api/v3/vault/lint"), false, "not read until it is asked for");
  const care = get("vault-care");
  care.open = true;
  care.listeners.get("toggle")();
  await settled();
  const rows = get("vault-care-list").children;
  assert.equal(rows.length, 2);
  assert.match(text(rows[0]), /raw\/agents\/Grok\/r\.md/);
  assert.match(text(rows[0]), /外部来的，还没整理进笔记/);
  assert.doesNotMatch(text(rows[0]), /还没有整理进 wiki/, "a reason that says the same thing is not repeated");
  assert.match(text(rows[1]), /链到了还不存在的笔记：\[\[还没写\]\] 找不到对应的笔记/);
  calls.length = 0;
  await click(rows[0]);
  assert.equal(calls[0].path, "/api/v3/vault/note?path=raw%2Fagents%2FGrok%2Fr.md");
});

test("with nothing to tidy the section is not there", async () => {
  const { ui, get } = setup({ status: { ...STATUS, issues: {}, backlog: 0 } });
  await ui.load();
  assert.equal(get("vault-care").hidden, true);
});

test("a change of space leaves nothing of the old one on the page", async () => {
  const { ui, get } = setup();
  await ui.load();
  await ui.open("wiki/张三.md");
  ui.clear();
  assert.equal(get("knowledge-list").children.length, 0);
  assert.equal(get("vault-note").hidden, true);
  assert.equal(get("knowledge-empty-editor").hidden, false);
  assert.equal(text(get("vault-status")), "");
  assert.equal(get("knowledge-search").value, "");
  assert.equal(ui.hasDraft(), false);
});
