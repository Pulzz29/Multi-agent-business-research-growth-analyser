import { useState } from "react";
import { AlertTriangle, ChevronRight, Inbox, Info, Moon, RefreshCw, Sun } from "lucide-react";
import { humanize } from "../lib/format.js";

/** Long stored text (e.g. publisher feed descriptions) clamped to four lines, fully expandable. */
export function ClampText({ label, text, limit = 360, className = "" }) {
  const [open, setOpen] = useState(false);
  if (!text) return null;
  const long = text.length > limit;
  return (
    <div className={className}>
      <p className={long && !open ? "clamp" : undefined}>
        {label && <span className="label-inline">{label} </span>}
        {text}
      </p>
      {long && (
        <button type="button" className="clamp-toggle" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
          {open ? "Show less" : "Show full text"}
        </button>
      )}
    </div>
  );
}

export function Badge({ tone = "neutral", children, title }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  );
}

const STATUS_TONES = {
  completed: "positive",
  verified: "positive",
  running: "info",
  queued: "info",
  pending: "neutral",
  failed: "negative",
  unavailable: "warning",
  unknown: "neutral",
};

export function StatusBadge({ status, label }) {
  const tone = STATUS_TONES[status] || "neutral";
  return (
    <Badge tone={tone}>
      <span className={`dot dot-${tone}`} aria-hidden="true" />
      {label || humanize(status || "unknown")}
    </Badge>
  );
}

export function Card({ title, subtitle, action, children, className = "", id, eyebrow }) {
  return (
    <section className={`card ${className}`} id={id} aria-labelledby={title && id ? `${id}-title` : undefined}>
      {(title || action) && (
        <header className="card-head">
          <div>
            {eyebrow && <p className="eyebrow">{eyebrow}</p>}
            {title && (
              <h2 className="card-title" id={id ? `${id}-title` : undefined}>
                {title}
              </h2>
            )}
            {subtitle && <p className="card-subtitle">{subtitle}</p>}
          </div>
          {action && <div className="card-action">{action}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({ label, value, title, sub, tone, icon: Icon }) {
  return (
    <div className="stat">
      <div className="stat-label">
        {Icon && <Icon size={15} aria-hidden="true" />}
        <span>{label}</span>
      </div>
      <div className={`stat-value ${tone ? `tone-${tone}` : ""}`} title={title}>
        {value}
      </div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

export function EmptyState({ title, children, icon: Icon = Inbox, action }) {
  return (
    <div className="empty">
      <Icon size={22} aria-hidden="true" />
      <div>
        <p className="empty-title">{title}</p>
        {children && <div className="empty-body">{children}</div>}
        {action && <div className="empty-action">{action}</div>}
      </div>
    </div>
  );
}

export function ErrorState({ message, onRetry }) {
  return (
    <div className="alert alert-error" role="alert">
      <AlertTriangle size={18} aria-hidden="true" />
      <div className="alert-body">
        <p>{message}</p>
      </div>
      {onRetry && (
        <button type="button" className="btn btn-ghost btn-sm" onClick={onRetry}>
          <RefreshCw size={14} aria-hidden="true" /> Retry
        </button>
      )}
    </div>
  );
}

export function Notice({ tone = "info", children, title }) {
  return (
    <div className={`alert alert-${tone}`} role={tone === "error" ? "alert" : "note"}>
      {tone === "warning" || tone === "error" ? (
        <AlertTriangle size={17} aria-hidden="true" />
      ) : (
        <Info size={17} aria-hidden="true" />
      )}
      <div className="alert-body">
        {title && <p className="alert-title">{title}</p>}
        <div>{children}</div>
      </div>
    </div>
  );
}

export function Skeleton({ lines = 3, height }) {
  return (
    <div className="skeleton-wrap" aria-busy="true" aria-label="Loading">
      {height ? (
        <div className="skeleton" style={{ height }} />
      ) : (
        Array.from({ length: lines }, (_, i) => (
          <div key={i} className="skeleton" style={{ width: `${92 - i * 14}%` }} />
        ))
      )}
    </div>
  );
}

export function Tabs({ tabs, active, onChange, label }) {
  function onKeyDown(event) {
    const index = tabs.findIndex((t) => t.id === active);
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      const next = (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      onChange(tabs[next].id);
      document.getElementById(`tab-${tabs[next].id}`)?.focus();
    }
  }
  return (
    <div className="tabs" role="tablist" aria-label={label} onKeyDown={onKeyDown}>
      {tabs.map((tab) => (
        <button
          key={tab.id}
          id={`tab-${tab.id}`}
          role="tab"
          type="button"
          aria-selected={active === tab.id}
          tabIndex={active === tab.id ? 0 : -1}
          className={`tab ${active === tab.id ? "is-active" : ""}`}
          onClick={() => onChange(tab.id)}
        >
          {tab.icon && <tab.icon size={15} aria-hidden="true" />}
          {tab.label}
          {tab.count !== undefined && tab.count !== null && <span className="tab-count">{tab.count}</span>}
        </button>
      ))}
    </div>
  );
}

/** Collapsible technical evidence. Default view stays readable; full detail remains available. */
export function EvidencePanel({ label = "Evidence details", rows, children }) {
  const visible = (rows || []).filter(([, value]) => value !== undefined && value !== null && value !== "");
  return (
    <details className="evidence">
      <summary>
        <ChevronRight size={14} aria-hidden="true" className="evidence-chevron" />
        {label}
      </summary>
      <div className="evidence-body">
        {visible.length > 0 && (
          <dl className="kv">
            {visible.map(([key, value]) => (
              <div key={key} className="kv-row">
                <dt>{key}</dt>
                <dd>{renderValue(value)}</dd>
              </div>
            ))}
          </dl>
        )}
        {children}
      </div>
    </details>
  );
}

function renderValue(value) {
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "None";
  if (typeof value === "object") return <code className="code-inline">{JSON.stringify(value)}</code>;
  return String(value);
}

export function CompanyMark({ ticker, size = "md" }) {
  return (
    <span className={`mark mark-${ticker} mark-${size}`} aria-hidden="true">
      {ticker?.slice(0, 1)}
    </span>
  );
}

export function ThemeToggle({ theme, onToggle }) {
  const next = theme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      className="icon-btn"
      onClick={onToggle}
      aria-label={`Switch to ${next} theme`}
      title={`Switch to ${next} theme`}
    >
      {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
    </button>
  );
}

export function ExternalLink({ href, children }) {
  if (!href || !/^https:\/\//.test(href)) return <span>{children}</span>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="ext-link">
      {children}
    </a>
  );
}

export function SectionHeading({ id, title, subtitle, action }) {
  return (
    <div className="section-heading">
      <div>
        <h2 id={id}>{title}</h2>
        {subtitle && <p>{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}
