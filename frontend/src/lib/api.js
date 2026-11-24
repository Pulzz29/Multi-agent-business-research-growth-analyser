const BASE = "/api/v1";

export class ApiError extends Error {
  constructor(message, status, code = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

const STATUS_MESSAGES = {
  401: "The access key was not accepted.",
  404: "That record could not be found.",
  409: "The report is not ready yet.",
  422: "Please check the selected information.",
  429: "The service is busy. Try again shortly.",
  502: "The AI provider could not complete the request.",
  503: "The service is temporarily unavailable.",
};

async function request(path, token, options = {}) {
  if (!token) {
    throw new ApiError("Connect your workspace first.", 401);
  }

  let response;

  try {
    response = await fetch(`${BASE}${path}`, {
      method: options.method || "GET",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/json",
        ...(options.body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
      cache: "no-store",
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError("Could not connect to the server. Check that InsightIQ is running.", 0);
  }

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

/** Authenticated file download (a plain link cannot carry the bearer key). */
async function download(path, token) {
  if (!token) throw new ApiError("Connect your workspace first.", 401);
  let response;
  try {
    response = await fetch(`${BASE}${path}`, { headers: { Authorization: `Bearer ${token}` }, cache: "no-store" });
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
  // Portfolio / system (read-only)
  overview: (token, signal) => request("/dashboard/overview", token, { signal }),
  companiesSummary: (token, signal) => request("/companies/summary", token, { signal }),
  runs: (token, ticker, signal) =>
    request(`/research/runs${ticker ? `?ticker=${enc(ticker)}` : ""}`, token, { signal }),
  traces: (token, id, signal) =>
    request(`/observability/runs/${enc(id)}/traces?limit=200`, token, { signal }),

  // Company (read-only)
  company: (token, ticker, signal) => request(`/companies/${enc(ticker)}/overview`, token, { signal }),
  companyReport: (token, ticker, id, signal) =>
    request(`/companies/${enc(ticker)}/reports/${enc(id)}`, token, { signal }),
  newsStatus: (token, ticker, signal) => request(`/news/status/${enc(ticker)}`, token, { signal }),
  freshness: (token, ticker, signal) =>
    request(`/companies/${enc(ticker)}/freshness`, token, { signal }),

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
  refreshNews: (token, ticker) =>
    request("/news/refresh", token, { method: "POST", body: { ticker } }),
  // Markets and sector comparison
  markets: (token, signal) => request("/markets", token, { signal }),
  topCompanies: (token, market, signal) => request(`/markets/${enc(market)}/top-companies?limit=5`, token, { signal }),
  searchCompanies: (token, market, q, signal) =>
    request(`/markets/${enc(market)}/companies?search=${enc(q)}`, token, { signal }),
  marketCompany: (token, market, symbol, signal) =>
    request(`/markets/${enc(market)}/companies/${enc(symbol)}`, token, { signal }),
  sectors: (token, market, search, signal) =>
    request(`/markets/${enc(market)}/sectors?search=${enc(search || "")}`, token, { signal }),
  latestComparison: (token, market, sector, signal) =>
    request(`/markets/${enc(market)}/sectors/${enc(sector)}/comparison`, token, { signal }),
  comparisonHistory: (token, market, sector, signal) =>
    request(`/markets/${enc(market)}/sectors/${enc(sector)}/comparisons`, token, { signal }),
  comparison: (token, id, signal) => request(`/sector-comparisons/${enc(id)}`, token, { signal }),
  // Explicit actions (call market data providers and/or Groq)
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
  generateComparison: (token, market, sector, refresh = true) =>
    request(`/markets/${enc(market)}/sectors/${enc(sector)}/comparison`, token, {
      method: "POST",
      body: { refresh },
      preferDetail: true,
    }),
  downloadComparisonReport: (token, id, format) => download(`/sector-comparisons/${enc(id)}/report?format=${enc(format)}`, token),
  generateSummary: (token, ticker, id) =>
    request(`/companies/${enc(ticker)}/reports/${enc(id)}/executive-summary`, token, {
      method: "POST",
      preferDetail: true,
    }),
};
