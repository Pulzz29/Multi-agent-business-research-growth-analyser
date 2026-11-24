// Pure decisions for the AI summary + thorough research panel (ReportViewer).

// Automatic generation runs only for the latest report, only when nothing
// current exists, never while one runs, and never right after a failure (the
// user retries explicitly), so viewing reports does not repeatedly spend tokens.
export function shouldAutoStart(state, { isLatest, alreadyStarted }) {
  if (!isLatest || alreadyStarted || !state) return false;
  if (state.status === "generating") return false;
  if (state.job_status === "failed") return false;
  if (state.status === "none") return true;
  return state.status === "completed" && state.is_current === false;
}

// What the panel shows: "loading" | "generating" | "summary" | "failed" | "empty".
export function panelState(state, summary) {
  const record = state?.summary || summary || null;
  const completed = record?.status === "completed";
  if (!state && !record) return { view: "loading", completed: false, failed: false, running: false };
  const running = state?.status === "generating";
  const failed = state?.job_status === "failed" && !running;
  let view = "empty";
  if (completed) view = "summary";
  else if (running) view = "generating";
  else if (failed || record?.status === "unavailable") view = "failed";
  return { view, completed, failed, running, record, research: completed ? record.research || null : null };
}
