import { Activity, ChevronRight, Globe2, Layers, LayoutDashboard, LogOut, Menu, X } from "lucide-react";
import { COMPANIES, companyByTicker } from "../lib/reportModel.js";
import { CompanyMark, ThemeToggle } from "./ui.jsx";

export function Sidebar({ route, navigate, open, onClose, activeJob }) {
  const go = (next) => {
    navigate(next);
    onClose();
  };
  return (
    <>
      <div className={`scrim ${open ? "is-open" : ""}`} onClick={onClose} aria-hidden="true" />
      <aside className={`sidebar ${open ? "is-open" : ""}`} aria-label="Main navigation">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            IQ
          </span>
          <div>
            <p className="brand-name">InsightIQ</p>
            <p className="brand-tag">Business intelligence</p>
          </div>
          <button type="button" className="icon-btn sidebar-close" onClick={onClose} aria-label="Close menu">
            <X size={18} />
          </button>
        </div>

        <nav className="nav">
          <p className="nav-label">Workspace</p>
          <button
            type="button"
            className={`nav-item ${route.page === "overview" ? "is-active" : ""}`}
            aria-current={route.page === "overview" ? "page" : undefined}
            onClick={() => go({ page: "overview" })}
          >
            <LayoutDashboard size={17} aria-hidden="true" /> Overview
          </button>
          <button
            type="button"
            className={`nav-item ${route.page === "activity" ? "is-active" : ""}`}
            aria-current={route.page === "activity" ? "page" : undefined}
            onClick={() => go({ page: "activity" })}
          >
            <Activity size={17} aria-hidden="true" /> Activity
          </button>

          <p className="nav-label">Markets</p>
          {[
            ["US", "United States"],
            ["IN", "India"],
          ].map(([market, label]) => {
            const isActive = ["market", "market-company"].includes(route.page) && route.market === market;
            return (
              <button
                key={market}
                type="button"
                className={`nav-item ${isActive ? "is-active" : ""}`}
                aria-current={isActive ? "page" : undefined}
                onClick={() => go({ page: "market", market })}
              >
                <Globe2 size={17} aria-hidden="true" /> {label}
              </button>
            );
          })}
          <button
            type="button"
            className={`nav-item ${route.page === "sectors" ? "is-active" : ""}`}
            aria-current={route.page === "sectors" ? "page" : undefined}
            onClick={() => go({ page: "sectors", market: route.market || "US" })}
          >
            <Layers size={17} aria-hidden="true" /> Sector-wise Comparison
          </button>

          <p className="nav-label">Companies</p>
          {COMPANIES.map((company) => {
            const isActive = route.page === "company" && route.ticker === company.ticker;
            const running = activeJob?.ticker === company.ticker && ["queued", "running"].includes(activeJob?.status);
            return (
              <button
                key={company.ticker}
                type="button"
                className={`nav-item nav-company ${isActive ? "is-active" : ""}`}
                aria-current={isActive ? "page" : undefined}
                onClick={() => go({ page: "company", ticker: company.ticker, tab: "overview" })}
              >
                <CompanyMark ticker={company.ticker} size="sm" />
                <span className="nav-company-name">{company.name}</span>
                {running ? (
                  <span className="pulse" title="Research running" aria-label="Research running" />
                ) : (
                  <span className="nav-ticker">{company.ticker}</span>
                )}
              </button>
            );
          })}
        </nav>

        <div className="sidebar-foot">
          <p>Data: SEC EDGAR · FMP · Finnhub · Yahoo Finance · NewsData.io · Azure OpenAI</p>
          <p>Research support only. Not investment advice.</p>
        </div>
      </aside>
    </>
  );
}

const TAB_LABELS = {
  overview: "Overview",
  financials: "Financials",
  forecasts: "Forecasts",
  news: "News",
  research: "Research",
};

export function Topbar({ route, navigate, theme, onToggleTheme, onMenu, onDisconnect }) {
  const crumbs = [{ label: "InsightIQ", route: { page: "overview" } }];
  if (route.page === "activity") crumbs.push({ label: "Activity" });
  if (route.page === "overview") crumbs.push({ label: "Overview" });
  const MARKET_NAMES = { US: "United States", IN: "India" };
  if (route.page === "market") crumbs.push({ label: `${MARKET_NAMES[route.market]} market` });
  if (route.page === "market-company") {
    crumbs.push({ label: `${MARKET_NAMES[route.market]} market`, route: { page: "market", market: route.market } });
    crumbs.push({ label: route.symbol });
  }
  if (route.page === "sectors") {
    crumbs.push({
      label: `${MARKET_NAMES[route.market]} sectors`,
      route: route.sector ? { page: "sectors", market: route.market } : null,
    });
    if (route.sector) crumbs.push({ label: route.sector.replace(/_/g, " ") });
  }
  if (route.page === "company") {
    const company = companyByTicker(route.ticker);
    crumbs.push({ label: "Companies", route: { page: "overview" } });
    crumbs.push({
      label: company ? `${company.name} (${company.ticker})` : route.ticker,
      route: route.tab !== "overview" || route.runId ? { page: "company", ticker: route.ticker, tab: "overview" } : null,
    });
    if (route.tab && route.tab !== "overview") {
      crumbs.push({
        label: TAB_LABELS[route.tab],
        route: route.runId ? { page: "company", ticker: route.ticker, tab: route.tab } : null,
      });
    }
    if (route.runId) crumbs.push({ label: `Report ${route.runId.slice(0, 8)}` });
  }

  return (
    <header className="topbar">
      <button type="button" className="icon-btn menu-btn" onClick={onMenu} aria-label="Open menu">
        <Menu size={19} />
      </button>
      <nav aria-label="Breadcrumb" className="crumbs">
        <ol>
          {crumbs.map((crumb, i) => (
            <li key={`${crumb.label}-${i}`}>
              {i > 0 && <ChevronRight size={13} aria-hidden="true" className="crumb-sep" />}
              {crumb.route && i < crumbs.length - 1 ? (
                <button type="button" className="crumb-link" onClick={() => navigate(crumb.route)}>
                  {crumb.label}
                </button>
              ) : (
                <span aria-current={i === crumbs.length - 1 ? "page" : undefined}>{crumb.label}</span>
              )}
            </li>
          ))}
        </ol>
      </nav>
      <div className="topbar-actions">
        <ThemeToggle theme={theme} onToggle={onToggleTheme} />
        <button type="button" className="btn btn-ghost btn-sm" onClick={onDisconnect} title="Disconnect this browser tab">
          <LogOut size={15} aria-hidden="true" />
          <span className="hide-sm">Disconnect</span>
        </button>
      </div>
    </header>
  );
}
