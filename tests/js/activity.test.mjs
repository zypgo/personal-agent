import assert from "node:assert/strict";
import { test } from "node:test";
import { activityLabel, connectionLabel } from "../../src/personal_agent/channels/web/static/activity.js";

test("waiting, final failure and completed work are distinct", () => {
  assert.equal(activityLabel({ state: "awaiting_approval" }), "Waiting for your approval");
  assert.equal(activityLabel({ state: "stopped" }), "Stopped after retries");
  assert.equal(activityLabel({ state: "completed" }), "Completed");
  assert.match(activityLabel({ state: "retrying", next_retry_at: 1_790_000_000 }), /Next retry/);
  assert.doesNotMatch(activityLabel({ state: "paused_budget", next_retry_at: null }), /1970|Invalid|Next retry/);
});

test("phone connection labels distinguish encryption and local Wi-Fi", () => {
  assert.equal(connectionLabel({ via: "tailscale", secure: true }), "Tailscale · encrypted HTTPS");
  assert.equal(connectionLabel({ via: "lan", secure: false }), "Local Wi-Fi · HTTP");
  assert.equal(connectionLabel({ via: "https", secure: true }), "Encrypted HTTPS");
});
