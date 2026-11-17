// What the page knows: the conversation, what is being sent, and what is waiting for an answer.
//
// The server's database is the source of truth; this is a cache of it that the page can draw from.
// Turns never change once written, so a turn that is already here is simply skipped. What does
// change is the state of an approval, and that is kept apart from the turns that mention it.

export class Store {
  constructor() {
    this.turns = new Map(); // id -> turn
    this.order = []; // ids, oldest first
    this.pending = []; // messages typed here that the server has not confirmed yet
    this.approvals = new Map(); // id -> approval
    this.drafts = new Map(); // run_id -> bounded, temporary answer preview
    this.completedRuns = new Set(); // final replies suppress a delayed snapshot from an old stream
    this.more = false; // are there older turns on the server?
    this.working = false;
    this.tool = null;
  }

  get firstId() {
    return this.order.length ? this.order[0] : 0;
  }

  get lastId() {
    return this.order.length ? this.order[this.order.length - 1] : 0;
  }

  /** Take in turns, in any order. Returns the ones that were new, oldest first. */
  addTurns(list) {
    const fresh = [];
    for (const turn of list) {
      if (this.turns.has(turn.id)) continue;
      this.turns.set(turn.id, turn);
      if (turn.direction === "out" && turn.meta?.run_id && turn.meta.final_response === true) {
        this.completedRuns.add(turn.meta.run_id);
        this.drafts.delete(turn.meta.run_id);
      }
      fresh.push(turn);
      for (const approval of turn.approvals ?? []) {
        // what a turn carried is a snapshot: it may fill a gap, never overwrite something newer
        if (!this.approvals.has(approval.id)) this.approvals.set(approval.id, approval);
      }
    }
    if (fresh.length) {
      this.order = [...this.turns.keys()].sort((a, b) => a - b);
      fresh.sort((a, b) => a.id - b.id);
    }
    return fresh;
  }

  /** Newer word on approvals (from the stream, or from answering one). */
  setApprovals(list) {
    for (const approval of list) this.approvals.set(approval.id, approval);
  }

  approval(snapshot) {
    return this.approvals.get(snapshot.id) ?? snapshot;
  }

  get waiting() {
    return [...this.approvals.values()]
      .filter((a) => a.status === "pending")
      .sort((a, b) => a.id - b.id);
  }

  setWorking(working, tool = null) {
    this.working = Boolean(working);
    this.tool = this.working ? tool : null;
  }

  /** Replace all previews, including on reconnect. They never enter history or local storage. */
  replaceDrafts(list) {
    this.drafts.clear();
    for (const draft of (Array.isArray(list) ? list : []).slice(0, 16)) {
      if (!draft || typeof draft !== "object") continue;
      if (typeof draft.run_id !== "string" || !draft.run_id || draft.run_id.length > 100) continue;
      if (this.completedRuns.has(draft.run_id)) continue;
      if (typeof draft.text !== "string" || !Number.isSafeInteger(draft.sequence)) continue;
      this.drafts.set(draft.run_id, { ...draft, text: draft.text.slice(0, 32000) });
    }
  }

  // ---- messages on their way ---------------------------------------------------------------
  addPending({ clientId, text, files = [], voice = null, at }) {
    const entry = { clientId, text, files, voice, at, state: "sending", error: null };
    this.pending.push(entry);
    return entry;
  }

  findPending(clientId) {
    return this.pending.find((p) => p.clientId === clientId) ?? null;
  }

  dropPending(clientId) {
    const before = this.pending.length;
    this.pending = this.pending.filter((p) => p.clientId !== clientId);
    return this.pending.length !== before;
  }

  /** The server answered: the message is a turn now. Returns it if it was not here already. */
  settle(clientId, turn) {
    this.dropPending(clientId);
    return turn ? this.addTurns([turn]) : [];
  }

  /**
   * A turn arrived on the stream. If it is the message this page is still waiting to hear
   * about, the stream got there before the reply to the request did: drop the waiting copy.
   */
  matchIncoming(turn) {
    if (turn.direction !== "in" || turn.channel !== "web") return null;
    const count = turn.meta?.attachments?.length ?? 0;
    const spoken = Boolean(turn.meta?.voice);
    const match = this.pending.find(
      (p) =>
        p.state === "sending" &&
        p.files.length === count &&
        Boolean(p.voice) === spoken &&
        (spoken || p.text === turn.text),
    );
    if (!match) return null;
    this.dropPending(match.clientId);
    return match;
  }

  /** The turns in order, then the messages still on their way. */
  get timeline() {
    return [
      ...this.order.map((id) => ({ key: `t${id}`, turn: this.turns.get(id) })),
      ...this.pending.map((p) => ({ key: `p${p.clientId}`, pending: p })),
    ];
  }
}
