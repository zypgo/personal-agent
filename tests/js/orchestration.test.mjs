import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { FakeElement, elements, find, toHtml } from "./fake_dom.mjs";
import { createOrchestrationUI, buildCollaborationTask, secretReferences, wakeDescription, artifactPath, statusLabel, wakeStatus, connectionStatus, threadOf, arrangeThreads } from "../../src/personal_agent/channels/web/static/orchestration.js";

class Element extends FakeElement {
  constructor(tag) {
    super(tag);this.dataset={};this.value="";this.disabled=false;this.checked=false;this.hidden=false;
    this.classList={add:value=>this.className=`${this.className} ${value}`.trim(),toggle:(value,on)=>{const names=new Set(this.className.split(/\s+/).filter(Boolean));if(on)names.add(value);else names.delete(value);this.className=[...names].join(" ");}};
  }
  append(...kids){super.append(...kids);for(const child of this.children)child.parentElement=this;}
  replaceChildren(...kids){for(const child of this.children)child.parentElement=null;this.children=[];this.append(...kids);}
  contains(value){return this===value || elements(this).includes(value);}
  closest(tag){for(let item=this;item;item=item.parentElement)if(item.tagName===tag.toUpperCase())return item;return null;}
  querySelector(selector){return this.querySelectorAll(selector)[0] || null;}
  querySelectorAll(selector){return selector.includes("data-dirty")?find(this,"form").filter(f=>f.dataset.dirty==="1" || f.dataset.busy==="1"):selector==="form"?find(this,"form"):[];}
  get elements(){const controls=elements(this).filter(el=>["INPUT","SELECT","TEXTAREA","BUTTON"].includes(el.tagName));controls.namedItem=name=>controls.find(el=>el.name===name) || null;return controls;}
}
const agentsFixture=()=>[
  {id:"grok-a",name:"Grok 研究",kind:"grok",owner_id:"alice",state:"active",allowed_actor_ids:[],shared_environment:true,availability:"unknown",wake_mode:"manual"},
  {id:"muse-a",name:"Muse 整理",kind:"muse",owner_id:"bob",state:"active",shared_environment:true,availability:"unknown",wake_mode:"manual"},
];
const taskFixture=()=>({
  id:"task-a",title:"研究计划",actor_id:"alice",visibility:"private",state:"planned",revision:3,plan_hash:"confirmed-plan",created_at:100,
  steps:[{id:"s1",agent_id:"grok-a",prompt:"研究市场",acceptance:"注明来源",required:true,depends_on:[],state:"planned",attempt:null}],questions:[],
});
function setup({task=taskFixture(),agents=agentsFixture(),principal="alice",role="member",admin=false,respond,confirm=true}={}) {
  const ids=new Map();const get=id=>{if(!ids.has(id))ids.set(id,new Element("div"));return ids.get(id);};
  const document={createElement:tag=>new Element(tag),getElementById:get,activeElement:null};
  const context={epoch:0,space:"space-a",me:{principal:{id:principal,display_name:"Alice",is_admin:admin},role,spaces:[{id:"space-a",kind:"project"}]}};
  const calls=[],notices=[],secrets=[],downloads=[],opened=[];
  const api=async(path,options={})=>{
    calls.push({path,body:options.body,method:options.method || "GET"});
    const override=respond?.(path,options);if(override!==undefined)return await override;
    if(path==="/api/v4/agents")return options.method==="POST"?{agent:{id:"new-agent",owner_id:principal,...options.body},worker_token:"single-display-credential",connection:{instructions:"在连接设置中保存",mcp_url:"https://example.invalid/api/v4/mcp"}}:{agents};
    if(path.endsWith("/token"))return {agent:agents[0],worker_token:"rotated-single-display"};
    if(path==="/api/v3/members")return {members:[{principal_id:"alice",display_name:"Alice",role:"member"},{principal_id:"bob",display_name:"Bob",role:"member"}]};
    if(path==="/api/v4/tasks")return options.method==="POST"?{task}:{tasks:[task]};
    if(path===`/api/v4/tasks/${task.id}`)return {task};
    return {task};
  };
  const ui=createOrchestrationUI({api,getContext:()=>context,document,confirm:()=>confirm,randomUUID:()=>"stable-request",notice:(...args)=>notices.push(args),showSecret:(...args)=>secrets.push(args),onOpenNote:path=>opened.push(path),downloadArtifact:(...args)=>downloads.push(args)});
  return {ui,get,document,context,calls,notices,secrets,downloads,opened,task,agents};
}
const button=(root,text)=>find(root,"button").find(el=>el.textContent===text);
const submit=async form=>form.listeners.get("submit")({preventDefault(){}});
const click=async el=>el.listeners.get("click")();

test("parallel and sequential plans contain only explicit context, ordered dependencies and default private visibility",()=>{
  const fields={title:" Work ",prompt:"Provided context",acceptance:"Cite evidence",steps:[{agent_id:"grok",instruction:"Research"},{agent_id:"muse",instruction:"Synthesize"}]};
  const parallel=buildCollaborationTask({...fields,mode:"parallel"},"request");
  assert.equal(parallel.client_request_id,"request");assert.equal(parallel.visibility,"private");
  assert.deepEqual(parallel.steps.map(step=>step.depends_on),[[],[]]);
  assert.equal(parallel.steps[0].prompt,"Provided context\n\n本步骤要求：\nResearch");
  const serial=buildCollaborationTask({...fields,mode:"sequential",visibility:"space"},"request");
  assert.deepEqual(serial.steps[1].depends_on,["s1"]);assert.equal(serial.steps[1].required,true);
  assert.equal(serial.visibility,"space");assert.throws(()=>buildCollaborationTask({...fields,steps:[]},"request"),/选择/);
});

test("parallel research followed by synthesis waits for every earlier accepted result",()=>{
  const task=buildCollaborationTask({title:"Research",prompt:"Explicit context",acceptance:"Cite sources",mode:"research_then_synthesis",steps:[{agent_id:"a"},{agent_id:"b"},{agent_id:"c",instruction:"Write a final report"}]},"request");
  assert.deepEqual(task.steps.map(step=>step.depends_on),[[],[],["s1","s2"]]);
  assert.match(task.steps[2].prompt,/汇总全部已验收的前序成果/);assert.match(task.steps[2].prompt,/Write a final report/);
  assert.throws(()=>buildCollaborationTask({title:"R",prompt:"P",acceptance:"A",mode:"research_then_synthesis",steps:[{agent_id:"a"}]},"r"),/至少需要两个/);
});

test("credential references reject raw URLs, secrets and shell expressions",()=>{
  assert.deepEqual(secretReferences(" GROK_ROUTINE_URL ","GROK_KEY"),{webhook_url:"GROK_ROUTINE_URL",webhook_key:"GROK_KEY"});
  for(const raw of ["https://example.invalid/hook","$(cat key)","abc-token-value","secret\nTOKEN"])assert.throws(()=>secretReferences(raw,""),/环境变量名/);
  assert.deepEqual(secretReferences("",""),{});
});

test("Muse wake labels distinguish manual and existing native schedules",()=>{
  assert.match(wakeDescription({kind:"muse",wake_mode:"manual"}),/手动领取/);
  assert.match(wakeDescription({kind:"muse",wake_mode:"native_schedule"}),/原生定时领取/);
  assert.match(wakeDescription({kind:"cursor",wake_mode:"manual"}),/自有电脑执行/);
  assert.equal(statusLabel("planned"),"待确认计划");assert.equal(statusLabel("private_internal_code"),"等待状态更新");
});

test("connection observations expire honestly and wake acknowledgments never imply execution",()=>{
  assert.equal(connectionStatus({availability:"online",last_seen_at:100},1000),"等待下次响应");
  assert.equal(connectionStatus({availability:"online",last_seen_at:995},1000),"刚有响应");
  assert.match(wakeStatus({state:"sent"}).message,/等待 Agent 领取/);
  assert.match(wakeStatus({state:"uncertain"}).message,/不会自动重复触发/);
  assert.equal(wakeStatus({state:"uncertain",claimed_at:100}).attention,false);
});

test("agent creation is pull-only for ordinary members, limits sharing explicitly and shows credential once",async()=>{
  const {ui,get,calls,secrets}=setup();await ui.loadAgents();
  const form=find(get("agent-create-content"),"form")[0];
  assert.equal(form.elements.namedItem("webhook_url_env"),null);
  assert.equal(form.elements.namedItem("wake_mode").children.length,1);
  assert.equal(form.elements.namedItem("shared_environment").disabled,true);
  form.elements.namedItem("name").value="My Grok";
  const grant=form.elements.find(el=>el.name==="allowed_actor_ids");grant.checked=true;
  await submit(form);
  const sent=calls.find(call=>call.path==="/api/v4/agents" && call.method==="POST");
  assert.deepEqual(sent.body.secret_env,{});assert.equal(sent.body.wake_mode,"manual");assert.deepEqual(sent.body.allowed_actor_ids,["bob"]);
  assert.equal("availability" in sent.body,false);
  assert.equal(secrets.length,1);assert.equal(secrets[0][1],"single-display-credential");
  assert.doesNotMatch(get("agents-list").textContent+get("agent-create-content").textContent,/single-display-credential/);
  assert.match(get("agents-list").textContent,/已授权给你使用/);
  const source=readFileSync(new URL("../../src/personal_agent/channels/web/static/orchestration.js",import.meta.url),"utf8");
  assert.doesNotMatch(source,/localStorage|sessionStorage|console\./);
});

test("only agent owner receives rotation and revoke controls; revoked agents cannot be selected",async()=>{
  const agents=agentsFixture();agents[0].state="revoked";
  const {ui,get}=setup({agents,principal:"carol",role:"owner"});await ui.loadAgents();
  assert.equal(find(get("agents-list"),"button").length,0);
  await ui.loadTasks();const form=find(get("collaboration-compose-content"),"form")[0];
  assert.deepEqual(form.elements.filter(el=>el.name==="selected_agent").map(el=>el.value),["muse-a"]);
});

test("agent edits preserve existing capability declarations and use the full PATCH contract",async()=>{
  const agents=agentsFixture();agents[0]={...agents[0],capabilities:["claim"],wake_mode:"grok_routine",secret_env:{webhook_url:"OLD_URL",webhook_key:"OLD_KEY"}};
  const {ui,get,calls,secrets}=setup({agents,admin:true,respond:(path,options)=>path==="/api/v4/agents/grok-a" ? {agent:options.method==="PATCH"?{...agents[0],...options.body}:agents[0]} : undefined});
  await ui.loadAgents();await click(button(get("agents-list"),"编辑连接"));
  const form=find(get("agent-create-content"),"form")[0];assert.equal(form.elements.namedItem("webhook_url_env").value,"OLD_URL");
  form.elements.namedItem("webhook_url_env").value="NEW_URL";await submit(form);
  const sent=calls.find(call=>call.method==="PATCH");assert.equal(sent.path,"/api/v4/agents/grok-a");assert.deepEqual(sent.body.capabilities,["claim"]);
  assert.deepEqual(sent.body.secret_env,{webhook_url:"NEW_URL",webhook_key:"OLD_KEY"});assert.equal("availability" in sent.body,false);assert.equal(secrets.length,0);
  const ordinary=setup({agents});await ordinary.ui.loadAgents();assert.equal(button(ordinary.get("agents-list"),"编辑连接"),undefined);
});

test("new connections become selectable without losing a task draft or its lost-response request ID",async()=>{
  const agents=agentsFixture();const {ui,get}=setup({agents});await ui.loadTasks();
  const form=find(get("collaboration-compose-content"),"form")[0];form.elements.namedItem("title").value="Keep this draft";form.elements.namedItem("prompt").value="Explicit background";form.dataset.dirty="1";form.dataset.requestId="keep-retry-key";
  agents.push({id:"new",name:"New Agent",kind:"muse",state:"active"});await ui.loadTasks();
  const refreshed=find(get("collaboration-compose-content"),"form")[0];assert.notEqual(refreshed,form);
  assert.equal(refreshed.elements.namedItem("title").value,"Keep this draft");assert.equal(refreshed.elements.namedItem("prompt").value,"Explicit background");assert.equal(refreshed.dataset.requestId,"keep-retry-key");
  assert.ok(refreshed.elements.find(el=>el.name==="selected_agent" && el.value==="new"));
});

test("creating a plan never dispatches it; approval binds to its exact revision and hash",async()=>{
  const {ui,get,calls}=setup();await ui.loadTasks();
  const form=find(get("collaboration-compose-content"),"form")[0];
  form.elements.namedItem("title").value="Compare";form.elements.namedItem("prompt").value="Only this context";form.elements.namedItem("acceptance").value="Citations";
  const choice=form.elements.find(el=>el.name==="selected_agent");choice.checked=true;choice.listeners.get("change")();
  await submit(form);
  assert.equal(calls.filter(call=>call.path.endsWith("/approve")).length,0);
  const sent=calls.find(call=>call.path==="/api/v4/tasks" && call.method==="POST");
  assert.equal(sent.body.visibility,"private");assert.equal(sent.body.client_request_id,"stable-request");
  const approval=find(get("collaboration-detail"),"form").find(item=>button(item,"批准并派发"));
  assert.ok(approval);
  await submit(approval);
  assert.deepEqual(calls.find(call=>call.path.endsWith("/approve")).body,{revision:3,plan_hash:"confirmed-plan"});
});

test("a lost plan-create response retains its request ID for a deliberate retry and never auto-replays",async()=>{
  let attempts=0;const {ui,get,calls}=setup({respond:(path,options)=>{if(path==="/api/v4/tasks" && options.method==="POST"){attempts++;if(attempts===1)throw new TypeError("network");}}});
  await ui.loadTasks();const form=find(get("collaboration-compose-content"),"form")[0];
  for(const [name,value] of [["title","Research"],["prompt","Explicit context"],["acceptance","Evidence"]])form.elements.namedItem(name).value=value;
  const choice=form.elements.find(el=>el.name==="selected_agent");choice.checked=true;choice.listeners.get("change")();
  await submit(form);assert.equal(attempts,1);assert.equal(form.dataset.requestId,"stable-request");await submit(form);
  const writes=calls.filter(call=>call.path==="/api/v4/tasks" && call.method==="POST");assert.equal(writes.length,2);assert.deepEqual(writes[0].body,writes[1].body);
});

test("untrusted prompts, results, agent names and questions remain text; artifact links use owned authenticated paths",async()=>{
  const task=taskFixture();task.title='<img src=x onerror="attack()">';task.state="result_reported";
  task.steps[0]={...task.steps[0],state:"result_reported",prompt:"<script>attack()</script>",attempt:{id:"attempt-a",result:{text:"<svg onload=attack()>",artifacts:[{id:"file-a",name:"<img>.txt",download_url:"javascript:attack()"}]}}};
  task.questions=[{id:"q-a",state:"open",text:"<iframe src=attack>",choices:[]}];
  const {ui,get,downloads}=setup({task});await ui.loadTasks();ui.renderTask(task);
  const detail=get("collaboration-detail");for(const tag of ["img","script","svg","iframe"])assert.equal(find(detail,tag).length,0);
  assert.match(toHtml(detail),/&lt;script/);await click(button(detail,"下载 <img>.txt"));
  assert.deepEqual(downloads,[["/api/v4/artifacts/file-a","<img>.txt"]]);assert.equal(artifactPath({id:"../private"}),null);
});

test("reported results need explicit acceptance and bind the exact step, attempt and approved plan",async()=>{
  const task=taskFixture();task.state="result_reported";task.steps[0]={...task.steps[0],state:"result_reported",current_attempt:"attempt-a",attempt:{id:"attempt-a",result:{text:"Report",artifacts:[],outcome:"succeeded"}}};
  const {ui,get,calls}=setup({task});ui.renderTask(task);await click(button(get("collaboration-detail"),"验收通过"));
  assert.deepEqual(calls.find(call=>call.path.endsWith("/accept")).body,{step_id:"s1",attempt_id:"attempt-a",revision:3,plan_hash:"confirmed-plan"});
  assert.equal(task.steps[0].state,"result_reported");assert.match(get("collaboration-detail").textContent,/0 \/ 1 步已验收/);
});

test("private task owners and server capability flags govern action controls independently of project owner role",()=>{
  const task=taskFixture();task.questions=[{id:"q",state:"open",text:"A question",choices:[]}];
  const {ui,get}=setup({task,principal:"bob",role:"owner"});ui.renderTask(task);
  assert.equal(find(get("collaboration-detail"),"form").length,0);assert.equal(button(get("collaboration-detail"),"取消任务"),undefined);
  task.visibility="space";task.can_manage=true;task.can_approve=false;ui.renderTask(task);
  assert.ok(button(get("collaboration-detail"),"取消任务"));assert.equal(button(get("collaboration-detail"),"批准并派发"),undefined);
  task.can_approve=true;ui.renderTask(task);assert.ok(button(get("collaboration-detail"),"批准并派发"));
  const viewer=setup({task,principal:"alice",role:"viewer"});viewer.ui.renderTask(task);assert.equal(find(viewer.get("collaboration-detail"),"form").length,0);
});

test("shared-task reviewers see the actual recipient metadata even without an agent-use grant",async()=>{
  const task=taskFixture();task.visibility="space";task.can_manage=true;task.can_approve=true;task.steps[0]={...task.steps[0],agent_id:"unlisted",agent_name:"Bob's Grok",agent_kind:"grok",agent_owner_id:"bob",shared_environment:true};
  const {ui,get}=setup({task,agents:[],role:"owner"});await ui.loadTasks();ui.renderTask(task);
  assert.match(get("collaboration-detail").textContent,/Bob's Grok/);assert.match(get("collaboration-detail").textContent,/Grok · 所有者：Bob/);
});

test("uncertain execution offers no retry or acceptance, and queued dependencies remain blocked until accepted",async()=>{
  const task=taskFixture();task.state="uncertain";task.steps[0]={...task.steps[0],state:"uncertain",attempt:{id:"attempt-a",result:{text:"Late result",artifacts:[]}}};
  task.steps.push({id:"s2",agent_id:"muse-a",prompt:"Continue",acceptance:"Summary",state:"queued",depends_on:["s1"],required:true});
  const {ui,get}=setup({task});await ui.loadTasks();ui.renderTask(task);const detail=get("collaboration-detail");
  assert.match(detail.textContent,/不会自动重派/);assert.match(detail.textContent,/等待前序验收/);
  assert.equal(button(detail,"验收通过"),undefined);assert.equal(button(detail,"重试"),undefined);
});

test("cancel requests do not manufacture a terminal state",async()=>{
  const task=taskFixture();task.state="running";
  const {ui,get,calls,notices}=setup({task});ui.renderTask(task);await click(button(get("collaboration-detail"),"取消任务"));
  assert.ok(calls.find(call=>call.path.endsWith("/cancel")));assert.equal(task.state,"running");
  assert.match(notices.at(-1)[0],/已提交取消/);assert.match(get("collaboration-detail").textContent,/正在处理/);
});

test("failed remote results cannot be accepted even when another task step is eligible",()=>{
  const task=taskFixture();task.state="result_reported";task.can_accept=true;
  task.steps[0]={...task.steps[0],state:"result_reported",attempt:{id:"attempt-a",result:{text:"Failure",artifacts:[],outcome:"failed"}}};
  const {ui,get}=setup({task});ui.renderTask(task);
  assert.equal(button(get("collaboration-detail"),"验收通过"),undefined);
  assert.ok(button(get("collaboration-detail"),"拒绝此成果"));
});

test("stop confirmation needs the owner capability and names the current attempt",async()=>{
  const task=taskFixture();task.state="uncertain";task.steps[0]={...task.steps[0],state:"uncertain",can_confirm_stopped:true,attempt:{id:"attempt-a"}};
  const {ui,get,calls}=setup({task});ui.renderTask(task);
  const form=find(get("collaboration-detail"),"form").find(item=>button(item,"确认已停止"));
  await submit(form);
  assert.deepEqual(calls.find(call=>call.path.endsWith("/stopped")).body,{step_id:"s1",attempt_id:"attempt-a",note:"已确认停止"});
  task.steps[0].can_confirm_stopped=false;ui.renderTask(task);assert.equal(button(get("collaboration-detail"),"确认已停止"),undefined);
});

test("Grok wake uncertainty is visible and the owner can release a wake",async()=>{
  const task=taskFixture();task.state="queued";task.steps[0].state="queued";task.wakes=[{id:"wake-a",step_id:"s1",state:"uncertain",can_confirm_stopped:true}];
  const {ui,get,calls}=setup({task});await ui.loadTasks();ui.renderTask(task);
  assert.match(get("collaboration-list").textContent,/触发待核查/);assert.match(get("collaboration-detail").textContent,/未确认 Routine 是否收到触发/);
  const form=find(get("collaboration-detail"),"form").find(f=>button(f,"确认触发已结束"));
  await submit(form);
  assert.deepEqual(calls.find(call=>call.path.endsWith("/wakes/wake-a/stopped")).body,{note:"已确认停止"});
});

test("connection guide shows server-provided MCP, OpenAPI and Bridge addresses without retaining a credential",async()=>{
  const {ui,get,downloads}=setup({respond:path=>path==="/api/v4/agents/grok-a" ? {connection:{mcp_url:"https://example.invalid/mcp",openapi_url:"https://example.invalid/openapi.json",openapi_download_path:"/api/v4/agents/grok-a/openapi.json",bridge_url:"https://example.invalid/bridge.py",worker_token:"must-not-render"}} : undefined});
  await ui.loadAgents();const guide=find(get("agents-list"),"details")[0];guide.open=true;await guide.listeners.get("toggle")();
  const addresses=find(guide,"input").map(el=>el.value);
  assert.deepEqual(addresses,["https://example.invalid/mcp","https://example.invalid/openapi.json","https://example.invalid/bridge.py"]);
  assert.equal(find(guide,"button").filter(el=>el.textContent==="复制").length,3);
  assert.doesNotMatch(guide.textContent,/must-not-render/);
  await click(button(guide,"下载 OpenAPI 定义"));assert.deepEqual(downloads,[["/api/v4/agents/grok-a/openapi.json","Grok 研究-openapi.json"]]);
  ui.clear();assert.equal(get("agents-list").textContent,"");
});

test("resource owner can clear opaque occupancy without fetching another member's private task",async()=>{
  const {ui,get,calls}=setup({respond:path=>path==="/api/v4/agents/grok-a/occupancy" ? {attempts:[{task_id:"private-task",step_id:"s-private",attempt_id:"attempt-private",state:"uncertain",can_confirm_stopped:true,prompt:"must-not-render-private-prompt"}],wakes:[]} : undefined});
  await ui.loadAgents();const panel=find(get("agents-list"),"details").find(el=>el.children[0].textContent==="处理占用与失联");panel.open=true;await panel.listeners.get("toggle")();
  assert.doesNotMatch(panel.textContent,/must-not-render-private-prompt|private-task|attempt-private/);
  const form=find(panel,"form")[0];
  await submit(form);assert.deepEqual(calls.find(call=>call.path==="/api/v4/tasks/private-task/stopped").body,{step_id:"s-private",attempt_id:"attempt-private",note:"已确认停止"});
  assert.equal(calls.some(call=>call.path==="/api/v4/tasks/private-task"),false);
});

test("progress comes from cursor-based events for the current attempt and stays text",async()=>{
  const task=taskFixture();task.state="running";task.steps[0]={...task.steps[0],state:"running",attempt:{id:"attempt-a"}};
  const {ui,get,calls}=setup({task,respond:path=>path.includes("/events?") ? {cursor:9,events:[{id:9,event_type:"worker.progress",step_id:"s1",attempt_id:"attempt-a",data:{text:"Checked sources <img src=x>"}}]} : undefined});
  ui.renderTask(task);await ui.loadTasks(true);assert.match(get("collaboration-detail").textContent,/Checked sources/);assert.equal(find(get("collaboration-detail"),"img").length,0);
  await ui.loadTasks();assert.ok(calls.some(call=>call.path.endsWith("/events?after=9")));ui.clear();assert.equal(get("collaboration-detail").textContent,"");
});

test("periodic refresh preserves an unsent answer and answer POST targets the displayed question",async()=>{
  const task=taskFixture();task.state="waiting_input";task.questions=[{id:"q-a",state:"open",text:"Which format?",choices:[]}];
  const {ui,get,calls}=setup({task});ui.renderTask(task);
  const form=find(get("collaboration-detail"),"form").find(item=>item.elements.namedItem("answer"));form.elements.namedItem("answer").value="CSV";form.listeners.get("input")();
  await ui.loadTasks();assert.ok(find(get("collaboration-detail"),"form").includes(form));assert.equal(form.elements.namedItem("answer").value,"CSV");
  await ui.refreshTasks(true);assert.ok(find(get("collaboration-detail"),"form").includes(form));
  await submit(form);assert.deepEqual(calls.find(call=>call.path.endsWith("/questions/q-a/answer")).body,{answer:"CSV"});
});

test("question choices keep exact whitespace and an answered pause waits for the executor",async()=>{
  const task=taskFixture();task.state="waiting_input";task.steps[0].state="waiting_input";task.questions=[{id:"q-a",step_id:"s1",state:"open",text:"Format?",choices:[" CSV ","Text"]}];
  const {ui,get,calls}=setup({task});ui.renderTask(task);
  const form=find(get("collaboration-detail"),"form").find(f=>f.elements.namedItem("answer"));form.elements.namedItem("answer").value=" CSV ";await submit(form);
  assert.equal(calls.find(call=>call.path.endsWith("/answer")).body.answer," CSV ");
  task.questions[0].state="answered";task.questions[0].answer=" CSV ";ui.renderTask(task);
  assert.match(get("collaboration-detail").textContent,/等待 Agent 继续/);assert.doesNotMatch(get("collaboration-detail").textContent,/等待你的回答/);
  assert.equal(button(get("collaboration-detail"),"发送补充回答"),undefined);
});

test("switching space discards in-flight private reads and one-time token responses",async()=>{
  let release;const late=new Promise(resolve=>{release=resolve;});
  const {ui,get,context,secrets}=setup({respond:(path,options)=>path==="/api/v4/agents" && options.method==="POST"?late:undefined});
  await ui.loadAgents();const form=find(get("agent-create-content"),"form")[0];form.elements.namedItem("name").value="New Agent";
  const creating=submit(form);context.epoch++;context.space="space-b";ui.clear();release({agent:{name:"Old Space"},worker_token:"must-discard"});await creating;
  assert.equal(secrets.length,0);assert.equal(get("agents-list").textContent,"");assert.equal(get("collaboration-detail").hidden,true);
  let finishRead;const reading=new Promise(resolve=>{finishRead=resolve;});
  const other=setup({respond:path=>path==="/api/v4/agents"?reading:undefined});const pending=other.ui.loadAgents();other.context.epoch++;other.ui.clear();finishRead({agents:agentsFixture()});
  await assert.rejects(pending,{name:"AbortError"});assert.equal(other.get("agents-list").textContent,"");
});

test("reselecting the current collaboration task preserves an unanswered draft",async()=>{
  const task=taskFixture();task.state="waiting_input";task.steps[0].state="waiting_input";
  task.questions=[{id:"question-a",step_id:"s1",state:"open",text:"What should be included?",choices:[]}];
  const {ui,get,calls}=setup({task});
  await ui.loadTasks();ui.renderTask(task);
  const editor=find(get("collaboration-detail"),"form").find(item=>item.elements.namedItem("answer"));
  editor.elements.namedItem("answer").value="Keep my unfinished answer";editor.listeners.get("input")();
  await ui.loadTasks();
  const reads=calls.filter(call=>call.path===`/api/v4/tasks/${task.id}`).length;
  const selected=find(get("collaboration-list"),"button").find(item=>item.dataset.taskId===task.id);
  await click(selected);
  assert.ok(find(get("collaboration-detail"),"form").includes(editor));
  assert.equal(editor.elements.namedItem("answer").value,"Keep my unfinished answer");
  assert.equal(editor.dataset.dirty,"1");
  assert.equal(calls.filter(call=>call.path===`/api/v4/tasks/${task.id}`).length,reads);
});

for(const reduced of [true,false])test(`opening an agent editor ${reduced ? "honors reduced motion" : "uses the standard scroll transition"}`,async()=>{
  const {ui,get,document}=setup();
  const scrolls=[];
  document.defaultView={matchMedia:query=>({matches:query==="(prefers-reduced-motion:reduce)" && reduced})};
  get("agent-create").scrollIntoView=options=>scrolls.push(options);
  await ui.loadAgents();await click(button(get("agents-list"),"编辑连接"));
  assert.deepEqual(scrolls,[{behavior:reduced ? "auto" : "smooth",block:"start"}]);
  assert.equal(get("agent-create").open,true);
});

// ---- every task is a thread: the ones that need you come first, finished ones can be put away ----
const threaded=(id,state,thread={})=>({...taskFixture(),id,title:`任务 ${id}`,state,thread:{phase:"running",label:"进行中",need:null,archived:false,snoozed_until:null,note:null,...thread}});
const row=(root,title)=>find(root,"button").find(el=>el.className.includes("task-card") && el.textContent.includes(title));

test("tasks that need you come first and the filters count what they would show",()=>{
  const tasks=[
    threaded("a","running"),
    threaded("b","succeeded",{phase:"done",label:"完成了"}),
    threaded("c","awaiting_acceptance",{phase:"needs_you",label:"等你验收",need:"accept"}),
    threaded("d","succeeded",{phase:"archived",label:"完成了",archived:true}),
  ];
  const all=arrangeThreads(tasks);
  assert.deepEqual(all.shown.map(task=>task.id),["c","a","b"]);
  assert.deepEqual(all.counts,{all:3,needs:1,archived:1});
  assert.deepEqual(arrangeThreads(tasks,"needs").shown.map(task=>task.id),["c"]);
  assert.deepEqual(arrangeThreads(tasks,"archived").shown.map(task=>task.id),["d"]);
});

test("a server that does not describe the thread still gets a sensible list",()=>{
  const plain=taskFixture();
  assert.equal(threadOf(plain).phase,"running");
  assert.equal(threadOf({...plain,state:"succeeded"}).phase,"done");
  assert.equal(threadOf({...plain,state:"failed"}).phase,"closed");
  assert.equal(arrangeThreads([plain]).counts.archived,0);
});

test("the list shows the thread's own words, and the filter bar switches what is listed",async()=>{
  const tasks=[
    threaded("a","running"),
    threaded("c","awaiting_acceptance",{phase:"needs_you",label:"等你验收",need:"accept"}),
    threaded("d","succeeded",{phase:"archived",label:"完成了",archived:true}),
  ];
  const {ui,get}=setup({respond:(path,options)=>path==="/api/v4/tasks" && options.method!=="POST" ? {tasks} : undefined});
  await ui.loadTasks();
  const list=get("collaboration-list"),bar=get("thread-filter");
  assert.equal(bar.hidden,false);
  assert.deepEqual(find(bar,"button").map(el=>el.textContent),["全部","等你处理 1","已收起 1"]);
  assert.deepEqual(find(list,"h3").map(el=>el.textContent),["任务 c","任务 a"]);
  assert.match(row(list,"任务 c").textContent,/等你验收/);assert.match(row(list,"任务 a").textContent,/进行中/);
  await click(button(bar,"已收起 1"));
  assert.deepEqual(find(list,"h3").map(el=>el.textContent),["任务 d"]);
  assert.match(row(list,"任务 d").textContent,/已收起/);
  assert.equal(button(bar,"已收起 1").className,"selected");assert.equal(button(bar,"已收起 1").getAttribute("aria-pressed"),"true");
  await click(button(bar,"全部"));
  assert.deepEqual(find(list,"h3").map(el=>el.textContent),["任务 c","任务 a"]);
});

test("a filter with nothing left in it goes back to the full list",async()=>{
  const tasks=[threaded("a","running"),threaded("d","succeeded",{phase:"archived",archived:true})];
  const {ui,get}=setup({respond:(path,options)=>path==="/api/v4/tasks" && options.method!=="POST" ? {tasks} : undefined});
  await ui.loadTasks();await click(button(get("thread-filter"),"已收起 1"));
  tasks[1].thread.archived=false;tasks[1].thread.phase="done";
  await ui.loadTasks(true);
  assert.deepEqual(find(get("collaboration-list"),"h3").map(el=>el.textContent),["任务 a","任务 d"]);
  assert.deepEqual(find(get("thread-filter"),"button").map(el=>el.textContent),["全部"]);
});

test("when every task is put away the list says where they went and no tasks hides the bar",async()=>{
  const tasks=[threaded("d","succeeded",{phase:"archived",archived:true})];
  const {ui,get}=setup({respond:(path,options)=>path==="/api/v4/tasks" && options.method!=="POST" ? {tasks} : undefined});
  await ui.loadTasks();
  assert.match(get("collaboration-list").textContent,/已收起/);
  tasks.length=0;await ui.loadTasks(true);
  assert.equal(get("thread-filter").hidden,true);assert.match(get("collaboration-list").textContent,/还没有协作任务/);
});

test("a finished task can be put away and brought back from its detail",async()=>{
  const done=threaded("a","succeeded",{phase:"done",label:"完成了"});done.actor_id="alice";
  const {ui,get,calls,notices}=setup({task:done});
  ui.renderTask(done);
  assert.equal(button(get("collaboration-detail"),"先不提醒我"),undefined,"nothing is waiting on the owner");
  await click(button(get("collaboration-detail"),"收起"));
  assert.deepEqual(calls.find(call=>call.path==="/api/v4/tasks/a/archive").body,{});
  assert.match(notices.at(-1)[0],/已收起/);
  const away={...done,thread:{...done.thread,phase:"archived",archived:true}};
  ui.renderTask(away);
  assert.equal(button(get("collaboration-detail"),"收起"),undefined);
  await click(button(get("collaboration-detail"),"放回列表"));
  assert.ok(calls.some(call=>call.path==="/api/v4/tasks/a/unarchive"));
});

test("an open task that waits on you can be quieted for a day, once",async()=>{
  const waiting=threaded("a","awaiting_acceptance",{phase:"needs_you",label:"等你验收",need:"accept"});waiting.actor_id="alice";
  const {ui,get,calls,notices}=setup({task:waiting});
  ui.renderTask(waiting);
  assert.equal(button(get("collaboration-detail"),"收起"),undefined,"a running or waiting task is not put away");
  await click(button(get("collaboration-detail"),"先不提醒我"));
  assert.deepEqual(calls.find(call=>call.path==="/api/v4/tasks/a/snooze").body,{hours:24});
  assert.match(notices.at(-1)[0],/24 小时/);
  const quiet={...waiting,thread:{...waiting.thread,snoozed_until:Date.now()/1000+3600}};
  ui.renderTask(quiet);
  assert.equal(button(get("collaboration-detail"),"先不提醒我"),undefined);
  assert.match(get("collaboration-detail").textContent,/提醒推迟到/);
});

test("the thread note opens in the knowledge base, and people who cannot manage the task cannot put it away",async()=>{
  const done=threaded("a","succeeded",{phase:"done",label:"完成了",note:"threads/a.md"});done.actor_id="alice";
  const {ui,get,opened}=setup({task:done});
  ui.renderTask(done);
  await click(button(get("collaboration-detail"),"在知识库里看这条线索"));
  assert.deepEqual(opened,["threads/a.md"]);
  const other=setup({task:done,principal:"bob"});
  other.ui.renderTask(done);
  assert.equal(button(other.get("collaboration-detail"),"收起"),undefined);
  assert.ok(button(other.get("collaboration-detail"),"在知识库里看这条线索"),"reading is allowed");
  const viewer=setup({task:done,role:"viewer"});
  viewer.ui.renderTask(done);
  assert.equal(button(viewer.get("collaboration-detail"),"收起"),undefined);
});
