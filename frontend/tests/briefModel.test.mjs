import { test } from "node:test";
import assert from "node:assert/strict";
import { panelState, shouldAutoStart } from "../src/lib/briefModel.js";

const opts = (o = {}) => ({ isLatest: true, alreadyStarted: false, ...o });

test("auto-generation only for the latest report when nothing current exists", () => {
  assert.equal(shouldAutoStart({ status: "none", job_status: "none" }, opts()), true);
  assert.equal(shouldAutoStart({ status: "completed", is_current: false, job_status: "done" }, opts()), true);
  assert.equal(shouldAutoStart({ status: "completed", is_current: true, job_status: "done" }, opts()), false);
  assert.equal(shouldAutoStart({ status: "none" }, opts({ isLatest: false })), false, "older reports keep the button");
  assert.equal(shouldAutoStart({ status: "none" }, opts({ alreadyStarted: true })), false, "once per view");
});

test("no automatic retry loop after a failure and none while running", () => {
  assert.equal(shouldAutoStart({ status: "unavailable", job_status: "failed" }, opts()), false);
  assert.equal(shouldAutoStart({ status: "completed", is_current: false, job_status: "failed" }, opts()), false);
  assert.equal(shouldAutoStart({ status: "generating", job_status: "running" }, opts()), false);
  assert.equal(shouldAutoStart(null, opts()), false);
});

test("panel states: loading, generating, summary, failed with previous version, empty", () => {
  assert.equal(panelState(null, null).view, "loading");
  assert.equal(panelState({ status: "generating", job_status: "running", summary: null }, null).view, "generating");
  const done = { status: "completed", sections: [], research: { status: "completed", sections: [{ section: "outlook" }] } };
  const ok = panelState({ status: "completed", job_status: "done", summary: done }, null);
  assert.equal(ok.view, "summary");
  assert.equal(ok.research.sections.length, 1);
  // A failed regeneration keeps the previous successful version visible.
  const kept = panelState({ status: "completed", job_status: "failed", summary: done }, null);
  assert.equal(kept.view, "summary");
  assert.equal(kept.failed, true);
  assert.equal(panelState({ status: "unavailable", job_status: "failed", summary: { status: "unavailable" } }, null).view, "failed");
  assert.equal(panelState({ status: "none", job_status: "none", summary: null }, null).view, "empty");
  // Regenerating while a summary exists keeps showing it.
  const regen = panelState({ status: "generating", job_status: "running", summary: done }, null);
  assert.equal(regen.view, "summary");
  assert.equal(regen.running, true);
});
