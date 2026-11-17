import { RequestScope, buildTaskRequest, buildStepRequest, buildAnswerRequest } from "./query.js";
// API boundaries are documented in workbench.d.ts. No credential is stored in browser storage.
/** @typedef {{id:string, display_name:string, is_admin:boolean, authz_version?:number}} Principal */
/** @typedef {{id:string,name:string,kind:'personal'|'project'}} Space */
/** @typedef {{principal:Principal,spaces:Space[],space_id:string,role:string}} Me */
/** @typedef {import('./workbench').Task} Task */

/** @type {{me:Me|null,space:string,tab:string,epoch:number,pendingInvite:string,taskId:string|null,knowledgeRevision:number|null}} */
const state = {me:null,space:new URL(location.href).searchParams.get("space") || "",tab:"tasks",epoch:0,pendingInvite:"",taskId:null,knowledgeRevision:null};
const $ = (id) => /** @type {HTMLElement} */ (document.getElementById(id));
const input = (id) => /** @type {HTMLInputElement} */ (document.getElementById(id));
const form = (id) => /** @type {HTMLFormElement} */ (document.getElementById(id));
const field = (f, name) => /** @type {HTMLInputElement|HTMLTextAreaElement|HTMLSelectElement} */ (f.elements.namedItem(name));
const statuses = {awaiting_approval:"等待审批",queued:"等待执行设备",submitting:"正在交接",admitted:"设备已接收",running:"正在执行",collecting:"正在收集成果",succeeded:"已完成",failed:"执行失败",cancel_requested:"取消待确认",cancelled:"已取消",uncertain:"执行结果待核查",expired:"已过期",rejected:"已拒绝",waiting_for_runner:"等待设备上线",waiting_input:"等待补充信息"};
const terminal = new Set(["succeeded","failed","cancelled","expired","rejected"]);
let refreshing = false;
let restoringAccess = false;
const openLogAttempts = new Set();

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
function notice(text, error = false) {
  $("notice").textContent = text;
  $("notice").className = error ? "notice error" : "notice";
  $("notice").hidden = !text;
}
function readable(error) { return error instanceof Error ? error.message : "操作未完成，请重试。"; }
function run(action) { Promise.resolve().then(action).catch(error => {
  if (error?.name === "AbortError") return;
  notice(readable(error),true);
}); }
async function restoreAccess() {
  if (restoringAccess) return;
  // Discard the revoked space synchronously, even when a parallel request is cancelled first.
  restoringAccess = true;
  try { await switchSpace(""); }
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
  if (["failed","uncertain","rejected"].includes(value)) result.classList.add("bad");
  return result;
}
function empty(container, text) { container.replaceChildren(node("p",text,"empty")); }
function scopedUrl(path) {
  const url = new URL(path,location.origin);
  if (state.space) url.searchParams.set("space",state.space);
  return url.pathname + url.search;
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
  state.taskId = null;
  state.knowledgeRevision = null;
  openLogAttempts.clear();
  form("knowledge-form").reset();
  form("knowledge-search-form").reset();
  input("knowledge-history").disabled=true;
  form("task-form").reset();
  delete form("task-form").dataset.requestId;
  for (const id of ["tasks-list","task-detail","members-list","knowledge-list","nodes-list","devices-list","channel-bindings-list","review-list"]) $(id).replaceChildren();
  $("task-detail").hidden = true;
  closeSecret();
  closeHistory();
}
function showLogin() {
  cancelRequests();
  state.me = null;
  clearSpace();
  $("workspace").hidden = true;
  $("login").hidden = false;
  input("enroll-code").value = state.pendingInvite;
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
  $("identity-name").textContent = me.principal.display_name;
  $("space-role").textContent = {owner:"负责人",member:"成员",viewer:"访客"}[me.role] || me.role || "";
  $("chat-link").setAttribute("href",scopedUrl("/"));
  $("device-chat-link").setAttribute("href",scopedUrl("/"));
  const isProject = me.spaces.find(s => s.id === state.space)?.kind === "project";
  $("member-invite-card").hidden = !isProject || me.role !== "owner";
  $("node-invite-form").hidden = !me.principal.is_admin;
  $("review-card").hidden = !me.principal.is_admin;
  $("channel-pair-button").hidden = me.role === "viewer";
  $("task-compose").hidden = me.role === "viewer";
  for (const item of form("knowledge-form").elements) if ("disabled" in item && item.id!=="knowledge-history") item.disabled = me.role === "viewer";
  $("join-banner").hidden = !state.pendingInvite;
  $("login").hidden = true;
  $("workspace").hidden = false;
  history.replaceState(null,"",scopedUrl("/workspace"));
  return true;
}
async function switchSpace(space) {
  cancelRequests();
  state.space = space;
  clearSpace();
  notice("");
  if (await loadMe()) await refresh();
}
async function selectTab(tab) {
  state.tab = tab;
  for (const button of document.querySelectorAll("[data-tab]")) {
    const active = button.getAttribute("data-tab") === tab;
    button.classList.toggle("selected",active);
    if (active) button.setAttribute("aria-current","page"); else button.removeAttribute("aria-current");
  }
  for (const name of ["tasks","spaces","knowledge","devices"]) $("panel-" + name).hidden = name !== tab;
  await refresh();
}
async function refresh() {
  if (!state.me) return;
  if (state.tab === "tasks") await loadTasks();
  if (state.tab === "spaces") await loadMembers();
  if (state.tab === "knowledge") await loadKnowledge();
  if (state.tab === "devices") await loadDevices();
}
function editingTask() {
  const panel=$("task-detail");
  return Boolean(panel.querySelector('form[data-dirty="1"],form[data-busy="1"]') ||
    (panel.contains(document.activeElement) && document.activeElement?.closest("form")));
}
async function loadTasks(force = false) {
  const [data,diagnostics] = await Promise.all([api("/api/v3/tasks"),api("/api/v3/diagnostics")]);
  renderDiagnostics(diagnostics);
  const list = $("tasks-list");
  list.replaceChildren();
  const tasks = data.tasks || [];
  if (!tasks.length) empty(list,"还没有任务。交办一件事，进度和成果会留在这里。");
  for (const task of tasks) {
    const button = node("button","","task-card");
    button.type = "button";
    button.classList.toggle("selected",state.taskId === task.id);
    button.append(node("h3",task.title || "未命名任务"));
    const meta = node("div","","task-meta");
    meta.append(badge(task.state || task.status),node("span",date(task.updated_at || task.created_at)));
    button.append(meta);
    button.addEventListener("click",() => run(async () => {state.taskId=task.id;await loadTasks(true);if(matchMedia("(max-width:720px)").matches) $("task-detail").scrollIntoView({behavior:"smooth",block:"start"});}));
    list.append(button);
  }
  if (state.taskId && (force || !editingTask())) {
    const taskId=state.taskId;
    const data = await api(`/api/v3/tasks/${encodeURIComponent(taskId)}`);
    if(taskId===state.taskId) renderTask(data.task || data, data.jobs);
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
    for(const [label,value] of [["当前空间今日花费",money(budget.space_spent_usd)],["实例今日剩余额度",money(budget.instance_remaining_usd)],["已连接执行电脑",`${connected} / ${devices.length}`],["等待执行的任务",String(waiting)]]) {
      const metric=node("div","","metric");metric.append(node("span",label),node("strong",value));grid.append(metric);
    }
    box.append(grid);
    if(!connected) box.append(node("p",waiting ? "执行电脑当前离线，任务会等待设备连接。聊天和任务查询仍可使用。" : "连接自己的执行电脑后，已批准的任务即可开始。","hint"));
    if(data.cleanup_pending) box.append(node("p",`${data.cleanup_pending} 项执行正在等待设备确认清理，记录会继续保留。`,"hint"));
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
    const paths = [...(plan.input_manifest || []).map(item => `输入：${item.path}（${item.size} 字节）\nSHA-256: ${item.sha256}`),...(plan.output_paths || []).map(path => `输出：${path}`)];
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
    editor.append(node("p","每一步单独审批。上一步成果不会自动变成这一步的输入，请先确认并准备需要的文件。","hint"));
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
  panel.append(node("h2",task.title || "任务详情"));
  const taskState = task.state || task.status;
  const canWrite=state.me?.role!=="viewer";
  const canManage=canWrite && (state.me?.role==="owner" || state.me?.principal.id===task.actor_id);
  const steps=jobs || task.jobs || [];
  const meta=node("div","","task-meta");meta.append(badge(taskState),node("span",`第 ${task.revision} 版`));panel.append(meta);
  if (taskState === "uncertain") panel.append(node("p","设备状态暂时无法确认。系统会继续核查原执行，请勿重复创建同一操作。","hint"));
  if (!terminal.has(taskState) && canManage) {
    const cancel = node("button",taskState === "cancel_requested" ? "取消待确认" : "取消任务","ghost danger");
    cancel.disabled = taskState === "cancel_requested";
    cancel.addEventListener("click",() => run(async () => {cancel.disabled=true;try {await api(`/api/v3/tasks/${encodeURIComponent(task.id)}/cancel`,{method:"POST",body:{}});notice("已提交取消请求，停止状态以设备确认为准。");await loadTasks(true);} finally {cancel.disabled=false;}}));
    const actions=node("div","","actions");actions.append(cancel);panel.append(actions);
  }
  renderQuestions(panel,task,canWrite);
  for (const job of steps) {
    const section=node("section");
    section.append(node("h3",`第 ${job.step_id} 步${job.task_revision!==task.revision ? ` · 旧版本 ${job.task_revision}` : ""}`));
    section.append(badge(job.state || job.status));
    section.append(node("pre",job.plan?.command || job.command || ""));
    const plan=job.plan || {};
    const inputs=(plan.input_manifest || []).map(item => `${item.path}（${item.size} 字节）\nSHA-256: ${item.sha256}`).join("\n");
    const outputs=(plan.output_paths || []).join("、");
    section.append(node("p",`最长 ${plan.timeout_s || 900} 秒 · ${plan.network === "none" ? "不连接网络" : "按批准的网络范围执行"}`,"hint"));
    if (inputs) {const files=node("details");files.append(node("summary","输入文件快照"),node("pre",inputs));section.append(files);}
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
      } else section.append(node("p","等待该资源的审批人确认。","hint"));
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
  const data=await api("/api/v3/members");
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
        try {await api("/api/v3/members",{method:"POST",body:{principal_id:member.principal_id || member.id,role:select.value,action:"role"}});await loadMe();await loadMembers();}
        finally{select.disabled=false;}
      }));
      const revoke=node("button","移除","ghost danger");
      revoke.addEventListener("click",()=>run(async()=>{
        if(!confirm(`从当前项目移除 ${member.display_name || "该成员"}？对方的私人空间保持独立。`)) return;
        await api("/api/v3/members",{method:"POST",body:{principal_id:member.principal_id || member.id,action:"revoke",revoke:true}});
        await loadMe();await loadMembers();
      }));actions.append(select,revoke);row.append(actions);
    } else row.append(node("span",{owner:"负责人",member:"成员",viewer:"访客"}[member.role] || member.role,"badge"));
    list.append(row);
  }
  if (!list.childElementCount) empty(list,"此空间没有其他成员。");
}
async function loadKnowledge() {
  const query=input("knowledge-search").value.trim();
  const data=await api(`/api/v3/knowledge${query ? `?q=${encodeURIComponent(query)}` : ""}`);
  if(query!==input("knowledge-search").value.trim()) return;
  const list=$("knowledge-list");list.replaceChildren();
  for (const document of data.documents || []) {
    const entry=typeof document === "string" ? {path:document} : document;
    const button=node("button",entry.path || entry.name || "资料","document-link ghost");
    button.addEventListener("click",() => {
      const editor=form("knowledge-form");field(editor,"path").value=entry.path || "";field(editor,"text").value=entry.text || entry.content || "";
      state.knowledgeRevision=entry.revision ?? null;$("knowledge-editor-title").textContent="编辑资料";input("knowledge-history").disabled=false;
    });list.append(button);
  }
  if (!list.childElementCount) empty(list,query ? "没有找到匹配的资料，请调整关键词。" : "还没有资料。可以保存项目背景、来源和共同笔记。");
}
async function loadDevices() {
  const [nodes,devices,diagnostics,channels]=await Promise.all([api("/api/v3/nodes"),api("/api/v3/devices"),api("/api/v3/diagnostics"),api("/api/v3/channel-bindings")]);
  renderDiagnostics(diagnostics);
  const list=$("nodes-list");list.replaceChildren();
  for (const device of nodes.nodes || []) {
    const row=node("div","","row"),copy=node("div","","row-copy");
    copy.append(node("strong",device.label || device.name || "执行电脑"));
    copy.append(node("span",device.last_seen_at ? `最近连接 ${date(device.last_seen_at)}` : "尚未连接"));
    const online = device.online ?? (device.last_seen_at && Date.now()/1000 - device.last_seen_at < 90);
    row.append(copy,node("span",device.revoked_at ? "已撤销" : online ? "在线" : "等待连接","badge"));
    if(state.me?.principal.is_admin && !device.revoked_at) {
      const revoke=node("button","撤销","ghost danger");
      revoke.addEventListener("click",()=>run(async()=>{
        if(!confirm(`撤销执行电脑 ${device.label || device.name || "此设备"} 的权限？已有执行会收到停止请求。`)) return;
        await api(`/api/v3/nodes/${encodeURIComponent(device.id)}/revoke`,{method:"POST",body:{}});await loadDevices();
      }));row.append(revoke);
    }
    list.append(row);
  }
  if (!list.childElementCount) empty(list,"还没有获授权的执行电脑。任务会等待设备上线。");
  const sessions=$("devices-list");sessions.replaceChildren();
  for (const device of devices.devices || []) {
    const row=node("div","","row"),copy=node("div","","row-copy");
    copy.append(node("strong",device.label || device.agent || "浏览器设备"),node("span",date(device.last_seen_at)));
    const revoke=node("button","撤销","ghost danger");
    revoke.addEventListener("click",() => run(async () => {await api(`/api/v3/devices/${encodeURIComponent(device.id)}/revoke`,{method:"POST",body:{}});notice("该设备的登录权限已撤销。");if(await loadMe()) await loadDevices();}));
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
  const data=await api(`/api/v3/knowledge/history?path=${encodeURIComponent(path)}`);
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
    f.dataset.busy="1";
    const controls=[...f.elements].filter(item=>"disabled" in item);
    const disabled=controls.map(item=>item.disabled);
    for(const item of controls) item.disabled=true;
    run(async () => {try {await handler(f);} finally {
      delete f.dataset.busy;
      controls.forEach((item,index)=>{item.disabled=disabled[index] || (id === "knowledge-form" && state.me?.role === "viewer");});
    }});
  });
}
function bindForm(id,handler) { bindFormElement(form(id),handler,id); }

bindForm("enroll-form",async f => {
  const data=await api("/api/v3/enroll",{method:"POST",body:{code:field(f,"code").value.trim(),display_name:field(f,"display_name").value.trim()}});
  state.pendingInvite="";f.reset();state.space="";
  if(await loadMe()) await refresh();
  if(data.recovery_code) showSecret("保存你的恢复码",data.recovery_code,"只在这里展示。所有设备失效时，用它恢复访问，请离线保存。");
});
bindForm("recover-form",async f => {
  await api("/api/v3/recover",{method:"POST",body:{code:field(f,"code").value.trim()}});f.reset();state.space="";
  if(await loadMe()) await refresh();notice("已恢复访问。请在设备页撤销丢失设备，并生成新的恢复码。");
});
$("accept-invite").addEventListener("click",() => run(async () => {
  await api("/api/v3/enroll",{method:"POST",body:{code:state.pendingInvite,display_name:state.me?.principal.display_name || ""}});
  state.pendingInvite="";if(await loadMe()) await selectTab("spaces");notice("已加入项目，可从上方切换空间。");
}));
bindForm("space-form",async f => {const data=await api("/api/v3/spaces",{method:"POST",body:{name:field(f,"name").value.trim()}});f.reset();await switchSpace(data.space?.id || data.id);await selectTab("spaces");});
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
$("knowledge-new").addEventListener("click",()=>{form("knowledge-form").reset();state.knowledgeRevision=null;input("knowledge-history").disabled=true;$("knowledge-editor-title").textContent="新增资料";});
field(form("knowledge-form"),"path").addEventListener("input",()=>{state.knowledgeRevision=null;input("knowledge-history").disabled=true;});
bindForm("device-pair-form",async f => {const data=await api("/api/v3/devices/pair",{method:"POST",body:{label:field(f,"label").value.trim()}});f.reset();showSecret("本人设备配对",inviteLink(data.code,"pair"),"在你自己的新设备上打开。它会登录同一个账号，请勿当作成员邀请分享。",`有效期至 ${date(data.expires_at)}`);});
bindForm("node-invite-form",async f => {const data=await api("/api/v3/node-invites",{method:"POST",body:{label:field(f,"label").value.trim(),space_ids:[state.space]}});f.reset();showSecret("连接执行电脑",data.code,"仅在你管理的执行电脑上使用此代码。电脑只获得所选空间的任务权限。",`在电脑终端运行 pa node pair --url ${location.origin}，按提示粘贴代码，然后运行 pa node run。有效期至 ${date(data.expires_at)}。`);});
$("channel-pair-button").addEventListener("click",()=>run(async()=>{const data=await api("/api/v3/channel-binding",{method:"POST",body:{}});showSecret("绑定聊天账号",`/bind ${data.code}`,"复制命令，在 Telegram 或钉钉与机器人私聊发送。它会绑定你的账号与当前空间。",`仅能使用一次，有效期至 ${date(data.expires_at)}。`);}));
$("recovery-button").addEventListener("click",()=>run(async()=>{const data=await api("/api/v3/recovery-code",{method:"POST",body:{}});showSecret("保存新的恢复码",data.code,"旧恢复码已经失效。请离线保存这个一次性恢复码。");}));
$("logout-button").addEventListener("click",()=>run(async()=>{showLogin();await api("/api/logout",{method:"POST",body:{}});notice("已退出当前设备。");}));
$("space-select").addEventListener("change",event=>run(()=>switchSpace(event.target.value)));
for (const button of document.querySelectorAll("[data-tab]")) button.addEventListener("click",()=>run(()=>selectTab(button.getAttribute("data-tab"))));
for (const id of ["refresh-tasks","refresh-knowledge","refresh-devices"]) $(id).addEventListener("click",()=>run(refresh));
$("secret-close").addEventListener("click",closeSecret);
$("secret-dialog").addEventListener("close",()=>{input("secret-value").value="";$("secret-extra").textContent="";});
$("secret-copy").addEventListener("click",()=>run(async()=>{if(navigator.clipboard) {await navigator.clipboard.writeText(input("secret-value").value);$("secret-copy").textContent="已复制";} else {input("secret-value").focus();input("secret-value").select();}}));
document.addEventListener("visibilitychange",()=>{if(!document.hidden) run(refresh);});
setInterval(()=>{if(!state.me || document.hidden || refreshing || !["tasks","devices"].includes(state.tab)) return;refreshing=true;run(async()=>{try{await refresh();}finally{refreshing=false;}});},8000);
run(async()=>{if(await loadMe()) await refresh();});
