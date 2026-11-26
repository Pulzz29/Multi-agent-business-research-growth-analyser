import { useCallback, useEffect, useMemo, useState } from "react";
import { Download, FileText } from "lucide-react";
import { Card, CompanyMark, EmptyState, ErrorState, Skeleton } from "../components/ui.jsx";
import { api } from "../lib/api.js";
import { runReportRoute } from "../lib/auth.js";
import { formatReportTime, shortId } from "../lib/format.js";
import { MARKET_NAMES } from "../lib/marketOverviewModel.js";
import { filterSavedReports } from "../lib/savedReports.js";

/**
 * Every saved research report (US and India), newest first. Viewing or
 * downloading reads the stored report; nothing is regenerated and no AI or
 * provider call is made.
 */
export function SavedReportsPage({ token, navigate, onAuthError }) {
  const [state, setState] = useState({ loading: true, error: "", reports: [] });
  const [market, setMarket] = useState("ALL");
  const [company, setCompany] = useState("ALL");
  const [downloading, setDownloading] = useState(null);
  const [downloadError, setDownloadError] = useState("");

  const load = useCallback(
    (signal) =>
      api
        .savedReports(token, signal)
        .then((body) => setState({ loading: false, error: "", reports: body.reports || [] }))
        .catch((err) => {
          if (err?.name === "AbortError") return;
          if (err?.status === 401) onAuthError?.();
          setState({ loading: false, error: err.message, reports: [] });
        }),
    [token, onAuthError]
  );

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const companies = useMemo(() => {
    const seen = new Map();
    for (const r of state.reports) if (market === "ALL" || r.market === market) seen.set(r.ticker, r.name);
    return [...seen.entries()].sort((a, b) => a[1].localeCompare(b[1]));
  }, [state.reports, market]);
  const rows = filterSavedReports(state.reports, market, company);

  async function download(report) {
    setDownloading(report.run_id);
    setDownloadError("");
    try {
      const { blob, filename } = await api.downloadReportPdf(token, report.ticker, report.run_id);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      if (err?.status === 401) onAuthError?.();
      setDownloadError(`${report.name}: ${err.message}`);
    } finally {
      setDownloading(null);
    }
  }

  const view = (report) => {
    const route = runReportRoute({ ...report, report_available: true }, () => report.sec_validated);
    if (route) navigate(route);
  };

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <p className="eyebrow">Research library</p>
          <h1>Saved Reports</h1>
          <p className="page-lede">Every saved multi-agent research report, newest first. Viewing or downloading never re-runs research.</p>
        </div>
      </header>

      <Card
        title="Reports"
        subtitle={state.loading ? "Loading…" : `${rows.length} of ${state.reports.length} reports`}
        action={
          <div className="saved-filters">
            <label className="select-wrap">
              <span className="sr-only">Filter by market</span>
              <select
                value={market}
                onChange={(e) => {
                  setMarket(e.target.value);
                  setCompany("ALL");
                }}
              >
                <option value="ALL">All markets</option>
                <option value="US">{MARKET_NAMES.US}</option>
                <option value="IN">{MARKET_NAMES.IN}</option>
              </select>
            </label>
            <label className="select-wrap">
              <span className="sr-only">Filter by company</span>
              <select value={company} onChange={(e) => setCompany(e.target.value)}>
                <option value="ALL">All companies</option>
                {companies.map(([ticker, name]) => (
                  <option key={ticker} value={ticker}>
                    {name} ({ticker})
                  </option>
                ))}
              </select>
            </label>
          </div>
        }
      >
        {downloadError && <p className="field-error small" role="alert">{downloadError}</p>}
        {state.loading ? (
          <Skeleton lines={6} />
        ) : state.error ? (
          <ErrorState message={state.error} onRetry={() => load()} />
        ) : !rows.length ? (
          <EmptyState title="No saved reports match" icon={FileText} />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Company</th>
                  <th scope="col">Market</th>
                  <th scope="col">Report</th>
                  <th scope="col">Generated</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.run_id}>
                    <td>
                      <span className="inline-company">
                        <CompanyMark ticker={r.ticker} name={r.name} size="sm" /> {r.name}{" "}
                        <code className="code-inline">{r.ticker}</code>
                      </span>
                    </td>
                    <td>{MARKET_NAMES[r.market] || r.market}</td>
                    <td>
                      {r.title}
                      <span className="small muted" style={{ display: "block" }}>
                        Run {shortId(r.run_id)}
                      </span>
                    </td>
                    <td>{formatReportTime(r.generated_at_utc)}</td>
                    <td>
                      <div className="saved-actions">
                        <button type="button" className="btn btn-sm" onClick={() => view(r)} aria-label={`View ${r.name} report ${shortId(r.run_id)}`}>
                          <FileText size={14} aria-hidden="true" /> View Report
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm btn-primary"
                          onClick={() => download(r)}
                          disabled={downloading === r.run_id}
                          aria-label={`Download PDF of ${r.name} report ${shortId(r.run_id)}`}
                        >
                          <Download size={14} aria-hidden="true" /> {downloading === r.run_id ? "Preparing…" : "Download PDF"}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
