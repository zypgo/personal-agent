import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { FakeElement, elements, find, toHtml } from "./fake_dom.mjs";
import { RequestScope, buildTaskRequest, buildStepRequest, buildAnswerRequest } from "../../src/personal_agent/channels/web/static/query.js";

// Execute the real renderer and form handlers without browser startup/network or a new dependency.
// The same fake DOM used by the Markdown tests rejects turning untrusted text into HTML.
class Element extends FakeElement {
  constructor(tag) {
    super(tag);
    this.dataset = {};
    this.disabled = false;
    this.value = "";
    this.classList = {
      add: value => this.className = `${this.className} ${value}`.trim(),
      toggle: (value, enabled) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        if (enabled) names.add(value); else names.delete(value);
        this.className = [...names].join(" ");
      },
    };
  }
  append(...kids) { super.append(...kids); for (const child of this.children) child.parentElement = this; }
  replaceChildren(...kids) {
    for (const child of this.children) child.parentElement = null;
    this.children = []; this.append(...kids);
  }
  contains(value) { return this === value || elements(this).includes(value); }
  closest(tag) {
    for (let item = this; item; item = item.parentElement) if (item.tagName === tag.toUpperCase()) return item;
    return null;
  }
  querySelectorAll(selector) {
    if (selector === "button") return find(this, "button");
    if (selector.includes("data-dirty")) return find(this, "form").filter(f => f.dataset.dirty === "1" || f.dataset.busy === "1");
    return [];
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  get childElementCount() { return this.children.filter(child => child.nodeType === 1).length; }
  get isConnected() { return Boolean(this.root || this.parentElement?.isConnected); }
  get elements() {
    const controls = elements(this).filter(item => ["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(item.tagName));
    controls.namedItem = name => controls.find(item => item.name === name) || null;
    return controls;
  }
  reset() { for (const item of this.elements) item.value = ""; }
}

const source = readFileSync(new URL("../../src/personal_agent/channels/web/static/workbench.js", import.meta.url), "utf8");
// The VM has a deliberately tiny DOM. Inject the actual bundled request adapter; only its module
// import is removed, so these tests exercise the same networking implementation as the browser.
const requestImport = 'import { RequestScope, buildTaskRequest, buildStepRequest, buildAnswerRequest } from "./query.js";\n';
assert.ok(source.startsWith(requestImport));
const definitions = source.slice(0, source.indexOf('bindForm("enroll-form"')).replace(requestImport, "");
const taskFixture = () => ({
  id: "task-a", actor_id: "alice", title: "Report", state: "awaiting_approval", revision: 2,
  jobs: [{id: "job-a", task_id: "task-a", task_revision: 2, step_id: "1", state: "awaiting_approval",
    approver_id: "owner", plan_hash: "hash-a", current_attempt: null,
    plan: {command: "printf hello", timeout_s: 60, network: "none", input_manifest: [], output_paths: []}}],
  questions: [],
});

function setup(task = taskFixture(), {principal = "alice", role = "member", respond} = {}) {
  const ids = new Map();
  const get = id => {
    if (!ids.has(id)) { const item = new Element("div"); item.root = true; ids.set(id, item); }
    return ids.get(id);
  };
  const document = {createElement: tag => new Element(tag), getElementById: get, activeElement: null};
  const calls = [];
  const context = {document, URL, URLSearchParams, AbortController, DOMException, RequestScope, buildTaskRequest, buildStepRequest, buildAnswerRequest, crypto: webcrypto,
    location: {href: "https://example.invalid/workspace", origin: "https://example.invalid", hash: ""},
    history: {replaceState() {}}, fetch: async (url, options) => {
      const path = new URL(url, "https://example.invalid").pathname;
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({path, body});
      const override = respond?.(new URL(url, "https://example.invalid"), options);
      if (override !== undefined) return await override;
      if (path.endsWith("/answer")) {
        task.questions[0].state = "answered"; task.questions[0].answer = body.answer;
      }
      const data = path.endsWith("/logs") ? {logs: [{sequence: 1, text: "first"}, {sequence: 2, text: "first second"}], snapshots: true}
        : path === "/api/v3/tasks" ? {tasks: [task]} : path.endsWith("/task-a") ? {task} : {};
      return {ok: true, status: 200, json: async () => data};
    },
  };
  runInNewContext(definitions + "\nglobalThis.ui={state,renderTask,loadTasks,editingTask,showLogin};", context);
  context.ui.state.me = {principal: {id: principal, is_admin: false}, role, spaces: []};
  context.ui.state.space = "space-a"; context.ui.state.taskId = task.id;
  context.ui.renderTask(task);
  return {ui: context.ui, detail: get("task-detail"), get, calls, task};
}

async function settled() { for (let index = 0; index < 5; index++) await new Promise(setImmediate); }

test("task answers and untrusted question text remain scoped and bound to the displayed revision", async () => {
  const task = taskFixture(); task.state = "waiting_input";
  task.questions = [{id: "question-a", task_revision: 2, state: "open", question: '<img src=x onerror="attack()">', choices: ["text", "csv"]}];
  const {detail, calls} = setup(task);
  assert.equal(find(detail, "img").length, 0);
  assert.match(toHtml(detail), /&lt;img/);
  const form = find(detail, "form").find(item => item.elements.namedItem("answer"));
  form.elements.namedItem("answer").value = "csv";
  form.listeners.get("submit")({preventDefault() {}});
  await settled();
  const sent = calls.find(call => call.path.endsWith("/answer"));
  assert.deepEqual(sent.body, {expected_revision: 2, answer: "csv"});
  assert.equal(task.questions[0].state, "answered");
});

test("viewers cannot answer, cancel, revise, append steps or approve in task detail", () => {
  const task = taskFixture();
  task.questions = [{id: "question-a", task_revision: 2, state: "open", question: "Format?", choices: ["text"]}];
  const {detail} = setup(task, {principal: "bob", role: "viewer"});
  assert.equal(find(detail, "form").length, 0);
  assert.equal(find(detail, "button").length, 0);
  assert.match(detail.textContent, /等待有编辑权限的成员回答/);
});

test("periodic task refresh retains a dirty task form until an explicit discard or save", async () => {
  const {ui, detail} = setup();
  const editor = find(detail, "form").find(item => item.elements.namedItem("command"));
  editor.elements.namedItem("command").value = "draft command";
  editor.listeners.get("input")();
  assert.equal(ui.editingTask(), true);
  await ui.loadTasks();
  assert.ok(find(detail, "form").includes(editor));
  assert.equal(editor.elements.namedItem("command").value, "draft command");
});

test("logs replace earlier cumulative snapshots instead of repeating prefixes", async () => {
  const task = taskFixture(); task.jobs[0].current_attempt = "attempt-a";
  const {detail} = setup(task);
  const logs = find(detail, "details").find(item => item.className === "task-logs");
  logs.open = true; logs.listeners.get("toggle")();
  await settled();
  assert.equal(find(logs, "pre")[0].textContent, "first second");
});

test("step submission preserves explicit inputs and revision instead of interpolating answers", async () => {
  const {detail, calls} = setup();
  const editor = find(detail, "form").find(item => item.elements.namedItem("command"));
  editor.elements.namedItem("command").value = "printf explicit";
  editor.elements.namedItem("input_paths").value = "input.txt\n data.csv\n";
  editor.elements.namedItem("output_paths").value = "report.txt";
  editor.elements.namedItem("timeout_s").value = "60";
  editor.listeners.get("submit")({preventDefault() {}});
  await settled();
  const sent = calls.find(call => call.path.endsWith("/steps"));
  assert.deepEqual(sent.body, {expected_revision: 2, step_id: "2", command: "printf explicit", input_paths: ["input.txt", "data.csv"], output_paths: ["report.txt"], timeout_s: 60});
});

test("a revoked project is cleared when a parallel request is cancelled before the forbidden response propagates", async () => {
  let releaseOld;
  const oldDiagnostics = new Promise(resolve => { releaseOld = resolve; });
  const {ui, detail, get} = setup(taskFixture(), { respond: url => {
    if (url.searchParams.get("space") === "space-a") {
      if (url.pathname.endsWith("/tasks")) return { ok: false, status: 403, json: async () => ({error: "Membership revoked"}) };
      if (url.pathname.endsWith("/diagnostics")) return oldDiagnostics;
    }
    if (url.pathname.endsWith("/me")) return { ok: true, status: 200, json: async () => ({
      principal: {id: "alice", is_admin: false, display_name: "Alice"}, role: "owner", space_id: "personal-a",
      spaces: [{id: "personal-a", kind: "personal", name: "Personal"}],
    }) };
    if (url.pathname.endsWith("/tasks")) return { ok: true, status: 200, json: async () => ({ tasks: [] }) };
  } });
  assert.match(detail.textContent, /Report/);
  await assert.rejects(ui.loadTasks());
  await settled();
  assert.equal(detail.textContent, "");
  assert.equal(detail.hidden, true);
  assert.equal(ui.state.space, "personal-a");
  assert.equal(ui.state.taskId, null);
  releaseOld({ ok: true, status: 200, json: async () => ({ budget: {space_spent_usd: 999} }) });
  await settled();
  assert.doesNotMatch(get("task-diagnostics").textContent, /999/);
});
