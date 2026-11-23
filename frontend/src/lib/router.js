import { useCallback, useEffect, useState } from "react";

export const COMPANY_TABS = ["overview", "financials", "forecasts", "news", "research"];

/**
 * Hash routes (survive reloads, no server config needed):
 *   #/overview
 *   #/activity
 *   #/companies/META             -> overview tab
 *   #/companies/META/financials
 *   #/companies/META/reports/<run-id>
 */
export function parseHash(hash) {
  const parts = String(hash || "")
    .replace(/^#\/?/, "")
    .split("/")
    .filter(Boolean)
    .map(decodeURIComponent);

  if (parts[0] === "activity") return { page: "activity" };

  if (parts[0] === "companies" && parts[1]) {
    const ticker = parts[1].toUpperCase();
    if (parts[2] === "reports" && parts[3]) {
      return { page: "company", ticker, tab: "research", runId: parts[3] };
    }
    const tab = COMPANY_TABS.includes(parts[2]) ? parts[2] : "overview";
    return { page: "company", ticker, tab };
  }

  return { page: "overview" };
}

export function buildHash(route) {
  if (route.page === "activity") return "#/activity";
  if (route.page === "company" && route.ticker) {
    if (route.runId) return `#/companies/${route.ticker}/reports/${route.runId}`;
    if (route.tab && route.tab !== "overview") return `#/companies/${route.ticker}/${route.tab}`;
    return `#/companies/${route.ticker}`;
  }
  return "#/overview";
}

export function useHashRoute() {
  const [route, setRoute] = useState(() => parseHash(window.location.hash));

  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  const navigate = useCallback((next) => {
    const hash = buildHash(next);
    if (window.location.hash !== hash) {
      window.location.hash = hash;
    } else {
      setRoute(parseHash(hash));
    }
    window.scrollTo({ top: 0, behavior: "auto" });
  }, []);

  return [route, navigate];
}
