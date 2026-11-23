const BASE = "/api/v1";

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

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
        ...(options.body !== undefined
          ? { "Content-Type": "application/json" }
          : {}),
      },
      body:
        options.body === undefined
          ? undefined
          : JSON.stringify(options.body),
      signal: options.signal,
      cache: "no-store",
    });
  } catch (error) {
    if (error?.name === "AbortError") {
      throw error;
    }

    throw new ApiError(
      "Could not connect to the server. Check that InsightIQ is running.",
      0
    );
  }

  if (!response.ok) {
    const messages = {
      401: "The access key was not accepted.",
      404: "That research record could not be found.",
      409: "The report is not ready yet.",
      422: "Please check the selected information.",
      429: "The research queue is busy. Try again shortly.",
      503: "The service is temporarily unavailable.",
    };

    throw new ApiError(
      messages[response.status] ||
        `Request failed (HTTP ${response.status}).`,
      response.status
    );
  }

  return response.json();
}

export const api = {
  // INSIGHTIQ_NEWS_REFRESH_UI_V1

  newsStatus: (token, ticker, signal) =>
    request(
      `/news/status/${encodeURIComponent(ticker)}`,
      token,
      { signal }
    ),

  refreshNews: (token, ticker) =>
    request("/news/refresh", token, {
      method: "POST",
      body: { ticker },
    }),

  overview: (token, signal) =>
    request("/dashboard/overview", token, { signal }),

  submit: (token, ticker) =>
    request("/jobs", token, {
      method: "POST",
      body: { ticker },
    }),

  job: (token, id, signal) =>
    request(`/jobs/${encodeURIComponent(id)}`, token, { signal }),

  report: (token, id) =>
    request(`/reports/${encodeURIComponent(id)}`, token),

  traces: (token, id) =>
    request(
      `/observability/runs/${encodeURIComponent(id)}/traces`,
      token
    ),
};
