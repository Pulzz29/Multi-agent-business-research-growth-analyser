const BASE = "/api/v1";

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
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
    const message =
      options.preferDetail && detail
        ? detail
        : STATUS_MESSAGES[response.status] || `Request failed (HTTP ${response.status}).`;
    throw new ApiError(message, response.status);
  }

  return response.json();
}

const enc = encodeURIComponent;

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

  // Explicit user actions (may call external providers)
  submit: (token, ticker) => request("/jobs", token, { method: "POST", body: { ticker } }),
  job: (token, id, signal) => request(`/jobs/${enc(id)}`, token, { signal }),
  refreshNews: (token, ticker) =>
    request("/news/refresh", token, { method: "POST", body: { ticker } }),
  generateSummary: (token, ticker, id) =>
    request(`/companies/${enc(ticker)}/reports/${enc(id)}/executive-summary`, token, {
      method: "POST",
      preferDetail: true,
    }),
};
