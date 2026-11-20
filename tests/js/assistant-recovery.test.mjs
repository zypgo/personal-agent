import assert from "node:assert/strict";
import { test } from "node:test";
import { FakeElement, elements, find } from "./fake_dom.mjs";
import { createAssistantCards } from "../../src/personal_agent/channels/web/static/assistant.js";

class Element extends FakeElement {
  constructor(tag) { super(tag); this.value = ""; this.disabled = false; this.checked = false; this.dataset = {}; }
  append(...nodes) { for (const node of nodes) node?.remove?.(); super.append(...nodes); for (const child of this.children) child.parentElement = this; }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  insertBefore(node, reference) { node.remove?.(); const index = this.children.indexOf(reference); this.children.splice(index < 0 ? this.children.length : index, 0, node); node.parentElement = this; }
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(item => item !== this); this.parentElement = null; }
  reportValidity() { return true; }
}
const clone = value => JSON.parse(JSON.stringify(value));
const click = control => control.listeners.get("click")?.({ preventDefault() {} });
const button = (ui, text) => find(ui.element, "button").find(item => item.textContent === text);
const consent = ui => find(ui.element, "input").find(item => item.type === "checkbox");
const failure = () => Object.assign(new Error("connection lost"), { status: 0 });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const plan = () => ({ id: "plan-a", kind: "agent_plan", title: "整理研究", status: "planned", data: { task: { id: "task-a", revision: 1, plan_hash: "a".repeat(64), state: "planned", visibility: "private", steps: [] } }, actions: [{ id: "approve" }, { id: "cancel" }] });
const proposal = () => ({ id: "ac-a", kind: "workspace_action", title: "创建项目", status: "proposed", data: { action: "create_space", target_id: "", draft: { name: "读书" } }, actions: [{ id: "open" }, { id: "dismiss" }] });
function setup(card = plan()) {
  const state = { cards: [card], read: null, write: null }, calls = [];
  const document = { createElement: tag => new Element(tag), hidden: false };
  const ui = createAssistantCards({ document, pollMs: 0, api: async (path, options = {}) => {
    calls.push({ path, options });
    if (path === "/api/assistant/cards") return state.read ? await state.read() : { cards: clone(state.cards) };
    if (path === "/api/v3/members") return { members: [] };
    if (path === "/api/v3/me") return { principal: { id: "me" }, role: "owner", space_id: "space-a", spaces: [] };
    if (options.method === "POST" && state.write) return await state.write(path, options);
    return {};
  } });
  const writes = () => calls.filter(call => call.options.method === "POST");
  return { ui, state, calls, writes };
}

test("a failed native decision becomes reviewable after a fresh authorized read without resending it", async () => {
  const s = setup(); s.state.write = () => { throw failure(); };
  await s.ui.refresh(); consent(s.ui).checked = true;
  const old = button(s.ui, "确认并派发"); await click(old);
  assert.equal(old.disabled, true);
  await s.ui.refresh();
  const refreshed = button(s.ui, "确认并派发");
  assert.equal(refreshed.disabled, false);
  assert.equal(consent(s.ui).checked, false, "the newly read plan requires fresh consent");
  assert.equal(s.writes().length, 1, "a read never repeats a decision");
  await click(old); await click(refreshed);
  assert.equal(s.writes().length, 1, "old handlers and unchecked decisions remain inert");
  s.state.write = () => ({}); consent(s.ui).checked = true; await click(refreshed);
  assert.equal(s.writes().length, 2);
});

test("refresh failure and recovery cannot unlock a native decision that is still in flight", async () => {
  const pending = deferred(), s = setup(); s.state.write = () => pending.promise;
  await s.ui.refresh(); consent(s.ui).checked = true;
  const deciding = click(button(s.ui, "确认并派发"));
  s.state.read = () => { throw failure(); }; await s.ui.refresh();
  s.state.read = null; await s.ui.refresh();
  const refreshed = button(s.ui, "确认并派发");
  assert.equal(refreshed.disabled, true);
  consent(s.ui).checked = true; await click(refreshed);
  assert.equal(s.writes().length, 1);
  pending.resolve({}); await deciding;
});

test("a read started before the failed decision does not unlock it when that old read arrives late", async () => {
  const pending = deferred(), read = deferred(), s = setup(); s.state.write = () => pending.promise;
  await s.ui.refresh(); consent(s.ui).checked = true;
  const deciding = click(button(s.ui, "确认并派发"));
  s.state.read = () => read.promise; const refreshing = s.ui.refresh();
  pending.reject(failure()); await deciding;
  read.resolve({ cards: clone(s.state.cards) }); await refreshing;
  assert.equal(button(s.ui, "确认并派发").disabled, true);
  s.state.read = null; await s.ui.refresh();
  assert.equal(button(s.ui, "确认并派发").disabled, false);
});

test("fresh reads cannot restore an action whose server permission was removed", async () => {
  const s = setup(); s.state.write = () => { throw failure(); };
  await s.ui.refresh(); consent(s.ui).checked = true; await click(button(s.ui, "确认并派发"));
  s.state.cards[0].actions = []; await s.ui.refresh();
  assert.equal(button(s.ui, "确认并派发"), undefined);
  assert.equal(s.writes().length, 1);
});

test("a failed dismiss can be reconsidered after refresh but never retries on its own", async () => {
  const s = setup(proposal()); s.state.write = () => { throw failure(); };
  await s.ui.refresh(); await click(button(s.ui, "暂不处理"));
  await s.ui.refresh();
  assert.equal(button(s.ui, "暂不处理").disabled, false);
  assert.equal(s.writes().length, 1);
});

test("refresh does not reopen a workspace execution whose begin response is uncertain", async () => {
  const s = setup(proposal()); s.state.write = () => { throw failure(); };
  await s.ui.refresh(); await click(button(s.ui, "核对并填写"));
  const form = find(s.ui.element, "form")[0];
  await form.listeners.get("submit")({ preventDefault() {} });
  await s.ui.refresh();
  assert.equal(button(s.ui, "核对并填写").disabled, true);
  assert.equal(s.writes().length, 1);
  assert.ok(elements(s.ui.element).every(item => !item.name || item.disabled));
});
