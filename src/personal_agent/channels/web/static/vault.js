// The knowledge base page: the notes you and every agent keep in one folder (an Obsidian vault).
// Look through them, read one, write one, and see what changed. The folder is the truth: this
// page only reads it and writes to it through the server, the same way Obsidian does on disk.
import { renderMarkdown } from "./md.js";

/** The four places notes live, and what each is for. */
export const AREAS = Object.freeze({ wiki: "笔记", raw: "外部资料", threads: "任务线索", daily: "日记" });
const PINNED = Object.freeze([["wiki/index.md", "目录"], ["wiki/log.md", "记录"]]);
const TYPES = Object.freeze({ thread: "任务线索", daily: "日记", index: "目录", log: "记录", result: "成果" });
const WHO = Object.freeze({ user: "你", agent: "助手", dream: "夜里整理", system: "系统" });
const WHAT = Object.freeze({ create: "新建", append: "追加", replace: "改写", section: "改了一节", restore: "恢复", revert: "撤销" });
const CARE = Object.freeze({
  uncompiled: "外部来的，还没整理进笔记",
  broken_link: "链到了还不存在的笔记",
  orphan: "没有别的笔记链到它",
  duplicate: "标题重复",
  properties: "属性写得不对",
  large: "太长，只看了前面一部分",
  skipped: "笔记太多，有些没看",
});
const MAX_TEXT = 400000;

/** A note's text without the properties block at the top. */
export function bodyOf(text) {
  const value = String(text || "");
  const match = /^---[ \t]*\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)/.exec(value);
  return match ? value.slice(match[0].length) : value;
}

/** `[[张三|老张]]` reads as 「老张」: a link by name, which the list below the note makes usable. */
export function withLinksShown(text) {
  // No class below takes a `[`: text with many unclosed `[[` would otherwise be rescanned to its end from each one.
  return String(text || "").replace(/!?\[\[([^[\]\n|#]+)(?:#[^[\]\n|]*)?(?:\|([^[\]\n]+))?\]\]/g, (_all, target, alias) => `「${(alias || target).trim()}」`);
}

/** Where a new note goes: a bare name lands in `wiki/`, a path is taken as written. */
export function notePath(input) {
  let value = String(input || "").trim().replaceAll("\\", "/").replace(/^\/+/, "");
  if (!value) throw new Error("给这篇笔记起个名字。");
  if (!value.includes("/")) value = `wiki/${value}`;
  return /\.md$/i.test(value) ? value : `${value}.md`;
}

/** The folders on the way to `folder`, for the line above the list. */
export function trailOf(folder) {
  const parts = String(folder || "").split("/").filter(Boolean);
  return parts.map((name, index) => ({ name: index === 0 ? AREAS[name] || name : name, folder: parts.slice(0, index + 1).join("/") }));
}

export function areaLabel(name) {
  return AREAS[name] || name;
}

/** One calm sentence about what is in the folder. */
export function statusLine(status) {
  if (!status?.enabled) return "知识库没有打开。";
  if (!status.notes) return "还是空的。让栖序记点什么，或者自己写一篇。";
  const parts = [`${status.notes} 篇笔记`];
  if (status.backlog) parts.push(`${status.backlog} 篇外部来的资料等着整理`);
  return parts.join(" · ");
}

export function issueCount(status) {
  return Object.entries(status?.issues || {}).filter(([kind]) => kind !== "skipped").reduce((sum, [, count]) => sum + count, 0);
}

export const careLabel = (kind) => CARE[kind] || kind;
export const whoLabel = (actor) => WHO[actor] || "系统";
export const whatLabel = (action) => WHAT[action] || "改了";

export function kindLabel(note) {
  if (TYPES[note.type]) return TYPES[note.type];
  const area = String(note.path || "").split("/")[0];
  return area === "wiki" ? "" : AREAS[area] || "";
}

const stamp = (value) => {
  const seconds = Number(value);
  if (!seconds) return "";
  const date = new Date(seconds > 1e12 ? seconds : seconds * 1000);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
};

export function createVaultUI({ api, getContext, notice, describe = (error) => error?.message || "没做成，再试一次。", document: doc = globalThis.document, confirm: ask = globalThis.confirm, copy = (text) => globalThis.navigator?.clipboard?.writeText(text) }) {
  const get = (id) => doc.getElementById(id);
  const context = () => getContext() || {};
  const canWrite = () => Boolean(context().me && context().me.role !== "viewer");
  const key = () => `${context().epoch}:${context().space}:${context().me?.principal?.id}:${context().me?.role}`;
  const state = { folder: "", query: "", status: null, note: null, mode: "empty", listing: "", reads: 0, noteReads: 0, care: null };

  const node = (tag, text = "", className = "") => {
    const element = doc.createElement(tag);
    if (text) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  };
  const button = (text, className = "ghost", type = "button") => {
    const element = node("button", text, className);
    element.type = type;
    return element;
  };
  const act = (action) => Promise.resolve().then(action).catch((error) => {
    if (error?.name !== "AbortError") notice(describe(error), true);
  });
  const params = (values) => Object.entries(values).filter(([, value]) => value).map(([name, value]) => `${name}=${encodeURIComponent(value)}`).join("&");

  // ---- the page around the note ---------------------------------------------------------
  function showOff() {
    get("vault-status").textContent = statusLine({ enabled: false });
    get("knowledge-list").replaceChildren(node("p", "在设置里把 [vault] 的 enabled 打开，就能用。", "empty"));
    get("vault-trail").replaceChildren();
    get("vault-care").hidden = true;
    get("vault-howto").hidden = true;
    get("knowledge-new").disabled = true;
  }

  function renderStatus(status) {
    state.status = status;
    get("vault-status").textContent = statusLine(status);
    get("knowledge-new").disabled = !canWrite();
    const care = get("vault-care");
    const count = issueCount(status);
    care.hidden = !count;
    get("vault-care-summary").textContent = count ? `整理建议（${count}）` : "整理建议";
    if (!count) care.open = false;
    const howto = get("vault-howto");
    howto.hidden = false;
    const body = get("vault-howto-body");
    const lines = [node("p", status.root
      ? "这个文件夹就是一个 Obsidian 仓库。在 Obsidian 里选「打开文件夹作为仓库」，选到下面这个位置。你在那边写的，助手下次就看得到；助手记的，你在那边也看得到。"
      : "这个项目的笔记放在服务器上，在这里看和写。", "muted")];
    if (status.root) {
      const path = node("code", status.root, "vault-path");
      const copyButton = button("复制路径", "ghost");
      copyButton.addEventListener("click", () => act(async () => { await copy(status.root); copyButton.textContent = "已复制"; }));
      lines.push(path, copyButton);
    }
    body.replaceChildren(...lines);
  }

  function renderTrail() {
    const trail = get("vault-trail");
    const items = [];
    const link = (text, folder, current = false) => {
      if (current) return node("span", text, "vault-here");
      const item = button(text, "ghost vault-step");
      item.addEventListener("click", () => act(() => browse(folder)));
      return item;
    };
    if (state.query) {
      items.push(node("span", `搜索「${state.query}」`, "vault-here"));
      const clear = button("清除", "ghost vault-step");
      clear.addEventListener("click", () => act(async () => { state.query = ""; get("knowledge-search").value = ""; await load(); }));
      items.push(clear);
    } else {
      const trailing = trailOf(state.folder);
      items.push(link("全部", "", !trailing.length));
      trailing.forEach((step, index) => items.push(link(step.name, step.folder, index === trailing.length - 1)));
    }
    const pinned = node("span", "", "vault-pinned");
    for (const [path, name] of PINNED) {
      const item = button(name, "ghost vault-step");
      item.addEventListener("click", () => act(() => open(path)));
      pinned.append(item);
    }
    trail.replaceChildren(...items, pinned);
  }

  function renderList(listing) {
    const signature = JSON.stringify([state.folder, state.query, listing.folders, listing.notes]);
    renderTrail();
    if (signature === state.listing) { highlight(); return; }
    state.listing = signature;
    const list = get("knowledge-list");
    const focused = list.contains?.(doc.activeElement) ? doc.activeElement?.dataset?.path : null;
    const rows = [];
    for (const folder of listing.folders || []) {
      const row = button("", "document-link vault-folder");
      row.dataset.folder = `${state.folder ? `${state.folder}/` : ""}${folder.name}`;
      row.append(node("span", state.folder ? folder.name : areaLabel(folder.name), "vault-name"), node("span", `${folder.count}`, "vault-count"));
      row.addEventListener("click", () => act(() => browse(row.dataset.folder)));
      rows.push(row);
    }
    for (const entry of listing.notes || []) {
      const row = button("", "document-link vault-row");
      row.dataset.path = entry.path;
      const head = node("span", "", "vault-line");
      head.append(node("span", entry.title || entry.path, "vault-name"));
      const kind = kindLabel(entry);
      if (kind) head.append(node("span", kind, "badge"));
      if (entry.outside) head.append(node("span", "外部来的", "badge"));
      row.append(head);
      if (entry.summary) row.append(node("span", entry.summary, "vault-summary"));
      row.addEventListener("click", () => act(() => open(entry.path)));
      rows.push(row);
    }
    if (!rows.length) rows.push(node("p", state.query ? "没有找到。换个词试试。" : state.folder ? "这个文件夹里还没有笔记。" : "还没有笔记。", "empty"));
    list.replaceChildren(...rows);
    highlight();
    if (focused) for (const row of rows) if (row.dataset?.path === focused) row.focus?.();
  }

  function highlight() {
    const path = state.note?.path || "";
    for (const row of get("knowledge-list").children || []) {
      if (row.dataset?.path === undefined) continue;
      const selected = row.dataset.path === path;
      row.classList?.toggle("selected", selected);
      if (selected) row.setAttribute("aria-current", "true"); else row.removeAttribute?.("aria-current");
    }
  }

  async function load() {
    const serial = ++state.reads;
    const where = key();
    const status = await api("/api/v3/vault");
    if (serial !== state.reads || where !== key()) return;
    if (!status.enabled) { showOff(); return; }
    renderStatus(status);
    const tag = state.query.startsWith("#") ? state.query.slice(1) : "";
    const listing = await api(`/api/v3/vault/notes?${params({ folder: state.folder, q: tag ? "" : state.query, tag })}`);
    if (serial !== state.reads || where !== key()) return;
    renderList(listing);
    if (get("vault-care").open) await loadCare();
    await refreshNote();
  }

  /** What an agent wrote into the open note a moment ago shows without reopening it. */
  async function refreshNote() {
    const note = state.note;
    if (!note || state.mode !== "view") return;
    const serial = state.noteReads;
    const where = key();
    let data;
    try {
      data = await api(`/api/v3/vault/note?${params({ path: note.path })}`);
    } catch (error) {
      if (error?.status !== 404) throw error;
      if (serial === state.noteReads && state.note === note) { notice("这篇笔记已经不在了。"); emptyPane(); }
      return;
    }
    if (serial !== state.noteReads || where !== key() || state.note !== note || state.mode !== "view") return;
    if (data.note.rev !== note.rev) show(data.note, { quiet: true });
  }

  async function browse(folder) {
    if (state.query) { state.query = ""; get("knowledge-search").value = ""; }
    state.folder = folder;
    await load();
  }

  async function search(query) {
    state.query = String(query || "").trim();
    state.folder = "";
    await load();
  }

  async function loadCare() {
    const where = key();
    const data = await api("/api/v3/vault/lint");
    if (where !== key()) return;
    const rows = [];
    for (const issue of data.issues || []) {
      const row = button("", "document-link vault-issue");
      row.append(node("span", issue.path || "整个知识库", "vault-name"), node("span", `${careLabel(issue.kind)}${issue.detail && issue.kind !== "orphan" && issue.kind !== "uncompiled" ? `：${issue.detail}` : ""}`, "vault-summary"));
      if (issue.path) row.addEventListener("click", () => act(() => open(issue.path)));
      rows.push(row);
    }
    get("vault-care-list").replaceChildren(...rows);
    state.care = data;
  }

  // ---- one note ---------------------------------------------------------------------------
  const editor = () => get("vault-note").querySelector?.("form") || null;

  /** True when it is fine to put something else where the note is being written. */
  function mayLeave() {
    const form = editor();
    if (!form) return true;
    if (form.dataset.busy) { notice("正在保存，等保存完再切换。"); return false; }
    return !form.dataset.dirty || ask("这篇有还没保存的修改，继续会放弃它们。继续吗？");
  }

  function emptyPane() {
    state.note = null;
    state.mode = "empty";
    get("vault-note").hidden = true;
    get("vault-note").replaceChildren();
    get("knowledge-empty-editor").hidden = false;
    highlight();
  }

  function pane(children, { quiet = false } = {}) {
    const panel = get("vault-note");
    panel.replaceChildren(...children.filter(Boolean));
    panel.hidden = false;
    get("knowledge-empty-editor").hidden = true;
    if (quiet) return;
    if (globalThis.matchMedia?.("(max-width:760px)").matches) panel.scrollIntoView?.({ behavior: globalThis.matchMedia("(prefers-reduced-motion:reduce)").matches ? "auto" : "smooth", block: "start" });
    panel.querySelector?.("h2")?.focus?.({ preventScroll: true });
  }

  function heading(title, path, ...actions) {
    const head = node("div", "", "editor-heading");
    const copy = node("div", "", "vault-title");
    const name = node("h2", title);
    name.tabIndex = -1;
    copy.append(name, node("p", path, "muted vault-where"));
    const bar = node("div", "", "vault-actions");
    bar.append(...actions);
    head.append(copy, bar);
    return head;
  }

  function closeButton() {
    const close = button("关闭", "ghost");
    close.setAttribute("aria-label", "关闭这篇笔记");
    close.addEventListener("click", () => { if (mayLeave()) emptyPane(); });
    return close;
  }

  function chips(title, items, onPick) {
    if (!items.length) return null;
    const section = node("div", "", "vault-links");
    section.append(node("h3", title));
    const row = node("div", "", "vault-chips");
    for (const item of items) {
      const chip = button(item.title, "ghost vault-chip");
      chip.addEventListener("click", () => act(() => onPick(item)));
      row.append(chip);
    }
    section.append(row);
    return section;
  }

  function show(note, { quiet = false } = {}) {
    state.note = note;
    state.mode = "view";
    const actions = [];
    if (note.obsidian) {
      const link = node("a", "在 Obsidian 打开", "button ghost");
      link.setAttribute("href", note.obsidian);
      actions.push(link);
    }
    const history = button("历史");
    history.addEventListener("click", () => act(showHistory));
    actions.push(history);
    if (canWrite() && !note.generated) {
      const edit = button("编辑", "primary");
      edit.addEventListener("click", () => startEdit());
      actions.push(edit);
    }
    actions.push(closeButton());
    const badges = node("div", "", "vault-badges");
    const kind = kindLabel(note);
    for (const text of [kind, note.outside ? "外部来的" : "", note.generated ? "自动生成，不用手改" : "", note.status, ...(note.tags || []).map((tag) => `#${tag}`), stamp(note.updated)]) {
      if (text) badges.append(node("span", text, "badge"));
    }
    const body = node("div", "", "vault-body md");
    body.append(renderMarkdown(doc, withLinksShown(bodyOf(note.text)).trim() || "（这篇是空的）"));
    const found = (note.outgoing || []);
    const missing = (note.missing || []).map((name) => ({ title: name, name }));
    pane(
      [
        heading(note.title || note.path, note.path, ...actions),
        badges,
        body,
        chips("这篇链到", found, (item) => open(item.path)),
        chips("还没有这些笔记，点一下就写", missing, (item) => startNew(item.name)),
        chips("这些笔记链到了它", note.backlinks || [], (item) => open(item.path)),
      ],
      { quiet },
    );
    highlight();
  }

  async function open(path, { force = false } = {}) {
    if (!force && !mayLeave()) return;
    const serial = ++state.noteReads;
    const where = key();
    const data = await api(`/api/v3/vault/note?${params({ path })}`);
    if (serial !== state.noteReads || where !== key()) return;
    show(data.note);
  }

  // ---- writing ----------------------------------------------------------------------------
  function writer({ isNew, note, text, name }) {
    const element = node("form", "", "vault-form");
    const fields = [];
    let nameInput = null;
    if (isNew) {
      nameInput = node("input");
      Object.assign(nameInput, { name: "name", type: "text", required: true, maxLength: 200, value: name || "", placeholder: "名字，比如 张三；放进别的文件夹就写 wiki/项目/张三" });
      const label = node("label", "名字");
      label.append(nameInput);
      fields.push(label);
    }
    const area = node("textarea");
    Object.assign(area, { name: "text", rows: 18, maxLength: MAX_TEXT, value: text, placeholder: "想记的内容。可以用 Markdown，用 [[双链]] 连到别的笔记。" });
    area.required = true;
    const label = node("label", isNew ? "内容" : "全文（最上面的 --- 之间是属性）");
    label.append(area);
    fields.push(label);
    const hint = node("p", "", "hint vault-conflict");
    hint.hidden = true;
    const save = button("保存", "primary", "submit");
    const cancel = button("放弃", "ghost");
    const fresh = button("载入最新", "ghost");
    fresh.hidden = true;
    const actions = node("div", "", "actions");
    actions.append(save, cancel, fresh);
    element.append(...fields, hint, actions);
    element.addEventListener("input", () => { element.dataset.dirty = "1"; });
    cancel.addEventListener("click", () => {
      if (!mayLeave()) return;
      if (note) show(note); else emptyPane();
    });
    fresh.addEventListener("click", () => act(async () => {
      if (!ask("换成最新的，你刚写的会丢掉。继续吗？")) return;
      delete element.dataset.dirty;
      await open(note.path, { force: true });
    }));
    element.addEventListener("submit", (event) => {
      event.preventDefault();
      if (element.dataset.busy) return;
      element.dataset.busy = "1";
      const controls = [save, cancel, fresh, area, ...(nameInput ? [nameInput] : [])];
      for (const control of controls) control.disabled = true;
      act(async () => {
        try {
          const path = isNew ? notePath(nameInput.value) : note.path;
          const body = isNew ? { path, text: area.value, mode: "create" } : { path, text: area.value, mode: "replace", rev: note.rev };
          const saved = await api("/api/v3/vault/note", { method: "POST", body });
          delete element.dataset.dirty;
          notice(saved.notice || "保存好了。");
          show(saved.note);
          await load();
        } catch (error) {
          if (error?.status === 409) {
            hint.textContent = "这篇刚刚在别处被改过。你写的还在这里，可以先复制出来。";
            hint.hidden = false;
            fresh.hidden = false;
            notice("这篇刚刚在别处被改过，你写的还在。", true);
            return;
          }
          throw error;
        } finally {
          delete element.dataset.busy;
          for (const control of controls) control.disabled = false;
        }
      });
    });
    return element;
  }

  function startEdit() {
    const note = state.note;
    if (!note || !canWrite() || note.generated) return;
    state.mode = "edit";
    pane([heading(note.title || note.path, note.path), writer({ isNew: false, note, text: note.text })]);
    get("vault-note").querySelector?.("textarea")?.focus?.();
  }

  function startNew(name = "") {
    if (!canWrite() || !mayLeave()) return;
    state.note = null;
    state.mode = "new";
    pane([heading("新笔记", "保存以后就在知识库里了"), writer({ isNew: true, text: "", name })]);
    get("vault-note").querySelector?.("input")?.focus?.();
    highlight();
  }

  // ---- looking back -----------------------------------------------------------------------
  function closeHistory() {
    const dialog = get("knowledge-history-dialog");
    if (dialog.open) dialog.close();
    get("knowledge-history-list").replaceChildren();
  }

  async function showHistory() {
    const note = state.note;
    if (!note) return;
    const where = key();
    const data = await api(`/api/v3/vault/history?${params({ path: note.path })}`);
    if (where !== key() || state.note?.path !== note.path) return;
    get("knowledge-history-title").textContent = `「${note.title || note.path}」的历史`;
    const list = get("knowledge-history-list");
    const rows = [];
    for (const version of data.versions || []) {
      const details = node("details");
      details.append(node("summary", `${stamp(version.created_at)} · ${whoLabel(version.actor)}${whatLabel(version.action)}`));
      details.append(node("pre", version.restorable ? version.text || "（改之前是空的）" : "（这是新建的，之前没有内容）"));
      if (version.restorable && canWrite()) {
        const restore = button("恢复到改之前", "ghost");
        restore.addEventListener("click", () => act(async () => {
          restore.disabled = true;
          try {
            await api("/api/v3/vault/restore", { method: "POST", body: { version_id: version.id } });
          } finally {
            restore.disabled = false;
          }
          closeHistory();
          notice("已经恢复。");
          await open(note.path, { force: true });
          await load();
        }));
        details.append(restore);
      }
      rows.push(details);
    }
    if (!rows.length) rows.push(node("p", "还没有改动记录。", "empty"));
    list.replaceChildren(...rows);
    get("knowledge-history-dialog").showModal();
  }

  // ---- wiring -----------------------------------------------------------------------------
  get("knowledge-search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    act(() => search(get("knowledge-search").value));
  });
  get("knowledge-search").addEventListener("search", () => act(() => search(get("knowledge-search").value)));
  get("knowledge-new").addEventListener("click", () => startNew());
  get("knowledge-history-close").addEventListener("click", closeHistory);
  get("knowledge-history-dialog").addEventListener("close", () => get("knowledge-history-list").replaceChildren());
  get("vault-care").addEventListener("toggle", () => { if (get("vault-care").open) act(loadCare); });

  return {
    load,
    open,
    search,
    startNew,
    state,
    closeHistory,
    /** Something is being written that has not been saved. */
    hasDraft() {
      const form = editor();
      return Boolean(form && (form.dataset.dirty || form.dataset.busy));
    },
    /** A different space or account: nothing of the old one stays on the page. */
    clear() {
      state.reads++;
      state.noteReads++;
      state.folder = "";
      state.query = "";
      state.status = null;
      state.listing = "";
      state.care = null;
      get("knowledge-search").value = "";
      get("knowledge-list").replaceChildren();
      get("vault-trail").replaceChildren();
      get("vault-care-list").replaceChildren();
      get("vault-status").textContent = "";
      emptyPane();
      closeHistory();
    },
  };
}
