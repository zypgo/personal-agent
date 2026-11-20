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
