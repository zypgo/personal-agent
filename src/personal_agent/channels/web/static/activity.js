// Human-readable durable states, shared by the phone view and its tests.
const STATES = {
  queued: "等待处理", running: "正在处理", awaiting_approval: "等待你确认",
  paused_budget: "等待增加额度", retrying: "等待重试",
  completed: "已完成", silent: "已完成，未发送消息", failed: "未能完成",
  stopped: "重试后已停止", cancelled: "已取消",
};

export function activityLabel(item) {
  let label = STATES[item.state] ?? "状态暂不可用";
  if (Number.isFinite(item.next_retry_at)) {
    label += ` · ${item.state === "queued" ? "可开始于" : "下次重试"} ${new Date(item.next_retry_at * 1000).toLocaleString("zh-CN")}`;
  }
  return label;
}

export function connectionLabel(info) {
  if (info?.via === "tailscale") return "Tailscale · 加密连接";
  if (info?.secure) return "加密连接";
  if (info?.via === "lan") return "本地 Wi-Fi · 未加密";
  return "当前电脑";
}

// What the assistant is doing, in words a person would use. Tool names are an implementation
// detail; an unfamiliar one (a plugin's, for instance) is described by what it is, not by its id.
const TOOLS = {
  web_search: "搜索网页", web_fetch: "阅读网页", http_request: "请求网络服务",
  shell: "运行命令", list_files: "查看文件", read_file: "阅读文件", write_file: "写入文件", edit_file: "修改文件",
  memory: "整理记忆", schedule: "安排提醒", send_message: "发送消息", publish_artifact: "准备文件",
  search_history: "回顾对话", search_tool_history: "回顾操作记录", skill: "使用技能",
  submit_job: "安排后台任务", job_status: "查看任务进度", cancel_job: "取消任务", ask_task_input: "询问所需信息",
  list_agents: "查看可用助手", delegate_agents: "交给助手协作", agent_task_status: "查看协作进度", cancel_agent_task: "取消协作任务",
  workspace_capabilities: "查看工作空间", inspect_workspace: "查看工作空间", prepare_workspace_action: "准备工作空间操作",
};

/** "运行命令": the action, for a label or an approval. */
export function toolLabel(name) {
  return (typeof name === "string" && Object.prototype.hasOwnProperty.call(TOOLS, name)) ? TOOLS[name] : "使用工具";
}

/** "正在运行命令…": the action, while it happens. Without a tool it is simply "处理". */
export function toolProgress(name) {
  return typeof name === "string" && name ? `正在${toolLabel(name)}…` : "正在为你处理…";
}
