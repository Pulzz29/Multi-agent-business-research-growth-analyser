import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Download, Layers, ListChecks, Search, Sparkles, Trophy } from "lucide-react";
import { BarChart, RadarChart } from "../components/charts.jsx";
import { Badge, Card, EmptyState, ErrorState, EvidencePanel, Notice, Skeleton } from "../components/ui.jsx";
import { api } from "../lib/api.js";
import { formatDateTime } from "../lib/format.js";
import { replaceHash } from "../lib/router.js";
import { MARKET_LABELS, buildComparisonModel } from "../lib/sectorModel.js";
import { filterCompanies, initialSelection, selectAll, selectionStatus, toggle } from "../lib/selection.js";
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
        title={
          model.isCustom
            ? `Custom comparison: ${model.sectorLabel}, ${model.rows.length} companies · ${model.marketLabel}`
            : `${model.sectorLabel} · ${model.marketLabel}`
        }
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
        {model.isCustom && (
          <Notice tone="info" title={`Custom comparison of ${model.selectedCompanies.length} selected companies`}>
            {model.relativeNote} Compared: {model.selectedCompanies.map((c) => `${c.name || c.symbol} (${c.symbol})`).join(", ")}.
            {model.duplicatesRemoved.length > 0 ? ` Duplicates removed: ${model.duplicatesRemoved.join(", ")}.` : ""}
          </Notice>
        )}
        <p>{model.overview}</p>
        <p className="small muted">
          {model.overviewSource === "ai" ? model.narrativeLabel : "Deterministic overview from the scoring model."}
          {model.narrativeStatus !== "completed" && model.narrativeReason ? ` ${model.narrativeReason}` : ""}
        </p>
        {model.status !== "ranked" && (
          <Notice tone="warning" title="Ranking not possible">
            {model.status === "insufficient_companies"
              ? model.isCustom
                ? "Fewer than two of the selected companies have enough data to be ranked."
                : "Fewer than two companies in this sector have enough data to be ranked."
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

      <Card
        title="Comparison table"
        subtitle={`Raw values; * marks a missing value replaced by the ${model.isCustom ? "median of the selected companies" : "sector median"} for scoring`}
      >
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

/* ------------------------------------------- custom comparison (selection) */

function SelectionPanel({ label, members, limits, selected, setSelected, ignored, busy, onCompare, onRetry }) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => filterCompanies(members.list, query), [members.list, query]);
  const status = limits ? selectionStatus(selected, limits) : null;
  const names = useMemo(() => Object.fromEntries(members.list.map((c) => [c.symbol, c.name])), [members.list]);

  return (
    <Card
      title="Select companies to compare"
      subtitle={limits ? `Choose ${limits.min}–${limits.max} companies from ${label}; they are ranked against each other only` : `Companies in ${label}`}
    >
      {members.loading ? (
        <Skeleton lines={4} />
      ) : members.error ? (
        <ErrorState message={members.error} onRetry={onRetry} />
      ) : (
        <>
          {ignored.length > 0 && (
            <Notice tone="warning">Not in this sector, so not selected: {ignored.join(", ")}.</Notice>
          )}
          {limits && members.list.length < limits.min && (
            <Notice tone="warning">
              This sector lists only {members.list.length} {members.list.length === 1 ? "company" : "companies"}; a custom comparison needs at least {limits.min}.
            </Notice>
          )}
          <label className="search-field">
            <Search size={16} aria-hidden="true" />
            <span className="sr-only">Filter companies</span>
            <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Filter by name or symbol" maxLength={60} />
          </label>
          <div className="selection-toolbar">
            <span className={`selection-count ${status && !status.valid ? "is-invalid" : ""}`} aria-live="polite">
              {status?.label}
            </span>
            <div className="btn-row">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => setSelected((s) => selectAll(filtered, s, limits.max))}
                disabled={!limits || !filtered.length || selected.length >= limits.max || busy}
              >
                {query.trim() ? "Select shown" : "Select all"}
              </button>
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => setSelected([])} disabled={!selected.length || busy}>
                Clear
              </button>
            </div>
          </div>
          {!filtered.length ? (
            <p className="muted small">No companies match “{query.trim()}”.</p>
          ) : (
            <ul className="select-list" aria-label={`Companies in ${label}`}>
              {filtered.map((c) => {
                const checked = selected.includes(c.symbol);
                return (
                  <li key={c.symbol}>
                    <label className={`select-item ${checked ? "is-selected" : ""}`}>
                      <input type="checkbox" checked={checked} disabled={busy} onChange={() => setSelected((s) => toggle(s, c.symbol))} />
                      <span className="result-name">{c.name}</span>
                      <span className="muted small">{c.symbol}</span>
                      {c.in_universe === false && (
                        <Badge tone="info" title="Added from a provider search; mapped to this sector">
                          added
                        </Badge>
                      )}
                    </label>
                  </li>
                );
              })}
            </ul>
          )}
          {selected.length > 0 && (
            <p className="small muted">Selected: {selected.map((s) => `${names[s] || s} (${s})`).join(", ")}</p>
          )}
          <div className="selection-actions">
            <button
              type="button"
              className="btn btn-primary"
              onClick={onCompare}
              disabled={!status?.valid || busy}
              aria-describedby={status && !status.valid ? "compare-reason" : undefined}
            >
              <ListChecks size={16} aria-hidden="true" />
              {busy ? "Comparing companies…" : `Compare ${selected.length} selected`}
            </button>
            {status && !status.valid && (
              <span id="compare-reason" className="small muted">
                {status.reason}
              </span>
            )}
          </div>
        </>
      )}
    </Card>
  );
}

function ModeSwitch({ mode, onChange }) {
  const options = [
    ["sector", "Entire sector", Layers],
    ["custom", "Selected companies", ListChecks],
  ];
  return (
    <div className="segmented" role="tablist" aria-label="Comparison mode">
      {options.map(([id, text, Icon]) => (
        <button
          key={id}
          type="button"
          role="tab"
          aria-selected={mode === id}
          className={`segmented-item ${mode === id ? "is-active" : ""}`}
          onClick={() => onChange(id)}
        >
          <Icon size={14} aria-hidden="true" /> {text}
        </button>
      ))}
    </div>
  );
}

function SectorDetail({ token, market, sector, companies: requested, navigate, onAuthError }) {
  const [mode, setMode] = useState(requested?.length ? "custom" : "sector");
  const [state, setState] = useState({ loading: true, error: "", status: 0, snapshot: null });
  const [generate, setGenerate] = useState({ busy: false, error: "" });
  const [reloadKey, setReloadKey] = useState(0);
  const busy = useRef(false);

  // Custom comparison state lives here so it survives switching modes; the
  // page is keyed by market and sector, so changing sector starts fresh.
  const [members, setMembers] = useState({ loading: true, error: "", list: [], limits: null });
  const [membersKey, setMembersKey] = useState(0);
  const [selected, setSelected] = useState([]);
  const [ignored, setIgnored] = useState([]);
  const [custom, setCustom] = useState({ busy: false, error: "", snapshot: null });
  const appliedRequest = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    setMembers((m) => ({ ...m, loading: true, error: "" }));
    api
      .sectors(token, market, "", controller.signal)
      .then((data) => {
        const entry = (data.sectors || []).find((s) => s.key === sector);
        const limits = data.custom_comparison
          ? { min: data.custom_comparison.min_companies, max: data.custom_comparison.max_companies }
          : null;
        if (!entry) {
          setMembers({ loading: false, error: "This sector is not available for this market.", list: [], limits });
        } else if (!limits) {
          setMembers({
            loading: false,
            error: "The backend does not support custom comparisons yet. Restart the backend (scripts\\run_backend.py) and reload this page.",
            list: [],
            limits: null,
          });
        } else {
          setMembers({ loading: false, error: "", list: entry.companies || [], limits, label: entry.label });
          if (!appliedRequest.current && requested?.length) {
            appliedRequest.current = true;
            const initial = initialSelection(requested, entry.companies || []);
            setSelected(initial.selected);
            setIgnored(initial.ignored);
          }
        }
      })
      .catch((err) => {
        if (err?.name === "AbortError") return;
        if (err.status === 401) onAuthError?.();
        setMembers({ loading: false, error: err.message, list: [], limits: null });
      });
    return () => controller.abort();
    // `requested` is read once (appliedRequest); its identity must not re-trigger loads.
  }, [token, market, sector, membersKey, onAuthError]);

  const changeMode = useCallback(
    (next) => {
      setMode(next);
      const shown = next === "custom" && custom.snapshot ? (custom.snapshot.selected_companies || []).map((c) => c.symbol) : [];
      replaceHash({ page: "sectors", market, sector, companies: shown });
    },
    [custom.snapshot, market, sector]
  );

  const compare = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    setCustom((c) => ({ ...c, busy: true, error: "" }));
    try {
      const snapshot = await api.generateComparison(token, market, sector, true, selected);
      setCustom({ busy: false, error: "", snapshot });
      replaceHash({ page: "sectors", market, sector, companies: (snapshot.selected_companies || []).map((c) => c.symbol) });
    } catch (err) {
      if (err.status === 401) onAuthError?.();
      setCustom((c) => ({ ...c, busy: false, error: err.message }));
    } finally {
      busy.current = false;
    }
  }, [token, market, sector, selected, onAuthError]);

  const customModel = useMemo(() => buildComparisonModel(custom.snapshot), [custom.snapshot]);
  const resultSymbols = (custom.snapshot?.selected_companies || []).map((c) => c.symbol);
  const selectionChanged =
    customModel && (resultSymbols.length !== selected.length || resultSymbols.some((s) => !selected.includes(s)));

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
  const label = model?.sectorLabel || members.label || sector;

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
        {mode === "sector" && (
          <button type="button" className="btn btn-primary" onClick={run} disabled={generate.busy}>
            <Trophy size={16} aria-hidden="true" />
            {generate.busy ? "Comparing companies…" : state.snapshot ? "Generate new comparison" : "Generate comparison"}
          </button>
        )}
      </header>
      <p className="small muted">
        {mode === "custom" ? "Compare" : "Generating a comparison"} refreshes stale company data from the market's providers and makes one Azure OpenAI
        request for the explanation (every number is checked). The result is saved as a new comparison; earlier ones are kept.
      </p>
      <ModeSwitch mode={mode} onChange={changeMode} />
      {mode === "custom" ? (
        <>
          <SelectionPanel
            label={label}
            members={members}
            limits={members.limits}
            selected={selected}
            setSelected={setSelected}
            ignored={ignored}
            busy={custom.busy}
            onCompare={compare}
            onRetry={() => setMembersKey((k) => k + 1)}
          />
          {custom.busy && (
            <Notice tone="info">Refreshing the selected companies' data, ranking them and writing the explanation. This can take a minute.</Notice>
          )}
          {custom.error && <Notice tone="error">The custom comparison could not be generated: {custom.error}</Notice>}
          {customModel ? (
            <>
              {selectionChanged && (
                <Notice tone="warning">The selection has changed since this comparison. Click Compare to update it.</Notice>
              )}
              <ComparisonView model={customModel} token={token} />
            </>
          ) : (
            !custom.busy && (
              <EmptyState title="No custom comparison yet" icon={ListChecks}>
                Select between {members.limits?.min ?? "two"} and {members.limits?.max ?? "several"} companies above and click Compare. Scores are relative to the selected companies and may differ from the full sector ranking.
              </EmptyState>
            )
          )}
        </>
      ) : (
        <>
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
        </>
      )}
    </div>
  );
}

export function SectorComparisonPage({ token, market, sector, companies, navigate, onAuthError }) {
  if (!sector) return <SectorList token={token} market={market} navigate={navigate} onAuthError={onAuthError} />;
  return <SectorDetail token={token} market={market} sector={sector} companies={companies} navigate={navigate} onAuthError={onAuthError} />;
}
