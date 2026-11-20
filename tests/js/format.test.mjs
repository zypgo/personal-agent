process.env.TZ = "UTC";

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  bytesLabel,
  clip,
  clockTime,
  commandHints,
  dayKey,
  dayLabel,
  durationLabel,
  expiresIn,
  newClientId,
  parsePairing,
} from "../../src/personal_agent/channels/web/static/format.js";

const at = (iso) => Date.parse(iso) / 1000;

test("times are written the way the locale writes them", () => {
  assert.equal(clockTime(at("2026-11-15T14:05:00Z"), "en-GB"), "14:05");
  assert.match(clockTime(at("2026-11-15T14:05:00Z"), "en-US"), /^2:05\s?PM$/);
});

test("days are calendar days in the owner's own time", () => {
  assert.equal(dayKey(at("2026-11-15T23:59:00Z")), "2026-11-15");
  assert.equal(dayKey(at("2026-11-16T00:00:00Z")), "2026-11-16");
});

test("today, yesterday, then a date; the year only when it is not this one", () => {
  const now = at("2026-11-15T10:00:00Z");
  assert.equal(dayLabel(at("2026-11-15T00:01:00Z"), now, "en-GB"), "今天");
  assert.equal(dayLabel(at("2026-11-14T23:59:00Z"), now, "en-GB"), "昨天");
  assert.equal(dayLabel(at("2026-11-10T12:00:00Z"), now, "en-GB"), "Tue 10 Nov");
  assert.match(dayLabel(at("2025-12-24T12:00:00Z"), now, "en-GB"), /^Wed,? 24 Dec 2025$/); // ICU decides the comma
});

test("yesterday is the day before even when the clocks changed", () => {
  // (the machine running the test is in UTC, so this checks the arithmetic, not a zone)
  const now = at("2026-03-29T00:30:00Z");
  assert.equal(dayLabel(at("2026-03-28T23:00:00Z"), now, "en-GB"), "昨天");
});

test("lengths of time", () => {
  assert.equal(durationLabel(0), "0:00");
  assert.equal(durationLabel(7.9), "0:07");
  assert.equal(durationLabel(125), "2:05");
  assert.equal(durationLabel(-5), "0:00");
});

test("sizes", () => {
  assert.equal(bytesLabel(900), "900 B");
  assert.equal(bytesLabel(12_345), "12 kB");
  assert.equal(bytesLabel(3_456_789), "3.5 MB");
});

test("how long until something expires", () => {
  assert.equal(expiresIn(100, 100), "已过期");
  assert.equal(expiresIn(90, 100), "已过期");
  assert.equal(expiresIn(145, 100), "45 秒");
  assert.equal(expiresIn(100 + 600, 100), "10 分钟");
  assert.equal(expiresIn(100 + 3 * 3600, 100), "3 小时");
  assert.equal(expiresIn(100 + 3 * 86400, 100), "3 天");
});

test("a pairing code is found in a link, in a fragment, or on its own", () => {
  const code = "Abcdefghijklmnopqrstuvwxyz012345";
  assert.equal(parsePairing(code), code);
  assert.equal(parsePairing(`  ${code}\n`), code);
  assert.equal(parsePairing(`http://127.0.0.1:8765/#pair=${code}`), code);
  assert.equal(parsePairing(`https://mac.tail1234.ts.net/#pair=${code}&x=1`), code);
  assert.equal(parsePairing(`#pair=${code}`), code);
  assert.equal(parsePairing(`Here you go: https://m.ts.net/#pair=${code} (valid 10 minutes)`), code);
  assert.equal(parsePairing(`pair=${code}`), null); // not in a fragment, not a bare code
});

test("what is not a pairing code is not taken for one", () => {
  for (const bad of ["", "   ", null, undefined, "short", "has spaces in it, so no", "a".repeat(200), "<script>alert(1)</script>"]) {
    assert.equal(parsePairing(bad), null, String(bad));
  }
});

test("client ids are what the server accepts, and they differ", () => {
  const seen = new Set();
  for (let i = 0; i < 100; i++) {
    const id = newClientId();
    assert.match(id, /^[A-Za-z0-9_-]{8,64}$/);
    seen.add(id);
  }
  assert.equal(seen.size, 100);
  assert.equal(newClientId({ getRandomValues: (b) => b.fill(0xab) }), "ab".repeat(12));
});

test("command hints appear while a command word is being typed, and only then", () => {
  const names = (text) => commandHints(text).map(([name]) => name);
  assert.deepEqual(names("/"), ["/new", "/status", "/approvals", "/approve", "/deny", "/help"]);
  assert.deepEqual(names("/ap"), ["/approvals", "/approve"]);
  assert.deepEqual(names("/APP"), ["/approvals", "/approve"]);
  assert.deepEqual(names("/status"), []); // already typed in full
  assert.deepEqual(names("/approve 3"), []); // arguments: no longer a word
  assert.deepEqual(names("hello /ap"), []);
  assert.deepEqual(names(""), []);
});

test("clipping prefers the end of a word", () => {
  assert.equal(clip("short", 10), "short");
  assert.equal(clip("the quick brown fox jumps", 12), "the quick…");
  assert.equal(clip("abcdefghijklmnopqrstuvwxyz", 10), "abcdefghij…");
});
