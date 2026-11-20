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
  removeAttribute(name) { this.attrs.delete(name); }
  focus() {}
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
const definitions = source.slice(0, source.indexOf('bindForm("enroll-form"')).replace(requestImport, "").replace('import { createProductShell } from "./product-shell.js";\n', "");
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
  const location = {};
  const setLocation = value => {const url=new URL(value,"https://example.invalid");Object.assign(location,{href:url.href,origin:url.origin,pathname:url.pathname,search:url.search,hash:url.hash});};
  setLocation(href);
  const historyCalls=[];
  const context = {document, URL, URLSearchParams, AbortController, DOMException, RequestScope, buildTaskRequest, buildStepRequest, buildAnswerRequest, crypto: webcrypto,
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
  runInNewContext(definitions + "\nglobalThis.ui={state,renderTask,loadTasks,loadMembers,editingTask,showLogin,routeFromLocation,workspaceUrl,selectTab,selectTaskView,followLocation,showPanel,loadMe,openKnowledge,closeKnowledge,loadKnowledge,bindButton,notice};", context);
  context.ui.state.me = {principal: {id: principal, is_admin: false}, role, spaces: []};
  context.ui.state.space = "space-a"; context.ui.state.taskId = task.id; context.ui.state.taskView="computer";
  context.ui.renderTask(task);
  return {ui: context.ui, detail: get("task-detail"), get, calls, task, context, location, historyCalls, setLocation};
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
  const draft=get("knowledge-form");draft.dataset.dirty="1";
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

test("a knowledge draft is kept when closing or switching documents is declined", () => {
  const {ui,get}=setup(taskFixture(),{confirm:()=>false});
  const editor=get("knowledge-form");
  for(const [name,value] of [["path","draft.md"],["text","unsaved text"]]) {const input=new Element("input");input.name=name;input.value=value;editor.append(input);}
  editor.dataset.dirty="1";get("knowledge-editor-panel").hidden=false;
  ui.openKnowledge({path:"other.md",text:"other text",revision:3});
  ui.closeKnowledge();
  assert.equal(editor.elements.namedItem("text").value,"unsaved text");
  assert.equal(editor.elements.namedItem("path").value,"draft.md");
  assert.equal(get("knowledge-editor-panel").hidden,false);
});


test("an in-flight knowledge save pins the editor until its revision and dirty state settle", async () => {
  let release,confirmations=0;
  const saved=new Promise(resolve=>{release=resolve;});
  const {ui,get,context,calls}=setup(taskFixture(),{confirm:()=>{confirmations++;return true;},respond:(url,options)=>{
    if(url.pathname!=="/api/v3/knowledge") return;
    if(options.method==="POST") return saved;
    return {ok:true,status:200,json:async()=>({documents:[{path:"a.md",text:"saved A",revision:8}]})};
  }});
  const editor=get("knowledge-form");
  for(const [name,value] of [["path","a.md"],["text","saved A"]]) {const input=new Element("input");input.name=name;input.value=value;editor.append(input);}
  editor.dataset.dirty="1";ui.state.knowledgeRevision=7;
  get("knowledge-editor-panel").hidden=false;get("knowledge-empty-editor").hidden=true;
  const start=source.indexOf('bindForm("knowledge-form",');
  const end=source.indexOf('bindForm("knowledge-search-form"',start);
  runInNewContext(source.slice(start,end),context);
  editor.listeners.get("submit")({preventDefault(){}});
  await settled();
  assert.equal(editor.dataset.busy,"1");
  ui.openKnowledge({path:"b.md",text:"draft B",revision:30});
  ui.openKnowledge();
  ui.closeKnowledge();
  assert.equal(editor.elements.namedItem("path").value,"a.md");
  assert.equal(editor.elements.namedItem("text").value,"saved A");
  assert.equal(ui.state.knowledgeRevision,7);assert.equal(editor.dataset.dirty,"1");
  assert.equal(get("knowledge-editor-panel").hidden,false);
  assert.equal(get("knowledge-empty-editor").hidden,true);
  assert.equal(confirmations,0);assert.match(get("notice").textContent,/正在保存/);
  assert.deepEqual(calls.find(call=>call.body?.path==="a.md").body,{path:"a.md",text:"saved A",revision:7});
  release({ok:true,status:200,json:async()=>({revision:8})});
  await settled();
  assert.equal(editor.elements.namedItem("path").value,"a.md");
  assert.equal(ui.state.knowledgeRevision,8);
  assert.equal(editor.dataset.busy,undefined);assert.equal(editor.dataset.dirty,undefined);
  ui.openKnowledge({path:"b.md",text:"draft B",revision:30});
  assert.equal(editor.elements.namedItem("path").value,"b.md");
  assert.equal(ui.state.knowledgeRevision,30);
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

for(const succeeds of [true,false])test(`first knowledge save ${succeeds ? "enables history for the new revision" : "keeps history unavailable after failure"}`,async()=>{
  const {ui,get,context}=setup(taskFixture(),{respond:(url,options)=>{
    if(url.pathname!=="/api/v3/knowledge")return;
    if(options.method==="POST")return {ok:succeeds,status:succeeds ? 200 : 422,json:async()=>succeeds ? {revision:1} : {error:"Invalid document"}};
    return {ok:true,status:200,json:async()=>({documents:[{path:"new.md",text:"New note",revision:1}]})};
  }});
  const editor=get("knowledge-form");
  for(const [name,value] of [["path","new.md"],["text","New note"]]) {const input=new Element("input");input.name=name;input.value=value;editor.append(input);}
  const history=get("knowledge-history");history.tagName="BUTTON";history.disabled=true;editor.append(history);
  editor.dataset.dirty="1";ui.state.knowledgeRevision=null;
  const start=source.indexOf('bindForm("knowledge-form",');
  const end=source.indexOf('bindForm("knowledge-search-form"',start);
  runInNewContext(source.slice(start,end),context);
  editor.listeners.get("submit")({preventDefault(){}});await settled();
  assert.equal(history.disabled,!succeeds);
  assert.equal(ui.state.knowledgeRevision,succeeds ? 1 : null);
  assert.equal(editor.dataset.dirty,succeeds ? undefined : "1");
  assert.equal(editor.dataset.busy,undefined);
  assert.equal(editor.elements.namedItem("text").value,"New note");
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
  assert.equal(welcome.context.document.title, "资料 · Personal Agent"); assert.equal(welcome.get("masthead-title").textContent, "资料");

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
  ui.notice("资料已保存到当前空间。");
  assert.equal(get("notice").textContent, "资料已保存到当前空间。"); assert.equal(get("notice").hidden, false);
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
