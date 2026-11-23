import React, {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  Activity,
  ArrowRight,
  ArrowUpRight,
  BarChart3,
  BookOpen,
  Building2,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock3,
  FileText,
  Globe2,
  LayoutDashboard,
  LockKeyhole,
  LogOut,
  Menu,
  Moon,
  Newspaper,
  Play,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Sun,
  TrendingUp,
  X,
} from "lucide-react";

import { api } from "./api.js";


const COMPANIES = [
  { ticker: "AAPL", name: "Apple", initials: "A" },
  { ticker: "AMZN", name: "Amazon", initials: "a" },
  { ticker: "GOOGL", name: "Alphabet", initials: "G" },
  { ticker: "META", name: "Meta", initials: "M" },
  { ticker: "MSFT", name: "Microsoft", initials: "M" },
  { ticker: "NVDA", name: "NVIDIA", initials: "N" },
];

const NAV = [
  { id: "overview", label: "Overview", icon: LayoutDashboard },
  { id: "research", label: "Research", icon: Search },
  { id: "activity", label: "Activity", icon: Activity },
];

const AGENTS = [
  { id: "financial", label: "Financial analysis", icon: BarChart3 },
  { id: "news", label: "News research", icon: Newspaper },
  { id: "strategic", label: "Strategic analysis", icon: Sparkles },
];

const PENDING = ["queued", "running"];

const formatCount = (value) =>
  typeof value === "number"
    ? value.toLocaleString("en-US")
    : "—";

const formatDuration = (value) =>
  typeof value === "number"
    ? `${(value / 1000).toFixed(2)}s`
    : "—";

const formatDate = (value) => {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      });
};

const niceLabel = (value) =>
  String(value || "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());

const companyName = (ticker) =>
  COMPANIES.find((item) => item.ticker === ticker)?.name || ticker;

const isRecord = (value) =>
  value !== null && typeof value === "object" && !Array.isArray(value);

function displayValue(value) {
  if (value == null) return "Not available";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return formatCount(value);
  if (typeof value === "string") return value;

  if (Array.isArray(value)) {
    if (value.every((item) => typeof item !== "object")) {
      return value.join(", ");
    }
    return `${value.length} items`;
  }

  return "See details";
}

function readPrimaryText(item, keys) {
  if (typeof item === "string") return item;
  if (!isRecord(item)) return "Source-backed finding";

  for (const name of keys) {
    const value = item[name];

    if (typeof value === "string" && value.trim()) {
      return value;
    }
  }

  return "Source-backed finding";
}

function ThemeToggle({ theme, toggle }) {
  return (
    <button
      type="button"
      className="icon-button theme-toggle"
      onClick={toggle}
      title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      aria-label={theme === "dark" ? "Use light theme" : "Use dark theme"}
    >
      {theme === "dark" ? <Sun size={19} /> : <Moon size={19} />}
    </button>
  );
}

function Badge({ status, children }) {
  const normalized = String(status || "unknown");

  const tone = ["completed", "verified", "healthy", "normal"].includes(
    normalized
  )
    ? "good"
    : ["failed", "alerting", "degraded"].includes(normalized)
      ? "bad"
      : ["running", "queued"].includes(normalized)
        ? "blue"
        : "muted";

  return (
    <span className={`badge ${tone}`}>
      <span className="badge-dot" />
      {children || niceLabel(normalized)}
    </span>
  );
}

function Panel({ title, subtitle, action, children, className = "" }) {
  return (
    <section className={`panel ${className}`}>
      <div className="panel-heading">
        <div>
          <h2>{title}</h2>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

function Empty({ icon: Icon = FileText, title, description }) {
  return (
    <div className="empty">
      <span className="empty-icon">
        <Icon size={22} />
      </span>
      <strong>{title}</strong>
      <p>{description}</p>
    </div>
  );
}

function Stat({ icon: Icon, label, value, detail, accent }) {
  return (
    <div className="stat-card">
      <div className="stat-head">
        <span className="stat-label">{label}</span>
        <span className={`stat-icon ${accent || ""}`}>
          <Icon size={18} />
        </span>
      </div>
      <div className="stat-value">{value}</div>
      <div className="stat-detail">{detail}</div>
    </div>
  );
}

function CompanyMark({ ticker, large = false }) {
  const company = COMPANIES.find((item) => item.ticker === ticker);

  return (
    <div
      className={`company-mark mark-${ticker?.toLowerCase()} ${
        large ? "large" : ""
      }`}
    >
      {company?.initials || ticker?.slice(0, 1) || "?"}
    </div>
  );
}

function LoadingView() {
  return (
    <div className="loading-grid" aria-label="Loading dashboard">
      {[1, 2, 3, 4].map((number) => (
        <div className="skeleton" key={number} />
      ))}
    </div>
  );
}

function Overview({ data, loading, refresh, openJob, startResearch }) {
  if (!data) {
    return loading ? (
      <LoadingView />
    ) : (
      <Panel title="Welcome to InsightIQ">
        <Empty
          icon={LockKeyhole}
          title="Connect to view your workspace"
          description="Enter your access key above to load your research data."
        />
      </Panel>
    );
  }

  const jobs = data.research_jobs || {};
  const counts = jobs.status_counts || {};
  const performance = data.performance?.components?.worker;
  const backup = data.backup_health || {};

  return (
    <div className="page-stack">
      <section className="welcome">
        <div className="welcome-content">
          <div className="overline light">INSIGHTIQ WORKSPACE</div>
          <h1>Make better-informed decisions.</h1>
          <p>
            Explore financial evidence, company news, and strategic
            research in one simple workspace.
          </p>
          <button className="button welcome-button" onClick={startResearch}>
            Start new research <ArrowRight size={17} />
          </button>
        </div>
        <div className="welcome-art" aria-hidden="true">
          <div className="art-circle one" />
          <div className="art-circle two" />
          <div className="art-lines" />
          <div className="art-symbol">
            <TrendingUp size={64} strokeWidth={1.2} />
          </div>
        </div>
      </section>

      <div className="section-bar">
        <div>
          <h2>At a glance</h2>
          <p>Current records from your research database</p>
        </div>
        <button
          className="button button-secondary"
          onClick={refresh}
          disabled={loading}
        >
          <RefreshCw size={15} className={loading ? "spin" : ""} />
          Refresh
        </button>
      </div>

      <div className="stats-grid">
        <Stat
          icon={Building2}
          label="Companies"
          value={formatCount(data.supported_companies?.length)}
          detail="Available for research"
          accent="violet"
        />
        <Stat
          icon={BarChart3}
          label="Financial facts"
          value={formatCount(data.source_data?.sec_fact_count)}
          detail="SEC records"
          accent="blue"
        />
        <Stat
          icon={Newspaper}
          label="News articles"
          value={formatCount(data.source_data?.news_article_count)}
          detail="Stored source articles"
          accent="orange"
        />
        <Stat
          icon={FileText}
          label="Completed reports"
          value={formatCount(counts.completed || 0)}
          detail={`${formatCount(jobs.total_count || 0)} total research jobs`}
          accent="green"
        />
      </div>

      <div className="two-columns">
        <Panel
          title="Recent research"
          subtitle="Open a job to see its report or progress"
          action={
            <span className="small-muted">
              {formatCount(jobs.total_count || 0)} total
            </span>
          }
        >
          {(jobs.recent || []).length ? (
            <div className="job-list">
              {jobs.recent.slice(0, 6).map((job) => (
                <button
                  type="button"
                  className="job-row"
                  key={job.run_id}
                  onClick={() => openJob(job)}
                >
                  <CompanyMark ticker={job.ticker} />
                  <span className="job-row-text">
                    <strong>{companyName(job.ticker)}</strong>
                    <small>
                      {job.ticker} · {formatDate(job.created_at_utc)}
                    </small>
                  </span>
                  <Badge status={job.status} />
                  <ChevronRight size={17} className="row-chevron" />
                </button>
              ))}
            </div>
          ) : (
            <Empty
              title="No research yet"
              description="Start your first company analysis."
            />
          )}
        </Panel>

        <div className="side-stack">
          <Panel
            title="System status"
            subtitle="Research and data protection"
          >
            <div className="info-line">
              <span>
                <ShieldCheck size={17} /> Backups
              </span>
              <Badge
                status={
                  backup.recovery_points_complete
                    ? "verified"
                    : "degraded"
                }
              />
            </div>
            <div className="info-line">
              <span>
                <Activity size={17} /> Reliability
              </span>
              <Badge status={data.reliability?.status} />
            </div>
            <div className="info-line">
              <span>
                <Clock3 size={17} /> Workflow p95
              </span>
              <strong>
                {formatDuration(performance?.p95_success_latency_ms)}
              </strong>
            </div>
            <p className="panel-note">
              Performance measurements are based on recorded executions.
              Small samples may not represent typical usage.
            </p>
          </Panel>

          <Panel title="Companies" subtitle="Available coverage">
            <div className="company-grid-mini">
              {(data.supported_companies || []).map((ticker) => (
                <div className="company-mini" key={ticker}>
                  <CompanyMark ticker={ticker} />
                  <span>{ticker}</span>
                </div>
              ))}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}

function EvidenceFields({ item, omit = [] }) {
  if (!isRecord(item)) {
    return <p className="evidence-text">{displayValue(item)}</p>;
  }

  const entries = Object.entries(item).filter(
    ([field, value]) =>
      !omit.includes(field) &&
      value !== null &&
      value !== undefined
  );

  return (
    <div className="evidence-fields">
      {entries.map(([field, value]) => {
        if (Array.isArray(value) || isRecord(value)) {
          return (
            <details className="nested-evidence" key={field}>
              <summary>
                {niceLabel(field)}
                <ChevronDown size={14} />
              </summary>
              <div className="nested-content">
                {Array.isArray(value) ? (
                  value.map((entry, index) => (
                    <div className="nested-item" key={index}>
                      <EvidenceFields item={entry} />
                    </div>
                  ))
                ) : (
                  <EvidenceFields item={value} />
                )}
              </div>
            </details>
          );
        }

        return (
          <div className="evidence-field" key={field}>
            <span>{niceLabel(field)}</span>
            <strong>{displayValue(value)}</strong>
          </div>
        );
      })}
    </div>
  );
}

function FindingCards({ items, variant }) {
  if (!Array.isArray(items) || !items.length) {
    return (
      <Empty
        title="No findings available"
        description="This report does not include records for this section."
      />
    );
  }

  const titles = {
    financial: [
      "statement",
      "claim",
      "text",
      "description",
      "summary",
    ],
    news: [
      "title",
      "headline",
      "article_title",
      "summary",
    ],
    strategic: [
      "priority",
      "title",
      "theme",
      "description",
      "rationale",
    ],
  };

  return (
    <div className="finding-list">
      {items.map((item, index) => {
        const heading = readPrimaryText(
          item,
          titles[variant] || ["title", "text"]
        );

        return (
          <article className="finding-card" key={index}>
            <div className="finding-number">
              {String(index + 1).padStart(2, "0")}
            </div>
            <div className="finding-body">
              <h4>{heading}</h4>
              <details className="finding-details">
                <summary>
                  View evidence <ChevronDown size={15} />
                </summary>
                <EvidenceFields item={item} />
              </details>
            </div>
          </article>
        );
      })}
    </div>
  );
}

function ReportSection({ icon: Icon, title, description, children }) {
  return (
    <section className="report-section">
      <div className="report-section-head">
        <div className="report-section-icon">
          <Icon size={19} />
        </div>
        <div>
          <h3>{title}</h3>
          <p>{description}</p>
        </div>
      </div>
      {children}
    </section>
  );
}

function Report({ report }) {
  if (!report) return null;

  const claims =
    report.financial?.verified_claims?.verified_claims || [];

  const articles =
    report.news?.approved_primary_articles || [];

  const priorities =
    report.strategic?.research_priorities || [];

  const [section, setSection] = useState("financial");

  return (
    <div className="report-shell">
      <div className="report-heading">
        <CompanyMark ticker={report.ticker} large />
        <div className="report-title">
          <div className="overline">RESEARCH REPORT</div>
          <h2>{companyName(report.ticker)}</h2>
          <p>{report.ticker} · Source-grounded research</p>
        </div>
        <Badge status={report.status} />
      </div>

      <div className="report-highlights">
        <div>
          <strong>{formatCount(claims.length)}</strong>
          <span>Financial claims</span>
        </div>
        <div>
          <strong>{formatCount(articles.length)}</strong>
          <span>News articles</span>
        </div>
        <div>
          <strong>{formatCount(priorities.length)}</strong>
          <span>Research priorities</span>
        </div>
      </div>

      <div className="report-tabs" role="tablist" aria-label="Report sections">
        {[
          { id: "financial", label: "Financial", icon: BarChart3 },
          { id: "news", label: "News", icon: Newspaper },
          { id: "strategic", label: "Strategy", icon: Sparkles },
        ].map(({ id, label, icon: Icon }) => (
          <button
            type="button"
            role="tab"
            aria-selected={section === id}
            className={`report-tab ${section === id ? "selected" : ""}`}
            key={id}
            onClick={() => setSection(id)}
          >
            <Icon size={16} /> {label}
          </button>
        ))}
      </div>

      {section === "financial" && (
        <ReportSection
          icon={BarChart3}
          title="Financial analysis"
          description="Validated claims and financial model evidence"
        >
          <FindingCards items={claims} variant="financial" />
          <details className="additional-evidence">
            <summary>
              More financial details <ChevronDown size={16} />
            </summary>
            <EvidenceFields item={report.financial} />
          </details>
        </ReportSection>
      )}

      {section === "news" && (
        <ReportSection
          icon={Newspaper}
          title="News and developments"
          description="Articles selected from stored news evidence"
        >
          <FindingCards items={articles} variant="news" />
          <details className="additional-evidence">
            <summary>
              News review details <ChevronDown size={16} />
            </summary>
            <EvidenceFields item={report.news} />
          </details>
        </ReportSection>
      )}

      {section === "strategic" && (
        <ReportSection
          icon={Sparkles}
          title="Strategic priorities"
          description="Research priorities grounded in the collected evidence"
        >
          <FindingCards items={priorities} variant="strategic" />
          <details className="additional-evidence">
            <summary>
              Strategic analysis details <ChevronDown size={16} />
            </summary>
            <EvidenceFields item={report.strategic} />
          </details>
        </ReportSection>
      )}

      <div className="report-footnote">
        <ShieldCheck size={16} />
        Forecasts are estimates, not guarantees. Review the evidence,
        sources, and uncertainty notes before making decisions.
      </div>
    </div>
  );
}

function Research({
  token,
  ticker,
  setTicker,
  job,
  report,
  busy,
  error,
  submit,
}) {
  // INSIGHTIQ_NEWS_REFRESH_UI_V1

  const [newsStatus, setNewsStatus] = useState(null);
  const [newsLoading, setNewsLoading] = useState(false);
  const [newsRefreshing, setNewsRefreshing] = useState(false);
  const [newsError, setNewsError] = useState("");
  const [newsMessage, setNewsMessage] = useState("");

  const refreshInProgress = useRef(false);
  const selectedNewsTicker = useRef(ticker);
  selectedNewsTicker.current = ticker;

  useEffect(() => {
    if (!token) {
      setNewsStatus(null);
      setNewsLoading(false);
      setNewsError("");
      setNewsMessage("");
      return;
    }

    const controller = new AbortController();

    setNewsStatus(null);
    setNewsError("");
    setNewsMessage("");
    setNewsLoading(true);

    api.newsStatus(token, ticker, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) {
          setNewsStatus(result);
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) {
          setNewsError(
            err?.message || "Could not load news coverage."
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setNewsLoading(false);
        }
      });

    return () => controller.abort();
  }, [token, ticker]);

  const refreshCompanyNews = async () => {
    if (!token || refreshInProgress.current) return;

    const requestedTicker = ticker;

    refreshInProgress.current = true;
    setNewsRefreshing(true);
    setNewsError("");
    setNewsMessage("");

    try {
      const result = await api.refreshNews(
        token,
        requestedTicker
      );

      if (selectedNewsTicker.current !== requestedTicker) {
        return;
      }

      setNewsStatus(result);

      if (result.status === "recently_refreshed") {
        setNewsMessage(
          "News was refreshed recently. Saved articles were reused."
        );
      } else {
        const added = Number(result.records_inserted || 0);

        setNewsMessage(
          `Refresh complete. ${added} new article${
            added === 1 ? "" : "s"
          } saved.`
        );
      }
    } catch (err) {
      if (selectedNewsTicker.current === requestedTicker) {
        setNewsError(
          err?.message || "News refresh failed."
        );
      }
    } finally {
      refreshInProgress.current = false;
      setNewsRefreshing(false);
    }
  };

  return (
    <div className="page-stack">
      <div className="page-intro">
        <div>
          <div className="overline">RESEARCH</div>
          <h1>Research a company</h1>
          <p>
            Choose a company and InsightIQ will bring together
            financial evidence, news, and strategic findings.
          </p>
        </div>
      </div>

      <div className="research-grid">
        <Panel
          title="Choose a company"
          subtitle="Select one of the six supported companies"
        >
          <div className="company-select-grid">
            {COMPANIES.map((company) => (
              <button
                type="button"
                key={company.ticker}
                className={`company-choice ${
                  ticker === company.ticker ? "selected" : ""
                }`}
                onClick={() => setTicker(company.ticker)}
                aria-pressed={ticker === company.ticker}
              >
                <CompanyMark ticker={company.ticker} />
                <span className="company-choice-text">
                  <strong>{company.name}</strong>
                  <small>{company.ticker}</small>
                </span>
                {ticker === company.ticker && (
                  <Check size={16} className="choice-check" />
                )}
              </button>
            ))}
          </div>

          <div className="news-refresh-section">
            <div className="news-refresh-heading">
              <div>
                <strong>Company news</strong>
                <p>
                  Check for newer articles before running research.
                </p>
              </div>

              <span className="news-refresh-count">
                {newsLoading
                  ? "Loading…"
                  : newsStatus
                    ? `${newsStatus.article_count} saved`
                    : "—"}
              </span>
            </div>

            {newsStatus && (
              <div className="news-refresh-details">
                <div>
                  <span>Latest article published</span>
                  <strong>
                    {formatDate(
                      newsStatus.latest_published_at
                    )}
                  </strong>
                </div>

                <div>
                  <span>Last successful refresh</span>
                  <strong>
                    {formatDate(
                      newsStatus.last_successful_refresh_at
                    )}
                  </strong>
                </div>
              </div>
            )}

            <button
              type="button"
              className="button news-refresh-button full-width"
              onClick={refreshCompanyNews}
              disabled={
                !token ||
                newsRefreshing ||
                busy ||
                PENDING.includes(job?.status)
              }
              aria-label={`Refresh news for ${ticker}`}
            >
              <RefreshCw
                size={17}
                className={newsRefreshing ? "spin" : ""}
              />

              {newsRefreshing
                ? "Refreshing news…"
                : `Refresh ${ticker} news`}
            </button>

            {newsMessage && (
              <p className="news-refresh-success" role="status">
                <CheckCircle2 size={16} />
                {newsMessage}
              </p>
            )}

            {newsError && (
              <p className="news-refresh-error" role="alert">
                {newsError}
              </p>
            )}

            <p className="helper-note">
              Refreshing news does not change saved reports
              or automatically start a research job.
            </p>
          </div>

          <button
            type="button"
            className="button button-primary full-width start-button"
            onClick={submit}
            disabled={!token || busy || PENDING.includes(job?.status)}
          >
            {busy ? (
              <>
                <RefreshCw size={17} className="spin" /> Starting…
              </>
            ) : (
              <>
                <Play size={17} /> Start research
              </>
            )}
          </button>

          <p className="helper-note">
            Research runs in the background. Your job status updates
            automatically while this page is open.
          </p>
        </Panel>

        <Panel
          title="Research progress"
          subtitle="Live status of your research job"
        >
          {job ? (
            <>
              <div className="current-job">
                <CompanyMark ticker={job.ticker} />
                <div className="current-job-name">
                  <strong>{companyName(job.ticker)}</strong>
                  <span>{job.ticker}</span>
                </div>
                <Badge status={job.status} />
              </div>

              <div className="agent-list">
                {AGENTS.map(({ id, label, icon: Icon }) => (
                  <div className="agent-progress-row" key={id}>
                    <span className="agent-progress-icon">
                      <Icon size={17} />
                    </span>
                    <span>{label}</span>
                    <Badge
                      status={job.agent_statuses?.[id] || "pending"}
                    />
                  </div>
                ))}
              </div>

              <div className="job-reference">
                <span>Research ID</span>
                <code>{job.run_id}</code>
              </div>

              {PENDING.includes(job.status) && (
                <div className="progress-note">
                  <RefreshCw size={15} className="spin" />
                  Research is in progress.
                </div>
              )}

              {job.status === "failed" && (
                <div className="inline-error">
                  Research did not complete. Please try a new request.
                </div>
              )}
            </>
          ) : (
            <Empty
              icon={Activity}
              title="Ready when you are"
              description="Select a company and start research to see progress here."
            />
          )}

          {error && (
            <div className="inline-error" role="alert">
              {error}
            </div>
          )}
        </Panel>
      </div>

      {report && (
        <Panel
          title="Your research report"
          subtitle="Results from the completed research workflow"
          className="report-panel"
        >
          <Report key={report.run_id} report={report} />
        </Panel>
      )}
    </div>
  );
}

function PerformanceBar({ name, item, max }) {
  const value = item?.p95_success_latency_ms;

  const percent =
    typeof value === "number" && max > 0
      ? Math.max(2, (value / max) * 100)
      : 0;

  return (
    <div className="performance-row">
      <div className="performance-label">
        <strong>{name}</strong>
        <span>{formatDuration(value)}</span>
      </div>
      <div className="performance-track">
        <div
          className="performance-fill"
          style={{ width: `${percent}%` }}
        />
      </div>
      <small>
        {formatCount(item?.completed_span_count || 0)} recorded samples
      </small>
    </div>
  );
}

function ActivityView({ data }) {
  if (!data) {
    return (
      <Panel title="Activity">
        <Empty
          icon={Activity}
          title="No activity loaded"
          description="Connect to see your research performance."
        />
      </Panel>
    );
  }

  const performance = data.performance?.components || {};
  const reliability = data.reliability || {};
  const backup = data.backup_health || {};

  const max = Math.max(
    1,
    ...["worker", "financial", "news", "strategic"]
      .map((name) => performance[name]?.p95_success_latency_ms)
      .filter((value) => typeof value === "number")
  );

  return (
    <div className="page-stack">
      <div className="page-intro">
        <div>
          <div className="overline">ACTIVITY</div>
          <h1>System activity</h1>
          <p>
            A simple look at research performance and system health,
            using measurements saved by InsightIQ.
          </p>
        </div>
      </div>

      <div className="stats-grid">
        <Stat
          icon={Activity}
          label="Recorded spans"
          value={formatCount(data.performance?.sampled_span_count)}
          detail="Measured execution steps"
          accent="blue"
        />
        <Stat
          icon={Clock3}
          label="Workflow p95"
          value={formatDuration(
            performance.worker?.p95_success_latency_ms
          )}
          detail={`${
            performance.worker?.completed_span_count || 0
          } completed samples`}
          accent="violet"
        />
        <Stat
          icon={ShieldCheck}
          label="Verified backups"
          value={formatCount(backup.verified_backup_count)}
          detail="Completed research recovery points"
          accent="green"
        />
        <Stat
          icon={TrendingUp}
          label="Research runs"
          value={formatCount(data.research_jobs?.total_count)}
          detail="Saved jobs"
          accent="orange"
        />
      </div>

      <div className="two-columns">
        <Panel
          title="Execution performance"
          subtitle="Observed p95 duration for recorded executions"
        >
          <div className="performance-list">
            <PerformanceBar
              name="Entire workflow"
              item={performance.worker}
              max={max}
            />
            <PerformanceBar
              name="Financial agent"
              item={performance.financial}
              max={max}
            />
            <PerformanceBar
              name="News agent"
              item={performance.news}
              max={max}
            />
            <PerformanceBar
              name="Strategic agent"
              item={performance.strategic}
              max={max}
            />
          </div>

          <p className="panel-note">
            These measurements include development executions.
            Results based on small samples should be interpreted cautiously.
          </p>
        </Panel>

        <div className="side-stack">
          <Panel
            title="Reliability"
            subtitle="Monitoring results and sample requirements"
          >
            <div className="info-line">
              <span>Overall status</span>
              <Badge status={reliability.status} />
            </div>

            {(reliability.policies || []).map((policy) => (
              <div className="policy-item" key={policy.policy}>
                <div>
                  <strong>{niceLabel(policy.policy)}</strong>
                  <small>
                    {policy.observed_count} / {policy.minimum_samples} samples
                  </small>
                </div>
                <Badge status={policy.state} />
              </div>
            ))}
          </Panel>

          <Panel title="Data protection" subtitle="Research backup checks">
            <div className="info-line">
              <span>Recovery points</span>
              <Badge
                status={
                  backup.recovery_points_complete
                    ? "verified"
                    : "degraded"
                }
              />
            </div>
            <div className="info-line">
              <span>Completed jobs checked</span>
              <strong>
                {formatCount(backup.completed_jobs_checked)}
              </strong>
            </div>
            <div className="info-line">
              <span>Unverified backups</span>
              <strong>
                {formatCount(backup.unverified_backup_count)}
              </strong>
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}

function ConnectCard({ connect, loading }) {
  const [input, setInput] = useState("");
  const [visible, setVisible] = useState(false);

  function submit(event) {
    event.preventDefault();

    const value = input.trim();

    if (value) {
      connect(value);
      setInput("");
    }
  }

  return (
    <div className="connect-card">
      <div className="connect-copy">
        <span className="connect-icon">
          <LockKeyhole size={21} />
        </span>
        <div>
          <h3>Connect your workspace</h3>
          <p>
            Enter your InsightIQ API access key to view research and reports.
          </p>
        </div>
      </div>
      <form className="connect-form" onSubmit={submit}>
        <input
          aria-label="API access key"
          type={visible ? "text" : "password"}
          autoComplete="off"
          spellCheck="false"
          value={input}
          onChange={(event) => setInput(event.target.value)}
          placeholder="API access key"
          required
        />
        <button
          type="button"
          className="show-key"
          onClick={() => setVisible((value) => !value)}
          aria-label={visible ? "Hide access key" : "Show access key"}
        >
          {visible ? "Hide" : "Show"}
        </button>
        <button className="button button-primary" type="submit" disabled={loading}>
          Connect <ArrowRight size={15} />
        </button>
      </form>
    </div>
  );
}

export default function App() {
  const [theme, setTheme] = useState(() => {
    try {
      const saved = localStorage.getItem("insightiq-theme");
      if (saved === "light" || saved === "dark") return saved;
    } catch {
      // Continue using the system theme.
    }

    return window.matchMedia("(prefers-color-scheme: light)").matches
      ? "light"
      : "dark";
  });

  const [view, setView] = useState("overview");
  const [menuOpen, setMenuOpen] = useState(false);
  const [token, setToken] = useState("");
  const [overview, setOverview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [connectionError, setConnectionError] = useState("");
  const [refreshIndex, setRefreshIndex] = useState(0);

  const [ticker, setTicker] = useState("MSFT");
  const [job, setJob] = useState(null);
  const [report, setReport] = useState(null);
  const [busy, setBusy] = useState(false);
  const [researchError, setResearchError] = useState("");

  const activeLoad = useRef(0);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);

    try {
      localStorage.setItem("insightiq-theme", theme);
    } catch {
      // Theme remains active for the current page.
    }
  }, [theme]);

  const refresh = useCallback(() => {
    setRefreshIndex((number) => number + 1);
  }, []);

  useEffect(() => {
    if (!token) {
      setOverview(null);
      return;
    }

    const controller = new AbortController();

    setLoading(true);

    api.overview(token, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return;

        setOverview(result);
        setConnectionError("");
      })
      .catch((error) => {
        if (controller.signal.aborted) return;

        setConnectionError(error.message);

        if (error.status === 401) {
          setToken("");
          setOverview(null);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });

    return () => controller.abort();
  }, [token, refreshIndex]);

  useEffect(() => {
    if (!token || !job?.run_id || !PENDING.includes(job.status)) {
      return;
    }

    let stopped = false;
    let timer;

    async function poll() {
      try {
        const result = await api.job(token, job.run_id);

        if (stopped) return;

        setJob(result);

        if (result.status === "completed") {
          try {
            const finishedReport = await api.report(token, result.run_id);

            if (!stopped) {
              setReport(finishedReport);
              setResearchError("");
              refresh();
            }
          } catch (error) {
            if (!stopped) setResearchError(error.message);
          }

          return;
        }

        if (result.status === "failed") {
          setResearchError(
            "This research request did not complete. Start another request to retry."
          );
          refresh();
          return;
        }
      } catch (error) {
        if (!stopped) setResearchError(error.message);
      }

      if (!stopped) timer = window.setTimeout(poll, 2500);
    }

    timer = window.setTimeout(poll, 500);

    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [token, job?.run_id, job?.status, refresh]);

  async function connect(value) {
    setToken(value);
    setConnectionError("");
  }

  function disconnect() {
    setToken("");
    setOverview(null);
    setJob(null);
    setReport(null);
    setResearchError("");
    setConnectionError("");
  }

  async function submit() {
    if (!token || busy || PENDING.includes(job?.status)) return;

    setBusy(true);
    setResearchError("");
    setReport(null);
    setJob(null);

    try {
      const receipt = await api.submit(token, ticker);

      setJob({
        ...receipt,
        agent_statuses: {
          financial: "pending",
          news: "pending",
          strategic: "pending",
        },
      });
    } catch (error) {
      setResearchError(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function openJob(selected) {
    if (!token) return;

    const requestId = ++activeLoad.current;

    setView("research");
    setMenuOpen(false);
    setResearchError("");
    setReport(null);

    setJob({
      ...selected,
      agent_statuses: {},
    });

    try {
      const latest = await api.job(token, selected.run_id);

      if (requestId !== activeLoad.current) return;

      setJob(latest);
      setTicker(latest.ticker);

      if (latest.status === "completed") {
        const savedReport = await api.report(token, latest.run_id);

        if (requestId === activeLoad.current) {
          setReport(savedReport);
        }
      }
    } catch (error) {
      if (requestId === activeLoad.current) {
        setResearchError(error.message);
      }
    }
  }

  function navigate(next) {
    setView(next);
    setMenuOpen(false);
  }

  return (
    <div className="app">
      <aside className={`sidebar ${menuOpen ? "mobile-open" : ""}`}>
        <div className="brand">
          <div className="brand-symbol">
            <BarChart3 size={23} strokeWidth={2.4} />
          </div>
          <div>
            <strong>InsightIQ</strong>
            <span>Business research</span>
          </div>
          <button
            className="mobile-close"
            onClick={() => setMenuOpen(false)}
            aria-label="Close navigation"
          >
            <X size={20} />
          </button>
        </div>

        <div className="nav-section-label">WORKSPACE</div>

        <nav className="main-nav" aria-label="Main navigation">
          {NAV.map(({ id, label, icon: Icon }) => (
            <button
              type="button"
              key={id}
              className={`nav-link ${view === id ? "active" : ""}`}
              onClick={() => navigate(id)}
              aria-current={view === id ? "page" : undefined}
            >
              <Icon size={19} />
              <span>{label}</span>
              {view === id && <ChevronRight size={15} className="nav-arrow" />}
            </button>
          ))}
        </nav>

        <div className="sidebar-spacer" />

        <div className="sidebar-help">
          <div className="help-icon">
            <BookOpen size={19} />
          </div>
          <strong>Research with confidence</strong>
          <p>
            Every report stays connected to its underlying evidence.
          </p>
        </div>

        <div className="sidebar-foot">
          <span className="version-dot" />
          InsightIQ · Development
        </div>
      </aside>

      {menuOpen && (
        <button
          className="mobile-overlay"
          aria-label="Close navigation"
          onClick={() => setMenuOpen(false)}
        />
      )}

      <div className="workspace">
        <header className="topbar">
          <div className="topbar-left">
            <button
              type="button"
              className="icon-button mobile-menu"
              aria-label="Open navigation"
              onClick={() => setMenuOpen(true)}
            >
              <Menu size={21} />
            </button>
            <div className="breadcrumb">
              <span>Workspace</span>
              <ChevronRight size={14} />
              <strong>{NAV.find((item) => item.id === view)?.label}</strong>
            </div>
          </div>

          <div className="topbar-right">
            <span className={`connection-status ${token && overview ? "online" : ""}`}>
              <span />
              {token && overview ? "Connected" : "Not connected"}
            </span>

            <ThemeToggle
              theme={theme}
              toggle={() =>
                setTheme((value) => value === "dark" ? "light" : "dark")
              }
            />

            {token && (
              <button
                type="button"
                className="icon-button"
                onClick={disconnect}
                title="Disconnect"
                aria-label="Disconnect"
              >
                <LogOut size={18} />
              </button>
            )}
          </div>
        </header>

        <main className="content">
          {!token && (
            <ConnectCard connect={connect} loading={loading} />
          )}

          {connectionError && (
            <div className="message-error" role="alert">
              {connectionError}
            </div>
          )}

          {view === "overview" && (
            <Overview
              data={overview}
              loading={loading}
              refresh={refresh}
              openJob={openJob}
              startResearch={() => navigate("research")}
            />
          )}

          {view === "research" && (
            <Research
              token={token}
              ticker={ticker}
              setTicker={setTicker}
              job={job}
              report={report}
              busy={busy}
              error={researchError}
              submit={submit}
            />
          )}

          {view === "activity" && (
            <ActivityView data={overview} />
          )}

          <footer className="page-footer">
            <span>InsightIQ</span>
            <span>Evidence-driven business research</span>
          </footer>
        </main>
      </div>
    </div>
  );
}
