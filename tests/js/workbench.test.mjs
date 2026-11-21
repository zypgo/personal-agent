import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { FakeElement, elements, find, toHtml } from "./fake_dom.mjs";
import { RequestScope, buildTaskRequest, buildStepRequest, buildAnswerRequest } from "../../src/personal_agent/channels/web/static/query.js";
import * as vaultModule from "../../src/personal_agent/channels/web/static/vault.js";

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
  removeAttribute(name) { this.attrs.delete(name); }
  focus() {}
  // A dialog opens, and closing it tells the page afterwards, as a browser does.
  showModal() { this.open = true; }
  close() { this.open = false; Promise.resolve().then(() => this.listeners.get("close")?.()); }
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
// The knowledge page is loaded the first time it is opened; here the same module is handed over directly.
const definitions = source.slice(0, source.indexOf('bindForm("enroll-form"')).replace(requestImport, "").replace('import { createProductShell } from "./product-shell.js";\n', "").replace('import("./vault.js")', "Promise.resolve(vaultModule)");
assert.ok(definitions.includes("Promise.resolve(vaultModule)"));
const taskFixture = () => ({
  id: "task-a", actor_id: "alice", title: "Report", state: "awaiting_approval", revision: 2,
  jobs: [{id: "job-a", task_id: "task-a", task_revision: 2, step_id: "1", state: "awaiting_approval",
    approver_id: "owner", plan_hash: "hash-a", current_attempt: null,
    plan: {command: "printf hello", timeout_s: 60, network: "none", input_manifest: [], output_paths: []}}],
  questions: [],
});

function setup(task = taskFixture(), {principal = "alice", role = "member", respond, href="https://example.invalid/workspace?space=space-a", confirm=()=>true, globals = {}, documentExtras = {}} = {}) {
  const ids = new Map();
  const get = id => {
    if (!ids.has(id)) { const item = new Element("div"); item.root = true; ids.set(id, item); }
    return ids.get(id);
  };
  const document = {createElement: tag => new Element(tag), getElementById: get, activeElement: null, ...documentExtras};
  const calls = [];
  const assigned = [];
  const location = {assign: path => { assigned.push(path); }};
  const setLocation = value => {const url=new URL(value,"https://example.invalid");Object.assign(location,{href:url.href,origin:url.origin,pathname:url.pathname,search:url.search,hash:url.hash});};
  setLocation(href);
  const historyCalls=[];
  const context = {document, URL, URLSearchParams, AbortController, DOMException, RequestScope, buildTaskRequest, buildStepRequest, buildAnswerRequest, vaultModule, crypto: webcrypto,
    location, history: {replaceState(_a,_b,path) {historyCalls.push(["replace",path]);setLocation(path);},pushState(_a,_b,path) {historyCalls.push(["push",path]);setLocation(path);}}, confirm, fetch: async (url, options) => {
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
    ...globals,
  };
  runInNewContext(definitions + "\nglobalThis.ui={state,renderTask,loadTasks,loadMembers,editingTask,showLogin,routeFromLocation,workspaceUrl,selectTab,selectTaskView,followLocation,showPanel,loadMe,vaultUI,bindButton,notice,renderInvite,loadInvite,readable,reachable,inviteLink,showSecret,finishSecret,closeSecret};", context);
  context.ui.state.me = {principal: {id: principal, is_admin: false}, role, spaces: []};
  context.ui.state.space = "space-a"; context.ui.state.taskId = task.id; context.ui.state.taskView="computer";
  context.ui.renderTask(task);
  return {ui: context.ui, detail: get("task-detail"), get, calls, task, context, location, historyCalls, setLocation, assigned};
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
  assert.deepEqual(find(detail, "button").map(button=>button.textContent), ["返回列表"]);
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

test("sign-out removes private diagnostics and notices before another account can reuse the workspace", async () => {
  const {ui, get} = setup(taskFixture(), { respond: url => {
    if (url.pathname === "/api/v3/diagnostics") return {ok: true, status: 200, json: async () => ({
      budget: {space_spent_usd: 731}, nodes: [{online: true}], tasks: {queued: 4},
    })};
  } });
  await ui.loadTasks();
  ui.notice("Saved private-project-report.md", true);
  for (const id of ["task-diagnostics", "device-diagnostics"]) assert.match(get(id).textContent, /731/);
  ui.showLogin();
  for (const id of ["task-diagnostics", "device-diagnostics", "notice"]) assert.equal(get(id).textContent, "");
  assert.equal(get("notice").hidden, true);
});

test("leaving a project switches to an accessible space before requesting any more private data", async () => {
  let left = false;
  const rejectedSpaceReads = [];
  const {ui, get, calls} = setup(taskFixture(), {principal: "alice", role: "owner", respond: (url, options) => {
    const space = url.searchParams.get("space");
    if (url.pathname === "/api/v3/members" && options.method === "POST") {
      left = true;
      return {ok: true, status: 200, json: async () => ({members: []})};
    }
    if (left && space === "space-a") {
      rejectedSpaceReads.push(url.pathname);
      return {ok: false, status: 403, json: async () => ({error: "No longer a member"})};
    }
    if (url.pathname === "/api/v3/members") return {ok: true, status: 200, json: async () => ({
      members: [{principal_id: "alice", display_name: "Alice", role: "owner"}],
    })};
    if (url.pathname === "/api/v3/me") return {ok: true, status: 200, json: async () => ({
      principal: {id: "alice", display_name: "Alice", is_admin: false}, role: "owner", space_id: "personal-a",
      spaces: [{id: "personal-a", name: "Personal", kind: "personal"}],
    })};
  }});
  ui.state.me.spaces = [{id: "space-a", name: "Project", kind: "project"}];
  ui.state.tab = "spaces";
  await ui.loadMembers();
  find(get("members-list"), "button")[0].listeners.get("click")();
  await settled();
  assert.equal(left, true);
  assert.equal(ui.state.space, "personal-a");
  assert.equal(ui.state.taskId, null);
  assert.deepEqual(rejectedSpaceReads, []);
  assert.match(get("notice").textContent, /已离开该项目/);
  assert.equal(calls.filter(call => call.body?.revoke).length, 1);
});


test("drawer destinations survive reload and invalid URL values fall back to real panels", () => {
  const {ui}=setup();
  const route=ui.routeFromLocation("https://example.invalid/workspace?tab=knowledge&space=team-a");
  assert.equal(route.tab,"knowledge");assert.equal(route.space,"team-a");
  assert.equal(ui.routeFromLocation("https://example.invalid/workspace?tab=__proto__").tab,"tasks");
  assert.equal(ui.routeFromLocation("https://example.invalid/workspace?tab=tasks&view=computer").taskView,"computer");
  ui.state.tab="agents";
  assert.equal(new URL(ui.workspaceUrl(),"https://example.invalid").searchParams.get("space"),"space-a");
  assert.equal(new URL(ui.workspaceUrl(),"https://example.invalid").searchParams.get("tab"),"agents");
});

test("panel changes and browser history preserve unsent forms in the same space", async () => {
  const {ui,get,setLocation,location,historyCalls}=setup();
  ui.state.me=null; // This exercises navigation independently of the data loaders.
  const draft=get("task-form");draft.dataset.dirty="1";
  const content=new Element("textarea");content.name="text";content.value="unsent private note";draft.append(content);
  await ui.selectTab("knowledge");
  assert.equal(get("panel-knowledge").hidden,false);assert.equal(get("panel-tasks").hidden,true);
  assert.equal(new URL(location.href).searchParams.get("tab"),"knowledge");
  await ui.selectTab("devices");
  setLocation("https://example.invalid/workspace?space=space-a&tab=knowledge");
  await ui.followLocation();
  assert.equal(ui.state.tab,"knowledge");assert.equal(get("panel-knowledge").hidden,false);
  assert.equal(content.value,"unsent private note");assert.equal(draft.dataset.dirty,"1");
  assert.ok(historyCalls.some(([method,path])=>method==="push" && path.includes("tab=devices")));
});

test("membership edits send the strict contract without legacy action fields", async () => {
  const {ui,get,calls}=setup(taskFixture(),{role:"owner",respond:(url,options)=>{
    if(url.pathname==="/api/v3/members") {
      if(options.method==="POST") {
        const body=JSON.parse(options.body);
        assert.deepEqual(Object.keys(body).sort(),["principal_id","role"]);
      }
      return {ok:true,status:200,json:async()=>({members:[{principal_id:"bob",display_name:"Bob",role:"member"}]})};
    }
    if(url.pathname==="/api/v3/me") return {ok:true,status:200,json:async()=>({principal:{id:"alice",display_name:"Alice",is_admin:false},role:"owner",space_id:"space-a",spaces:[{id:"space-a",name:"Project",kind:"project"}]})};
  }});
  ui.state.me.spaces=[{id:"space-a",name:"Project",kind:"project"}];
  await ui.loadMembers();
  const select=find(get("members-list"),"select")[0];select.value="viewer";select.listeners.get("change")();
  await settled();
  assert.deepEqual(calls.find(call=>call.body?.principal_id==="bob").body,{principal_id:"bob",role:"viewer"});
});

test("accepting an invitation pins the existing session and concurrent clicks make one request", async () => {
  let release;
  const result=new Promise(resolve=>{release=resolve;});
  const {ui,get,calls,context}=setup(taskFixture(),{respond:url=>url.pathname==="/api/v3/enroll" ? result : undefined});
  ui.state.pendingInvite="private-one-time-code";
  const start=source.indexOf('bindButton("accept-invite"');
  const end=source.indexOf('bindForm("space-form"',start);
  runInNewContext(source.slice(start,end),context);
  const button=get("accept-invite");button.listeners.get("click")();button.listeners.get("click")();
  await settled();
  const mutations=calls.filter(call=>call.path==="/api/v3/enroll");
  assert.equal(mutations.length,1);assert.equal(mutations[0].body.require_existing_session,true);
  release({ok:false,status:401,json:async()=>({error:"Session revoked"})});await settled();
  assert.equal(ui.state.me,null);assert.equal(calls.filter(call=>call.path==="/api/v3/enroll").length,1);
});

test("reselecting the current computer task preserves its unfinished editor", async () => {
  const {ui,detail,get,calls}=setup();
  const editor=find(detail,"form").find(item=>item.elements.namedItem("command"));
  editor.elements.namedItem("command").value="keep this unfinished command";
  editor.listeners.get("input")();
  await ui.loadTasks();
  const reads=calls.filter(call=>call.path.endsWith("/task-a")).length;
  const selected=find(get("tasks-list"),"button").find(item=>item.dataset.taskId==="task-a");
  selected.listeners.get("click")();await settled();
  assert.ok(find(detail,"form").includes(editor));
  assert.equal(editor.elements.namedItem("command").value,"keep this unfinished command");
  assert.equal(editor.dataset.dirty,"1");
  assert.equal(calls.filter(call=>call.path.endsWith("/task-a")).length,reads);
});

test("opening the knowledge page loads it once and reads the knowledge base", async () => {
  const {ui,get,calls}=setup(taskFixture(),{respond:url=>{
    if(url.pathname==="/api/v3/vault") return {ok:true,status:200,json:async()=>({enabled:true,exists:true,root:null,notes:1,areas:{},backlog:0,issues:{}})};
    if(url.pathname==="/api/v3/vault/notes") return {ok:true,status:200,json:async()=>({folder:"",query:"",folders:[],notes:[{path:"a.md",title:"甲",summary:"",outside:false}]})};
  }});
  await ui.selectTab("knowledge");
  assert.deepEqual(calls.filter(call=>call.path.startsWith("/api/v3/vault")).map(call=>call.path),["/api/v3/vault","/api/v3/vault/notes"]);
  assert.equal(get("vault-status").textContent,"1 篇笔记");
  assert.equal(find(get("knowledge-list"),"button")[0].textContent,"甲");
  await ui.selectTab("knowledge");
  assert.equal(await ui.vaultUI(),await ui.vaultUI(),"one page object, not one per visit");
});

test("a failed first load of the knowledge page can be tried again", async () => {
  let fail=true;
  const {ui,get,calls}=setup(taskFixture(),{respond:url=>{
    if(url.pathname==="/api/v3/vault") return fail ? {ok:false,status:500,json:async()=>({error:"断了"})} : {ok:true,status:200,json:async()=>({enabled:false})};
  }});
  await assert.rejects(()=>ui.selectTab("knowledge"));
  fail=false;
  await ui.selectTab("knowledge");
  assert.equal(get("vault-status").textContent,"知识库没有打开。");
  assert.equal(calls.filter(call=>call.path==="/api/v3/vault").length,2);
});

test("changing page cross-fades only where the browser can, and only when motion is welcome", () => {
  const updates = [];
  const startViewTransition = update => {
    updates.push(update);
    // A transition that is skipped rejects `ready`; that must never surface as an unhandled rejection.
    return {ready: Promise.reject(new Error("skipped")), finished: Promise.resolve(), updateCallbackDone: Promise.resolve()};
  };
  const welcome = setup(taskFixture(), {documentExtras: {startViewTransition}, globals: {matchMedia: () => ({matches: false})}});
  welcome.ui.state.tab = "tasks"; welcome.ui.showPanel();
  assert.equal(updates.length, 0, "the first page appears at once; there is nothing to fade from");
  assert.equal(welcome.get("panel-tasks").hidden, false);
  welcome.ui.state.tab = "tasks"; welcome.ui.showPanel();
  assert.equal(updates.length, 0, "showing the same page again is not a change");
  welcome.ui.state.tab = "knowledge"; welcome.ui.showPanel();
  assert.equal(updates.length, 1);
  assert.deepEqual([welcome.get("panel-tasks").hidden, welcome.get("panel-knowledge").hidden], [false, true], "the swap happens inside the transition, not before it");
  updates[0]();
  assert.equal(welcome.get("panel-knowledge").hidden, false); assert.equal(welcome.get("panel-tasks").hidden, true);
  assert.equal(welcome.context.document.title, "知识库 · 栖序"); assert.equal(welcome.get("masthead-title").textContent, "知识库");

  const still = setup(taskFixture(), {documentExtras: {startViewTransition}, globals: {matchMedia: query => ({matches: query.includes("reduce")})}});
  still.ui.state.tab = "tasks"; still.ui.showPanel();
  still.ui.state.tab = "agents"; still.ui.showPanel();
  assert.equal(updates.length, 1, "reduced motion never starts a transition");
  assert.equal(still.get("panel-agents").hidden, false, "and the swap is immediate");

  const plain = setup();
  plain.ui.state.tab = "tasks"; plain.ui.showPanel(); plain.ui.state.tab = "devices"; plain.ui.showPanel();
  assert.equal(plain.get("panel-devices").hidden, false, "a browser without transitions switches at once");
});

test("a notice that reports good news leaves by itself and one that reports a problem stays", () => {
  const timers = [], cleared = [];
  const globals = {setTimeout: (callback, delay) => {timers.push({callback, delay});return timers.length;}, clearTimeout: id => cleared.push(id)};
  const {ui, get} = setup(taskFixture(), {globals});
  ui.notice("保存好了。");
  assert.equal(get("notice").textContent, "保存好了。"); assert.equal(get("notice").hidden, false);
  assert.equal(timers.length, 1); assert.equal(timers[0].delay, 6000);
  timers[0].callback();
  assert.equal(get("notice").hidden, true, "good news goes after a moment");
  ui.notice("内容或权限已变化。", true);
  assert.equal(get("notice").className, "notice error"); assert.equal(timers.length, 1, "an error sets no timer");
  timers.length = 0; cleared.length = 0;
  ui.notice("第一条"); ui.notice("第二条");
  assert.deepEqual(cleared.length, 2, "a newer notice cancels the older one's timer so it cannot hide the new text early");
  assert.equal(get("notice").textContent, "第二条");
});

// ---- inviting people into a project, and joining one ----------------------------------------------

const answer = (data, status = 200) => ({ok: status < 400, status, json: async () => data});
const PROJECT = {id: "space-p", name: "产品研究", kind: "project"};
const joinedAs = (role, extra = {}) => answer({ok: true, recovery_code: null, created: false, joined: true, space: PROJECT, role, ...extra});
const preview = (extra = {}) => answer({space_name: "产品研究", role: "member", invited_by: "小明", expires_at: 4102444800, already_member: false, ...extra});
const dead = () => answer({error: "Invitation is unavailable."}, 400);

/** Run the part of the page that wires up the forms and buttons between two markers. */
function wire(context, from, to) {
  const start = source.indexOf(from), end = source.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `${from} … ${to}`);
  runInNewContext(source.slice(start, end), context);
}

function fill(form, values) {
  for (const [name, value] of Object.entries(values)) {
    const control = new Element("input"); control.name = name; control.value = value; form.append(control);
  }
  return form;
}

test("an invitation link tells the sign-in page which project it is for and who sent it", async () => {
  const {ui, get, calls} = setup(taskFixture(), {respond: url => url.pathname === "/api/v3/invites/preview" ? preview() : undefined});
  ui.state.me = null; ui.state.pendingInvite = "one-time-code-123456";
  ui.showLogin(); await settled();
  assert.equal(get("login-title").textContent, "加入「产品研究」");
  assert.match(get("login-intro").textContent, /小明 邀请你以成员加入/);
  assert.equal(get("enroll-code-field").hidden, true, "the code came with the link, there is nothing to type");
  assert.equal(get("enroll-submit").textContent, "加入并开始对话");
  assert.deepEqual(calls.filter(call => call.path === "/api/v3/invites/preview").map(call => call.body), [{code: "one-time-code-123456"}], "asked once, with the code in the body and never in the address");
});

test("an invitation from the instance owner, who has not given themselves a name, says 负责人 and not Owner", async () => {
  const {ui, get} = setup(taskFixture(), {respond: url => url.pathname === "/api/v3/invites/preview" ? preview({invited_by: "Owner"}) : undefined});
  ui.state.me = null; ui.state.pendingInvite = "one-time-code-123456";
  ui.showLogin(); await settled();
  assert.match(get("login-intro").textContent, /^负责人 邀请你以成员加入/);
});

test("a visitor invitation says up front that it is for reading", async () => {
  const {ui, get} = setup(taskFixture(), {respond: url => url.pathname === "/api/v3/invites/preview" ? preview({role: "viewer"}) : undefined});
  ui.state.me = null; ui.state.pendingInvite = "one-time-code-123456";
  ui.showLogin(); await settled();
  assert.match(get("login-intro").textContent, /以访客加入：能看，不能发言/);
});

test("a used or expired invitation says so and gives the code box back", async () => {
  const {ui, get} = setup(taskFixture(), {respond: url => url.pathname === "/api/v3/invites/preview" ? dead() : undefined});
  ui.state.me = null; ui.state.pendingInvite = "used-code-0123456789";
  ui.showLogin(); await settled();
  assert.match(get("login-intro").textContent, /这条邀请已经失效：用过了，或者超过 24 小时/);
  assert.equal(get("enroll-code-field").hidden, false, "they can type another code");
  assert.equal(get("enroll-code").value, "", "the dead code is not left in the box");
});

test("when the invitation cannot be looked up, joining is still offered", async () => {
  const {ui, get} = setup(taskFixture(), {respond: url => { if (url.pathname === "/api/v3/invites/preview") throw new TypeError("offline"); }});
  ui.state.me = null; ui.state.pendingInvite = "one-time-code-123456";
  ui.showLogin(); await settled();
  assert.equal(ui.state.invite, null, "no answer is not a no");
  assert.equal(get("login-title").textContent, "进入你的空间");
  assert.equal(get("enroll-code-field").hidden, true);
  assert.equal(get("enroll-submit").textContent, "创建账号并加入");
});

test("the sign-in page without an invitation is the ordinary one and asks nothing", async () => {
  const {ui, get, calls} = setup();
  ui.state.me = null; ui.state.pendingInvite = "";
  ui.showLogin(); await settled();
  assert.equal(get("login-title").textContent, "进入你的空间");
  assert.equal(get("enroll-code-field").hidden, false);
  assert.equal(get("join-banner").hidden, true);
  assert.equal(calls.some(call => call.path === "/api/v3/invites/preview"), false);
});

test("someone who is already in the project is told so, and the invitation stays unspent", async () => {
  const {ui, get} = setup(taskFixture(), {respond: url => url.pathname === "/api/v3/invites/preview" ? preview({already_member: true}) : undefined});
  ui.state.pendingInvite = "one-time-code-123456";
  ui.renderInvite(); await ui.loadInvite();
  assert.equal(get("join-banner").hidden, false);
  assert.equal(get("join-title").textContent, "你已经在「产品研究」里了");
  assert.match(get("join-lead").textContent, /没有被用掉/);
  assert.equal(get("accept-invite").textContent, "进入对话");
});

test("a signed-in person who accepts an invitation goes straight to the project's chat", async () => {
  const s = setup(taskFixture(), {respond: url => url.pathname === "/api/v3/enroll" ? joinedAs("member") : undefined});
  s.ui.state.pendingInvite = "one-time-code-123456";
  wire(s.context, 'bindButton("accept-invite"', 'bindForm("space-form"');
  s.get("accept-invite").listeners.get("click")(); await settled();
  assert.deepEqual(s.assigned, ["/?space=space-p&joined=member"]);
  assert.equal(s.ui.state.pendingInvite, "", "the code is forgotten once it has been used");
});

test("a signed-in person who was already in the project is taken there without a welcome", async () => {
  const s = setup(taskFixture(), {respond: url => url.pathname === "/api/v3/enroll" ? joinedAs("owner", {joined: false}) : undefined});
  s.ui.state.pendingInvite = "one-time-code-123456";
  wire(s.context, 'bindButton("accept-invite"', 'bindForm("space-form"');
  s.get("accept-invite").listeners.get("click")(); await settled();
  assert.deepEqual(s.assigned, ["/?space=space-p"], "nothing was joined, so there is nothing to welcome them to");
});

test("the button on a dead invitation only dismisses it", async () => {
  const s = setup();
  s.ui.state.pendingInvite = "used-code-0123456789"; s.ui.state.invite = {unavailable: true};
  wire(s.context, 'bindButton("accept-invite"', 'bindForm("space-form"');
  s.get("accept-invite").listeners.get("click")(); await settled();
  assert.equal(s.ui.state.pendingInvite, ""); assert.equal(s.get("join-banner").hidden, true);
  assert.equal(s.calls.some(call => call.path === "/api/v3/enroll"), false, "a code that is known to be dead is not sent");
  assert.deepEqual(s.assigned, []);
});

test("a new person is shown their recovery code once, then goes on into the project", async () => {
  const s = setup(taskFixture(), {respond: url => url.pathname === "/api/v3/enroll" ? joinedAs("member", {created: true, recovery_code: "RECOVERY-ONE-TIME"}) : undefined});
  s.ui.state.me = null; s.ui.state.pendingInvite = "one-time-code-123456";
  wire(s.context, 'bindForm("enroll-form"', 'bindForm("recover-form"');
  wire(s.context, '$("secret-close").addEventListener', '$("secret-copy").addEventListener');
  const enroll = fill(s.get("enroll-form"), {code: "one-time-code-123456", display_name: "小红"});
  enroll.listeners.get("submit")({preventDefault() {}}); await settled();
  assert.deepEqual(s.calls.find(call => call.path === "/api/v3/enroll").body, {code: "one-time-code-123456", display_name: "小红"});
  assert.equal(s.get("secret-dialog").open, true);
  assert.equal(s.get("secret-value").value, "RECOVERY-ONE-TIME");
  assert.equal(s.get("secret-close").textContent, "我已保存，进入对话");
  assert.deepEqual(s.assigned, [], "not before they have had the chance to copy it");
  s.get("secret-close").listeners.get("click")(); await settled();
  assert.deepEqual(s.assigned, ["/?space=space-p&joined=member"]);
  assert.equal(s.get("secret-value").value, "", "and the code is wiped from the page");
});

test("closing the recovery dialog any way at all still goes on into the project, once", async () => {
  const s = setup(taskFixture(), {respond: url => url.pathname === "/api/v3/enroll" ? joinedAs("viewer", {created: true, recovery_code: "RECOVERY-ONE-TIME"}) : undefined});
  s.ui.state.pendingInvite = "one-time-code-123456";
  wire(s.context, 'bindForm("enroll-form"', 'bindForm("recover-form"');
  wire(s.context, '$("secret-close").addEventListener', '$("secret-copy").addEventListener');
  fill(s.get("enroll-form"), {code: "one-time-code-123456", display_name: "小红"}).listeners.get("submit")({preventDefault() {}}); await settled();
  s.get("secret-dialog").listeners.get("close")(); // Escape, or the browser closing it
  s.get("secret-close").listeners.get("click")(); await settled();
  assert.deepEqual(s.assigned, ["/?space=space-p&joined=viewer"]);
});

test("a person with no recovery code to show goes straight into the project", async () => {
  const s = setup(taskFixture(), {respond: url => url.pathname === "/api/v3/enroll" ? joinedAs("member") : undefined});
  wire(s.context, 'bindForm("enroll-form"', 'bindForm("recover-form"');
  fill(s.get("enroll-form"), {code: "one-time-code-123456", display_name: "小红"}).listeners.get("submit")({preventDefault() {}}); await settled();
  assert.deepEqual(s.assigned, ["/?space=space-p&joined=member"]);
  assert.equal(s.get("secret-dialog").open, undefined);
});

test("a dialog closed by the program never moves the page", async () => {
  const s = setup();
  wire(s.context, '$("secret-close").addEventListener', '$("secret-copy").addEventListener');
  let moved = 0;
  s.ui.showSecret("保存", "SECRET", "说明", "", {then: () => { moved += 1; }});
  s.ui.closeSecret(); await settled();
  assert.equal(moved, 0);
  s.ui.showSecret("保存", "SECRET", "说明", "", {then: () => { moved += 1; }});
  s.ui.finishSecret(); await settled();
  assert.equal(moved, 1, "only the person closing it goes on");
});

test("someone who restores access with an invitation in hand goes on into the project", async () => {
  const me = {principal: {id: "bob", display_name: "小红", is_admin: false}, role: "member", space_id: "space-p", spaces: [PROJECT]};
  const s = setup(taskFixture(), {respond: url => {
    if (url.pathname === "/api/v3/recover") return answer({ok: true});
    if (url.pathname === "/api/v3/enroll") return joinedAs("member");
    if (url.pathname === "/api/v3/me") return answer(me);
  }});
  s.ui.state.me = null; s.ui.state.pendingInvite = "one-time-code-123456";
  wire(s.context, 'bindForm("recover-form"', 'bindButton("accept-invite"');
  fill(s.get("recover-form"), {code: "RECOVERY-CODE-0123"}).listeners.get("submit")({preventDefault() {}}); await settled();
  const enrol = s.calls.find(call => call.path === "/api/v3/enroll");
  assert.equal(enrol.body.require_existing_session, true, "joins as the person who just showed who they are");
  assert.deepEqual(s.assigned, ["/?space=space-p&joined=member"]);
});

test("restoring access still works when the invitation turns out to be dead, and says so", async () => {
  const me = {principal: {id: "bob", display_name: "小红", is_admin: false}, role: "owner", space_id: "personal", spaces: [{id: "personal", name: "Personal", kind: "personal"}]};
  const s = setup(taskFixture(), {respond: url => {
    if (url.pathname === "/api/v3/recover") return answer({ok: true});
    if (url.pathname === "/api/v3/enroll") return dead();
    if (url.pathname === "/api/v3/me") return answer(me);
  }});
  s.ui.state.me = null; s.ui.state.pendingInvite = "used-code-0123456789";
  wire(s.context, 'bindForm("recover-form"', 'bindButton("accept-invite"');
  fill(s.get("recover-form"), {code: "RECOVERY-CODE-0123"}).listeners.get("submit")({preventDefault() {}}); await settled();
  assert.deepEqual(s.assigned, []);
  assert.match(s.get("notice").textContent, /已恢复访问，但没能加入项目：这条邀请已经失效/);
  assert.equal(s.ui.state.pendingInvite, "");
  assert.notEqual(s.ui.state.me, null, "they are signed in to their own space");
});

test("a recovery code that does not work is told apart from an invitation that does not work", async () => {
  const s = setup(taskFixture(), {respond: url => url.pathname === "/api/v3/recover" ? dead() : undefined});
  s.ui.state.me = null;
  wire(s.context, 'bindForm("recover-form"', 'bindButton("accept-invite"');
  fill(s.get("recover-form"), {code: "WRONG-CODE-0123456"}).listeners.get("submit")({preventDefault() {}}); await settled();
  assert.equal(s.get("notice").textContent, "恢复码不对，或者已经用过了。");
  assert.equal(s.get("notice").className, "notice error");
});

test("the plain answers a person can act on", () => {
  const {ui, context} = setup();
  const VMError = runInNewContext("Error", context); // an error made where the page runs, as the page's own are
  const failed = (status, message, about) => Object.assign(new VMError(message), {status, about});
  assert.match(ui.readable(failed(400, "Invitation is unavailable.")), /^这条邀请已经失效/);
  assert.equal(ui.readable(failed(400, "Invitation is unavailable.", "recovery")), "恢复码不对，或者已经用过了。");
  assert.equal(ui.readable(failed(429, "Too many attempts. Try again later.")), "试得有点频繁了，过一分钟再试。");
  assert.equal(ui.readable(failed(400, "Name is too long.")), "Name is too long.", "any other 400 keeps the server's own words");
  assert.match(ui.readable(failed(400, "Too many unused invitations; use or revoke an existing invitation.")), /撤销还没用的邀请/, "a day-long invitation makes this reachable: it must say what to do");
});

test("the project's own spaces are named in Chinese, and a personal space is never called Personal", async () => {
  const me = {principal: {id: "alice", display_name: "小明", is_admin: false}, role: "owner", space_id: "personal", spaces: [{id: "personal", name: "Personal", kind: "personal"}, PROJECT]};
  const s = setup(taskFixture(), {respond: url => url.pathname === "/api/v3/me" ? answer(me) : undefined});
  s.ui.state.space = "personal";
  await s.ui.loadMe();
  assert.deepEqual(s.get("space-select").children.map(option => option.textContent), ["个人空间", "产品研究"]);
  assert.equal(s.get("workspace-context").textContent, "个人空间");
});

test("an invitation link made on this computer says that only this computer can open it", async () => {
  const s = setup(taskFixture(), {href: "http://127.0.0.1:18765/workspace?space=space-a", respond: url => {
    if (url.pathname === "/api/v3/invites") return answer({code: "abc123", expires_at: 4102444800});
    if (url.pathname === "/api/session") return answer({connection: {via: "local", address: "http://127.0.0.1:18765/", secure: false}});
  }});
  wire(s.context, 'bindForm("invite-form"', 'bindButton("revoke-invites"');
  fill(s.get("invite-form"), {role: "member", label: ""}).listeners.get("submit")({preventDefault() {}}); await settled();
  assert.equal(s.get("secret-value").value, "http://127.0.0.1:18765/workspace#invite=abc123");
  assert.match(s.get("secret-extra").textContent, /只有这台电脑打得开/);
  assert.match(s.get("secret-extra").textContent, /只能用一次/);
});

test("an invitation link made behind a public address uses that address and carries no warning", async () => {
  const s = setup(taskFixture(), {href: "http://127.0.0.1:18765/workspace?space=space-a", respond: url => {
    if (url.pathname === "/api/v3/invites") return answer({code: "abc123", expires_at: 4102444800});
    if (url.pathname === "/api/session") return answer({connection: {via: "tailscale", address: "https://qixu.example.ts.net/", secure: true}});
  }});
  wire(s.context, 'bindForm("invite-form"', 'bindButton("revoke-invites"');
  fill(s.get("invite-form"), {role: "member", label: ""}).listeners.get("submit")({preventDefault() {}}); await settled();
  assert.equal(s.get("secret-value").value, "https://qixu.example.ts.net/workspace#invite=abc123");
  assert.doesNotMatch(s.get("secret-extra").textContent, /这台电脑/);
});

test("a link made from an address other devices can already reach is left as it is", async () => {
  const {ui, calls} = setup(taskFixture());
  const made = await ui.inviteLink("abc123", "invite");
  assert.equal(made.link, "https://example.invalid/workspace#invite=abc123");
  assert.equal(made.local, false);
  assert.equal(calls.some(call => call.path === "/api/session"), false, "nothing to ask");
});

test("the owner can take back the invitations nobody has used yet", async () => {
  const s = setup(taskFixture(), {role: "owner", respond: url => url.pathname === "/api/v3/invites/revoke" ? answer({ok: true, revoked: 2}) : undefined});
  wire(s.context, 'bindButton("revoke-invites"', 'bindForm("task-form"');
  s.get("revoke-invites").listeners.get("click")(); await settled();
  assert.deepEqual(s.calls.find(call => call.path === "/api/v3/invites/revoke").body, {});
  assert.equal(s.get("notice").textContent, "已收回 2 条还没用的邀请。");
  const none = setup(taskFixture(), {role: "owner", respond: url => url.pathname === "/api/v3/invites/revoke" ? answer({ok: true, revoked: 0}) : undefined});
  wire(none.context, 'bindButton("revoke-invites"', 'bindForm("task-form"');
  none.get("revoke-invites").listeners.get("click")(); await settled();
  assert.equal(none.get("notice").textContent, "没有还没用的邀请。");
});
