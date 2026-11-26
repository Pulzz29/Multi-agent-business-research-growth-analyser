// Chat widget helpers (pure; unit-tested).

export const CHAT_MAX_INPUT = 1000;
export const CHAT_CONVERSATION_KEY = "insightiq-chat-conversation";
const PENDING = new Set(["queued", "running"]);

/** Only plain http(s) links from server-provided sources; anything else is dropped. */
export function safeExternalUrl(value) {
  try {
    const url = new URL(String(value || ""));
    if ((url.protocol === "https:" || url.protocol === "http:") && !url.username && !url.password) return url.href;
  } catch {
    // not a URL
  }
  return null;
}

/** Suggestions from the latest assistant message (deduplicated, at most 5). */
export function latestSuggestions(messages) {
  const last = [...(messages || [])].reverse().find((m) => m.role === "assistant");
  const items = (last?.payload?.suggestions || []).filter((s) => typeof s === "string" && s.trim());
  return [...new Set(items)].slice(0, 5);
}

/** Research job shown by a message, when it still needs progress polling. */
export function pendingJob(messages) {
  const last = [...(messages || [])].reverse().find((m) => m.payload?.job?.run_id);
  const job = last?.payload?.job;
  return job && PENDING.has(job.status) ? job : null;
}

export function reportLabel(report) {
  const date = String(report?.date || "").slice(0, 10);
  return date ? `View report (${date})` : "View report";
}

/** Ticker whose on-demand report is still being generated (no result message yet). */
export function awaitingReport(messages) {
  const list = messages || [];
  for (let i = list.length - 1; i >= 0; i -= 1) {
    const m = list[i];
    if (m.role !== "assistant") continue;
    if (m.intent === "report_ready" || m.intent === "report_failed") return null;
    if (m.intent === "generating") return m.payload?.generating?.ticker || m.ticker || null;
  }
  return null;
}
