import { useCallback, useEffect, useRef, useState } from "react";
import { Eye, EyeOff, KeyRound } from "lucide-react";
import { Sidebar, Topbar } from "./components/Layout.jsx";
import { ThemeToggle } from "./components/ui.jsx";
import { api } from "./lib/api.js";
import { companyByTicker, setSecCompanies } from "./lib/reportModel.js";
import { useHashRoute } from "./lib/router.js";
import { ActivityPage } from "./pages/ActivityPage.jsx";
import { CompanyPage } from "./pages/CompanyPage.jsx";
import { MarketCompanyPage } from "./pages/MarketCompanyPage.jsx";
import { MarketPage } from "./pages/MarketPage.jsx";
import { OverviewPage } from "./pages/OverviewPage.jsx";
import { SectorComparisonPage } from "./pages/SectorComparisonPage.jsx";

const PENDING = ["queued", "running"];

// Indian (NSE/BSE) symbols carry an exchange suffix; everything else is US.
const marketOf = (symbol) => (/\.(NS|BO)$/i.test(String(symbol || "")) ? "IN" : "US");
const TOKEN_KEY = "insightiq-token";
const THEME_KEY = "insightiq-theme";

function readStorage(storage, key) {
  try {
    return window[storage].getItem(key) || "";
  } catch {
    return "";
  }
}

function writeStorage(storage, key, value) {
  try {
    if (value) window[storage].setItem(key, value);
    else window[storage].removeItem(key);
  } catch {
    // Storage unavailable (private mode); state stays in memory.
  }
}

function ConnectCard({ onConnect, error, theme, onToggleTheme }) {
  const [value, setValue] = useState("");
  const [visible, setVisible] = useState(false);
  return (
    <main className="connect">
      <div className="connect-theme">
        <ThemeToggle theme={theme} onToggle={onToggleTheme} />
      </div>
      <form
        className="connect-card card"
        onSubmit={(e) => {
          e.preventDefault();
          if (value.trim()) onConnect(value.trim());
        }}
      >
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            IQ
          </span>
          <div>
            <p className="brand-name">InsightIQ</p>
            <p className="brand-tag">Multi-agent business intelligence</p>
          </div>
        </div>
        <h1>Connect to your workspace</h1>
        <p className="muted">
          Enter the <code className="code-inline">API_ACCESS_KEY</code> from the environment file the backend was started with. The key is kept only for this browser tab.
        </p>
        <label className="field">
          <span>Access key</span>
          <span className="input-wrap">
            <KeyRound size={16} aria-hidden="true" />
            <input
              type={visible ? "text" : "password"}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              placeholder="Paste access key"
              aria-invalid={Boolean(error)}
            />
            <button type="button" className="icon-btn icon-btn-sm" onClick={() => setVisible((v) => !v)} aria-label={visible ? "Hide key" : "Show key"}>
              {visible ? <EyeOff size={15} /> : <Eye size={15} />}
            </button>
          </span>
        </label>
        {error && (
          <p className="field-error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" className="btn btn-primary btn-block" disabled={!value.trim()}>
          Connect
        </button>
        <p className="small muted">Research support only. Forecasts are statistical estimates; nothing here is investment advice.</p>
      </form>
    </main>
  );
}

export default function App() {
  const [theme, setTheme] = useState(() => {
    const saved = readStorage("localStorage", THEME_KEY);
    if (saved === "light" || saved === "dark") return saved;
    return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
  });
  const [token, setToken] = useState(() => readStorage("sessionStorage", TOKEN_KEY));
  const [route, navigate] = useHashRoute();
  const [menuOpen, setMenuOpen] = useState(false);

  const [summary, setSummary] = useState(null);
  const [dashboard, setDashboard] = useState(null);
  const [runs, setRuns] = useState(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [connectError, setConnectError] = useState("");
  const [refreshIndex, setRefreshIndex] = useState(0);

  const [activeJob, setActiveJob] = useState(null);
  const [jobError, setJobError] = useState("");
  const [startBusy, setStartBusy] = useState(false);
  const submitting = useRef(false);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    writeStorage("localStorage", THEME_KEY, theme);
  }, [theme]);

  const refresh = useCallback(() => setRefreshIndex((n) => n + 1), []);

  const disconnect = useCallback((message = "") => {
    writeStorage("sessionStorage", TOKEN_KEY, "");
    setToken("");
    setSummary(null);
    setDashboard(null);
    setRuns(null);
    setActiveJob(null);
    setConnectError(message);
  }, []);

  const onAuthError = useCallback(() => disconnect("The access key was not accepted. Connect again."), [disconnect]);

  // Portfolio-level data (overview + activity). Read-only endpoints only.
  useEffect(() => {
    if (!token) return undefined;
    const controller = new AbortController();
    setLoading(true);
    Promise.allSettled([
      api.companiesSummary(token, controller.signal),
      api.overview(token, controller.signal),
      api.runs(token, null, controller.signal),
    ]).then((results) => {
      if (controller.signal.aborted) return;
      const [s, d, r] = results;
      const auth = results.find((x) => x.status === "rejected" && x.reason?.status === 401);
      if (auth) {
        onAuthError();
        return;
      }
      if (s.status === "fulfilled") {
        setSecCompanies(s.value?.companies);
        setSummary(s.value);
      }
      if (d.status === "fulfilled") setDashboard(d.value);
      if (r.status === "fulfilled") setRuns(r.value);
      const failed = results.filter((x) => x.status === "rejected" && x.reason?.name !== "AbortError");
      setLoadError(failed.length ? failed[0].reason.message : "");
      setLoading(false);
    });
    return () => controller.abort();
  }, [token, refreshIndex, onAuthError]);

  // Poll the active research job until it completes or fails.
  useEffect(() => {
    if (!token || !activeJob?.run_id || !PENDING.includes(activeJob.status)) return undefined;
    let stopped = false;
    let timer;

    async function poll() {
      try {
        const result = await api.job(token, activeJob.run_id);
        if (stopped) return;
        setActiveJob((current) => (current?.run_id === result.run_id ? { ...current, ...result } : current));
        if (result.status === "completed" || result.status === "failed") {
          if (result.status === "failed") setJobError("");
          refresh();
          return;
        }
      } catch (error) {
        if (stopped) return;
        if (error.status === 401) {
          onAuthError();
          return;
        }
        setJobError(`Could not check research progress: ${error.message} Retrying…`);
      }
      if (!stopped) timer = window.setTimeout(poll, 2500);
    }

    timer = window.setTimeout(poll, 600);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [token, activeJob?.run_id, activeJob?.status, refresh, onAuthError]);

  const startResearch = useCallback(
    async (ticker, options = {}) => {
      if (!token || submitting.current || PENDING.includes(activeJob?.status)) return;
      submitting.current = true;
      setStartBusy(true);
      setJobError("");
      try {
        const receipt = await api.submit(token, ticker, options);
        setActiveJob({
          ...receipt,
          created_at_utc: new Date().toISOString(),
          agent_statuses: { financial: "pending", news: "pending", strategic: "pending" },
        });
      } catch (error) {
        if (error.status === 401) onAuthError();
        setActiveJob({ ticker, status: "not_started" });
        setJobError(`Research could not be started: ${error.message}`);
      } finally {
        submitting.current = false;
        setStartBusy(false);
      }
    },
    [token, activeJob?.status, onAuthError]
  );

  if (!token) {
    return (
      <ConnectCard
        error={connectError}
        theme={theme}
        onToggleTheme={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
        onConnect={(value) => {
          writeStorage("sessionStorage", TOKEN_KEY, value);
          setConnectError("");
          setToken(value);
        }}
      />
    );
  }

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <Sidebar route={route} navigate={navigate} open={menuOpen} onClose={() => setMenuOpen(false)} activeJob={activeJob} />
      <div className="workspace">
        <Topbar
          route={route}
          navigate={navigate}
          theme={theme}
          onToggleTheme={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
          onMenu={() => setMenuOpen(true)}
          onDisconnect={() => disconnect("")}
        />
        <main id="main" className="content" tabIndex={-1}>
          {route.page === "overview" && (
            <OverviewPage summary={summary} dashboard={dashboard} runs={runs} loading={loading} error={loadError} onRetry={refresh} navigate={navigate} />
          )}
          {route.page === "activity" && (
            <ActivityPage token={token} runs={runs} dashboard={dashboard} loading={loading} error={loadError} onRetry={refresh} navigate={navigate} />
          )}
          {route.page === "market" && <MarketPage key={route.market} token={token} market={route.market} navigate={navigate} onAuthError={onAuthError} />}
          {(route.page === "market-company" || (route.page === "company" && !companyByTicker(route.ticker))) && (
            // Provider-sourced companies (any company outside the six SEC dashboards).
            <MarketCompanyPage
              key={`${route.market || marketOf(route.ticker)}-${route.symbol || route.ticker}`}
              token={token}
              market={route.market || marketOf(route.ticker)}
              symbol={route.symbol || route.ticker}
              runId={route.runId}
              showResearch={route.page === "company"}
              navigate={navigate}
              activeJob={activeJob}
              jobError={jobError}
              onStartResearch={startResearch}
              startBusy={startBusy}
              onAuthError={onAuthError}
            />
          )}
          {route.page === "sectors" && (
            <SectorComparisonPage
              key={`${route.market}-${route.sector || "list"}`}
              token={token}
              market={route.market}
              sector={route.sector}
              companies={route.companies}
              navigate={navigate}
              onAuthError={onAuthError}
            />
          )}
          {route.page === "company" && companyByTicker(route.ticker) && (
            <CompanyPage
              key={route.ticker}
              token={token}
              ticker={route.ticker}
              tab={route.tab}
              runId={route.runId}
              navigate={navigate}
              activeJob={activeJob}
              jobError={jobError}
              onStartResearch={startResearch}
              startBusy={startBusy}
              onAuthError={onAuthError}
            />
          )}
        </main>
      </div>
    </div>
  );
}
