import assert from "node:assert/strict";
import { test } from "node:test";

import { Store } from "../../src/personal_agent/channels/web/static/store.js";

const turn = (id, extra = {}) => ({
  id,
  at: 1000 + id,
  direction: "out",
  channel: "web",
  kind: "message",
  text: `turn ${id}`,
  ...extra,
});
const approval = (id, status = "pending") => ({ id, tool: "shell", summary: "git push", status });

test("turns come back oldest first however they arrive", () => {
  const store = new Store();
  assert.deepEqual(
    store.addTurns([turn(5), turn(3), turn(4)]).map((t) => t.id),
    [3, 4, 5],
  );
  assert.deepEqual(store.order, [3, 4, 5]);
  assert.equal(store.firstId, 3);
  assert.equal(store.lastId, 5);
});

test("a turn already known is not new", () => {
  const store = new Store();
  store.addTurns([turn(1), turn(2)]);
  const again = store.addTurns([turn(2, { text: "changed" }), turn(3)]);
  assert.deepEqual(again.map((t) => t.id), [3]);
  assert.equal(store.turns.get(2).text, "turn 2"); // turns never change once written
  assert.deepEqual(store.addTurns([]), []);
});

test("an empty store has no ids", () => {
  const store = new Store();
  assert.equal(store.firstId, 0);
  assert.equal(store.lastId, 0);
  assert.deepEqual(store.timeline, []);
});

test("older history can be added after newer", () => {
  const store = new Store();
  store.addTurns([turn(10), turn(11)]);
  store.addTurns([turn(8), turn(9)]);
  assert.deepEqual(store.order, [8, 9, 10, 11]);
});

test("an approval carried by a turn fills a gap but never overrides newer word", () => {
  const store = new Store();
  store.addTurns([turn(1, { approvals: [approval(7)] })]);
  assert.equal(store.approvals.get(7).status, "pending");
  store.setApprovals([approval(7, "approved")]);
  store.addTurns([turn(2, { approvals: [approval(7, "pending")] })]); // an older snapshot
  assert.equal(store.approvals.get(7).status, "approved");
  assert.equal(store.approval(approval(7)).status, "approved");
  assert.equal(store.approval(approval(99)).id, 99); // unknown: the snapshot itself
});

test("what is waiting is in the order it was asked", () => {
  const store = new Store();
  store.setApprovals([approval(3), approval(1), approval(2, "denied")]);
  assert.deepEqual(store.waiting.map((a) => a.id), [1, 3]);
});

test("working carries its tool only while working", () => {
  const store = new Store();
  store.setWorking(true, "web_search");
  assert.deepEqual([store.working, store.tool], [true, "web_search"]);
  store.setWorking(false, "web_search");
  assert.deepEqual([store.working, store.tool], [false, null]);
});

// ---- messages on their way ---------------------------------------------------------------------
const send = (store, clientId, text, extra = {}) =>
  store.addPending({ clientId, text, at: 1, ...extra });

test("a message waits until the server confirms it, then becomes a turn", () => {
  const store = new Store();
  send(store, "c1", "hello");
  assert.deepEqual(store.timeline.map((e) => e.key), ["pc1"]);
  const fresh = store.settle("c1", turn(1, { direction: "in", text: "hello" }));
  assert.deepEqual(fresh.map((t) => t.id), [1]);
  assert.deepEqual(store.timeline.map((e) => e.key), ["t1"]);
});

test("pending messages stay after every turn, in the order they were typed", () => {
  const store = new Store();
  store.addTurns([turn(1)]);
  send(store, "a", "first");
  send(store, "b", "second");
  store.addTurns([turn(2)]);
  assert.deepEqual(store.timeline.map((e) => e.key), ["t1", "t2", "pa", "pb"]);
});

test("a failed message stays put until it is sent again or dropped", () => {
  const store = new Store();
  const entry = send(store, "c1", "hello");
  entry.state = "failed";
  assert.equal(store.findPending("c1"), entry);
  assert.equal(store.dropPending("c1"), true);
  assert.equal(store.dropPending("c1"), false);
});

test("when the stream delivers the message before the reply does, there is still only one", () => {
  const store = new Store();
  send(store, "c1", "hello");
  const incoming = turn(1, { direction: "in", text: "hello" });
  assert.equal(store.matchIncoming(incoming).clientId, "c1");
  assert.deepEqual(store.pending, []);
  assert.deepEqual(store.addTurns([incoming]).map((t) => t.id), [1]);
  assert.deepEqual(store.settle("c1", incoming), []); // the reply arrives late: nothing new
  assert.deepEqual(store.timeline.map((e) => e.key), ["t1"]);
});

test("a turn from somewhere else is never mistaken for a message waiting here", () => {
  const store = new Store();
  send(store, "c1", "hello");
  assert.equal(store.matchIncoming(turn(1, { direction: "in", channel: "telegram", text: "hello" })), null);
  assert.equal(store.matchIncoming(turn(2, { direction: "out", text: "hello" })), null);
  assert.equal(store.matchIncoming(turn(3, { direction: "in", text: "something else" })), null);
  assert.equal(store.pending.length, 1);
});

test("a message that has already failed is not matched by the stream", () => {
  const store = new Store();
  send(store, "c1", "hello").state = "failed";
  assert.equal(store.matchIncoming(turn(1, { direction: "in", text: "hello" })), null);
});

test("pictures and voice are matched by what they are, since their words are not known here", () => {
  const store = new Store();
  send(store, "pics", "", { files: [{}, {}] });
  send(store, "talk", "", { voice: { seconds: 3 } });
  const two = { attachments: [{ id: "a" }, { id: "b" }] };
  assert.equal(store.matchIncoming(turn(1, { direction: "in", text: "", meta: two })).clientId, "pics");
  assert.equal(
    store.matchIncoming(turn(2, { direction: "in", text: "what they said", meta: { voice: true } })).clientId,
    "talk",
  );
});

test("two identical messages are matched one at a time", () => {
  const store = new Store();
  send(store, "a", "again");
  send(store, "b", "again");
  assert.equal(store.matchIncoming(turn(1, { direction: "in", text: "again" })).clientId, "a");
  assert.deepEqual(store.pending.map((p) => p.clientId), ["b"]);
});
