// Human-readable durable states, shared by the phone view and its tests.
const STATES = {
  queued: "Queued", running: "Working", awaiting_approval: "Waiting for your approval",
  paused_budget: "Waiting for a budget increase", retrying: "Waiting to retry",
  completed: "Completed", silent: "Completed without a message", failed: "Failed",
  stopped: "Stopped after retries", cancelled: "Cancelled",
};

export function activityLabel(item) {
  let label = STATES[item.state] ?? "Unknown state";
  if (Number.isFinite(item.next_retry_at)) {
    label += ` · ${item.state === "queued" ? "Available" : "Next retry"} ${new Date(item.next_retry_at * 1000).toLocaleString()}`;
  }
  return label;
}

export function connectionLabel(info) {
  if (info?.via === "tailscale") return "Tailscale · encrypted HTTPS";
  if (info?.secure) return "Encrypted HTTPS";
  if (info?.via === "lan") return "Local Wi-Fi · HTTP";
  return "This computer";
}
