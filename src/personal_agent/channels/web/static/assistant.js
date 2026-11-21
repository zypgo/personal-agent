// Cards in the chat. They show state kept by the server. The model can only propose a named
// action; the buttons and forms here are what carry it out.
import { buildTaskRequest, buildStepRequest, buildAnswerRequest } from "./query.js";
import { AGENT_KINDS, statusLabel, wakeDescription, createDescription, secretReferences, currentAttempt, artifactPath } from "./orchestration.js";
import { createAgentMark, createProductIcon } from "./product-shell.js";

const CLOSED = new Set(["completed", "dismissed", "stale", "executing", "uncertain", "succeeded", "failed", "expired", "revoked", "cancelled", "cancel_requested", "cancelling", "accepted", "rejected", "stopped"]);
const ROLES = [["member", "成员 · 交办任务、编辑笔记"], ["viewer", "访客 · 查看项目内容"], ["owner", "负责人 · 管理项目与成员"]];
const txt = value => typeof value === "string" ? value : "";
const lines = value => Array.isArray(value) ? value.join("\n") : txt(value);
const splitLines = value => txt(value).split(/\r?\n/).map(item => item.trim()).filter(Boolean);
const identifier = value => {
  if ((typeof value !== "string" && typeof value !== "number") || !String(value)) throw new Error("操作对象已失效，请刷新待办。");
  return encodeURIComponent(String(value));
};
const post = (path, json, method = "POST") => ({ path, method, json });
const can = (card, action) => !CLOSED.has(card.status) && (card.actions || []).some(item => item.id === action);
function cardSignature(card) {
  if (!["execution_question", "agent_question", "agent_result"].includes(card.kind)) return JSON.stringify(card);
  const data = card.data || {}, task = data.task || {};
  // Progress in an unrelated parallel step must not erase an answer being typed.
  // The operation's own version, state, permissions and frozen content still invalidate it.
  return JSON.stringify({ ...card, data: { ...data, task: { id: task.id, title: task.title, revision: task.revision, plan_hash: task.plan_hash } } });
}

/** No URL, verb, or extra request field supplied by a model is interpreted here. */
export function workspaceRequest(action, target, payload, routing = {}) {
  switch (action) {
    case "create_space": return post("/api/v3/spaces", payload);
    case "invite_member": return post("/api/v3/invites", payload);
    case "accept_project_invite": return post("/api/v3/enroll", payload);
    case "change_member": return post("/api/v3/members", payload);
    case "pair_device": return post("/api/v3/devices/pair", payload);
    case "revoke_device": return post(`/api/v3/devices/${identifier(target)}/revoke`, payload);
    case "create_recovery_code": return post("/api/v3/recovery-code", payload);
    case "bind_channel": return post("/api/v3/channel-binding", payload);
    case "revoke_channel": return post("/api/v3/channel-bindings/revoke", payload);
    case "invite_node": return post("/api/v3/node-invites", payload);
    case "revoke_node": return post(`/api/v3/nodes/${identifier(target)}/revoke`, payload);
    case "write_knowledge": return post("/api/v3/knowledge", payload);
    case "create_execution_task": return post("/api/v3/tasks", payload);
    case "revise_execution_task": return post(`/api/v3/tasks/${identifier(target)}/revise`, payload);
    case "add_execution_step": return post(`/api/v3/tasks/${identifier(target)}/steps`, payload);
    case "cancel_execution_task": return post(`/api/v3/tasks/${identifier(target)}/cancel`, payload);
    case "create_agent": return post("/api/v4/agents", payload);
    case "edit_agent": return post(`/api/v4/agents/${identifier(target)}`, payload, "PATCH");
    case "rotate_agent_token": return post(`/api/v4/agents/${identifier(target)}/token`, payload);
    case "revoke_agent": return post(`/api/v4/agents/${identifier(target)}/revoke`, payload);
    case "create_agent_task": return post("/api/v4/tasks", payload);
    case "cancel_agent_task": return post(`/api/v4/tasks/${identifier(target)}/cancel`, payload);
    case "confirm_agent_stopped": return post(`/api/v4/tasks/${identifier(target)}/stopped`, payload);
    case "confirm_wake_stopped": return post(`/api/v4/tasks/${identifier(target)}/wakes/${identifier(routing.wake_id)}/stopped`, { note: payload.note });
    default: throw new Error("这项操作暂不支持，请说明你要完成的事情。");
  }
}

/** Native approvals retain the precise revision/hash/attempt the person actually reviewed. */
export function nativeCardRequest(card, action, input = {}) {
  if (!can(card, action)) throw new Error("这条待办已变化，请刷新后查看。");
  const data = card.data || {}, task = data.task || {};
  if (card.kind === "execution_approval" && ["approve", "deny"].includes(action)) {
    if (!data.space_id || !data.job_id || !data.plan_hash) throw new Error("执行计划不完整，请刷新待办。");
    return post("/api/v3/review-jobs", { space_id: data.space_id, job_id: data.job_id, plan_hash: data.plan_hash, allowed: action === "approve" });
  }
  const base = `/api/${card.kind === "execution_question" ? "v3" : "v4"}/tasks/${identifier(task.id)}`;
  if (card.kind === "agent_plan") {
    if (action === "cancel") return post(`${base}/cancel`, {});
    if (action === "approve" && task.plan_hash && Number.isInteger(task.revision)) return post(`${base}/approve`, { revision: task.revision, plan_hash: task.plan_hash });
  }
  if (card.kind === "agent_result" && ["accept", "reject"].includes(action)) {
    const step = data.step || {}, attempt = data.attempt || currentAttempt(step) || {};
    if (!step.id || !attempt.id || !task.plan_hash || !Number.isInteger(task.revision)) throw new Error("成果信息已变化，请刷新待办。");
    const body = { step_id: step.id, attempt_id: attempt.id, revision: task.revision, plan_hash: task.plan_hash };
    if (action === "reject") {
      if (!txt(input.reason).trim()) throw new Error("请说明哪些验收要求尚未满足。");
      body.reason = input.reason.trim();
    }
    return post(`${base}/${action}`, body);
  }
  if (["execution_question", "agent_question"].includes(card.kind) && action === "answer") {
    const question = data.question || {};
    if (!txt(input.answer).trim()) throw new Error("请填写回答。");
    const answer = question.choices?.length ? input.answer : input.answer.trim();
    if (question.choices?.length && !question.choices.includes(answer)) throw new Error("请选择当前问题提供的选项。");
    const revision = data.revision ?? task.revision;
    if (card.kind === "execution_question" && !Number.isInteger(revision)) throw new Error("问题版本已变化，请刷新待办。");
    return post(`${base}/questions/${identifier(question.id)}/answer`, card.kind === "execution_question" ? buildAnswerRequest(answer, revision) : { answer });
  }
  throw new Error("这条待办暂不支持此操作。");
}

function makeNode(doc, tag, text = "", className = "") {
  const node = doc.createElement(tag);
  if (text) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

/** A greeting that follows the clock, so the first sentence of the day sounds like a person. */
export function greeting(hour) {
  if (!Number.isInteger(hour) || hour < 0 || hour > 23) return "你好";
  if (hour < 5) return "夜深了";
  if (hour < 11) return "早上好";
  if (hour < 13) return "中午好";
  if (hour < 18) return "下午好";
  return "晚上好";
}

export function createAssistantWelcome({ document: doc, onPrompt, now = () => new Date() }) {
  const welcome = makeNode(doc, "section", "", "assistant-welcome");
  const scene = makeNode(doc, "div", "", "assistant-scene");
  scene.setAttribute("aria-hidden", "true");
  scene.append(createAgentMark(doc, "", "idle"), makeNode(doc, "span", "", "assistant-orbit"), makeNode(doc, "span", "", "assistant-spark"));
  const title = makeNode(doc, "h2");
  title.append(makeNode(doc, "span", "想让我做点什么，"), makeNode(doc, "span", "直接说就行。", "assistant-title-finish"));
  welcome.append(scene, makeNode(doc, "p", greeting(now().getHours()), "assistant-eyebrow"), title, makeNode(doc, "p", "可以是一个想法、一篇笔记，或者一件还没办的事。", "assistant-muted"));
  const prompts = makeNode(doc, "div", "", "assistant-shortcuts");
  for (const [kind, label, detail, prompt, symbol] of [
    ["organize", "看看待办", "有哪些事等你处理", "帮我看看现在有哪些需要我处理的事情。", "tasks"],
    ["remind", "设个提醒", "到点给你发消息", "帮我安排一个提醒：", "remind"],
    ["collect", "记笔记", "把零散的想法记下来", "帮我记一篇笔记：", "collect"],
    ["begin", "交办任务", "先拟计划，你确认后再做", "我想交办一项任务，请先帮我拟好计划：", "begin"],
  ]) {
    const button = makeNode(doc, "button", "", "assistant-shortcut");
    button.type = "button";
    button.dataset.kind = kind;
    const motif = makeNode(doc, "span", "", "assistant-shortcut-icon");
    motif.setAttribute("aria-hidden", "true");
    motif.append(createProductIcon(doc, symbol));
    const copy = makeNode(doc, "span", "", "assistant-shortcut-copy");
    copy.append(makeNode(doc, "strong", label), makeNode(doc, "span", detail));
    button.append(motif, copy);
    button.addEventListener("click", () => onPrompt(prompt));
    prompts.append(button);
  }
  welcome.append(prompts);
  return welcome;
}

export function createAssistantCards({ document: doc = globalThis.document, api, onChange = () => {}, onNotice = () => {}, onPrompt = () => {}, onContextChanged = () => {}, downloadArtifact, downloadConnection, pollMs = 12000 }) {
  const node = (tag, text = "", className = "") => makeNode(doc, tag, text, className);
  const root = node("section", "", "assistant-inbox");
  root.setAttribute("id", "assistant-inbox");
  root.setAttribute("aria-label", "助理待办与任务");
  const heading = node("h2", "接下来要处理的事"), status = node("p", "正在查看待办…", "assistant-hint");
  status.setAttribute("role", "status");
  const list = node("div", "", "assistant-card-list"), refreshButton = button("刷新", () => refresh());
  const header = node("div", "", "assistant-inbox-heading");
  header.append(heading, refreshButton); root.append(header, status, list);
  const entries = new Map(), suppressed = new Set(), dialogs = new Set();
  const inFlight = new Set(), recheckAfter = new Map();
  let alive = true, reading = false, again = false, me = null, memberNames = null, principalId = "", timer = null, serial = 0;

  function button(label, callback, className = "") {
    const value = node("button", label, `assistant-button ${className}`.trim());
    value.type = "button";
    if (callback) value.addEventListener("click", callback);
    return value;
  }
  const message = (entry, text, error = false) => { entry.message.textContent = text; entry.message.className = `assistant-hint${error ? " assistant-error" : ""}`; };
  const current = entry => alive && entry.active && entries.get(entry.card.id) === entry;
  function lock(entry) {
    entry.busy = true;
    entry.controls.forEach(control => { control.disabled = true; });
    entry.node.setAttribute("aria-busy", "true");
  }
  const register = (entry, control) => { entry.controls.push(control); return control; };
  function input(entry, name, label, value = "", options = {}) {
    const control = node(options.multiline ? "textarea" : options.options ? "select" : "input");
    control.name = name;
    if (options.options) for (const [key, title] of options.options) { const option = node("option", title); option.value = key; control.append(option); }
    else {
      if (!options.multiline) control.type = options.type || "text"; // a textarea's type is read-only
      control.maxLength = options.maxLength ?? 16000; control.autocomplete = "off";
    }
    control.value = String(value ?? ""); control.required = Boolean(options.required);
    if (options.multiline) control.rows = options.rows || 3;
    if (options.min != null) control.min = options.min;
    if (options.max != null) control.max = options.max;
    if (options.placeholder) control.placeholder = options.placeholder;
    const wrapper = node("label", label, "assistant-field"); wrapper.append(control);
    register(entry, control);
    return { element: wrapper, control, read: () => control.value };
  }
  function check(entry, label, checked = false) {
    const control = register(entry, node("input")); control.type = "checkbox"; control.checked = Boolean(checked);
    const element = node("label", "", "assistant-check"); element.append(control, node("span", label));
    return { element, control };
  }
  function disclosure(label, ...content) {
    const details = node("details", "", "assistant-disclosure"); details.append(node("summary", label), ...content); return details;
  }

  function disableEntry(entry) {
    entry.active = false; entry.controls.forEach(control => { control.disabled = true; });
    entry.privateControls.forEach(control => { control.value = ""; });
  }
  function requireFreshReview(entry, text) {
    // A read already in flight may describe the state before this failed request. Only a
    // later successful read may recreate the card, with fresh state and permissions.
    recheckAfter.set(entry.card.id, serial);
    message(entries.get(entry.card.id) || entry, text, true);
  }
  async function refresh() {
    if (!alive) return;
    if (reading) { again = true; return; }
    reading = true; const turn = ++serial; refreshButton.disabled = true;
    try {
      const response = await api("/api/assistant/cards");
      if (!alive || turn !== serial) return;
      const cards = (Array.isArray(response.cards) ? response.cards : []).filter(card => card && typeof card.id === "string" && !suppressed.has(card.id));
      principalId = response.principal?.id || principalId;
      if (memberNames === null && cards.some(card => ["agent_plan", "agent_result"].includes(card.kind) || card.data?.execution_kind === "agent")) {
        try {
          // Resolve only names visible in this same space, never the private review space.
          const members = await api("/api/v3/members");
          memberNames = new Map((members.members || []).map(member => [member.principal_id || member.id, member.display_name]));
        } catch { memberNames = new Map(); }
        if (!alive || turn !== serial) return;
      }
      const wanted = new Set(cards.map(card => card.id));
      for (const [id, entry] of entries) if (!wanted.has(id)) { disableEntry(entry); entries.delete(id); }
      const nodes = [];
      for (const card of cards) {
        const signature = cardSignature(card); let entry = entries.get(card.id);
        const reviewedAgain = recheckAfter.has(card.id) && turn > recheckAfter.get(card.id) && !inFlight.has(card.id);
        // Keep a form only while the server still advertises exactly the same operation.
        if (!entry || entry.signature !== signature || reviewedAgain) {
          if (entry) disableEntry(entry);
          entry = { card, signature, active: true, busy: false, controls: [], privateControls: [], node: node("article", "", "assistant-card"), message: node("p", "", "assistant-hint") };
          entries.set(card.id, entry); renderCard(entry);
          if (reviewedAgain) recheckAfter.delete(card.id);
          // Refresh recovery or a new signature cannot unlock a request still being sent.
          if (inFlight.has(card.id) || recheckAfter.has(card.id)) lock(entry);
        }
        nodes.push(entry.node);
      }
      // Replacing even identical nodes blurs a focused field in real browsers. Insert only
      // changed positions, so polling preserves typing, selection and open disclosures.
      for (const [index, element] of nodes.entries()) {
        const at = list.children[index];
        if (at !== element) list.insertBefore(element, at || null);
      }
      const kept = new Set(nodes);
      for (const element of [...list.children]) if (!kept.has(element)) element.remove();
      const count = cards.filter(card => (card.actions || []).length && !CLOSED.has(card.status)).length;
      heading.textContent = count ? `${count} 件事等你处理` : "任务与进展";
      root.dataset.empty = String(cards.length === 0);
      status.textContent = cards.length ? "" : "暂无待处理的事";
      onChange(count, { total: cards.length });
    } catch (error) {
      if (!alive || error?.name === "AbortError") return;
      root.dataset.empty = "false";
      status.textContent = "待办暂时无法更新，请稍后刷新。";
      // Keep the content to review, but never allow an action on an unverified stale view.
      for (const entry of entries.values()) { disableEntry(entry); entry.signature = ""; }
      onChange(0);
    } finally {
      reading = false;
      if (alive) refreshButton.disabled = false;
      if (again && alive) { again = false; void refresh(); }
    }
  }

  async function nativeAction(entry, action, values = {}) {
    if (!current(entry) || entry.busy || inFlight.has(entry.card.id) || recheckAfter.has(entry.card.id)) return;
    let request;
    try { request = nativeCardRequest(entry.card, action, values); }
    catch (error) { message(entry, error.message, true); return; }
    lock(entry); inFlight.add(entry.card.id); message(entry, "正在提交…");
    try {
      await api(request.path, { method: request.method, json: request.json });
      if (!alive) return;
      suppressed.add(entry.card.id);
      message(entry, "已处理。"); onNotice("已处理，正在更新任务状态。"); await refresh();
    } catch (error) {
      if (!alive || error?.name === "AbortError") return;
      requireFreshReview(entry, error.status >= 400 && error.status < 500 ? "这条待办已经变化，请刷新后再试。" : "没收到结果，请刷新看看。");
    } finally { inFlight.delete(entry.card.id); entry.node.setAttribute("aria-busy", "false"); }
  }

  function actionButton(entry, action, label, options = {}) {
    if (!can(entry.card, action)) return null;
    return register(entry, button(label, () => nativeAction(entry, action), options.className || ""));
  }

  function renderSteps(parent, task, { open = false } = {}) {
    for (const [index, step] of (task.steps || []).entries()) {
      const section = node("section", "", "assistant-step");
      section.append(node("h4", `${index + 1}. ${step.agent_name || AGENT_KINDS[step.agent_kind] || "已连接的助手"}`));
      const owner = memberNames?.get(step.agent_owner_id) || (step.agent_owner_id === principalId ? "我" : "连接所有者");
      if (step.agent_kind || step.agent_owner_id) section.append(node("p", `${AGENT_KINDS[step.agent_kind] || "助手"}${step.agent_owner_id ? ` · 所有者：${owner}` : ""}`, "assistant-hint"));
      if (step.state) section.append(node("p", statusLabel(step.state), "assistant-hint"));
      if (step.depends_on?.length) section.append(node("p", `等待${step.depends_on.map(id => { const n = (task.steps || []).findIndex(item => item.id === id); return n < 0 ? "前序步骤" : `第 ${n + 1} 步`; }).join("、")}验收通过后继续。`, "assistant-hint"));
      const details = disclosure("交办内容与验收标准", node("p", step.prompt || "", "assistant-prewrap"), node("h5", "验收标准"), node("p", step.acceptance || "", "assistant-prewrap")); details.open = open;
      section.append(details); parent.append(section);
    }
  }

  function renderCard(entry) {
    const { card } = entry, data = card.data || {}, task = data.task || {};
    entry.node.dataset.kind = card.kind;
    entry.node.append(node("p", ({ execution_approval: "等待执行授权", agent_plan: "等待确认计划", agent_result: "等待验收成果", execution_question: "需要你补充", agent_question: "需要你补充", workspace_action: "助理拟好的操作", task_status: "任务进展" })[card.kind] || "待办", "assistant-eyebrow"), node("h3", card.title || task.title || "待处理事项"));
    if (card.summary && !["execution_question", "agent_question", "task_status"].includes(card.kind)) entry.node.append(node("p", card.summary, "assistant-prewrap"));
    if (card.kind === "workspace_action") {
      if (["executing", "uncertain"].includes(card.status) && !card.summary) entry.node.append(node("p", "这项操作已经提交，结果还没确认。", "assistant-impact"));
      if (can(card, "open")) {
        const open = register(entry, button("填写", () => openWorkspaceForm(entry), "primary")); entry.node.append(open);
        const cancel = register(entry, button("暂不处理", async () => {
          if (!current(entry) || entry.busy || inFlight.has(card.id) || recheckAfter.has(card.id)) return;
          lock(entry); inFlight.add(card.id);
          try { await api(`/api/assistant/cards/${identifier(card.id)}/dismiss`, { method: "POST", json: {} }); if (alive) { suppressed.add(card.id); await refresh(); } }
          catch (error) { if (alive && error?.name !== "AbortError") requireFreshReview(entry, "没收起来，请刷新看看。"); }
          finally { inFlight.delete(card.id); entry.node.setAttribute("aria-busy", "false"); }
        })); entry.node.append(cancel);
      }
    } else if (card.kind === "execution_approval") {
      entry.node.append(node("h4", "将执行的内容"), node("pre", data.command || "", "assistant-command"));
      const paths = [...(data.input_manifest || []).map(item => `读取：${item.path}`), ...(data.output_paths || []).map(path => `保留：${path}`)];
      if (paths.length) entry.node.append(node("p", paths.join("\n"), "assistant-prewrap"));
      if (data.timeout_s) entry.node.append(node("p", `最长执行 ${data.timeout_s} 秒。`, "assistant-hint"));
      appendActions(entry, [actionButton(entry, "approve", "允许执行", { className: "primary" }), actionButton(entry, "deny", "不允许")]);
    } else if (card.kind === "agent_plan") {
      renderSteps(entry.node, task, { open: true });
      appendActions(entry, [actionButton(entry, "approve", "确认并派发", { className: "primary" }), actionButton(entry, "cancel", "取消这项计划")]);
    } else if (card.kind === "agent_result") {
      const step = data.step || {}, attempt = data.attempt || currentAttempt(step) || {}, result = step.result || attempt.result || {};
      entry.node.append(node("h4", "验收标准"), node("p", step.acceptance || "未写验收标准。", "assistant-prewrap"), node("h4", "提交的成果"), node("p", typeof result === "string" ? result : result.text || result.summary || "已提交成果文件。", "assistant-prewrap"));
      if (result.outcome === "failed") entry.node.append(node("p", "执行端报告没有完成。", "assistant-impact"));
      renderArtifacts(entry.node, result.artifacts || []);
      appendActions(entry, [actionButton(entry, "accept", "验收通过", { className: "primary" })]);
      if (can(card, "reject")) {
        const form = node("form", "", "assistant-form"), reason = input(entry, "reason", "未满足的要求", "", { multiline: true, required: true, maxLength: 2000 });
        const submit = register(entry, button("记录未通过", null, "danger")); submit.type = "submit";
        form.append(reason.element, submit); form.addEventListener("submit", event => { event.preventDefault(); return nativeAction(entry, "reject", { reason: reason.read() }); });
        entry.node.append(disclosure("成果需要调整", form));
      }
    } else if (["execution_question", "agent_question"].includes(card.kind)) {
      const question = data.question || {};
      entry.node.append(node("p", question.text || question.question || "", "assistant-prewrap"));
      if (can(card, "answer")) {
        const form = node("form", "", "assistant-form"), answer = input(entry, "answer", "你的回答", "", { required: true, multiline: !question.choices?.length, options: question.choices?.length ? [["", "请选择"], ...question.choices.map(choice => [choice, choice])] : null, maxLength: card.kind === "execution_question" ? 4000 : 8000 });
        const submit = register(entry, button("发送回答", null, "primary")); submit.type = "submit";
        form.append(answer.element, submit);
        form.addEventListener("submit", event => { event.preventDefault(); return nativeAction(entry, "answer", { answer: answer.read() }); }); entry.node.append(form);
      }
    } else if (card.kind === "task_status") {
      entry.node.append(node("p", statusLabel(task.state || card.status), "assistant-status"));
      const detail = disclosure("查看进展与成果");
      if (data.execution_kind === "agent") {
        renderSteps(detail, task);
        for (const [index, step] of (task.steps || []).entries()) {
          const attempt = currentAttempt(step), result = step.result || attempt?.result;
          const progress = attempt?.progress_text || step.progress_text;
          if (progress) detail.append(node("p", `第 ${index + 1} 步进展：${progress}`, "assistant-prewrap"));
          if (result) {
            detail.append(node("h4", `第 ${index + 1} 步成果`), node("p", typeof result === "string" ? result : result.text || result.summary || "已提交文件。", "assistant-prewrap"));
            renderArtifacts(detail, result.artifacts || []);
          }
        }
      } else for (const [index, job] of (task.jobs || []).entries()) {
        detail.append(node("h4", `第 ${index + 1} 步 · ${statusLabel(job.state)}`));
        if (job.plan?.command) detail.append(node("pre", job.plan.command, "assistant-command"));
        if (job.result?.stale_revision) detail.append(node("p", "这是之前版本的成果。", "assistant-hint"));
        for (const text of [job.result?.stdout, job.result?.stderr, job.result?.error]) if (text) detail.append(node("pre", String(text), "assistant-command"));
        renderArtifacts(detail, job.result?.artifacts || [], "execution");
      }
      if (detail.children.length > 1) entry.node.append(detail);
      if (task.state === "uncertain") entry.node.append(node("p", "不确定有没有执行成功。", "assistant-impact"));
    }
    entry.node.append(entry.message);
  }

  function appendActions(entry, controls) {
    const row = node("div", "", "assistant-actions"); controls.filter(Boolean).forEach(control => row.append(control));
    if (row.children.length) entry.node.append(row);
  }
  function renderArtifacts(parent, artifacts, kind = "agent") {
    for (const artifact of artifacts) {
      const id = typeof artifact === "string" ? artifact : artifact.id || artifact.artifact_id;
      const path = kind === "execution" ? /^[0-9a-f]{32}$/.test(id) ? `/api/artifacts/${id}` : null : artifactPath(artifact);
      const name = artifact.name || artifact.filename || "成果文件";
      if (!path || !downloadArtifact) { parent.append(node("p", `${name} · 文件暂不可下载`, "assistant-hint")); continue; }
      const control = button(`下载 ${name}`, async () => {
        if (!alive || control.disabled) return; control.disabled = true;
        try { await downloadArtifact(path, name); }
        catch { if (alive) onNotice("文件暂时无法下载，请稍后重试。", true); }
        finally { if (alive) control.disabled = false; }
      }); parent.append(control);
    }
  }

  // Workspace forms and the execution fence follow below.
  async function context() {
    if (!me) {
      const response = await api("/api/v3/me");
      if (alive) me = response;
    }
    if (!alive) throw new DOMException("页面已关闭", "AbortError");
    return me;
  }

  function connectionGuide(agentId, connection, { entry = null, available = () => true } = {}) {
    const section = disclosure("接入指引与文件"), controls = [];
    const valid = () => alive && available() && (!entry || current(entry));
    const remember = control => { controls.push(control); if (entry) register(entry, control); return control; };
    section.append(node("p", "在执行环境里选一种接入方式，把凭据存成 Bearer Token。", "assistant-hint"));
    for (const [key, label] of [["mcp_url", "MCP 服务器地址"], ["openapi_url", "HTTP 工具定义地址（OpenAPI）"], ["bridge_url", "Python 连接脚本地址"]]) {
      const address = connection?.[key];
      if (typeof address !== "string" || !address) continue;
      const value = remember(node("textarea")); value.readOnly = true; value.rows = 2; value.value = address; value.setAttribute("aria-label", label);
      const field = node("label", label, "assistant-field"); field.append(value);
      const feedback = node("p", "", "assistant-hint");
      const copy = remember(button("复制地址", async () => {
        if (!valid() || copy.disabled) return;
        try { await globalThis.navigator.clipboard.writeText(value.value); if (valid()) feedback.textContent = "地址已复制。"; }
        catch { if (valid()) { value.focus?.(); value.select?.(); feedback.textContent = "请选中地址后手动复制。"; } }
      })); section.append(field, copy, feedback);
    }
    const download = (kind, label) => {
      const control = remember(button(label, async () => {
        if (!valid() || control.disabled) return;
        control.disabled = true;
        try { await downloadConnection(agentId, kind); }
        catch { if (valid()) feedback.textContent = "文件暂时无法下载，请稍后重试。"; }
        finally { if (valid()) control.disabled = false; }
      }));
      const feedback = node("p", "", "assistant-hint"); section.append(control, feedback);
    };
    if (agentId && downloadConnection && connection?.openapi_download_path === `/api/v4/agents/${encodeURIComponent(agentId)}/openapi.json`) {
      download("openapi", "下载 OpenAPI 定义");
      section.append(node("p", "可导入支持 HTTP 工具的执行环境。", "assistant-hint"));
    }
    if (agentId && downloadConnection && typeof connection?.bridge_url === "string" && connection.bridge_url) {
      download("bridge", "下载 Python Bridge");
      section.append(node("p", "下载后运行 python bridge.py --help 查看用法。", "assistant-hint"));
    }
    if (typeof connection?.instructions === "string" && connection.instructions) section.append(disclosure("给执行助手的协议说明", node("pre", connection.instructions, "assistant-command")));
    if (!["mcp_url", "openapi_url", "bridge_url"].some(key => typeof connection?.[key] === "string" && connection[key])) section.append(node("p", "暂时没有接入信息，请稍后再打开。", "assistant-hint"));
    return { element: section, clear() { controls.forEach(control => { control.disabled = true; if ("value" in control) control.value = ""; }); section.replaceChildren(); } };
  }

  function showSecret(action, response) {
    const code = response.worker_token || response.code;
    if (!alive || typeof code !== "string" || !code) return;
    const dialog = node("dialog", "", "assistant-secret-dialog");
    const title = node("h2", response.worker_token ? "保存连接凭据" : "保存一次性代码");
    const value = node("textarea"); value.readOnly = true; value.autocomplete = "off"; value.rows = 4;
    value.setAttribute("aria-label", response.worker_token ? "连接凭据" : "一次性代码");
    value.value = action === "bind_channel" ? `/bind ${code}` : code;
    dialog.append(title, node("p", "只显示这一次，请先复制。", "assistant-hint"), value);
    const extra = [];
    for (const [label, address] of [["设备配对地址", response.url || response.pairing_url]]) {
      if (typeof address === "string" && address) { const field = node("textarea"); field.readOnly = true; field.value = address; field.rows = 2; field.setAttribute("aria-label", label); extra.push(field); dialog.append(node("p", label, "assistant-hint"), field); }
    }
    let visible = true, guide = null;
    if (response.worker_token && response.connection) {
      guide = connectionGuide(response.agent?.id || response.connection.agent_id, response.connection, { available: () => visible });
      dialog.append(guide.element);
    }
    const feedback = node("p", "", "assistant-hint");
    const copy = button("复制代码", async () => {
      try { await globalThis.navigator.clipboard.writeText(value.value); feedback.textContent = "已复制。"; }
      catch { feedback.textContent = "请选中代码后手动复制。"; value.focus?.(); value.select?.(); }
    });
    const clear = () => { visible = false; value.value = ""; extra.forEach(field => { field.value = ""; }); guide?.clear(); dialog.close?.(); dialog.remove(); dialogs.delete(dialog); };
    dialog.addEventListener("cancel", event => { event.preventDefault(); clear(); });
    dialog.addEventListener("close", () => { visible = false; value.value = ""; extra.forEach(field => { field.value = ""; }); guide?.clear(); dialog.remove(); dialogs.delete(dialog); });
    dialog.append(feedback, copy, button("已保存，关闭", clear));
    dialog.clearPrivate = clear; dialogs.add(dialog); doc.body.append(dialog); dialog.showModal();
  }

  async function executeWorkspace(entry, payload, privateFields = null) {
    if (!current(entry) || entry.busy || !can(entry.card, "open")) return;
    const base = `/api/assistant/cards/${identifier(entry.card.id)}`;
    lock(entry); message(entry, "正在提交…");
    let execution = null, received = false;
    try {
      execution = await api(`${base}/begin`, { method: "POST", json: { payload } });
      if (!alive) return;
      if (!execution.execution_id || execution.action !== entry.card.data.action || (execution.target_id || "") !== (entry.card.data.target_id || "")) throw new Error("操作确认已变化。");
      const request = workspaceRequest(execution.action, execution.target_id, execution.payload, execution.routing);
      if (execution.action === "accept_project_invite") {
        if (!privateFields?.code) throw new Error("请重新填写邀请码。");
        request.json = { code: privateFields.code, display_name: privateFields.display_name, require_existing_session: true };
      }
      const response = await api(request.path, { method: request.method, json: request.json });
      received = true;
      if (!alive) return;
      const changedOwnMembership = execution.action === "change_member" && execution.payload.principal_id === me?.principal?.id;
      // Taking an invitation lands them in a project: say which, and (below) go there.
      const project = execution.action === "accept_project_invite" && response?.space?.id ? response : null;
      const done = project ? (project.joined === false ? `你已经在「${project.space.name}」里了。` : `已加入「${project.space.name}」。`) : "操作已完成。";
      // The response may contain credentials. They are never included in complete or chat.
      showSecret(execution.action, response || {});
      me = null; memberNames = null;
      message(entry, done); onNotice(done);
      // The old proposal's membership snapshot is intentionally no longer authorized.
      // Do not issue a guaranteed forbidden complete/read from a space the user has left.
      if (changedOwnMembership) {
        suppressed.add(entry.card.id);
        if (execution.payload.revoke) { destroy(); onContextChanged({ leftSpace: true }); }
        else { onContextChanged({ roleChanged: true }); await refresh(); }
        return;
      }
      try { await api(`${base}/complete`, { method: "POST", json: { execution_id: execution.execution_id, outcome: "succeeded" } }); }
      catch { if (alive) onNotice("操作已经完成，待办列表还没更新，刷新一下就行。"); }
      if (alive) {
        suppressed.add(entry.card.id);
        onContextChanged(project ? { joinedSpace: project.space, role: project.role, joined: project.joined } : undefined);
        await refresh();
      }
    } catch (error) {
      if (!alive || error?.name === "AbortError") return;
      const definite = !received && error.status >= 400 && error.status < 500;
      if (execution?.execution_id) {
        try { await api(`${base}/complete`, { method: "POST", json: { execution_id: execution.execution_id, outcome: definite ? "failed" : "uncertain" } }); } catch { /* The persisted begin still prevents a second execution. */ }
      }
      message(entry, definite ? "没有完成，请刷新看看。要改的话，告诉助理重新准备。" : "没收到结果，请刷新看看。", true);
    } finally {
      if (privateFields) { privateFields.code = ""; privateFields.display_name = ""; }
      entry.privateControls.forEach(control => { control.value = ""; });
      entry.node.setAttribute("aria-busy", "false");
    }
  }

  async function openWorkspaceForm(entry) {
    if (!current(entry) || entry.busy || entry.opened) return;
    entry.opened = true; message(entry, "正在打开表单…");
    try {
      const form = await workspaceForm(entry);
      if (!current(entry)) return;
      entry.node.insertBefore(form, entry.message); message(entry, "");
    } catch (error) { if (current(entry)) { entry.opened = false; message(entry, error?.name === "AbortError" ? "" : "表单暂时打不开，请刷新后再试。", true); } }
  }

  async function workspaceForm(entry) {
    const { action, target_id: target, draft = {} } = entry.card.data;
    const ctx = await context(), form = node("form", "", "assistant-form");
    let read = () => ({}), readPrivate = () => null, label = "提交";
    const field = (name, title, value, options = {}) => { const item = input(entry, name, title, value, options); form.append(item.element); return item; };
    const note = text => form.append(node("p", text, "assistant-impact"));
    const required = { required: true };
    switch (action) {
      case "create_space": {
        const name = field("name", "项目名称", draft.name, { ...required, maxLength: 60 });
        read = () => ({ name: name.read().trim() }); label = "创建项目"; break;
      }
      case "invite_member": {
        const remark = field("label", "邀请备注", draft.label, { maxLength: 60 }), role = field("role", "项目角色", draft.role || "member", { options: ROLES });
        note("会生成一次性邀请码，发给对方就能加入。");
        read = () => ({ label: remark.read().trim(), role: role.read() }); label = "生成邀请"; break;
      }
      case "accept_project_invite": {
        const code = field("invite_code", "邀请代码或邀请链接", "", { ...required, type: "password", maxLength: 2000 }); entry.privateControls.push(code.control);
        readPrivate = () => {
          const raw = code.read().trim(), linked = /[#&?]invite=([A-Za-z0-9_-]{16,128})(?![A-Za-z0-9_-])/.exec(raw);
          const value = linked ? linked[1] : raw;
          if (!/^[A-Za-z0-9_-]{16,128}$/.test(value)) throw new Error("请填写完整的一次性邀请代码或链接。");
          code.control.value = "";
          return { code: value, display_name: ctx.principal?.display_name || "" };
        };
        label = "接受邀请"; break;
      }
      case "change_member": {
        const members = await api("/api/v3/members"), member = (members.members || []).find(item => (item.principal_id || item.id) === target);
        if (!member) throw new Error("成员已变化。");
        note(`成员：${member.display_name || "当前项目成员"}`);
        const mode = field("mode", "调整方式", draft.revoke ? "revoke" : "role", { options: [["role", "修改角色"], ["revoke", "移出当前项目"]] });
        const role = field("role", "新的项目角色", draft.role || member.role || "member", { options: ROLES });
        read = () => mode.read() === "revoke" ? { principal_id: target, revoke: true } : { principal_id: target, role: role.read() }; label = "保存"; break;
      }
      case "pair_device": {
        const remark = field("label", "设备名称", draft.label, { maxLength: 60 }); read = () => ({ label: remark.read().trim() }); label = "生成配对代码"; break;
      }
      case "create_recovery_code": note("会生成新的恢复码，旧的失效。"); label = "生成恢复码"; break;
      case "bind_channel": note("会生成绑定码，在要绑定的聊天里发给机器人。"); label = "生成绑定码"; break;
      case "revoke_channel": {
        const data = await api("/api/v3/channel-bindings"), bindings = data.bindings || data.channel_bindings || [];
        const binding = bindings.find(item => item.channel === draft.channel && item.account_id === draft.account_id && item.sender_id === draft.sender_id);
        if (!binding) throw new Error("绑定已变化。");
        note(`解除 ${binding.channel} 的绑定。`);
        read = () => ({ channel: binding.channel, account_id: binding.account_id, sender_id: binding.sender_id }); label = "解除绑定"; break;
      }
      case "invite_node": {
        const remark = field("label", "电脑名称", draft.label, { ...required, maxLength: 60 });
        const choices = [];
        for (const space of ctx.spaces || []) { const choice = check(entry, space.kind === "personal" ? "个人空间" : space.name || "当前空间", (draft.space_ids || [ctx.space_id]).includes(space.id)); form.append(choice.element); choices.push([space.id, choice.control]); }
        read = () => { const space_ids = choices.filter(([, control]) => control.checked).map(([id]) => id); if (!space_ids.length) throw new Error("请选择至少一个空间。"); return { label: remark.read().trim(), space_ids }; }; label = "生成电脑邀请"; break;
      }
      case "revoke_device": case "revoke_node": case "revoke_agent": case "rotate_agent_token": {
        const isAgent = ["revoke_agent", "rotate_agent_token"].includes(action);
        const response = await api(isAgent ? `/api/v4/agents/${identifier(target)}` : action === "revoke_device" ? "/api/v3/devices" : "/api/v3/nodes");
        const item = isAgent ? response.agent || response : (response[action === "revoke_device" ? "devices" : "nodes"] || []).find(value => String(value.id) === String(target));
        if (!item) throw new Error("对象已变化。");
        note(`${item.name || item.label || (isAgent ? "已连接的助手" : "已登录的设备")}`);
        note(action === "rotate_agent_token" ? "旧凭据会立即失效。" : "撤销后不能再使用。");
        label = action === "rotate_agent_token" ? "更换凭据" : "撤销"; break;
      }
      case "write_knowledge": {
        const path = field("path", "笔记名称", draft.path || target || "", { ...required, maxLength: 100, placeholder: "例如：项目笔记.md" });
        const body = field("text", "笔记内容", draft.text || "", { ...required, multiline: true, rows: 8, maxLength: 30000 });
        read = () => ({ path: path.read().trim(), text: body.read(), ...(draft.revision == null ? {} : { revision: draft.revision }) }); label = "保存笔记"; break;
      }
      case "create_execution_task": case "add_execution_step": {
        const title = action === "create_execution_task" ? field("title", "任务名称", draft.title, { ...required, maxLength: 200 }) : null;
        const command = field("command", "执行内容", draft.command, { ...required, multiline: true, rows: 4 });
        const inputs = field("input_paths", "读取的文件 · 每行一个路径", lines(draft.input_paths), { multiline: true }), outputs = field("output_paths", "保留的成果文件 · 每行一个路径", lines(draft.output_paths), { multiline: true });
        const timeout = field("timeout_s", "最长执行时间（秒）", draft.timeout_s || 900, { ...required, type: "number", min: 1, max: 3600 });
        note("提交后，要批准才会执行。");
        read = () => { const fields = { command: command.read(), input_paths: inputs.read(), output_paths: outputs.read(), timeout_s: timeout.read() }; return title ? buildTaskRequest({ ...fields, title: title.read() }, `assistant:${entry.card.id}`) : buildStepRequest(fields, draft.expected_revision, draft.step_id); }; label = title ? "提交待批准计划" : "补充执行步骤"; break;
      }
      case "revise_execution_task": {
        const title = field("title", "新的任务名称", draft.title, { ...required, maxLength: 200 }); note("修改后需要重新批准。"); read = () => ({ revision: draft.revision, title: title.read().trim() }); label = "保存修改"; break;
      }
      case "cancel_execution_task": case "cancel_agent_task": label = "取消任务"; break;
      case "confirm_agent_stopped": case "confirm_wake_stopped": {
        note("确认对方已经停止，释放占用。");
        read = () => action === "confirm_wake_stopped" ? { note: "已确认停止" } : { step_id: draft.step_id, attempt_id: draft.attempt_id, note: "已确认停止" }; label = "确认已停止"; break;
      }
      case "create_agent": case "edit_agent": read = await agentForm(entry, form, ctx, draft, target); label = action === "create_agent" ? "保存连接并生成凭据" : "保存连接修改"; break;
      case "create_agent_task": read = await agentTaskForm(entry, form, draft); label = "生成待确认计划"; break;
      default: throw new Error("未知操作。");
    }
    const submit = register(entry, button(label, null, "primary")); submit.type = "submit"; form.append(submit);
    form.addEventListener("submit", event => {
      event.preventDefault();
      if (!current(entry) || entry.busy) return;
      if (typeof form.reportValidity === "function" && !form.reportValidity()) return;
      try { const payload = read(); return executeWorkspace(entry, payload, readPrivate()); } catch (error) { message(entry, error.message, true); }
    });
    return form;
  }

  async function agentForm(entry, form, ctx, draft, target) {
    const editing = entry.card.data.action === "edit_agent";
    const existing = editing ? await api(`/api/v4/agents/${identifier(target)}`) : null;
    const previous = existing?.agent || null;
    if (editing && !previous) throw new Error("连接已变化。");
    if (editing) form.append(connectionGuide(target, existing.connection, { entry }).element);
    const initial = { ...(previous || {}), ...draft };
    const members = (await api("/api/v3/members")).members || [];
    const add = (name, label, value, options = {}) => { const field = input(entry, name, label, value, options); form.append(field.element); return field; };
    const name = add("name", "助手名称", initial.name || "", { required: true, maxLength: 100 });
    const kind = add("kind", "运行在哪里", initial.kind || "grok", { options: Object.entries(AGENT_KINDS) }); kind.control.disabled = editing;
    const wake = add("wake_mode", "如何领取任务", "manual", { options: [["manual", "手动领取"]] });
    const guidance = node("p", "", "assistant-hint"); form.append(guidance);
    const shared = check(entry, "使用助手原有的运行环境", initial.shared_environment); form.append(shared.element);
    const grants = [];
    form.append(node("h4", "谁可以交办任务"));
    for (const member of members) {
      const id = member.principal_id || member.id;
      if (id === ctx.principal?.id || member.role === "viewer") continue;
      const choice = check(entry, member.display_name || "项目成员", initial.allowed_actor_ids?.includes(id)); grants.push([id, choice.control]); form.append(choice.element);
    }
    const capabilities = add("capabilities", "可处理的内容 · 每行一项", lines(initial.capabilities), { multiline: true, maxLength: 4000 });
    let url = null, key = null, advanced = null;
    if (ctx.principal?.is_admin) {
      url = input(entry, "webhook_url_env", "Routine 网址的环境变量名", previous?.secret_env?.webhook_url || "", { maxLength: 128 });
      key = input(entry, "webhook_key_env", "认证密钥的环境变量名", previous?.secret_env?.webhook_key || "", { maxLength: 128 });
      advanced = disclosure("已配置 Grok Routine？", node("p", "填环境变量名，不是网址或密钥。", "assistant-hint"), url.element, key.element); form.append(advanced);
    }
    const updateMode = () => { guidance.textContent = createDescription({ kind: kind.read(), wake_mode: wake.read() }); if (url) { const required = kind.read() === "grok" && wake.read() === "grok_routine"; url.control.required = key.control.required = required; advanced.hidden = kind.read() !== "grok"; advanced.open = required; } };
    const updateKind = (first = false) => {
      const options = kind.read() === "muse" ? [["manual", "手动领取"], ["native_schedule", "使用 Muse 已有的原生定时任务"]] : kind.read() === "grok" && ctx.principal?.is_admin ? [["manual", "手动领取"], ["grok_routine", "使用已配置的 Grok Routine"]] : [["manual", "由执行端主动领取"]];
      wake.control.replaceChildren(...options.map(([value, label]) => { const option = node("option", label); option.value = value; return option; }));
      wake.control.value = first && options.some(([value]) => value === initial.wake_mode) ? initial.wake_mode : "manual";
      shared.control.checked = kind.read() === "grok" || (first ? Boolean(initial.shared_environment) : kind.read() === "muse"); shared.control.disabled = kind.read() === "grok"; updateMode();
    };
    kind.control.addEventListener("change", () => updateKind()); wake.control.addEventListener("change", updateMode); updateKind(true);
    form.append(node("p", editing ? "保存后，旧计划会失效。" : "保存后，还要在助手那边完成配置。", "assistant-impact"));
    return () => {
      const secret_env = url && kind.read() === "grok" ? secretReferences(url.read(), key.read()) : {};
      if (wake.read() === "grok_routine" && (!secret_env.webhook_url || !secret_env.webhook_key)) throw new Error("请填写已配置的环境变量名，或选择手动领取。");
      return { name: name.read().trim(), kind: kind.read(), wake_mode: wake.read(), allowed_actor_ids: grants.filter(([, control]) => control.checked).map(([id]) => id), capabilities: splitLines(capabilities.read()), shared_environment: kind.read() === "grok" || shared.control.checked, secret_env };
    };
  }

  async function agentTaskForm(entry, form, draft) {
    const agents = ((await api("/api/v4/agents")).agents || []).filter(agent => !agent.revoked_at && agent.state !== "revoked");
    if (!agents.length) {
      const prompt = register(entry, button("先连接一个助手", () => onPrompt("我想连接一个已有的助手，请准备连接表单。")));
      form.append(node("p", "当前空间还没有可用的助手。"), prompt);
      return () => { throw new Error("请先连接一个可用的助手，再准备任务。"); };
    }
    const add = (name, label, value, options = {}) => { const field = input(entry, name, label, value, options); form.append(field.element); return field; };
    const title = add("title", "任务名称", draft.title || "", { required: true, maxLength: 200 });
    const visibility = add("visibility", "谁可以查看", draft.visibility || "private", { options: [["private", "仅自己"], ["space", "当前项目成员"]] });
    const timeout = add("timeout_s", "任务期限（秒）", draft.timeout_s || 3600, { type: "number", min: 60, max: 604800, required: true });
    const steps = [], host = node("div", "", "assistant-plan-editor"); form.append(host);
    const updateDependencies = () => {
      for (const step of steps) {
        const selected = new Map(step.dependencies.map(([id, control]) => [id, control.checked]));
        step.dependencyHost.replaceChildren(); step.dependencies = [];
        for (const prior of steps) {
          if (prior === step) continue;
          const choice = check(entry, `等待第 ${prior.index + 1} 步验收后继续`, selected.has(prior.id) ? selected.get(prior.id) : step.seedDependencies.includes(prior.id));
          step.dependencies.push([prior.id, choice.control]); step.dependencyHost.append(choice.element);
        }
      }
    };
    const addStep = (seed = {}, update = true) => {
      if (steps.length >= 16) return;
      const index = steps.length; let generated = index + 1;
      while (steps.some(step => step.id === `s${generated}`)) generated += 1;
      const id = seed.id || `s${generated}`, section = node("fieldset", "", "assistant-step-editor"); section.append(node("legend", `第 ${index + 1} 步`));
      const agent = input(entry, `agent_${index}`, "交给谁", seed.agent_id || agents[0].id, { options: agents.map(item => [item.id, item.name || AGENT_KINDS[item.kind] || "已连接的助手"]), required: true });
      const prompt = input(entry, `prompt_${index}`, "实际发给此助手的内容", seed.prompt || "", { required: true, multiline: true, rows: 4, maxLength: 16000 });
      const acceptance = input(entry, `acceptance_${index}`, "验收标准", seed.acceptance || "", { required: true, multiline: true, maxLength: 4000 });
      section.append(agent.element, prompt.element, acceptance.element);
      const dependencyHost = node("div", "", "assistant-dependencies"); section.append(dependencyHost);
      const required = check(entry, "这是完成任务必须通过的步骤", seed.required !== false); section.append(required.element);
      const scope = node("p", "", "assistant-hint"); const updateScope = () => { const selected = agents.find(item => item.id === agent.read()); scope.textContent = selected ? wakeDescription(selected) : "请选择可用助手。"; }; agent.control.addEventListener("change", updateScope); updateScope(); section.append(scope);
      steps.push({ id, index, agent, prompt, acceptance, dependencies: [], dependencyHost, seedDependencies: seed.depends_on || [], required }); host.append(section);
      if (update) updateDependencies();
    };
    const seeds = draft.steps?.length ? draft.steps : [{}]; seeds.forEach(seed => addStep(seed, false)); updateDependencies();
    const more = register(entry, button("添加一步", () => { addStep(); more.disabled = steps.length >= 16; })); form.append(more);
    form.append(node("p", "提交后会生成一张待确认的计划。", "assistant-impact"));
    return () => {
      const proposed = steps.map(step => ({ id: step.id, agent_id: step.agent.read(), prompt: step.prompt.read().trim(), acceptance: step.acceptance.read().trim(), required: step.required.control.checked, depends_on: step.dependencies.filter(([, control]) => control.checked).map(([id]) => id) }));
      const ids = new Set(proposed.map(step => step.id));
      if (ids.size !== proposed.length || steps.some(step => step.seedDependencies.some(id => !ids.has(id)))) throw new Error("计划中的步骤依赖不完整，请让助理重新准备。");
      const done = new Set(), visiting = new Set();
      const visit = id => {
        if (visiting.has(id)) throw new Error("步骤不能互相等待，请调整前后顺序。");
        if (done.has(id)) return;
        visiting.add(id); proposed.find(step => step.id === id).depends_on.forEach(visit); visiting.delete(id); done.add(id);
      };
      proposed.forEach(step => visit(step.id));
      return { client_request_id: `assistant:${entry.card.id}`, title: title.read().trim(), visibility: visibility.read(), timeout_s: Number(timeout.read()), steps: proposed };
    };
  }

  if (pollMs > 0) timer = setInterval(() => { if (!doc.hidden) void refresh(); }, pollMs);
  function destroy() {
    alive = false; serial += 1; clearInterval(timer); entries.forEach(disableEntry); entries.clear(); suppressed.clear(); inFlight.clear(); recheckAfter.clear(); me = null; memberNames = null; principalId = "";
    for (const dialog of [...dialogs]) dialog.clearPrivate();
    list.replaceChildren(); onChange(0);
  }
  return {
    element: root, refresh,
    focus: () => root.scrollIntoView?.({ block: "start", behavior: globalThis.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ? "auto" : "smooth" }),
    destroy,
  };
}
