import { useEffect, useMemo, useRef, useState } from "react";

function useWidth(fallback = 640) {
  const ref = useRef(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    if (!ref.current || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver((entries) => {
      const next = Math.round(entries[0].contentRect.width);
      if (next > 0) setWidth(next);
    });
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

const toTime = (iso) => {
  const [y, m, d] = String(iso).slice(0, 10).split("-").map(Number);
  return Date.UTC(y, m - 1, d);
};

function niceStep(range, target = 5) {
  const raw = range / target;
  const power = 10 ** Math.floor(Math.log10(raw || 1));
  const n = raw / power;
  const step = n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10;
  return step * power;
}

function yTicks(min, max) {
  const step = niceStep(max - min || 1);
  const start = Math.floor(min / step) * step;
  const ticks = [];
  for (let v = start; v <= max + step * 0.001; v += step) ticks.push(Number(v.toFixed(10)));
  return ticks;
}

/**
 * Time-series line chart.
 * series: [{ id, label, tone, dashed, points: [{ x: "YYYY-MM-DD", y, note }], band: [{ x, lo, hi }] }]
 * y values are in the unit given by yFormat (e.g. billions of USD).
 */
export function LineChart({ series, height = 280, yFormat = (v) => v, xFormat, ariaLabel, footnote, yTitle, xTitle }) {
  const [wrapRef, width] = useWidth();
  const [active, setActive] = useState(null);
  const pad = { top: 16, right: 16, bottom: 30, left: 58 };
  const innerW = Math.max(width - pad.left - pad.right, 50);
  const innerH = height - pad.top - pad.bottom;

  const prepared = useMemo(
    () =>
      series
        .filter((s) => s.points?.length || s.band?.length)
        .map((s) => ({
          ...s,
          pts: (s.points || []).filter((p) => p.y !== null && Number.isFinite(p.y)).map((p) => ({ ...p, t: toTime(p.x) })),
          bnd: (s.band || []).map((b) => ({ ...b, t: toTime(b.x) })),
        })),
    [series]
  );

  const domain = useMemo(() => {
    const ts = [];
    const ys = [0];
    prepared.forEach((s) => {
      s.pts.forEach((p) => {
        ts.push(p.t);
        ys.push(p.y);
      });
      s.bnd.forEach((b) => {
        ts.push(b.t);
        ys.push(b.lo, b.hi);
      });
    });
    if (!ts.length) return null;
    const tMin = Math.min(...ts);
    const tMax = Math.max(...ts);
    const yMin = Math.min(...ys);
    const yMax = Math.max(...ys);
    const span = yMax - yMin || 1;
    return { tMin, tMax: tMax === tMin ? tMin + 1 : tMax, yMin: yMin < 0 ? yMin - span * 0.06 : 0, yMax: yMax + span * 0.08 };
  }, [prepared]);

  const xs = useMemo(() => {
    const all = new Set();
    prepared.forEach((s) => s.pts.forEach((p) => all.add(p.x)));
    return [...all].sort();
  }, [prepared]);

  if (!domain) return null;

  const sx = (t) => pad.left + ((t - domain.tMin) / (domain.tMax - domain.tMin)) * innerW;
  const sy = (v) => pad.top + innerH - ((v - domain.yMin) / (domain.yMax - domain.yMin)) * innerH;
  const ticks = yTicks(domain.yMin, domain.yMax).filter((v) => v >= domain.yMin && v <= domain.yMax);

  const years = [];
  const firstYear = new Date(domain.tMin).getUTCFullYear();
  const lastYear = new Date(domain.tMax).getUTCFullYear();
  const yearStep = Math.max(1, Math.ceil((lastYear - firstYear + 1) / Math.max(2, Math.floor(innerW / 70))));
  for (let y = firstYear; y <= lastYear + 1; y += yearStep) {
    const t = Date.UTC(y, 0, 1);
    if (t >= domain.tMin && t <= domain.tMax) years.push(t);
  }

  function onMove(event) {
    if (!xs.length) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const px = ((event.clientX - rect.left) / rect.width) * width;
    let best = 0;
    let bestDist = Infinity;
    xs.forEach((x, i) => {
      const d = Math.abs(sx(toTime(x)) - px);
      if (d < bestDist) {
        bestDist = d;
        best = i;
      }
    });
    setActive(best);
  }

  function onKey(event) {
    if (!xs.length) return;
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      setActive((current) => {
        const base = current === null ? xs.length - 1 : current;
        return Math.min(xs.length - 1, Math.max(0, base + (event.key === "ArrowRight" ? 1 : -1)));
      });
    } else if (event.key === "Escape") {
      setActive(null);
    }
  }

  const activeX = active !== null ? xs[active] : null;
  const tooltipRows = activeX
    ? prepared
        .map((s) => {
          const p = s.pts.find((pt) => pt.x === activeX);
          const b = s.bnd.find((bd) => bd.x === activeX);
          return p ? { s, p, b } : null;
        })
        .filter(Boolean)
    : [];
  const activeLeft = activeX ? sx(toTime(activeX)) : 0;

  return (
    <figure className="chart" ref={wrapRef}>
      {yTitle && <p className="chart-axis-title">{yTitle}</p>}
      <div className="chart-legend" aria-hidden="true">
        {prepared.map((s) => (
          <span key={s.id} className="legend-item">
            <span className={`legend-swatch tone-${s.tone} ${s.dashed ? "is-dashed" : ""}`} />
            {s.label}
          </span>
        ))}
        {prepared.some((s) => s.bnd.length) && (
          <span className="legend-item">
            <span className="legend-swatch legend-band" />
            95% interval (model-based)
          </span>
        )}
      </div>
      <div className="chart-canvas">
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label={ariaLabel}
          tabIndex={0}
          onMouseMove={onMove}
          onMouseLeave={() => setActive(null)}
          onKeyDown={onKey}
          onBlur={() => setActive(null)}
        >
          {ticks.map((v) => (
            <g key={v}>
              <line className="grid" x1={pad.left} x2={width - pad.right} y1={sy(v)} y2={sy(v)} />
              <text className="axis-label" x={pad.left - 8} y={sy(v)} dy="0.32em" textAnchor="end">
                {yFormat(v)}
              </text>
            </g>
          ))}
          {domain.yMin < 0 && <line className="zero-line" x1={pad.left} x2={width - pad.right} y1={sy(0)} y2={sy(0)} />}
          {years.map((t) => (
            <text key={t} className="axis-label" x={sx(t)} y={height - 8} textAnchor="middle">
              {new Date(t).getUTCFullYear()}
            </text>
          ))}
          {prepared.map((s) =>
            s.bnd.length ? (
              <path
                key={`${s.id}-band`}
                className={`band tone-${s.tone}`}
                d={`M${s.bnd.map((b) => `${sx(b.t)},${sy(b.hi)}`).join("L")}L${[...s.bnd]
                  .reverse()
                  .map((b) => `${sx(b.t)},${sy(b.lo)}`)
                  .join("L")}Z`}
              />
            ) : null
          )}
          {prepared.map((s) =>
            s.pts.length ? (
              <g key={s.id}>
                <path
                  className={`line tone-${s.tone} ${s.dashed ? "is-dashed" : ""}`}
                  d={`M${s.pts.map((p) => `${sx(p.t)},${sy(p.y)}`).join("L")}`}
                />
                {(s.dashed || s.pts.length <= 16) &&
                  s.pts.map((p) => (
                    <circle key={p.x} className={`point tone-${s.tone}`} cx={sx(p.t)} cy={sy(p.y)} r={s.dashed ? 3.5 : 2.5} />
                  ))}
              </g>
            ) : null
          )}
          {activeX && (
            <g>
              <line className="crosshair" x1={activeLeft} x2={activeLeft} y1={pad.top} y2={pad.top + innerH} />
              {tooltipRows.map(({ s, p }) => (
                <circle key={s.id} className={`point-active tone-${s.tone}`} cx={activeLeft} cy={sy(p.y)} r={5} />
              ))}
            </g>
          )}
        </svg>
        {activeX && tooltipRows.length > 0 && (
          <div
            className="chart-tooltip"
            style={{
              left: Math.min(Math.max(activeLeft, 90), width - 90),
            }}
            role="status"
          >
            <p className="tooltip-title">{xFormat ? xFormat(activeX, tooltipRows) : activeX}</p>
            {tooltipRows.map(({ s, p, b }) => (
              <p key={s.id} className="tooltip-row">
                <span className={`legend-swatch tone-${s.tone} ${s.dashed ? "is-dashed" : ""}`} />
                <span>{s.label}</span>
                <strong>{yFormat(p.y, true)}</strong>
                {b && (
                  <span className="tooltip-sub">
                    95%: {yFormat(b.lo, true)} – {yFormat(b.hi, true)}
                  </span>
                )}
                {p.note && <span className="tooltip-sub">{p.note}</span>}
              </p>
            ))}
          </div>
        )}
      </div>
      {footnote && <figcaption className="chart-note">{footnote}</figcaption>}
      {xTitle && <p className="chart-axis-title chart-axis-x">{xTitle}</p>}
    </figure>
  );
}

/** Grouped vertical bars. data: [{ key, label, values: [{ id, value, tone, title }] }] */
export function BarChart({ data, height = 240, yFormat = (v) => v, legend, ariaLabel, yTitle, xTitle }) {
  const [wrapRef, width] = useWidth();
  const [active, setActive] = useState(null);
  const pad = { top: 14, right: 12, bottom: 34, left: 54 };
  const innerW = Math.max(width - pad.left - pad.right, 50);
  const innerH = height - pad.top - pad.bottom;
  const values = data.flatMap((d) => d.values.map((v) => v.value)).filter((v) => Number.isFinite(v));
  if (!values.length) return null;
  const yMin = Math.min(0, ...values) * 1.08;
  const yMax = Math.max(0, ...values) * 1.08 || 1;
  const sy = (v) => pad.top + innerH - ((v - yMin) / (yMax - yMin)) * innerH;
  const group = innerW / data.length;
  const perGroup = data[0]?.values.length || 1;
  const barW = Math.max(3, Math.min(22, (group * 0.72) / perGroup));
  const ticks = yTicks(yMin, yMax).filter((v) => v >= yMin && v <= yMax);

  return (
    <figure className="chart" ref={wrapRef}>
      {yTitle && <p className="chart-axis-title">{yTitle}</p>}
      {legend && (
        <div className="chart-legend" aria-hidden="true">
          {legend.map((l) => (
            <span key={l.id} className="legend-item">
              <span className={`legend-swatch tone-${l.tone}`} />
              {l.label}
            </span>
          ))}
        </div>
      )}
      <div className="chart-canvas">
        <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={ariaLabel}>
          {ticks.map((v) => (
            <g key={v}>
              <line className="grid" x1={pad.left} x2={width - pad.right} y1={sy(v)} y2={sy(v)} />
              <text className="axis-label" x={pad.left - 8} y={sy(v)} dy="0.32em" textAnchor="end">
                {yFormat(v)}
              </text>
            </g>
          ))}
          {data.map((d, gi) => {
            const gx = pad.left + gi * group + (group - barW * perGroup - 2 * (perGroup - 1)) / 2;
            return (
              <g
                key={d.key}
                onMouseEnter={() => setActive(gi)}
                onMouseLeave={() => setActive(null)}
                className={active === gi ? "bar-group is-active" : "bar-group"}
              >
                <rect className="bar-hit" x={pad.left + gi * group} y={pad.top} width={group} height={innerH} />
                {d.values.map((v, vi) =>
                  Number.isFinite(v.value) ? (
                    <rect
                      key={v.id}
                      className={`bar tone-${v.tone}`}
                      x={gx + vi * (barW + 2)}
                      y={Math.min(sy(v.value), sy(0))}
                      width={barW}
                      height={Math.max(1, Math.abs(sy(v.value) - sy(0)))}
                      rx={2}
                    >
                      <title>{v.title}</title>
                    </rect>
                  ) : null
                )}
                <text className="axis-label" x={pad.left + gi * group + group / 2} y={height - 10} textAnchor="middle">
                  {d.label}
                </text>
              </g>
            );
          })}
        </svg>
        {active !== null && (
          <div className="chart-tooltip" style={{ left: Math.min(Math.max(pad.left + active * group + group / 2, 90), width - 90) }} role="status">
            <p className="tooltip-title">{data[active].fullLabel || data[active].label}</p>
            {data[active].values.map((v) => (
              <p key={v.id} className="tooltip-row">
                <span className={`legend-swatch tone-${v.tone}`} />
                <span>{v.label}</span>
                <strong>{Number.isFinite(v.value) ? yFormat(v.value, true) : "Unavailable"}</strong>
              </p>
            ))}
          </div>
        )}
      </div>
      {xTitle && <p className="chart-axis-title chart-axis-x">{xTitle}</p>}
    </figure>
  );
}

export function Sparkline({ values, tone = "primary", width = 96, height = 28, label }) {
  const nums = (values || []).map(Number).filter(Number.isFinite);
  if (nums.length < 2) return null;
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = max - min || 1;
  const step = width / (nums.length - 1);
  const d = nums.map((v, i) => `${(i * step).toFixed(1)},${(height - 3 - ((v - min) / span) * (height - 6)).toFixed(1)}`).join("L");
  return (
    <svg className="spark" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
      <path className={`line tone-${tone}`} d={`M${d}`} />
    </svg>
  );
}

/**
 * Radar chart for normalised 0-100 scores.
 * axes: [{key, label}], series: [{id, label, tone, values: number[]}]
 */
export function RadarChart({ axes, series, size = 320, ariaLabel }) {
  const [wrapRef, width] = useWidth(size);
  const [active, setActive] = useState(null);
  if (!axes?.length || axes.length < 3 || !series?.length) return null;
  const dim = Math.max(220, Math.min(width, 420));
  const cx = dim / 2;
  const cy = dim / 2;
  const radius = dim / 2 - 64;
  const angle = (i) => (Math.PI * 2 * i) / axes.length - Math.PI / 2;
  const point = (i, value) => {
    const r = (Math.max(0, Math.min(100, value)) / 100) * radius;
    return [cx + r * Math.cos(angle(i)), cy + r * Math.sin(angle(i))];
  };
  const rings = [25, 50, 75, 100];

  return (
    <figure className="chart radar" ref={wrapRef}>
      <div className="chart-legend">
        {series.map((s) => (
          <button
            key={s.id}
            type="button"
            className={`legend-item legend-button ${active && active !== s.id ? "is-dim" : ""}`}
            onMouseEnter={() => setActive(s.id)}
            onMouseLeave={() => setActive(null)}
            onFocus={() => setActive(s.id)}
            onBlur={() => setActive(null)}
          >
            <span className={`legend-swatch tone-${s.tone}`} />
            {s.label}
          </button>
        ))}
      </div>
      <div className="chart-canvas radar-canvas">
        <svg width={dim} height={dim} viewBox={`0 0 ${dim} ${dim}`} role="img" aria-label={ariaLabel}>
          {rings.map((ring) => (
            <polygon
              key={ring}
              className="grid"
              fill="none"
              points={axes.map((_, i) => point(i, ring).join(",")).join(" ")}
            />
          ))}
          {axes.map((axis, i) => {
            const [x, y] = point(i, 100);
            const [lx, ly] = point(i, 118);
            const anchor = Math.abs(lx - cx) < 8 ? "middle" : lx > cx ? "start" : "end";
            return (
              <g key={axis.key}>
                <line className="grid" x1={cx} y1={cy} x2={x} y2={y} />
                <text className="axis-label radar-label" x={lx} y={ly} dy="0.32em" textAnchor={anchor}>
                  {axis.label.length > 18 ? `${axis.label.slice(0, 17)}…` : axis.label}
                  <title>{axis.label}</title>
                </text>
              </g>
            );
          })}
          {series.map((s) => (
            <polygon
              key={s.id}
              className={`radar-area tone-${s.tone} ${active && active !== s.id ? "is-dim" : ""}`}
              points={s.values.map((v, i) => point(i, v).join(",")).join(" ")}
            >
              <title>
                {s.label}: {axes.map((a, i) => `${a.label} ${Math.round(s.values[i])}`).join(", ")}
              </title>
            </polygon>
          ))}
        </svg>
      </div>
      <figcaption className="chart-note">Relative scores within the sector (0 = weakest, 100 = strongest).</figcaption>
    </figure>
  );
}
