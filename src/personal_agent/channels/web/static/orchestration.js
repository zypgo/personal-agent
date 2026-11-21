// Cloud collaboration UI: connected agents, tasks, steps and their results.
/** @typedef {import('./orchestration').Agent} Agent */
/** @typedef {import('./orchestration').Task} Task */

export const AGENT_KINDS = Object.freeze({grok:"Grok",muse:"Meta Muse",personal_agent:"栖序",codex:"Codex",cursor:"Cursor"});
const STATES = Object.freeze({
  planned:"待确认计划",draft:"待确认计划",awaiting_approval:"待确认计划",waiting_approval:"等待批准",approved:"已批准",
  pending:"等待前序验收",blocked:"等待前序验收",ready:"等待领取",queued:"等待领取",waiting_claim:"等待领取",
  claimed:"已领取",running:"正在处理",waiting_input:"等待你的回答",result_reported:"成果待验收",
  reported:"成果待验收",awaiting_acceptance:"成果待验收",accepted:"已验收",succeeded:"已完成",
  failed:"执行失败",rejected:"成果未通过",cancel_requested:"取消待确认",cancelling:"取消待确认",cancelled:"已取消",
  uncertain:"原执行待核查",stopped:"已确认停止",expired:"已过期",revoked:"授权已撤销",skipped:"前序未通过，已跳过",
});
const TASK_TERMINAL = new Set(["succeeded","failed","cancelled","expired","revoked","rejected"]);
const REVIEW_STATES = new Set(["result_reported","reported","awaiting_acceptance"]);
const PLAN_STATES = new Set(["planned","draft","awaiting_approval"]);
const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;
const STOPPED_NOTE = "已确认停止";
export const statusLabel = state => STATES[state] || "等待状态更新";

export function connectionStatus(agent, now=Date.now()/1000) {
  if(agent.revoked_at || agent.state==="revoked")return "已撤销";
  if(agent.availability==="offline")return "执行端离线";
  if(agent.last_seen_at)return now-Number(agent.last_seen_at)<120 ? "刚有响应" : "等待下次响应";
  return "等待首次连接";
}

export function wakeStatus(wake) {
  if(wake.claimed_at!=null)return {label:"已领取",message:"Agent 已领取任务。",attention:false};
  if(wake.state==="stopped")return {label:"已确认触发结束",message:"你已确认这次触发结束。这个任务不会再派一遍。",attention:false};
  if(wake.state==="uncertain")return {label:"触发待核查",message:"未确认 Routine 是否收到触发，请先到 Grok 里看看。不会自动重复触发。",attention:true};
  if(wake.state==="failed")return {label:"触发未成功",message:"Routine 触发未能完成。请检查连接配置和服务器环境变量，也可在远端手动领取。",attention:true};
  if(wake.state==="sent")return {label:"已触发，等待领取",message:"Routine 触发请求已送达，仍在等待 Agent 领取。",attention:false};
  return {label:"正在交接触发",message:"正在交接 Routine 触发，尚未确认领取。",attention:false};
}

export function wakeDescription(agent) {
  if (agent.kind === "muse") return ["native_schedule","muse_task"].includes(agent.wake_mode)
    ? "原生定时领取 · 先在 Muse 里配好定时任务。"
    : "手动领取 · 在 Muse 里启动后领取任务。";
  if (agent.kind === "grok") return ["grok_routine","verified_hook"].includes(agent.wake_mode)
    ? "Routine 触发 · 通过你配置的 Grok Routine 通知它来领取。"
    : "手动领取 · 在 Grok 启动已连接的任务工具后领取。";
  if (["codex","cursor"].includes(agent.kind)) return "自有电脑执行 · 需要一个能领取任务的执行端。";
  return "由你自己的栖序执行端领取任务并回传结果。";
}

/** Build the task from the form fields. */
export function buildCollaborationTask({title,prompt,acceptance,mode,steps,visibility="private",timeout_s=3600}, requestId) {
  if (!title?.trim() || !prompt?.trim() || !acceptance?.trim()) throw new Error("请填写任务名称、目标与验收标准。");
  if (!steps?.length || steps.length > 16) throw new Error("请选择 1 至 16 个 Agent。");
  if(mode==="research_then_synthesis" && steps.length<2)throw new Error("并行后汇总至少需要两个 Agent，最后一个负责汇总。");
  const ids = steps.map((_,index)=>`s${index + 1}`);
  return {
    client_request_id:requestId,title:title.trim(),visibility,timeout_s:Number(timeout_s),
    steps:steps.map((step,index)=>{
      const synthesis=mode==="research_then_synthesis" && index===steps.length-1;
      const instructions=[synthesis ? "汇总全部已验收的前序成果，并满足下方验收标准。" : "",step.instruction?.trim() || ""].filter(Boolean).join("\n");
      const text=instructions ? `${prompt.trim()}\n\n本步骤要求：\n${instructions}` : prompt.trim();
      const criteria=step.acceptance?.trim() || acceptance.trim();
      if(text.length>16000 || criteria.length>4000)throw new Error("每步外发内容请控制在 16000 字符内，验收标准在 4000 字符内。");
      return {id:ids[index],agent_id:step.agent_id,prompt:text,acceptance:criteria,depends_on:synthesis ? ids.slice(0,index) : mode === "sequential" && index > 0 ? [ids[index - 1]] : [],required:true};
    }),
  };
}

export function secretReferences(urlName, keyName) {
  const result = {};
  for (const [key,value] of [["webhook_url",urlName],["webhook_key",keyName]]) {
    const name = value?.trim();
    if (!name) continue;
    if (!ENV_NAME.test(name)) throw new Error("这里填环境变量名，例如 GROK_ROUTINE_URL，不是网址或密钥。");
    result[key] = name;
  }
  return result;
}

export function currentAttempt(step) {
  if (step.current_attempt && typeof step.current_attempt === "object") return step.current_attempt;
  return step.attempt || step.attempts?.find(item=>item.id === step.current_attempt) || null;
}

/** Download path for an artifact id. */
export function artifactPath(artifact) {
  const id = artifact.id || artifact.artifact_id;
  if (typeof id !== "string" || !/^[A-Za-z0-9_.:-]{1,128}$/.test(id)) return null;
  return `/api/v4/artifacts/${encodeURIComponent(id)}`;
}

export function createOrchestrationUI({api,getContext,notice,showSecret,downloadArtifact,onOpenAgents,document:doc=globalThis.document,confirm:confirmAction=globalThis.confirm,randomUUID=()=>globalThis.crypto.randomUUID()}) {
  const get = id=>doc.getElementById(id);
  const motionBehavior = ()=>(doc.defaultView || globalThis).matchMedia?.("(prefers-reduced-motion:reduce)").matches ? "auto" : "smooth";
  const state = {agents:[],members:[],lastTasks:[],task:null,taskId:null,steps:[],generation:0,agentsRead:0,tasksRead:0,detailRead:0,agentFormBuilt:false,taskFormBuilt:false,taskAgentsSignature:"",connections:new Map(),openConnections:new Set(),progress:new Map(),occupancies:new Map(),openOccupancies:new Set()};
  const context = ()=>getContext() || {};
  const key = ()=>{const c=context();return `${c.epoch}:${c.space}:${c.me?.principal?.id}:${c.me?.role}:${c.me?.principal?.authz_version ?? 0}:${state.generation}`;};
  const canWrite = ()=>Boolean(context().me && context().me.role !== "viewer");
  const isAdmin = ()=>Boolean(context().me?.principal?.is_admin);
  const owns = agent=>agent.owner_id === context().me?.principal?.id;
  const canManageTask = task=>canWrite() && (typeof task.can_manage === "boolean" ? task.can_manage : task.actor_id === context().me?.principal?.id);
  const canApproveTask = task=>canWrite() && (typeof task.can_approve === "boolean" ? task.can_approve : canManageTask(task));
  const canAcceptTask = task=>canWrite() && (typeof task.can_accept === "boolean" ? task.can_accept : canManageTask(task));
  const node = (tag,text="",className="")=>{const el=doc.createElement(tag);if(text)el.textContent=String(text);if(className)el.className=className;return el;};
  const field = (form,name)=>form.elements.namedItem(name);
  const label = (text,control)=>{const el=node("label",text);el.append(control);return el;};
  const input = (name,{type="text",value="",placeholder="",required=false,maxLength=200}={})=>{
    const el=node("input");Object.assign(el,{name,type,value,placeholder,required,maxLength});return el;
  };
  const textarea = (name,placeholder,rows=3,maxLength=8000)=>{const el=node("textarea");Object.assign(el,{name,placeholder,rows,maxLength});return el;};
  const select = (name,options)=>{const el=node("select");el.name=name;for(const [value,text] of options){const option=node("option",text);option.value=value;el.append(option);}el.value=options[0]?.[0] || "";return el;};
  const check = (name,text,checked=false)=>{const control=input(name,{type:"checkbox"});control.checked=checked;const el=label(text,control);el.className="or-check";return {el,control};};
  const badge = (value,text=statusLabel(value))=>{const el=node("span",text,"badge");if(["accepted","succeeded"].includes(value))el.classList.add("good");if(PLAN_STATES.has(value) || REVIEW_STATES.has(value) || value==="waiting_approval")el.classList.add("wait");if(["failed","uncertain","rejected","revoked"].includes(value))el.classList.add("bad");return el;};
  const when = value=>{if(!value)return "";const date=new Date(typeof value === "string" && !/^\d+(\.\d+)?$/.test(value)?value:Number(value)*(Number(value)>1e12?1:1000));return Number.isNaN(date.getTime())?"":date.toLocaleString("zh-CN",{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});};
  const agentName = id=>state.agents.find(agent=>agent.id===id)?.name || "已连接的 Agent";
  const agentById = id=>state.agents.find(agent=>agent.id===id);
  const agentForStep = step=>step.agent_name ? {...agentById(step.agent_id),name:step.agent_name,kind:step.agent_kind,owner_id:step.agent_owner_id,shared_environment:step.shared_environment} : agentById(step.agent_id);
  const stepAgentName = step=>agentForStep(step)?.name || "已连接的 Agent";
  const nativeShared = agent=>Boolean(agent && (agent.shared_environment || agent.kind==="grok"));
  const taskLabel = task=>task.state==="waiting_input" && !(task.questions || []).some(question=>question.state==="open") ? "等待 Agent 继续" : statusLabel(task.state);
  const choicesSignature = ()=>JSON.stringify(state.agents.map(agent=>[agent.id,agent.name,agent.kind,agent.state,agent.revoked_at,agent.shared_environment,agent.wake_mode]));
  const memberName = id=>id===context().me?.principal?.id ? "我" : state.members.find(member=>(member.principal_id || member.id)===id)?.display_name || "其他项目成员";
  const editing = root=>Boolean(root?.querySelector('form[data-dirty="1"],form[data-busy="1"]') || (root?.contains(doc.activeElement) && doc.activeElement?.closest("form")));
  const interrupted = ()=>new DOMException("Space changed","AbortError");
  async function request(path,options={}) {const before=key();const data=await api(path,options);if(before!==key())throw interrupted();return data;}
  function reportError(error,write=false) {
    if(error?.name === "AbortError")return;
    if(error?.status === 409)notice("任务状态已经变化，请刷新后再试。",true);
    else if(error?.status === 401 || error?.status === 403)notice("没有权限做这个操作。",true);
    else if(error?.status === 0 || error instanceof TypeError)notice(write ? "没收到服务器的回复，操作可能已经生效，请刷新看看。" : "连不上服务器，请稍后刷新。",true);
    else notice(error?.status ? "没有成功，请检查填写的内容后重试。" : error?.message || "没有成功，请稍后重试。",true);
  }
  function action(button,handler) {
    button.type="button";
    button.addEventListener("click",async()=>{
      if(button.disabled)return;
      const before=key();button.disabled=true;
      try{await handler();}catch(error){if(before===key()){reportError(error,true);if(error?.status===409 && state.taskId)await loadTask(true).catch(()=>{});}}
      finally{button.disabled=false;}
    });return button;
  }
  function bind(form,handler) {
    form.addEventListener("input",()=>{if(!form.dataset.busy){form.dataset.dirty="1";delete form.dataset.requestId;}});
    form.addEventListener("submit",async event=>{
      event.preventDefault();if(form.dataset.busy)return;
      const before=key();form.dataset.busy="1";
      const controls=[...form.elements].filter(el=>"disabled" in el),disabled=controls.map(el=>el.disabled);
      controls.forEach(el=>el.disabled=true);
      try{await handler(form);delete form.dataset.dirty;}
      catch(error){if(before===key()){reportError(error,true);if(error?.status===409 && state.taskId)await loadTask(true).catch(()=>{});}}
      finally{delete form.dataset.busy;controls.forEach((el,i)=>el.disabled=disabled[i]);}
    });
  }
  function disclosure(title,text) {const details=node("details","","or-disclosure");details.append(node("summary",title),node("p",text,"hint"));return details;}
  function secretDialog(data,agent) {
    if(!data.worker_token) {notice("连接已保存，但没有返回凭据。可以在这个 Agent 下重新生成。",true);return;}
    const connection=data.connection || {};
    const lines=[wakeDescription(agent)];
    lines.push("把凭据存成 Bearer Token，再用下面的地址接入。地址也能在 Agent 卡片的「接入指引」里再看。");
    for(const [name,labelText] of [["mcp_url","MCP 地址"],["openapi_url","OpenAPI 地址"],["bridge_url","Python Bridge 下载地址"]])if(typeof connection[name] === "string")lines.push(`${labelText}：${connection[name]}`);
    showSecret(`${agent.name} · 连接凭据`,data.worker_token,"只显示这一次，请先复制。",lines.join("\n\n"));
  }

  function clear() {
    state.generation++;state.agentsRead++;state.tasksRead++;state.detailRead++;
    state.agents=[];state.members=[];state.lastTasks=[];state.task=null;state.taskId=null;state.steps=[];state.agentFormBuilt=false;state.taskFormBuilt=false;state.taskAgentsSignature="";state.connections.clear();state.openConnections.clear();state.progress.clear();state.occupancies.clear();state.openOccupancies.clear();
    for(const id of ["agents-list","agent-create-content","collaboration-compose-content","collaboration-list","collaboration-detail"])get(id)?.replaceChildren();
    if(get("collaboration-detail"))get("collaboration-detail").hidden=true;
    if(get("agent-create"))get("agent-create").open=false;
    if(get("agent-create-summary"))get("agent-create-summary").textContent="连接 Agent";
    if(get("collaboration-compose"))get("collaboration-compose").open=false;
  }

  async function loadAgents() {
    const serial=++state.agentsRead;
    const [agents,members]=await Promise.all([request("/api/v4/agents"),request("/api/v3/members")]);
    if(serial!==state.agentsRead)return;
    state.agents=agents.agents || [];state.members=members.members || [];
    if(!editing(get("agents-list")))renderAgents();
    if(!state.agentFormBuilt)renderAgentForm();
  }
  function renderAgents() {
    const list=get("agents-list");list.replaceChildren();
    get("agent-create").hidden=!canWrite();
    for(const agent of state.agents) {
      const card=node("article","","card or-agent-card");
      const head=node("div","","or-card-heading"),copy=node("div");
      copy.append(node("p",AGENT_KINDS[agent.kind] || "Agent","eyebrow"),node("h2",agent.name));
      const availability=connectionStatus(agent);
      head.append(copy,node("span",availability,"badge"));card.append(head);
      const sharing=owns(agent) ? (agent.allowed_actor_ids?.length ? `已授权 ${agent.allowed_actor_ids.length} 位成员使用` : "仅自己使用") : "已授权给你使用";
      card.append(node("p",`所有者：${memberName(agent.owner_id)} · ${sharing}`,"muted"));
      card.append(node("p",wakeDescription(agent),"or-connection-note"));
      if(agent.last_seen_at)card.append(node("p",`最近响应 ${when(agent.last_seen_at)}`,"hint"));
      if(owns(agent) && !agent.revoked_at && agent.state !== "revoked")card.append(connectionGuide(agent));
      if(canWrite() && owns(agent))card.append(occupancyGuide(agent));
      if(canWrite() && owns(agent) && !agent.revoked_at && agent.state !== "revoked") {
        const actions=node("div","","actions");
        if(isAdmin() || !Object.keys(agent.secret_env || {}).length)actions.append(action(node("button","编辑连接","ghost"),async()=>{
          const data=await request(`/api/v4/agents/${encodeURIComponent(agent.id)}`);renderAgentForm(data.agent || agent);get("agent-create").open=true;get("agent-create").scrollIntoView?.({behavior:motionBehavior(),block:"start"});
        }));else card.append(node("p","此连接含实例环境配置，修改需要实例管理员权限。","hint"));
        actions.append(action(node("button","重新生成连接凭据","ghost"),async()=>{
          if(!confirmAction(`重新生成 ${agent.name} 的连接凭据？原凭据会马上失效。`))return;
          const data=await request(`/api/v4/agents/${encodeURIComponent(agent.id)}/token`,{method:"POST",body:{}});
          secretDialog(data,data.agent || agent);await loadAgents();
        }));
        actions.append(action(node("button","撤销连接","ghost danger"),async()=>{
          if(!confirmAction(`撤销 ${agent.name}？它以后不能再领任务。`))return;
          await request(`/api/v4/agents/${encodeURIComponent(agent.id)}/revoke`,{method:"POST",body:{}});notice("已撤销。");await loadAgents();
        }));card.append(actions);
      }
      list.append(card);
    }
    if(!state.agents.length) {
      const empty=node("section","","empty");empty.append(node("h3","还没有连接助手"),node("p","把你已有的 Grok、Muse 或其他 Agent 连进来，就可以把任务分给它们做。"));
      if(canWrite()) empty.append(action(node("button","连接第一个助手","primary"),async()=>{get("agent-create").open=true;get("agent-create-content").querySelector("input")?.focus?.();}));
      list.append(empty);
    }
  }
  function occupancyGuide(agent) {
    const ownerScope=key(),details=node("details","","or-disclosure"),content=node("div");details.open=state.openOccupancies.has(agent.id);
    details.append(node("summary","处理占用与失联"),content);
    async function refreshOccupancy() {
      const response=await request(`/api/v4/agents/${encodeURIComponent(agent.id)}/occupancy`);
      const keep=(record,fields)=>Object.fromEntries(fields.filter(field=>field in record).map(field=>[field,record[field]]));
      state.occupancies.set(agent.id,{
        attempts:(response.attempts || []).map(row=>keep(row,["task_id","step_id","attempt_id","state","desired_state","can_confirm_stopped"])),
        wakes:(response.wakes || []).map(row=>keep(row,["task_id","wake_id","state","can_confirm_stopped"])),
      });render();
    }
    function render() {
      const data=state.occupancies.get(agent.id);content.replaceChildren();
      content.append(node("p","这个连接正在占用的任务。到远端确认停了以后，在这里释放。","hint"));
      if(!data){content.append(node("p","正在读取占用状态…","hint"));return;}
      if(!data.attempts.length && !data.wakes.length)content.append(node("p","没有待处理的占用。","hint"));
      for(const [kind,rows] of [["attempt",data.attempts],["wake",data.wakes]])rows.forEach((row,index)=>{
        const record=node("section","","or-occupancy-record"),isWake=kind==="wake";
        const labelText=isWake ? wakeStatus(row).label : row.desired_state==="cancel" && row.state!=="uncertain" ? "取消待确认" : statusLabel(row.state);
        record.append(node("h3",`${isWake ? "触发" : "执行"} ${index+1} · ${labelText}`));
        if(!row.can_confirm_stopped || !canWrite()){record.append(node("p","暂时不能在这里释放，请先到远端确认状态。","hint"));content.append(record);return;}
        const form=node("form");
        const submit=node("button",isWake ? "确认触发已结束" : "确认执行已停止","danger");submit.type="submit";form.append(submit);
        bind(form,async()=>{
          const base=`/api/v4/tasks/${encodeURIComponent(row.task_id)}`;
          await request(isWake ? `${base}/wakes/${encodeURIComponent(row.wake_id)}/stopped` : `${base}/stopped`,{method:"POST",body:isWake ? {note:STOPPED_NOTE} : {step_id:row.step_id,attempt_id:row.attempt_id,note:STOPPED_NOTE}});
          notice("已释放占用。要继续的话，请重新建一个计划。");await refreshOccupancy();
        });record.append(form);content.append(record);
      });
      content.append(action(node("button","刷新占用状态","ghost"),refreshOccupancy));
    }
    details.addEventListener("toggle",async()=>{
      if(ownerScope!==key() || details.isConnected===false)return;
      if(!details.open){state.openOccupancies.delete(agent.id);return;}
      state.openOccupancies.add(agent.id);render();
      try{await refreshOccupancy();}catch(error){if(ownerScope===key()){content.replaceChildren(node("p","占用状态暂不可用，收起后重新打开可重试。","hint"));reportError(error);}}
    });if(details.open)render();return details;
  }
  function connectionGuide(agent) {
    const ownerScope=key();
    const details=node("details","","or-disclosure");details.open=state.openConnections.has(agent.id);
    const content=node("div");details.append(node("summary","接入指引与地址"),content);
    const render=()=>{
      const connection=state.connections.get(agent.id);content.replaceChildren();
      content.append(node("p","在执行环境里选一种接入方式，把凭据存成 Bearer Token。","hint"));
      content.append(node("p",wakeDescription(agent),"hint"));
      if(!connection){content.append(node("p","正在读取接入地址…","hint"));return;}
      const copyField=(title,text)=>{
        const row=node("div","","or-address"),control=input("",{value:text,maxLength:4000});control.readOnly=true;control.setAttribute("aria-label",title);
        row.append(label(title,control),action(node("button","复制","ghost"),async()=>{
          if(globalThis.navigator?.clipboard){await globalThis.navigator.clipboard.writeText(text);notice(`已复制${title}。`);}
          else {control.focus?.();control.select?.();notice("地址已选中，请复制。");}
        }));return row;
      };
      for(const [fieldName,title] of [["mcp_url","支持 MCP：添加此服务器"],["openapi_url","支持 HTTP 工具：导入此 OpenAPI"],["bridge_url","支持 Python：下载独立 Bridge 脚本"]]) {
        if(typeof connection[fieldName] === "string")content.append(copyField(title,connection[fieldName]));
      }
      const schemaPath=`/api/v4/agents/${encodeURIComponent(agent.id)}/openapi.json`;
      if(connection.openapi_download_path===schemaPath && downloadArtifact) {
        content.append(action(node("button","下载 OpenAPI 定义","ghost"),async()=>downloadArtifact(schemaPath,`${agent.name}-openapi.json`)));
        content.append(node("p","读取这个地址需要 Bearer 凭据。也可以下载定义文件，导入到 Muse 这类支持导入的工具里。","hint"));
      }
      if(connection.bridge_url)content.append(node("p","下载后运行 python bridge.py --help 查看用法。","hint"));
      if(typeof connection.instructions === "string") {
        const instructions=node("details","","or-disclosure");instructions.append(node("summary","给执行 Agent 的协议说明"),node("pre",connection.instructions));content.append(instructions);
      }
      if(!["mcp_url","openapi_url","bridge_url"].some(name=>connection[name]))content.append(node("p","接入地址暂不可用，请刷新后重试。","hint"));
    };
    details.addEventListener("toggle",async()=>{
      if(ownerScope!==key() || details.isConnected===false)return;
      if(!details.open){state.openConnections.delete(agent.id);return;}
      state.openConnections.add(agent.id);render();
      if(state.connections.has(agent.id))return;
      const before=key();
      try {
        const response=await request(`/api/v4/agents/${encodeURIComponent(agent.id)}`);
        const connection={};for(const name of ["mcp_url","openapi_url","openapi_download_path","bridge_url","instructions"])if(typeof response.connection?.[name]==="string")connection[name]=response.connection[name];
        state.connections.set(agent.id,connection);render();
      }catch(error){if(before===key()){content.replaceChildren(node("p","接入地址读取失败。收起后再打开可重试。","hint"));reportError(error);}}
    });
    if(details.open)render();return details;
  }
  function renderAgentForm(editAgent=null) {
    state.agentFormBuilt=true;const root=get("agent-create-content");root.replaceChildren();
    get("agent-create-summary").textContent=editAgent ? `编辑连接 · ${editAgent.name}` : "连接 Agent";
    if(!canWrite())return;
    const form=node("form");form.id="agent-create-form";
    const kind=select("kind",Object.entries(AGENT_KINDS));
    const name=input("name",{required:true,maxLength:100,placeholder:"例如：我的 Grok 研究助手"});
    form.append(label("Agent 名称",name),label("运行在哪里",kind),node("p",`所有者：${context().me?.principal?.display_name || "我"}`,"hint"));
    const mode=select("wake_mode",[["manual","手动领取"]]);const modeLabel=label("任务如何到达 Agent",mode),guidance=node("p","","or-connection-note");
    form.append(modeLabel,guidance);
    const shared=check("shared_environment","使用助手原有的运行环境",true);
    form.append(shared.el);
    const sharing=node("fieldset","","or-fieldset");sharing.append(node("legend","允许谁使用"));
    let members=0;
    for(const member of state.members) {
      const id=member.principal_id || member.id;
      if(id===context().me?.principal?.id || member.role==="viewer")continue;
      const choice=check("allowed_actor_ids",member.display_name || "项目成员",editAgent?.allowed_actor_ids?.includes(id));choice.control.value=id;sharing.append(choice.el);members++;
    }
    if(!members)sharing.append(node("p","当前空间没有其他可授权成员。","hint"));
    form.append(sharing);
    let envDetails=null;
    if(isAdmin()) {
      envDetails=node("details","","or-disclosure");envDetails.append(node("summary","已配置 Grok Routine？填写服务器环境变量名"));
      const url=input("webhook_url_env",{placeholder:"GROK_ROUTINE_URL",maxLength:128}),secret=input("webhook_key_env",{placeholder:"GROK_ROUTINE_KEY",maxLength:128});
      url.pattern=secret.pattern="[A-Za-z_][A-Za-z0-9_]{0,127}";url.autocomplete=secret.autocomplete="off";
      envDetails.append(node("p","填服务器上已设置的环境变量名。Routine 需要网址和认证密钥两个变量。","hint"),label("Routine 网址的环境变量名",url),label("认证密钥的环境变量名",secret));form.append(envDetails);
    } else form.append(node("p","要用 Routine 触发，需要实例管理员来配置。","hint"));
    const refreshMode=()=>{
      guidance.textContent=wakeDescription({kind:kind.value,wake_mode:mode.value});
      if(envDetails){const required=mode.value==="grok_routine";envDetails.open=required;field(form,"webhook_url_env").required=required;field(form,"webhook_key_env").required=required;}
    };
    const refreshKind=()=>{
      const options=kind.value==="muse" ? [["manual","手动领取"],["native_schedule","使用 Muse 已有原生定时任务"]] : kind.value==="grok" && isAdmin() ? [["manual","手动领取"],["grok_routine","使用已配置的 Grok Routine"]] : [["manual","由执行端主动领取"]];
      const replacement=select("wake_mode",options);mode.replaceChildren(...replacement.children);mode.value=options[0][0];
      shared.control.checked=["grok","muse"].includes(kind.value);
      shared.control.disabled=kind.value==="grok";
      if(envDetails){envDetails.hidden=kind.value!=="grok";envDetails.open=false;field(form,"webhook_url_env").value="";field(form,"webhook_key_env").value="";}
      refreshMode();
    };
    kind.addEventListener("change",refreshKind);mode.addEventListener("change",refreshMode);refreshKind();
    if(editAgent) {
      name.value=editAgent.name;kind.value=editAgent.kind;refreshKind();kind.disabled=true;mode.value=editAgent.wake_mode || "manual";
      shared.control.checked=nativeShared(editAgent);
      if(envDetails){field(form,"webhook_url_env").value=editAgent.secret_env?.webhook_url || "";field(form,"webhook_key_env").value=editAgent.secret_env?.webhook_key || "";}refreshMode();
      form.append(node("p","保存后，这个 Agent 的旧计划会失效。","or-attention"));
    }
    const submit=node("button",editAgent ? "保存修改" : "保存连接并生成凭据","primary");submit.type="submit";form.append(submit);
    if(editAgent)form.append(action(node("button","取消编辑","ghost"),async()=>{renderAgentForm();get("agent-create").open=false;}));
    bind(form,async()=>{
      const secret_env=isAdmin() && kind.value==="grok" ? secretReferences(field(form,"webhook_url_env").value,field(form,"webhook_key_env").value) : {};
      if(mode.value==="grok_routine" && (!secret_env.webhook_url || !secret_env.webhook_key))throw new Error("请填写 Routine 网址和认证密钥对应的环境变量名，或改用手动领取。");
      const allowed_actor_ids=[...form.elements].filter(el=>el.name==="allowed_actor_ids" && el.checked).map(el=>el.value);
      const body={name:name.value.trim(),kind:kind.value,allowed_actor_ids,shared_environment:shared.control.checked,capabilities:editAgent?.capabilities || [],secret_env,wake_mode:mode.value};
      const data=await request(editAgent ? `/api/v4/agents/${encodeURIComponent(editAgent.id)}` : "/api/v4/agents",{method:editAgent ? "PATCH" : "POST",body});
      if(!editAgent)secretDialog(data,data.agent || body);get("agent-create").open=false;state.agentFormBuilt=false;await loadAgents();notice(editAgent ? "连接已更新，旧计划已失效。" : "连接已保存。在助手那边配好以后就能领取任务。");
    });root.append(form);
  }

  async function loadTasks(force=false) {
    const serial=++state.tasksRead;
    const [tasks,agents,members]=await Promise.all([request("/api/v4/tasks"),request("/api/v4/agents"),request("/api/v3/members")]);
    if(serial!==state.tasksRead)return;
    state.agents=agents.agents || [];state.members=members.members || [];
    renderTaskList(tasks.tasks || []);
    get("collaboration-compose").hidden=!canWrite();
    if(!state.taskFormBuilt)renderTaskForm();
    else if(state.taskAgentsSignature!==choicesSignature()) {
      const form=get("collaboration-compose-content").querySelector("form");
      const draft=form ? {title:field(form,"title").value,prompt:field(form,"prompt").value,acceptance:field(form,"acceptance").value,mode:field(form,"mode").value,timeout_s:field(form,"timeout_s").value,visibility:field(form,"space_visibility")?.checked,dirty:form.dataset.dirty,requestId:form.dataset.requestId} : null;
      const count=state.steps.length;
      state.steps=state.steps.filter(step=>state.agents.some(agent=>agent.id===step.agent_id && !agent.revoked_at && agent.state!=="revoked"));
      if(draft && state.steps.length!==count)delete draft.requestId;
      renderTaskForm(draft);
    }
    if(state.taskId && (force || !editing(get("collaboration-detail"))))await loadTask(force);
  }
  function renderTaskList(tasks) {
    state.lastTasks=tasks;
    const root=get("collaboration-list");
    const focusedTask=root.contains(doc.activeElement) ? doc.activeElement?.closest("button")?.dataset.taskId : null;
    root.replaceChildren();
    for(const task of tasks) {
      const button=node("button","","task-card");button.dataset.taskId=task.id;button.classList.toggle("selected",state.taskId===task.id);
      button.append(node("h3",task.title || "协作任务"));const meta=node("div","","task-meta");
      meta.append(badge(task.state,taskLabel(task)),node("span",task.visibility==="space" ? "项目可见" : "私人"));
      const wake=(task.wakes || []).find(item=>item.claimed_at==null && ["failed","uncertain"].includes(item.state));if(wake)meta.append(badge(wake.state,wakeStatus(wake).label));
      if(task.steps?.length)meta.append(node("span",`${task.steps.filter(step=>step.state==="accepted").length}/${task.steps.length} 步已验收`));
      button.append(meta,node("p",when(task.updated_at || task.created_at),"hint"));
      action(button,async()=>{
        if(state.taskId===task.id && editing(get("collaboration-detail")))return;
        if(state.taskId!==task.id && editing(get("collaboration-detail")) && !confirmAction("切换任务会丢弃尚未提交的回答，继续吗？"))return;
        state.taskId=task.id;await loadTask(true);renderTaskList(tasks);
        get("collaboration-detail").scrollIntoView?.({behavior:motionBehavior(),block:"nearest"});
      });root.append(button);
      if(focusedTask===task.id) button.focus?.({preventScroll:true});
    }
    if(!tasks.length) {
      const empty=node("section","","empty");empty.append(node("h3","还没有协作任务"),node("p","写清楚目标，选好让谁来做。你确认计划之后，进度和结果会显示在这里。"));
      if(canWrite()) {
        const available=state.agents.some(agent=>!agent.revoked_at && agent.state!=="revoked");
        empty.append(action(node("button",available ? "交办第一项任务" : "先连接一个助手","primary"),async()=>{
          if(!available){await onOpenAgents?.();return;}
          get("collaboration-compose").open=true;get("collaboration-compose-content").querySelector("input")?.focus?.();
        }));
      }
      root.append(empty);
    }
  }
  function renderTaskForm(draft=null) {
    state.taskFormBuilt=true;const root=get("collaboration-compose-content");root.replaceChildren();if(!canWrite())return;
    state.taskAgentsSignature=choicesSignature();
    const form=node("form");form.id="collaboration-task-form";
    const title=input("title",{required:true,maxLength:200,placeholder:"例如：研究竞品并给出产品建议"});
    const prompt=textarea("prompt","写明目标和背景资料，会发给每个选中的 Agent。",5,12000);prompt.required=true;
    const acceptance=textarea("acceptance","例如：注明来源和日期；交付一份可下载的报告；列明未确认事项。",3,4000);acceptance.required=true;
    form.append(label("任务名称",title),label("目标与外发内容（最多 12000 字符）",prompt),label("验收标准（最多 4000 字符）",acceptance));
    const mode=select("mode",[["parallel","并行研究 · 各自独立完成"],["sequential","按顺序接力 · 前一步验收后继续"],["research_then_synthesis","并行后汇总 · 最后一个 Agent 汇总成果"]]);
    form.append(label("协作方式",mode));
    const choices=node("div","","or-agent-choices"),steps=node("div","","or-selected-steps");
    const choicesSection=node("fieldset","","or-fieldset");choicesSection.append(node("legend","选择参与的 Agent（最多 16 个）"),choices);
    const available=state.agents.filter(agent=>!agent.revoked_at && agent.state !== "revoked");
    if(!available.length) {
      choices.append(node("p","当前没有可用的 Agent 连接。","hint"));
      choices.append(action(node("button","去连接 Agent","ghost"),async()=>onOpenAgents?.()));
    }
    for(const agent of available) {
      const selected=check("selected_agent",agent.name,state.steps.some(step=>step.agent_id===agent.id));selected.control.value=agent.id;
      const meta=node("span",`${AGENT_KINDS[agent.kind] || "Agent"} · 所有者：${memberName(agent.owner_id)}`,"hint");selected.el.append(meta);
      selected.control.addEventListener("change",()=>{
        if(selected.control.checked && state.steps.length>=16){selected.control.checked=false;notice("每项任务最多选择 16 个 Agent。",true);return;}
        if(selected.control.checked)state.steps.push({agent_id:agent.id,instruction:""});else state.steps=state.steps.filter(step=>step.agent_id!==agent.id);
        delete form.dataset.requestId;form.dataset.dirty="1";renderSelectedSteps();
      });choices.append(selected.el);
    }
    function renderSelectedSteps() {
      steps.replaceChildren();
      state.steps.forEach((step,index)=>{
        const row=node("section","","or-step-draft"),header=node("div","","or-card-heading");
        const synthesis=mode.value==="research_then_synthesis" && index===state.steps.length-1;
        header.append(node("h3",`${index+1}. ${agentName(step.agent_id)}${synthesis ? " · 负责汇总" : ""}`));
        if(mode.value!=="parallel" && index>0)header.append(action(node("button","上移","ghost or-small"),async()=>{[state.steps[index-1],state.steps[index]]=[state.steps[index],state.steps[index-1]];delete form.dataset.requestId;renderSelectedSteps();}));
        row.append(header);
        const instruction=textarea(`instruction_${index}`,"可选：这个 Agent 特别负责什么",2,3500);instruction.value=step.instruction;
        instruction.addEventListener("input",()=>{step.instruction=instruction.value;delete form.dataset.requestId;});row.append(label("本步骤要求",instruction));
        if(mode.value==="sequential" && index>0)row.append(node("p",`在 ${agentName(state.steps[index-1].agent_id)} 的成果通过验收后领取；会接收前序成果。`,"hint"));
        if(synthesis)row.append(node("p","负责汇总前面所有 Agent 的成果；全部前序验收通过后领取，并接收这些成果。","hint"));
        steps.append(row);
      });
    }
    mode.addEventListener("change",()=>{delete form.dataset.requestId;renderSelectedSteps();});
    form.append(choicesSection,steps,node("p","提交后会生成计划，批准后才开始派发。","hint"));
    const visibility=check("space_visibility","项目成员可查看此任务（默认仅自己可见）");
    if(context().me?.spaces?.find(space=>space.id===context().space)?.kind==="project")form.append(visibility.el);
    const timeout=select("timeout_s",[["3600","1 小时"],["21600","6 小时"],["86400","24 小时"],["604800","7 天"]]);form.append(label("等待与执行期限",timeout));
    if(draft){title.value=draft.title;prompt.value=draft.prompt;acceptance.value=draft.acceptance;mode.value=draft.mode;timeout.value=draft.timeout_s;visibility.control.checked=Boolean(draft.visibility);if(draft.dirty)form.dataset.dirty=draft.dirty;if(draft.requestId)form.dataset.requestId=draft.requestId;}renderSelectedSteps();
    const submit=node("button","生成待确认计划","primary");submit.type="submit";form.append(submit);
    bind(form,async()=>{
      if(!form.dataset.requestId)form.dataset.requestId=randomUUID();
      const body=buildCollaborationTask({title:title.value,prompt:prompt.value,acceptance:acceptance.value,mode:mode.value,steps:state.steps,visibility:visibility.control.checked?"space":"private",timeout_s:timeout.value},form.dataset.requestId);
      const data=await request("/api/v4/tasks",{method:"POST",body});const task=data.task || data;
      state.taskId=task.id;state.steps=[];state.taskFormBuilt=false;get("collaboration-compose").open=false;
      notice("计划已保存，批准后开始派发。");await loadTasks(true);
    });root.append(form);
  }
  async function loadTask(force=false) {
    if(!state.taskId)return;
    const id=state.taskId,serial=++state.detailRead;
    const saved=state.progress.get(id) || {cursor:0,items:[]};
    const [data,events]=await Promise.all([
      request(`/api/v4/tasks/${encodeURIComponent(id)}`),
      request(`/api/v4/tasks/${encodeURIComponent(id)}/events?after=${saved.cursor}`).catch(error=>{if(error?.name==="AbortError" || [401,403].includes(error?.status))throw error;return null;}),
    ]);
    if(id!==state.taskId || serial!==state.detailRead || (!force && editing(get("collaboration-detail"))))return;
    if(events) {
      const additions=(events.events || []).filter(event=>["worker.progress","worker.started","worker.checkpoint"].includes(event.event_type) && typeof event.data?.text==="string");
      const cursor=Number.isSafeInteger(events.cursor) && events.cursor>=saved.cursor ? events.cursor : saved.cursor;
      state.progress.set(id,{cursor,items:[...saved.items,...additions].slice(-50)});
      if(state.progress.size>10)state.progress.delete(state.progress.keys().next().value);
    }
    renderTask(data.task || data);
  }
  function renderTask(task) {
    state.task=task;state.taskId=task.id;
    const root=get("collaboration-detail");root.replaceChildren();root.hidden=false;
    const heading=node("div","","or-card-heading");
    const close=action(node("button","返回列表","ghost detail-close"),async()=>{
      if(root.querySelector('form[data-dirty="1"],form[data-busy="1"]') && !confirmAction("关闭详情会放弃尚未提交的内容。继续吗？"))return;
      state.taskId=null;state.task=null;root.replaceChildren();root.hidden=true;renderTaskList(state.lastTasks);
      const previous=[...get("collaboration-list").querySelectorAll("button")].find(button=>button.dataset.taskId===task.id);previous?.focus?.();
    });
    heading.append(node("h2",task.title || "协作任务"),close);root.append(heading,badge(task.state,taskLabel(task)));
    const steps=task.steps || [],accepted=steps.filter(step=>step.state==="accepted").length;
    root.append(node("p",`${accepted} / ${steps.length} 步已验收 · ${task.visibility==="space" ? "项目成员可查看" : "仅你可查看"}`,"muted"));
    if(task.deadline_at)root.append(node("p",`任务期限：${when(task.deadline_at)}`,"hint"));
    const progress=node("progress");progress.max=Math.max(steps.length,1);progress.value=accepted;progress.setAttribute("aria-label",`已验收 ${accepted} 步，共 ${steps.length} 步`);root.append(progress);
    if(task.state==="uncertain" || steps.some(step=>step.state==="uncertain"))root.append(node("p","结果还没确认，请先到对应 Agent 那里看看。不会自动重派。","or-attention"));
    if(["cancel_requested","cancelling"].includes(task.state))root.append(node("p","取消请求已发出，仍在等待执行端确认停止。","or-attention"));
    if(["waiting_claim","queued","approved"].includes(task.state))root.append(node("p","等待领取。手动模式需要你在对应 Agent 里启动领取。","hint"));
    if(PLAN_STATES.has(task.state))renderPlanReview(root,task);
    for(const [index,step] of steps.entries())renderStep(root,task,step,index);
    for(const question of task.questions || [])renderQuestion(root,task,question);
    if(canManageTask(task) && !TASK_TERMINAL.has(task.state) && !["cancel_requested","cancelling"].includes(task.state)) {
      const actions=node("div","","actions");actions.append(action(node("button","取消任务","ghost danger"),async()=>{
        if(!confirmAction("取消这项任务？"))return;
        await request(`/api/v4/tasks/${encodeURIComponent(task.id)}/cancel`,{method:"POST",body:{}});notice("已提交取消。");await loadTasks(true);
      }));root.append(actions);
    }
  }
  function renderPlanReview(root,task) {
    const review=node("section","","or-plan-review");review.append(node("h3","确认这份派发计划"));
    review.append(node("p","下面是每一步发给 Agent 的内容和验收标准。","hint"));
    if(canApproveTask(task)) {
      const form=node("form");
      const approve=node("button","批准并派发","primary");approve.type="submit";form.append(approve);
      bind(form,async()=>{
        await request(`/api/v4/tasks/${encodeURIComponent(task.id)}/approve`,{method:"POST",body:{revision:task.revision,plan_hash:task.plan_hash}});
        notice("已批准，等 Agent 领取。");await loadTasks(true);
      });review.append(form);
    } else review.append(node("p","等待任务发起者批准。","hint"));
    root.append(review);
  }
  function renderStep(root,task,step,index) {
    const section=node("section","","or-step"),head=node("div","","or-card-heading");
    const waitingDependency=["queued","ready","waiting_claim"].includes(step.state) && step.depends_on?.some(id=>(task.steps || []).find(prior=>prior.id===id)?.state!=="accepted");
    const waitingAgent=step.state==="waiting_input" && !(task.questions || []).some(question=>question.step_id===step.id && question.state==="open");
    head.append(node("h3",`${index+1}. ${stepAgentName(step)}`),badge(waitingDependency ? "blocked" : step.state,waitingDependency ? statusLabel("blocked") : waitingAgent ? "等待 Agent 继续" : statusLabel(step.state)));section.append(head);
    const agent=agentForStep(step);
    if(agent)section.append(node("p",`${AGENT_KINDS[agent.kind] || "Agent"} · 所有者：${memberName(agent.owner_id)}`,"hint"));
    if(!waitingDependency && ["waiting_claim","queued","ready"].includes(step.state) && agent)section.append(node("p",wakeDescription(agent),"hint"));
    for(const wake of task.wakes || [])if(wake.step_id===step.id)renderWake(section,task,wake);
    if(step.depends_on?.length) {
      const prior=step.depends_on.map(id=>{const at=(task.steps || []).findIndex(item=>item.id===id);return at>=0?`第 ${at+1} 步`:"前序步骤";});
      section.append(node("p",`依赖：${prior.join("、")}验收通过${step.required===false ? " · 可选步骤" : ""}`,"hint"));
    }
    const plan=node("details","","or-disclosure");plan.open=PLAN_STATES.has(task.state);plan.append(node("summary","外发内容与验收标准"),node("p","外发内容","eyebrow"),node("pre",step.prompt || ""),node("p","验收标准","eyebrow"),node("p",step.acceptance || "","or-prewrap"));section.append(plan);
    const attempt=currentAttempt(step),result=step.result || attempt?.result;
    const event=state.progress.get(task.id)?.items.findLast(item=>item.step_id===step.id && item.attempt_id===attempt?.id);
    const message=event?.data.text || attempt?.progress_text || attempt?.progress?.message || step.progress_text;
    if(typeof message==="string") {
      section.append(node("p",`执行端进展${event?.created_at ? ` · ${when(event.created_at)}` : ""}`,"eyebrow"),node("p",message.length>500 ? `${message.slice(0,500)}…` : message,"or-prewrap"));
      if(message.length>500){const complete=node("details","","or-disclosure");complete.append(node("summary","查看完整进展"),node("pre",message));section.append(complete);}
    }
    if(result) {
      section.append(node("h3","提交的成果"),node("pre",typeof result==="string" ? result : result.text || result.summary || "已提交文件。","or-result"));
      if(result.outcome==="failed")section.append(node("p","执行端报告没有完成。","or-attention"));
      if(result.artifacts?.length) {
        const files=node("div","","or-files");
        for(const artifact of result.artifacts) {
          const path=artifactPath(artifact),name=artifact.name || artifact.filename || "成果文件";
          if(path && downloadArtifact)files.append(action(node("button",`下载 ${name}`,"ghost or-file"),async()=>downloadArtifact(path,name)));
          else files.append(node("p",`${name} · 文件暂不可下载`,"hint"));
        }section.append(files);
      }
      if(REVIEW_STATES.has(step.state) && canManageTask(task) && attempt?.id) {
        const actions=node("div","","actions");if(canAcceptTask(task) && result.outcome==="succeeded" && attempt.result_eligible!==false)actions.append(action(node("button","验收通过","primary"),async()=>{
          await request(`/api/v4/tasks/${encodeURIComponent(task.id)}/accept`,{method:"POST",body:{step_id:step.id,attempt_id:attempt.id,revision:task.revision,plan_hash:task.plan_hash}});notice("该步骤已验收。");await loadTasks(true);
        }));section.append(actions);
        const rejection=node("details","","or-disclosure");rejection.append(node("summary","成果未通过"));const form=node("form"),reason=textarea("reason","说明哪里没达到标准。",2,2000);reason.required=true;
        const submit=node("button","拒绝此成果","danger");submit.type="submit";form.append(label("未通过的原因",reason),submit);
        bind(form,async()=>{await request(`/api/v4/tasks/${encodeURIComponent(task.id)}/reject`,{method:"POST",body:{step_id:step.id,attempt_id:attempt.id,revision:task.revision,plan_hash:task.plan_hash,reason:reason.value.trim()}});notice("已标记未通过，依赖它的后续步骤不会开始。");await loadTasks(true);});rejection.append(form);section.append(rejection);
      }
    }
    if(canWrite() && step.can_confirm_stopped && attempt?.id) {
      const stopped=node("details","","or-disclosure");stopped.append(node("summary","确认已停止"),node("p","到对应 Agent 或电脑上停掉这次执行后，在这里释放占用。","hint"));
      const form=node("form");
      const submit=node("button","确认已停止","danger");submit.type="submit";form.append(submit);
      bind(form,async()=>{
        await request(`/api/v4/tasks/${encodeURIComponent(task.id)}/stopped`,{method:"POST",body:{step_id:step.id,attempt_id:attempt.id,note:STOPPED_NOTE}});
        notice("已释放占用。要继续的话，请新建一个计划。");await loadTasks(true);
      });stopped.append(form);section.append(stopped);
    }
    if(["failed","rejected","revoked","expired","skipped"].includes(step.state) && !result)section.append(node("p","该步骤没有可验收成果。依赖它的后续步骤不会自动执行。","hint"));
    root.append(section);
  }
  function renderQuestion(root,task,question) {
    const section=node("section","","task-question");section.append(node("h3",question.state==="open" ? "需要你补充" : question.state==="answered" ? "已补充的信息" : "已关闭的问题"),node("p",question.text || question.question || ""));
    if(question.state!=="open"){if(question.answer)section.append(node("p",question.answer,"or-prewrap"));if(question.state==="answered" && (task.steps || []).some(step=>step.id===question.step_id && step.state==="waiting_input"))section.append(node("p","回答已发送，等待执行端读取并继续。","hint"));root.append(section);return;}
    if(!canManageTask(task)){section.append(node("p","等待任务发起者回答。","hint"));root.append(section);return;}
    const form=node("form"),answer=question.choices?.length ? select("answer",[["","请选择"],...question.choices.map(choice=>[String(choice),String(choice)])]) : textarea("answer","补充的信息会发送给提出问题的 Agent。",3,8000);answer.required=true;
    const submit=node("button","发送补充回答","primary");submit.type="submit";form.append(label("你的回答",answer),submit);
    bind(form,async()=>{if(!answer.value.trim())throw new Error("请填写回答。");await request(`/api/v4/tasks/${encodeURIComponent(task.id)}/questions/${encodeURIComponent(question.id)}/answer`,{method:"POST",body:{answer:question.choices?.length ? answer.value : answer.value.trim()}});notice("已发送补充回答。");await loadTasks(true);});section.append(form);root.append(section);
  }
  function renderWake(root,task,wake) {
    const status=wakeStatus(wake);root.append(node("p",status.message,status.attention ? "or-attention" : "hint"));
    if(!canWrite() || !wake.can_confirm_stopped)return;
    const details=node("details","","or-disclosure");details.append(node("summary","确认触发已结束"),node("p","到远端确认这次触发没有执行或已经停掉后，在这里释放占用。","hint"));
    const form=node("form");
    const submit=node("button","确认触发已结束","danger");submit.type="submit";form.append(submit);
    bind(form,async()=>{
      await request(`/api/v4/tasks/${encodeURIComponent(task.id)}/wakes/${encodeURIComponent(wake.id || wake.wake_id)}/stopped`,{method:"POST",body:{note:STOPPED_NOTE}});
      notice("已确认触发结束。要继续的话，请新建一个计划。");await loadTasks(true);
    });details.append(form);root.append(details);
  }
  async function refreshTasks(force=false) {
    if(force && editing(get("collaboration-detail"))){notice("已保留尚未提交的内容；提交后会刷新任务详情。");await loadTasks(false);}
    else await loadTasks(force);
  }
  return {loadAgents,loadTasks,refreshTasks,clear,renderTask,renderAgents,get selectedTaskId(){return state.taskId;}};
}
