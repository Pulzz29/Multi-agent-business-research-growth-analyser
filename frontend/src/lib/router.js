import { useCallback, useEffect, useState } from "react";
import { parseSymbols } from "./selection.js";

export const COMPANY_TABS = ["overview", "financials", "forecasts", "news", "research"];
export const MARKETS = ["US", "IN"];

/**
 * Hash routes (survive reloads, no server config needed):
 *   #/overview
 *   #/activity
 *   #/companies/META             -> overview tab
 *   #/companies/META/financials
 *   #/companies/META/reports/<run-id>
 *   #/markets/US/sectors/technology?companies=AAPL,MSFT  (custom comparison)
 */
export function parseHash(hash) {
  const [path, query = ""] = String(hash || "").split("?");
  const parts = path
    .replace(/^#\/?/, "")
    .split("/")
    .filter(Boolean)
    .map(decodeURIComponent);

  if (parts[0] === "activity") return { page: "activity" };

  // #/markets/US, #/markets/IN/companies/TCS.NS, #/markets/US/sectors[/technology]
  if (parts[0] === "markets") {
    const market = MARKETS.includes(String(parts[1] || "").toUpperCase()) ? parts[1].toUpperCase() : "US";
    if (parts[2] === "companies" && parts[3]) {
      return { page: "market-company", market, symbol: parts[3].toUpperCase() };
    }
    if (parts[2] === "sectors") {
      const sector = parts[3] ? parts[3].toLowerCase() : null;
      const companies = sector ? parseSymbols(new URLSearchParams(query).get("companies")) : [];
      return companies.length ? { page: "sectors", market, sector, companies } : { page: "sectors", market, sector };
    }
    return { page: "market", market };
  }

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
  if (route.page === "market") return `#/markets/${route.market || "US"}`;
  if (route.page === "market-company" && route.symbol) {
    return `#/markets/${route.market || "US"}/companies/${encodeURIComponent(route.symbol)}`;
  }
  if (route.page === "sectors") {
    const base = `#/markets/${route.market || "US"}/sectors${route.sector ? `/${encodeURIComponent(route.sector)}` : ""}`;
    const companies = route.sector && Array.isArray(route.companies) ? route.companies : [];
    return companies.length ? `${base}?companies=${companies.map(encodeURIComponent).join(",")}` : base;
  }
  if (route.page === "company" && route.ticker) {
    if (route.runId) return `#/companies/${route.ticker}/reports/${route.runId}`;
    if (route.tab && route.tab !== "overview") return `#/companies/${route.ticker}/${route.tab}`;
    return `#/companies/${route.ticker}`;
  }
  return "#/overview";
}

/** Update the address bar (bookmarkable) without re-routing or scrolling. */
export function replaceHash(route) {
  const hash = buildHash(route);
  if (window.location.hash !== hash) window.history.replaceState(null, "", hash);
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
