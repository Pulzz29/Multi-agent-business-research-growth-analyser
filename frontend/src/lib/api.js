import { csrfHeaders } from "./auth.js";

const BASE = "/api/v1";

const cookies = () => (typeof document === "undefined" ? "" : document.cookie);

export class ApiError extends Error {
  constructor(message, status, code = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

const STATUS_MESSAGES = {
  401: "Your session has ended. Sign in again.",
  404: "That record could not be found.",
  409: "The report is not ready yet.",
  422: "Please check the selected information.",
  429: "The service is busy. Try again shortly.",
  500: "Something went wrong on our side. Please try again in a moment.",
  502: "The AI provider could not complete the request.",
  503: "The service is temporarily unavailable.",
};

// Authentication is the HttpOnly session cookie set by /auth/login; the
// `token` argument kept in every call is unused (pages pass a signed-in flag).
export const REQUEST_TIMEOUT_MS = 120000;

/** An AbortSignal that also fires after the timeout (requests never hang). */
export function timeoutSignal(signal, ms = REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new DOMException("timeout", "TimeoutError")), ms);
  const forward = () => controller.abort(signal.reason);
  if (signal) {
    if (signal.aborted) forward();
    else signal.addEventListener("abort", forward, { once: true });
  }
  return { signal: controller.signal, done: () => clearTimeout(timer), timedOut: () => controller.signal.reason?.name === "TimeoutError" };
}

async function request(path, token, options = {}) {
  let response;
  const method = options.method || "GET";
  const guard = timeoutSignal(options.signal, options.timeoutMs);

  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      credentials: "same-origin",
      headers: {
        ...csrfHeaders(method, cookies()),
        Accept: "application/json",
        ...(options.body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: guard.signal,
      cache: "no-store",
    });
  } catch (error) {
    guard.done();
    if (guard.timedOut()) throw new ApiError("The request took too long. Please try again.", 0, "timeout");
    if (error?.name === "AbortError") throw error;
    throw new ApiError("Could not connect to the server. Check that InsightIQ is running.", 0);
  }
  guard.done();

  if (!response.ok) {
    // Backend details are user-safe by design (no provider errors or paths);
    // prefer them for AI-summary failures where the reason matters.
    let detail = "";
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // Body was not JSON.
    }
    // FastAPI answers an unknown *route* with exactly "Not Found". InsightIQ's own
    // 404s always carry a specific reason, so this means the running backend is
    // older than the dashboard (it was not restarted after an update).
    if (response.status === 404 && detail === "Not Found") {
      throw new ApiError(
        "The backend does not provide this feature. It is probably running older code: restart the backend (scripts\\run_backend.py) and reload this page.",
        404,
        "route_missing"
      );
    }
    const message =
      options.preferDetail && detail
        ? detail
        : STATUS_MESSAGES[response.status] || `Request failed (HTTP ${response.status}).`;
    throw new ApiError(message, response.status);
  }

  return response.json();
}

const enc = encodeURIComponent;

/** Authenticated file download (session cookie). */
async function download(path) {
  let response;
  try {
    response = await fetch(`${BASE}${path}`, { credentials: "same-origin", cache: "no-store" });
  } catch {
    throw new ApiError("Could not connect to the server. Check that InsightIQ is running.", 0);
  }
  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // not JSON
    }
    throw new ApiError(detail || STATUS_MESSAGES[response.status] || `Download failed (HTTP ${response.status}).`, response.status);
  }
  const disposition = response.headers.get("content-disposition") || "";
  const match = disposition.match(/filename="([^"]+)"/);
  return { blob: await response.blob(), filename: match ? match[1] : "insightiq-report" };
}

export const api = {
  // Sign-in (email + password; the session lives in an HttpOnly cookie)
  login: (email, password) =>
    request("/auth/login", null, { method: "POST", body: { email, password }, preferDetail: true }),
  me: (signal) => request("/auth/me", null, { signal }),
  logout: () => request("/auth/logout", null, { method: "POST" }),
  // Research assistant (session user; conversations are private to the user).
  chatConversations: (token, signal, runId) =>
    request(`/chat/conversations${runId ? `?run_id=${enc(runId)}` : ""}`, token, { signal }),
  // scope: { ticker, run_id } binds the chat to one report's executive summary.
  newChat: (token, scope) => request("/chat/conversations", token, { method: "POST", body: scope ? { scope } : {} }),
  chatMessages: (token, id, signal) => request(`/chat/conversations/${enc(id)}/messages`, token, { signal }),
  sendChat: (token, id, body) =>
    request(`/chat/conversations/${enc(id)}/messages`, token, { method: "POST", body, preferDetail: true }),
  // Every saved research report (stored data only).
  savedReports: (token, signal) => request("/saved-reports", token, { signal }),
  // Companies with at least one completed run that has a saved report (US and India).
  researchCompanies: (token, signal) => request("/research/companies", token, { signal }),
  // Portfolio / system (read-only)
  overview: (token, signal) => request("/dashboard/overview", token, { signal }),
  companiesSummary: (token, signal) => request("/companies/summary", token, { signal }),
  runs: (token, ticker, signal) =>
    request(`/research/runs${ticker ? `?ticker=${enc(ticker)}` : ""}`, token, { signal }),
  traces: (token, id, signal) =>
    request(`/observability/runs/${enc(id)}/traces?limit=200`, token, { signal }),
  // Measured operations telemetry (PostgreSQL): latency, runs, tokens, alerts, traces.
  operations: (token, signal) => request("/operations/overview", token, { signal }),
  operationsTrace: (token, traceId, signal) => request(`/operations/traces/${enc(traceId)}`, token, { signal }),

  // Company (read-only)
  company: (token, ticker, signal) => request(`/companies/${enc(ticker)}/overview`, token, { signal }),
  companyReport: (token, ticker, id, signal) =>
    request(`/companies/${enc(ticker)}/reports/${enc(id)}`, token, { signal }),
  // Frozen chart and metrics dataset for one saved report (no external calls).
  reportInsights: (token, ticker, id, signal) =>
    request(`/companies/${enc(ticker)}/reports/${enc(id)}/insights`, token, { signal }),
  newsStatus: (token, ticker, signal) => request(`/news/status/${enc(ticker)}`, token, { signal }),
  freshness: (token, ticker, signal) =>
    request(`/companies/${enc(ticker)}/freshness`, token, { signal }),
  // Growth analysis from stored data (no provider or AI calls).
  growth: (token, ticker, signal) => request(`/companies/${enc(ticker)}/growth`, token, { signal }),

  // Explicit user actions (may call external providers)
  // options: { refresh: "if_stale" | "skip", on_refresh_failure: "continue_with_cached" | "fail" }
  submit: (token, ticker, options = {}) =>
    request("/jobs", token, { method: "POST", body: { ticker, ...options }, preferDetail: true }),
  refreshSources: (token, ticker, sources, force = false) =>
    request(`/companies/${enc(ticker)}/refresh`, token, {
      method: "POST",
      body: { sources, force },
      preferDetail: true,
    }),
  job: (token, id, signal) => request(`/jobs/${enc(id)}`, token, { signal }),
  cancelJob: (token, id) => request(`/jobs/${enc(id)}/cancel`, token, { method: "POST", preferDetail: true }),
  summaryStatus: (token, ticker, id, signal) =>
    request(`/companies/${enc(ticker)}/reports/${enc(id)}/executive-summary`, token, { signal }),
  refreshNews: (token, ticker) =>
    request("/news/refresh", token, { method: "POST", body: { ticker } }),
  // Markets and sector comparison
  markets: (token, signal) => request("/markets", token, { signal }),
  topCompanies: (token, market, signal) => request(`/markets/${enc(market)}/top-companies?limit=5`, token, { signal }),
  searchCompanies: (token, market, q, signal) =>
    request(`/markets/${enc(market)}/companies?search=${enc(q)}`, token, { signal }),
  marketCompany: (token, market, symbol, signal) =>
    request(`/markets/${enc(market)}/companies/${enc(symbol)}`, token, { signal }),
  marketOverview: (token, market, signal) => request(`/markets/${enc(market)}/overview`, token, { signal }),
  fundamentals: (token, market, symbol, signal) =>
    request(`/markets/${enc(market)}/companies/${enc(symbol)}/fundamentals`, token, { signal }),
  marketFinancials: (token, market, symbol, signal) =>
    request(`/markets/${enc(market)}/companies/${enc(symbol)}/financials`, token, { signal }),
  marketForecasts: (token, market, symbol, signal) =>
    request(`/markets/${enc(market)}/companies/${enc(symbol)}/forecasts`, token, { signal }),
  logos: (token, symbols, signal) => request(`/logos?symbols=${symbols.map(enc).join(",")}`, token, { signal }),
  sectors: (token, market, search, signal) =>
    request(`/markets/${enc(market)}/sectors?search=${enc(search || "")}`, token, { signal }),
  latestComparison: (token, market, sector, signal) =>
    request(`/markets/${enc(market)}/sectors/${enc(sector)}/comparison`, token, { signal }),
  comparisonHistory: (token, market, sector, signal) =>
    request(`/markets/${enc(market)}/sectors/${enc(sector)}/comparisons`, token, { signal }),
  comparison: (token, id, signal) => request(`/sector-comparisons/${enc(id)}`, token, { signal }),
  // Explicit actions (call market data providers and/or Azure OpenAI)
  providerSearch: (token, market, q) =>
    request(`/markets/${enc(market)}/companies/search`, token, { method: "POST", body: { q }, preferDetail: true }),
  refreshMarketCompany: (token, market, symbol, body = {}) =>
    request(`/markets/${enc(market)}/companies/${enc(symbol)}/refresh`, token, {
      method: "POST",
      body,
      preferDetail: true,
    }),
  // Company data with server-side freshness rules: mode "auto" refreshes only
  // when never refreshed or older than the TTL; "force" is the manual button.
  companyData: (token, market, symbol, mode = "auto", name) =>
    request(`/markets/${enc(market)}/companies/${enc(symbol)}/data`, token, {
      method: "POST",
      body: name ? { mode, name } : { mode },
      preferDetail: true,
    }),
  refreshMarketQuotes: (token, market) =>
    request(`/markets/${enc(market)}/quotes/refresh`, token, { method: "POST", preferDetail: true }),
  // companies: optional list of symbols from this sector (custom comparison).
  generateComparison: (token, market, sector, refresh = true, companies = null) =>
    request(`/markets/${enc(market)}/sectors/${enc(sector)}/comparison`, token, {
      method: "POST",
      body: Array.isArray(companies) ? { refresh, companies } : { refresh },
      preferDetail: true,
    }),
  downloadComparisonReport: (token, id, format) => download(`/sector-comparisons/${enc(id)}/report?format=${enc(format)}`, token),
  // Research report PDF (no provider or AI calls).
  downloadReportPdf: (token, ticker, id) => download(`/companies/${enc(ticker)}/reports/${enc(id)}/pdf`, token),
  // force=true is the button (regenerate even if a current version exists);
  // force=false is the automatic path (no AI call when a current version exists).
  generateSummary: (token, ticker, id, force = false) =>
    request(`/companies/${enc(ticker)}/reports/${enc(id)}/executive-summary`, token, {
      method: "POST",
      body: { force },
      preferDetail: true,
    }),
};
