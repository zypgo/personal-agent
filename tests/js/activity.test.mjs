import assert from "node:assert/strict";
import { test } from "node:test";
import { activityLabel, connectionLabel, toolLabel, toolProgress } from "../../src/personal_agent/channels/web/static/activity.js";

test("waiting, final failure and completed work are distinct", () => {
  assert.equal(activityLabel({ state: "awaiting_approval" }), "等待你确认");
  assert.equal(activityLabel({ state: "stopped" }), "重试后已停止");
  assert.equal(activityLabel({ state: "completed" }), "已完成");
  assert.match(activityLabel({ state: "retrying", next_retry_at: 1_790_000_000 }), /下次重试/);
  assert.doesNotMatch(activityLabel({ state: "paused_budget", next_retry_at: null }), /1970|Invalid|下次重试/);
});

test("phone connection labels distinguish encryption and local Wi-Fi", () => {
  assert.equal(connectionLabel({ via: "tailscale", secure: true }), "Tailscale · 加密连接");
  assert.equal(connectionLabel({ via: "lan", secure: false }), "本地 Wi-Fi · 未加密");
  assert.equal(connectionLabel({ via: "https", secure: true }), "加密连接");
});

test("tools are described by what they do, and unfamiliar ones never leak their identifiers", () => {
  assert.equal(toolLabel("shell"), "运行命令");
  assert.equal(toolProgress("web_search"), "正在搜索网页…");
  assert.equal(toolProgress(null), "正在为你处理…");
  assert.equal(toolProgress(""), "正在为你处理…");
  assert.equal(toolLabel("plugin__demo__lookup"), "使用工具");
  assert.equal(toolProgress("plugin__demo__lookup"), "正在使用工具…");
  for (const inherited of ["constructor", "__proto__", "toString", "hasOwnProperty"]) assert.equal(toolLabel(inherited), "使用工具");
  assert.equal(toolLabel(42), "使用工具");
});
