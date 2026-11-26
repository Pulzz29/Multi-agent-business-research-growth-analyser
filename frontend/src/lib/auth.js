// Login form validation and CSRF header helpers (pure; unit-tested).

export const CSRF_COOKIE = "insightiq_csrf";
const EMAIL_RE = /^[^@\s]{1,64}@[^@\s]{1,255}\.[A-Za-z]{2,}$/;
const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** Field errors for the login form ({} when valid). */
export function validateLogin(email, password) {
  const errors = {};
  const value = String(email || "").trim();
  if (!value) errors.email = "Enter your email address.";
  else if (value.length > 320 || !EMAIL_RE.test(value)) errors.email = "Enter a valid email address.";
  if (!password) errors.password = "Enter your password.";
  else if (password.length > 256) errors.password = "Password is too long.";
  return errors;
}

/** Value of one cookie from a document.cookie string ("" when absent). */
export function readCookie(cookieString, name) {
  for (const part of String(cookieString || "").split(";")) {
    const index = part.indexOf("=");
    if (index < 0) continue;
    if (part.slice(0, index).trim() === name) {
      try {
        return decodeURIComponent(part.slice(index + 1).trim());
      } catch {
        return "";
      }
    }
  }
  return "";
}

/** The X-CSRF-Token header for state-changing requests (double-submit cookie). */
export function csrfHeaders(method, cookieString) {
  if (!UNSAFE.has(String(method || "GET").toUpperCase())) return {};
  const token = readCookie(cookieString, CSRF_COOKIE);
  return token ? { "X-CSRF-Token": token } : {};
}

/**
 * Route that opens one run's saved report (null when it has none).
 * SEC-validated tickers use the SEC company workspace; everything else
 * (Indian and provider-sourced US companies) uses the market company page.
 */
export function runReportRoute(run, isSecTicker) {
  if (!run?.report_available || !run.ticker || !run.run_id) return null;
  if (isSecTicker(run.ticker)) return { page: "company", ticker: run.ticker, tab: "research", runId: run.run_id };
  const market = /\.(NS|BO)$/i.test(run.ticker) ? "IN" : "US";
  return { page: "market-company", market, symbol: run.ticker, tab: "research", runId: run.run_id };
}
