import { RequestScope, buildTaskRequest, buildStepRequest, buildAnswerRequest } from "./query.js";
import { createProductShell } from "./product-shell.js";
// API boundaries are documented in workbench.d.ts. No credential is stored in browser storage.
/** @typedef {{id:string, display_name:string, is_admin:boolean, authz_version?:number}} Principal */
/** @typedef {{id:string,name:string,kind:'personal'|'project'}} Space */
/** @typedef {{principal:Principal,spaces:Space[],space_id:string,role:string}} Me */
/** @typedef {import('./workbench').Task} Task */

/** @type {{me:Me|null,space:string,tab:string,taskView:string,epoch:number,pendingInvite:string,taskId:string|null,knowledgeRevision:number|null}} */
const WORKSPACE_TABS = {tasks:"任务",knowledge:"资料",agents:"助手与连接",spaces:"项目与成员",devices:"设备与账号"};
function routeFromLocation(href = location.href) {
  const params = new URL(href).searchParams;
  const requested = params.get("tab");
  return {space:params.get("space") || "",tab:Object.hasOwn(WORKSPACE_TABS,requested) ? requested : "tasks",taskView:params.get("view") === "computer" ? "computer" : "collaboration"};
}
const state = {me:null,...routeFromLocation(),epoch:0,pendingInvite:"",taskId:null,knowledgeRevision:null};
const $ = (id) => /** @type {HTMLElement} */ (document.getElementById(id));
const input = (id) => /** @type {HTMLInputElement} */ (document.getElementById(id));
const form = (id) => /** @type {HTMLFormElement} */ (document.getElementById(id));
const field = (f, name) => /** @type {HTMLInputElement|HTMLTextAreaElement|HTMLSelectElement} */ (f.elements.namedItem(name));
const statuses = {awaiting_approval:"等待审批",queued:"等待执行设备",submitting:"正在交接",admitted:"设备已接收",running:"正在执行",collecting:"正在收集成果",succeeded:"已完成",failed:"执行失败",cancel_requested:"取消待确认",cancelled:"已取消",uncertain:"执行结果待核查",expired:"已过期",rejected:"已拒绝",waiting_for_runner:"等待设备上线",waiting_input:"等待补充信息"};
const terminal = new Set(["succeeded","failed","cancelled","expired","rejected"]);
let refreshing = false;
let productShell = null;
let refreshSerial = 0;
const reads = {tasks:0,members:0,knowledge:0,devices:0};
let restoringAccess = false;
const openLogAttempts = new Set();
let orchestration = null;
let orchestrationLoading = null;
const artifactUrls = new Set();

// Consume the fragment before doing any network work. It never enters storage or an API URL.
const fragment = new URLSearchParams(location.hash.slice(1));
state.pendingInvite = fragment.get("invite") || "";
if (location.hash) history.replaceState(null,"",location.pathname + location.search);

function node(tag, text = "", className = "") {
  const result = document.createElement(tag);
  if (text) result.textContent = text;
  if (className) result.className = className;
  return result;
}
let noticeTimer = 0;
function notice(text, error = false) {
  if(typeof clearTimeout === "function") clearTimeout(noticeTimer);
  $("notice").textContent = text;
  $("notice").className = error ? "notice error" : "notice";
  $("notice").hidden = !text;
  // Good news says its piece and leaves; anything that went wrong stays until it is dealt with.
  if(text && !error && typeof setTimeout === "function") noticeTimer = setTimeout(() => notice(""), 6000);
}
function readable(error) {
  if(error?.status === 0 || error instanceof TypeError) return "没收到服务器的回复，操作可能已经生效，请刷新看看。";
  if(error?.status === 409) return "内容已经变化，请刷新后再试。填写的内容还在。";
  if(error?.status === 422) return "请检查填写的内容后再提交。";
  if(error?.status === 401 || error?.status === 403) return "没有权限，请重新登录或换个空间。";
  return error instanceof Error ? error.message : "操作未完成，请重试。";
}
function run(action) { Promise.resolve().then(action).catch(error => {
  if (error?.name === "AbortError") return;
  notice(readable(error),true);
}); }
async function restoreAccess() {
  if (restoringAccess) return;
  // Discard the revoked space synchronously, even when a parallel request is cancelled first.
  restoringAccess = true;
  try { await switchSpace("", {discard:true,historyMode:"replace"}); }
  catch (error) {
    if (error?.status === 401) showLogin();
    else if (error?.name !== "AbortError") notice(readable(error),true);
  } finally { restoringAccess = false; }
}
function date(value) {
  if (!value) return "";
  const result = new Date(Number(value) > 1e12 ? Number(value) : Number(value) * 1000);
  return Number.isNaN(result.getTime()) ? "" : result.toLocaleString("zh-CN",{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});
}
function badge(value) {
  const result = node("span",statuses[value] || value || "等待处理","badge");
  if (value === "succeeded") result.classList.add("good");
  if (value === "awaiting_approval") result.classList.add("wait");
  if (["failed","uncertain","rejected"].includes(value)) result.classList.add("bad");
  return result;
}
function empty(container, text) { container.replaceChildren(node("p",text,"empty")); }
async function openWorkspace() {
  $("workspace-loading").hidden=false;$("workspace-error").hidden=true;notice("");
  try {if(await loadMe()) await refresh();}
  catch(error) {if(error?.name!=="AbortError" && !state.me) $("workspace-error").hidden=false;throw error;}
  finally {$("workspace-loading").hidden=true;}
}
function scopedUrl(path) {
  const url = new URL(path,location.origin);
  if (state.space) url.searchParams.set("space",state.space);
  return url.pathname + url.search + url.hash;
}
function workspaceUrl() {
  const url=new URL(scopedUrl("/workspace"),location.origin);
  url.searchParams.set("tab",state.tab);
  if(state.tab==="tasks" && state.taskView==="computer") url.searchParams.set("view","computer");
  return url.pathname + url.search;
}
function updateRoute(mode="replace") {
  const path=workspaceUrl();
  if(mode==="push" && location.pathname + location.search !== path) history.pushState(null,"",path);
  else history.replaceState(null,"",path);
}
function hasDrafts(except=null) { return [...$("workspace").querySelectorAll('form[data-dirty="1"],form[data-busy="1"]')].some(item=>item!==except); }
let shownPanel="";
function showPanel({focus=false}={}) {
  const apply=()=>{
    for(const name of Object.keys(WORKSPACE_TABS)) $("panel-" + name).hidden=name!==state.tab;
    for(const name of ["collaboration","computer"]) {
      const active=state.taskView===name;
      $(name + "-tasks").hidden=!active;
      $("task-view-" + name).classList.toggle("selected",active);
      $("task-view-" + name).setAttribute("aria-pressed",String(active));
    }
    $("tasks-intro").textContent=state.taskView==="collaboration" ? "把目标交给助手，进度与成果都在这里。" : "安排电脑去做具体的事。";
    document.title=`${WORKSPACE_TABS[state.tab]} · 栖序`;
    $("masthead-title").textContent=WORKSPACE_TABS[state.tab];
    productShell?.setActive(state.tab);
    if(focus) $("panel-" + state.tab).querySelector("h1")?.focus({preventScroll:true});
  };
  // Changing page cross-fades where the browser can do it; everywhere else, and for people who
  // asked for less motion, the switch is immediate exactly as before.
  const key=state.tab + ":" + (state.tab==="tasks" ? state.taskView : "");
  const animate=shownPanel && shownPanel!==key && typeof document.startViewTransition==="function" && !(typeof matchMedia==="function" && matchMedia("(prefers-reduced-motion: reduce)").matches);
  shownPanel=key;
  if(!animate) {apply();return;}
  const transition=document.startViewTransition(apply);
  for(const name of ["ready","finished","updateCallbackDone"]) transition?.[name]?.catch?.(()=>{});
}
const requests = new RequestScope({
  resolve: scopedUrl,
  fetcher: (...args) => fetch(...args),
  onAccessLost: status => {
    if (!state.me) return;
    if (status === 401) showLogin(); else void restoreAccess();
  },
});
async function api(path, options = {}) {
  return await requests.request(path, {method:options.method, json:options.body, signal:options.signal});
}
function cancelRequests() {
  state.epoch += 1;
  requests.clear();
}
function clearSpace() {
  notice("");
  state.taskId = null;
  state.knowledgeRevision = null;
  for(const name of Object.keys(reads)) reads[name]++;
  openLogAttempts.clear();
  orchestration?.clear();
  for(const url of artifactUrls) URL.revokeObjectURL(url);
  artifactUrls.clear();
  form("knowledge-form").reset();
  form("knowledge-search-form").reset();
  input("knowledge-history").disabled=true;
  form("task-form").reset();
  for(const id of ["knowledge-form","task-form","space-form","invite-form","device-pair-form","node-invite-form"]) {
    form(id).reset();delete form(id).dataset.requestId;delete form(id).dataset.dirty;
  }
  $("knowledge-editor-panel").hidden=true;$("knowledge-empty-editor").hidden=false;
  for (const id of ["tasks-list","task-detail","task-diagnostics","device-diagnostics","members-list","knowledge-list","nodes-list","devices-list","channel-bindings-list","review-list"]) $(id).replaceChildren();
  $("task-detail").hidden = true;
  closeSecret();
  closeHistory();
}
function showLogin() {
  cancelRequests();
  state.me = null;
  state.space = "";
  clearSpace();
  $("workspace").hidden = true;
  $("login").hidden = false;
  input("enroll-code").value = state.pendingInvite;
  $("workspace-loading").hidden=true;
  $("workspace-context").textContent="栖序";
  $("space-role").textContent="";
  $("space-select").replaceChildren();
  productShell?.setContext({name:"",space:"",role:""});
}
async function loadMe() {
  const previous = state.me;
  const previousSpace = state.space;
  const epoch = state.epoch;
  try { state.me = await api("/api/v3/me"); }
  catch (error) {
    if (error.status === 401) {showLogin();return false;}
    if (error.status === 403 && state.space && epoch === state.epoch) {
      cancelRequests();clearSpace();state.space="";
      state.me=await api("/api/v3/me");
    } else throw error;
  }
  const me = state.me;
  if (!me || !me.principal || !Array.isArray(me.spaces)) throw new Error("账号信息暂时不可用。");
  state.space = me.space_id || me.spaces[0]?.id || "";
  if (previous && (previous.principal.id !== me.principal.id || previousSpace !== state.space || previous.role !== me.role || previous.principal.authz_version !== me.principal.authz_version)) clearSpace();
  requests.setContext(`${me.principal.id}:${state.space}:${me.role}:${me.principal.authz_version ?? 0}`);
  const select = /** @type {HTMLSelectElement} */ ($("space-select"));
  select.replaceChildren();
  for (const space of me.spaces) {
    const option = document.createElement("option");
    option.value = space.id;
    option.textContent = `${space.name}${space.kind === "personal" ? " · 私人" : ""}`;
    select.append(option);
  }
  select.value = state.space;
  const space=me.spaces.find(item=>item.id===state.space);
  const role={owner:"负责人",member:"成员",viewer:"只读访问"}[me.role] || "";
  $("workspace-context").textContent=space?.name || "个人空间";
  $("space-role").textContent=space?.kind==="personal" ? "仅自己" : role;
  productShell?.setContext({name:me.principal.display_name,space:space?.name || "个人空间",role:space?.kind==="personal" ? "个人空间" : role});
  for(const id of ["header-chat-link","device-chat-link","knowledge-chat-link"]) $(id).setAttribute("href",scopedUrl("/"));
  const isProject = me.spaces.find(s => s.id === state.space)?.kind === "project";
  $("member-invite-card").hidden = !isProject || me.role !== "owner";
  $("node-invite-form").hidden = !me.principal.is_admin;
  $("node-invite-details").hidden = !me.principal.is_admin;
  input("knowledge-new").disabled = me.role === "viewer";
  $("review-card").hidden = !me.principal.is_admin;
  $("channel-pair-button").hidden = me.role === "viewer";
  $("task-compose").hidden = me.role === "viewer";
  for (const item of form("knowledge-form").elements) if ("disabled" in item && item.id!=="knowledge-history") item.disabled = me.role === "viewer";
  $("join-banner").hidden = !state.pendingInvite;
  $("login").hidden = true;
  $("workspace").hidden = false;
  $("workspace-loading").hidden=true;
  showPanel();
  updateRoute();
  return true;
}
async function switchSpace(space, {discard=false,historyMode="push"}={}) {
  if(!discard && space!==state.space && hasDrafts() && !confirm("切换空间会清除尚未提交的内容。继续切换？")) {
    input("space-select").value=state.space;return false;
  }
  cancelRequests();
  state.space = space;
  clearSpace();
  notice("");
  updateRoute(historyMode);
  if (await loadMe()) await refresh();
  return true;
}
async function selectTab(tab, {historyMode="push",focus=true}={}) {
  if(!Object.hasOwn(WORKSPACE_TABS,tab)) return;
  const changed=state.tab!==tab;
  state.tab=tab;
  showPanel({focus:focus && changed});
  updateRoute(historyMode);
  if(changed) notice("");
  await refresh();
}
async function followLocation() {
  const route=routeFromLocation();
  if(route.space!==state.space && hasDrafts() && !confirm("切换空间会清除尚未提交的内容。继续切换？")) {updateRoute();return;}
  const changedSpace=route.space!==state.space;
  state.tab=route.tab;state.taskView=route.taskView;showPanel({focus:true});
  if(changedSpace) await switchSpace(route.space,{discard:true,historyMode:"replace"});
  else await refresh();
}
async function agentUI() {
  if(!orchestrationLoading) orchestrationLoading=import("./orchestration.js").then(module=>{
    orchestration=module.createOrchestrationUI({api,getContext:()=>state,notice,showSecret,
      onOpenAgents:()=>selectTab("agents"),
      downloadArtifact:async(path,name)=>{
        const epoch=state.epoch,blob=await requests.blob(path);
        if(epoch!==state.epoch || !state.me) return;
        const url=URL.createObjectURL(blob);artifactUrls.add(url);
        const link=node("a");link.href=url;link.download=String(name).replace(/[\\/\u0000-\u001f]/g,"_") || "result";
        document.body.append(link);link.click();link.remove();
        setTimeout(()=>{URL.revokeObjectURL(url);artifactUrls.delete(url);},1000);
      },
    });return orchestration;
  }).catch(error=>{orchestrationLoading=null;throw error;});
  return await orchestrationLoading;
}
async function selectTaskView(view) {
  if(!["collaboration","computer"].includes(view)) return;
  state.taskView=view;
  showPanel();updateRoute("push");
  await refresh();
}
async function refresh(force = false) {
  if (!state.me) return;
  const epoch=state.epoch,tab=state.tab,view=state.taskView,serial=++refreshSerial;
  const panel=$("panel-" + tab),button=$("refresh-" + tab);
  panel.setAttribute("aria-busy","true");
  if(button) {button.disabled=true;button.textContent="更新中…";}
  try {
    if(tab==="agents" || (tab==="tasks" && view==="collaboration")) {
      const ui=await agentUI();
      if(epoch!==state.epoch || tab!==state.tab || view!==state.taskView || !state.me) return;
      if(tab==="agents") await ui.loadAgents(); else await ui.refreshTasks(force);
    } else if(tab==="tasks") await loadTasks(force && !editingTask());
    else if(tab==="spaces") await loadMembers();
    else if(tab==="knowledge") await loadKnowledge();
    else if(tab==="devices") await loadDevices();
  } finally {
    if(serial===refreshSerial || tab!==state.tab) {
      panel.removeAttribute("aria-busy");
      if(button) {button.disabled=false;button.textContent="刷新";}
    }
  }
}
function editingTask() {
  const panel=$("task-detail");
  return Boolean(panel.querySelector('form[data-dirty="1"],form[data-busy="1"]') ||
    (panel.contains(document.activeElement) && document.activeElement?.closest("form")));
}
async function loadTasks(force = false) {
  const serial=++reads.tasks;
  const [data,diagnostics] = await Promise.all([api("/api/v3/tasks"),api("/api/v3/diagnostics")]);
  if(serial!==reads.tasks) return;
  renderDiagnostics(diagnostics);
  const list = $("tasks-list");
  const focusedTask=list.contains(document.activeElement) ? document.activeElement?.closest("button")?.dataset.taskId : null;
  list.replaceChildren();
  const tasks = data.tasks || [];
  if (!tasks.length) {
    const box=node("div","","empty");box.append(node("h3","还没有电脑任务"),node("p","交给电脑做的任务，进度和结果会显示在这里。"));
    if(state.me?.role!=="viewer") {
      const button=node("button","新建电脑任务","primary");button.type="button";
      button.addEventListener("click",()=>{$("task-compose").open=true;field(form("task-form"),"title").focus?.();});box.append(button);
    }
    list.append(box);
  }
  for (const task of tasks) {
    const button = node("button","","task-card");
    button.type = "button";button.dataset.taskId=task.id;
    button.classList.toggle("selected",state.taskId === task.id);
    button.append(node("h3",task.title || "未命名任务"));
    const meta = node("div","","task-meta");
    meta.append(badge(task.state || task.status),node("span",date(task.updated_at || task.created_at)));
    button.append(meta);
    button.addEventListener("click",() => run(async () => {
      if(state.taskId===task.id && editingTask()) return;
      if(state.taskId!==task.id && editingTask() && !confirm("切换任务会丢弃尚未提交的内容，继续吗？")) return;
      state.taskId=task.id;await loadTasks(true);
      if(matchMedia("(max-width:760px)").matches) $("task-detail").scrollIntoView({behavior:matchMedia("(prefers-reduced-motion:reduce)").matches ? "auto" : "smooth",block:"start"});
    }));
    list.append(button);
    if(focusedTask===task.id) button.focus?.({preventScroll:true});
  }
  if (state.taskId && (force || !editingTask())) {
    const taskId=state.taskId;
    const data = await api(`/api/v3/tasks/${encodeURIComponent(taskId)}`);
    if(taskId===state.taskId && serial===reads.tasks) renderTask(data.task || data, data.jobs);
  }
  if (state.me?.principal.is_admin) await loadReviews();
}
function renderDiagnostics(data) {
  const money=value=>typeof value==="number" && Number.isFinite(value) ? new Intl.NumberFormat("zh-CN",{style:"currency",currency:"USD",maximumFractionDigits:3}).format(value) : "—";
  const budget=data.budget || {}, counts=data.tasks || {}, devices=data.nodes || [];
  const connected=devices.filter(device=>device.online).length;
  const waiting=Number(counts.queued || 0);
  for(const id of ["task-diagnostics","device-diagnostics"]) {
    const box=$(id);box.replaceChildren();
    const grid=node("div","","diagnostics-grid");
    for(const [label,value] of [["当前空间今日花费",money(budget.space_spent_usd)],["今日可用额度",money(budget.instance_remaining_usd)],["在线电脑",`${connected} / ${devices.length}`],["等待执行的任务",String(waiting)]]) {
      const metric=node("div","","metric");metric.append(node("span",label),node("strong",value));grid.append(metric);
    }
    box.append(grid);
    if(!connected) box.append(node("p",waiting ? "执行电脑当前离线，任务会等待设备连接。聊天和任务查询仍可使用。" : "连接自己的执行电脑后，已批准的任务即可开始。","hint"));
    if(data.cleanup_pending) box.append(node("p",`${data.cleanup_pending} 项执行正在等设备清理。`,"hint"));
  }
}
async function loadReviews() {
  const data = await api("/api/v3/review-jobs");
  const list = $("review-list");list.replaceChildren();
  for (const entry of data.pending || data.jobs || []) {
    const job = entry.job || entry;
    const card = node("section","","review-item");
    card.append(node("h3",entry.title || entry.space_name || "待批准的执行计划"));
    card.append(node("pre",job.plan?.command || ""));
    const plan = job.plan || {};
    card.append(node("p",`最长 ${plan.timeout_s || 900} 秒 · ${plan.network === "none" ? "不连接网络" : "按计划连接网络"}`,"hint"));
    const paths = [...(plan.input_manifest || []).map(item => `输入：${item.path}（${item.size} 字节）`),...(plan.output_paths || []).map(path => `输出：${path}`)];
    if (paths.length) card.append(node("pre",paths.join("\n")));
    const actions=node("div","","actions");
    for (const [allowed,label] of [[true,"批准这份计划"],[false,"拒绝"]]) {
      const button=node("button",label,allowed ? "primary" : "ghost");
      button.addEventListener("click",()=>run(async()=>{
        for (const item of actions.querySelectorAll("button")) item.disabled=true;
        try {await api("/api/v3/review-jobs",{method:"POST",body:{space_id:entry.space_id,job_id:job.id,plan_hash:job.plan_hash,allowed}});await loadTasks();}
        finally {for(const item of actions.querySelectorAll("button")) item.disabled=false;}
      }));actions.append(button);
    }
    card.append(actions);list.append(card);
  }
  if(!list.childElementCount) empty(list,"暂时没有需要你批准的执行计划。");
}
function taskEditor(parent, title, handler) {
  const details=node("details","","task-editor");
  details.append(node("summary",title));
  const editor=node("form");
  editor.addEventListener("input",()=>{editor.dataset.dirty="1";if(!editor.dataset.busy) delete editor.dataset.requestId;});
  details.append(editor);parent.append(details);
  bindFormElement(editor,handler);
  return editor;
}
function taskField(editor, name, label, {kind="input",value="",placeholder="",required=true,maxLength=0} = {}) {
  const wrapper=node("label",label),control=node(kind);
  control.name=name;control.value=value;control.required=required;
  if(placeholder) control.placeholder=placeholder;
  if(maxLength) control.maxLength=maxLength;
  if(kind==="textarea") control.rows=3;
  wrapper.append(control);editor.append(wrapper);
  return control;
}
function submitButton(editor, text) {
  const actions=node("div","","actions");
  const button=node("button",text,"primary");button.type="submit";
  const discard=node("button","放弃修改","ghost");discard.type="button";
  discard.addEventListener("click",()=>run(async()=>{delete editor.dataset.dirty;delete editor.dataset.requestId;await loadTasks(true);}));
  actions.append(button,discard);editor.append(actions);
}
function taskPaths(editor, name) { return field(editor,name).value.split(/\r?\n/).map(value=>value.trim()).filter(Boolean); }
function planFields(editor) {
  return Object.fromEntries(["command","input_paths","output_paths","timeout_s"].map(name=>[name,field(editor,name).value]));
}
async function finishTaskEdit(editor, message) {
  delete editor.dataset.dirty;delete editor.dataset.requestId;
  notice(message);await loadTasks(true);
}
function renderQuestions(panel, task, canWrite) {
  for(const question of task.questions || []) {
    const current=question.task_revision===task.revision;
    const section=node("section","","task-question");
    section.append(node("h3",question.state==="open" && current ? "需要补充的信息" : "补充信息记录"));
    section.append(node("p",question.question));
    if(question.state==="answered") {
      section.append(node("pre",question.answer || ""));
      section.append(node("p",`${current ? "已回答" : "旧版本的回答"} · ${date(question.answered_at)}`,"hint"));
    } else if(question.state==="open" && current && canWrite) {
      const editor=node("form");
      editor.addEventListener("input",()=>{editor.dataset.dirty="1";});
      const choices=question.choices || [];
      const control=taskField(editor,"answer","你的回答",{kind:choices.length ? "select" : "textarea",maxLength:4000});
      if(choices.length) {
        const placeholder=node("option","请选择");placeholder.value="";placeholder.disabled=true;placeholder.selected=true;control.append(placeholder);
        for(const choice of choices) {const option=node("option",choice);option.value=choice;control.append(option);}
      }
      editor.append(node("p","回答会解除等待。执行内容如需调整，请另存一份计划并审批。","hint"));
      submitButton(editor,"提交回答");
      bindFormElement(editor,async f=>{
        await api(`/api/v3/tasks/${encodeURIComponent(task.id)}/questions/${encodeURIComponent(question.id)}/answer`,{
          method:"POST",body:buildAnswerRequest(field(f,"answer").value,task.revision),
        });
        await finishTaskEdit(f,"回答已保存，任务可以继续。");
      });section.append(editor);
    } else section.append(node("p",question.state==="open" ? "等待有编辑权限的成员回答。" : "这条问题已关闭。","hint"));
    panel.append(section);
  }
}
function renderTaskEditors(panel, task, jobs) {
  const currentJobs=jobs.filter(job=>job.task_revision===task.revision);
  const active=jobs.some(job=>["submitting","admitted","running","collecting","uncertain"].includes(job.state));
  const canContinue=!["failed","cancelled","cancel_requested","expired","rejected"].includes(task.state);
  const openQuestion=(task.questions || []).some(question=>question.state==="open" && question.task_revision===task.revision);
  if(canContinue && !active && !openQuestion) {
    const editor=taskEditor(panel,"＋ 等待成员补充信息",async f=>{
      if(!f.dataset.requestId) f.dataset.requestId=crypto.randomUUID();
      await api(`/api/v3/tasks/${encodeURIComponent(task.id)}/questions`,{method:"POST",body:{
        expected_revision:task.revision,request_id:f.dataset.requestId,
        question:field(f,"question").value.trim(),choices:taskPaths(f,"choices"),
      }});await finishTaskEdit(f,"问题已保存，后续执行等待回答。");
    });
    taskField(editor,"question","需要确认什么？",{kind:"textarea",maxLength:1000});
    taskField(editor,"choices","可选答案",{kind:"textarea",required:false,placeholder:"每行一个，最多十项；留空允许填写文字"});
    editor.append(node("p","暂停尚未开始的执行，直到当前空间的成员回答。","hint"));
    submitButton(editor,"保存问题并等待");
  }
  if(canContinue && currentJobs.length<8) {
    const step=String(currentJobs.reduce((maximum,job)=>Math.max(maximum,Number(job.step_id)||0),0)+1);
    const editor=taskEditor(panel,`＋ 添加第 ${step} 步`,async f=>{
      await api(`/api/v3/tasks/${encodeURIComponent(task.id)}/steps`,{method:"POST",body:buildStepRequest(planFields(f),task.revision,step)});
      await finishTaskEdit(f,"新步骤已保存，等待审批；前一步成功后才会执行。");
    });
    taskField(editor,"command","执行命令",{kind:"textarea",maxLength:16000});
    taskField(editor,"input_paths","输入文件",{kind:"textarea",required:false,placeholder:"当前工作区的相对路径，每行一个"});
    taskField(editor,"output_paths","需要保留的输出文件",{kind:"textarea",required:false,placeholder:"例如：report.md"});
    const timeout=taskField(editor,"timeout_s","最长执行时间（秒）",{value:"900"});timeout.type="number";timeout.min="1";timeout.max="3600";
    editor.append(node("p","每一步单独批准。","hint"));
    submitButton(editor,"保存步骤并申请审批");
  }
  const revise=taskEditor(panel,"修改任务目标，建立新版本",async f=>{
    await api(`/api/v3/tasks/${encodeURIComponent(task.id)}/revise`,{method:"POST",body:{revision:task.revision,title:field(f,"title").value.trim()}});
    await finishTaskEdit(f,"已建立新版本。请添加并审批新的执行计划。");
  });
  taskField(revise,"title","新的任务目标",{value:task.title,maxLength:200});
  revise.append(node("p","未完成的旧计划会收到取消请求，旧结果仍可查看。新版本需要重新添加步骤并审批。","hint"));
  submitButton(revise,"建立新版本");
}
function renderLogs(section, task, job) {
  if(!job.current_attempt) return;
  const details=node("details","","task-logs");details.append(node("summary","执行日志"));
  const output=node("pre","展开查看最近的执行输出。");
  const update=node("button","刷新日志","ghost");update.type="button";
  details.append(output,update);section.append(details);
  let loading=false;
  const load=async()=>{
    if(loading) return;loading=true;update.disabled=true;
    try {
      const response=await api(`/api/v3/attempts/${encodeURIComponent(job.current_attempt)}/logs`);
      if(!output.isConnected || state.taskId!==task.id) return;
      const snapshots=response.logs || [];
      output.textContent=snapshots.length ? snapshots[snapshots.length-1].text || "暂无日志内容。" : "设备尚未报告日志。";
    } finally {loading=false;update.disabled=false;}
  };
  details.addEventListener("toggle",()=>{
    if(details.open) {openLogAttempts.add(job.current_attempt);run(load);}
    else openLogAttempts.delete(job.current_attempt);
  });
  update.addEventListener("click",()=>run(load));
  details.open=openLogAttempts.has(job.current_attempt);
}
function renderTask(task, jobs) {
  const panel = $("task-detail");
  panel.replaceChildren();
  panel.hidden = false;
  const heading=node("div","","detail-heading"),close=node("button","返回列表","ghost detail-close");close.type="button";
  close.addEventListener("click",()=>{
    if(panel.querySelector('form[data-dirty="1"],form[data-busy="1"]') && !confirm("关闭详情会放弃尚未提交的内容。继续吗？")) return;
    const selected=[...$("tasks-list").querySelectorAll("button")].find(button=>button.dataset.taskId===task.id);
    state.taskId=null;panel.hidden=true;panel.replaceChildren();selected?.classList.toggle("selected",false);selected?.focus?.();
  });
  heading.append(node("h2",task.title || "任务详情"),close);panel.append(heading);
  const taskState = task.state || task.status;
  const canWrite=state.me?.role!=="viewer";
  const canManage=canWrite && (state.me?.role==="owner" || state.me?.principal.id===task.actor_id);
  const steps=jobs || task.jobs || [];
  const meta=node("div","","task-meta");meta.append(badge(taskState),node("span",`第 ${task.revision} 版`));panel.append(meta);
  if (taskState === "uncertain") panel.append(node("p","设备状态暂时确认不了，会继续查。","hint"));
  if (!terminal.has(taskState) && canManage) {
    const cancel = node("button",taskState === "cancel_requested" ? "取消待确认" : "取消任务","ghost danger");
    cancel.disabled = taskState === "cancel_requested";
    cancel.addEventListener("click",() => run(async () => {cancel.disabled=true;try {await api(`/api/v3/tasks/${encodeURIComponent(task.id)}/cancel`,{method:"POST",body:{}});notice("已提交取消。");await loadTasks(true);} finally {cancel.disabled=false;}}));
    const actions=node("div","","actions");actions.append(cancel);panel.append(actions);
  }
  renderQuestions(panel,task,canWrite);
  for (const job of steps) {
    const section=node("section");
    section.append(node("h3",`第 ${job.step_id} 步${job.task_revision!==task.revision ? ` · 旧版本 ${job.task_revision}` : ""}`));
    section.append(badge(job.state || job.status));
    section.append(node("pre",job.plan?.command || job.command || ""));
    const plan=job.plan || {};
    const inputs=(plan.input_manifest || []).map(item => `${item.path}（${item.size} 字节）`).join("\n");
    const outputs=(plan.output_paths || []).join("、");
    section.append(node("p",`最长 ${plan.timeout_s || 900} 秒 · ${plan.network === "none" ? "不连接网络" : "按批准的网络范围执行"}`,"hint"));
    if (inputs) {const files=node("details");files.append(node("summary","输入文件"),node("pre",inputs));section.append(files);}
    if (outputs) section.append(node("p",`保留成果：${outputs}`,"hint"));
    if ((job.state || job.status) === "awaiting_approval") {
      if (canWrite && (state.me?.principal.id === job.approver_id || (!job.approver_id && state.me?.principal.is_admin))) {
        const actions=node("div","","actions");
        for (const [allowed,label] of [[true,"批准执行"],[false,"拒绝"]]) {
          const button=node("button",label,allowed ? "primary" : "ghost");
          button.addEventListener("click",() => run(async () => {for(const item of actions.querySelectorAll("button")) item.disabled=true;try {await api(`/api/v3/jobs/${encodeURIComponent(job.id)}/approve`,{method:"POST",body:{plan_hash:job.plan_hash,allowed}});await loadTasks(true);} finally {for(const item of actions.querySelectorAll("button")) item.disabled=false;}}));
          actions.append(button);
        }
        section.append(actions);
      } else section.append(node("p","等批准使用这台电脑的人确认。","hint"));
    }
    renderLogs(section,task,job);
    const result=job.result;
    if (result) {
      section.append(node("h3","执行结果"));
      if (result.stale_revision) section.append(node("p","这是旧版本任务的成果，未推进当前任务。","hint"));
      if (result.stdout) section.append(node("pre",String(result.stdout)));
      if (result.stderr) section.append(node("pre",String(result.stderr)));
      if (result.error) section.append(node("p",String(result.error),"hint"));
      if (result.exit_code !== undefined && result.exit_code !== null) section.append(node("p",`退出状态：${result.exit_code}`,"hint"));
      for (const artifact of result.artifacts || []) {
        const id=typeof artifact === "string" ? artifact : artifact.id || artifact.artifact_id;
        if (!id) continue;
        const link=node("a",artifact.name || artifact.filename || "下载成果","button");
        link.href=scopedUrl(`/api/artifacts/${encodeURIComponent(id)}`);
        link.setAttribute("download","");section.append(link);
      }
    }
    panel.append(section);
  }
  if(canManage) renderTaskEditors(panel,task,steps);
}
async function loadMembers() {
  const serial=++reads.members;
  const data=await api("/api/v3/members");
  if(serial!==reads.members) return;
  const list=$("members-list");list.replaceChildren();
  for (const member of data.members || []) {
    const row=node("div","","row");
    row.append(node("strong",member.display_name || member.name || "成员"));
    const canManage=state.me?.role === "owner" && state.me.spaces.find(space=>space.id===state.space)?.kind === "project";
    if(canManage) {
      const actions=node("div","","actions"),select=node("select");
      select.setAttribute("aria-label",`设置 ${member.display_name || "成员"} 的角色`);
      for(const [value,label] of [["owner","负责人"],["member","成员"],["viewer","访客"]]) {const option=node("option",label);option.value=value;select.append(option);}
      select.value=member.role;
      select.addEventListener("change",()=>run(async()=>{
        select.disabled=true;
        try {await api("/api/v3/members",{method:"POST",body:{principal_id:member.principal_id || member.id,role:select.value}});await loadMe();await loadMembers();}
        finally{select.disabled=false;}
      }));
      const revoke=node("button","移除","ghost danger");
      revoke.addEventListener("click",()=>run(async()=>{
        if(!confirm(`从当前项目移除 ${member.display_name || "该成员"}？对方的私人空间保持独立。`)) return;
        const principalId=member.principal_id || member.id;
        const leaving=principalId===state.me?.principal.id;
        await api("/api/v3/members",{method:"POST",body:{principal_id:principalId,revoke:true}});
        if(leaving) {await switchSpace("",{discard:true});notice("已离开该项目，已切换到可访问的空间。");return;}
        await loadMe();await loadMembers();
      }));actions.append(select,revoke);row.append(actions);
    } else row.append(node("span",{owner:"负责人",member:"成员",viewer:"访客"}[member.role] || member.role,"badge"));
    list.append(row);
  }
  if (!list.childElementCount) empty(list,"此空间没有其他成员。");
}
function discardKnowledge() {
  if(form("knowledge-form").dataset.busy) {
    notice("资料正在保存，请等待保存完成后再切换或关闭。");
    return false;
  }
  return !form("knowledge-form").dataset.dirty || confirm("这份资料有尚未保存的修改，继续会放弃修改。继续吗？");
}
function highlightKnowledge(path) {
  for(const button of $("knowledge-list").querySelectorAll("button")) {
    const selected=button.dataset.path===path;
    button.classList.toggle("selected",selected);
    if(selected) button.setAttribute("aria-current","true"); else button.removeAttribute("aria-current");
  }
}
function openKnowledge(entry=null) {
  if(!discardKnowledge()) return;
  const editor=form("knowledge-form");
  editor.reset();delete editor.dataset.dirty;
  field(editor,"path").value=entry?.path || "";
  field(editor,"text").value=entry?.text || entry?.content || "";
  state.knowledgeRevision=entry?.revision ?? null;
  $("knowledge-editor-title").textContent=entry ? (state.me?.role==="viewer" ? "查看资料" : "编辑资料") : "新建资料";
  input("knowledge-history").disabled=state.knowledgeRevision===null;
  $("knowledge-editor-panel").hidden=false;$("knowledge-empty-editor").hidden=true;
  highlightKnowledge(entry?.path || "");
  if(!entry) field(editor,"path").focus?.();
  if(globalThis.matchMedia?.("(max-width:760px)").matches) $("knowledge-editor-panel").scrollIntoView?.({behavior:globalThis.matchMedia("(prefers-reduced-motion:reduce)").matches ? "auto" : "smooth",block:"start"});
}
function closeKnowledge() {
  if(!discardKnowledge()) return;
  form("knowledge-form").reset();delete form("knowledge-form").dataset.dirty;
  state.knowledgeRevision=null;input("knowledge-history").disabled=true;
  $("knowledge-editor-panel").hidden=true;$("knowledge-empty-editor").hidden=false;
  highlightKnowledge("");$("knowledge-new").focus?.();
}
async function loadKnowledge() {
  const serial=++reads.knowledge,query=input("knowledge-search").value.trim();
  const data=await api(`/api/v3/knowledge${query ? `?q=${encodeURIComponent(query)}` : ""}`);
  if(serial!==reads.knowledge || query!==input("knowledge-search").value.trim()) return;
  const list=$("knowledge-list");list.replaceChildren();
  for (const document of data.documents || []) {
    const entry=typeof document === "string" ? {path:document} : document;
    const button=node("button",entry.path || entry.name || "资料","document-link ghost");
    button.type="button";button.dataset.path=entry.path || "";
    button.addEventListener("click",() => openKnowledge(entry));list.append(button);
  }
  highlightKnowledge(field(form("knowledge-form"),"path").value);
  if (!list.childElementCount) empty(list,query ? "没有找到匹配的资料。换个关键词试试。" : "还没有资料。保存一份笔记，也可以在对话中让助理帮你整理。");
}
async function loadDevices() {
  const serial=++reads.devices;
  const [nodes,devices,diagnostics,channels]=await Promise.all([api("/api/v3/nodes"),api("/api/v3/devices"),api("/api/v3/diagnostics"),api("/api/v3/channel-bindings")]);
  if(serial!==reads.devices) return;
  renderDiagnostics(diagnostics);
  const list=$("nodes-list");list.replaceChildren();
  for (const device of nodes.nodes || []) {
    const row=node("div","","row"),copy=node("div","","row-copy");
    copy.append(node("strong",device.label || device.name || "执行电脑"));
    copy.append(node("span",device.last_seen_at ? `最近连接 ${date(device.last_seen_at)}` : "尚未连接"));
    const online = device.online ?? (device.last_seen_at && Date.now()/1000 - device.last_seen_at < 90);
    const live = online && !device.revoked_at;
    row.append(copy,node("span",device.revoked_at ? "已撤销" : online ? "在线" : "等待连接",live ? "badge good" : "badge"));
    if(state.me?.principal.is_admin && !device.revoked_at) {
      const revoke=node("button","撤销","ghost danger");
      revoke.addEventListener("click",()=>run(async()=>{
        if(!confirm(`撤销执行电脑 ${device.label || device.name || "此设备"}？已有的执行会被叫停。`)) return;
        await api(`/api/v3/nodes/${encodeURIComponent(device.id)}/revoke`,{method:"POST",body:{}});await loadDevices();
      }));row.append(revoke);
    }
    list.append(row);
  }
  if (!list.childElementCount) empty(list,"还没有执行电脑。任务会等设备上线。");
  const sessions=$("devices-list");sessions.replaceChildren();
  for (const device of devices.devices || []) {
    const row=node("div","","row"),copy=node("div","","row-copy");
    copy.append(node("strong",device.label || device.agent || "浏览器设备"),node("span",date(device.last_seen_at)));
    const revoke=node("button","撤销","ghost danger");
    revoke.addEventListener("click",() => run(async () => {await api(`/api/v3/devices/${encodeURIComponent(device.id)}/revoke`,{method:"POST",body:{}});notice("已撤销。");if(await loadMe()) await loadDevices();}));
    row.append(copy,revoke);sessions.append(row);
  }
  if (!sessions.childElementCount) empty(sessions,"没有活动登录设备。");
  const bindings=$("channel-bindings-list");bindings.replaceChildren();
  for (const binding of channels.bindings || []) {
    const row=node("div","","row"),copy=node("div","","row-copy");
    const label={telegram:"Telegram",dingtalk:"钉钉"}[binding.channel] || binding.channel;
    copy.append(node("strong",`${label} · ${binding.sender_id}`),node("span",`绑定于 ${date(binding.bound_at)}${binding.active === false ? " · 权限已变化，请重新绑定" : ""}`));
    const revoke=node("button","解除绑定","ghost danger");
    revoke.addEventListener("click",()=>run(async()=>{
      await api("/api/v3/channel-bindings/revoke",{method:"POST",body:{channel:binding.channel,account_id:binding.account_id,sender_id:binding.sender_id}});
      notice("渠道绑定已解除，该聊天账号将无法继续访问此空间。");await loadDevices();
    }));row.append(copy,revoke);bindings.append(row);
  }
  if (!bindings.childElementCount) empty(bindings,"你在当前空间还没有绑定聊天账号。");
}
function showSecret(title,value,description,extra="") {
  $("secret-title").textContent=title;$("secret-description").textContent=description;
  input("secret-value").value=value;$("secret-extra").textContent=extra;
  $("secret-copy").textContent="复制";
  /** @type {HTMLDialogElement} */ ($("secret-dialog")).showModal();
}
function closeSecret() {
  const dialog=/** @type {HTMLDialogElement} */ ($("secret-dialog"));
  if (dialog.open) dialog.close();
  input("secret-value").value="";$("secret-extra").textContent="";
}
function closeHistory() {
  const dialog=/** @type {HTMLDialogElement} */ ($("knowledge-history-dialog"));
  if(dialog.open) dialog.close();
  $("knowledge-history-list").replaceChildren();$("knowledge-history-title").textContent="资料历史";
}
async function showHistory() {
  const path=field(form("knowledge-form"),"path").value.trim();
  if(!path || state.knowledgeRevision===null) return;
  const revision=state.knowledgeRevision;
  const data=await api(`/api/v3/knowledge/history?path=${encodeURIComponent(path)}`);
  if(path!==field(form("knowledge-form"),"path").value.trim() || revision!==state.knowledgeRevision) return;
  const list=$("knowledge-history-list");list.replaceChildren();
  $("knowledge-history-title").textContent=`资料历史 · ${path}`;
  for(const version of data.versions || []) {
    const details=node("details"),action={create:"新增",str_replace:"更新",insert:"插入",delete:"删除",rename:"重命名"}[version.action] || "更新";
    const actor={user:"用户",agent:"助手",dream:"整理任务"}[version.actor] || "系统";
    details.append(node("summary",`${date(version.created_at)} · ${actor}${action}`),node("pre",version.text || "（无内容）"));list.append(details);
  }
  if(!list.childElementCount) empty(list,"还没有历史版本。");
  /** @type {HTMLDialogElement} */ ($("knowledge-history-dialog")).showModal();
}
function inviteLink(code,kind) { const url=new URL(kind === "pair" ? "/" : "/workspace",location.origin);url.hash=`${kind}=${encodeURIComponent(code)}`;return url.toString(); }
function bindFormElement(f,handler,id="") {
  f.addEventListener("submit",event => {
    event.preventDefault();
    if (f.dataset.busy) return;
    f.dataset.busy="1";f.setAttribute("aria-busy","true");
    const controls=[...f.elements].filter(item=>"disabled" in item);
    const disabled=controls.map(item=>item.disabled);
    for(const item of controls) item.disabled=true;
    run(async () => {try {if(await handler(f)!==false) delete f.dataset.dirty;} finally {
      delete f.dataset.busy;f.removeAttribute("aria-busy");
      controls.forEach((item,index)=>{item.disabled=disabled[index] || (id === "knowledge-form" && state.me?.role === "viewer");});
      // A successful save creates a revision. Restore read-only history from that new state,
      // not from the disabled value captured before saving a new document.
      if(id === "knowledge-form") input("knowledge-history").disabled=state.knowledgeRevision===null;
    }});
  });
}
function bindForm(id,handler) { bindFormElement(form(id),handler,id); }
function bindButton(id,handler) {
  const button=$(id);
  button.addEventListener("click",()=>{
    if(button.disabled) return;
    button.disabled=true;
    run(async()=>{try {await handler();} finally {button.disabled=false;}});
  });
}

bindForm("enroll-form",async f => {
  const data=await api("/api/v3/enroll",{method:"POST",body:{code:field(f,"code").value.trim(),display_name:field(f,"display_name").value.trim()}});
  state.pendingInvite="";f.reset();state.space="";
  if(await loadMe()) await refresh();
  if(data.recovery_code) showSecret("保存你的恢复码",data.recovery_code,"只显示这一次。所有设备都登不进去时，用它恢复账号。");
});
bindForm("recover-form",async f => {
  await api("/api/v3/recover",{method:"POST",body:{code:field(f,"code").value.trim()}});f.reset();state.space="";
  if(await loadMe()) await refresh();notice("已恢复访问。请在设备页撤销丢失设备，并生成新的恢复码。");
});
bindButton("accept-invite",async () => {
  await api("/api/v3/enroll",{method:"POST",body:{code:state.pendingInvite,display_name:state.me?.principal.display_name || "",require_existing_session:true}});
  state.pendingInvite="";if(await loadMe()) await selectTab("spaces");notice("已加入项目，可从菜单中切换空间。");
});
bindForm("space-form",async f => {
  if(hasDrafts(f) && !confirm("创建后会进入新项目，其他页面尚未提交的内容会清除。继续吗？")) return false;
  const data=await api("/api/v3/spaces",{method:"POST",body:{name:field(f,"name").value.trim()}});
  f.reset();await switchSpace(data.space?.id || data.id,{discard:true});await selectTab("spaces");
});
bindForm("invite-form",async f => {const data=await api("/api/v3/invites",{method:"POST",body:{role:field(f,"role").value,label:field(f,"label").value.trim()}});showSecret("项目邀请",inviteLink(data.code,"invite"),"将此链接交给想邀请的人。链接只能使用一次。",`有效期至 ${date(data.expires_at)}`);});
bindForm("task-form",async f => {
  if (!f.dataset.requestId) f.dataset.requestId=crypto.randomUUID();
  const data=await api("/api/v3/tasks",{method:"POST",body:buildTaskRequest({...planFields(f),title:field(f,"title").value},f.dataset.requestId)});
  state.taskId=data.task?.id || data.id;delete f.dataset.requestId;f.reset();/** @type {HTMLDetailsElement} */ ($("task-compose")).open=false;notice("任务已保存。审批和执行进度会显示在这里。");await loadTasks();
});
form("task-form").addEventListener("input",()=>{if(!form("task-form").dataset.busy) delete form("task-form").dataset.requestId;});
bindForm("knowledge-form",async f => {
  const body={path:field(f,"path").value.trim(),text:field(f,"text").value};
  if(state.knowledgeRevision !== null) body.revision=state.knowledgeRevision;
  const data=await api("/api/v3/knowledge",{method:"POST",body});
  state.knowledgeRevision=data.revision ?? data.document?.revision ?? data.documents?.find(document=>document.path===body.path)?.revision ?? null;input("knowledge-history").disabled=state.knowledgeRevision===null;notice("资料已保存到当前空间。");await loadKnowledge();
});
bindForm("knowledge-search-form",async()=>loadKnowledge());
input("knowledge-search").addEventListener("search",()=>run(loadKnowledge));
$("knowledge-history").addEventListener("click",()=>run(showHistory));
$("knowledge-history-close").addEventListener("click",closeHistory);
$("knowledge-history-dialog").addEventListener("close",()=>{$("knowledge-history-list").replaceChildren();$("knowledge-history-title").textContent="资料历史";});
$("knowledge-new").addEventListener("click",()=>openKnowledge());
$("knowledge-close").addEventListener("click",closeKnowledge);
form("knowledge-form").addEventListener("input",()=>{form("knowledge-form").dataset.dirty="1";});
field(form("knowledge-form"),"path").addEventListener("input",()=>{state.knowledgeRevision=null;input("knowledge-history").disabled=true;});
bindForm("device-pair-form",async f => {const data=await api("/api/v3/devices/pair",{method:"POST",body:{label:field(f,"label").value.trim()}});f.reset();showSecret("本人设备配对",inviteLink(data.code,"pair"),"在你的新设备上打开，登录同一个账号。",`有效期至 ${date(data.expires_at)}`);});
bindForm("node-invite-form",async f => {const data=await api("/api/v3/node-invites",{method:"POST",body:{label:field(f,"label").value.trim(),space_ids:[state.space]}});f.reset();showSecret("连接执行电脑",data.code,"在要连接的电脑上使用这个代码。",`在电脑终端运行 pa node pair --url ${location.origin}，按提示粘贴代码，然后运行 pa node run。有效期至 ${date(data.expires_at)}。`);});
bindButton("channel-pair-button",async()=>{const data=await api("/api/v3/channel-binding",{method:"POST",body:{}});showSecret("绑定聊天账号",`/bind ${data.code}`,"复制命令，在 Telegram 或钉钉里私聊机器人发送。",`仅能使用一次，有效期至 ${date(data.expires_at)}。`);});
bindButton("recovery-button",async()=>{const data=await api("/api/v3/recovery-code",{method:"POST",body:{}});showSecret("保存新的恢复码",data.code,"旧的恢复码已失效。");});
bindButton("retry-workspace",openWorkspace);
bindButton("logout-button",async()=>{await api("/api/logout",{method:"POST",body:{}});showLogin();notice("已退出当前设备。");});
$("space-select").addEventListener("change",event=>run(()=>switchSpace(event.target.value)));

for (const id of ["refresh-tasks","refresh-agents","refresh-knowledge","refresh-devices"]) $(id).addEventListener("click",()=>run(()=>refresh(true)));
for (const view of ["collaboration","computer"]) $("task-view-"+view).addEventListener("click",()=>run(()=>selectTaskView(view)));
$("secret-close").addEventListener("click",closeSecret);
$("secret-dialog").addEventListener("close",()=>{input("secret-value").value="";$("secret-extra").textContent="";});
$("secret-copy").addEventListener("click",()=>run(async()=>{if(navigator.clipboard) {await navigator.clipboard.writeText(input("secret-value").value);$("secret-copy").textContent="已复制";} else {input("secret-value").focus();input("secret-value").select();}}));
document.addEventListener("visibilitychange",()=>{if(!document.hidden) run(refresh);});
setInterval(()=>{if(!state.me || document.hidden || refreshing || !["tasks","agents","devices"].includes(state.tab)) return;refreshing=true;run(async()=>{try{await refresh();}finally{refreshing=false;}});},8000);
productShell=createProductShell({document,content:document.querySelector(".shell"),current:state.tab,resolve:scopedUrl,onNavigate:tab=>run(()=>selectTab(tab)),spaceControl:$("space-select")});
document.querySelector(".masthead").prepend(productShell.toggle);
$("space-control-source").remove();
$("notice").addEventListener("click",()=>notice(""));
window.addEventListener("popstate",()=>run(followLocation));
window.addEventListener("beforeunload",event=>{if(hasDrafts()){event.preventDefault();event.returnValue="";}});
for(const id of ["task-form","space-form","invite-form","device-pair-form","node-invite-form"]) form(id).addEventListener("input",()=>{form(id).dataset.dirty="1";});
showPanel();
const syncScrolled=()=>{const scrolled=String(window.scrollY>64);if(document.documentElement.dataset.scrolled!==scrolled) document.documentElement.dataset.scrolled=scrolled;};
window.addEventListener("scroll",syncScrolled,{passive:true});
syncScrolled();
run(openWorkspace);
