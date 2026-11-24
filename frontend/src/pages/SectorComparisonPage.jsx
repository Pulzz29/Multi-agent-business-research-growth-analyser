import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Download, Layers, Search, Sparkles, Trophy } from "lucide-react";
import { BarChart, RadarChart } from "../components/charts.jsx";
import { Badge, Card, EmptyState, ErrorState, EvidencePanel, Notice, Skeleton } from "../components/ui.jsx";
import { api } from "../lib/api.js";
import { formatDateTime } from "../lib/format.js";
import { MARKET_LABELS, buildComparisonModel } from "../lib/sectorModel.js";
import { MarketToggle } from "./MarketPage.jsx";

/* ------------------------------------------------------------ sector list */

function SectorList({ token, market, navigate, onAuthError }) {
  const [search, setSearch] = useState("");
  const [state, setState] = useState({ loading: true, error: "", sectors: [] });

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      api
        .sectors(token, market, search.trim(), controller.signal)
        .then((d) => setState({ loading: false, error: "", sectors: d.sectors || [] }))
        .catch((err) => {
          if (err?.name === "AbortError") return;
          if (err.status === 401) onAuthError?.();
          setState({ loading: false, error: err.message, sectors: [] });
        });
    }, 200);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [token, market, search, onAuthError]);

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <p className="eyebrow">Sector-wise comparison</p>
          <h1>{MARKET_LABELS[market]} sectors</h1>
          <p className="page-lede">Choose a sector to rank its companies with a transparent weighted scoring model.</p>
        </div>
        <MarketToggle market={market} onChange={(m) => navigate({ page: "sectors", market: m })} />
      </header>
      <label className="search-field">
        <Search size={16} aria-hidden="true" />
        <span className="sr-only">Search sectors</span>
        <input type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search sectors, e.g. Banking" maxLength={60} />
      </label>
      {state.error && <ErrorState message={state.error} />}
      {state.loading ? (
        <Card>
          <Skeleton lines={4} />
        </Card>
      ) : !state.sectors.length ? (
        <EmptyState title={`No sector matches “${search}”`} icon={Layers} />
      ) : (
        <div className="sector-grid">
          {state.sectors.map((s) => (
            <button key={s.key} type="button" className="sector-card" onClick={() => navigate({ page: "sectors", market, sector: s.key })}>
              <span className="sector-name">{s.label}</span>
              <span className="muted small">{s.company_count} companies</span>
              <span className="sector-members small">{s.companies.slice(0, 6).map((c) => c.symbol).join(" · ")}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* --------------------------------------------------------- comparison view */

function RankCard({ row, model }) {
  return (
    <article className={`finding rank-card ${row.ranked ? "" : "is-unranked"}`}>
      <header className="finding-head">
        <div className="rank-title">
          <span className="rank-badge" aria-label={row.ranked ? `Rank ${row.rank}` : "Not ranked"}>
            {row.ranked ? `#${row.rank}` : "—"}
          </span>
          <div>
            <h3 className="finding-title">{row.name}</h3>
            <p className="finding-kicker">{row.symbol}</p>
          </div>
        </div>
        <div className="rank-score">
          <strong>{row.ranked ? row.score.toFixed(2) : "Not ranked"}</strong>
          <span className="muted small">{row.ranked ? "score / 100" : row.notRankedReason}</span>
        </div>
      </header>
      <div className="finding-body">
        <p>{row.why}</p>
        {(row.strengths.length > 0 || row.weaknesses.length > 0) && (
          <div className="sw-grid">
            {row.strengths.length > 0 && (
              <div>
                <p className="label-inline">Strengths</p>
                <ul className="plain-list">
                  {row.strengths.map((s) => (
                    <li key={s}>{s}</li>
                  ))}
                </ul>
              </div>
            )}
            {row.weaknesses.length > 0 && (
              <div>
                <p className="label-inline">Weaknesses</p>
                <ul className="plain-list">
                  {row.weaknesses.map((s) => (
                    <li key={s}>{s}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
        <p className="small muted">
          {row.textSource === "ai" ? (
            <>
              <Sparkles size={12} aria-hidden="true" /> AI-written explanation, checked against the comparison data.
            </>
          ) : (
            "Deterministic explanation from the score breakdown."
          )}
        </p>
      </div>
      <EvidencePanel label="Score breakdown">
        <div className="table-scroll">
          <table className="mini-table">
            <thead>
              <tr>
                <th>Metric</th>
                <th className="num">Value</th>
                <th className="num">Score</th>
                <th className="num">Weight</th>
                <th className="num">Contribution</th>
              </tr>
            </thead>
            <tbody>
              {row.breakdown.map((b) => (
                <tr key={b.metric}>
                  <td>
                    {b.label}
                    {b.direction === "lower_is_better" && <span className="muted small"> (lower is better)</span>}
                  </td>
                  <td className="num">
                    {row.values[b.metric]?.display}
                    {b.imputed && <Badge tone="warning">imputed</Badge>}
                  </td>
                  <td className="num">{b.normalized_score.toFixed(0)}</td>
                  <td className="num">{(b.weight * 100).toFixed(1)}%</td>
                  <td className="num">{b.contribution.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="small muted">
          Data coverage {((row.coverage || 0) * 100).toFixed(0)}% of applied weight
          {row.inputs.fiscal_period_end ? ` · fiscal year ending ${row.inputs.fiscal_period_end} (${row.inputs.statement_provider})` : ""}
          {row.inputs.quote ? ` · quote ${row.inputs.quote.provider}, ${formatDateTime(row.inputs.quote.fetched_at)}` : ""}
        </p>
      </EvidencePanel>
    </article>
  );
}

function ComparisonView({ model, token }) {
  const [download, setDownload] = useState({ busy: null, error: "" });

  async function save(format) {
    setDownload({ busy: format, error: "" });
    try {
      const { blob, filename } = await api.downloadComparisonReport(token, model.id, format);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setDownload({ busy: null, error: "" });
    } catch (err) {
      setDownload({ busy: null, error: err.message });
    }
  }

  const ranked = model.rows.filter((r) => r.ranked);
  return (
    <>
      <Card
        title={`${model.sectorLabel} · ${model.marketLabel}`}
        subtitle={`Generated ${formatDateTime(model.generatedAt)} · ${model.rankedCount} of ${model.rows.length} companies ranked · values in ${model.currency}`}
        action={
          <div className="btn-row">
            <button type="button" className="btn btn-primary btn-sm" onClick={() => save("pdf")} disabled={!!download.busy}>
              <Download size={14} aria-hidden="true" /> {download.busy === "pdf" ? "Preparing…" : "Download PDF"}
            </button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => save("docx")} disabled={!!download.busy}>
              <Download size={14} aria-hidden="true" /> {download.busy === "docx" ? "Preparing…" : "DOCX"}
            </button>
          </div>
        }
      >
        {download.error && <Notice tone="error">Download failed: {download.error}</Notice>}
        <p>{model.overview}</p>
        <p className="small muted">
          {model.overviewSource === "ai" ? model.narrativeLabel : "Deterministic overview from the scoring model."}
          {model.narrativeStatus !== "completed" && model.narrativeReason ? ` ${model.narrativeReason}` : ""}
        </p>
        {model.status !== "ranked" && (
          <Notice tone="warning" title="Ranking not possible">
            {model.status === "insufficient_companies"
              ? "Fewer than two companies in this sector have enough data to be ranked."
              : "No metric has data for at least two companies."}
          </Notice>
        )}
        {model.ties.length > 0 && (
          <Notice tone="info">Tied scores share a rank: {model.ties.map((t) => t.join(" = ")).join("; ")}.</Notice>
        )}
        <p className="small muted">
          Sources: {Object.entries(model.dataSources).map(([k, v]) => `${k} ${v}`).join(" · ")}
        </p>
      </Card>

      {ranked.length > 0 && (
        <div className="chart-grid">
          <Card title="Ranking scores" subtitle="Composite weighted score (0–100)">
            <BarChart data={model.scoreBars} yFormat={(v) => `${Math.round(v)}`} ariaLabel="Composite ranking score by company" />
          </Card>
          <Card title="Metric profile" subtitle="Top companies' relative metric scores">
            <RadarChart axes={model.radar.axes} series={model.radar.series} ariaLabel="Radar chart of relative metric scores" />
          </Card>
          {model.metricBarLegend.length > 0 && (
            <Card title="Key metrics compared" subtitle="Relative scores for growth, margin and returns" className="chart-wide">
              <BarChart data={model.metricBars} yFormat={(v) => `${Math.round(v)}`} legend={model.metricBarLegend} ariaLabel="Relative scores for key metrics" />
            </Card>
          )}
        </div>
      )}

      <Card title="Ranked companies" subtitle="Rank, score and why each company ranks where it does">
        <div className="rank-list">
          {model.rows.map((row) => (
            <RankCard key={row.symbol} row={row} model={model} />
          ))}
        </div>
      </Card>

      <Card title="Comparison table" subtitle="Raw values; * marks a missing value replaced by the sector median for scoring">
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Rank</th>
                <th scope="col">Company</th>
                <th scope="col" className="num">
                  Score
                </th>
                {model.metrics.map((m) => (
                  <th key={m.key} scope="col" className="num" title={`Weight ${((m.weight || 0) * 100).toFixed(1)}%`}>
                    {m.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {model.rows.map((row) => (
                <tr key={row.symbol}>
                  <td>{row.ranked ? row.rank : "—"}</td>
                  <td>
                    {row.name} <span className="muted small">{row.symbol}</span>
                  </td>
                  <td className="num">{row.ranked ? row.score.toFixed(2) : "—"}</td>
                  {model.metrics.map((m) => (
                    <td key={m.key} className="num">
                      {row.values[m.key].display}
                      {row.values[m.key].imputed ? " *" : ""}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="Methodology" subtitle="How the ranking is computed">
        <ul className="plain-list">
          {model.methodology.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        {model.droppedMetrics.length > 0 && (
          <p className="small muted">Not used: {model.droppedMetrics.map((d) => `${d.label} (${d.reason})`).join("; ")}.</p>
        )}
        <ul className="plain-list small muted">
          {model.disclaimers.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        {model.withheld.length > 0 && (
          <EvidencePanel label={`AI text withheld after validation (${model.withheld.length})`}>
            <ul className="plain-list small">
              {model.withheld.map((w, i) => (
                <li key={`${w.item}-${i}`}>
                  {w.item}: {w.reason}
                </li>
              ))}
            </ul>
          </EvidencePanel>
        )}
      </Card>
    </>
  );
}

function SectorDetail({ token, market, sector, navigate, onAuthError }) {
  const [state, setState] = useState({ loading: true, error: "", status: 0, snapshot: null });
  const [generate, setGenerate] = useState({ busy: false, error: "" });
  const [reloadKey, setReloadKey] = useState(0);
  const busy = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: true, error: "", status: 0, snapshot: null });
    api
      .latestComparison(token, market, sector, controller.signal)
      .then((snapshot) => setState({ loading: false, error: "", status: 200, snapshot }))
      .catch((err) => {
        if (err?.name === "AbortError") return;
        if (err.status === 401) onAuthError?.();
        setState({ loading: false, error: err.message, status: err.code === "route_missing" ? 0 : err.status, snapshot: null });
      });
    return () => controller.abort();
  }, [token, market, sector, reloadKey, onAuthError]);

  const run = useCallback(async () => {
    if (busy.current) return;
    const ok = window.confirm(
      "Generate a new sector comparison?\n\n" +
        "• Stale company data is refreshed from the market's data providers (bounded; may take a minute).\n" +
        "• The ranking is computed deterministically.\n" +
        "• One Groq request writes the explanation; every number is checked before it is shown.\n" +
        "• The result is saved as a new comparison; earlier comparisons are kept."
    );
    if (!ok) return;
    busy.current = true;
    setGenerate({ busy: true, error: "" });
    try {
      const snapshot = await api.generateComparison(token, market, sector, true);
      setState({ loading: false, error: "", status: 200, snapshot });
      setGenerate({ busy: false, error: "" });
    } catch (err) {
      if (err.status === 401) onAuthError?.();
      setGenerate({ busy: false, error: err.message });
    } finally {
      busy.current = false;
    }
  }, [token, market, sector, onAuthError]);

  const model = useMemo(() => buildComparisonModel(state.snapshot), [state.snapshot]);
  const label = model?.sectorLabel || sector;

  return (
    <div className="page">
      <button type="button" className="btn btn-ghost btn-sm back-link" onClick={() => navigate({ page: "sectors", market })}>
        <ArrowLeft size={14} aria-hidden="true" /> {MARKET_LABELS[market]} sectors
      </button>
      <header className="page-head">
        <div>
          <p className="eyebrow">Sector-wise comparison · {MARKET_LABELS[market]}</p>
          <h1>{label}</h1>
        </div>
        <button type="button" className="btn btn-primary" onClick={run} disabled={generate.busy}>
          <Trophy size={16} aria-hidden="true" />
          {generate.busy ? "Comparing companies…" : state.snapshot ? "Generate new comparison" : "Generate comparison"}
        </button>
      </header>
      {generate.busy && <Notice tone="info">Refreshing company data, ranking and writing the explanation. This can take a minute.</Notice>}
      {generate.error && <Notice tone="error">The comparison could not be generated: {generate.error}</Notice>}
      {state.loading ? (
        <Card>
          <Skeleton lines={6} />
        </Card>
      ) : state.status === 404 && !state.snapshot ? (
        <EmptyState title="No comparison for this sector yet" icon={Trophy}>
          Generate a comparison to load company data and rank the sector. Earlier comparisons are kept and the latest is shown here.
        </EmptyState>
      ) : state.error ? (
        <ErrorState message={state.error} onRetry={() => setReloadKey((k) => k + 1)} />
      ) : model ? (
        <ComparisonView model={model} token={token} />
      ) : (
        <ErrorState message="This comparison could not be read." />
      )}
    </div>
  );
}

export function SectorComparisonPage({ token, market, sector, navigate, onAuthError }) {
  if (!sector) return <SectorList token={token} market={market} navigate={navigate} onAuthError={onAuthError} />;
  return <SectorDetail token={token} market={market} sector={sector} navigate={navigate} onAuthError={onAuthError} />;
}
