import assert from "node:assert/strict";
import { test } from "node:test";
import { FakeElement, elements, find, toHtml } from "./fake_dom.mjs";
import { createAssistantCards, createAssistantWelcome, greeting, nativeCardRequest, workspaceRequest } from "../../src/personal_agent/channels/web/static/assistant.js";

class Element extends FakeElement {
  constructor(tag) { super(tag); this.value = ""; this.disabled = false; this.checked = false; this.hidden = false; this.dataset = {}; }
  append(...kids) {
    for (const kid of kids) if (kid?.parentElement) kid.remove();
    super.append(...kids); for (const child of this.children) child.parentElement = this;
  }
  replaceChildren(...kids) { for (const child of [...this.children]) { if (child.remove) child.remove(); else child.parentElement = null; } this.children = []; this.append(...kids); }
  insertBefore(child, reference) { child.remove?.(); const at = this.children.indexOf(reference); this.children.splice(at < 0 ? this.children.length : at, 0, child); child.parentElement = this; }
  remove() {
    if (this.ownerDocument?.activeElement && (this === this.ownerDocument.activeElement || elements(this).includes(this.ownerDocument.activeElement))) this.ownerDocument.activeElement = null;
    if (this.parentElement) { this.parentElement.children = this.parentElement.children.filter(child => child !== this); this.parentElement = null; }
  }
  focus() { if (this.ownerDocument) this.ownerDocument.activeElement = this; }
  showModal() { this.open = true; }
  close() { if (!this.open) return; this.open = false; this.listeners.get("close")?.(); }
  reportValidity() { return true; }
  get elements() { const controls = elements(this).filter(item => ["INPUT", "TEXTAREA", "SELECT", "BUTTON"].includes(item.tagName)); controls.namedItem = name => controls.find(item => item.name === name); return controls; }
}
const documentFixture = () => {
  const doc = { hidden: false, activeElement: null };
  doc.createElement = tag => {
    const element = new Element(tag); element.ownerDocument = doc;
    // As in a browser, a textarea's type cannot be assigned; in a module that throws.
    if (tag === "textarea") Object.defineProperty(element, "type", { get: () => "textarea" });
    return element;
  };
  doc.createElementNS = (_namespace, tag) => doc.createElement(tag);
  doc.body = doc.createElement("body"); return doc;
};
const button = (root, label) => find(root, "button").find(item => item.textContent === label);
const click = async control => { assert.ok(control, "button exists"); return control.listeners.get("click")?.({ preventDefault() {} }); };
const submit = async form => form.listeners.get("submit")({ preventDefault() {} });
const field = (root, name) => elements(root).find(item => item.name === name);
const clone = value => JSON.parse(JSON.stringify(value));
const task = () => ({ id: "task-a", title: "整理研究", revision: 3, plan_hash: "f".repeat(64), state: "planned", visibility: "private", steps: [{ id: "s1", agent_id: "agent-a", agent_name: "研究助手", agent_kind: "grok", shared_environment: true, prompt: "只使用这些资料", acceptance: "注明出处", depends_on: [], state: "planned" }] });
const planCard = () => ({ id: "plan-a", kind: "agent_plan", title: "整理研究", status: "planned", data: { task: task() }, actions: [{ id: "approve" }, { id: "cancel" }] });
const proposal = (action = "create_space", draft = { name: "读书项目" }, target = "") => ({ id: "ac_example", kind: "workspace_action", title: "核对这项操作", status: "proposed", data: { action, target_id: target, draft }, actions: [{ id: "open" }] });
const agents = [{ id: "agent-a", name: "研究助手", kind: "grok", state: "active", owner_id: "alice", allowed_actor_ids: [], shared_environment: true, wake_mode: "manual", secret_env: {} }, { id: "agent-b", name: "整理助手", kind: "muse", state: "active", owner_id: "alice", shared_environment: true, wake_mode: "manual" }];
const connectionFixture = () => ({ agent_id: "agent-a", mcp_url: "https://example.invalid/mcp?space=current", openapi_url: "https://example.invalid/worker/openapi.json?space=current", openapi_download_path: "/api/v4/agents/agent-a/openapi.json", bridge_url: "https://example.invalid/api/v4/bridge.py", instructions: "<script>not executable</script> Claim first; report a result." });

function setup({ cards = [], respond, admin = false, role = "owner" } = {}) {
  const document = documentFixture(), state = { cards: clone(cards) }, calls = [], notices = [], prompts = [], downloads = [], setupDownloads = [], contextChanges = [];
  const api = async (path, options = {}) => {
    const call = { path, method: options.method || "GET", json: options.json }; calls.push(call);
    const override = respond?.(path, options, state); if (override !== undefined) return await override;
    if (path === "/api/assistant/cards") return { space_id: "space-a", cards: clone(state.cards) };
    if (path === "/api/v3/me") return { principal: { id: "alice", display_name: "我的账号", is_admin: admin }, role, space_id: "space-a", spaces: [{ id: "space-a", name: "个人空间", kind: "personal" }] };
    if (path === "/api/v3/members") return { members: [{ principal_id: "alice", display_name: "我", role: "owner" }, { principal_id: "bob", display_name: "合作者", role: "member" }] };
    if (path === "/api/v4/agents") return options.method ? { agent: agents[0], worker_token: "one-display-token" } : { agents };
    if (path === "/api/v4/agents/agent-a") return { agent: agents[0] };
    if (path === "/api/v3/devices") return { devices: [{ id: 12, label: "我的手机" }] };
    if (path === "/api/v3/nodes") return { nodes: [{ id: "node-a", label: "我的电脑" }] };
    if (path === "/api/v3/channel-bindings") return { bindings: [{ channel: "test", account_id: "account", sender_id: "sender" }] };
    if (path.endsWith("/begin")) {
      const record = state.cards.find(item => path.includes(item.id));
      if (record.status !== "proposed") throw Object.assign(new Error("already begun"), { status: 409 });
      record.status = "executing"; record.actions = [];
      return { execution_id: "execution-a", action: record.data.action, target_id: record.data.target_id, payload: options.json.payload, routing: record.data.action === "confirm_wake_stopped" ? { wake_id: record.data.draft.wake_id } : {} };
    }
    if (path.endsWith("/complete")) { state.cards = state.cards.filter(item => !path.includes(item.id)); return { ok: true }; }
    return { ok: true };
  };
  const ui = createAssistantCards({ document, api, onNotice: (...args) => notices.push(args), onPrompt: prompt => prompts.push(prompt), onContextChanged: change => contextChanges.push(change), downloadArtifact: (...args) => downloads.push(args), downloadConnection: (...args) => setupDownloads.push(args), pollMs: 0 });
  document.body.append(ui.element);
  return { ui, document, state, calls, notices, prompts, downloads, setupDownloads, contextChanges };
}

test("welcome shortcuts prepare messages without sending or creating work", async () => {
  const document = documentFixture(), prompts = [];
  const welcome = createAssistantWelcome({ document, onPrompt: value => prompts.push(value) });
  await click(find(welcome, "button")[0]);
  assert.deepEqual(prompts, ["帮我看看现在有哪些需要我处理的事情。"]);
  assert.equal(find(welcome, "button").length, 4);
});

test("the welcome greets by the hour it is opened and never invents one", () => {
  const at = hour => createAssistantWelcome({ document: documentFixture(), onPrompt() {}, now: () => ({ getHours: () => hour }) });
  const eyebrow = welcome => find(welcome, "p").find(item => item.className === "assistant-eyebrow").textContent;
  assert.deepEqual([0, 4, 5, 10, 11, 12, 13, 17, 18, 23].map(hour => eyebrow(at(hour))),
    ["夜深了", "夜深了", "早上好", "早上好", "中午好", "中午好", "下午好", "下午好", "晚上好", "晚上好"]);
  for (const odd of [-1, 24, 7.5, NaN, undefined, "9"]) assert.equal(greeting(odd), "你好");
});

test("native decisions keep the reviewed revision, hash, attempt and cross-space review route", () => {
  const card = planCard();
  assert.deepEqual(nativeCardRequest(card, "approve"), { path: "/api/v4/tasks/task-a/approve", method: "POST", json: { revision: 3, plan_hash: "f".repeat(64) } });
  const review = { kind: "execution_approval", status: "awaiting_approval", actions: [{ id: "approve" }], data: { space_id: "another-private-space", job_id: "job-a", plan_hash: "job-hash", url: "https://evil.invalid" } };
  assert.deepEqual(nativeCardRequest(review, "approve"), { path: "/api/v3/review-jobs", method: "POST", json: { space_id: "another-private-space", job_id: "job-a", plan_hash: "job-hash", allowed: true } });
  const result = { ...card, kind: "agent_result", status: "result_reported", actions: [{ id: "reject" }], data: { task: task(), step: { id: "s1" }, attempt: { id: "attempt-a" } } };
  assert.deepEqual(nativeCardRequest(result, "reject", { reason: " 缺少出处 " }).json, { step_id: "s1", attempt_id: "attempt-a", revision: 3, plan_hash: "f".repeat(64), reason: "缺少出处" });
  assert.throws(() => nativeCardRequest(result, "accept"), /变化/);
  assert.throws(() => nativeCardRequest({ ...card, status: "completed" }, "approve"), /变化/);
  assert.throws(() => nativeCardRequest({ ...card, status: "revoked" }, "approve"), /变化/);
});

test("a delivered result carries its reason box, and one card never takes the whole inbox down", async () => {
  const card = { ...planCard(), id: "result-a", kind: "agent_result", status: "result_reported", actions: [{ id: "accept" }, { id: "reject" }], data: { task: task(), step: { id: "s1", acceptance: "每条都有出处" }, attempt: { id: "attempt-a", result: { text: "摘要写好了。" } } } };
  const s = setup({ cards: [card, planCard()] }); await s.ui.refresh();
  assert.ok(field(s.ui.element, "reason"), "the form for turning a result down is there");
  assert.ok(button(s.ui.element, "验收通过"));
  assert.ok(button(s.ui.element, "确认并派发"), "the other card is still shown");
  assert.doesNotMatch(toHtml(s.ui.element), /暂时无法更新/);
});

test("fixed workspace routes cover all 24 actions without interpreting model URLs", () => {
  const paths = new Map([
    ["create_space", "/api/v3/spaces"], ["invite_member", "/api/v3/invites"], ["change_member", "/api/v3/members"],
    ["pair_device", "/api/v3/devices/pair"], ["revoke_device", "/api/v3/devices/target/revoke"], ["create_recovery_code", "/api/v3/recovery-code"],
    ["bind_channel", "/api/v3/channel-binding"], ["revoke_channel", "/api/v3/channel-bindings/revoke"], ["invite_node", "/api/v3/node-invites"],
    ["accept_project_invite", "/api/v3/enroll"],
    ["revoke_node", "/api/v3/nodes/target/revoke"], ["write_knowledge", "/api/v3/knowledge"], ["create_execution_task", "/api/v3/tasks"],
    ["revise_execution_task", "/api/v3/tasks/target/revise"], ["add_execution_step", "/api/v3/tasks/target/steps"], ["cancel_execution_task", "/api/v3/tasks/target/cancel"],
    ["create_agent", "/api/v4/agents"], ["edit_agent", "/api/v4/agents/target"], ["rotate_agent_token", "/api/v4/agents/target/token"], ["revoke_agent", "/api/v4/agents/target/revoke"],
    ["create_agent_task", "/api/v4/tasks"], ["cancel_agent_task", "/api/v4/tasks/target/cancel"], ["confirm_agent_stopped", "/api/v4/tasks/target/stopped"],
    ["confirm_wake_stopped", "/api/v4/tasks/target/wakes/frozen-wake/stopped"],
  ]);
  assert.equal(paths.size, 24);
  for (const [action, path] of paths) assert.equal(workspaceRequest(action, "target", { note: "checked" }, { wake_id: "frozen-wake" }).path, path);
  assert.equal(workspaceRequest("edit_agent", "a", {}).method, "PATCH");
  for (const action of ["https://evil.invalid", "constructor", "__proto__", "fetch"]) assert.throws(() => workspaceRequest(action, "target", {}), /暂不支持/);
});

test("concurrent clicks on a plan send one mutation", async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const s = setup({ cards: [planCard()], respond: path => path.endsWith("/approve") ? pending : undefined });
  await s.ui.refresh(); const approve = button(s.ui.element, "确认并派发");
  const first = click(approve); await click(approve);
  assert.equal(s.calls.filter(item => item.path.endsWith("/approve")).length, 1);
  release({ ok: true }); await first;
  assert.equal(button(s.ui.element, "确认并派发"), undefined);
});

test("a stale or removed card cannot submit, including a retained old click handler", async () => {
  const s = setup({ cards: [planCard()] }); await s.ui.refresh();
  const old = button(s.ui.element, "确认并派发");
  s.state.cards[0].data.task.revision = 4; s.state.cards[0].data.task.plan_hash = "e".repeat(64); await s.ui.refresh();
  await click(old); assert.equal(s.calls.filter(item => item.method === "POST").length, 0);
  const newer = button(s.ui.element, "确认并派发");
  s.state.cards = []; await s.ui.refresh(); await click(newer);
  assert.equal(s.calls.filter(item => item.method === "POST").length, 0);
});

test("read-only and unknown cards never gain invented action buttons; remote text stays text", async () => {
  const card = planCard(); card.actions = []; card.title = "<img src=x onerror=alert(1)>"; card.data.task.steps[0].prompt = "<script>run()</script>";
  const s = setup({ cards: [card, { id: "alien", kind: "arbitrary", title: "Other", actions: [{ id: "open", url: "https://evil.invalid" }] }], role: "viewer" });
  await s.ui.refresh(); assert.equal(find(s.ui.element, "button").length, 1);
  assert.match(toHtml(s.ui.element), /&lt;script&gt;run\(\)&lt;\/script&gt;/);
  assert.equal(find(s.ui.element, "img").length, 0);
});

test("question inputs survive unchanged polling and answers use each API's revision contract", async () => {
  for (const kind of ["execution_question", "agent_question"]) {
    const card = { id: "q-a", kind, title: "需要补充", status: "waiting_input", data: { task: task(), question: { id: "question-a", question: "哪种格式？", text: "哪种格式？" }, revision: 3 }, actions: [{ id: "answer" }] };
    const s = setup({ cards: [card] }); await s.ui.refresh();
    const answer = field(s.ui.element, "answer"); answer.value = " 使用 Markdown "; answer.focus(); await s.ui.refresh();
    assert.equal(field(s.ui.element, "answer"), answer); assert.equal(s.document.activeElement, answer);
    s.state.cards[0].data.task.steps[0].progress_text = "其他步骤仍在执行"; await s.ui.refresh();
    assert.equal(field(s.ui.element, "answer"), answer); assert.equal(s.document.activeElement, answer);
    await submit(find(s.ui.element, "form")[0]);
    const request = s.calls.find(item => item.path.endsWith("/answer"));
    assert.deepEqual(request.json, kind === "execution_question" ? { answer: "使用 Markdown", expected_revision: 3 } : { answer: "使用 Markdown" });
    assert.equal(request.path, `/api/${kind === "execution_question" ? "v3" : "v4"}/tasks/task-a/questions/question-a/answer`);
  }
});

test("workspace submit begins once and executes only the server-frozen action payload", async () => {
  const s = setup({ cards: [proposal()], respond: (path, options) => path.endsWith("/begin") ? { execution_id: "frozen", action: "create_space", target_id: "", payload: { name: "规范后的项目名" } } : undefined });
  await s.ui.refresh(); await click(button(s.ui.element, "填写"));
  field(s.ui.element, "name").value = "用户核对的名称";
  await submit(find(s.ui.element, "form")[0]);
  assert.deepEqual(s.calls.filter(item => item.method === "POST").map(item => [item.path, item.json]), [
    ["/api/assistant/cards/ac_example/begin", { payload: { name: "用户核对的名称" } }],
    ["/api/v3/spaces", { name: "规范后的项目名" }],
    ["/api/assistant/cards/ac_example/complete", { execution_id: "frozen", outcome: "succeeded" }],
  ]);
  assert.equal(s.prompts.length, 0);
});

test("an unknown response never retries an operation, including after a new page reads persisted state", async () => {
  const s = setup({ cards: [proposal()], respond: path => path === "/api/v3/spaces" ? Promise.reject(Object.assign(new Error("connection lost"), { status: 0 })) : path.endsWith("/complete") ? { ok: true } : undefined });
  await s.ui.refresh(); await click(button(s.ui.element, "填写")); const form = find(s.ui.element, "form")[0]; await submit(form); await submit(form);
  assert.equal(s.calls.filter(item => item.path === "/api/v3/spaces").length, 1);
  assert.deepEqual(s.calls.find(item => item.path.endsWith("/complete")).json, { execution_id: "execution-a", outcome: "uncertain" });
  await s.ui.refresh(); assert.equal(button(s.ui.element, "填写"), undefined);
  const resumed = setup({ cards: s.state.cards }); await resumed.ui.refresh(); assert.equal(button(resumed.ui.element, "填写"), undefined);
  assert.match(resumed.ui.element.textContent, /结果还没确认/);
});

test("a lost begin response does not call the native endpoint or allow another begin", async () => {
  const s = setup({ cards: [proposal()], respond: (path, options, state) => {
    if (path.endsWith("/begin")) { state.cards[0].status = "executing"; state.cards[0].actions = []; return Promise.reject(Object.assign(new Error("lost response"), { status: 0 })); }
  } });
  await s.ui.refresh(); await click(button(s.ui.element, "填写")); const form = find(s.ui.element, "form")[0]; await submit(form); await submit(form); await s.ui.refresh();
  assert.equal(s.calls.filter(item => item.path.endsWith("/begin")).length, 1);
  assert.equal(s.calls.filter(item => item.path === "/api/v3/spaces").length, 0);
  assert.equal(button(s.ui.element, "填写"), undefined);
});

test("credentials only appear in the dedicated dialog and complete never contains the secret", async () => {
  const s = setup({ cards: [proposal("create_recovery_code", {})], respond: path => path === "/api/v3/recovery-code" ? { code: "single-display-recovery" } : undefined });
  await s.ui.refresh(); await click(button(s.ui.element, "填写"));
  await submit(find(s.ui.element, "form")[0]);
  const dialog = find(s.document.body, "dialog")[0], value = find(dialog, "textarea")[0];
  assert.equal(value.value, "single-display-recovery"); assert.doesNotMatch(s.ui.element.textContent, /single-display-recovery/);
  assert.doesNotMatch(JSON.stringify(s.calls), /single-display-recovery/); assert.equal(s.prompts.length, 0);
  await click(button(dialog, "已保存，关闭")); assert.equal(value.value, ""); assert.equal(find(s.document.body, "dialog").length, 0);
});

test("existing-account invite codes reach only enrollment, never begin, complete, prompts or storage", async () => {
  const code = "invitation_code_for_my_account";
  const s = setup({ cards: [proposal("accept_project_invite", { code: "model_must_not_prefill" })] });
  await s.ui.refresh(); await click(button(s.ui.element, "填写"));
  const control = field(s.ui.element, "invite_code"); assert.equal(control.value, ""); assert.equal(control.type, "password");
  control.value = `https://example.invalid/workspace#invite=${code}`;
  await submit(find(s.ui.element, "form")[0]);
  assert.equal(control.value, "");
  assert.deepEqual(s.calls.find(item => item.path.endsWith("/begin")).json, { payload: {} });
  assert.deepEqual(s.calls.find(item => item.path === "/api/v3/enroll").json, { code, display_name: "我的账号", require_existing_session: true });
  assert.deepEqual(s.calls.find(item => item.path.endsWith("/complete")).json, { execution_id: "execution-a", outcome: "succeeded" });
  assert.equal(s.prompts.length, 0);
  assert.doesNotMatch(JSON.stringify(s.calls.filter(item => item.path !== "/api/v3/enroll")), /invitation_code_for_my_account/);
});

test("revoked or destroyed invite cards clear unsubmitted private input", async () => {
  for (const destroy of [false, true]) {
    const s = setup({ cards: [proposal("accept_project_invite", {})] }); await s.ui.refresh(); await click(button(s.ui.element, "填写"));
    const control = field(s.ui.element, "invite_code"); control.value = "private_invitation_code";
    if (destroy) s.ui.destroy(); else { s.state.cards = []; await s.ui.refresh(); }
    assert.equal(control.value, ""); assert.equal(s.calls.filter(item => item.method === "POST").length, 0);
  }
});

test("destroying the session clears credentials and ignores late private responses", async () => {
  let resolve; const deferred = new Promise(done => { resolve = done; });
  const s = setup({ respond: path => path === "/api/assistant/cards" ? deferred : undefined });
  const pending = s.ui.refresh(); s.ui.destroy(); resolve({ cards: [planCard()] }); await pending;
  assert.equal(find(s.ui.element, "article").length, 0);
  const t = setup({ cards: [proposal("pair_device", { label: "我的手机" })], respond: path => path === "/api/v3/devices/pair" ? { code: "device-code" } : undefined });
  await t.ui.refresh(); await click(button(t.ui.element, "填写")); await submit(find(t.ui.element, "form")[0]);
  const secret = find(t.document.body, "dialog")[0], value = find(secret, "textarea")[0]; t.ui.destroy(); assert.equal(value.value, ""); assert.equal(find(t.document.body, "dialog").length, 0);
});

test("stop confirmations ignore model proof text and wake routing uses the immutable returned reference", async () => {
  const card = proposal("confirm_wake_stopped", { wake_id: "wake-fixed", note: "model claims stopped" }, "task-a");
  const s = setup({ cards: [card] }); await s.ui.refresh(); await click(button(s.ui.element, "填写"));
  assert.equal(field(s.ui.element, "note"), undefined);
  await submit(find(s.ui.element, "form")[0]);
  assert.deepEqual(s.calls.find(item => item.path.endsWith("/begin")).json, { payload: { note: "已确认停止" } });
  assert.deepEqual(s.calls.find(item => item.path.endsWith("/stopped")), { path: "/api/v4/tasks/task-a/wakes/wake-fixed/stopped", method: "POST", json: { note: "已确认停止" } });
});

test("member updates use the strict DTO without the legacy extra action field", async () => {
  for (const revoke of [false, true]) {
    const s = setup({ cards: [proposal("change_member", { role: "viewer", revoke }, "bob")] }); await s.ui.refresh(); await click(button(s.ui.element, "填写")); await submit(find(s.ui.element, "form")[0]);
    assert.deepEqual(s.calls.find(item => item.path.endsWith("/begin")).json.payload, revoke ? { principal_id: "bob", revoke: true } : { principal_id: "bob", role: "viewer" });
  }
});

test("leaving the current project stops scoped reads before navigation; self-demotion skips forbidden completion", async () => {
  for (const revoke of [true, false]) {
    const s = setup({ cards: [proposal("change_member", { role: "viewer", revoke }, "alice")] });
    await s.ui.refresh(); await click(button(s.ui.element, "填写"));
    await submit(find(s.ui.element, "form")[0]);
    const actionAt = s.calls.findIndex(item => item.path === "/api/v3/members" && item.method === "POST");
    assert.ok(actionAt > 0); assert.equal(s.calls.some(item => item.path.endsWith("/complete")), false);
    assert.deepEqual(s.contextChanges, [revoke ? { leftSpace: true } : { roleChanged: true }]);
    if (revoke) { await s.ui.refresh(); assert.equal(s.calls.length, actionAt + 1); }
  }
});

test("agent connection forms ignore model secret references and validate only user-entered names", async () => {
  const card = proposal("create_agent", { name: "我的 Grok", kind: "grok", wake_mode: "grok_routine", secret_env: { webhook_url: "MODEL_SUPPLIED", webhook_key: "MODEL_KEY" } });
  const s = setup({ cards: [card], admin: true }); await s.ui.refresh(); await click(button(s.ui.element, "填写"));
  assert.equal(field(s.ui.element, "webhook_url_env").value, "");
  field(s.ui.element, "webhook_url_env").value = "https://example.invalid/private"; field(s.ui.element, "webhook_key_env").value = "GROK_KEY";
  const form = find(s.ui.element, "form")[0]; await submit(form); assert.equal(s.calls.filter(item => item.path.endsWith("/begin")).length, 0);
  field(s.ui.element, "webhook_url_env").value = "GROK_URL"; await submit(form);
  assert.deepEqual(s.calls.find(item => item.path.endsWith("/begin")).json.payload.secret_env, { webhook_url: "GROK_URL", webhook_key: "GROK_KEY" });
  assert.equal(s.prompts.length, 0);
});

test("new and rotated credentials include setup guidance and two fixed download choices only in the safe dialog", async () => {
  for (const action of ["create_agent", "rotate_agent_token"]) {
    const s = setup({ cards: [proposal(action, action === "create_agent" ? { name: "研究助手", kind: "grok" } : {}, action === "create_agent" ? "" : "agent-a")], respond: (path, options) => {
      if ((path === "/api/v4/agents" || path === "/api/v4/agents/agent-a/token") && options.method === "POST") return { agent: agents[0], worker_token: "private-setup-token", connection: connectionFixture() };
    } });
    await s.ui.refresh(); await click(button(s.ui.element, "填写"));
    await submit(find(s.ui.element, "form")[0]);
    const dialog = find(s.document.body, "dialog")[0];
    assert.ok(dialog); assert.match(dialog.textContent, /接入指引与文件/);
    assert.ok(find(dialog, "textarea").some(control => control.value === connectionFixture().mcp_url));
    assert.ok(find(dialog, "textarea").some(control => control.value === connectionFixture().openapi_url));
    assert.ok(find(dialog, "textarea").some(control => control.value === connectionFixture().bridge_url));
    assert.match(toHtml(dialog), /&lt;script&gt;not executable&lt;\/script&gt;/); assert.equal(find(dialog, "script").length, 0);
    assert.equal(find(dialog, "a").length, 0);
    const openapi = button(dialog, "下载 OpenAPI 定义"), bridge = button(dialog, "下载 Python Bridge");
    await click(openapi); await click(bridge);
    assert.deepEqual(s.setupDownloads, [["agent-a", "openapi"], ["agent-a", "bridge"]]);
    assert.doesNotMatch(JSON.stringify(s.calls), /private-setup-token/); assert.equal(s.prompts.length, 0);
    const addresses = find(dialog, "textarea"); await click(button(dialog, "已保存，关闭"));
    assert.ok(addresses.every(control => control.value === ""));
    await click(bridge); assert.equal(s.setupDownloads.length, 2);
  }
});

test("editing an existing connection reads its real guide without creating or rotating a credential", async () => {
  const s = setup({ cards: [proposal("edit_agent", { name: "研究助手", connection: { openapi_url: "https://model.invalid/unsafe" } }, "agent-a")], respond: path => path === "/api/v4/agents/agent-a" ? { agent: agents[0], connection: connectionFixture() } : undefined });
  await s.ui.refresh(); await click(button(s.ui.element, "填写"));
  const guide = find(s.ui.element, "details").find(item => find(item, "summary")[0]?.textContent === "接入指引与文件");
  assert.ok(guide); assert.equal(Boolean(guide.open), false);
  assert.ok(find(guide, "textarea").some(control => control.value === connectionFixture().openapi_url));
  assert.equal(find(guide, "textarea").some(control => control.value.includes("model.invalid")), false);
  await click(button(guide, "下载 OpenAPI 定义")); await click(button(guide, "下载 Python Bridge"));
  assert.deepEqual(s.setupDownloads, [["agent-a", "openapi"], ["agent-a", "bridge"]]);
  assert.equal(s.calls.some(call => call.method !== "GET"), false);
  assert.equal(s.prompts.length, 0); assert.equal(find(s.document.body, "dialog").length, 0);
});

test("setup links remain text and unrecognized download paths never become a request", async () => {
  const connection = { ...connectionFixture(), openapi_download_path: "https://outside.invalid/export", bridge_url: "javascript:alert(1)" };
  const s = setup({ cards: [proposal("edit_agent", {}, "agent-a")], respond: path => path === "/api/v4/agents/agent-a" ? { agent: agents[0], connection } : undefined });
  await s.ui.refresh(); await click(button(s.ui.element, "填写"));
  assert.equal(button(s.ui.element, "下载 OpenAPI 定义"), undefined);
  assert.equal(find(s.ui.element, "a").length, 0);
  const bridge = button(s.ui.element, "下载 Python Bridge"); await click(bridge);
  assert.deepEqual(s.setupDownloads, [["agent-a", "bridge"]]);
  assert.equal(s.calls.some(call => /outside.invalid|javascript:/.test(call.path)), false);
  s.state.cards = []; await s.ui.refresh(); await click(bridge); assert.equal(s.setupDownloads.length, 1);
});

test("task forms preserve forward dependencies and never treat creation as approval", async () => {
  const s = setup({ cards: [proposal("create_agent_task", { title: "两步计划", steps: [{ id: "summary", agent_id: "agent-b", prompt: "汇总", acceptance: "有出处", depends_on: ["research"], required: true }, { id: "research", agent_id: "agent-a", prompt: "研究", acceptance: "有来源", depends_on: [], required: true }] })] });
  await s.ui.refresh(); await click(button(s.ui.element, "填写")); await submit(find(s.ui.element, "form")[0]);
  const body = s.calls.find(item => item.path === "/api/v4/tasks").json;
  assert.deepEqual(body.steps.map(step => step.depends_on), [["research"], []]); assert.equal(body.client_request_id, "assistant:ac_example");
  assert.equal(s.calls.filter(item => item.path.endsWith("/approve")).length, 0);
});

test("failed refresh disables the previous view until server state is verified again", async () => {
  let fail = false;
  const s = setup({ cards: [planCard()], respond: path => path === "/api/assistant/cards" && fail ? Promise.reject(new Error("offline")) : undefined });
  await s.ui.refresh(); const old = button(s.ui.element, "确认并派发");
  fail = true; await s.ui.refresh(); await click(old); assert.equal(s.calls.filter(item => item.method === "POST").length, 0);
  fail = false; await s.ui.refresh(); assert.notEqual(button(s.ui.element, "确认并派发"), old);
});
